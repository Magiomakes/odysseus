"""My Tasks board: CRUD scoping, handoff linking, ingest idempotency, reconcile pull."""

import asyncio
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
import routes.board_routes as board_routes
from core.database import ScheduledTask, TaskRun
from routes.board_routes import UserTask

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
board_routes.SessionLocal = _TS
board_routes.engine = _ENGINE


def _req(user="alice", body=None):
    r = SimpleNamespace(state=SimpleNamespace(current_user=user))
    if body is not None:
        async def _json():
            return body
        r.json = _json
    return r


def _router(scheduler=None):
    board_routes.SessionLocal = _TS
    if scheduler is None:
        scheduler = MagicMock()
        scheduler.run_task_now = AsyncMock(return_value=True)
    return board_routes.setup_board_routes(scheduler), scheduler


def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _clean_tables():
    db = _TS()
    try:
        db.query(UserTask).delete()
        db.query(TaskRun).delete()
        db.query(ScheduledTask).delete()
        db.commit()
    finally:
        db.close()


def test_create_and_list_scoped_by_owner():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    listing = _endpoint(router, "GET", "/api/board/tasks")

    _run(create(_req("alice"), board_routes.CardCreate(title="Alice card")))
    _run(create(_req("bob"), board_routes.CardCreate(title="Bob card")))

    out = _run(listing(_req("alice")))
    assert [t["title"] for t in out["tasks"]] == ["Alice card"]


def test_patch_denies_cross_owner():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    patch = _endpoint(router, "PATCH", "/api/board/tasks/{card_id}")

    card = _run(create(_req("alice"), board_routes.CardCreate(title="Mine")))
    with pytest.raises(HTTPException) as e:
        _run(patch(_req("bob"), card["id"], board_routes.CardPatch(title="Stolen")))
    assert e.value.status_code == 403


def test_done_sets_completed_at_and_backlog_clear():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    patch = _endpoint(router, "PATCH", "/api/board/tasks/{card_id}")

    card = _run(create(_req(), board_routes.CardCreate(title="T", planned_date="2026-07-22")))
    done = _run(patch(_req(), card["id"], board_routes.CardPatch(status="done")))
    assert done["completed_at"] is not None
    back = _run(patch(_req(), card["id"], board_routes.CardPatch(clear_planned_date=True)))
    assert back["planned_date"] is None


def test_handoff_creates_linked_run_now_task():
    router, scheduler = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    handoff = _endpoint(router, "POST", "/api/board/tasks/{card_id}/handoff")

    card = _run(create(_req(), board_routes.CardCreate(title="Draft the email", notes="to Russ")))
    out = _run(handoff(_req(), card["id"], board_routes.HandoffRequest()))
    assert out["ok"] and out["started"]
    scheduler.run_task_now.assert_awaited_once_with(out["scheduled_task_id"])

    db = _TS()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == out["scheduled_task_id"]).first()
        assert task is not None
        assert task.owner == "alice"
        assert "Draft the email" in task.prompt and "to Russ" in task.prompt
        assert task.output_target == "none"
        linked = db.query(UserTask).filter(UserTask.id == card["id"]).first()
        assert linked.status == "handed_off"
        assert linked.scheduled_task_id == task.id
    finally:
        db.close()

    # double handoff is a 409
    with pytest.raises(HTTPException) as e:
        _run(handoff(_req(), card["id"], board_routes.HandoffRequest()))
    assert e.value.status_code == 409


def test_reconcile_attaches_result_and_flips_to_in_review():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    handoff = _endpoint(router, "POST", "/api/board/tasks/{card_id}/handoff")
    listing = _endpoint(router, "GET", "/api/board/tasks")

    card = _run(create(_req(), board_routes.CardCreate(title="Research X")))
    out = _run(handoff(_req(), card["id"], board_routes.HandoffRequest(task_type="research")))

    from datetime import datetime
    db = _TS()
    try:
        db.add(TaskRun(id="run1", task_id=out["scheduled_task_id"],
                       started_at=datetime(2026, 7, 22, 12, 0),
                       finished_at=datetime(2026, 7, 22, 12, 5),
                       status="success", result="Findings: 42"))
        db.commit()
    finally:
        db.close()

    got = _run(listing(_req()))
    c = next(t for t in got["tasks"] if t["id"] == card["id"])
    assert c["status"] == "in_review"
    assert c["run_status"] == "success"
    assert c["result"] == "Findings: 42"


def test_reconcile_waits_for_running_run():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    handoff = _endpoint(router, "POST", "/api/board/tasks/{card_id}/handoff")
    listing = _endpoint(router, "GET", "/api/board/tasks")

    card = _run(create(_req(), board_routes.CardCreate(title="Slow job")))
    out = _run(handoff(_req(), card["id"], board_routes.HandoffRequest()))

    from datetime import datetime
    db = _TS()
    try:
        db.add(TaskRun(id="run-live", task_id=out["scheduled_task_id"],
                       started_at=datetime(2026, 7, 22, 12, 0),
                       status="running"))
        db.commit()
    finally:
        db.close()

    got = _run(listing(_req()))
    c = next(t for t in got["tasks"] if t["id"] == card["id"])
    assert c["status"] == "handed_off"
    assert c["result"] is None


def test_reconcile_rearms_aborted_run():
    """An aborted run (restart / foreground cancel) re-arms the task instead
    of surfacing stale progress text as an agent result."""
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    handoff = _endpoint(router, "POST", "/api/board/tasks/{card_id}/handoff")
    listing = _endpoint(router, "GET", "/api/board/tasks")

    card = _run(create(_req(), board_routes.CardCreate(title="Interrupted job")))
    out = _run(handoff(_req(), card["id"], board_routes.HandoffRequest(task_type="research")))

    from datetime import datetime
    db = _TS()
    try:
        db.add(TaskRun(id="ab1", task_id=out["scheduled_task_id"],
                       started_at=datetime(2026, 7, 28, 18, 54),
                       finished_at=datetime(2026, 7, 28, 18, 55),
                       status="aborted", error="Stopped by user",
                       result="Queued — waiting for Odysseus to be idle…"))
        db.commit()
    finally:
        db.close()

    got = _run(listing(_req()))
    c = next(t for t in got["tasks"] if t["id"] == card["id"])
    assert c["status"] == "handed_off"       # still with the agent, not "reviewed"
    assert c["result"] is None               # no fake result surfaced
    db = _TS()
    try:
        task = db.query(ScheduledTask).filter(
            ScheduledTask.id == out["scheduled_task_id"]).first()
        assert task.next_run is not None     # re-armed for the scheduler loop
    finally:
        db.close()


def test_reconcile_gives_up_after_repeated_aborts():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    handoff = _endpoint(router, "POST", "/api/board/tasks/{card_id}/handoff")
    listing = _endpoint(router, "GET", "/api/board/tasks")

    card = _run(create(_req(), board_routes.CardCreate(title="Cursed job")))
    out = _run(handoff(_req(), card["id"], board_routes.HandoffRequest()))

    from datetime import datetime
    db = _TS()
    try:
        for i in range(board_routes.MAX_ABORT_RETRIES + 1):
            db.add(TaskRun(id=f"ab{i}", task_id=out["scheduled_task_id"],
                           started_at=datetime(2026, 7, 28, 18, i),
                           finished_at=datetime(2026, 7, 28, 18, i, 30),
                           status="aborted", error="Stopped by user"))
        db.commit()
    finally:
        db.close()

    got = _run(listing(_req()))
    c = next(t for t in got["tasks"] if t["id"] == card["id"])
    assert c["status"] == "in_review"
    assert c["run_status"] == "error"
    assert "Stopped by user" in c["result"]


def test_ingest_bridge_payload_idempotent():
    router, _ = _router()
    ingest = _endpoint(router, "POST", "/api/board/ingest")
    listing = _endpoint(router, "GET", "/api/board/tasks")

    payload = {"text": "Run the Pitch it event on September 15th",
               "due": "2026-09-15", "source": "even-odysseus",
               "added_at": "2026-07-22T17:08:51Z"}
    first = _run(ingest(_req("alice", body=payload)))
    assert first["created"] == 1
    again = _run(ingest(_req("alice", body=payload)))
    assert again["created"] == 0 and again["skipped"] == 1

    got = _run(listing(_req("alice")))
    assert len(got["tasks"]) == 1
    t = got["tasks"][0]
    assert t["due"] == "2026-09-15"
    assert t["source"] == "even-odysseus"
    assert t["planned_date"] is None  # lands in backlog


def test_channel_horizon_estimate_roundtrip():
    router, _ = _router()
    create = _endpoint(router, "POST", "/api/board/tasks")
    patch = _endpoint(router, "PATCH", "/api/board/tasks/{card_id}")
    channels = _endpoint(router, "GET", "/api/board/channels")

    card = _run(create(_req(), board_routes.CardCreate(
        title="Plan jam", channel="#Boardgame Jam", horizon="quarter", estimate_minutes=90)))
    assert card["channel"] == "Boardgame Jam"  # hash stripped
    assert card["horizon"] == "quarter"
    assert card["estimate_minutes"] == 90

    # clear channel with "", clear estimate with 0, default horizon is "week"
    out = _run(patch(_req(), card["id"], board_routes.CardPatch(
        channel="", estimate_minutes=0, horizon="")))
    assert out["channel"] is None
    assert out["estimate_minutes"] is None
    assert out["horizon"] == "week"

    with pytest.raises(HTTPException):
        _run(patch(_req(), card["id"], board_routes.CardPatch(horizon="eventually")))

    _run(create(_req(), board_routes.CardCreate(title="a", channel="work")))
    _run(create(_req(), board_routes.CardCreate(title="b", channel="work")))
    got = _run(channels(_req()))
    assert {"name": "work", "open": 2} in got["channels"]


def test_ensure_columns_idempotent():
    # Running the migration against an already-migrated table is a no-op.
    board_routes._ensure_columns()
    board_routes._ensure_columns()


def test_ingest_accepts_list_and_skips_garbage():
    router, _ = _router()
    ingest = _endpoint(router, "POST", "/api/board/ingest")
    body = [
        {"text": "Real task", "source": "even-odysseus", "added_at": "2026-07-22T01:00:00Z"},
        {"text": "   ", "source": "even-odysseus"},
        {"nonsense": True},
        {"text": "Bad due survives", "due": "soon", "added_at": "2026-07-22T02:00:00Z"},
    ]
    out = _run(ingest(_req("alice", body=body)))
    assert out["created"] == 2
    assert out["skipped"] == 2

    db = _TS()
    try:
        bad_due = db.query(UserTask).filter(UserTask.title == "Bad due survives").first()
        assert bad_due is not None and bad_due.due is None
    finally:
        db.close()
