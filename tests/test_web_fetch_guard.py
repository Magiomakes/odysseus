"""SSRF-guard tests for the generic ``web_fetch`` tool (local mod).

The load-bearing invariant: a task agent (whose prompt is built from untrusted
input — voice transcripts, API-created task text) can NEVER steer ``web_fetch``
at loopback / private / link-local address space, unless the operator has
explicitly allowlisted that hostname via WEB_FETCH_ALLOWLIST. Public hosts are
unaffected. Complements tests/test_task_grounding_fetch.py, which pins the same
invariant for the research pre-fetch path.
"""
import asyncio
import json

import pytest

from src.net_guard import check_fetch_target
from src.agent_tools.web_tools import WebFetchTool


def test_loopback_ip_refused(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    assert check_fetch_target("http://127.0.0.1:7860/api/config") is not None


def test_private_and_link_local_refused(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    assert check_fetch_target("http://192.168.1.10/admin") is not None
    assert check_fetch_target("http://10.0.0.5/") is not None
    assert check_fetch_target("http://169.254.169.254/latest/meta-data/") is not None


def test_localhost_names_refused(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    assert check_fetch_target("http://localhost:11434/api/tags") is not None
    assert check_fetch_target("http://foo.localhost/") is not None


def test_hostname_resolving_private_is_refused(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))])
    assert check_fetch_target("https://innocent-looking.example.com/x") is not None


def test_public_ip_allowed(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    assert check_fetch_target("http://93.184.216.34/") is None  # example.com's IP


def test_public_hostname_allowed(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    import socket
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))])
    assert check_fetch_target("https://example.com/page") is None


def test_allowlisted_private_host_allowed(monkeypatch):
    # The operator's own tailnet recorder stays reachable — same env var and
    # exact-hostname semantics as the grounding pre-fetch.
    monkeypatch.setenv("WEB_FETCH_ALLOWLIST",
                       "orions-mac-mini.tailcbe5c6.ts.net, other.host")
    assert check_fetch_target(
        "https://orions-mac-mini.tailcbe5c6.ts.net/api/sessions/x") is None


def test_unresolvable_host_refused(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    import socket as _s
    def boom(*a, **k):
        raise _s.gaierror("no such host")
    monkeypatch.setattr(_s, "getaddrinfo", boom)
    assert check_fetch_target("https://definitely-not-a-host.example/") is not None


def test_web_fetch_tool_refuses_loopback(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    tool = WebFetchTool()
    out = asyncio.run(tool.execute(
        json.dumps({"url": "http://127.0.0.1:7860/api/config"}), {}))
    assert out.get("exit_code") == 1
    assert "refused" in (out.get("error") or "")


def test_web_fetch_tool_still_normalizes_scheme_before_guarding(monkeypatch):
    monkeypatch.delenv("WEB_FETCH_ALLOWLIST", raising=False)
    tool = WebFetchTool()
    out = asyncio.run(tool.execute("localhost:8765/health", {}))
    assert out.get("exit_code") == 1
    assert "refused" in (out.get("error") or "")
