"""Tests for calling MCP clients, whose synchronous methods use asyncio.run, from any context."""

import asyncio

from fcr_curator.mcp import outside_event_loop


async def answer() -> int:
    """Return 42, as an MCP client's coroutine would return its result."""
    return 42


def test_outside_event_loop_calls_directly_when_no_loop_is_running() -> None:
    """From a laptop script, the call runs as is."""
    assert outside_event_loop(lambda: asyncio.run(answer())) == 42


def test_outside_event_loop_lets_asyncio_run_work_inside_a_running_loop() -> None:
    """In a notebook, where a loop is already running, asyncio.run would fail; a worker thread avoids that."""

    async def notebook_cell() -> int:
        return outside_event_loop(lambda: asyncio.run(answer()))

    assert asyncio.run(notebook_cell()) == 42
