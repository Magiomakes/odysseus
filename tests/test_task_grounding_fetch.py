"""Grounding-pull tests for the even-odysseus "pull, not push" model.

A scheduled task's prompt can name a reachable URL the agent must GET for
grounding (ADR-0007: the recorder hands over a task + a location, the action
engine pulls it). The research executor has no tool loop, so it pre-fetches any
ALLOWLISTED URL in the prompt and injects the body as grounding context.

The load-bearing safety invariant under test: a host that is not on
WEB_FETCH_ALLOWLIST is NEVER selected for fetch, so a crafted task prompt can't
steer an autonomous run into an SSRF GET of an arbitrary/internal host.
"""
from src.task_scheduler import (
    _extract_grounding_urls,
    _format_grounding,
    _grounding_allowlist,
)


def test_allowlisted_host_is_selected():
    allow = {"orions-mac-mini.tailcbe5c6.ts.net"}
    prompt = (
        "Before acting, GET "
        "https://orions-mac-mini.tailcbe5c6.ts.net/api/sessions/2026-06-25_0912 "
        "and ground every specific in it."
    )
    assert _extract_grounding_urls(prompt, allow) == [
        "https://orions-mac-mini.tailcbe5c6.ts.net/api/sessions/2026-06-25_0912"
    ]


def test_non_allowlisted_host_is_never_selected():
    # The SSRF guard: an internal metadata IP and an arbitrary external host,
    # neither allowlisted, must both be ignored.
    allow = {"orions-mac-mini.tailcbe5c6.ts.net"}
    prompt = "GET http://169.254.169.254/latest/meta-data/ then http://evil.example.com/x"
    assert _extract_grounding_urls(prompt, allow) == []


def test_empty_allowlist_selects_nothing():
    # Feature off by default: no allowlist => no pull, even for a plausible URL.
    assert _extract_grounding_urls("GET https://anything.example.com/x", set()) == []


def test_trailing_punctuation_is_trimmed():
    allow = {"host.ts.net"}
    assert _extract_grounding_urls("see https://host.ts.net/api/sessions/1.", allow) == [
        "https://host.ts.net/api/sessions/1"
    ]


def test_dedupes_repeated_url():
    allow = {"host.ts.net"}
    prompt = "GET https://host.ts.net/a then https://host.ts.net/a again"
    assert _extract_grounding_urls(prompt, allow) == ["https://host.ts.net/a"]


def test_grounding_allowlist_parses_env(monkeypatch):
    monkeypatch.setenv("WEB_FETCH_ALLOWLIST", " A.ts.net , b.example.com ,")
    assert _grounding_allowlist() == {"a.ts.net", "b.example.com"}


def test_grounding_allowlist_empty_when_unset(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    assert _grounding_allowlist() == set()


class _FakeResp:
    def __init__(self, payload, content_type="application/json"):
        self._payload = payload
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


def test_format_grounding_extracts_session_fields():
    resp = _FakeResp(
        {
            "id": "2026-06-25_0912",
            "transcript": "we talked about lunch money the budgeting app",
            "record": {"summary": "evaluate budgeting apps"},
            "meta": {"duration": 540},
        }
    )
    out = _format_grounding("https://host.ts.net/api/sessions/2026-06-25_0912", resp)
    assert "Source: https://host.ts.net/api/sessions/2026-06-25_0912" in out
    assert "[transcript]" in out and "lunch money" in out
    assert "[record]" in out and "evaluate budgeting apps" in out


def test_format_grounding_non_json_uses_text_body():
    resp = _FakeResp("plain page text", content_type="text/html")
    out = _format_grounding("https://host.ts.net/p", resp)
    assert "Source: https://host.ts.net/p" in out
    assert "plain page text" in out


# ── redirect hops must re-pass the allowlist (SSRF invariant, hop > 0) ──────
import asyncio

from src.task_scheduler import _fetch_grounding_context


def _mock_async_client(monkeypatch, handler):
    import httpx
    orig = httpx.AsyncClient

    class Patched(orig):
        def __init__(self, **kw):
            kw.pop("transport", None)
            super().__init__(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Patched)


def test_redirect_to_off_allowlist_target_is_never_fetched(monkeypatch):
    import httpx
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        if request.url.host == "allowed.example":
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1:7860/internal"})
        return httpx.Response(200, text="INTERNAL SECRET")

    _mock_async_client(monkeypatch, handler)
    monkeypatch.setenv("WEB_FETCH_ALLOWLIST", "allowed.example")
    out = asyncio.run(_fetch_grounding_context("GET http://allowed.example/doc"))
    assert out == ""  # pull degrades to ungrounded, never errors the task
    assert all("127.0.0.1" not in u for u in fetched)


def test_same_host_redirect_is_followed(monkeypatch):
    import httpx

    def handler(request):
        if request.url.path == "/doc":
            return httpx.Response(302, headers={"location": "/doc-v2"})
        return httpx.Response(200, text="grounding body",
                              headers={"content-type": "text/plain"})

    _mock_async_client(monkeypatch, handler)
    monkeypatch.setenv("WEB_FETCH_ALLOWLIST", "allowed.example")
    out = asyncio.run(_fetch_grounding_context("GET http://allowed.example/doc"))
    assert "grounding body" in out
