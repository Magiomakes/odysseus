"""
bridge_routes.py — thin read/act proxy to the even-odysseus brain service.

The capture pipeline (glasses → ingest → whisper → extraction) lives in a
separate local service (the "brain", default http://127.0.0.1:8765) and owns
ALL of its data: session folders on disk, the review queue in the Self-Model
DB. This module makes those surfaces usable INSIDE Odysseus WITHOUT copying any
state: every route is a proxy, so the brain stays the single source of truth
and the phone plugin, the daily report, and these UIs can never disagree.

Consumers (ADR-0015 split): the "Captures" window (static/js/bridge.js) is
the recorded-sessions browser and uses only /sessions*; the /review* proxies
are consumed by the My Tasks board's Inbox section (feat/task-board mod) —
capture DECISIONS live on the board now. /draft-feedback forwards an
operator's email-draft edit to the brain's learn-from-edit endpoint.

Modularity contract (LOCAL-MODS.md): self-contained in this file + the
self-injecting static/js/bridge.js UI; app.py's only hook is one include_router
line, index.html's one script tag. Delete all three and Odysseus is stock.

Config (.env / environment; unset = the pane hides itself):
    BRIDGE_BASE_URL   brain service origin (default http://127.0.0.1:8765)
    BRIDGE_TOKEN      the brain's INGEST_TOKEN (server-to-server auth). The
                      browser never sees this token — that is the point of
                      proxying instead of calling the brain from the page.

Mutation surface: POST /review/resolve + /review/classify mirror the brain's
own contract (confirm routes a capture through the brain's bucket router —
manual → the My Tasks board via the brain's webhook sink, automatable → an
Odysseus agent-task; the human confirm stays the single gate, ADR-0011).
Exposure parity with board_routes: requests ride Odysseus's own page auth.
"""

import logging
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")

# Timeouts per call class: reads are quick; classify runs a local LLM over up
# to 10 cards and legitimately takes minutes on a cold model.
_T_READ = 15.0
_T_RESOLVE = 60.0
_T_CLASSIFY = 300.0
_T_AUDIO = 120.0


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
    """One JSON round-trip to the brain service. Raises HTTPException with the
    brain's status on a non-2xx so the UI sees real errors, and 502/504 when
    the brain is down/slow. Seam for tests (monkeypatch this)."""
    if not _configured():
        raise HTTPException(503, "bridge not configured (set BRIDGE_TOKEN)")
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
    these proxies. Mutating verbs ride the server's full-privilege
    BRIDGE_TOKEN (confused-deputy risk — the captures MCP server deliberately
    carries only the scoped read token for the same surface), and even reads
    expose recorded session content. Cookie sessions pass through untouched;
    AuthMiddleware already authenticated them. Mirrors the model_routes /
    codex_routes token-gate idiom.
    """
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if "chat" not in scopes:
            raise HTTPException(403, "API token missing required scope: chat")
        if not getattr(request.state, "api_token_owner", None):
            raise HTTPException(403, "API token has no owner")


def setup_bridge_routes() -> APIRouter:
    router = APIRouter(prefix="/api/bridge", tags=["bridge"])

    @router.get("/status")
    async def status(request: Request):
        """Config + liveness in one probe; the UI hides the pane when the
        bridge is unconfigured and shows a 'brain offline' state when down."""
        if not _configured():
            return {"configured": False, "ok": False}
        try:
            health = await _brain("GET", "/health")
        except HTTPException:
            return {"configured": True, "ok": False}
        return {"configured": True, "ok": bool(health.get("ok")),
                "brain_version": health.get("version")}

    @router.get("/review")
    async def review_list(request: Request):
        _require_token_access(request)
        return await _brain("GET", "/api/self/review")

    @router.post("/review/classify")
    async def review_classify(request: Request):
        _require_token_access(request)
        return await _brain("POST", "/api/self/review/classify", json_body={},
                            timeout=_T_CLASSIFY)

    @router.post("/review/resolve")
    async def review_resolve(request: Request):
        _require_token_access(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        allowed = {k: body[k] for k in ("index", "disposition", "bucket")
                   if k in body}
        if "index" not in allowed or "disposition" not in allowed:
            raise HTTPException(400, "index and disposition are required")
        return await _brain("POST", "/api/self/review/resolve",
                            json_body=allowed, timeout=_T_RESOLVE)

    @router.post("/draft-feedback")
    async def draft_feedback(request: Request):
        """Forward an edited email draft to the brain's learn-from-edit
        endpoint (ADR-0015): the brain Gemma-compares original vs edited and
        appends recipient facts to the Odysseus contact store. Slow (model
        call) — rides the classify-class timeout. Body is whitelisted and
        length-bounded; the edit itself was already saved by the board PATCH,
        so a failure here loses only the learning, never the edit."""
        _require_token_access(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(400, "body must be an object")
        allowed = {}
        for key, cap in (("card_id", 64), ("title", 500), ("original", 20000),
                         ("edited", 20000), ("session_id", 200)):
            if key in body:
                val = body[key]
                if not isinstance(val, str) or len(val) > cap:
                    raise HTTPException(400, f"{key} must be a string ≤ {cap} chars")
                allowed[key] = val
        for req_key in ("card_id", "original", "edited"):
            if req_key not in allowed:
                raise HTTPException(400, "card_id, original and edited are required")
        return await _brain("POST", "/api/self/draft_feedback",
                            json_body=allowed, timeout=_T_CLASSIFY)

    @router.get("/sessions")
    async def sessions(request: Request):
        _require_token_access(request)
        return await _brain("GET", "/api/sessions")

    @router.get("/sessions/{session_id}")
    async def session_detail(request: Request, session_id: str):
        _require_token_access(request)
        if not _SESSION_ID.match(session_id):
            raise HTTPException(400, "bad session id")
        return await _brain("GET", f"/api/sessions/{session_id}")

    @router.get("/sessions/{session_id}/audio")
    async def session_audio(request: Request, session_id: str):
        _require_token_access(request)
        """Stream the session's wav through so the pane's <audio> element works
        without exposing the brain token to the page."""
        if not _SESSION_ID.match(session_id):
            raise HTTPException(400, "bad session id")
        if not _configured():
            raise HTTPException(503, "bridge not configured")
        url = f"{_base()}/api/sessions/{session_id}/audio.wav"
        client = httpx.AsyncClient(timeout=_T_AUDIO)
        try:
            req = client.build_request("GET", url, headers=_headers())
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            raise HTTPException(502, f"brain unreachable ({e.__class__.__name__})")
        if upstream.status_code >= 400:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(upstream.status_code, "audio unavailable")

        async def _gen():
            try:
                async for chunk in upstream.aiter_bytes(64 * 1024):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(_gen(), media_type="audio/wav")

    return router
