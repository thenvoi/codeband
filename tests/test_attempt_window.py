"""Tests for the attempt-window observability instrument.

The instrument is a *pure observer* over the SDK ack path. The central
guarantee these tests defend is behavioral equivalence: wrapping
``mark_processing`` / ``mark_processed`` must not change their return values,
their exceptions, the SDK's swallow-the-422 behavior, or their side effects.

Construction note: tests build a REAL :class:`ThenvoiLink` and fake only the
lowest transport seam (``agent_api_messages._raw_client``), so the class-level
wraps on the real ``AsyncAgentApiMessagesClient`` methods are genuinely
exercised — not bypassed by a fake REST object.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import codeband.monitoring.attempt_window as aw
from codeband.monitoring.activity_log import ActivityLogger
from thenvoi.platform.link import ThenvoiLink
from thenvoi_rest.agent_api_messages.client import AsyncAgentApiMessagesClient
from thenvoi_rest.core.api_error import ApiError

# True SDK originals, snapshotted before any install can run. The autouse
# fixture restores these around every test so each starts from a clean,
# unpatched runtime (install() re-captures originals when the sentinel is gone).
_TRUE = {
    "link_processing": ThenvoiLink.mark_processing,
    "link_processed": ThenvoiLink.mark_processed,
    "rest_processing": AsyncAgentApiMessagesClient.mark_agent_message_processing,
    "rest_processed": AsyncAgentApiMessagesClient.mark_agent_message_processed,
}


def _restore_originals() -> None:
    ThenvoiLink.mark_processing = _TRUE["link_processing"]
    ThenvoiLink.mark_processed = _TRUE["link_processed"]
    AsyncAgentApiMessagesClient.mark_agent_message_processing = _TRUE["rest_processing"]
    AsyncAgentApiMessagesClient.mark_agent_message_processed = _TRUE["rest_processed"]


@pytest.fixture(autouse=True)
def _reset_instrument():
    _restore_originals()
    aw._open_registry.clear()
    aw._activity = None
    yield
    _restore_originals()
    aw._open_registry.clear()
    aw._activity = None


# --- transport doubles -----------------------------------------------------


class _FakeRaw:
    """Stand-in for ``AsyncRawAgentApiMessagesClient`` (the transport seam).

    ``processing`` / ``processed`` are zero-arg callables that either return a
    response object (with a ``.data`` attribute, as the real raw client does) or
    raise. Default = succeed with empty data.
    """

    def __init__(self, processing=None, processed=None):
        self._processing = processing
        self._processed = processed
        self.processing_calls = 0
        self.processed_calls = 0

    async def mark_agent_message_processing(self, chat_id, id, *, request_options=None):
        self.processing_calls += 1
        if self._processing is not None:
            return self._processing()
        return SimpleNamespace(data=None)

    async def mark_agent_message_processed(self, chat_id, id, *, request_options=None):
        self.processed_calls += 1
        if self._processed is not None:
            return self._processed()
        return SimpleNamespace(data=None)


def _make_link(agent_id="agent-A", processing=None, processed=None):
    """Real ThenvoiLink with only the raw transport faked."""
    link = ThenvoiLink(agent_id=agent_id, api_key="test-key")
    raw = _FakeRaw(processing=processing, processed=processed)
    # Touch the lazy property once, then swap its transport.
    link.rest.agent_api_messages._raw_client = raw
    return link, raw


def _raises_422():
    raise ApiError(status_code=422, body="already processed")


def _raises_500():
    raise ApiError(status_code=500, body="boom")


def _read_markers(log_path):
    rows = []
    if not log_path.exists():
        return rows
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        evt = json.loads(line)
        if evt.get("event_type") == aw.EVENT_TYPE:
            rows.append(evt)
    return rows


@pytest.fixture
def activity(tmp_path):
    return ActivityLogger(tmp_path / "activity.jsonl"), tmp_path / "activity.jsonl"


# --- 1: elapsed for a short message ---------------------------------------


async def test_short_message_logs_elapsed(activity):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)
    link, raw = _make_link(agent_id="agent-A")

    await link.mark_processing("room-1", "msg-1")
    await link.mark_processed("room-1", "msg-1")

    rows = _read_markers(path)
    assert len(rows) == 1
    d = rows[0]["details"]
    assert d["msg_id"] == "msg-1"
    assert d["room_id"] == "room-1"
    assert d["agent_id"] == "agent-A"
    assert d["ack_422"] is False
    assert d["elapsed_seconds"] is not None and d["elapsed_seconds"] >= 0
    assert raw.processing_calls == 1 and raw.processed_calls == 1


# --- 2: elapsed reflects a long handler -----------------------------------


async def test_long_handler_elapsed_reflects_clock(activity, monkeypatch):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)
    link, _ = _make_link()

    # Drive monotonic: open at t=100, ack at t=142.5 → elapsed 42.5s.
    ticks = iter([100.0, 142.5])
    monkeypatch.setattr(aw, "_monotonic", lambda: next(ticks))

    await link.mark_processing("room-1", "msg-long")
    await link.mark_processed("room-1", "msg-long")

    rows = _read_markers(path)
    assert len(rows) == 1
    assert rows[0]["details"]["elapsed_seconds"] == 42.5


# --- 3: both receive paths emit a marker ----------------------------------


async def test_both_receive_paths_emit(activity, monkeypatch):
    from unittest.mock import AsyncMock

    from thenvoi.platform.event import MessageEvent
    from thenvoi.client.streaming import MessageCreatedPayload
    from thenvoi.runtime.execution import ExecutionContext
    from thenvoi.runtime.types import PlatformMessage
    from datetime import datetime, timezone

    logger, path = activity
    aw.install_attempt_window_instrument(logger)
    link, _ = _make_link(agent_id="agent-A")

    executed = []

    async def _on_execute(ctx, event):
        executed.append(event)

    ctx = ExecutionContext("room-1", link, _on_execute, agent_id="agent-A")
    # Keep the test off the network: hydration is irrelevant to the ack path.
    monkeypatch.setattr(ctx, "_ensure_fresh_context", AsyncMock())

    # WS path.
    ws_event = MessageEvent(
        room_id="room-1",
        payload=MessageCreatedPayload(
            id="ws-msg",
            content="hi",
            message_type="text",
            sender_id="user-1",
            sender_type="User",
            inserted_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        ),
    )
    await ctx._process_event(ws_event)

    # /next backlog path.
    backlog_msg = PlatformMessage(
        id="backlog-msg",
        room_id="room-1",
        content="hi",
        sender_id="user-1",
        sender_type="User",
        sender_name=None,
        message_type="text",
        metadata={},
        created_at=datetime.now(timezone.utc),
    )
    await ctx._process_backlog_message(backlog_msg)

    rows = _read_markers(path)
    ids = {r["details"]["msg_id"] for r in rows}
    assert "ws-msg" in ids, "WS receive path did not emit a marker"
    assert "backlog-msg" in ids, "/next backlog path did not emit a marker"
    assert len(executed) == 2


# --- 4: CRITICAL — behavioral equivalence on the swallowed-422 path -------


async def test_behavioral_equivalence_422_swallow(activity):
    logger, path = activity

    # BEFORE install: SDK swallows the 422 — returns None, never raises.
    link_b, raw_b = _make_link(processed=_raises_422)
    pre_processing = await link_b.mark_processing("room-1", "msg-x")
    pre_processed = await link_b.mark_processed("room-1", "msg-x")
    assert pre_processing is None
    assert pre_processed is None
    assert raw_b.processing_calls == 1 and raw_b.processed_calls == 1

    # AFTER install: identical observable behavior, plus the marker.
    aw.install_attempt_window_instrument(logger)
    link_a, raw_a = _make_link(processed=_raises_422)
    post_processing = await link_a.mark_processing("room-1", "msg-x")
    post_processed = await link_a.mark_processed("room-1", "msg-x")

    assert post_processing is None, "wrap changed mark_processing return"
    assert post_processed is None, "wrap un-swallowed the 422 / changed return"
    assert raw_a.processing_calls == 1 and raw_a.processed_calls == 1

    rows = _read_markers(path)
    assert len(rows) == 1
    assert rows[0]["details"]["ack_422"] is True


# --- 5: 422 flag variants --------------------------------------------------


async def test_non_422_error_not_flagged_and_still_swallowed(activity):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)
    link, _ = _make_link(processed=_raises_500)

    result = await link.mark_processed("room-1", "msg-500")  # no open recorded
    assert result is None  # 500 still swallowed by the SDK

    rows = _read_markers(path)
    assert len(rows) == 1
    assert rows[0]["details"]["ack_422"] is False
    assert rows[0]["details"]["elapsed_seconds"] is None


async def test_success_not_flagged(activity):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)
    link, _ = _make_link()

    await link.mark_processing("room-1", "ok")
    await link.mark_processed("room-1", "ok")

    rows = _read_markers(path)
    assert rows[0]["details"]["ack_422"] is False


# --- 6: watchdog isolation (direct REST calls are invisible) --------------


async def test_watchdog_direct_calls_isolated(activity):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)

    # Simulate the watchdog: call the REST methods DIRECTLY (no ThenvoiLink
    # frame → no contextvar slot), with the transport-heal's intentional 422.
    direct, raw = _make_link(processed=_raises_422)
    rest = direct.rest.agent_api_messages

    # The unwrapped original raises ApiError(422); the wrapper must re-raise it
    # identically for a direct caller (the watchdog catches it itself).
    with pytest.raises(ApiError) as exc:
        await rest.mark_agent_message_processed(chat_id="room-9", id="msg-heal")
    assert exc.value.status_code == 422

    # No marker emitted for the direct call, and no leaked state.
    assert _read_markers(path) == []

    # A SUBSEQUENT real ThenvoiLink ack for the SAME (room, msg) must not be
    # contaminated by the watchdog's earlier 422.
    link, _ = _make_link(agent_id="agent-A")
    await link.mark_processing("room-9", "msg-heal")
    await link.mark_processed("room-9", "msg-heal")

    rows = _read_markers(path)
    assert len(rows) == 1
    assert rows[0]["details"]["ack_422"] is False, "watchdog 422 contaminated a real marker"


# --- 7: swallowed processing failure → elapsed None -----------------------


async def test_swallowed_processing_failure_yields_none_elapsed(activity):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)
    # mark_processing transport fails (SDK swallows); mark_processed succeeds.
    link, _ = _make_link(processing=_raises_500)

    proc = await link.mark_processing("room-1", "msg-noopen")
    assert proc is None  # failure swallowed
    await link.mark_processed("room-1", "msg-noopen")

    rows = _read_markers(path)
    assert len(rows) == 1
    assert rows[0]["details"]["elapsed_seconds"] is None


# --- 8: leak bound ---------------------------------------------------------


async def test_open_registry_leak_bound(activity, monkeypatch):
    logger, _ = activity
    aw.install_attempt_window_instrument(logger)
    monkeypatch.setattr(aw, "_MAX_OPEN", 16)
    link, _ = _make_link(agent_id="agent-A")

    # Many opens with no matching processed → registry must stay capped.
    for i in range(100):
        await link.mark_processing("room-1", f"msg-{i}")

    assert len(aw._open_registry) <= 16


# --- 9: multi-agent key isolation (round-2 regression) --------------------


async def test_two_agents_same_room_message_isolated(activity, monkeypatch):
    logger, path = activity
    aw.install_attempt_window_instrument(logger)

    link_a, _ = _make_link(agent_id="agent-A")
    link_b, _ = _make_link(agent_id="agent-B")

    # Both agents open the SAME (room, message); distinct windows by clock.
    ticks = iter([10.0, 25.0, 100.0, 130.0])  # A.open, B.open, A.ack, B.ack
    monkeypatch.setattr(aw, "_monotonic", lambda: next(ticks))

    await link_a.mark_processing("room-1", "shared-msg")  # t=10
    await link_b.mark_processing("room-1", "shared-msg")  # t=25
    await link_a.mark_processed("room-1", "shared-msg")   # t=100 → 90.0
    await link_b.mark_processed("room-1", "shared-msg")   # t=130 → 105.0

    rows = _read_markers(path)
    by_agent = {r["details"]["agent_id"]: r["details"]["elapsed_seconds"] for r in rows}
    assert by_agent == {"agent-A": 90.0, "agent-B": 105.0}, (
        "agents collided on (room, msg) — identity not isolating opens"
    )
