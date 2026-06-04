"""Streamable HTTP ("remote") transport routing in McpManager."""
import asyncio

from src.mcp_manager import McpManager


def test_streamable_http_routes_to_connect_method():
    """transport='streamable-http' dispatches to _connect_streamable_http, not the
    'Unknown transport' branch."""
    mgr = McpManager()
    seen = {}

    async def fake_connect(server_id, name, url):
        seen["url"] = url
        return True

    mgr._connect_streamable_http = fake_connect
    ok = asyncio.run(mgr.connect_server(
        server_id="abc123",
        name="openbrain",
        transport="streamable-http",
        url="https://example.com/mcp?key=secret",
    ))

    assert ok is True
    assert seen["url"] == "https://example.com/mcp?key=secret"


def test_http_alias_also_routes():
    """The 'http' alias maps to the same Streamable HTTP path."""
    mgr = McpManager()
    called = {"n": 0}

    async def fake_connect(server_id, name, url):
        called["n"] += 1
        return True

    mgr._connect_streamable_http = fake_connect
    asyncio.run(mgr.connect_server(server_id="x", name="y", transport="http", url="https://h/mcp"))

    assert called["n"] == 1


def test_unknown_transport_still_rejected():
    """A genuinely unknown transport returns False (no silent success)."""
    mgr = McpManager()
    ok = asyncio.run(mgr.connect_server(server_id="x", name="y", transport="carrier-pigeon"))
    assert ok is False
