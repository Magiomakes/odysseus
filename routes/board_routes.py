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
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Float, String, Text

from core.database import Base, SessionLocal, ScheduledTask, TaskRun, engine
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

VALID_STATUSES = ("todo", "handed_off", "in_review", "done", "archived")


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
    created_at        = Column(DateTime, default=lambda: datetime.utcnow())
    completed_at      = Column(DateTime, nullable=True)


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
        "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
        "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
    }


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


class CardPatch(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    planned_date: Optional[str] = None  # "" clears to backlog
    due: Optional[str] = None
    status: Optional[str] = None
    position: Optional[float] = None
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


def _reconcile_handed_off(db, owner) -> int:
    """Attach finished agent-run results to handed-off cards (pull model).

    Called on board reads. For each card in 'handed_off' with a linked
    scheduled task, look at that task's most recent run; once it reaches a
    terminal state the run's output lands on the card and the card flips
    to 'in_review'. Returns how many cards changed.
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
        card.last_run_id = run.id
        card.run_status = run.status
        card.result = run.result or run.error or "(no output)"
        card.status = "in_review"
        changed += 1
    if changed:
        db.commit()
    return changed


def setup_board_routes(task_scheduler) -> APIRouter:
    router = APIRouter(prefix="/api/board", tags=["board"])

    # Additive table creation — no migration framework in this codebase;
    # mirrors how the rest of the schema comes up via create_all.
    Base.metadata.create_all(bind=engine, tables=[UserTask.__table__])

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

            task = ScheduledTask(
                id=str(uuid.uuid4()),
                owner=card.owner,
                name=f"[Board] {card.title[:80]}",
                prompt=prompt,
                task_type=req.task_type,
                trigger_type="schedule",
                schedule="once",
                next_run=None,          # fired immediately below, not by the loop
                status="active",
                output_target="none",   # the board card is the delivery surface
                model=req.model,
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
            db.commit()
            task_id = task.id
        finally:
            db.close()

        started = await task_scheduler.run_task_now(task_id)
        if not started:
            logger.warning("Board handoff %s queued but scheduler did not start it", task_id)
        return {"ok": True, "scheduled_task_id": task_id, "started": bool(started)}

    @router.post("/ingest")
    async def ingest(request: Request):
        """Webhook for external capture pipelines (even-odysseus Task
        Manager sink). Accepts a single {text, due, source, added_at}
        object or a list of them. Idempotent on (owner, source, added_at,
        text) so a buffered sink can retry safely. New cards land in the
        backlog."""
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
        created, skipped = 0, 0
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
                due = None
                try:
                    due = _valid_date(item.due)
                except HTTPException:
                    pass  # bad due date from a capture pipeline: keep the task, drop the date
                db.add(UserTask(
                    id=str(uuid.uuid4()),
                    owner=user,
                    title=item.text.strip()[:500],
                    due=due,
                    planned_date=None,
                    status="todo",
                    position=_next_position(db, user, None),
                    source=(item.source or "bridge")[:50],
                    source_ref=ref,
                    created_at=_utcnow(),
                ))
                created += 1
            db.commit()
        finally:
            db.close()
        return {"ok": True, "created": created, "skipped": skipped}

    return router
