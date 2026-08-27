"""Captures bridge: config gating, proxy pass-through, input validation."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.bridge_routes as bridge_routes


def _req(body=None):
    r = SimpleNamespace(state=SimpleNamespace(current_user="alice"))
    if body is not None:
        async def _json():
            return body
        r.json = _json
    return r


def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_test")
    monkeypatch.setenv("BRIDGE_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture()
def calls(monkeypatch):
    """Replace the brain seam; records (method, path, json_body) per call."""
    seen = []

    async def fake_brain(method, path, *, json_body=None, timeout=0):
        seen.append((method, path, json_body))
        return {"ok": True, "echo": path}

    monkeypatch.setattr(bridge_routes, "_brain", fake_brain)
    return seen


def test_status_unconfigured(monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    router = bridge_routes.setup_bridge_routes()
    out = _run(_endpoint(router, "GET", "/api/bridge/status")(_req()))
    assert out == {"configured": False, "ok": False}


def test_status_configured_brain_up(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    out = _run(_endpoint(router, "GET", "/api/bridge/status")(_req()))
    assert out["configured"] is True and out["ok"] is True
    assert calls == [("GET", "/health", None)]


def test_review_proxies(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    _run(_endpoint(router, "GET", "/api/bridge/review")(_req()))
    assert calls == [("GET", "/api/self/review", None)]


def test_resolve_forwards_allowed_fields_only(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "POST", "/api/bridge/review/resolve")
    body = {"index": 7, "disposition": "confirm", "bucket": "manual",
            "evil": "field"}
    _run(ep(_req(body)))
    assert calls == [("POST", "/api/self/review/resolve",
                      {"index": 7, "disposition": "confirm", "bucket": "manual"})]


def test_resolve_requires_index_and_disposition(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "POST", "/api/bridge/review/resolve")
    with pytest.raises(HTTPException) as e:
        _run(ep(_req({"disposition": "confirm"})))
    assert e.value.status_code == 400
    assert calls == []


def test_session_detail_validates_id(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "GET", "/api/bridge/sessions/{session_id}")
    with pytest.raises(HTTPException) as e:
        _run(ep(_req(), session_id="../../etc/passwd"))
    assert e.value.status_code == 400
    assert calls == []
    _run(ep(_req(), session_id="2026-08-18_1358_future-makers"))
    assert calls == [("GET", "/api/sessions/2026-08-18_1358_future-makers", None)]


def test_unconfigured_review_is_503(monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    router = bridge_routes.setup_bridge_routes()
    with pytest.raises(HTTPException) as e:
        _run(_endpoint(router, "GET", "/api/bridge/review")(_req()))
    assert e.value.status_code == 503


def test_draft_feedback_forwards_whitelisted_fields(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "POST", "/api/bridge/draft-feedback")
    body = {"card_id": "c1", "title": "Email Delaney", "original": "Hi,",
            "edited": "Hi Delaney,", "session_id": "2026-08-21_0900",
            "evil": "field"}
    _run(ep(_req(body)))
    assert calls == [("POST", "/api/self/draft_feedback",
                      {"card_id": "c1", "title": "Email Delaney",
                       "original": "Hi,", "edited": "Hi Delaney,",
                       "session_id": "2026-08-21_0900"})]


def test_draft_feedback_requires_core_fields(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "POST", "/api/bridge/draft-feedback")
    with pytest.raises(HTTPException) as e:
        _run(ep(_req({"card_id": "c1", "original": "x"})))
    assert e.value.status_code == 400
    assert calls == []


def test_draft_feedback_bounds_lengths(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "POST", "/api/bridge/draft-feedback")
    with pytest.raises(HTTPException) as e:
        _run(ep(_req({"card_id": "c1", "original": "x" * 20001, "edited": "y"})))
    assert e.value.status_code == 400
    assert calls == []


def test_draft_feedback_unconfigured_is_503(monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    router = bridge_routes.setup_bridge_routes()
    ep = _endpoint(router, "POST", "/api/bridge/draft-feedback")
    with pytest.raises(HTTPException) as e:
        _run(ep(_req({"card_id": "c1", "original": "x", "edited": "y"})))
    assert e.value.status_code == 503


# ── bearer-token gate ────────────────────────────────────────────────────────

def _token_req(scopes, owner="alice", body=None):
    r = SimpleNamespace(state=SimpleNamespace(
        current_user="api", api_token=True,
        api_token_owner=owner, api_token_scopes=list(scopes)))
    if body is not None:
        async def _json():
            return body
        r.json = _json
    return r


def test_unscoped_token_rejected_on_reads_and_mutations(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    for method, path in (("GET", "/api/bridge/review"),
                         ("GET", "/api/bridge/sessions"),
                         ("POST", "/api/bridge/review/classify")):
        with pytest.raises(HTTPException) as exc:
            _run(_endpoint(router, method, path)(_token_req(scopes=["documents:read"])))
        assert exc.value.status_code == 403
    assert calls == []  # brain never touched


def test_ownerless_token_rejected(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    with pytest.raises(HTTPException) as exc:
        _run(_endpoint(router, "POST", "/api/bridge/review/resolve")(
            _token_req(scopes=["chat"], owner=None,
                       body={"index": 0, "disposition": "keep"})))
    assert exc.value.status_code == 403
    assert calls == []


def test_chat_scoped_owned_token_passes(configured, calls):
    router = bridge_routes.setup_bridge_routes()
    out = _run(_endpoint(router, "GET", "/api/bridge/review")(
        _token_req(scopes=["chat"], owner="alice")))
    assert out.get("ok") is True
    assert calls == [("GET", "/api/self/review", None)]
