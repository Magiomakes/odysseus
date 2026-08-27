"""Tests for the memory-projection route (feat/memory-projection).

A minimal FastAPI app hosts the router with a real MemoryManager against a
temp dir; middleware fakes the auth middleware's request.state (bearer ody_
token minted by 'orin'). Exercises the reconcile contract: add, idempotent
re-put, update, delete-on-omission, prefix isolation, manual rows untouched,
and validation errors.
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.memory.memory_projection_routes import setup_memory_projection_routes
from src.memory import MemoryManager


OWNER = "orin"


def make_client(tmp_path, token_owner=OWNER, api_token=True):
    mm = MemoryManager(str(tmp_path))
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.api_token = api_token
        request.state.api_token_owner = token_owner
        request.state.current_user = None if api_token else token_owner
        return await call_next(request)

    class _Auth:
        is_configured = True

    app.state.auth_manager = _Auth()
    app.include_router(setup_memory_projection_routes(mm))
    return TestClient(app), mm


def _put(client, prefix="eo-self:", entries=()):
    return client.put("/api/memory/projection",
                      json={"prefix": prefix, "entries": list(entries)})


def test_add_then_idempotent_then_update_then_delete(tmp_path):
    client, mm = make_client(tmp_path)

    r = _put(client, entries=[
        {"key": "eo-self:insight-1", "text": "Prefers mornings for deep work"},
        {"key": "eo-self:qa-7", "text": "Q: Deep work hours? A: 7 to 10.",
         "category": "preference"},
    ])
    assert r.status_code == 200
    assert r.json()["added"] == 2 and r.json()["deleted"] == 0

    rows = mm.load(owner=OWNER)
    assert {m["projection_key"] for m in rows} == {"eo-self:insight-1", "eo-self:qa-7"}
    assert all(m["source"] == "even-odysseus" for m in rows)

    # Same payload again → pure no-op.
    r = _put(client, entries=[
        {"key": "eo-self:insight-1", "text": "Prefers mornings for deep work"},
        {"key": "eo-self:qa-7", "text": "Q: Deep work hours? A: 7 to 10.",
         "category": "preference"},
    ])
    assert r.json() == {**r.json(), "added": 0, "updated": 0, "deleted": 0, "kept": 2}

    # Text change → update; omission → delete.
    r = _put(client, entries=[
        {"key": "eo-self:insight-1", "text": "Protects 7-10am for deep work"},
    ])
    body = r.json()
    assert body["updated"] == 1 and body["deleted"] == 1
    rows = mm.load(owner=OWNER)
    assert len(rows) == 1
    assert rows[0]["text"] == "Protects 7-10am for deep work"


def test_manual_and_foreign_rows_untouched(tmp_path):
    client, mm = make_client(tmp_path)
    manual = mm.add_entry("Manual memory the operator typed", owner=OWNER)
    other_prefix = mm.add_entry("Other projection", owner=OWNER)
    other_prefix["projection_key"] = "other:1"
    foreign = mm.add_entry("Someone else's projected row", owner="alice")
    foreign["projection_key"] = "eo-self:insight-9"
    mm.save([manual, other_prefix, foreign])

    r = _put(client, entries=[])  # empty desired set: wipe MY eo-self:* rows
    assert r.status_code == 200
    assert r.json()["deleted"] == 0  # none of the three qualify

    texts = {m["text"] for m in mm.load_all()}
    assert texts == {"Manual memory the operator typed", "Other projection",
                     "Someone else's projected row"}


def test_validation(tmp_path):
    client, _ = make_client(tmp_path)
    assert _put(client, prefix="x", entries=[]).status_code == 400
    assert _put(client, entries=[{"key": "wrong:1", "text": "t"}]).status_code == 400
    assert _put(client, entries=[{"key": "eo-self:1", "text": ""}]).status_code == 400
    assert _put(client, entries=[
        {"key": "eo-self:1", "text": "a"},
        {"key": "eo-self:1", "text": "b"},
    ]).status_code == 400


def test_projection_registers_before_stock_memory_router(tmp_path):
    """Route-order regression: the stock router's PUT /api/memory/{memory_id}
    must NOT capture "projection" as a memory id (it 422s on the missing Form
    `text` field). Mount both routers in app.py's order and hit the route."""
    from routes.memory.memory_routes import setup_memory_routes

    mm = MemoryManager(str(tmp_path))
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.api_token = True
        request.state.api_token_owner = OWNER
        request.state.current_user = None
        return await call_next(request)

    class _Auth:
        is_configured = True

    app.state.auth_manager = _Auth()
    app.include_router(setup_memory_projection_routes(mm))       # mod first
    app.include_router(setup_memory_routes(mm, session_manager=None))
    client = TestClient(app)
    r = client.put("/api/memory/projection", json={
        "prefix": "eo-self:",
        "entries": [{"key": "eo-self:insight-1", "text": "hello"}]})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1


def test_unauthenticated_token_401(tmp_path):
    # A bearer token with no minting owner and no cookie user, with auth
    # configured, must be refused — it must not write owner-less rows.
    client, _ = make_client(tmp_path, token_owner=None, api_token=True)
    r = _put(client, entries=[{"key": "eo-self:1", "text": "t"}])
    assert r.status_code == 401
