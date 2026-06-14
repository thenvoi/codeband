"""Tests for the watchdog task done-callback (T-16).

Verifies that an unexpected watchdog crash logs loudly, while normal
cancellation (shutdown) is silent.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from codeband.orchestration.runner import _make_watchdog_done_callback


class _FakeActivity:
    def __init__(self):
        self.events: list[tuple[str, str, str]] = []

    def log(self, event_type: str, actor: str, summary: str) -> None:
        self.events.append((event_type, actor, summary))


async def test_exception_logs_error_and_records_activity(caplog):
    """An unexpected exception in the watchdog task logs at ERROR level."""
    activity = _FakeActivity()
    callback = _make_watchdog_done_callback(activity)

    async def _raise():
        raise RuntimeError("sentinel boom")

    t = asyncio.get_event_loop().create_task(_raise())
    await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="codeband.orchestration.runner"):
        callback(t)

    errors = [
        r for r in caplog.records
        if r.name == "codeband.orchestration.runner" and r.levelno == logging.ERROR
    ]
    assert errors, "expected at least one ERROR log"
    assert "watchdog" in errors[0].getMessage().lower()
    assert "RuntimeError" in errors[0].getMessage()
    assert "sentinel boom" in errors[0].getMessage()

    crash_events = [e for e in activity.events if e[0] == "WATCHDOG_CRASH"]
    assert crash_events, "expected a WATCHDOG_CRASH activity event"
    assert "RuntimeError" in crash_events[0][2]


async def test_cancellation_is_silent(caplog):
    """Normal cancellation (shutdown path) must not produce any log output."""
    activity = _FakeActivity()
    callback = _make_watchdog_done_callback(activity)

    async def _sleep_forever():
        await asyncio.sleep(3600)

    t = asyncio.get_event_loop().create_task(_sleep_forever())
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass

    with caplog.at_level(logging.DEBUG, logger="codeband.orchestration.runner"):
        callback(t)

    assert not [
        r for r in caplog.records
        if r.name.startswith("codeband") and r.levelno >= logging.WARNING
    ], "cancellation should not produce any warning/error logs"
    assert not activity.events, "cancellation should not record any activity events"


async def test_normal_exit_is_silent(caplog):
    """A watchdog that exits cleanly (no exception, not cancelled) is also silent."""
    activity = _FakeActivity()
    callback = _make_watchdog_done_callback(activity)

    async def _ok():
        return 42

    t = asyncio.get_event_loop().create_task(_ok())
    await asyncio.sleep(0)

    with caplog.at_level(logging.DEBUG, logger="codeband.orchestration.runner"):
        callback(t)

    assert not [
        r for r in caplog.records
        if r.name.startswith("codeband") and r.levelno >= logging.ERROR
    ]
    assert not activity.events
