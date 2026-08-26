"""Capacity gate (interactive_gate.wait_for_capacity) — board handoffs start on
machine headroom, not UI quiet; a live even-odysseus session is a hard block
that no max-wait overrides."""
import asyncio
import time

import pytest

from src import interactive_gate as gate


@pytest.fixture(autouse=True)
def _reset_pipeline_cache(monkeypatch):
    monkeypatch.setitem(gate._PIPELINE_CACHE, "t", 0.0)
    monkeypatch.setitem(gate._PIPELINE_CACHE, "busy", False)
    yield


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_no_blockers_returns_immediately(monkeypatch):
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    monkeypatch.setattr(gate, "system_under_load", lambda: False)

    async def not_busy():
        return False
    monkeypatch.setattr(gate, "external_pipeline_busy", not_busy)
    assert _run(gate.wait_for_capacity("t", max_wait=5)) is False


def test_ui_presence_does_not_block(monkeypatch):
    # Requests in flight + fresh browser heartbeat: capacity gate ignores both.
    monkeypatch.setattr(gate, "_ACTIVE_REQUESTS", 3)
    monkeypatch.setattr(gate, "_LAST_ACTIVITY", time.monotonic())
    monkeypatch.setattr(gate, "_LAST_BROWSER_ACTIVITY", time.monotonic())
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    monkeypatch.setattr(gate, "system_under_load", lambda: False)

    async def not_busy():
        return False
    monkeypatch.setattr(gate, "external_pipeline_busy", not_busy)
    assert _run(gate.wait_for_capacity("t", max_wait=5)) is False


def test_soft_block_released_by_max_wait(monkeypatch):
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: True)
    monkeypatch.setattr(gate, "system_under_load", lambda: False)

    async def not_busy():
        return False
    monkeypatch.setattr(gate, "external_pipeline_busy", not_busy)

    async def fast_sleep(_):
        pass
    monkeypatch.setattr(gate.asyncio, "sleep", fast_sleep)
    # Soft budget of 3 polls burns down and the wait returns (waited=True).
    assert _run(gate.wait_for_capacity("t", max_wait=3)) is True


def test_hard_block_survives_max_wait(monkeypatch):
    """The live-session hard block must NOT be released by max_wait expiry."""
    calls = {"n": 0}

    async def busy_then_free():
        calls["n"] += 1
        return calls["n"] < 30          # stays busy long past the soft budget

    monkeypatch.setattr(gate, "external_pipeline_busy", busy_then_free)
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    monkeypatch.setattr(gate, "system_under_load", lambda: False)

    async def fast_sleep(_):
        pass
    monkeypatch.setattr(gate.asyncio, "sleep", fast_sleep)
    assert _run(gate.wait_for_capacity("t", max_wait=2)) is True
    # Released only because the pipeline finally went quiet, not on the budget.
    assert calls["n"] >= 30


def test_soft_budget_does_not_burn_while_hard_blocked(monkeypatch):
    """After a long hard block clears, load still holds the task for the full
    soft budget — the budget must not have burned during the hard block."""
    calls = {"n": 0, "load_checks": 0}

    async def busy_first_20():
        calls["n"] += 1
        return calls["n"] <= 20

    def under_load():
        calls["load_checks"] += 1
        return calls["load_checks"] <= 5   # stays loaded for 5 soft polls

    monkeypatch.setattr(gate, "external_pipeline_busy", busy_first_20)
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    monkeypatch.setattr(gate, "system_under_load", under_load)

    async def fast_sleep(_):
        pass
    monkeypatch.setattr(gate.asyncio, "sleep", fast_sleep)
    assert _run(gate.wait_for_capacity("t", max_wait=10)) is True
    # All 5 loaded polls happened after the hard block cleared: the soft
    # budget (10) outlived them, so release came from load clearing.
    assert calls["load_checks"] == 6


def test_external_pipeline_busy_unreachable_is_not_busy(monkeypatch):
    monkeypatch.setenv("BRIDGE_BASE_URL", "http://127.0.0.1:1")  # nothing there
    assert _run(gate.external_pipeline_busy()) is False


def test_quiet_gate_max_wait_override(monkeypatch):
    """wait_for_interactive_quiet returns at the override deadline even while
    foreground-active (callers then defer)."""
    monkeypatch.setattr(gate, "_LAST_BROWSER_ACTIVITY", time.monotonic())
    monkeypatch.setattr(gate, "_has_active_chat_stream", lambda: False)
    t0 = time.monotonic()
    assert _run(gate.wait_for_interactive_quiet("t", max_wait_override=0.3)) is True
    assert time.monotonic() - t0 < 2.0
