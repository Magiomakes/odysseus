"""
memory_projection_routes.py — declarative, token-reachable memory projection
for trusted local services (the even-odysseus brain).

Why this exists (local mod, LOCAL-MODS.md `feat/memory-projection`):

The even-odysseus self-model projects operator-CONFIRMED content (confirmed
insights + answered morning Q&A) into Odysseus's native memory, so the chat
AI knows those facts the way it knows anything it remembers — ambient prompt
injection via ChatProcessor (pinned + RAG-retrieved), no tool call needed
(PLAN-captures-context Phase 4; sanctioned by even-odysseus ADR-0009 §4: the
operator's Confirm is exactly the event that licenses projection).

The stock memory REST can't serve that writer: `POST /api/memory/add` calls
`require_privilege` → `require_user`, which 403s every bearer API token, and
PUT/DELETE resolve a token to the sandboxed "api" pseudo-user, whose owner
check can never match the operator's rows. So — same pattern as
`feat/contact-notes` — this route authenticates via `effective_user`: a
bearer `ody_` token minted by a real owner acts as that owner.

One verb, full reconcile: `PUT /api/memory/projection` carries the COMPLETE
desired set for a key-prefix; rows are upserted by `projection_key` and rows
whose key disappeared are deleted. The source of truth stays in the brain's
self-model DB; the copy here is derived, and deletions propagate (dismissing
an insight removes it from memory on the next sync).

Safety: the route only ever touches rows that (a) belong to the caller and
(b) carry a `projection_key` starting with the request's prefix. Manual
memories (no projection_key) are unreachable from here, whatever the payload.

Modularity contract: self-contained in this file + one `include_router` line
in app.py. Delete both and Odysseus is stock.
"""

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import effective_user, get_current_user

logger = logging.getLogger(__name__)

MAX_ENTRIES = 500
MAX_KEY_CHARS = 128
MAX_TEXT_CHARS = 4000
MIN_PREFIX_CHARS = 3


class ProjectionEntry(BaseModel):
    key: str
    text: str
    category: str = "fact"


class ProjectionRequest(BaseModel):
    prefix: str
    entries: List[ProjectionEntry]


def _require_user(request: Request) -> Optional[str]:
    """`effective_user` (bearer ody_ acts as its minting owner) or the cookie
    user; 401 only when auth is configured and neither is present. Mirrors
    contact_notes_routes."""
    # Bearer token with no minting owner: never allow — effective_user would
    # fall back to the "api" pseudo-user and the projection would write memory
    # rows invisible to every human UI and undeletable through the
    # owner-scoped memory routes.
    if getattr(request.state, "api_token", False) and \
            not getattr(request.state, "api_token_owner", None):
        raise HTTPException(403, "token has no owner")
    user = effective_user(request)
    if user:
        return user
    if get_current_user(request) is None and \
            getattr(request.app.state, "auth_manager", None) is not None and \
            getattr(request.app.state.auth_manager, "is_configured", False):
        raise HTTPException(401, "Not authenticated")
    return user or None


def setup_memory_projection_routes(memory_manager, memory_vector=None):
    router = APIRouter(prefix="/api/memory", tags=["memory-projection"])

    @router.put("/projection")
    def reconcile_projection(request: Request, body: ProjectionRequest):
        """Reconcile the caller's projected memories under one key-prefix to
        exactly the supplied set. Idempotent: same payload twice → no writes
        the second time. Returns per-op counts so the sync client can log."""
        user = _require_user(request)

        prefix = (body.prefix or "").strip()
        if len(prefix) < MIN_PREFIX_CHARS:
            raise HTTPException(400, f"prefix must be ≥{MIN_PREFIX_CHARS} chars")
        if len(body.entries) > MAX_ENTRIES:
            raise HTTPException(400, f"too many entries (max {MAX_ENTRIES})")
        desired: dict[str, ProjectionEntry] = {}
        for e in body.entries:
            key = (e.key or "").strip()
            text = (e.text or "").strip()
            if not key.startswith(prefix) or len(key) > MAX_KEY_CHARS:
                raise HTTPException(
                    400, f"entry key must start with the prefix and be "
                         f"≤{MAX_KEY_CHARS} chars: {key[:60]!r}")
            if not text or len(text) > MAX_TEXT_CHARS:
                raise HTTPException(
                    400, f"entry text must be 1..{MAX_TEXT_CHARS} chars "
                         f"(key {key[:60]!r})")
            if key in desired:
                raise HTTPException(400, f"duplicate key {key[:60]!r}")
            desired[key] = e

        all_mem = memory_manager.load_all()
        mine = {
            m.get("projection_key"): m for m in all_mem
            if isinstance(m, dict)
            and str(m.get("projection_key") or "").startswith(prefix)
            and m.get("owner") == user
        }

        added = updated = deleted = kept = 0
        vector_ok = memory_vector is not None and \
            getattr(memory_vector, "healthy", False)

        def _vector(op, *args):
            if not vector_ok:
                return
            try:
                getattr(memory_vector, op)(*args)
            except Exception:
                logger.debug("memory vector %s failed", op, exc_info=True)

        # Deletes: projected rows whose key vanished from the desired set.
        stale_ids = {m["id"] for k, m in mine.items() if k not in desired}
        if stale_ids:
            all_mem = [m for m in all_mem if m.get("id") not in stale_ids]
            for mid in stale_ids:
                _vector("remove", mid)
            deleted = len(stale_ids)

        # Upserts.
        for key, e in desired.items():
            row = mine.get(key)
            if row is None:
                entry = memory_manager.add_entry(
                    e.text.strip(), source="even-odysseus",
                    category=(e.category or "fact").strip()[:32] or "fact",
                    owner=user)
                entry["projection_key"] = key
                all_mem.append(entry)
                _vector("add", entry["id"], entry["text"])
                added += 1
            elif (row.get("text") != e.text.strip()
                  or row.get("category") != e.category):
                row["text"] = e.text.strip()
                row["category"] = (e.category or "fact").strip()[:32] or "fact"
                row["timestamp"] = int(time.time())
                _vector("remove", row["id"])
                _vector("add", row["id"], row["text"])
                updated += 1
            else:
                kept += 1

        if added or updated or deleted:
            memory_manager.save(all_mem)
            # Same event the stock memory POST fires — badge counts / reindex
            # listeners must see projected memories too.
            try:
                from src.event_bus import fire_event
                fire_event("memory_added", user)
            except Exception:
                logger.debug("memory_added event dispatch failed", exc_info=True)

        logger.info("memory projection [%s] owner=%s: +%d ~%d -%d =%d",
                    prefix, user, added, updated, deleted, kept)
        return {"ok": True, "prefix": prefix, "added": added,
                "updated": updated, "deleted": deleted, "kept": kept,
                "count": len(desired)}

    return router
