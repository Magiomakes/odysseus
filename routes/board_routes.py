"""
board_routes.py — "My Tasks" board: the user's personal task list.

Distinct from scheduled_tasks (agent jobs): a user_task is a card the HUMAN
owns — created in the UI, or ingested from an external capture pipeline
(the even-odysseus bridge's Task Manager webhook sink POSTs manual tasks
here). Cards live on a Sunsama-style day board (planned_date) or in the
backlog (planned_date NULL).

A card can be handed off to an agent: handoff creates a run-now
ScheduledTask whose prompt is built from the card, links it via
scheduled_task_id, and flips the card to 'handed_off'. Completion is
reconciled PULL-style on board reads (no scheduler hooks — keeps this
module fully additive): when the linked task's latest run finishes, the
run result is attached to the card and it flips to 'in_review'. The human
reviews and marks it done — the review gate stays with the user.

Lifecycle rule (even-odysseus ADR-0010, "no silent loss"): the system
never deletes a card. 'done' cards archive; explicit DELETE is the user's
act only.
"""

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Float, Integer, String, Text, text as sa_text

from core.database import Base, SessionLocal, ScheduledTask, TaskRun, engine
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

VALID_STATUSES = ("todo", "handed_off", "in_review", "done", "archived")
# Sunsama-style backlog horizons, nearest first. NULL is treated as "week".
VALID_HORIZONS = ("week", "month", "quarter", "year", "someday", "never")


class UserTask(Base):
    """A personal task card on the My Tasks board."""
    __tablename__ = "user_tasks"

    id                = Column(String, primary_key=True, index=True)
    owner             = Column(String, nullable=True, index=True)
    title             = Column(String, nullable=False)
    notes             = Column(Text, nullable=True)
    planned_date      = Column(String, nullable=True, index=True)  # "YYYY-MM-DD"; NULL = backlog
    due               = Column(String, nullable=True)              # "YYYY-MM-DD" hard deadline
    status            = Column(String, default="todo", index=True)
    position          = Column(Float, default=0.0)                 # sort order within a column
    source            = Column(String, default="manual")           # manual | bridge | email
    source_ref        = Column(String, nullable=True)              # e.g. bridge added_at stamp (idempotency)
    scheduled_task_id = Column(String, nullable=True, index=True)  # link when handed to an agent
    last_run_id       = Column(String, nullable=True)
    run_status        = Column(String, nullable=True)              # success | error (last reconciled run)
    result            = Column(Text, nullable=True)                # agent output attached to the card
    channel           = Column(String, nullable=True)              # free-form #channel tag
    horizon           = Column(String, nullable=True)              # backlog horizon (VALID_HORIZONS)
    estimate_minutes  = Column(Integer, nullable=True)             # planned effort
    session_id        = Column(String, nullable=True, index=True)  # source recording (even-odysseus Session Folder basename)
    context_url       = Column(String, nullable=True)              # reachable pull URL for grounding (brain read API)
    bucket            = Column(String, nullable=True)              # even-odysseus Agent Bucket (research | email-draft | manual)
    result_original   = Column(Text, nullable=True)                # pristine agent output before any human edit
    draft_saved       = Column(Integer, default=0)                 # email-draft copied to IMAP Drafts exactly once
    created_at        = Column(DateTime, default=lambda: datetime.utcnow())
    completed_at      = Column(DateTime, nullable=True)


def _ensure_columns():
    """Additive schema evolution for existing installs: create_all only makes
    missing TABLES, so columns added after the table first shipped must be
    ALTERed in. SQLite ADD COLUMN is cheap and idempotent-guarded here."""
    wanted = {
        "channel": "VARCHAR",
        "horizon": "VARCHAR",
        "estimate_minutes": "INTEGER",
        "session_id": "VARCHAR",
        "context_url": "VARCHAR",
        "bucket": "VARCHAR",
        "result_original": "TEXT",
        "draft_saved": "INTEGER",
    }
    try:
        with engine.connect() as conn:
            have = {row[1] for row in conn.execute(sa_text("PRAGMA table_info(user_tasks)"))}
            for col, sqltype in wanted.items():
                if col not in have:
                    conn.execute(sa_text(f"ALTER TABLE user_tasks ADD COLUMN {col} {sqltype}"))
            conn.commit()
    except Exception:
        logger.warning("user_tasks column migration failed", exc_info=True)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _card_to_dict(t: UserTask) -> dict:
    return {
        "id": t.id,
        "owner": t.owner,
        "title": t.title,
        "notes": t.notes,
        "planned_date": t.planned_date,
        "due": t.due,
        "status": t.status,
        "position": t.position,
        "source": t.source,
        "scheduled_task_id": t.scheduled_task_id,
        "last_run_id": t.last_run_id,
        "run_status": t.run_status,
        "result": t.result,
        "channel": t.channel,
        "horizon": t.horizon or "week",
        "estimate_minutes": t.estimate_minutes,
        "session_id": t.session_id,
        "source_ref": t.source_ref,
        "context_url": t.context_url,
        "bucket": t.bucket,
        "result_original": t.result_original,
        "draft_saved": bool(t.draft_saved),
        "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
        "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
    }


def _valid_horizon(h):
    if h is None or h == "":
        return None
    if h not in VALID_HORIZONS:
        raise HTTPException(400, f"Invalid horizon '{h}'")
    return h


def _clean_channel(c):
    if c is None:
        return None
    c = c.strip().lstrip("#").strip()
    return c[:60] or None


def _valid_date(s: Optional[str]) -> Optional[str]:
    if s is None or s == "":
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        raise HTTPException(400, f"Invalid date '{s}' (expected YYYY-MM-DD)")


def _next_position(db, owner, planned_date) -> float:
    q = db.query(UserTask).filter(UserTask.planned_date == planned_date)
    if owner:
        q = q.filter(UserTask.owner == owner)
    top = q.order_by(UserTask.position.desc()).first()
    return (top.position + 1.0) if top and top.position is not None else 1.0


class CardCreate(BaseModel):
    title: str
    notes: Optional[str] = None
    planned_date: Optional[str] = None
    due: Optional[str] = None
    channel: Optional[str] = None
    horizon: Optional[str] = None
    estimate_minutes: Optional[int] = None


class CardPatch(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    planned_date: Optional[str] = None  # "" clears to backlog
    due: Optional[str] = None
    status: Optional[str] = None
    position: Optional[float] = None
    channel: Optional[str] = None        # "" clears
    horizon: Optional[str] = None
    estimate_minutes: Optional[int] = None  # 0 clears
    result: Optional[str] = None         # human edit of the agent output (ADR-0015)
    # Explicit flags because None is a meaningful value for these two fields
    clear_planned_date: Optional[bool] = False
    clear_due: Optional[bool] = False


class HandoffRequest(BaseModel):
    prompt: Optional[str] = None   # override; default is built from the card
    model: Optional[str] = None
    task_type: str = "llm"         # "llm" | "research"


class IngestItem(BaseModel):
    text: str
    due: Optional[str] = None
    source: Optional[str] = "bridge"
    added_at: Optional[str] = None
    # ADR-0015 v2 fields — all optional so v1 sinks keep working unchanged.
    session_id: Optional[str] = None    # source recording (Session Folder basename)
    context_url: Optional[str] = None   # reachable pull URL for grounding
    planned_date: Optional[str] = None  # land pre-scheduled on this day column
    horizon: Optional[str] = None       # else this backlog horizon
    bucket: Optional[str] = None        # Agent Bucket label (display / draft routing)
    task_type: Optional[str] = None     # "llm" | "research" → auto-handoff
    prompt: Optional[str] = None        # full grounded agent prompt (built by the brain)


MAX_ABORT_RETRIES = 3


def _create_handoff_task(db, card, prompt, task_type, model=None):
    """The single card→agent handoff seam (drag-to-agent AND ingest
    auto-handoff): create the run-now ScheduledTask, link it to the card,
    flip the card to 'handed_off'. Caller commits and then fires
    task_scheduler.run_task_now(task.id)."""
    task = ScheduledTask(
        id=str(uuid.uuid4()),
        owner=card.owner,
        name=f"[Board] {card.title[:80]}",
        prompt=prompt,
        task_type=task_type,
        trigger_type="schedule",
        schedule="once",
        next_run=None,          # fired immediately by the caller, not the loop
        status="active",
        output_target="none",   # the board card is the delivery surface
        model=model,
        run_count=0,
        email_results=False,
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    db.add(task)
    card.scheduled_task_id = task.id
    card.status = "handed_off"
    card.run_status = None
    card.result = None
    return task


_CONTEXT_URL_BLOCK = (
    "\n\nThis task came from a recorded conversation. Pull it for grounding "
    "before you act:\n  GET {url}\nGround every specific in that context or a "
    "cited source; do not invent details."
)


# ── email-draft → stock IMAP Drafts folder (ADR-0015) ──────────────────────
# A completed email-draft card's result is copied once into the owner's
# Drafts folder so it's findable in the stock email tab — not only in a
# pop-once notification / the Activity log. Saves run on a daemon thread
# (IMAP is slow and this is called from the board-read reconcile path);
# `draft_saved` flips only on success, so failures retry on later reads.

_DRAFT_HEADER_RE = re.compile(r"^(To|Subject):\s*(.+)$", re.IGNORECASE)
_draft_saves_inflight: set = set()
_draft_saves_lock = threading.Lock()


def _parse_draft(text: str, fallback_subject: str):
    """Split an agent-written draft into (to, subject, body). The email-draft
    bucket instruction asks for 'Subject + body only', so leading To:/Subject:
    header lines are recognized; anything else is body. Missing subject falls
    back to the card title."""
    lines = (text or "").splitlines()
    to = subject = None
    body_start = 0
    for i, line in enumerate(lines[:4]):
        m = _DRAFT_HEADER_RE.match(line.strip())
        if not m:
            if not line.strip() and (to or subject):
                body_start = i + 1
            break
        if m.group(1).lower() == "to":
            to = m.group(2).strip()
        else:
            subject = m.group(2).strip()
        body_start = i + 1
    body = "\n".join(lines[body_start:]).strip() or (text or "")
    return to, (subject or fallback_subject), body


def _save_email_draft(card) -> bool:
    """IMAP-append the card's draft to the owner's Drafts folder, stamped
    X-Odysseus-Kind/Ref for task linkage. Mirrors POST /api/email/draft's
    mechanics via the module-level email helpers (NOT the route — its
    require_owner would resolve a server-side call to the 'api' pseudo-user).
    Best-effort: any failure returns False and the reconcile retries later."""
    try:
        import email.utils as email_utils
        from email.mime.text import MIMEText

        from routes.email_helpers import (_detect_drafts_folder,
                                          _get_email_config, _imap)
        cfg = _get_email_config(owner=card.owner or "")
        to, subject, body = _parse_draft(card.result or "", card.title or "Draft")
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = email_utils.formataddr(
            (cfg.get("display_name") or "", cfg["from_address"]))
        if to:
            msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
        try:
            from routes.email_routes import _apply_odysseus_headers
            _apply_odysseus_headers(msg, kind="task", ref_id=card.scheduled_task_id)
        except Exception:
            msg["X-Odysseus-Kind"] = "task"
            if card.scheduled_task_id:
                msg["X-Odysseus-Ref"] = card.scheduled_task_id
        with _imap(owner=card.owner or "") as imap:
            imap.append(_detect_drafts_folder(imap), "\\Draft", None, msg.as_bytes())
        logger.info("Board card %s: draft saved to IMAP Drafts (%s)", card.id, subject)
        return True
    except Exception:
        logger.info("Board card %s: draft not saved to IMAP (will retry on next "
                    "board read)", card.id, exc_info=True)
        return False


def _draft_save_worker(card_id: str):
    """Thread body: re-read the card in a fresh session, save, mark once."""
    try:
        db = SessionLocal()
        try:
            card = db.query(UserTask).filter(UserTask.id == card_id).first()
            if card and card.result and not card.draft_saved:
                if _save_email_draft(card):
                    card.draft_saved = 1
                    db.commit()
        finally:
            db.close()
    finally:
        with _draft_saves_lock:
            _draft_saves_inflight.discard(card_id)


def _queue_draft_save(card_id: str):
    """Fire-and-forget draft save; the in-flight set stops a burst of board
    reads from appending the same draft twice."""
    with _draft_saves_lock:
        if card_id in _draft_saves_inflight:
            return
        _draft_saves_inflight.add(card_id)
    threading.Thread(target=_draft_save_worker, args=(card_id,),
                     daemon=True, name=f"board-draft-{card_id[:8]}").start()


def _reconcile_handed_off(db, owner) -> int:
    """Attach finished agent-run results to handed-off cards (pull model).

    Called on board reads. For each card in 'handed_off' with a linked
    scheduled task, look at that task's most recent run; once it reaches a
    terminal state the run's output lands on the card and the card flips
    to 'in_review'. Returns how many cards changed.

    Aborted runs are NOT results: a run dies aborted when a server restart
    or the foreground gate cancels it mid-flight, and a once-task whose
    next_run is NULL would never retry — the card would sit 'handed off'
    forever (or worse, surface stale progress text as the agent's answer).
    Instead the task is re-armed (next_run=now, the scheduler re-fires it
    when Odysseus is idle), bounded by MAX_ABORT_RETRIES; past the bound
    the card flips to review with the abort surfaced honestly.
    """
    q = db.query(UserTask).filter(
        UserTask.status == "handed_off",
        UserTask.scheduled_task_id.isnot(None),
    )
    if owner:
        q = q.filter(UserTask.owner == owner)
    changed = 0
    for card in q.all():
        run = (
            db.query(TaskRun)
            .filter(TaskRun.task_id == card.scheduled_task_id)
            .order_by(TaskRun.started_at.desc())
            .first()
        )
        if not run or run.status in ("queued", "running"):
            continue

        if run.status == "aborted":
            aborts = (
                db.query(TaskRun)
                .filter(TaskRun.task_id == card.scheduled_task_id,
                        TaskRun.status == "aborted")
                .count()
            )
            task = db.query(ScheduledTask).filter(
                ScheduledTask.id == card.scheduled_task_id).first()
            if task and task.status == "active" and aborts <= MAX_ABORT_RETRIES:
                # Interrupted, not failed — re-arm and keep waiting.
                if task.next_run is None:
                    task.next_run = _utcnow()
                    logger.info(
                        "Board card %s: re-armed aborted handoff task %s (attempt %s)",
                        card.id, task.id, aborts + 1,
                    )
                    changed += 1
                continue
            # Retries exhausted (or task gone/paused): surface the abort,
            # preferring the abort reason over stale progress text.
            card.last_run_id = run.id
            card.run_status = "error"
            card.result = run.error or "Agent run was interrupted repeatedly and gave up."
            card.status = "in_review"
            changed += 1
            continue

        card.last_run_id = run.id
        card.run_status = run.status
        card.result = run.result or run.error or "(no output)"
        card.status = "in_review"
        changed += 1
    if changed:
        db.commit()

    # ADR-0015: completed email-draft results also land in the stock IMAP
    # Drafts folder (async, once). This sweep catches both fresh flips above
    # and earlier failures (draft_saved stays 0 until a save succeeds).
    pending_drafts = db.query(UserTask).filter(
        UserTask.status == "in_review",
        UserTask.bucket == "email-draft",
        UserTask.run_status == "success",
        UserTask.result.isnot(None),
    )
    if owner:
        pending_drafts = pending_drafts.filter(UserTask.owner == owner)
    for card in pending_drafts.all():
        if not card.draft_saved:
            _queue_draft_save(card.id)
    return changed


def setup_board_routes(task_scheduler) -> APIRouter:
    router = APIRouter(prefix="/api/board", tags=["board"])

    # Additive table creation — no migration framework in this codebase;
    # mirrors how the rest of the schema comes up via create_all.
    Base.metadata.create_all(bind=engine, tables=[UserTask.__table__])
    _ensure_columns()

    def _owner(request: Request):
        # Same attribution rule as task_routes: bearer ody_ tokens credit
        # the minting owner, so bridge-ingested cards land on the human's
        # board, not under the "api" pseudo-user.
        return effective_user(request)

    @router.get("/tasks")
    async def list_cards(request: Request, start: Optional[str] = None,
                         end: Optional[str] = None, archived: bool = False):
        """All the caller's cards: backlog + optionally a date window.

        Reconciles handed-off cards against their agent runs first, so the
        board is always current without any scheduler-side hook.
        """
        user = _owner(request)
        _valid_date(start); _valid_date(end)
        db = SessionLocal()
        try:
            _reconcile_handed_off(db, user)
            q = db.query(UserTask)
            if user:
                q = q.filter(UserTask.owner == user)
            if not archived:
                q = q.filter(UserTask.status != "archived")
            if start:
                # keep backlog (NULL planned_date) plus the window
                q = q.filter((UserTask.planned_date.is_(None)) | (UserTask.planned_date >= start))
            if end:
                q = q.filter((UserTask.planned_date.is_(None)) | (UserTask.planned_date <= end))
            cards = q.order_by(UserTask.planned_date, UserTask.position).all()
            return {"tasks": [_card_to_dict(t) for t in cards]}
        finally:
            db.close()

    @router.post("/tasks")
    async def create_card(request: Request, req: CardCreate):
        user = _owner(request)
        title = (req.title or "").strip()
        if not title:
            raise HTTPException(400, "Title is required")
        db = SessionLocal()
        try:
            card = UserTask(
                id=str(uuid.uuid4()),
                owner=user,
                title=title[:500],
                notes=req.notes,
                planned_date=_valid_date(req.planned_date),
                due=_valid_date(req.due),
                status="todo",
                position=_next_position(db, user, _valid_date(req.planned_date)),
                source="manual",
                channel=_clean_channel(req.channel),
                horizon=_valid_horizon(req.horizon),
                estimate_minutes=req.estimate_minutes if (req.estimate_minutes or 0) > 0 else None,
                created_at=_utcnow(),
            )
            db.add(card)
            db.commit()
            return _card_to_dict(card)
        finally:
            db.close()

    @router.patch("/tasks/{card_id}")
    async def patch_card(request: Request, card_id: str, req: CardPatch):
        user = _owner(request)
        db = SessionLocal()
        try:
            card = db.query(UserTask).filter(UserTask.id == card_id).first()
            if not card:
                raise HTTPException(404, "Card not found")
            if user and card.owner != user:
                raise HTTPException(403, "Access denied")
            if req.title is not None:
                title = req.title.strip()
                if not title:
                    raise HTTPException(400, "Title cannot be empty")
                card.title = title[:500]
            if req.notes is not None:
                card.notes = req.notes
            if req.clear_planned_date:
                card.planned_date = None
            elif req.planned_date is not None:
                card.planned_date = _valid_date(req.planned_date)
            if req.clear_due:
                card.due = None
            elif req.due is not None:
                card.due = _valid_date(req.due)
            if req.position is not None:
                card.position = req.position
            if req.channel is not None:
                card.channel = _clean_channel(req.channel)
            if req.horizon is not None:
                card.horizon = _valid_horizon(req.horizon)
            if req.estimate_minutes is not None:
                card.estimate_minutes = req.estimate_minutes if req.estimate_minutes > 0 else None
            if req.result is not None:
                # Human edit of the agent output (ADR-0015 learn-from-edit).
                # The pristine agent draft is preserved exactly once so the
                # brain can diff original vs edited later — never overwritten
                # by subsequent edits.
                if card.result_original is None and card.result:
                    card.result_original = card.result
                card.result = req.result
            if req.status is not None:
                if req.status not in VALID_STATUSES:
                    raise HTTPException(400, f"Invalid status '{req.status}'")
                card.status = req.status
                card.completed_at = _utcnow() if req.status == "done" else None
            db.commit()
            return _card_to_dict(card)
        finally:
            db.close()

    @router.delete("/tasks/{card_id}")
    async def delete_card(request: Request, card_id: str):
        """Explicit user delete — the only path that removes a card."""
        user = _owner(request)
        db = SessionLocal()
        try:
            card = db.query(UserTask).filter(UserTask.id == card_id).first()
            if not card:
                raise HTTPException(404, "Card not found")
            if user and card.owner != user:
                raise HTTPException(403, "Access denied")
            db.delete(card)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/tasks/{card_id}/handoff")
    async def handoff_card(request: Request, card_id: str, req: HandoffRequest):
        """Hand a card to an agent: create a run-now scheduled task linked
        back to the card. The card flips to 'handed_off'; the result comes
        back via reconciliation on the next board read."""
        user = _owner(request)
        if req.task_type not in ("llm", "research"):
            raise HTTPException(400, "task_type must be 'llm' or 'research'")
        db = SessionLocal()
        try:
            card = db.query(UserTask).filter(UserTask.id == card_id).first()
            if not card:
                raise HTTPException(404, "Card not found")
            if user and card.owner != user:
                raise HTTPException(403, "Access denied")
            if card.status == "handed_off":
                raise HTTPException(409, "Card is already handed off")

            prompt = (req.prompt or "").strip()
            if not prompt:
                prompt = f"Complete this task for the user: {card.title}"
                if card.notes:
                    prompt += f"\n\nContext / notes:\n{card.notes}"
                if card.due:
                    prompt += f"\n\nDeadline: {card.due}"
                if card.context_url:
                    # ADR-0015: a capture-sourced card carries its recording's
                    # pull URL — the agent grounds in the actual conversation.
                    prompt += _CONTEXT_URL_BLOCK.format(url=card.context_url)

            task = _create_handoff_task(db, card, prompt, req.task_type,
                                        model=req.model)
            db.commit()
            task_id = task.id
        finally:
            db.close()

        started = await task_scheduler.run_task_now(task_id)
        if not started:
            logger.warning("Board handoff %s queued but scheduler did not start it", task_id)
        return {"ok": True, "scheduled_task_id": task_id, "started": bool(started)}

    @router.get("/channels")
    async def list_channels(request: Request):
        """Distinct channels with open-card counts, for the header filter."""
        user = _owner(request)
        db = SessionLocal()
        try:
            q = db.query(UserTask).filter(
                UserTask.channel.isnot(None),
                UserTask.status != "archived",
            )
            if user:
                q = q.filter(UserTask.owner == user)
            counts = {}
            for card in q.all():
                counts[card.channel] = counts.get(card.channel, 0) + (card.status != "done")
            return {"channels": [
                {"name": name, "open": n}
                for name, n in sorted(counts.items(), key=lambda kv: kv[0].lower())
            ]}
        finally:
            db.close()

    @router.post("/ingest")
    async def ingest(request: Request):
        """Webhook for external capture pipelines (even-odysseus Task
        Manager sink). Accepts a single item or a list. Idempotent on
        (owner, source, added_at, text) so a buffered sink can retry safely
        — a dupe skips the card AND any handoff.

        ADR-0015 v2 (all optional, lenient — a bad field is dropped, the
        card is kept): `planned_date` lands the card pre-scheduled on that
        day column, else `horizon` picks the backlog group (default 'week');
        `session_id`/`context_url` carry recording provenance; `bucket`
        labels the card; `prompt` + `task_type` trigger an immediate
        internal handoff (same path as drag-to-agent) so a brain-confirmed
        agent task executes on a LINKED card — its result lands here."""
        user = _owner(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON body")
        raw_items = body if isinstance(body, list) else [body]
        items = []
        for raw in raw_items:
            try:
                items.append(IngestItem(**raw) if isinstance(raw, dict) else None)
            except Exception:
                items.append(None)
        db = SessionLocal()
        created, skipped, handed_off = 0, 0, 0
        handoff_task_ids = []
        try:
            for item in items:
                if item is None or not (item.text or "").strip():
                    skipped += 1
                    continue
                ref = f"{item.source or 'bridge'}:{item.added_at or ''}:{item.text[:120]}"
                dupe_q = db.query(UserTask).filter(UserTask.source_ref == ref)
                if user:
                    dupe_q = dupe_q.filter(UserTask.owner == user)
                if dupe_q.first():
                    skipped += 1
                    continue
                due = planned = horizon = None
                try:
                    due = _valid_date(item.due)
                except HTTPException:
                    pass  # bad due date from a capture pipeline: keep the task, drop the date
                try:
                    planned = _valid_date(item.planned_date)
                except HTTPException:
                    pass
                try:
                    horizon = _valid_horizon(item.horizon)
                except HTTPException:
                    pass
                card = UserTask(
                    id=str(uuid.uuid4()),
                    owner=user,
                    title=item.text.strip()[:500],
                    due=due,
                    planned_date=planned,
                    status="todo",
                    position=_next_position(db, user, planned),
                    source=(item.source or "bridge")[:50],
                    source_ref=ref,
                    session_id=(item.session_id or None),
                    context_url=(item.context_url or None),
                    bucket=(item.bucket or None),
                    # scheduled cards need no horizon; backlog cards surface
                    # in the given group, else the nearest one
                    horizon=None if planned else (horizon or "week"),
                    created_at=_utcnow(),
                )
                db.add(card)
                created += 1
                prompt = (item.prompt or "").strip()
                if prompt and item.task_type in ("llm", "research"):
                    task = _create_handoff_task(db, card, prompt, item.task_type)
                    handoff_task_ids.append(task.id)
                    handed_off += 1
            db.commit()
        finally:
            db.close()
        for tid in handoff_task_ids:
            started = await task_scheduler.run_task_now(tid)
            if not started:
                logger.warning("Board ingest handoff %s queued but scheduler "
                               "did not start it", tid)
        return {"ok": True, "created": created, "skipped": skipped,
                "handed_off": handed_off}

    return router
