"""Auto-archive sweep — finished one-off tasks flip to 'archived' after
task_archive_completed_days; recurring / recent / active tasks are untouched."""
import asyncio
import tempfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import core.database as cdb
from core.database import ScheduledTask
from src.task_scheduler import TaskScheduler

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _mk(db, *, name, status, schedule, days_old):
    t = ScheduledTask(
        id=f"t-{name}", name=name, prompt="p", owner="alice",
        status=status, schedule=schedule,
        updated_at=datetime.utcnow() - timedelta(days=days_old),
        created_at=datetime.utcnow() - timedelta(days=days_old),
    )
    db.add(t)
    return t


def test_sweep_archives_only_aged_completed_one_offs(monkeypatch):
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: 3 if k == "task_archive_completed_days" else d)

    db = _TS()
    try:
        db.query(ScheduledTask).delete()
        _mk(db, name="old-done-once", status="completed", schedule="once", days_old=5)
        _mk(db, name="fresh-done-once", status="completed", schedule="once", days_old=1)
        _mk(db, name="old-done-daily", status="completed", schedule="daily", days_old=5)
        _mk(db, name="old-active-once", status="active", schedule="once", days_old=5)
        db.commit()
    finally:
        db.close()

    sched = TaskScheduler.__new__(TaskScheduler)   # no init side effects needed
    sched._last_archive_sweep = 0.0
    asyncio.new_event_loop().run_until_complete(sched._archive_finished_sweep())

    db = _TS()
    try:
        got = {t.name: t.status for t in db.query(ScheduledTask).all()}
    finally:
        db.close()
    assert got == {
        "old-done-once": "archived",
        "fresh-done-once": "completed",
        "old-done-daily": "completed",
        "old-active-once": "active",
    }


def test_sweep_disabled_and_throttled(monkeypatch):
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: -1 if k == "task_archive_completed_days" else d)

    db = _TS()
    try:
        db.query(ScheduledTask).delete()
        _mk(db, name="old-done-once", status="completed", schedule="once", days_old=30)
        db.commit()
    finally:
        db.close()

    sched = TaskScheduler.__new__(TaskScheduler)
    sched._last_archive_sweep = 0.0
    loop = asyncio.new_event_loop()
    loop.run_until_complete(sched._archive_finished_sweep())   # -1 → disabled

    db = _TS()
    try:
        assert db.query(ScheduledTask).first().status == "completed"
    finally:
        db.close()

    # Hourly throttle: a second call inside the window is a no-op even if the
    # setting would now archive.
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: 0 if k == "task_archive_completed_days" else d)
    loop.run_until_complete(sched._archive_finished_sweep())
    db = _TS()
    try:
        assert db.query(ScheduledTask).first().status == "completed"
    finally:
        db.close()
