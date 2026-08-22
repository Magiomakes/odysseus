"""Contact notes mod: vCard NOTE surgery, sidecar CRUD, auth gating."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.contacts.contact_notes_routes as notes_mod
import routes.contacts.contacts_routes as contacts_mod


FIXTURE_VCARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:4.0\r\n"
    "UID:abc-123\r\n"
    "FN:Delaney Fixture\r\n"
    "N:Fixture;Delaney;;;\r\n"
    "EMAIL;PREF=1:delaney@example\r\n"
    " .com\r\n"                     # folded continuation (RFC 6350 3.2)
    "ORG:Roofers R Us\r\n"
    "PHOTO;ENCODING=b;TYPE=JPEG:MEQ4vRQAAA\r\n"
    "item1.TEL:555-0100\r\n"
    "END:VCARD\r\n"
)


def _req(user="alice", api_token=False, token_owner=None):
    state = SimpleNamespace(current_user=user, api_token=api_token,
                            api_token_owner=token_owner)
    app_state = SimpleNamespace(auth_manager=SimpleNamespace(is_configured=True))
    return SimpleNamespace(state=state, app=SimpleNamespace(state=app_state))


def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── pure vCard NOTE surgery ──

def test_set_note_preserves_foreign_properties():
    out = notes_mod.set_note_in_vcard(FIXTURE_VCARD, "Prefers text.\nQuote pending, high")
    assert "ORG:Roofers R Us" in out
    assert "PHOTO;ENCODING=b;TYPE=JPEG:MEQ4vRQAAA" in out
    assert "item1.TEL:555-0100" in out
    # folded email survives (unfolded form)
    assert "EMAIL;PREF=1:delaney@example.com" in out
    # note escaped per RFC 6350 (newline + comma)
    assert "NOTE:Prefers text.\\nQuote pending\\, high" in out
    assert out.rstrip().endswith("END:VCARD")


def test_get_note_round_trip_and_replace():
    note = "Line one, with comma\nLine two; semicolon"
    card = notes_mod.set_note_in_vcard(FIXTURE_VCARD, note)
    assert notes_mod.get_note_from_vcard(card) == note
    # replacing removes the old NOTE (exactly one NOTE line)
    card2 = notes_mod.set_note_in_vcard(card, "fresh")
    assert card2.count("NOTE:") == 1
    assert notes_mod.get_note_from_vcard(card2) == "fresh"
    # clearing removes it entirely
    card3 = notes_mod.set_note_in_vcard(card2, "")
    assert "NOTE" not in card3


def test_get_note_missing_is_empty():
    assert notes_mod.get_note_from_vcard(FIXTURE_VCARD) == ""


# ── sidecar (no CardDAV) CRUD via the routes ──

@pytest.fixture()
def local_store(monkeypatch, tmp_path):
    monkeypatch.setattr(notes_mod, "_carddav_configured", lambda *a: False)
    monkeypatch.setattr(notes_mod, "_notes_path",
                        lambda: tmp_path / "contact_notes.json")
    contacts = [{"uid": "u1", "name": "Delaney Fixture",
                 "emails": ["delaney@example.com"], "phones": [], "address": ""}]
    monkeypatch.setattr(notes_mod, "_fetch_contacts",
                        lambda force=False: contacts)
    return tmp_path


def test_note_append_and_replace_local(local_store):
    router = notes_mod.setup_contact_notes_routes()
    post = _endpoint(router, "POST", "/api/contact-notes/{uid}/note")
    get = _endpoint(router, "GET", "/api/contact-notes/{uid}/note")

    out = _run(post(_req(), "u1", {"append": "[eo card:c1 2026-08-21] roofer"}))
    assert out["success"] is True
    out = _run(post(_req(), "u1", {"append": "prefers text"}))
    got = _run(get(_req(), "u1"))
    assert got["note"] == "[eo card:c1 2026-08-21] roofer\nprefers text"

    _run(post(_req(), "u1", {"note": "replaced"}))
    assert _run(get(_req(), "u1"))["note"] == "replaced"
    _run(post(_req(), "u1", {"note": ""}))
    assert _run(get(_req(), "u1"))["note"] == ""
    # sidecar file cleaned of the cleared key
    data = json.loads((local_store / "contact_notes.json").read_text())
    assert "u1" not in data


def test_search_attaches_notes(local_store):
    router = notes_mod.setup_contact_notes_routes()
    post = _endpoint(router, "POST", "/api/contact-notes/{uid}/note")
    search = _endpoint(router, "GET", "/api/contact-notes/search")
    _run(post(_req(), "u1", {"append": "the roofer"}))
    out = _run(search(_req(), q="delaney"))
    assert len(out["results"]) == 1
    assert out["results"][0]["note"] == "the roofer"
    assert _run(search(_req(), q=""))["results"] == []


def test_post_note_validates_body(local_store):
    router = notes_mod.setup_contact_notes_routes()
    post = _endpoint(router, "POST", "/api/contact-notes/{uid}/note")
    with pytest.raises(HTTPException) as e:
        _run(post(_req(), "u1", {"nothing": True}))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        _run(post(_req(), "u1", {"append": "   "}))


# ── auth: bearer tokens act as their minting owner; ownerless are refused ──

def test_bearer_token_with_owner_allowed(local_store):
    router = notes_mod.setup_contact_notes_routes()
    get = _endpoint(router, "GET", "/api/contact-notes/{uid}/note")
    out = _run(get(_req(user="api", api_token=True, token_owner="orin"), "u1"))
    assert out["uid"] == "u1"


def test_bearer_token_without_owner_403(local_store):
    router = notes_mod.setup_contact_notes_routes()
    get = _endpoint(router, "GET", "/api/contact-notes/{uid}/note")
    with pytest.raises(HTTPException) as e:
        _run(get(_req(user="api", api_token=True, token_owner=None), "u1"))
    assert e.value.status_code == 403


def test_anonymous_with_configured_auth_401(local_store):
    router = notes_mod.setup_contact_notes_routes()
    get = _endpoint(router, "GET", "/api/contact-notes/{uid}/note")
    with pytest.raises(HTTPException) as e:
        _run(get(_req(user=None), "u1"))
    assert e.value.status_code == 401


# ── add: dedupe by email via the store ──

def test_add_dedupes_and_creates(local_store, monkeypatch):
    router = notes_mod.setup_contact_notes_routes()
    add = _endpoint(router, "POST", "/api/contact-notes/add")
    out = _run(add(_req(), {"name": "Delaney", "email": "delaney@example.com"}))
    assert out["existing"] is True and out["contact"]["uid"] == "u1"

    created = []
    monkeypatch.setattr(notes_mod, "_create_contact",
                        lambda name, email="": created.append((name, email)) or True)
    out = _run(add(_req(), {"name": "Sam New", "email": "sam@example.com"}))
    assert out["success"] is True and created == [("Sam New", "sam@example.com")]
    with pytest.raises(HTTPException):
        _run(add(_req(), {}))
