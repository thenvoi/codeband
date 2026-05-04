"""Tests for deterministic watchdog daemon."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import WatchdogConfig


def _make_chat_room(room_id: str) -> MagicMock:
    """Create a mock ChatRoom Pydantic model."""
    room = MagicMock()
    room.id = room_id
    return room


def _make_chats_response(rooms: list) -> MagicMock:
    """Create a mock ListAgentChatsResponse."""
    resp = MagicMock()
    resp.data = rooms
    return resp


def _make_message(sender_id: str, minutes_ago: float) -> MagicMock:
    """Create a mock ChatMessage Pydantic model."""
    msg = MagicMock()
    msg.sender_id = sender_id
    msg.inserted_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return msg


def _make_messages_response(messages: list) -> MagicMock:
    """Create a mock ListAgentMessagesResponse."""
    resp = MagicMock()
    resp.data = messages
    return resp


def _make_participant(
    pid: str, display_name: str | None = None, ptype: str = "Agent",
) -> MagicMock:
    """Create a mock ChatParticipantDetails.

    Display name defaults to the id so older tests that pass plain ids keep
    working without modification. ``ptype`` defaults to ``"Agent"`` to match
    the SDK's ``ChatParticipantType`` literal; set to ``"User"`` for humans.
    """
    p = MagicMock()
    p.id = pid
    p.name = display_name if display_name is not None else pid
    p.type = ptype
    return p


def _make_participants_response(items: list) -> MagicMock:
    """Create a mock ListAgentChatParticipantsResponse.

    Accepts plain ids (``"agent-p0"``), ``(id, display_name)`` tuples, or
    ``(id, display_name, type)`` tuples where ``type`` is ``"Agent"`` or
    ``"User"``.
    """
    resp = MagicMock()
    parts: list[MagicMock] = []
    for item in items:
        if isinstance(item, tuple):
            parts.append(_make_participant(*item))
        else:
            parts.append(_make_participant(item))
    resp.data = parts
    return resp


class TestWatchdogDaemon:
    """Tests for the deterministic watchdog patrol loop."""

    @pytest.fixture
    def watchdog_config(self) -> WatchdogConfig:
        return WatchdogConfig(
            check_interval_seconds=120,
            stale_threshold_seconds=300,
        )

    @pytest.fixture
    def mock_rest_client(self):
        """Mock Band.ai REST client.

        By default, participants includes every agent id used in existing tests
        so the participant filter is a no-op for those cases.
        """
        client = MagicMock()
        client.agent_api_chats = MagicMock()
        client.agent_api_messages = MagicMock()
        client.agent_api_participants = MagicMock()
        client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response(
                ["agent-wd", "agent-cond", "agent-p0"],
            ),
        )
        return client

    @pytest.mark.asyncio
    async def test_patrol_healthy_agents(self, watchdog_config, mock_rest_client):
        """No nudges sent when all agents are active."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=2),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-cond",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reviewer_verdict_makes_reviewer_idle(
        self, watchdog_config, mock_rest_client,
    ):
        """A reviewer who already reported a verdict must not be nudged.

        Regression: after a code reviewer sent "Review PASSED", the swarm was
        still globally active because the PR had not been merged yet. The
        watchdog treated the correctly-idle reviewer as stale and sent a noisy
        status check.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        dispatch = _make_message("agent-coder", minutes_ago=8)
        dispatch.content = (
            "@Reviewer-Claude-0 Review requested for PR #2: "
            "https://github.com/o/r/pull/2"
        )
        verdict = _make_message("agent-reviewer", minutes_ago=6)
        verdict.content = "@Conductor Review PASSED for PR #2 (risk: low)."

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([dispatch, verdict]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-coder", "Coder-Codex-0"),
                ("agent-reviewer", "Reviewer-Claude-0"),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-cond",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_mergemaster_merge_result_makes_mergemaster_idle(
        self, watchdog_config, mock_rest_client,
    ):
        """A completed merge report means Mergemaster is idle until re-mentioned."""
        from codeband.agents.watchdog import WatchdogDaemon

        request = _make_message("agent-cond", minutes_ago=20)
        request.content = "@Mergemaster — please merge PR #2."
        result = _make_message("agent-mm", minutes_ago=16)
        result.content = (
            "@Conductor Merged PR #2 into master. Tests: 1643 passed, "
            "same 8 pre-existing failures."
        )

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([request, result]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-mm", "Mergemaster"),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-cond",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_patrol_detects_stale_agent(self, watchdog_config, mock_rest_client):
        """Stale agent (no activity > threshold) gets nudged."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),  # Stale!
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        call_kwargs = (
            mock_rest_client.agent_api_messages.create_agent_chat_message.call_args
        )
        assert "agent-p0" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_patrol_escalates_after_nudge(self, watchdog_config, mock_rest_client):
        """Agent past 2x threshold with prior nudge escalates exactly once."""
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=15),  # Very stale (>2x threshold)
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        # Pre-set state: already nudged
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(minutes=15),
            nudged_at=datetime.now(UTC) - timedelta(minutes=6),
            nudge_count=1,
        )

        await daemon._patrol()

        # Exactly one escalation message sent, mentioning the conductor
        assert mock_rest_client.agent_api_messages.create_agent_chat_message.call_count == 1
        assert daemon._state["agent-p0"].escalated is True

        # Second patrol with the same conditions must not escalate again
        await daemon._patrol()
        assert mock_rest_client.agent_api_messages.create_agent_chat_message.call_count == 1

    @pytest.mark.asyncio
    async def test_nudge_grace_window_blocks_early_escalation(
        self, watchdog_config, mock_rest_client,
    ):
        """Agent nudged within grace window must not escalate on next patrol."""
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=15),  # Very stale (>2x threshold)
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        # Nudged 10 seconds ago — inside the 60s default grace window.
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(minutes=15),
            nudged_at=datetime.now(UTC) - timedelta(seconds=10),
            nudge_count=1,
        )

        await daemon._patrol()

        # No escalation — grace has not elapsed.
        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        assert daemon._state["agent-p0"].escalated is False

    @pytest.mark.asyncio
    async def test_escalation_fires_after_grace_window(
        self, watchdog_config, mock_rest_client,
    ):
        """Agent still stale past the grace window escalates."""
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=15),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        # Nudged 2 minutes ago — past the 60s default grace window.
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(minutes=15),
            nudged_at=datetime.now(UTC) - timedelta(seconds=120),
            nudge_count=1,
        )

        await daemon._patrol()

        assert mock_rest_client.agent_api_messages.create_agent_chat_message.call_count == 1
        assert daemon._state["agent-p0"].escalated is True

    @pytest.mark.asyncio
    async def test_patrol_handles_api_errors(self, watchdog_config, mock_rest_client):
        """Patrol continues even if REST API throws."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            side_effect=Exception("Network error")
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

    @pytest.mark.asyncio
    async def test_patrol_handles_stale_room_404_cleanly(
        self, watchdog_config, mock_rest_client, caplog,
    ):
        """A 404 on a listed-but-dead room logs a friendly warning, not a stack trace.

        Regression: Band.ai's list_agent_chats returns rooms the agent still
        holds membership in even after the room has been deleted server-side.
        Inspecting such a room 404s; the bare ``Exception`` branch used to emit
        a full traceback at ERROR level, which looked like a genuine failure.
        """
        import logging

        from thenvoi_rest.errors.not_found_error import NotFoundError
        from thenvoi_rest.types.error import Error, ErrorError

        from codeband.agents.watchdog import WatchdogDaemon

        live_room = _make_chat_room("live-room")
        dead_room = _make_chat_room("dead-room")
        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([dead_room, live_room]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
            ]),
        )
        not_found = NotFoundError(
            body=Error(error=ErrorError(
                code="not_found", message="Resource not found", request_id="r",
            )),
            headers={},
        )
        # Dead room 404s on participant list; live room returns normally. The
        # mock is stateful — first call raises, subsequent calls return default.
        default_parts = mock_rest_client.agent_api_participants.list_agent_chat_participants
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            side_effect=[not_found, default_parts.return_value],
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        with caplog.at_level(logging.WARNING, logger="codeband.agents.watchdog"):
            await daemon._patrol()

        # No ERROR-level traceback record for the dead room.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Expected no ERROR records, got: {[r.getMessage() for r in error_records]}"
        )
        # A single WARNING mentioning the dead room and pointing at `cb reset`.
        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "dead-room" in r.getMessage()
        ]
        assert len(warn_records) == 1
        assert "cb reset" in warn_records[0].getMessage()

    @pytest.mark.asyncio
    async def test_stale_room_warning_dedupes_per_room(
        self, watchdog_config, mock_rest_client, caplog,
    ):
        """The stale-room warning fires once per room, not on every patrol cycle."""
        import logging

        from thenvoi_rest.errors.not_found_error import NotFoundError
        from thenvoi_rest.types.error import Error, ErrorError

        from codeband.agents.watchdog import WatchdogDaemon

        dead_a = _make_chat_room("dead-a")
        dead_b = _make_chat_room("dead-b")
        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([dead_a, dead_b]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([]),
        )
        not_found = NotFoundError(
            body=Error(error=ErrorError(
                code="not_found", message="Resource not found", request_id="r",
            )),
            headers={},
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            side_effect=not_found,
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        with caplog.at_level(logging.WARNING, logger="codeband.agents.watchdog"):
            await daemon._patrol()
            await daemon._patrol()  # second cycle — should NOT re-warn for the same rooms.

        warn_a = [r for r in caplog.records if "dead-a" in r.getMessage()]
        warn_b = [r for r in caplog.records if "dead-b" in r.getMessage()]
        assert len(warn_a) == 1, f"Expected 1 warning for dead-a, got {len(warn_a)}"
        assert len(warn_b) == 1, f"Expected 1 warning for dead-b, got {len(warn_b)}"

    @pytest.mark.asyncio
    async def test_patrol_handles_rate_limit_without_traceback(
        self, watchdog_config, mock_rest_client, caplog,
    ):
        """A 429 from Band.ai logs a one-line warning, not a stack trace.

        Regression: Cloudflare's rate limiter returns 429 with an empty body,
        which the SDK wraps as ``ApiError``. The bare ``except Exception``
        branch used to dump a multi-screen traceback per room per cycle.
        """
        import logging

        from thenvoi_rest.core.api_error import ApiError

        from codeband.agents.watchdog import WatchdogDaemon

        live_room = _make_chat_room("live-room")
        rl_room = _make_chat_room("rl-room")
        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([rl_room, live_room]),
        )
        rate_limited = ApiError(status_code=429, headers={}, body="")
        # First call (rl-room) hits 429, second (live-room) succeeds.
        default_msgs = mock_rest_client.agent_api_messages.list_agent_messages
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            side_effect=[
                rate_limited,
                _make_messages_response([_make_message("agent-cond", minutes_ago=1)]),
            ],
        )
        del default_msgs
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        with caplog.at_level(logging.WARNING, logger="codeband.agents.watchdog"):
            await daemon._patrol()

        # No ERROR-level traceback record.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Expected no ERROR records, got: {[r.getMessage() for r in error_records]}"
        )
        # A WARNING that mentions the rate-limited room and the 429 status.
        rl_warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "rl-room" in r.getMessage()
            and "429" in r.getMessage()
        ]
        assert len(rl_warns) == 1, (
            f"Expected one 429 warning for rl-room, got {len(rl_warns)}"
        )
        assert "rate-limited" in rl_warns[0].getMessage().lower()

    @pytest.mark.asyncio
    async def test_patrol_handles_list_chats_rate_limit(
        self, watchdog_config, mock_rest_client, caplog,
    ):
        """A 429 on the top-level list_chats call aborts the cycle with a warning."""
        import logging

        from thenvoi_rest.core.api_error import ApiError

        from codeband.agents.watchdog import WatchdogDaemon

        rate_limited = ApiError(status_code=429, headers={}, body="")
        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            side_effect=rate_limited,
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        with caplog.at_level(logging.WARNING, logger="codeband.agents.watchdog"):
            await daemon._patrol()

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Expected no ERROR records, got: {[r.getMessage() for r in error_records]}"
        )
        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "429" in r.getMessage()
        ]
        assert len(warn_records) == 1
        assert "rate-limited" in warn_records[0].getMessage().lower()

    @pytest.mark.asyncio
    async def test_skips_senders_not_in_room(self, watchdog_config, mock_rest_client):
        """Stale senders who are no longer room participants must not be nudged.

        Regression: the server rejects @-mentions of non-participants with
        ``mentioned_participant_not_in_room`` (HTTP 422), which previously
        aborted the whole patrol cycle.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-ghost", minutes_ago=15),  # removed from room
            ]),
        )
        # agent-ghost left the room — not in the participants list.
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response(["agent-wd", "agent-cond"]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_patrol_ignores_human_user_participants(
        self, watchdog_config, mock_rest_client,
    ):
        """Human users (``type="User"``) must never be nudged or escalated.

        Regression: the human who opens the session is a room participant and
        typically sends the kickoff message, then goes quiet. Without a
        participant-type filter the watchdog would eventually treat them as a
        stale agent and @-mention them with a status check / escalation.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("user-ofer", minutes_ago=15),  # human, very stale
            ]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog", "Agent"),
                ("agent-cond", "Conductor", "Agent"),
                ("user-ofer", "Ofer Mendelevitch", "User"),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        # Run twice — if the human were being tracked, the second patrol would
        # escalate past the nudge grace window.
        await daemon._patrol()
        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        assert "user-ofer" not in daemon._state

    @pytest.mark.asyncio
    async def test_post_response_cooldown_suppresses_renudge(
        self, watchdog_config, mock_rest_client,
    ):
        """After an agent responds to a nudge, don't re-nudge within the cooldown.

        Regression: without this, a legitimately-idle agent (e.g. a Planner
        waiting on human approval) would be nudged → reply → state cleared →
        stale again after 5min → nudged again, forever. In a real session this
        produced 10+ hours of pointless 6-minute polling at Plan-Reviewer-Codex.
        """
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        # Patrol 1: agent-p0 was nudged 10s ago and has just responded (last
        # activity 5s ago — well under the 300s threshold). The healthy branch
        # must now record `confirmed_alive_at` instead of dropping the state.
        fresh_seen = datetime.now(UTC) - timedelta(seconds=5)
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=fresh_seen,
            nudged_at=datetime.now(UTC) - timedelta(seconds=10),
            nudge_count=1,
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=5 / 60),  # 5s ago
            ]),
        )
        await daemon._patrol()

        state = daemon._state["agent-p0"]
        assert state.confirmed_alive_at is not None
        assert state.nudged_at is None
        assert state.nudge_count == 0
        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

        # Patrol 2: 10 minutes later agent-p0 has gone silent again (past the
        # 300s threshold), but the 1800s cooldown is still active. No nudge.
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),
            ]),
        )
        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        # confirmed_alive_at is still honoured; state not reset.
        assert daemon._state["agent-p0"].confirmed_alive_at is not None

    @pytest.mark.asyncio
    async def test_cooldown_survives_intermediate_healthy_patrol(
        self, watchdog_config, mock_rest_client,
    ):
        """Cooldown must persist across healthy patrols where ``nudged_at`` is None.

        Regression: the existing post-response cooldown test goes directly
        from "healthy patrol that sets confirmed_alive_at" to "stale patrol
        that should be suppressed" — but in production at least one
        intermediate healthy patrol fires while the agent is still recently
        active. That intermediate patrol observed ``state.nudged_at is None``
        and dropped the entry, losing the cooldown. A subsequent stale patrol
        then nudged again. This test exercises the full three-patrol
        sequence that reproduces the 6-minute re-nudge cycle.
        """
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        # Patrol 1: just-replied to a recent nudge. Healthy branch records
        # confirmed_alive_at and clears nudged_at.
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(seconds=5),
            nudged_at=datetime.now(UTC) - timedelta(seconds=10),
            nudge_count=1,
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=5 / 60),  # 5s ago
            ]),
        )
        await daemon._patrol()

        assert "agent-p0" in daemon._state
        confirmed_after_p1 = daemon._state["agent-p0"].confirmed_alive_at
        assert confirmed_after_p1 is not None
        assert daemon._state["agent-p0"].nudged_at is None

        # Patrol 2: still healthy (last activity 30s ago, well under 300s
        # threshold) but nudged_at is None from Patrol 1. State must be
        # preserved so the cooldown survives — it is the loss of state at
        # this step that produced the 6-minute re-nudge cycle in production.
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=30 / 60),  # 30s ago
            ]),
        )
        await daemon._patrol()

        assert "agent-p0" in daemon._state, (
            "state was dropped during a healthy patrol with no nudged_at — "
            "cooldown information lost"
        )
        assert daemon._state["agent-p0"].confirmed_alive_at == confirmed_after_p1
        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

        # Patrol 3: agent has gone stale again (10min) but the 1800s
        # cooldown is still active. No nudge.
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),
            ]),
        )
        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        assert daemon._state["agent-p0"].confirmed_alive_at is not None

    @pytest.mark.asyncio
    async def test_cooldown_elapsed_allows_new_nudge(
        self, watchdog_config, mock_rest_client,
    ):
        """Once the cooldown elapses, a still-stale agent may be nudged again."""
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        # Agent-p0 is stale (10min past last_seen) and the confirmation sits
        # outside the 1800s (30min) cooldown window — so a fresh nudge is due.
        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(minutes=10),
            confirmed_alive_at=datetime.now(UTC) - timedelta(minutes=40),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        # confirmed_alive_at reset so the new nudge→escalate state machine runs cleanly.
        assert daemon._state["agent-p0"].confirmed_alive_at is None
        assert daemon._state["agent-p0"].nudged_at is not None

    @pytest.mark.asyncio
    async def test_one_bad_nudge_does_not_abort_patrol(
        self, watchdog_config, mock_rest_client,
    ):
        """A send failure for one agent must not prevent nudging other stale agents."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),
                _make_message("agent-p1", minutes_ago=10),
            ]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response(
                ["agent-wd", "agent-cond", "agent-p0", "agent-p1"],
            ),
        )

        calls: list[str] = []

        async def flaky_create(**kwargs):
            mentions = kwargs["message"].mentions
            target = mentions[0].id if mentions else ""
            calls.append(target)
            if target == "agent-p0":
                raise RuntimeError("simulated send failure")

        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock(
            side_effect=flaky_create,
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        # Both agents were attempted even though p0 failed.
        assert "agent-p0" in calls
        assert "agent-p1" in calls

    @pytest.mark.asyncio
    async def test_nudge_uses_participant_display_name(
        self, watchdog_config, mock_rest_client,
    ):
        """Nudge text must reference the stale agent's display name, not its UUID.

        Regression: previously ``f"Status check for @{agent_id}"`` leaked the
        raw Band.ai agent UUID into the chat body.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),  # stale
            ]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-p0", "Coder-Claude-0"),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        sent = mock_rest_client.agent_api_messages.create_agent_chat_message.call_args
        content = sent.kwargs["message"].content
        assert "Coder-Claude-0" in content, (
            f"display name missing from nudge body: {content!r}"
        )
        assert "agent-p0" not in content, (
            f"raw UUID leaked into nudge body: {content!r}"
        )

    @pytest.mark.asyncio
    async def test_nudge_falls_back_to_id_when_name_missing(
        self, watchdog_config, mock_rest_client,
    ):
        """Stale sender not in participant-name map falls back to the raw id.

        Defensive: participant listing and message listing are two separate
        API calls, and an agent could (in theory) be removed between them. The
        participant filter already drops ghosts, but if the race window
        produces a name-less participant, we must still format a valid send.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),
            ]),
        )
        # Participant entry exists (so p0 isn't filtered out) but carries
        # no display name — simulating the free-tier race.
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-p0", None),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        content = (
            mock_rest_client.agent_api_messages.create_agent_chat_message
            .call_args.kwargs["message"].content
        )
        assert "agent-p0" in content  # fallback — id acts as name

    @pytest.mark.asyncio
    async def test_escalation_does_not_mention_conductor(
        self, watchdog_config, mock_rest_client,
    ):
        """Escalation must not @-mention the conductor.

        Regression: the Watchdog runs under Conductor credentials, so mentioning
        ``self._conductor_id`` triggered HTTP 422 ``cannot_mention_self`` on
        every escalation. Today the escalation mentions the stuck agent as a
        final loud ping; the conductor receives the message through its own
        inbound chat event stream because it's a room participant.
        """
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=15),
            ]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-p0", "Coder-Claude-0"),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(minutes=15),
            nudged_at=datetime.now(UTC) - timedelta(seconds=120),
            nudge_count=1,
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        msg = (
            mock_rest_client.agent_api_messages.create_agent_chat_message
            .call_args.kwargs["message"]
        )
        mention_ids = {m.id for m in msg.mentions}
        assert "agent-cond" not in mention_ids, (
            "escalation must not self-mention the conductor — Band.ai rejects "
            "with cannot_mention_self when sender == mention target"
        )
        assert "agent-p0" in mention_ids
        assert "Coder-Claude-0" in msg.content

    @pytest.mark.asyncio
    async def test_escalation_flag_set_even_on_send_failure(
        self, watchdog_config, mock_rest_client,
    ):
        """``state.escalated`` must flip to True once an escalation is attempted.

        Regression: previously the flag was set only after a successful send,
        so any 422 left escalation unflipped and the Watchdog retried the
        same escalation on every patrol interval forever.
        """
        from codeband.agents.watchdog import AgentHealthState, WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=15),
            ]),
        )
        mock_rest_client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-p0", "Coder-Claude-0"),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock(
            side_effect=RuntimeError("simulated server rejection"),
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )
        daemon._state["agent-p0"] = AgentHealthState(
            last_seen=datetime.now(UTC) - timedelta(minutes=15),
            nudged_at=datetime.now(UTC) - timedelta(seconds=120),
            nudge_count=1,
        )

        await daemon._patrol()
        assert daemon._state["agent-p0"].escalated is True

        # Second patrol under the same conditions must not attempt another send.
        call_count_after_first = (
            mock_rest_client.agent_api_messages.create_agent_chat_message.call_count
        )
        await daemon._patrol()
        assert (
            mock_rest_client.agent_api_messages.create_agent_chat_message.call_count
            == call_count_after_first
        ), "escalation retried after failure — escalate-once invariant broken"

    @pytest.mark.asyncio
    async def test_skips_own_messages(self, watchdog_config, mock_rest_client):
        """Watchdog does not flag itself as stale."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-wd", minutes_ago=20),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()


def _make_memory_record(content: str, inserted_at: datetime) -> MagicMock:
    """Create a mock memory record with the fields the watchdog reads."""
    rec = MagicMock()
    rec.content = content
    rec.inserted_at = inserted_at
    rec.updated_at = inserted_at
    return rec


def _make_memory_list_response(records: list) -> MagicMock:
    """Create a mock list-memories response — duck-types the SDK shape."""
    resp = MagicMock()
    resp.data = records
    return resp


class TestSwarmStatusGate:
    """Watchdog reads the Conductor's ``swarm status …`` memory envelope and
    suppresses patrols when the swarm is idle between user tasks."""

    @pytest.fixture
    def watchdog_config(self) -> WatchdogConfig:
        return WatchdogConfig(
            check_interval_seconds=120,
            stale_threshold_seconds=300,
            swarm_idle_grace_seconds=1800,
        )

    @pytest.fixture
    def mock_rest_client(self):
        client = MagicMock()
        client.agent_api_chats = MagicMock()
        client.agent_api_messages = MagicMock()
        client.agent_api_participants = MagicMock()
        client.agent_api_memories = MagicMock()
        client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-p0", minutes_ago=10),  # stale
            ]),
        )
        client.agent_api_messages.create_agent_chat_message = AsyncMock()
        client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response(
                ["agent-wd", "agent-cond", "agent-p0"],
            ),
        )
        return client

    @pytest.mark.asyncio
    async def test_no_nudge_when_swarm_status_is_complete(
        self, watchdog_config, mock_rest_client,
    ):
        """Stale agent is not nudged while the swarm-idle grace window holds."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_memories.list_agent_memories = AsyncMock(
            return_value=_make_memory_list_response([
                _make_memory_record(
                    "swarm status complete task fix-login",
                    datetime.now(UTC) - timedelta(seconds=60),
                ),
            ]),
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        # Idle-skip log gate flipped on so subsequent skip cycles stay quiet.
        assert daemon._idle_skip_logged is True

    @pytest.mark.asyncio
    async def test_nudge_resumes_when_swarm_status_returns_to_active(
        self, watchdog_config, mock_rest_client,
    ):
        """An ``active`` envelope (newer than any ``complete``) re-enables nudging."""
        from codeband.agents.watchdog import WatchdogDaemon

        now = datetime.now(UTC)
        mock_rest_client.agent_api_memories.list_agent_memories = AsyncMock(
            return_value=_make_memory_list_response([
                _make_memory_record(
                    "swarm status complete task fix-login",
                    now - timedelta(minutes=20),
                ),
                _make_memory_record(
                    "swarm status active task add-export",
                    now - timedelta(seconds=30),
                ),
            ]),
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        assert daemon._idle_skip_logged is False

    @pytest.mark.asyncio
    async def test_no_nudge_when_waiting_on_human_approval(
        self, watchdog_config, mock_rest_client,
    ):
        """Agents are correctly idle while the Conductor waits for merge approval."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_memories.list_agent_memories = AsyncMock(
            return_value=_make_memory_list_response([
                _make_memory_record(
                    "swarm status waiting_human_approval task add-redact pr 1",
                    datetime.now(UTC) - timedelta(seconds=60),
                ),
            ]),
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        assert daemon._idle_skip_logged is True

    @pytest.mark.asyncio
    async def test_no_swarm_envelope_falls_back_to_time_based(
        self, watchdog_config, mock_rest_client,
    ):
        """When memory has no swarm-status record, the watchdog behaves as before."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_memories.list_agent_memories = AsyncMock(
            return_value=_make_memory_list_response([]),
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_envelope_past_grace_resumes_nudging(
        self, watchdog_config, mock_rest_client,
    ):
        """A ``complete`` envelope older than the grace window no longer suppresses."""
        from codeband.agents.watchdog import WatchdogDaemon

        # 2 hours > 1800s grace.
        mock_rest_client.agent_api_memories.list_agent_memories = AsyncMock(
            return_value=_make_memory_list_response([
                _make_memory_record(
                    "swarm status complete task fix-login",
                    datetime.now(UTC) - timedelta(hours=2),
                ),
            ]),
        )

        daemon = WatchdogDaemon(
            config=watchdog_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()


class TestRoleAwareThresholds:
    """Per-role stale thresholds — Coders and Mergemaster run long."""

    @pytest.fixture
    def role_config(self) -> WatchdogConfig:
        # default 300s, coder override 900s (matches production default).
        return WatchdogConfig(
            check_interval_seconds=120,
            stale_threshold_seconds=300,
            role_stale_thresholds={"coder": 900, "mergemaster": 900},
        )

    @pytest.fixture
    def mock_rest_client(self):
        client = MagicMock()
        client.agent_api_chats = MagicMock()
        client.agent_api_messages = MagicMock()
        client.agent_api_participants = MagicMock()
        client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-coder", "Coder-Claude-0"),
                ("agent-planner", "Planner-Claude-0"),
            ]),
        )
        return client

    @pytest.mark.asyncio
    async def test_coder_silent_under_role_threshold_not_nudged(
        self, role_config, mock_rest_client,
    ):
        """A coder silent for 7 min (under 15 min role threshold) is not nudged.

        This is the primary regression: under the old uniform 300s rule the
        coder would be nudged at ~5 min, breaking the "chat for notifications
        only" discipline the prompts enforce.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-coder", minutes_ago=7),  # 420s — past 300s default
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={
                "agent-cond": "conductor",
                "agent-coder": "coder",
                "agent-planner": "planner",
            },
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_coder_silent_past_role_threshold_gets_nudged(
        self, role_config, mock_rest_client,
    ):
        """A coder silent for 20 min (past the 15 min role threshold) is nudged."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-coder", minutes_ago=20),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={
                "agent-cond": "conductor",
                "agent-coder": "coder",
            },
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        sent = (
            mock_rest_client.agent_api_messages.create_agent_chat_message
            .call_args.kwargs["message"]
        )
        assert {m.id for m in sent.mentions} == {"agent-coder"}

    @pytest.mark.asyncio
    async def test_coordinator_uses_default_threshold(
        self, role_config, mock_rest_client,
    ):
        """A planner (no role override) silent for 7 min is nudged — default 300s."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-planner", minutes_ago=7),  # past 300s
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={
                "agent-cond": "conductor",
                "agent-planner": "planner",
            },
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()
        sent = (
            mock_rest_client.agent_api_messages.create_agent_chat_message
            .call_args.kwargs["message"]
        )
        assert {m.id for m in sent.mentions} == {"agent-planner"}

    @pytest.mark.asyncio
    async def test_mentioned_but_silent_coder_gets_nudged(
        self, role_config, mock_rest_client,
    ):
        """A Coder @-mentioned by the Conductor but never replying gets nudged.

        Regression for the redact() session: Coder-Codex-0 was dispatched a
        task and crashed before sending any reply. Because the patrol used to
        iterate only agents that had sent messages, the silent coder was
        invisible — the watchdog never nudged it. The fix is to start the
        staleness clock from `max(last_message_ts, last_mentioned_ts)` and
        iterate over every participant, not just senders.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        now = datetime.now(UTC)
        # Conductor dispatched the coder 20 minutes ago (past 15min role
        # threshold). The coder has zero outbound messages.
        dispatch_msg = MagicMock()
        dispatch_msg.sender_id = "agent-cond"
        dispatch_msg.inserted_at = now - timedelta(minutes=20)
        dispatch_msg.content = (
            "@Coder-Claude-0 — please implement st-1 on branch "
            "codeband/coder-claude_sdk-0/add-redact-helper."
        )
        dispatch_msg.mentions = []

        recent_unrelated = _make_message("agent-cond", minutes_ago=1)
        recent_unrelated.content = "status update"
        recent_unrelated.mentions = []

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                recent_unrelated,
                dispatch_msg,
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={
                "agent-cond": "conductor",
                "agent-coder": "coder",
            },
        )

        await daemon._patrol()

        # Watchdog must @mention the silent coder, not the conductor.
        send_mock = mock_rest_client.agent_api_messages.create_agent_chat_message
        send_mock.assert_called_once()
        sent = send_mock.call_args.kwargs["message"]
        assert {m.id for m in sent.mentions} == {"agent-coder"}

    @pytest.mark.asyncio
    async def test_never_mentioned_never_spoken_agent_not_nudged(
        self, role_config, mock_rest_client,
    ):
        """A pool member never given work and never speaking is left alone.

        Counterpart to the silent-coder test above: the new patrol logic must
        not start the staleness clock for a fresh participant who has no
        activity *and* no inbound mention. Otherwise every dormant pool
        member would be nudged on the first patrol after session start.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        # Only the conductor has spoken. The coder is in the room but
        # neither spoke nor was @-mentioned.
        recent_msg = _make_message("agent-cond", minutes_ago=1)
        recent_msg.content = "status update"
        recent_msg.mentions = []

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([recent_msg]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={
                "agent-cond": "conductor",
                "agent-coder": "coder",
            },
        )

        await daemon._patrol()

        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_not_called()
        assert "agent-coder" not in daemon._state

    def test_mention_boundary_rejects_embedded_match(self):
        """`@Coder-Claude-0` inside a longer token must NOT count as a mention.

        Catches both substring matches (`@Coder-Claude-01`, `@Coder-Claude-0a`)
        and embedded-prefix matches (`email@Coder-Claude-0`). Word boundaries
        on both sides of the name are required.
        """
        from codeband.agents.watchdog import _mentioned_participant_ids

        names = {"agent-coder": "Coder-Claude-0"}

        def _msg(text: str):
            m = MagicMock()
            m.content = text
            m.mentions = []
            return m

        # Genuine mentions match.
        assert _mentioned_participant_ids(
            _msg("@Coder-Claude-0 please implement"), names,
        ) == {"agent-coder"}
        assert _mentioned_participant_ids(
            _msg("hello @Coder-Claude-0!"), names,
        ) == {"agent-coder"}

        # Right-boundary violations do NOT match.
        assert _mentioned_participant_ids(
            _msg("@Coder-Claude-01 different agent"), names,
        ) == set()
        assert _mentioned_participant_ids(
            _msg("@Coder-Claude-0a typo"), names,
        ) == set()

        # Left-boundary violations do NOT match (the regression this guards).
        assert _mentioned_participant_ids(
            _msg("contact: email@Coder-Claude-0 (not a mention)"), names,
        ) == set()
        assert _mentioned_participant_ids(
            _msg("foo-@Coder-Claude-0 still embedded"), names,
        ) == set()

    @pytest.mark.asyncio
    async def test_unknown_agent_id_uses_default_threshold(
        self, role_config, mock_rest_client,
    ):
        """Agent_id absent from role map falls back to the default threshold."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_rest_client.agent_api_chats.list_agent_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        mock_rest_client.agent_api_messages.list_agent_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-coder", minutes_ago=7),
            ]),
        )
        mock_rest_client.agent_api_messages.create_agent_chat_message = AsyncMock()

        # Empty role map — coder identity is unknown, default threshold applies.
        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_rest_client,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={},
        )

        await daemon._patrol()

        # Coder unknown → default 300s → 7min silence triggers nudge.
        mock_rest_client.agent_api_messages.create_agent_chat_message.assert_called_once()


class TestHumanApiLiveness:
    """Enterprise-tier path: liveness signal includes thoughts and tool_calls."""

    @pytest.fixture
    def role_config(self) -> WatchdogConfig:
        return WatchdogConfig(
            check_interval_seconds=120,
            stale_threshold_seconds=300,
            role_stale_thresholds={"coder": 900, "mergemaster": 900},
        )

    @pytest.fixture
    def mock_agent_rest(self):
        """Agent-API REST client (writes + participant listing)."""
        client = MagicMock()
        client.agent_api_chats = MagicMock()
        client.agent_api_messages = MagicMock()
        client.agent_api_participants = MagicMock()
        client.agent_api_participants.list_agent_chat_participants = AsyncMock(
            return_value=_make_participants_response([
                ("agent-wd", "Watchdog"),
                ("agent-cond", "Conductor"),
                ("agent-coder", "Coder-Claude-0"),
            ]),
        )
        client.agent_api_messages.create_agent_chat_message = AsyncMock()
        return client

    @pytest.fixture
    def mock_human_rest(self):
        """Human-API REST client (enterprise reads — captures all message types)."""
        client = MagicMock()
        client.human_api_chats = MagicMock()
        client.human_api_messages = MagicMock()
        client.human_api_chats.list_my_chats = AsyncMock(
            return_value=_make_chats_response([_make_chat_room("room-1")]),
        )
        return client

    @pytest.mark.asyncio
    async def test_coder_emitting_thoughts_is_not_stale(
        self, role_config, mock_agent_rest, mock_human_rest,
    ):
        """A coder whose latest signal is a `thought` within 10min is healthy.

        Rationale: on agent-tier we'd see only chat text (coder silent), but
        the human API surfaces thoughts and tool_calls — so an actively
        thinking coder is visibly alive even when not chatting. This is the
        whole point of the enterprise-tier upgrade.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        # All signals come via the human API; the thought is recent.
        mock_human_rest.human_api_messages.list_my_chat_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                # Coder's last *chat text* was 20 min ago (past the 15min role
                # threshold), but a thought 10 min ago keeps them alive.
                _make_message("agent-coder", minutes_ago=10),
            ]),
        )

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_agent_rest,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={
                "agent-cond": "conductor",
                "agent-coder": "coder",
            },
            human_rest_client=mock_human_rest,
        )

        await daemon._patrol()

        # No nudge — coder's thought 10min ago < 15min role threshold.
        # (the companion `test_human_api_read_path_is_preferred` covers that
        # the agent-API read surface is untouched when human_rest is set.)
        mock_agent_rest.agent_api_messages.create_agent_chat_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_human_api_read_path_is_preferred(
        self, role_config, mock_agent_rest, mock_human_rest,
    ):
        """When human_rest_client is set, reads go through the human API."""
        from codeband.agents.watchdog import WatchdogDaemon

        mock_human_rest.human_api_messages.list_my_chat_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-coder", minutes_ago=2),
            ]),
        )
        # Agent-API read mock is present but should never be awaited.
        mock_agent_rest.agent_api_messages.list_agent_messages = AsyncMock()
        mock_agent_rest.agent_api_chats.list_agent_chats = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_agent_rest,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={"agent-cond": "conductor", "agent-coder": "coder"},
            human_rest_client=mock_human_rest,
        )

        await daemon._patrol()

        mock_human_rest.human_api_chats.list_my_chats.assert_awaited_once()
        mock_human_rest.human_api_messages.list_my_chat_messages.assert_awaited_once()
        mock_agent_rest.agent_api_messages.list_agent_messages.assert_not_awaited()
        mock_agent_rest.agent_api_chats.list_agent_chats.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writes_always_use_agent_client(
        self, role_config, mock_agent_rest, mock_human_rest,
    ):
        """Nudges/escalations must be sent via the agent client regardless of tier.

        This preserves the existing "Watchdog speaks as Conductor" semantics.
        The human client never writes — it's read-only for liveness polling.
        """
        from codeband.agents.watchdog import WatchdogDaemon

        mock_human_rest.human_api_messages.list_my_chat_messages = AsyncMock(
            return_value=_make_messages_response([
                _make_message("agent-cond", minutes_ago=1),
                _make_message("agent-coder", minutes_ago=20),  # stale past 15min
            ]),
        )
        # Ensure the human client has no write surface that could be awaited.
        mock_human_rest.human_api_messages.send_my_chat_message = AsyncMock()

        daemon = WatchdogDaemon(
            config=role_config,
            rest_client=mock_agent_rest,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            agent_id_to_role={"agent-cond": "conductor", "agent-coder": "coder"},
            human_rest_client=mock_human_rest,
        )

        await daemon._patrol()

        mock_agent_rest.agent_api_messages.create_agent_chat_message.assert_called_once()
        mock_human_rest.human_api_messages.send_my_chat_message.assert_not_awaited()
