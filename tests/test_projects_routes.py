"""Projects view: docs_dir resolution, confinement, proxy merge, upload/delete."""

import asyncio
import io
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.projects_routes as projects_routes


def _run(coro):
    # asyncio.run, not get_event_loop().run_until_complete: under full-suite
    # ordering an earlier test can close/unset the main loop (see
    # test_bridge_routes.py).
    return asyncio.run(coro)


def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _entity(eid=3, kind="project-instance", name="Williams Fellowship",
            meta=None, **extra):
    e = {"id": eid, "kind": kind, "name": name, "status": "active",
         "meta": json.dumps(meta if meta is not None else {})}
    e.update(extra)
    return e


class FakeManager:
    def __init__(self):
        self.tracked = []
        self.excluded_files = set()
        self.excluded_calls = []

    def get_indexed_directories(self):
        return list(self.tracked)

    def add_directory(self, directory, *, index=True, owner=None):
        self.tracked.append(directory)
        assert index is False  # chunks already added in-process with owner

    def exclude_file(self, path):
        self.excluded_calls.append(path)
        self.excluded_files.add(os.path.abspath(path))

    def _save_excluded(self):
        pass


class FakeRag:
    def __init__(self):
        self.added = []
        self.deleted = []

    def _split_into_chunks(self, text, chunk_size=1000):
        return [text]

    def add_document(self, chunk, metadata):
        self.added.append((chunk, metadata))
        return True

    def delete_by_source(self, source):
        self.deleted.append(source)
        return 2


class FakeUpload:
    def __init__(self, filename, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]


# ---------------------------------------------------------------- resolution

def test_canonical_docs_dir_passes_through():
    rel, needs = projects_routes._resolve_docs_rel(
        _entity(meta={"docs_dir": "projects/Williams Fellowship"}))
    assert rel == "projects/Williams Fellowship" and needs is False


def test_legacy_absolute_docs_dir_recovers_tail():
    # NB: the entity NAME differs from the folder — recovery must keep the
    # legacy pointer's target, not re-derive from the name.
    rel, needs = projects_routes._resolve_docs_rel(
        _entity(name="Innovation Sprint Fall 2026",
                meta={"docs_dir": "~/Documents/EvenOdysseus/projects/Innovation Sprint"}))
    assert rel == "projects/Innovation Sprint" and needs is True


def test_missing_docs_dir_derives_from_name():
    rel, needs = projects_routes._resolve_docs_rel(
        _entity(name="Board Game Jam", meta={}))
    assert rel == "projects/Board Game Jam" and needs is True


def test_traversal_in_stored_docs_dir_is_rejected_then_derived():
    rel, needs = projects_routes._resolve_docs_rel(
        _entity(name="Evil", meta={"docs_dir": "projects/../../etc"}))
    assert rel == "projects/Evil" and needs is True


def test_folder_name_sanitized_spaces_preserved():
    assert projects_routes._sanitize_folder_name("  My  Proj/2026: a\\b  ") == "My Proj2026 ab"


def test_valid_docs_dir_guard():
    ok = projects_routes._is_canonical_docs_dir
    assert ok("projects/X") and ok("projects/X/sub")
    assert not ok("projects") and not ok("other/X") and not ok("projects/..")
    assert not ok("/projects/X") and not ok("projects/") and not ok("projects\\X")


def test_docs_abs_confined(monkeypatch, tmp_path):
    monkeypatch.setattr(projects_routes, "PERSONAL_DIR", str(tmp_path))
    assert projects_routes._docs_abs("projects/X").startswith(str(tmp_path.resolve()))
    with pytest.raises(HTTPException):
        projects_routes._docs_abs("projects/../../etc")


# --------------------------------------------------------------------- routes

@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_test")
    monkeypatch.setattr(projects_routes, "PERSONAL_DIR", str(tmp_path))
    mgr = FakeManager()
    rag = FakeRag()
    monkeypatch.setattr(projects_routes, "get_rag_manager", lambda: rag)
    router = projects_routes.setup_projects_routes(mgr)
    return SimpleNamespace(router=router, mgr=mgr, rag=rag, base=tmp_path)


def _fake_brain(monkeypatch, responses, calls=None):
    calls = calls if calls is not None else []

    async def fake(method, path, *, json_body=None, timeout=0):
        calls.append((method, path, json_body))
        for prefix, resp in responses.items():
            if path == prefix:
                return resp
        raise HTTPException(404, "no entity")

    monkeypatch.setattr(projects_routes, "_brain", fake)
    return calls


def test_list_filters_projects_and_counts_files(env, monkeypatch):
    proj_dir = env.base / "projects" / "Williams Fellowship"
    proj_dir.mkdir(parents=True)
    (proj_dir / "a.md").write_text("hi")
    world = {"entities": [
        _entity(eid=3, meta={"docs_dir": "projects/Williams Fellowship"}),
        _entity(eid=11, kind="area", name="car"),
    ]}
    _fake_brain(monkeypatch, {"/api/self/world": world})
    ep = _endpoint(env.router, "GET", "/api/projects")
    out = _run(ep(owner="alice", _admin=None))
    assert [p["id"] for p in out["projects"]] == [3]
    p = out["projects"][0]
    assert p["file_count"] == 1
    assert p["docs_dir"] == "projects/Williams Fellowship"
    assert isinstance(p["meta"], dict)


def test_detail_merges_file_list(env, monkeypatch):
    proj_dir = env.base / "projects" / "Williams Fellowship"
    proj_dir.mkdir(parents=True)
    (proj_dir / "a.md").write_text("hello")
    _fake_brain(monkeypatch, {"/api/self/world/3": _entity(
        eid=3, meta={"docs_dir": "projects/Williams Fellowship"}, facets=[])})
    ep = _endpoint(env.router, "GET", "/api/projects/{project_id}")
    out = _run(ep(project_id=3, owner="alice", _admin=None))
    assert out["files"][0]["name"] == "a.md"
    assert out["files"][0]["size"] == 5
    assert out["file_count"] == 1


def test_detail_404_for_non_project(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world/11": _entity(eid=11, kind="area")})
    ep = _endpoint(env.router, "GET", "/api/projects/{project_id}")
    with pytest.raises(HTTPException) as exc:
        _run(ep(project_id=11, owner="alice", _admin=None))
    assert exc.value.status_code == 404


def test_upload_writes_indexes_registers_and_stamps(env, monkeypatch):
    # Legacy docs_dir → the upload must re-stamp the canonical relative form.
    calls = _fake_brain(monkeypatch, {
        "/api/self/world/4": _entity(
            eid=4, name="Board Game Jam",
            meta={"docs_dir": "~/Documents/EvenOdysseus/projects/Board Game Jam"}),
        "/api/self/world/4/docs-dir": {"ok": True},
    })
    ep = _endpoint(env.router, "POST", "/api/projects/{project_id}/files")
    out = _run(ep(project_id=4, files=[FakeUpload("notes.md", b"# hi there")],
                  owner="alice", _admin=None))
    assert out["ok"] is True
    assert out["uploaded"][0]["name"] == "notes.md"
    target = env.base / "projects" / "Board Game Jam" / "notes.md"
    assert target.read_bytes() == b"# hi there"
    # Indexed in-process with owner metadata
    chunk, meta = env.rag.added[0]
    assert meta["owner"] == "alice" and meta["source"] == str(target)
    # Folder registered with the manager
    assert env.mgr.tracked == [str((env.base / "projects" / "Board Game Jam").resolve())]
    # Canonical stamp fired
    assert ("POST", "/api/self/world/4/docs-dir",
            {"docs_dir": "projects/Board Game Jam"}) in calls
    assert out["docs_dir_stamped"] is True


def test_upload_stamp_failure_is_non_fatal(env, monkeypatch):
    async def fake(method, path, *, json_body=None, timeout=0):
        if path == "/api/self/world/4":
            return _entity(eid=4, name="Board Game Jam", meta={})
        raise HTTPException(502, "brain down")

    monkeypatch.setattr(projects_routes, "_brain", fake)
    ep = _endpoint(env.router, "POST", "/api/projects/{project_id}/files")
    out = _run(ep(project_id=4, files=[FakeUpload("a.md", b"x")],
                  owner="alice", _admin=None))
    assert out["ok"] is True and out["docs_dir_stamped"] is False


def test_upload_collision_gets_suffixed_name(env, monkeypatch):
    proj_dir = env.base / "projects" / "P"
    proj_dir.mkdir(parents=True)
    (proj_dir / "a.md").write_text("old")
    _fake_brain(monkeypatch, {
        "/api/self/world/9": _entity(eid=9, name="P",
                                     meta={"docs_dir": "projects/P"}),
    })
    ep = _endpoint(env.router, "POST", "/api/projects/{project_id}/files")
    out = _run(ep(project_id=9, files=[FakeUpload("a.md", b"new")],
                  owner="alice", _admin=None))
    stored = out["uploaded"][0]["name"]
    assert stored != "a.md" and stored.startswith("a-") and stored.endswith(".md")
    assert (proj_dir / "a.md").read_text() == "old"


def test_delete_removes_deindexes_excludes(env, monkeypatch):
    proj_dir = env.base / "projects" / "Williams Fellowship"
    proj_dir.mkdir(parents=True)
    target = proj_dir / "gone.md"
    target.write_text("bye")
    _fake_brain(monkeypatch, {"/api/self/world/3": _entity(
        eid=3, meta={"docs_dir": "projects/Williams Fellowship"})})
    ep = _endpoint(env.router, "DELETE", "/api/projects/{project_id}/files")
    out = _run(ep(project_id=3, name="gone.md", owner="alice", _admin=None))
    assert out["ok"] is True and out["deleted_from_disk"] is True
    assert out["removed_chunks"] == 2
    assert not target.exists()
    assert env.rag.deleted == [str(target.resolve())]
    assert env.mgr.excluded_calls == [str(target.resolve())]


def test_delete_rejects_traversal_and_missing(env, monkeypatch):
    (env.base / "projects" / "P").mkdir(parents=True)
    _fake_brain(monkeypatch, {"/api/self/world/9": _entity(
        eid=9, name="P", meta={"docs_dir": "projects/P"})})
    ep = _endpoint(env.router, "DELETE", "/api/projects/{project_id}/files")
    for bad in ("../secrets.env", "a/b.md", ".hidden"):
        with pytest.raises(HTTPException) as exc:
            _run(ep(project_id=9, name=bad, owner="alice", _admin=None))
        assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        _run(ep(project_id=9, name="nope.md", owner="alice", _admin=None))
    assert exc.value.status_code == 404


def test_unconfigured_is_503(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(projects_routes, "PERSONAL_DIR", str(tmp_path))
    router = projects_routes.setup_projects_routes(FakeManager())
    ep = _endpoint(router, "GET", "/api/projects")
    with pytest.raises(HTTPException) as exc:
        _run(ep(owner="alice", _admin=None))
    assert exc.value.status_code == 503
