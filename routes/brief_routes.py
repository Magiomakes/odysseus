"""
brief_routes.py — the Morning Brief: one aggregate read + two act verbs,
proxied to the even-odysseus brain service.

The nightly self-model loop (brain ADR-0013/0014) generates a daily report,
inferred insights, and ≤5 morning questions — but its output surface was a
txt file in a folder, so the loop never closed. This module gives the brain's
morning surfaces one glanceable pane inside Odysseus (static/js/brief.js):

    GET  /api/brief/status   config + liveness probe (pane hides when unset)
    GET  /api/brief/brief    ONE aggregate fetch: yesterday's report narrative
                             + live unconfirmed insights + pending questions
                             + review-pending count. Live endpoints, not the
                             persisted report snapshot — the report row is
                             INSERT OR IGNORE and goes stale for the day.
    POST /api/brief/insight  {id, disposition: confirm|dismiss} → the brain's
                             /api/self/insights/resolve (ADR-0016). Confirm is
                             the ADR-0009 provenance gate opening on a real
                             human action; neither verb fires anything.
    POST /api/brief/answer   {id, answer} or {id, disposition: dismiss} → the
                             brain's /api/self/questions/answer (ADR-0014 §5:
                             "I don't know yet" is first-class; a yes to a
                             promotion stages a Review card only).

Modularity contract (LOCAL-MODS.md): self-contained in this file + the
self-injecting static/js/brief.js UI; app.py's only hook is one include_router
line, index.html's one script tag. Delete all three and Odysseus is stock.

Config (.env / environment; unset = the pane hides itself) — shared with the
feat/bridge-review mod by design, one brain, one credential:
    BRIDGE_BASE_URL   brain service origin (default http://127.0.0.1:8765)
    BRIDGE_TOKEN      the brain's INGEST_TOKEN (server-to-server auth; the
                      browser never sees it — that is the point of proxying).

The pane polls nothing — it fetches on open and after actions only — so no
interactive_gate passive-prefix entry is needed for these paths.
"""

import asyncio
import datetime
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

_T_READ = 15.0
_T_RESOLVE = 60.0


def _base() -> str:
    return (os.environ.get("BRIDGE_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")


def _token() -> str:
    return os.environ.get("BRIDGE_TOKEN", "")


def _configured() -> bool:
    return bool(_token())


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


async def _brain(method: str, path: str, *, json_body=None,
                 timeout: float = _T_READ) -> dict:
    """One JSON round-trip to the brain service (same contract as
    bridge_routes._brain). Seam for tests (monkeypatch this)."""
    if not _configured():
        raise HTTPException(503, "brief not configured (set BRIDGE_TOKEN)")
    url = _base() + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=json_body,
                                        headers=_headers())
    except httpx.TimeoutException:
        raise HTTPException(504, f"brain timed out on {path}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"brain unreachable ({e.__class__.__name__})")
    if resp.status_code >= 400:
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", detail)
        except Exception:
            pass
        raise HTTPException(resp.status_code, detail)
    try:
        return resp.json()
    except Exception:
        raise HTTPException(502, f"brain returned non-JSON for {path}")



def _require_token_access(request: Request) -> None:
    """Bearer ody_ tokens must be owner-attributed and chat-scoped to drive
    these proxies: the mutating verbs resolve self-model insights/questions
    through the server's full-privilege BRIDGE_TOKEN (confused-deputy risk),
    and the brief itself is personal self-model content. Cookie sessions pass
    through untouched — AuthMiddleware already authenticated them. Mirrors
    the bridge_routes / model_routes token-gate idiom.
    """
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if "chat" not in scopes:
            raise HTTPException(403, "API token missing required scope: chat")
        if not getattr(request.state, "api_token_owner", None):
            raise HTTPException(403, "API token has no owner")


def setup_brief_routes() -> APIRouter:
    router = APIRouter(prefix="/api/brief", tags=["brief"])

    @router.get("/status")
    async def status(request: Request):
        """Config + liveness in one probe; the UI injects nothing when the
        brief is unconfigured and shows an offline state when the brain is
        down."""
        if not _configured():
            return {"configured": False, "ok": False}
        try:
            health = await _brain("GET", "/health")
        except HTTPException:
            return {"configured": True, "ok": False}
        return {"configured": True, "ok": bool(health.get("ok")),
                "brain_version": health.get("version")}

    @router.get("/brief")
    async def brief(request: Request, day: str | None = None):
        """The morning aggregate. `day` (YYYY-MM-DD) is the REPORT day and
        defaults to yesterday — the night's run analyzes the day that ended.
        Insights/questions/review are live state, deliberately NOT the report
        row's frozen snapshot."""
        _require_token_access(request)
        if day:
            try:
                datetime.date.fromisoformat(day)
            except ValueError:
                raise HTTPException(400, "day must be YYYY-MM-DD")
        else:
            day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

        async def _report():
            data = await _brain(
                "GET", f"/api/self/reports/daily?period_key={day}")
            reports = data.get("reports") or []
            return reports[0] if reports else None

        async def _insights():
            data = await _brain("GET", "/api/self/insights?confirmed=0")
            return data.get("insights") or []

        async def _questions():
            return await _brain("GET", "/api/self/questions")

        async def _review_pending():
            data = await _brain("GET", "/api/self/review")
            return len(data.get("review") or [])

        report, insights, questions, review_pending = await asyncio.gather(
            _report(), _insights(), _questions(), _review_pending())
        return {
            "day": day,
            "report": report,
            "insights": insights,
            "questions": questions.get("questions") or [],
            "question_budget": questions.get("budget") or 5,
            "review_pending": review_pending,
        }

    @router.post("/insight")
    async def insight(request: Request):
        _require_token_access(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        if not isinstance(body.get("id"), int):
            raise HTTPException(400, "id (integer) is required")
        if body.get("disposition") not in ("confirm", "dismiss"):
            raise HTTPException(400, "disposition must be confirm or dismiss")
        return await _brain(
            "POST", "/api/self/insights/resolve",
            json_body={"id": body["id"], "disposition": body["disposition"],
                       "via": "morning-brief"},
            timeout=_T_RESOLVE)

    @router.post("/answer")
    async def answer(request: Request):
        _require_token_access(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        if not isinstance(body.get("id"), int):
            raise HTTPException(400, "id (integer) is required")
        allowed: dict = {"id": body["id"]}
        answer_text = body.get("answer")
        if answer_text is not None:
            if not isinstance(answer_text, str) or len(answer_text) > 4000:
                raise HTTPException(400, "answer must be a string ≤ 4000 chars")
            allowed["answer"] = answer_text
        if body.get("disposition") == "dismiss":
            allowed["disposition"] = "dismiss"
        if "answer" not in allowed and "disposition" not in allowed:
            raise HTTPException(400, "answer text or disposition=dismiss required")
        return await _brain("POST", "/api/self/questions/answer",
                            json_body=allowed, timeout=_T_RESOLVE)

    return router
