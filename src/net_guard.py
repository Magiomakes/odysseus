"""SSRF guard for the generic ``web_fetch`` tool (local mod).

Why this exists: scheduled/llm task agents get ``web_fetch`` promoted to
always-available, and their prompts are built from UNTRUSTED input (voice
transcripts captured by even-odysseus, task text created over the REST API).
An injected instruction like "fetch http://localhost:7860/api/config" would
otherwise let a task agent read any service on this machine or LAN — upstream's
THREAT_MODEL.md lists "agent acting on injected instructions" as in-scope.

Policy: a fetch target whose host resolves to loopback / private / link-local /
reserved / unspecified address space is refused, unless the hostname is
explicitly named in ``WEB_FETCH_ALLOWLIST`` (the same env var — and the same
exact-hostname semantics — the task scheduler's grounding pre-fetch uses, so
the allowlist finally constrains everything its name implies). Public hosts
are unaffected.

Resolution happens here, before the fetch, and EVERY resolved address must be
public — a DNS name pointing at 127.0.0.1 (rebinding-style) is refused even
though the URL "looks" external. Failure to resolve is refused too: the fetch
would fail anyway, and guessing open on resolver errors would be a hole.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


def _allowlist() -> set:
    """Hostnames exempt from the private-address block (comma-separated env).
    Mirrors src/task_scheduler.py::_grounding_allowlist — keep in sync."""
    raw = os.getenv("WEB_FETCH_ALLOWLIST", "") or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_unspecified or ip.is_multicast
    )


def check_fetch_target(url: str) -> str | None:
    """Return None when ``url`` may be fetched, else a human-readable refusal.

    Cheap, blocking (getaddrinfo) — call it off the event loop, next to the
    fetch it guards.
    """
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return "no host in URL"
    if host in _allowlist():
        return None
    # Literal IP first — no resolver round-trip.
    try:
        ip = ipaddress.ip_address(host)
        return None if _is_public_ip(ip) else \
            f"{host} is a non-public address (add it to WEB_FETCH_ALLOWLIST to permit)"
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return f"{host} is a local hostname (add it to WEB_FETCH_ALLOWLIST to permit)"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return f"cannot resolve {host}: {e}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])  # strip v6 zone id
        except ValueError:
            continue
        if not _is_public_ip(ip):
            return (f"{host} resolves to non-public address {ip} "
                    f"(add the hostname to WEB_FETCH_ALLOWLIST to permit)")
    return None
