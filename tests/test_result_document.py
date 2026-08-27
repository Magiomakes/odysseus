"""create_result_document — the shared library-materialization helper used by
board research reconcile and the scheduler's 'document' output target."""
from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

from core.database import Document, DocumentVersion, SessionLocal
from src.document_actions import create_result_document


def test_creates_document_and_version(monkeypatch):
    fired = []
    import src.event_bus as eb
    monkeypatch.setattr(eb, "fire_event", lambda name, owner=None: fired.append((name, owner)))

    doc_id = create_result_document(
        title="Research: shower plumbing",
        content="# Findings\n…",
        owner="alice",
        summary="Board research handoff result",
    )
    assert doc_id
    db = SessionLocal()
    try:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        assert doc is not None
        assert doc.title == "Research: shower plumbing"
        assert doc.owner == "alice"
        assert doc.language == "markdown"
        ver = db.query(DocumentVersion).filter(
            DocumentVersion.document_id == doc_id).first()
        assert ver is not None and ver.version_number == 1
        assert ver.source == "ai"
    finally:
        db.close()
    assert fired == [("document_created", "alice")]


def test_failure_returns_none(monkeypatch):
    import src.document_actions as da

    class _BoomSession:
        def add(self, *_): raise RuntimeError("db down")
        def commit(self): pass
        def close(self): pass

    monkeypatch.setattr("core.database.SessionLocal", lambda: _BoomSession())
    # The helper imports SessionLocal at call time from core.database.
    assert da.create_result_document(title="t", content="c") is None
