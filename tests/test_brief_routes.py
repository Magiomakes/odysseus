"""Morning Brief: config gating, aggregate composition, action validation."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.brief_routes as brief_routes


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
    # asyncio.run, not get_event_loop().run_until_complete: under full-suite
    # ordering an earlier test can close/unset the main loop, and every _run
    # test then dies with 'There is no current event loop'.
    return asyncio.run(coro)


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_test")
    monkeypatch.setenv("BRIDGE_BASE_URL", "http://127.0.0.1:9")


@pytest.fixture()
def calls(monkeypatch):
    """Replace the brain seam; records (method, path, json_body) per call and
    plays back canned brain responses per path."""
    seen = []
    canned = {
        "/health": {"ok": True, "version": "abc1234"},
        "/api/self/insights?confirmed=0": {"insights": [{"id": 7, "text": "t"}]},
        "/api/self/questions": {"questions": [{"id": 3, "text": "q"}], "budget": 5},
        "/api/self/review": {"review": [{"queue_index": 0}, {"queue_index": 1}]},
    }

    async def fake_brain(method, path, *, json_body=None, timeout=0):
        seen.append((method, path, json_body))
        if path.startswith("/api/self/reports/daily"):
            return {"reports": [{"period_key": path[-10:], "narrative": "did things"}]}
        return canned.get(path, {"ok": True, "echo": path})

    monkeypatch.setattr(brief_routes, "_brain", fake_brain)
    return seen


def test_status_unconfigured(monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    router = brief_routes.setup_brief_routes()
    out = _run(_endpoint(router, "GET", "/api/brief/status")(_req()))
    assert out == {"configured": False, "ok": False}


def test_status_configured(configured, calls):
    router = brief_routes.setup_brief_routes()
    out = _run(_endpoint(router, "GET", "/api/brief/status")(_req()))
    assert out == {"configured": True, "ok": True, "brain_version": "abc1234"}


def test_brief_aggregates_live_surfaces(configured, calls):
    router = brief_routes.setup_brief_routes()
    out = _run(_endpoint(router, "GET", "/api/brief/brief")(_req(), day="2026-08-24"))
    assert out["day"] == "2026-08-24"
    assert out["report"]["narrative"] == "did things"
    assert [i["id"] for i in out["insights"]] == [7]
    assert [q["id"] for q in out["questions"]] == [3]
    assert out["question_budget"] == 5
    assert out["review_pending"] == 2
    paths = {p for _, p, _ in calls}
    assert "/api/self/insights?confirmed=0" in paths
    assert "/api/self/questions" in paths
    assert "/api/self/review" in paths
    assert any(p.startswith("/api/self/reports/daily") for p in paths)


def test_brief_defaults_to_yesterday(configured, calls):
    import datetime
    router = brief_routes.setup_brief_routes()
    out = _run(_endpoint(router, "GET", "/api/brief/brief")(_req(), day=None))
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    assert out["day"] == yesterday


def test_brief_rejects_bad_day(configured, calls):
    router = brief_routes.setup_brief_routes()
    with pytest.raises(HTTPException) as e:
        _run(_endpoint(router, "GET", "/api/brief/brief")(_req(), day="not-a-day"))
    assert e.value.status_code == 400


def test_insight_proxies_with_via(configured, calls):
    router = brief_routes.setup_brief_routes()
    _run(_endpoint(router, "POST", "/api/brief/insight")(
        _req({"id": 7, "disposition": "confirm"})))
    assert calls[-1] == ("POST", "/api/self/insights/resolve",
                         {"id": 7, "disposition": "confirm", "via": "morning-brief"})


@pytest.mark.parametrize("body", [
    {"disposition": "confirm"},                 # missing id
    {"id": "7", "disposition": "confirm"},      # non-int id
    {"id": 7, "disposition": "maybe"},          # bad disposition
    "nope",                                     # non-object
])
def test_insight_validation(configured, calls, body):
    router = brief_routes.setup_brief_routes()
    with pytest.raises(HTTPException) as e:
        _run(_endpoint(router, "POST", "/api/brief/insight")(_req(body)))
    assert e.value.status_code == 400
    assert not calls


def test_answer_proxies_text(configured, calls):
    router = brief_routes.setup_brief_routes()
    _run(_endpoint(router, "POST", "/api/brief/answer")(
        _req({"id": 3, "answer": "It's already launched"})))
    assert calls[-1] == ("POST", "/api/self/questions/answer",
                         {"id": 3, "answer": "It's already launched"})


def test_answer_proxies_dismiss(configured, calls):
    router = brief_routes.setup_brief_routes()
    _run(_endpoint(router, "POST", "/api/brief/answer")(
        _req({"id": 3, "disposition": "dismiss"})))
    assert calls[-1] == ("POST", "/api/self/questions/answer",
                         {"id": 3, "disposition": "dismiss"})


@pytest.mark.parametrize("body", [
    {"id": 3},                                  # neither answer nor dismiss
    {"id": 3, "answer": "x" * 4001},            # over cap
    {"id": "3", "answer": "hi"},                # non-int id
])
def test_answer_validation(configured, calls, body):
    router = brief_routes.setup_brief_routes()
    with pytest.raises(HTTPException) as e:
        _run(_endpoint(router, "POST", "/api/brief/answer")(_req(body)))
    assert e.value.status_code == 400
    assert not calls


def test_unconfigured_brief_is_503(monkeypatch):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    router = brief_routes.setup_brief_routes()
    with pytest.raises(HTTPException) as e:
        _run(_endpoint(router, "GET", "/api/brief/brief")(_req(), day="2026-08-24"))
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


def test_unscoped_token_rejected(configured, calls):
    router = brief_routes.setup_brief_routes()
    with pytest.raises(HTTPException) as exc:
        _run(_endpoint(router, "GET", "/api/brief/brief")(_token_req(scopes=["todos:read"])))
    assert exc.value.status_code == 403
    assert calls == []


def test_ownerless_token_rejected_on_mutation(configured, calls):
    router = brief_routes.setup_brief_routes()
    with pytest.raises(HTTPException) as exc:
        _run(_endpoint(router, "POST", "/api/brief/insight")(
            _token_req(scopes=["chat"], owner=None,
                       body={"id": 1, "disposition": "confirm"})))
    assert exc.value.status_code == 403
    assert calls == []
