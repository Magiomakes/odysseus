"""A wedged or slow agent-loop stream must not hold the serial execution slot.

Task execution acquires a single ``Semaphore(1)`` slot, and ``_run_agent_loop``
consumes ``stream_agent_loop`` with an ``async for``. Without a ceiling, one
half-open / trickling stream holds that slot forever and every later task queues
behind it indefinitely — the scheduler keeps ticking but nothing ever runs
(observed as "no task fired for weeks"). ``task_agent_timeout_seconds`` caps the
loop: on expiry the stream is cancelled, partial output is kept, and the slot is
released so the queue drains.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.helpers.import_state import clear_fake_database_modules

clear_fake_database_modules()

import src.agent_loop as agent_loop
import src.settings as settings
import src.task_endpoint as task_endpoint
import src.task_scheduler as ts


def _run(coro):
    # Outer guard so a regression (no cap) fails the test instead of hanging it.
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


def _patch_common(monkeypatch, budget):
    # No real endpoints / fallbacks — keep the unit hermetic and fast.
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates",
                        lambda **kw: [], raising=False)
    monkeypatch.setattr(settings, "get_setting",
                        lambda key, default=None: budget if key == "task_agent_timeout_seconds" else default)


def test_hanging_stream_is_capped_and_returns_partial(monkeypatch):
    """A stream that emits one token then hangs is cancelled at the budget,
    and the partial token is preserved with a stop note."""

    async def _hanging_stream(**kwargs):
        yield 'data: {"delta": "partial answer"}'
        await asyncio.sleep(3600)  # wedge
        yield "data: [DONE]"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", _hanging_stream, raising=False)
    _patch_common(monkeypatch, budget=1)

    sched = ts.TaskScheduler(MagicMock())
    task = SimpleNamespace(id="t-hang", prompt="do thing", owner="orion", max_steps=5)
    result = _run(sched._run_agent_loop("http://h/v1/chat/completions", "m", task, "sess"))

    assert "partial answer" in result          # partial output preserved
    assert "time budget" in result.lower()      # stop note appended


def test_disabled_budget_allows_completion(monkeypatch):
    """budget=0 disables the cap — a normal (fast) stream completes untouched."""

    async def _quick_stream(**kwargs):
        yield 'data: {"delta": "all done"}'
        yield "data: [DONE]"

    monkeypatch.setattr(agent_loop, "stream_agent_loop", _quick_stream, raising=False)
    _patch_common(monkeypatch, budget=0)

    sched = ts.TaskScheduler(MagicMock())
    task = SimpleNamespace(id="t-ok", prompt="do thing", owner="orion", max_steps=5)
    result = _run(sched._run_agent_loop("http://h/v1/chat/completions", "m", task, "sess"))

    assert result == "all done"
    assert "time budget" not in result.lower()
