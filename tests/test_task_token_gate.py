"""Bearer-token gate on task CRUD (fix/task-owner-attribution).

Owner attribution must not widen what a token can do:
  1. a token without the chat scope gets 403 (no task CRUD),
  2. an ownerless token gets 403 instead of degrading to the "api" silo,
  3. a token minted by an admin is still NOT admin for the shell-executing
     task actions (run_local / run_script / ssh_command) — a delegated
     credential must not unlock subprocess execution.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import task_routes


def _endpoint(router, path, method):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise AssertionError(f"no route {method} {path}")


def _token_request(scopes, owner="alice"):
    return SimpleNamespace(
        state=SimpleNamespace(
            current_user="api",
            api_token=True,
            api_token_owner=owner,
            api_token_scopes=list(scopes),
        ),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )


def _router():
    return task_routes.setup_task_routes(task_scheduler=SimpleNamespace())


def test_unscoped_token_cannot_touch_tasks():
    create = _endpoint(_router(), "/api/tasks", "POST")
    req = task_routes.TaskCreate(task_type="llm", prompt="hi", schedule="once")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create(_token_request(scopes=["documents:read"]), req))
    assert exc.value.status_code == 403
    assert "chat" in str(exc.value.detail)


def test_ownerless_token_rejected_not_siloed():
    create = _endpoint(_router(), "/api/tasks", "POST")
    req = task_routes.TaskCreate(task_type="llm", prompt="hi", schedule="once")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create(_token_request(scopes=["chat"], owner=None), req))
    assert exc.value.status_code == 403
    assert "owner" in str(exc.value.detail).lower()


def test_admin_minted_token_still_blocked_from_shell_actions(monkeypatch):
    # Even when the token's owner IS an admin, the token itself must not
    # pass the admin gate for shell-executing actions.
    monkeypatch.setattr(task_routes, "owner_has_admin_task_privileges", lambda user: True)
    create = _endpoint(_router(), "/api/tasks", "POST")
    req = task_routes.TaskCreate(task_type="action", action="run_local")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create(_token_request(scopes=["chat"], owner="alice"), req))
    assert exc.value.status_code == 403
    assert "admin" in str(exc.value.detail).lower()


def test_cookie_session_admin_still_allowed(monkeypatch):
    # The from_token gate must not regress the interactive admin path: a
    # cookie session (api_token falsy) with admin privileges passes the admin
    # gate and proceeds to the NEXT validation (missing schedule → 400),
    # rather than being 403'd.
    monkeypatch.setattr(task_routes, "owner_has_admin_task_privileges", lambda user: True)
    create = _endpoint(_router(), "/api/tasks", "POST")
    req = task_routes.TaskCreate(task_type="action", action="run_local")
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="root", api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create(request, req))
    assert exc.value.status_code == 400
    assert "schedule" in str(exc.value.detail).lower()
