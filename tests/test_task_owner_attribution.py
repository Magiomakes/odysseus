"""Task CRUD must be attributed to the real owner behind a bearer token.

Regression test for the "api" pseudo-user siloing bug: a bearer ``ody_`` token
arrives as ``current_user == "api"`` (sandboxed so it can't wander into cookie
routes) but carries its minting owner on ``request.state.api_token_owner``. The
task routes' ``_owner`` helper must credit that real owner via ``effective_user``
so externally-created tasks — and the completion notifications that fan out to
``task.owner`` — surface in the owner's named-login UI instead of an invisible
"api" silo. A token with no owner must still fall back to ``get_current_user``
so the change never escalates privileges.
"""

import asyncio
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import routes.task_routes as task_routes
import core.database as cdb
from core.database import ScheduledTask

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
task_routes.SessionLocal = _TS


def _bearer_req(owner):
    """A bearer ody_ token request: sandboxed current_user, real owner stashed."""
    return SimpleNamespace(
        state=SimpleNamespace(
            current_user="api",
            api_token=True,
            api_token_owner=owner,
        )
    )


def _cookie_req(user):
    """A logged-in cookie session (no api_token on state)."""
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


def _get_task_endpoint():
    task_routes.SessionLocal = _TS
    router = task_routes.setup_task_routes(MagicMock())
    for route in router.routes:
        if str(getattr(route, "path", "")).endswith("/{task_id}") and "GET" in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError("GET /{task_id} not found")


def _seed_task(task_id, owner):
    db = _TS()
    try:
        db.add(ScheduledTask(
            id=task_id,
            owner=owner,
            name=task_id,
            prompt="do work",
            task_type="llm",
            trigger_type="schedule",
            schedule="once",
            status="active",
            output_target="notification",
        ))
        db.commit()
    finally:
        db.close()


def test_bearer_token_owner_can_see_own_task():
    """ody_ token minted by 'orion' resolves to orion, so orion's task is visible."""
    _seed_task("t-bearer-own", "orion")
    get_task = _get_task_endpoint()
    # No 403 => _owner resolved to orion (the token's owner) and matched task.owner.
    result = asyncio.run(get_task(_bearer_req("orion"), "t-bearer-own"))
    assert result["id"] == "t-bearer-own"
    # The API must expose the resolved owner so a bearer-token client can
    # confirm attribution rather than reading a missing field as "owner=None".
    assert result["owner"] == "orion"


def test_cookie_session_unchanged():
    """Cookie sessions resolve identically to get_current_user — a no-op for browsers."""
    _seed_task("t-cookie", "orion")
    get_task = _get_task_endpoint()
    result = asyncio.run(get_task(_cookie_req("orion"), "t-cookie"))
    assert result["id"] == "t-cookie"


def test_bearer_token_cannot_cross_owner_boundary():
    """A token minted by 'mallory' must not reach orion's task."""
    _seed_task("t-cross", "orion")
    get_task = _get_task_endpoint()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_task(_bearer_req("mallory"), "t-cross"))
    assert exc.value.status_code == 403


def test_ownerless_token_does_not_escalate():
    """A bearer token with no stashed owner falls back to the 'api' pseudo-user
    (no escalation) and is denied an owned task."""
    _seed_task("t-noowner", "orion")
    get_task = _get_task_endpoint()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_task(_bearer_req(None), "t-noowner"))
    assert exc.value.status_code == 403
