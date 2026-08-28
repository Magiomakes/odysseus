"""save_to_project tool: parsing, project resolution, filing + indexing,
confinement, error surfaces, registration, and the ctx-passing dispatch path.

Style mirrors tests/test_projects_routes.py — the tool reuses that module's
helpers, so the same seams are monkeypatched (projects_routes._brain,
projects_routes.PERSONAL_DIR) plus the tool module's own _rag/_docs_manager.
NB: nothing here imports src.agent_loop (its import health is covered by the
integration branch, where fix/agent-loop-any-import is composed).
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.projects_routes as projects_routes
import src.agent_tools.project_tools as project_tools
from src.agent_tools.project_tools import SaveToProjectTool


def _run(coro):
    return asyncio.run(coro)


def _entity(eid=3, kind="project-instance", name="Williams Fellowship",
            meta=None, status="active", **extra):
    e = {"id": eid, "kind": kind, "name": name, "status": status,
         "meta": json.dumps(meta if meta is not None else {})}
    e.update(extra)
    return e


class FakeManager:
    def __init__(self):
        self.tracked = []
        self.excluded_files = set()

    def get_indexed_directories(self):
        return list(self.tracked)

    def add_directory(self, directory, *, index=True, owner=None):
        self.tracked.append(directory)
        assert index is False  # chunks already added in-process with owner

    def _save_excluded(self):
        pass


class FakeRag:
    def __init__(self):
        self.added = []

    def _split_into_chunks(self, text, chunk_size=1000):
        return [text]

    def add_document(self, chunk, metadata):
        self.added.append((chunk, metadata))
        return True


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_TOKEN", "tok_test")
    monkeypatch.setattr(projects_routes, "PERSONAL_DIR", str(tmp_path))
    mgr = FakeManager()
    rag = FakeRag()
    monkeypatch.setattr(project_tools, "_rag", lambda: rag)
    monkeypatch.setattr(project_tools, "_docs_manager", lambda: mgr)
    return SimpleNamespace(mgr=mgr, rag=rag, base=tmp_path)


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


_WORLD = {"entities": [
    _entity(eid=3, meta={"docs_dir": "projects/Williams Fellowship"}),
    _entity(eid=4, name="Board Game Jam",
            meta={"docs_dir": "~/Documents/EvenOdysseus/projects/Board Game Jam"}),
    _entity(eid=11, kind="area", name="car"),
]}


# ------------------------------------------------------------------ parsing

def test_parse_xml_tags():
    project, title, fmt, body = project_tools._parse_content(
        "<project>P</project>\n<title>notes</title>\n<format>txt</format>\n"
        "<content>\nhello\nworld\n</content>")
    assert (project, title, fmt) == ("P", "notes", "txt")
    assert body == "hello\nworld"


def test_parse_line_fallback_derives_title():
    project, title, fmt, body = project_tools._parse_content(
        "Williams Fellowship\n# Meeting notes\nline two")
    assert project == "Williams Fellowship" and title is None
    assert body == "# Meeting notes\nline two"
    assert project_tools._derive_title(body) == "Meeting notes"


# --------------------------------------------------------------- happy path

def test_files_indexes_registers_and_stamps(env, monkeypatch):
    # Legacy docs_dir (entity 4) → the tool must re-stamp the canonical form.
    calls = _fake_brain(monkeypatch, {
        "/api/self/world": _WORLD,
        "/api/self/world/4/docs-dir": {"ok": True},
    })
    out = _run(SaveToProjectTool().execute(
        "<project>Board Game Jam</project>\n<title>meeting-notes</title>\n"
        "<content>\n# Notes\nhi there\n</content>",
        {"session_id": "s1", "owner": "alice"}))
    assert out["exit_code"] == 0
    target = env.base / "projects" / "Board Game Jam" / "meeting-notes.md"
    assert target.read_text() == "# Notes\nhi there"
    chunk, meta = env.rag.added[0]
    assert meta["owner"] == "alice"
    assert meta["source"] == str(target)
    assert meta["stored_filename"] == "meeting-notes.md"
    assert meta["chunk_id"] == 0
    assert env.mgr.tracked == [
        str((env.base / "projects" / "Board Game Jam").resolve())]
    assert ("POST", "/api/self/world/4/docs-dir",
            {"docs_dir": "projects/Board Game Jam"}) in calls
    msg = out["output"]
    assert "Board Game Jam" in msg and "meeting-notes.md" in msg
    assert "1 chunk" in msg


def test_name_match_is_case_insensitive(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>williams fellowship</project><content>hi</content>",
        {"owner": "alice"}))
    assert out["exit_code"] == 0
    assert "Williams Fellowship" in out["output"]


def test_id_match(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>3</project><content>hi</content>", {"owner": "alice"}))
    assert out["exit_code"] == 0
    assert "Williams Fellowship" in out["output"]


def test_collision_gets_suffixed_name(env, monkeypatch):
    proj = env.base / "projects" / "Williams Fellowship"
    proj.mkdir(parents=True)
    (proj / "notes.md").write_text("old")
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>3</project><title>notes</title><content>new</content>",
        {"owner": "alice"}))
    assert out["exit_code"] == 0
    assert (proj / "notes.md").read_text() == "old"
    stored = env.rag.added[0][1]["stored_filename"]
    assert stored != "notes.md" and stored.endswith(".md")


def test_txt_format_and_stamp_failure_nonfatal(env, monkeypatch):
    async def fake(method, path, *, json_body=None, timeout=0):
        if path == "/api/self/world":
            return {"entities": [_entity(eid=7, name="P", meta={})]}
        raise HTTPException(502, "brain down")  # the docs-dir stamp

    monkeypatch.setattr(projects_routes, "_brain", fake)
    out = _run(SaveToProjectTool().execute(
        "<project>P</project><format>txt</format><title>t</title>"
        "<content>plain</content>", {"owner": "alice"}))
    assert out["exit_code"] == 0
    assert (env.base / "projects" / "P" / "t.txt").read_text() == "plain"


# ------------------------------------------------------------------- errors

def test_no_match_lists_available_projects(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>Fellowship</project><content>hi</content>", {"owner": "a"}))
    assert out["exit_code"] == 1
    assert "Williams Fellowship" in out["error"]
    assert "Board Game Jam" in out["error"]
    assert "car" not in out["error"]  # non-project entity never offered
    assert not env.rag.added


def test_unconfigured_bridge_is_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_TOKEN", raising=False)
    monkeypatch.setattr(projects_routes, "PERSONAL_DIR", str(tmp_path))
    monkeypatch.setattr(project_tools, "_rag", lambda: FakeRag())
    out = _run(SaveToProjectTool().execute(
        "<project>X</project><content>hi</content>", {}))
    assert out["exit_code"] == 1
    assert "not configured" in out["error"]


def test_empty_content_and_missing_project(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>3</project><content>   </content>", {"owner": "a"}))
    assert out["exit_code"] == 1 and "empty" in out["error"]
    out = _run(SaveToProjectTool().execute("", {"owner": "a"}))
    assert out["exit_code"] == 1 and "project" in out["error"].lower()


def test_oversized_content_rejected(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    big = "x" * (project_tools.MAX_CONTENT_BYTES + 1)
    out = _run(SaveToProjectTool().execute(
        f"<project>3</project><content>{big}</content>", {"owner": "a"}))
    assert out["exit_code"] == 1 and "too large" in out["error"]
    assert not env.rag.added


def test_no_rag_is_error(env, monkeypatch):
    monkeypatch.setattr(project_tools, "_rag", lambda: None)
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>3</project><content>hi</content>", {"owner": "a"}))
    assert out["exit_code"] == 1 and "RAG" in out["error"]


# -------------------------------------------------------------- confinement

def test_malicious_title_cannot_escape_folder(env, monkeypatch):
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    out = _run(SaveToProjectTool().execute(
        "<project>3</project><title>../../../evil</title><content>x</content>",
        {"owner": "a"}))
    assert out["exit_code"] == 0
    stored = env.rag.added[0][1]["source"]
    proj_root = str((env.base / "projects" / "Williams Fellowship").resolve())
    assert os.path.dirname(stored) == proj_root
    assert not (env.base.parent / "evil.md").exists()


def test_traversal_docs_dir_falls_back_to_derived_name(env, monkeypatch):
    world = {"entities": [_entity(eid=8, name="Evil",
                                  meta={"docs_dir": "projects/../../etc"})]}
    _fake_brain(monkeypatch, {"/api/self/world": world,
                              "/api/self/world/8/docs-dir": {"ok": True}})
    out = _run(SaveToProjectTool().execute(
        "<project>Evil</project><content>x</content>", {"owner": "a"}))
    assert out["exit_code"] == 0
    stored = env.rag.added[0][1]["source"]
    assert stored.startswith(str((env.base / "projects" / "Evil").resolve()))


# ------------------------------------------------------------- registration

def test_registered_everywhere():
    import src.agent_tools as at
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_security import (NON_ADMIN_BLOCKED_TOOLS,
                                   _PLAN_MODE_KNOWN_MUTATORS)
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    assert "save_to_project" in at.TOOL_HANDLERS
    assert "save_to_project" in at.TOOL_TAGS
    names = [s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS]
    assert "save_to_project" in names
    schema = next(s for s in FUNCTION_TOOL_SCHEMAS
                  if s["function"]["name"] == "save_to_project")
    assert schema["function"]["parameters"]["required"] == ["project", "content"]
    # Mutating + personal-store tool: blocked in plan mode and for non-admins.
    assert "save_to_project" in _PLAN_MODE_KNOWN_MUTATORS
    assert "save_to_project" in NON_ADMIN_BLOCKED_TOOLS
    assert "save_to_project" in BUILTIN_TOOL_DESCRIPTIONS


def test_function_call_converts_to_xml_block():
    from src.tool_schemas import function_call_to_tool_block
    blk = function_call_to_tool_block("save_to_project", json.dumps({
        "project": "Williams Fellowship", "title": "meeting-notes",
        "content": "hello"}))
    assert blk.tool_type == "save_to_project"
    assert "<project>Williams Fellowship</project>" in blk.content
    assert "<title>meeting-notes</title>" in blk.content
    assert "<content>hello</content>" in blk.content


# ------------------------------------------------- dispatch path (ctx-passing)

def test_execute_tool_block_passes_owner_ctx(env, monkeypatch):
    """The dispatcher must route save_to_project through the ctx-passing
    branch (owner in ctx → owner-scoped chunk metadata), NOT the ownerless
    dynamic_handlers catch-all."""
    import src.tool_execution as tool_execution
    from src.agent_tools import ToolBlock
    _fake_brain(monkeypatch, {"/api/self/world": _WORLD})
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda o: True)
    block = ToolBlock("save_to_project",
                      "<project>3</project><content>dispatched</content>")
    desc, result = _run(tool_execution.execute_tool_block(
        block, session_id="s1", owner="alice"))
    assert result["exit_code"] == 0, result
    assert desc.startswith("save_to_project:")
    assert env.rag.added[0][1]["owner"] == "alice"
