"""Projects view, world-model browse: computed obligation state (plain
arithmetic, no LLM), plain-speech lines, /world overview + person/area
detail routes. Sibling of test_projects_routes.py — the project-only
contracts there stay untouched."""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.projects_routes as projects_routes


def _run(coro):
    return asyncio.run(coro)


def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _entity(eid, kind, name, status="active", **extra):
    e = {"id": eid, "kind": kind, "name": name, "status": status,
         "meta": json.dumps(extra.pop("meta", {})),
         "updated_at": "2026-08-27T17:09:53"}
    e.update(extra)
    return e


def _ob(name, cadence_days=None, season="", last_done=None, importance=""):
    return {"name": name, "cadence_days": cadence_days, "season": season,
            "last_done": last_done, "importance": importance}


AUG = date(2026, 8, 28)
NOV = date(2026, 11, 5)
OCT = date(2026, 10, 20)


# ------------------------------------------------------- obligation status

def test_never_recorded_with_season_out_of_window():
    s = projects_routes._obligation_status(_ob("winter tires", season="11"), AUG)
    assert s["state"] == "never-recorded"
    assert s["line"] == "never recorded; comes due around November"


def test_season_due_in_month_and_month_before():
    for today in (NOV, OCT):
        s = projects_routes._obligation_status(_ob("winter tires", season="11"), today)
        assert s["state"] == "due"
        assert s["line"] == "due now; never recorded; comes due around November"


def test_season_done_recently_not_due():
    s = projects_routes._obligation_status(
        _ob("winter tires", season="11", last_done="2026-10-15"), NOV)
    assert s["state"] == "ok"
    assert s["line"] == "last done 2026-10-15; comes due around November"


def test_season_january_window_wraps_to_december():
    s = projects_routes._obligation_status(_ob("x", season="1"), date(2026, 12, 10))
    assert s["state"] == "due"


def test_no_schedule_known():
    s = projects_routes._obligation_status(_ob("service"), AUG)
    assert s["state"] == "no-schedule"
    assert s["line"] == "no schedule known"


def test_no_schedule_with_last_done():
    s = projects_routes._obligation_status(_ob("service", last_done="2026-05-01"), AUG)
    assert s["state"] == "no-schedule"
    assert s["line"] == "last done 2026-05-01; no schedule known"


def test_cadence_overdue():
    s = projects_routes._obligation_status(
        _ob("dentist", cadence_days=180, last_done="2026-01-01"), AUG)
    assert s["state"] == "due"
    assert s["line"] == "due now; last done 2026-01-01; supposed to happen every 6 months"


def test_cadence_on_track_gives_next_due_date():
    s = projects_routes._obligation_status(
        _ob("dentist", cadence_days=180, last_done="2026-07-01"), AUG)
    assert s["state"] == "ok"
    assert s["line"] == "last done 2026-07-01; next due around 2026-12-28"


def test_cadence_never_recorded():
    s = projects_routes._obligation_status(_ob("dentist", cadence_days=180), AUG)
    assert s["state"] == "never-recorded"
    assert s["line"] == "never recorded; supposed to happen every 6 months"


def test_cadence_phrase_humanizes():
    p = projects_routes._cadence_phrase
    assert p(365) == "every year" and p(730) == "every 2 years"
    assert p(30) == "every month" and p(180) == "every 6 months"
    assert p(7) == "every week" and p(14) == "every 2 weeks"
    assert p(10) == "every 10 days" and p(1) == "every day"


def test_rollup_lines():
    r = projects_routes._obligation_rollup(
        [_ob("winter tires", season="11"), _ob("service")], AUG)
    assert (r["total"], r["due_now"], r["never_recorded"]) == (2, 0, 2)
    assert r["line"] == "2 obligations, 2 never recorded"
    r = projects_routes._obligation_rollup(
        [_ob("winter tires", season="11"), _ob("service")], NOV)
    assert r["due_now"] == 1
    assert r["line"] == "2 obligations, 1 due now, 2 never recorded"
    r = projects_routes._obligation_rollup([], AUG)
    assert r["line"] == "nothing tracked yet"
    r = projects_routes._obligation_rollup(
        [_ob("dentist", cadence_days=180, last_done="2026-07-01")], AUG)
    assert r["line"] == "1 obligation, all on track"


# ------------------------------------------------------------ person lines

def test_person_line_priority():
    line = projects_routes._person_line
    assert line("not-approached", []) == "not yet reached out"
    assert line("reached-out", None) == "reached out — no reply yet"
    assert line("lapsed", []) == "gone quiet"
    assert line("something-else", []) == "something-else"  # verbatim fallback
    them = [{"loop_state": "waiting-on-them"}]
    me = [{"loop_state": "waiting-on-me"}, {"loop_state": "waiting-on-them"}]
    assert line("partnered", them) == "waiting on their reply"
    assert line("partnered", me) == "you owe them a reply"


def test_loop_line_composition():
    lp = {"loop_state": "waiting-on-them", "about": "the fellowship intro",
          "ts": "2026-08-20T10:00:00"}
    assert projects_routes._loop_line(lp) == \
        "waiting on their reply about the fellowship intro since 2026-08-20"
    assert projects_routes._loop_line({"loop_state": "waiting-on-me"}) == \
        "you owe them a reply"


# ------------------------------------------------------------------ routes

@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_test")
    monkeypatch.setattr(projects_routes, "PERSONAL_DIR", str(tmp_path))
    router = projects_routes.setup_projects_routes(SimpleNamespace())
    return SimpleNamespace(router=router)


def _fake_brain(monkeypatch, responses):
    async def fake(method, path, *, json_body=None, timeout=0):
        if path in responses:
            return responses[path]
        raise HTTPException(404, "no entity")

    monkeypatch.setattr(projects_routes, "_brain", fake)


def _world_responses():
    sydney = _entity(5, "person", "Sydney", status="not-approached")
    car = _entity(11, "area", "car")
    return {
        "/api/self/world": {"entities": [
            _entity(3, "project-instance", "Williams Fellowship"),
            sydney,
            _entity(6, "person", "Arvin", status="not-approached"),
            car,
        ]},
        "/api/self/world/5": {**sydney, "facets": [], "open_loops": [
            {"loop_state": "waiting-on-them", "about": "recruitment",
             "ts": "2026-08-20T09:00:00"}],
            "edges": [], "last_activity": "2026-08-27T17:09:53"},
        "/api/self/world/6": {**_entity(6, "person", "Arvin",
                                        status="not-approached"),
                              "facets": [], "open_loops": [], "edges": [],
                              "last_activity": "2026-08-27T17:09:53"},
        "/api/self/world/11": {**car, "facets": [], "edges": [],
                               "obligations": [
                                   _ob("winter tires", season="11"),
                                   _ob("service")],
                               "last_activity": "2026-08-27T17:09:53"},
    }


def test_world_overview_lists_people_and_areas(env, monkeypatch):
    _fake_brain(monkeypatch, _world_responses())
    ep = _endpoint(env.router, "GET", "/api/projects/world")
    out = _run(ep(owner="alice", _admin=None))
    assert [p["name"] for p in out["people"]] == ["Arvin", "Sydney"]
    sydney = out["people"][1]
    assert sydney["line"] == "waiting on their reply"  # loop beats status
    assert out["people"][0]["line"] == "not yet reached out"
    assert sydney["last_activity"] == "2026-08-27T17:09:53"
    assert [a["name"] for a in out["areas"]] == ["car"]
    car = out["areas"][0]
    assert car["obligations"]["never_recorded"] == 2
    assert car["line"].startswith("2 obligations")
    # Projects never leak into the browse groups.
    assert all(p["kind"] == "person" for p in out["people"])


def test_world_overview_detail_failure_degrades_to_list_row(env, monkeypatch):
    responses = _world_responses()
    del responses["/api/self/world/6"]  # Arvin's detail fetch fails
    _fake_brain(monkeypatch, responses)
    ep = _endpoint(env.router, "GET", "/api/projects/world")
    out = _run(ep(owner="alice", _admin=None))
    arvin = out["people"][0]
    assert arvin["name"] == "Arvin"
    assert arvin["line"] == "not yet reached out"    # status still speaks
    assert arvin["last_activity"] == "2026-08-27T17:09:53"  # updated_at fallback


def test_world_person_detail(env, monkeypatch):
    _fake_brain(monkeypatch, _world_responses())
    ep = _endpoint(env.router, "GET", "/api/projects/world/{entity_id}")
    out = _run(ep(entity_id=5, owner="alice", _admin=None))
    assert out["relationship_line"] == "not yet reached out"
    assert out["open_loops"][0]["line"] == \
        "waiting on their reply about recruitment since 2026-08-20"
    assert isinstance(out["meta"], dict)
    assert "files" not in out


def test_world_area_detail_computes_obligations(env, monkeypatch):
    _fake_brain(monkeypatch, _world_responses())
    ep = _endpoint(env.router, "GET", "/api/projects/world/{entity_id}")
    out = _run(ep(entity_id=11, owner="alice", _admin=None))
    by_name = {o["name"]: o for o in out["obligations"]}
    assert by_name["service"]["state"] == "no-schedule"
    assert by_name["service"]["line"] == "no schedule known"
    assert by_name["winter tires"]["line"].startswith("never recorded")
    assert out["rollup"]["total"] == 2


def test_world_detail_404_for_project_and_unknown(env, monkeypatch):
    _fake_brain(monkeypatch, _world_responses())
    ep = _endpoint(env.router, "GET", "/api/projects/world/{entity_id}")
    with pytest.raises(HTTPException) as exc:
        _run(ep(entity_id=3, owner="alice", _admin=None))  # a project
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        _run(ep(entity_id=999, owner="alice", _admin=None))
    assert exc.value.status_code == 404


def test_world_literal_path_registers_before_wildcard(env):
    """Starlette matches in registration order: /world must precede
    /{project_id} or the wildcard swallows the literal segment."""
    paths = [getattr(r, "path", "") for r in env.router.routes]
    assert paths.index("/api/projects/world") < paths.index("/api/projects/{project_id}")
