"""Tests for the watchdog's transport-health (turn-boundary 422 pin) heal rung."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import WatchdogConfig


def _make_chat_room(room_id: str) -> MagicMock:
    room = MagicMock()
    room.id = room_id
    return room


def _make_chats_response(rooms: list) -> MagicMock:
    resp = MagicMock()
    resp.data = rooms
    return resp


def _make_message(message_id: str, inserted_at: datetime) -> MagicMock:
    msg = MagicMock()
    msg.id = message_id
    msg.sender_id = "someone-else"
    msg.inserted_at = inserted_at
    return msg


def _make_messages_response(messages: list) -> MagicMock:
    resp = MagicMock()
    resp.data = messages
    return resp


def _make_participant(pid: str, name: str | None = None) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.name = name or pid
    p.type = "Agent"
    return p


def _make_participants_response(items: list) -> MagicMock:
    resp = MagicMock()
    resp.data = [_make_participant(*i) if isinstance(i, tuple) else _make_participant(i)
                 for i in items]
    return resp


def _make_conductor_rest_client() -> MagicMock:
    """REST client for the Watchdog's borrowed Conductor credentials."""
    client = MagicMock()
    client.agent_api_chats = MagicMock()
    client.agent_api_chats.list_agent_chats = AsyncMock(
        return_value=_make_chats_response([_make_chat_room("room-1")]),
    )
    client.agent_api_messages = MagicMock()
    client.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=_make_messages_response([]),
    )
    client.agent_api_messages.create_agent_chat_message = AsyncMock()
    client.agent_api_participants = MagicMock()
    client.agent_api_participants.list_agent_chat_participants = AsyncMock(
        return_value=_make_participants_response(
            ["agent-cond", "agent-coder"],
        ),
    )
    return client


def _make_agent_rest_client(
    processing_messages: list,
    pending_messages: list | None = None,
    verify_pending_messages: list | None = None,
) -> MagicMock:
    """REST client for one agent's own deliveries (used by the heal rung).

    processing_messages: returned for status="processing" probes.
    pending_messages: returned for the first status="pending" probe (detection).
    verify_pending_messages: returned for subsequent pending probes (verify re-list).
    Defaults to empty lists so existing tests that only pass processing_messages work.
    """
    client = MagicMock()
    client.agent_api_messages = MagicMock()

    _pending = pending_messages or []
    _verify = verify_pending_messages if verify_pending_messages is not None else []
    _pending_call_count: dict[str, int] = {"n": 0}

    async def _list_side_effect(*args: object, **kwargs: object) -> MagicMock:
        status = kwargs.get("status", "")
        if status == "processing":
            return _make_messages_response(processing_messages)
        if status == "pending":
            _pending_call_count["n"] += 1
            if _pending_call_count["n"] == 1:
                return _make_messages_response(_pending)
            return _make_messages_response(_verify)
        return _make_messages_response([])

    client.agent_api_messages.list_agent_messages = AsyncMock(
        side_effect=_list_side_effect,
    )
    client.agent_api_messages.mark_agent_message_processed = AsyncMock()
    client.agent_api_messages.mark_agent_message_processing = AsyncMock()
    return client


@pytest.fixture
def watchdog_config() -> WatchdogConfig:
    return WatchdogConfig(
        # T is the pin threshold; default would be 1800s but tests use a
        # shorter horizon so the timestamps below stay readable.
        transport_pin_threshold_seconds=600,
        transport_heal_max_attempts=3,
    )


def _make_daemon(
    watchdog_config: WatchdogConfig,
    conductor_rest: MagicMock,
    agent_rest_clients: dict[str, MagicMock],
):
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=watchdog_config,
        rest_client=conductor_rest,
        agent_id="agent-cond",
        conductor_id="agent-cond",
        agent_rest_clients=agent_rest_clients,
    )


@pytest.mark.asyncio
async def test_stuck_processing_past_threshold_is_healed(watchdog_config):
    """A delivery stuck in processing past T → mark_processed called once."""
    now = datetime.now(UTC)
    stuck = _make_message("msg-pinned", now - timedelta(seconds=900))  # > T (600)
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client([stuck])
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    agent_client.agent_api_messages.list_agent_messages.assert_any_await(
        chat_id="room-1", status="processing", page_size=100,
    )
    agent_client.agent_api_messages.mark_agent_message_processed.assert_awaited_once_with(
        chat_id="room-1", id="msg-pinned",
    )
    # Successful heal clears tracking.
    assert ("agent-coder", "msg-pinned") not in daemon._pin_heal_attempts
    assert ("agent-coder", "msg-pinned") not in daemon._pin_escalated


@pytest.mark.asyncio
async def test_fresh_processing_below_threshold_is_left_alone(watchdog_config):
    """A processing delivery younger than T → no heal (mid-turn safety)."""
    now = datetime.now(UTC)
    fresh = _make_message("msg-fresh", now - timedelta(seconds=60))  # < T
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client([fresh])
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()


@pytest.mark.asyncio
async def test_idempotent_already_processed_returns_422_treated_as_noop(
    watchdog_config,
):
    """Re-asserting processed on an already-processed delivery returns 422
    ("no active processing attempt") — treated as a successful no-op."""
    from thenvoi_rest.core.api_error import ApiError

    now = datetime.now(UTC)
    stuck = _make_message("msg-already-done", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client([stuck])
    agent_client.agent_api_messages.mark_agent_message_processed = AsyncMock(
        side_effect=ApiError(
            status_code=422, headers={},
            body="no active processing attempt",
        ),
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    # The 422 is swallowed — no tracking added, no escalation.
    pin_key = ("agent-coder", "msg-already-done")
    assert pin_key not in daemon._pin_heal_attempts
    assert pin_key not in daemon._pin_escalated
    # And no owner-escalation post was made on the watchdog's own client.
    conductor_rest.agent_api_messages.create_agent_chat_message.assert_not_called()


@pytest.mark.asyncio
async def test_max_attempts_escalates_and_stops_healing(watchdog_config):
    """After N non-422 failures, escalate once and stop healing this pin."""
    from thenvoi_rest.core.api_error import ApiError

    now = datetime.now(UTC)
    stuck = _make_message("msg-bad-server", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client([stuck])
    agent_client.agent_api_messages.mark_agent_message_processed = AsyncMock(
        side_effect=ApiError(status_code=500, headers={}, body="server error"),
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    # Three failing patrols → escalation on the 3rd.
    for _ in range(3):
        await daemon._patrol()

    pin_key = ("agent-coder", "msg-bad-server")
    assert pin_key in daemon._pin_escalated
    assert daemon._pin_heal_attempts[pin_key] == 3
    # Exactly 3 heal attempts.
    assert agent_client.agent_api_messages.mark_agent_message_processed.await_count == 3
    # Owner-style escalation post on the watchdog's own (Conductor) client.
    posts = conductor_rest.agent_api_messages.create_agent_chat_message
    assert posts.await_count == 1

    # Fourth patrol: no further heal calls, no further escalations.
    await daemon._patrol()
    assert agent_client.agent_api_messages.mark_agent_message_processed.await_count == 3
    assert posts.await_count == 1


@pytest.mark.asyncio
async def test_kill_switch_disables_rung_entirely(watchdog_config):
    """transport_heal_enabled=False → no list-processing or mark-processed calls."""
    watchdog_config = WatchdogConfig(
        transport_heal_enabled=False,
        transport_pin_threshold_seconds=600,
    )
    now = datetime.now(UTC)
    stuck = _make_message("msg-pinned", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client([stuck])
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    agent_client.agent_api_messages.list_agent_messages.assert_not_called()
    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()


@pytest.mark.asyncio
async def test_existing_nudge_path_unaffected_when_no_pins(watchdog_config):
    """When no transport pins exist, the chat-recency nudge path runs normally.

    A stale-by-chat agent still gets nudged via the existing path; the new
    rung adds no calls and does not suppress the legacy behavior.
    """
    now = datetime.now(UTC)
    old_msg = MagicMock()
    old_msg.sender_id = "agent-coder"
    old_msg.inserted_at = now - timedelta(minutes=20)  # past stale threshold
    old_msg.content = ""

    conductor_rest = _make_conductor_rest_client()
    conductor_rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=_make_messages_response([old_msg]),
    )
    # No processing messages — pin rung finds nothing.
    agent_client = _make_agent_rest_client([])
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    # Pin rung made the read but no heal.
    agent_client.agent_api_messages.list_agent_messages.assert_awaited()
    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()
    # Nudge was sent through the regular path (one create_agent_chat_message
    # on the watchdog's own client).
    nudges = conductor_rest.agent_api_messages.create_agent_chat_message
    assert nudges.await_count == 1


@pytest.mark.asyncio
async def test_skip_self_agent_id(watchdog_config):
    """The watchdog's own (Conductor) id is skipped — its credentials are
    already used by `self._rest` and probing the same row via two clients is
    redundant."""
    now = datetime.now(UTC)
    stuck = _make_message("msg-pinned", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    # Map an entry under the conductor's own id — must be skipped.
    own_client = _make_agent_rest_client([stuck])
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-cond": own_client},
    )

    await daemon._patrol()

    own_client.agent_api_messages.list_agent_messages.assert_not_called()
    own_client.agent_api_messages.mark_agent_message_processed.assert_not_called()


@pytest.mark.asyncio
async def test_empty_clients_map_is_noop(watchdog_config):
    """No per-agent clients → rung is a no-op (degrades gracefully)."""
    conductor_rest = _make_conductor_rest_client()
    daemon = _make_daemon(watchdog_config, conductor_rest, {})

    await daemon._patrol()

    # No external probes triggered by the new rung; only the patrol's own
    # listing calls fire.
    conductor_rest.agent_api_messages.create_agent_chat_message.assert_not_called()


# ---------------------------------------------------------------------------
# Pending-bucket (post-turn 422 class) tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_head_older_than_threshold_is_healed(watchdog_config):
    """Pending head older than T → 2-step heal + verify → AGENT_PIN_HEALED, tracking cleared."""
    now = datetime.now(UTC)
    head = _make_message("msg-pending-head", now - timedelta(seconds=900))  # > T (600)
    conductor_rest = _make_conductor_rest_client()
    # First pending probe returns [head]; verify re-list returns [] (cursor advanced).
    agent_client = _make_agent_rest_client(
        processing_messages=[],
        pending_messages=[head],
        verify_pending_messages=[],
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    # Both 2-step calls must fire, in order.
    processing_call = agent_client.agent_api_messages.mark_agent_message_processing
    processed_call = agent_client.agent_api_messages.mark_agent_message_processed
    processing_call.assert_awaited_once_with(chat_id="room-1", id="msg-pending-head")
    processed_call.assert_awaited_once_with(chat_id="room-1", id="msg-pending-head")

    # Successful verify → tracking cleared.
    pin_key = ("agent-coder", "msg-pending-head")
    assert pin_key not in daemon._pin_heal_attempts
    assert pin_key not in daemon._pin_escalated


@pytest.mark.asyncio
async def test_pending_heal_verify_same_head_increments_and_escalates(watchdog_config):
    """When verify re-list still shows the same head, increment attempts; escalate at max."""
    now = datetime.now(UTC)
    head = _make_message("msg-stubborn", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    # Both detection and verify always return [head] — heal never advances cursor.
    agent_client = _make_agent_rest_client(
        processing_messages=[],
        pending_messages=[head],
        verify_pending_messages=[head],
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    pin_key = ("agent-coder", "msg-stubborn")
    posts = conductor_rest.agent_api_messages.create_agent_chat_message

    # Two failing patrols — no escalation yet.
    for _ in range(2):
        await daemon._patrol()

    assert pin_key not in daemon._pin_escalated
    assert daemon._pin_heal_attempts[pin_key] == 2
    # No AGENT_PIN_HEALED activity logged.
    posts.assert_not_called()

    # Third patrol — hits max_attempts (3).
    await daemon._patrol()

    assert pin_key in daemon._pin_escalated
    assert daemon._pin_heal_attempts[pin_key] == 3
    # Exactly one escalation post.
    assert posts.await_count == 1

    # Fourth patrol — heal skipped entirely (pin already in _pin_escalated).
    await daemon._patrol()
    assert posts.await_count == 1
    # mark_agent_message_processing was called 3 times (once per healing patrol).
    assert agent_client.agent_api_messages.mark_agent_message_processing.await_count == 3


@pytest.mark.asyncio
async def test_processing_head_is_one_step_unchanged(watchdog_config):
    """Processing head older than T → 1-step processed only; mark_agent_message_processing
    must NOT be called — processing-bucket behavior is byte-for-byte unchanged."""
    now = datetime.now(UTC)
    stuck = _make_message("msg-crash-recovery", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client(processing_messages=[stuck])
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    agent_client.agent_api_messages.mark_agent_message_processed.assert_awaited_once_with(
        chat_id="room-1", id="msg-crash-recovery",
    )
    agent_client.agent_api_messages.mark_agent_message_processing.assert_not_called()


@pytest.mark.asyncio
async def test_pending_head_younger_than_threshold_no_heal(watchdog_config):
    """Pending head younger than T → no heal calls (mid-turn safety)."""
    now = datetime.now(UTC)
    fresh = _make_message("msg-fresh-pending", now - timedelta(seconds=60))  # < T
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client(
        processing_messages=[],
        pending_messages=[fresh],
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    agent_client.agent_api_messages.mark_agent_message_processing.assert_not_called()
    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()


@pytest.mark.asyncio
async def test_only_pending_head_is_healed_not_non_head_entries(watchdog_config):
    """Three pending records → only data[0] (head) healed; non-head get no /processing call."""
    now = datetime.now(UTC)
    head = _make_message("msg-head", now - timedelta(seconds=900))
    msg2 = _make_message("msg-second", now - timedelta(seconds=800))
    msg3 = _make_message("msg-third", now - timedelta(seconds=700))
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client(
        processing_messages=[],
        pending_messages=[head, msg2, msg3],
        verify_pending_messages=[],
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    # /processing called exactly once, only for the head.
    processing_calls = agent_client.agent_api_messages.mark_agent_message_processing
    processing_calls.assert_awaited_once_with(chat_id="room-1", id="msg-head")

    # /processed also called once for the head.
    agent_client.agent_api_messages.mark_agent_message_processed.assert_awaited_once_with(
        chat_id="room-1", id="msg-head",
    )

    # Non-head ids never appear in any call.
    all_processing_calls = [str(c) for c in processing_calls.await_args_list]
    assert not any("msg-second" in c or "msg-third" in c for c in all_processing_calls)


@pytest.mark.asyncio
async def test_pending_probe_api_error_no_crash_patrol_continues(watchdog_config):
    """Pending probe raises ApiError → no crash, no heal; patrol completes normally."""
    from thenvoi_rest.core.api_error import ApiError

    now = datetime.now(UTC)
    conductor_rest = _make_conductor_rest_client()
    agent_client = MagicMock()
    agent_client.agent_api_messages = MagicMock()

    async def _list_side_effect(*args: object, **kwargs: object) -> MagicMock:
        status = kwargs.get("status", "")
        if status == "processing":
            return _make_messages_response([])
        raise ApiError(status_code=429, headers={}, body="rate limited")

    agent_client.agent_api_messages.list_agent_messages = AsyncMock(
        side_effect=_list_side_effect,
    )
    agent_client.agent_api_messages.mark_agent_message_processed = AsyncMock()
    agent_client.agent_api_messages.mark_agent_message_processing = AsyncMock()

    daemon = _make_daemon(watchdog_config, conductor_rest, {"agent-coder": agent_client})

    # Must not raise.
    await daemon._patrol()

    agent_client.agent_api_messages.mark_agent_message_processing.assert_not_called()
    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()


@pytest.mark.asyncio
async def test_pending_probe_generic_exception_no_crash(watchdog_config):
    """Pending probe raises a generic Exception → no crash, no heal."""
    now = datetime.now(UTC)
    conductor_rest = _make_conductor_rest_client()
    agent_client = MagicMock()
    agent_client.agent_api_messages = MagicMock()

    async def _list_side_effect(*args: object, **kwargs: object) -> MagicMock:
        status = kwargs.get("status", "")
        if status == "processing":
            return _make_messages_response([])
        raise RuntimeError("network gone")

    agent_client.agent_api_messages.list_agent_messages = AsyncMock(
        side_effect=_list_side_effect,
    )
    agent_client.agent_api_messages.mark_agent_message_processed = AsyncMock()
    agent_client.agent_api_messages.mark_agent_message_processing = AsyncMock()

    daemon = _make_daemon(watchdog_config, conductor_rest, {"agent-coder": agent_client})

    await daemon._patrol()

    agent_client.agent_api_messages.mark_agent_message_processing.assert_not_called()
    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()


@pytest.mark.asyncio
async def test_kill_switch_disables_both_probes(watchdog_config):
    """transport_heal_enabled=False → neither pending nor processing probe runs."""
    watchdog_config = WatchdogConfig(
        transport_heal_enabled=False,
        transport_pin_threshold_seconds=600,
    )
    now = datetime.now(UTC)
    head = _make_message("msg-pending-head", now - timedelta(seconds=900))
    conductor_rest = _make_conductor_rest_client()
    agent_client = _make_agent_rest_client(
        processing_messages=[head],
        pending_messages=[head],
    )
    daemon = _make_daemon(
        watchdog_config, conductor_rest, {"agent-coder": agent_client},
    )

    await daemon._patrol()

    agent_client.agent_api_messages.list_agent_messages.assert_not_called()
    agent_client.agent_api_messages.mark_agent_message_processing.assert_not_called()
    agent_client.agent_api_messages.mark_agent_message_processed.assert_not_called()
