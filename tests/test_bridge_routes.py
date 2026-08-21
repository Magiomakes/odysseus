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
