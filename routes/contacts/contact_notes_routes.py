"""
contact_notes_routes.py — contact context notes + a token-reachable contacts
surface for trusted local services (the even-odysseus brain).

Why this exists (local mod, LOCAL-MODS.md `feat/contact-notes`):

1. **Notes.** The upstream contacts integration stores name/email/phone/address
   only — no free-text context. The even-odysseus CRM loop (ADR-0015) needs a
   place to remember who a person IS ("Delaney — roofer, prefers text, quote
   pending") and to append facts learned from the operator's email-draft edits.
   In CardDAV mode the note lives in the vCard's standard NOTE property, edited
   **surgically** on the raw card (upstream's `_update_contact` REBUILDS the
   vCard from name/emails/phones and would destroy NOTE, ORG, PHOTO and any
   other property another client wrote — we never call it). Without CardDAV the
   notes live in a `DATA_DIR/contact_notes.json` sidecar.

2. **Bearer-token access.** Upstream contact routes require an admin cookie
   session (`require_admin`); a bearer `ody_` token resolves to the sandboxed
   "api" pseudo-user and gets 403 — so the brain could not look up recipients
   at task-fire time. These routes authenticate via the `effective_user`
   pattern instead (same as the task/board mods): a bearer token minted by a
   real owner acts as that owner. The Odysseus contact store is install-wide
   (upstream gates it admin-only, not per-owner), so this widens access to
   token-minting owners — acceptable on this single-operator install and
   scoped entirely to this mod.

Modularity contract: self-contained in this file + one `include_router` line in
app.py. Delete both and Odysseus is stock. Upstream `contacts_routes.py` is
imported for its helpers, never patched.
"""

import json
import logging
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from routes.contacts.contacts_routes import (
    DATA_DIR,
    _carddav_configured,
    _create_contact,
    _fetch_contacts,
    _get_carddav_config,
    _resolve_resource_url,
    _vesc,
    _vunesc,
)
from src.auth_helpers import effective_user, get_current_user

logger = logging.getLogger(__name__)

MAX_NOTE_CHARS = 8000
_NOTE_LINE = re.compile(r"^(?:[A-Za-z0-9-]+\.)?NOTE(?:;[^:]*)?:(.*)$")


def _notes_path() -> Path:
    return Path(DATA_DIR) / "contact_notes.json"


def _principal(request: Request) -> str:
    """The acting human: a cookie user, or the owner behind a bearer token.
    Anonymous callers are rejected — except in the same no-auth modes the rest
    of the app honors (get_current_user returns None but middleware let the
    request through; in that single-user case '' is the principal)."""
    # Bearer token with no minting owner: never allow — effective_user would
    # fall back to the "api" sandbox pseudo-user, which must not reach the
    # install-wide contact store.
    if getattr(request.state, "api_token", False) and \
            not getattr(request.state, "api_token_owner", None):
        raise HTTPException(403, "token has no owner")
    user = effective_user(request)
    if user:
        return user
    # Cookie path: middleware already authenticated (or auth is disabled) —
    # mirror get_current_user's None-in-single-user-mode behavior.
    if get_current_user(request) is None and \
            getattr(request.app.state, "auth_manager", None) is not None and \
            getattr(request.app.state.auth_manager, "is_configured", False):
        raise HTTPException(401, "Not authenticated")
    return user or ""


# ── vCard NOTE surgery (pure; unit-tested) ─────────────────────────────────
# Operates on the raw card text: unfold RFC-6350 line folding (semantics
# preserved), replace/insert exactly the NOTE logical line, keep every other
# property byte-for-byte. This is what makes the edit safe next to ORG/PHOTO/
# properties written by other CardDAV clients.

def _unfold(text: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", text or "")


def _vcard_lines(raw: str) -> list:
    return [l for l in _unfold(raw).replace("\r\n", "\n").split("\n")
            if l.strip()]


def get_note_from_vcard(raw: str) -> str:
    for line in _vcard_lines(raw):
        m = _NOTE_LINE.match(line)
        if m:
            return _vunesc(m.group(1))
    return ""


def set_note_in_vcard(raw: str, note: str) -> str:
    lines = [l for l in _vcard_lines(raw) if not _NOTE_LINE.match(l)]
    end = next((i for i, l in enumerate(lines)
                if l.strip().upper() == "END:VCARD"), len(lines))
    if note:
        lines.insert(end, f"NOTE:{_vesc(note)}")
    return "\r\n".join(lines) + "\r\n"


# ── note storage (CardDAV NOTE property, or the local sidecar) ─────────────

def _carddav_auth(cfg):
    return (cfg["username"], cfg["password"]) if cfg["username"] else None


def _fetch_raw_vcard(uid: str):
    url = _resolve_resource_url(uid)
    cfg = _get_carddav_config()
    r = httpx.get(url, auth=_carddav_auth(cfg), timeout=10)
    if r.status_code != 200:
        raise HTTPException(404, f"contact {uid} not found on CardDAV "
                                 f"({r.status_code})")
    return url, r.text


def _put_raw_vcard(url: str, text: str) -> bool:
    cfg = _get_carddav_config()
    r = httpx.put(url, data=text.encode("utf-8"),
                  headers={"Content-Type": "text/vcard; charset=utf-8"},
                  auth=_carddav_auth(cfg), timeout=10)
    if r.status_code not in (200, 201, 204):
        logger.warning("contact-notes PUT returned %s: %s",
                       r.status_code, r.text[:200])
        return False
    return True


def _sidecar_load() -> dict:
    p = _notes_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("contact_notes.json unreadable", exc_info=True)
    return {}


def _sidecar_save(notes: dict) -> None:
    from core.atomic_io import atomic_write_json
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(_notes_path()), notes, indent=2)


def read_note(uid: str) -> str:
    if _carddav_configured():
        _, raw = _fetch_raw_vcard(uid)
        return get_note_from_vcard(raw)
    return str(_sidecar_load().get(uid) or "")


def write_note(uid: str, note: str, *, prefetched=None) -> bool:
    """`prefetched` is an optional (url, raw_vcard) pair from an earlier
    _fetch_raw_vcard in the same request — the append path reads and writes
    the same card, and fetching twice both doubled the CardDAV round-trips
    and widened the read-modify-write race window."""
    note = (note or "")[:MAX_NOTE_CHARS]
    if _carddav_configured():
        url, raw = prefetched if prefetched is not None else _fetch_raw_vcard(uid)
        return _put_raw_vcard(url, set_note_in_vcard(raw, note))
    notes = _sidecar_load()
    if note:
        notes[uid] = note
    else:
        notes.pop(uid, None)
    _sidecar_save(notes)
    return True


# ── routes ─────────────────────────────────────────────────────────────────

def setup_contact_notes_routes() -> APIRouter:
    router = APIRouter(prefix="/api/contact-notes", tags=["contact-notes"])

    @router.get("/search")
    async def search(request: Request, q: str = Query("")):
        """Upstream search (name/email substring, ≤10) with each match's
        context note attached — one round-trip for the brain's recipient
        lookup at task-fire time."""
        _principal(request)
        if not q:
            return {"results": []}
        q_lower = q.lower()
        results = []
        for c in _fetch_contacts():
            if q_lower in (c.get("name") or "").lower() or any(
                    q_lower in e.lower() for e in c.get("emails") or []):
                results.append(dict(c))
        results = results[:10]
        for c in results:
            c.pop("href", None)
            try:
                c["note"] = read_note(c["uid"]) if c.get("uid") else ""
            except HTTPException:
                c["note"] = ""
        return {"results": results}

    @router.post("/add")
    async def add(request: Request, data: dict):
        """Create a contact (name required; email optional) and return it.
        Dedupes by email via the upstream store's own logic."""
        _principal(request)
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be an object")
        name = str(data.get("name") or "").strip()
        email = str(data.get("email") or "").strip()
        if not name and not email:
            raise HTTPException(400, "name or email required")
        if not name:
            name = email.split("@")[0]
        for c in _fetch_contacts():
            if email and email.lower() in [e.lower()
                                           for e in c.get("emails") or []]:
                return {"success": True, "existing": True,
                        "contact": {k: v for k, v in c.items() if k != "href"}}
        ok = _create_contact(name, email)
        contact = None
        if ok:
            for c in _fetch_contacts(force=True):
                if (email and email.lower() in [e.lower() for e in
                                                c.get("emails") or []]) or \
                        (not email and c.get("name") == name):
                    contact = {k: v for k, v in c.items() if k != "href"}
                    break
        return {"success": bool(ok), "existing": False, "contact": contact}

    @router.get("/{uid}/note")
    async def get_note(request: Request, uid: str):
        _principal(request)
        return {"uid": uid, "note": read_note(uid)}

    @router.post("/{uid}/note")
    async def post_note(request: Request, uid: str, data: dict):
        """Body {"append": text} adds a line to the note (the learn-from-edit
        path); {"note": text} replaces it ("" clears)."""
        _principal(request)
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be an object")
        # One CardDAV fetch serves both the append-read and the write below.
        prefetched = _fetch_raw_vcard(uid) if _carddav_configured() else None
        if "append" in data:
            addition = str(data.get("append") or "").strip()
            if not addition:
                raise HTTPException(400, "append must be non-empty")
            existing = get_note_from_vcard(prefetched[1]) if prefetched else read_note(uid)
            note = f"{existing}\n{addition}".strip() if existing else addition
        elif "note" in data:
            note = str(data.get("note") or "")
        else:
            raise HTTPException(400, "provide 'append' or 'note'")
        ok = write_note(uid, note, prefetched=prefetched)
        return {"success": ok, "uid": uid, "note": note[:MAX_NOTE_CHARS]}

    return router
