"""
captures_server.py

Read-only MCP server over the even-odysseus brain's captures + self-model read
API (docs/PLAN-captures-context.md Phase 2; brain-side surface: ADR-0017).
Gives the chat agent search/get access to the operator's recorded sessions
(glasses captures: transcripts, session records, day digests) and the
operator-profile document.

Security model (even-odysseus ADR-0013): this process carries ONLY the scoped
GET-only read token (`BRIDGE_READ_TOKEN`), never the full `BRIDGE_TOKEN` — the
chat model consumes untrusted transcripts and could be prompt-injected, so a
leak here exposes only data the model was already handed. Every tool is a GET;
tool names start with read verbs so plan-mode's readonly heuristic passes them.

Config from env (DB-registered MCP servers pass whatever Env the admin form
stored, so load .env defensively too):
    BRIDGE_BASE_URL    default http://127.0.0.1:8765
    BRIDGE_READ_TOKEN  the even-odysseus INGEST_READ_TOKEN value

Register via Settings → MCP → Add (stdio): command `<repo>/venv/bin/python`,
args `mcp_servers/captures_server.py`.
"""

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

try:  # defensively fold the repo .env in — explicit env still wins
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

BASE_URL = (os.environ.get("BRIDGE_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")
READ_TOKEN = os.environ.get("BRIDGE_READ_TOKEN", "").strip()

# Hard output caps — the brain's session detail embeds the full transcript
# (median 24 KB, max ~179 KB) and has no cap of its own, so the cap lives
# HERE, between the API and the model's context window.
MAX_CHARS_DEFAULT = 8000
MAX_CHARS_CEILING = 30000

server = Server("captures")


def _http_get(path: str, params: dict | None = None) -> dict:
    """GET BASE_URL+path with the scoped read token. Returns the parsed JSON
    dict, or {"error": ...}. The single HTTP seam — tests monkeypatch this."""
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {READ_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        return {"error": f"HTTP {e.code} from captures API"
                         + (f": {detail}" if detail else "")}
    except Exception as e:  # noqa: BLE001 — URLError, timeout, bad JSON
        return {"error": f"captures API unreachable at {BASE_URL} "
                         f"({type(e).__name__}: {e})"}


def _text(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _clip(text: str, max_chars: int) -> str:
    """Truncate with a loud notice — a silent cut reads as a complete text."""
    if len(text) <= max_chars:
        return text
    return (text[:max_chars]
            + f"\n\n[TRUNCATED at {max_chars} of {len(text)} characters — "
              f"call again with a larger max_chars, or use search_captures "
              f"to find the relevant part]")


def _strip_transcript_section(record: str) -> str:
    """Drop the record's embedded `## Transcript` section (it duplicates the
    transcript at ~2× the size); everything else stays verbatim."""
    lines = record.splitlines()
    out, skipping = [], False
    for ln in lines:
        if ln.strip().lower() == "## transcript":
            skipping = True
            continue
        if skipping and ln.startswith("## "):
            skipping = False
        if not skipping:
            out.append(ln)
    return "\n".join(out).strip()


def _summary_section(record: str) -> str:
    """The record's title + `## Summary` and `## Session Highlights` sections —
    the compact gist for part="summary"."""
    keep_heads = ("## summary", "## session highlights")
    lines = record.splitlines()
    out, keeping = [], False
    for ln in lines:
        if ln.startswith("# ") and not ln.startswith("## "):
            out.append(ln)
            continue
        if ln.startswith("## "):
            keeping = ln.strip().lower() in keep_heads
        if keeping:
            out.append(ln)
    return "\n".join(out).strip()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search_captures",
            description=(
                "Search the user's recorded sessions (glasses capture "
                "recordings, meeting transcripts, session records, voice "
                "notes) by keyword. Use when the user asks about a past "
                "conversation, meeting, recording, or capture — 'what did I "
                "say about X', 'when did we discuss Y', 'find the session "
                "about Z'. Returns ranked sessions with id, title, date, and "
                "a matching transcript snippet; follow up with get_capture "
                "for the full text."),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "Keywords to search for"},
                    "limit": {"type": "integer",
                              "description": "Max results (default 8, max 25)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_capture",
            description=(
                "Read one recorded session (capture/recording/meeting) by its "
                "session id — from search_captures or list_recent_captures. "
                "part='record' (default) is the structured session record: "
                "summary, highlights, decisions, tasks. part='transcript' is "
                "the raw spoken transcript ('what exactly did I say'). "
                "part='summary' is just the gist."),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string",
                                   "description": "The session folder id, e.g. "
                                                  "2026-07-27_1206_topic-slug"},
                    "part": {"type": "string",
                             "enum": ["record", "transcript", "summary"],
                             "description": "Which text to return (default record)"},
                    "max_chars": {"type": "integer",
                                  "description": f"Output cap (default "
                                                 f"{MAX_CHARS_DEFAULT}, max "
                                                 f"{MAX_CHARS_CEILING})"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="list_recent_captures",
            description=(
                "List the user's most recent recorded sessions (glasses "
                "captures, meeting recordings), newest first: id, title, "
                "date, duration, one-line summary. Use to browse what was "
                "recently recorded — 'what did I record today/this week', "
                "'latest meetings', 'recent voice sessions'."),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer",
                              "description": "Max sessions (default 10, max 50)"},
                },
            },
        ),
        Tool(
            name="get_day_digest",
            description=(
                "Read one day's compact digest of everything the user "
                "recorded: sessions with summaries, archived facts/ideas/"
                "decisions, tasks surfaced that day. Use for day-level recall "
                "— 'what happened on Tuesday', 'summarize my meetings "
                "yesterday', 'what did I capture on 2026-08-20'."),
            inputSchema={
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "The day, YYYY-MM-DD"},
                },
                "required": ["day"],
            },
        ),
        Tool(
            name="get_operator_profile",
            description=(
                "Read facts the user has personally confirmed about "
                "themselves and their work: confirmed insights and their "
                "verbatim answers to morning questions — about me, my "
                "preferences, my projects, my habits, how I work. Use when "
                "personalizing advice, drafts, or plans, or when asked 'what "
                "do you know about me / how I work'."),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


def _tool_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "Error: query is required"
    limit = max(1, min(25, int(args.get("limit") or 8)))
    data = _http_get("/api/sessions/search", {"q": query, "limit": limit})
    if data.get("error"):
        return f"Error: {data['error']}"
    results = data.get("results", [])
    if not results:
        return (f"No sessions match '{query}'. Try fewer or different "
                f"keywords, or list_recent_captures to browse.")
    lines = [f"{len(results)} session(s) matching '{query}' (best first):\n"]
    for r in results:
        dur = f", {r['duration_min']} min" if r.get("duration_min") else ""
        lines.append(f"- {r.get('id')} — {r.get('title')} ({r.get('date')}{dur})")
        if r.get("summary"):
            lines.append(f"  summary: {r['summary']}")
        if r.get("snippet"):
            lines.append(f"  snippet: {r['snippet']}")
    lines.append("\nUse get_capture(session_id) for the full record or transcript.")
    return "\n".join(lines)


def _tool_get(args: dict) -> str:
    sid = str(args.get("session_id") or "").strip()
    if not sid:
        return "Error: session_id is required"
    part = str(args.get("part") or "record").strip().lower()
    if part not in ("record", "transcript", "summary"):
        return "Error: part must be record, transcript, or summary"
    try:
        max_chars = int(args.get("max_chars") or MAX_CHARS_DEFAULT)
    except (TypeError, ValueError):
        max_chars = MAX_CHARS_DEFAULT
    max_chars = max(500, min(MAX_CHARS_CEILING, max_chars))
    data = _http_get(f"/api/sessions/{urllib.parse.quote(sid)}")
    if data.get("error"):
        return f"Error: {data['error']}"
    record = data.get("record") or ""
    if part == "transcript":
        body = data.get("transcript") or "(no transcript)"
    elif part == "summary":
        body = _summary_section(record) or "(no summary)"
    else:
        body = _strip_transcript_section(record) or "(no record)"
    meta = data.get("meta") or {}
    head = (f"Session {sid} — {meta.get('title', '')}"
            f" ({(meta.get('started') or '')[:16]}, part={part})\n\n")
    return head + _clip(body, max_chars)


def _tool_recent(args: dict) -> str:
    limit = max(1, min(50, int(args.get("limit") or 10)))
    data = _http_get("/api/sessions")
    if data.get("error"):
        return f"Error: {data['error']}"
    sessions = data.get("sessions", [])[:limit]
    if not sessions:
        return "No recorded sessions found."
    lines = [f"{len(sessions)} most recent session(s), newest first:\n"]
    for s in sessions:
        started = (s.get("started") or "")[:16]
        dur = (f", {round(s['duration_s'] / 60)} min"
               if s.get("duration_s") else "")
        summary = f" — {s['summary']}" if s.get("summary") else ""
        lines.append(f"- {s.get('id')} — {s.get('title')} ({started}{dur}){summary}")
    lines.append("\nUse get_capture(session_id) for the full record or transcript.")
    return "\n".join(lines)


def _tool_day(args: dict) -> str:
    day = str(args.get("day") or "").strip()
    if not day:
        return "Error: day (YYYY-MM-DD) is required"
    data = _http_get(f"/api/self/day/{urllib.parse.quote(day)}")
    if data.get("error"):
        return f"Error: {data['error']}"
    # The digest is already server-capped (~11 KB); pass it through as JSON.
    return _clip(json.dumps(data, ensure_ascii=False, indent=1),
                 MAX_CHARS_CEILING)


def _tool_profile(args: dict) -> str:
    data = _http_get("/api/self/profile")
    if data.get("error"):
        return f"Error: {data['error']}"
    return data.get("profile_markdown") or "(profile is empty)"


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    handlers = {
        "search_captures": _tool_search,
        "get_capture": _tool_get,
        "list_recent_captures": _tool_recent,
        "get_day_digest": _tool_day,
        "get_operator_profile": _tool_profile,
    }
    handler = handlers.get(name)
    if handler is None:
        return _text(f"Unknown tool: {name}")
    try:
        return _text(await asyncio.to_thread(handler, arguments or {}))
    except Exception as e:  # noqa: BLE001 — a tool error must not kill the server
        return _text(f"Error: {type(e).__name__}: {e}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
