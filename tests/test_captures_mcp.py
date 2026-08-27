"""Unit tests for the captures MCP server's tool handlers (feat/captures-mcp).

Same spirit as test_bridge_routes.py: the HTTP seam (`_http_get`) is
monkeypatched, so these exercise formatting, caps, part-selection, and error
paths without a running brain service.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_servers import captures_server as cs


RECORD = """# Stipend decision

## Summary
Decided the monthly stipend: $600.

## Session Highlights
- Stipend covers transit and meals.

## Transcript

[00:00] We went through the travel budget line by line.
[00:10] The stipend lands at six hundred dollars per month.

## Decisions (1)
- Stipend is $600/month.
"""

SESSION = {
    "id": "2026-06-30_0900_stipend",
    "meta": {"title": "Stipend decision", "started": "2026-06-30T09:00:00"},
    "transcript": "We went through the travel budget. The stipend is $600.",
    "record": RECORD,
}


def _patch(monkeypatch, responses):
    """Replace the HTTP seam with a lookup: path-prefix → payload."""
    calls = []

    def fake_get(path, params=None):
        calls.append((path, params or {}))
        for prefix, payload in responses.items():
            if path.startswith(prefix):
                return payload
        return {"error": f"unexpected path {path}"}

    monkeypatch.setattr(cs, "_http_get", fake_get)
    return calls


# ── search_captures ─────────────────────────────────────────────────────────

def test_search_formats_ranked_results(monkeypatch):
    calls = _patch(monkeypatch, {"/api/sessions/search": {
        "query": "stipend",
        "results": [{"id": "2026-06-30_0900_stipend", "title": "Stipend decision",
                     "date": "2026-06-30", "duration_min": 30,
                     "summary": "Decided the monthly stipend: $600.",
                     "snippet": "… the stipend lands at six hundred …",
                     "score": 1.2}],
    }})
    out = cs._tool_search({"query": "stipend"})
    assert "2026-06-30_0900_stipend" in out
    assert "Stipend decision" in out
    assert "six hundred" in out
    assert calls[0][1]["q"] == "stipend"


def test_search_requires_query():
    assert cs._tool_search({}).startswith("Error")


def test_search_clamps_limit(monkeypatch):
    calls = _patch(monkeypatch, {"/api/sessions/search": {"results": []}})
    cs._tool_search({"query": "x", "limit": 999})
    assert calls[0][1]["limit"] == 25


def test_search_no_results_suggests_alternatives(monkeypatch):
    _patch(monkeypatch, {"/api/sessions/search": {"results": []}})
    out = cs._tool_search({"query": "nonexistent"})
    assert "No sessions match" in out


def test_search_surfaces_api_error(monkeypatch):
    _patch(monkeypatch, {"/api/sessions/search": {"error": "HTTP 401 from captures API"}})
    assert "401" in cs._tool_search({"query": "x"})


# ── get_capture ─────────────────────────────────────────────────────────────

def test_get_record_strips_transcript_section(monkeypatch):
    _patch(monkeypatch, {"/api/sessions/": SESSION})
    out = cs._tool_get({"session_id": SESSION["id"]})
    assert "## Summary" in out
    assert "## Decisions" in out          # section AFTER transcript survives
    assert "## Transcript" not in out
    assert "[00:00]" not in out


def test_get_transcript_part(monkeypatch):
    _patch(monkeypatch, {"/api/sessions/": SESSION})
    out = cs._tool_get({"session_id": SESSION["id"], "part": "transcript"})
    assert "travel budget" in out
    assert "## Summary" not in out


def test_get_summary_part(monkeypatch):
    _patch(monkeypatch, {"/api/sessions/": SESSION})
    out = cs._tool_get({"session_id": SESSION["id"], "part": "summary"})
    assert "Decided the monthly stipend" in out
    assert "Session Highlights" in out
    assert "## Decisions" not in out


def test_get_truncates_with_notice(monkeypatch):
    big = dict(SESSION, transcript="word " * 5000)
    _patch(monkeypatch, {"/api/sessions/": big})
    out = cs._tool_get({"session_id": SESSION["id"], "part": "transcript",
                        "max_chars": 600})
    assert "TRUNCATED" in out
    assert len(out) < 1200


def test_get_requires_session_id():
    assert cs._tool_get({}).startswith("Error")


def test_get_rejects_bad_part():
    assert cs._tool_get({"session_id": "x", "part": "audio"}).startswith("Error")


# ── list_recent_captures ────────────────────────────────────────────────────

def test_recent_lists_and_limits(monkeypatch):
    sessions = [{"id": f"2026-08-0{i}_0900", "title": f"S{i}",
                 "started": f"2026-08-0{i}T09:00:00", "duration_s": 600,
                 "summary": f"summary {i}"} for i in range(1, 6)]
    _patch(monkeypatch, {"/api/sessions": {"sessions": sessions}})
    out = cs._tool_recent({"limit": 2})
    assert "S1" in out and "S2" in out and "S3" not in out


# ── get_day_digest ──────────────────────────────────────────────────────────

def test_day_requires_day():
    assert cs._tool_day({}).startswith("Error")


def test_day_passes_through(monkeypatch):
    calls = _patch(monkeypatch, {"/api/self/day/2026-08-20": {"day": "2026-08-20",
                                                             "sessions": []}})
    out = cs._tool_day({"day": "2026-08-20"})
    assert "2026-08-20" in out
    assert calls[0][0] == "/api/self/day/2026-08-20"


# ── get_operator_profile ────────────────────────────────────────────────────

def test_profile_returns_markdown(monkeypatch):
    _patch(monkeypatch, {"/api/self/profile": {
        "profile_markdown": "# Operator profile\n\n- Prefers mornings\n",
        "confirmed_insights": [], "answered_questions": []}})
    out = cs._tool_profile({})
    assert "Prefers mornings" in out


def test_profile_surfaces_unreachable(monkeypatch):
    _patch(monkeypatch, {"/api/self/profile": {
        "error": "captures API unreachable at http://127.0.0.1:8765"}})
    assert "unreachable" in cs._tool_profile({})


# ── call_tool dispatch ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_tool_dispatch_and_unknown(monkeypatch):
    _patch(monkeypatch, {"/api/self/profile": {"profile_markdown": "hi"}})
    res = await cs.call_tool("get_operator_profile", {})
    assert res[0].text == "hi"
    res = await cs.call_tool("nope", {})
    assert "Unknown tool" in res[0].text


@pytest.mark.asyncio
async def test_call_tool_exception_is_contained(monkeypatch):
    def boom(path, params=None):
        raise RuntimeError("kaput")
    monkeypatch.setattr(cs, "_http_get", boom)
    res = await cs.call_tool("get_operator_profile", {})
    assert "Error" in res[0].text and "kaput" in res[0].text


@pytest.mark.asyncio
async def test_tool_names_are_read_verbs():
    """Plan-mode's readonly heuristic passes tools whose names start with
    read verbs — every tool here must qualify (locked design decision)."""
    tools = await cs.list_tools()
    for t in tools:
        assert t.name.split("_")[0] in ("search", "get", "list", "read")
