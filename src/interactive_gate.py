"""Foreground activity gate for background work.

Background tasks are allowed to run only after normal UI/API traffic has
settled. This keeps scheduled jobs and email pollers from competing with the
user opening Odysseus, Cookbook, email, documents, notes, or other panels.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import time

logger = logging.getLogger(__name__)


_ACTIVE_REQUESTS = 0
_LAST_ACTIVITY = 0.0
_LAST_BROWSER_ACTIVITY = 0.0
_COND: asyncio.Condition | None = None
_COND_LOOP: asyncio.AbstractEventLoop | None = None


def _enabled() -> bool:
    return os.getenv("BACKGROUND_TASK_FOREGROUND_GATE", "true").lower() not in {"0", "false", "no", "off"}


def _quiet_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_QUIET_MS", "1500")) / 1000.0)
    except Exception:
        return 1.5


def _max_wait_seconds() -> float:
    """0 means wait indefinitely until the UI is quiet."""
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_MAX_WAIT_SECONDS", "0")))
    except Exception:
        return 0.0


def _browser_active_seconds() -> float:
    """How long a visible Odysseus browser heartbeat blocks background tasks."""
    try:
        return max(0.0, float(os.getenv("BACKGROUND_TASK_BROWSER_ACTIVE_SECONDS", "45")))
    except Exception:
        return 45.0


def _capacity_setting(key: str, default):
    """Settings-backed knobs for the capacity gate (env-free so the operator
    can tune them from the UI). Falls back hard if settings are unavailable."""
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def system_under_load() -> bool:
    """True when the machine itself lacks headroom — other processes included.

    Load is the 1-min loadavg normalized per core; memory is psutil's
    available bytes (loadavg-only when psutil is missing). Either threshold
    set to 0 disables that check.
    """
    try:
        max_load = float(_capacity_setting("task_capacity_max_load_per_core", 2.0))
    except (TypeError, ValueError):
        max_load = 2.0
    if max_load > 0:
        try:
            if os.getloadavg()[0] / max(1, os.cpu_count() or 1) > max_load:
                return True
        except OSError:
            pass
    try:
        min_free_mb = int(_capacity_setting("task_capacity_min_free_mem_mb", 2048))
    except (TypeError, ValueError):
        min_free_mb = 2048
    if min_free_mb > 0:
        try:
            import psutil
            if psutil.virtual_memory().available < min_free_mb * 1024 * 1024:
                return True
        except Exception:
            pass
    return False


# even-odysseus live-session signal, cached so gate polls don't hammer the
# brain service. {"t": monotonic-of-last-probe, "busy": last-answer}.
_PIPELINE_CACHE = {"t": 0.0, "busy": False}
_PIPELINE_CACHE_SECONDS = 5.0


async def external_pipeline_busy() -> bool:
    """True while a worn session is recording or its whisper/Gemma
    post-processing is in flight (even-odysseus ingest /health `busy`).

    This is the HARD tier of the capacity gate: a live session always wins
    and no max-wait ever overrides it. The brain being down or unreachable
    means not busy — never block on an absent service.
    """
    now = time.monotonic()
    if now - _PIPELINE_CACHE["t"] < _PIPELINE_CACHE_SECONDS:
        return _PIPELINE_CACHE["busy"]
    base = (os.environ.get("BRIDGE_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")
    busy = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=0.5) as client:
            resp = await client.get(f"{base}/health")
            busy = bool(resp.json().get("busy"))
    except Exception as e:
        # Fail-open by design, but not SILENTLY: a BRIDGE_BASE_URL typo or a
        # dead brain service would otherwise disable the hard block with no
        # trace. Rate-limited so the 1s-poll loop can't flood the log.
        if now - _PIPELINE_CACHE.get("warn_t", 0.0) > 600:
            _PIPELINE_CACHE["warn_t"] = now
            logger.warning(
                "capacity gate: brain /health probe failed (%s: %s) — "
                "treating pipeline as not-busy (fail-open)",
                type(e).__name__, e)
        busy = False
    _PIPELINE_CACHE["t"] = now
    _PIPELINE_CACHE["busy"] = busy
    return busy


async def wait_for_capacity(label: str = "", max_wait: float = 600.0) -> bool:
    """Gate for explicit user tasks (board handoffs): start when the machine
    has headroom, not when the UI is untouched.

    Tiered blockers:
    - HARD (max_wait never applies): external_pipeline_busy() — a live
      even-odysseus session or its post-processing owns the machine.
    - SOFT (max_wait can override): an active chat/agent stream, or
      system_under_load(). The soft budget only burns while not hard-blocked.

    UI presence (in-flight requests, browser heartbeat) deliberately does NOT
    block here: an open-but-idle tab is not competition for compute.
    Returns True if the caller had to wait at all.
    """
    if not _enabled():
        return False
    waited = False
    soft_budget = max_wait if max_wait and max_wait > 0 else None
    poll = 1.0
    while True:
        hard = await external_pipeline_busy()
        if not hard:
            if not (_has_active_chat_stream() or system_under_load()):
                return waited
            if soft_budget is not None:
                soft_budget -= poll
                if soft_budget <= 0:
                    return waited
        waited = True
        await asyncio.sleep(poll)


def _condition() -> asyncio.Condition:
    global _COND, _COND_LOOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _COND is None or _COND_LOOP is not loop:
        _COND = asyncio.Condition()
        _COND_LOOP = loop
    return _COND


_PASSIVE_EXACT_PATHS = {
    "/api/activity/heartbeat",
    "/api/client-perf",
    "/api/tasks/notifications",
    "/api/research/active",
    "/api/email/urgency-state",
    # Same class of automatic badge poll as urgency-state above: every open
    # tab fires it on a timer whether or not the user is present. An idle
    # phone tab's 30-min poll was cancelling agent handoff runs mid-flight
    # ("Stopped by user" with nobody at the keyboard).
    "/api/email/unread-state",
}

_PASSIVE_PREFIXES = (
    "/api/chat/stream_status",
    "/api/health",
    "/api/prefs",
    # The My Tasks board polls while agents work; its reads/edits never touch
    # the model, and treating them as foreground would cancel the very agent
    # runs the board exists to watch (watch the worker → kill the worker).
    "/api/board/",
    # The board's Inbox section and the Captures window poll these bridge
    # proxies on the same cadence as /api/board/ — same watch-the-worker
    # problem (a review poll cancelled the nightly question agent).
    "/api/bridge/",
)


def should_track_interactive_request(path: str, method: str = "GET") -> bool:
    if not _enabled():
        return False
    if (method or "").upper() == "OPTIONS":
        return False
    if path in _PASSIVE_EXACT_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in _PASSIVE_PREFIXES):
        return False
    # Notes READS fire on background timers from every open tab (the due-badge
    # refresh every 5 min and the calendar-reminder sweep both GET /api/notes)
    # — automatic polls, not user presence. Writes stay foreground: someone
    # editing a note really is at the keyboard.
    if (method or "").upper() == "GET" and (
            path == "/api/notes" or path.startswith("/api/notes/")):
        return False
    return True


async def mark_browser_activity() -> None:
    """Record that an authenticated browser tab is visibly using Odysseus."""
    global _LAST_BROWSER_ACTIVITY
    if not _enabled():
        return
    cond = _condition()
    async with cond:
        _LAST_BROWSER_ACTIVITY = time.monotonic()
        cond.notify_all()


def _has_recent_browser_activity(now: float | None = None) -> bool:
    ttl = _browser_active_seconds()
    if ttl <= 0 or _LAST_BROWSER_ACTIVITY <= 0:
        return False
    return ((now if now is not None else time.monotonic()) - _LAST_BROWSER_ACTIVITY) < ttl


def has_foreground_activity(now: float | None = None) -> bool:
    """Return True when foreground browser/model work should stop background jobs.

    Passive polling endpoints are excluded by should_track_interactive_request,
    so active/recent request tracking is safe to use here. This matters during
    initial page load: the heartbeat may not have landed yet, but the user is
    already waiting on real UI requests.
    """
    if not _enabled():
        return False
    t = now if now is not None else time.monotonic()
    if _ACTIVE_REQUESTS > 0:
        return True
    if _LAST_ACTIVITY > 0 and (t - _LAST_ACTIVITY) < _quiet_seconds():
        return True
    return _has_recent_browser_activity(t) or _has_active_chat_stream()


def _has_active_chat_stream() -> bool:
    """Best-effort check for foreground model work that outlives HTTP requests.

    Chat/agent streams are detached from the browser SSE so a stream can keep
    running after the request that started it has returned. Background LLM
    tasks must still wait for those runs; otherwise helpers like email
    auto-translate compete with the user's active chat on the same local model.
    """
    try:
        from routes import chat_routes as _chat_routes
        active_streams = getattr(_chat_routes, "_active_streams", {}) or {}
        if active_streams:
            return True
    except Exception:
        pass
    try:
        from src import agent_runs
        runs = getattr(agent_runs, "_RUNS", {}) or {}
        return any(getattr(run, "status", None) == "running" for run in runs.values())
    except Exception:
        return False


@asynccontextmanager
async def track_interactive_request(path: str = "", method: str = ""):
    global _ACTIVE_REQUESTS, _LAST_ACTIVITY
    if not _enabled():
        yield
        return

    cond = _condition()
    async with cond:
        _ACTIVE_REQUESTS += 1
        _LAST_ACTIVITY = time.monotonic()
        cond.notify_all()
    try:
        yield
    finally:
        async with cond:
            _ACTIVE_REQUESTS = max(0, _ACTIVE_REQUESTS - 1)
            _LAST_ACTIVITY = time.monotonic()
            cond.notify_all()


async def wait_for_interactive_quiet(label: str = "",
                                     max_wait_override: float | None = None) -> bool:
    """Wait until foreground requests have stopped for the configured window.

    Returns True if the caller had to wait at all. The label is intentionally
    only for future logging/debugging so callers can keep their code simple.
    max_wait_override (seconds; 0 = wait forever) replaces the
    BACKGROUND_TASK_MAX_WAIT_SECONDS env default — on expiry the wait simply
    returns, so callers that must not run while the world is busy should
    re-check has_foreground_activity()/external_pipeline_busy() and defer.
    """
    if not _enabled():
        return False

    quiet = _quiet_seconds()
    max_wait = (_max_wait_seconds() if max_wait_override is None
                else max(0.0, float(max_wait_override)))
    deadline = time.monotonic() + max_wait if max_wait > 0 else None
    cond = _condition()
    waited = False

    while True:
        async with cond:
            now = time.monotonic()
            quiet_remaining = quiet - (now - _LAST_ACTIVITY)
            active_stream = _has_active_chat_stream()
            browser_active = _has_recent_browser_activity(now)
            if _ACTIVE_REQUESTS <= 0 and quiet_remaining <= 0 and not active_stream and not browser_active:
                return waited

            waited = True
            timeout = 0.25 if (_ACTIVE_REQUESTS > 0 or active_stream or browser_active) else min(max(quiet_remaining, 0.05), 0.5)
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    return waited
                timeout = min(timeout, remaining)
            try:
                await asyncio.wait_for(cond.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
