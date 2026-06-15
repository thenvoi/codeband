"""Tests for df#5 recovery-observability markers.

Covers three items:
- AGENT_PIN_HEALED enriched with branch + pin_class at both heal sites
- AGENT_PIN_DEFER event at both #86 discriminator defer-decision points
- AGENT_RECONNECTED fires on reconnect cycles (attempt > 1), not on initial start
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import WatchdogConfig


# ─── shared helpers ──────────────────────────────────────────────────────────

def _make_message(message_id: str, inserted_at: datetime) -> MagicMock:
    msg = MagicMock()
    msg.id = message_id
    msg.inserted_at = inserted_at
    return msg


def _make_messages_response(messages: list) -> MagicMock:
    resp = MagicMock()
    resp.data = messages
    return resp


def _make_activity() -> MagicMock:
    activity = MagicMock()
    activity.log = MagicMock()
    return activity


def _make_heal_daemon(
    config: WatchdogConfig,
    activity: MagicMock | None = None,
):
    """Minimal WatchdogDaemon for _attempt_pin_heal tests (no patrol plumbing)."""
    from codeband.agents.watchdog import WatchdogDaemon

    conductor = MagicMock()
    conductor.agent_api_messages = MagicMock()
    conductor.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=_make_messages_response([]),
    )
    return WatchdogDaemon(
        config=config,
        rest_client=conductor,
        agent_id="agent-cond",
        conductor_id="agent-cond",
        activity=activity,
    )


def _make_agent_client_for_pending_heal(*, verify_messages: list) -> MagicMock:
    """Agent REST client for the 2-step pending-bucket heal path.

    step-a (mark_processing) and step-b (mark_processed) succeed; the verify
    re-list returns ``verify_messages``.
    """
    client = MagicMock()
    client.agent_api_messages = MagicMock()
    client.agent_api_messages.mark_agent_message_processing = AsyncMock()
    client.agent_api_messages.mark_agent_message_processed = AsyncMock()
    client.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=_make_messages_response(verify_messages),
    )
    return client


def _make_agent_client_for_processing_heal() -> MagicMock:
    """Agent REST client for the 1-step processing-bucket heal path."""
    client = MagicMock()
    client.agent_api_messages = MagicMock()
    client.agent_api_messages.mark_agent_message_processed = AsyncMock()
    return client


@pytest.fixture
def heal_config() -> WatchdogConfig:
    return WatchdogConfig(
        transport_pin_threshold_seconds=600,
        transport_heal_max_attempts=3,
    )


# ─── Item 1: AGENT_PIN_HEALED enrichment ─────────────────────────────────────

class TestPinHealedEnrichment:
    """AGENT_PIN_HEALED carries branch + pin_class at both heal sites."""

    @pytest.mark.asyncio
    async def test_pending_2step_emits_correct_branch_and_pin_class(self, heal_config):
        """Pending-bucket success → branch='pending_2step', pin_class='pending'."""
        activity = _make_activity()
        # Verify re-list empty → cursor advanced → HEALED
        agent_client = _make_agent_client_for_pending_heal(verify_messages=[])
        daemon = _make_heal_daemon(heal_config, activity)

        await daemon._attempt_pin_heal(
            "agent-1", "room-1", "msg-1", agent_client, bucket="pending",
        )

        healed = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_HEALED"]
        assert len(healed) == 1, f"expected 1 AGENT_PIN_HEALED, got {healed}"
        assert healed[0].kwargs["branch"] == "pending_2step"
        assert healed[0].kwargs["pin_class"] == "pending"

    @pytest.mark.asyncio
    async def test_processing_1step_emits_correct_branch_and_pin_class(self, heal_config):
        """Processing-bucket success → branch='processing_1step', pin_class='processing'."""
        activity = _make_activity()
        agent_client = _make_agent_client_for_processing_heal()
        daemon = _make_heal_daemon(heal_config, activity)

        await daemon._attempt_pin_heal(
            "agent-1", "room-1", "msg-1", agent_client, bucket="processing",
        )

        healed = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_HEALED"]
        assert len(healed) == 1, f"expected 1 AGENT_PIN_HEALED, got {healed}"
        assert healed[0].kwargs["branch"] == "processing_1step"
        assert healed[0].kwargs["pin_class"] == "processing"

    @pytest.mark.asyncio
    async def test_enrichment_is_additive_no_field_removed(self, heal_config):
        """New fields are additive — existing positional args (event_type, agent, summary)
        are unchanged; branch and pin_class are new kwargs only."""
        activity = _make_activity()
        agent_client = _make_agent_client_for_pending_heal(verify_messages=[])
        daemon = _make_heal_daemon(heal_config, activity)

        await daemon._attempt_pin_heal(
            "agent-coder", "room-1", "msg-additive", agent_client, bucket="pending",
        )

        healed = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_HEALED"]
        assert len(healed) == 1
        call = healed[0]
        # Positional: (event_type, agent, summary)
        assert call.args[0] == "AGENT_PIN_HEALED"
        assert call.args[1] == "watchdog"
        assert "agent-coder" in call.args[2]
        assert "msg-additive" in call.args[2]
        # New kwargs present
        assert "branch" in call.kwargs
        assert "pin_class" in call.kwargs

    @pytest.mark.asyncio
    async def test_no_healed_event_when_cursor_did_not_advance(self, heal_config):
        """Verify re-list still shows same head → no AGENT_PIN_HEALED (not a success)."""
        now = datetime.now(UTC)
        same_head = _make_message("msg-stubborn", now - timedelta(seconds=900))
        activity = _make_activity()
        agent_client = _make_agent_client_for_pending_heal(verify_messages=[same_head])
        daemon = _make_heal_daemon(heal_config, activity)

        await daemon._attempt_pin_heal(
            "agent-1", "room-1", "msg-stubborn", agent_client, bucket="pending",
        )

        healed = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_HEALED"]
        assert len(healed) == 0


# ─── Item 2: AGENT_PIN_DEFER ──────────────────────────────────────────────────

_SUBTASK_ID = "sub-1"
_TASK_ID = "task-1"
_ROOM_ID = "room-1"


def _seed_store(tmp_path, *, state: str = "in_progress"):
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(_TASK_ID, "demo task", _ROOM_ID, owner_id="owner-1")
    store.ensure_subtask(_SUBTASK_ID, _TASK_ID, state=state)
    return store


def _make_disc_rest() -> MagicMock:
    rest = MagicMock()
    rest.agent_api_messages = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    return rest


def _make_disc_agent_rest(
    *,
    pending: list | None = None,
    processing: list | None = None,
    raise_on_processing: Exception | None = None,
) -> MagicMock:
    client = MagicMock()
    client.agent_api_messages = MagicMock()

    async def _list(*args, **kwargs):
        status = kwargs.get("status", "")
        if status == "processing":
            if raise_on_processing is not None:
                raise raise_on_processing
            return _make_messages_response(processing or [])
        if status == "pending":
            return _make_messages_response(pending or [])
        return _make_messages_response([])

    client.agent_api_messages.list_agent_messages = AsyncMock(side_effect=_list)
    return client


def _make_disc_daemon(store, rest, *, role_map, agent_rest_clients, activity=None):
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=WatchdogConfig(transport_pin_threshold_seconds=600),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        state_store=store,
        owner_id="owner-1",
        owner_handle="Owner",
        agent_id_to_role=role_map,
        agent_rest_clients=agent_rest_clients,
        activity=activity,
    )


class TestAgentPinDefer:
    """AGENT_PIN_DEFER fires at both #86 discriminator defer-decision points."""

    @pytest.mark.asyncio
    async def test_defer_fires_when_expected_actor_pending_pinned(self, tmp_path):
        """Coder's pending head is old → defer fires with correct subtask/role/agent."""
        store = _seed_store(tmp_path, state="in_progress")
        now = datetime.now(UTC)
        pinned_head = _make_message("msg-pinned", now - timedelta(seconds=900))
        coder_client = _make_disc_agent_rest(pending=[pinned_head])
        activity = _make_activity()

        daemon = _make_disc_daemon(
            store, _make_disc_rest(),
            role_map={"coder-1": "coder"},
            agent_rest_clients={"coder-1": coder_client},
            activity=activity,
        )
        sub = store.get_subtask(_SUBTASK_ID, _TASK_ID)
        result = await daemon._send_blocked_escalation(sub)

        assert result is False
        defer = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_DEFER"]
        assert len(defer) == 1, f"expected 1 AGENT_PIN_DEFER, got {defer}"
        kwargs = defer[0].kwargs
        assert kwargs["subtask_id"] == _SUBTASK_ID
        assert kwargs["expected_role"] == "coder"
        assert kwargs["pinned_agent"] == "coder-1"

    @pytest.mark.asyncio
    async def test_defer_fires_on_probe_exception(self, tmp_path):
        """Probe raises → fail-toward-defer path → AGENT_PIN_DEFER with correct fields."""
        from thenvoi_rest.core.api_error import ApiError

        store = _seed_store(tmp_path, state="review_pending")
        reviewer_client = _make_disc_agent_rest(
            raise_on_processing=ApiError(status_code=429, headers={}, body="rate limited"),
        )
        activity = _make_activity()

        daemon = _make_disc_daemon(
            store, _make_disc_rest(),
            role_map={"reviewer-1": "reviewer"},
            agent_rest_clients={"reviewer-1": reviewer_client},
            activity=activity,
        )
        sub = store.get_subtask(_SUBTASK_ID, _TASK_ID)
        result = await daemon._send_blocked_escalation(sub)

        assert result is False
        defer = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_DEFER"]
        assert len(defer) == 1, f"expected 1 AGENT_PIN_DEFER, got {defer}"
        kwargs = defer[0].kwargs
        assert kwargs["subtask_id"] == _SUBTASK_ID
        assert kwargs["expected_role"] == "reviewer"
        assert kwargs["pinned_agent"] == "reviewer-1"

    @pytest.mark.asyncio
    async def test_no_defer_event_when_block_proceeds(self, tmp_path):
        """Clear mailbox → block proceeds, no AGENT_PIN_DEFER emitted."""
        store = _seed_store(tmp_path, state="in_progress")
        coder_client = _make_disc_agent_rest(pending=[], processing=[])
        activity = _make_activity()

        daemon = _make_disc_daemon(
            store, _make_disc_rest(),
            role_map={"coder-1": "coder"},
            agent_rest_clients={"coder-1": coder_client},
            activity=activity,
        )
        sub = store.get_subtask(_SUBTASK_ID, _TASK_ID)
        await daemon._send_blocked_escalation(sub)

        defer = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_PIN_DEFER"]
        assert len(defer) == 0


# ─── Item 4: AGENT_RECONNECTED ────────────────────────────────────────────────

class TestAgentReconnected:
    """AGENT_RECONNECTED fires on first successful turn after reconnect, not on attempt."""

    @staticmethod
    def _agent(coro_factory) -> MagicMock:
        agent = MagicMock()
        agent.run = coro_factory
        agent.stop = AsyncMock(return_value=True)
        return agent

    @pytest.mark.asyncio
    async def test_not_emitted_on_initial_start(self, monkeypatch) -> None:
        """Attempt 1 (initial start) must NOT emit AGENT_RECONNECTED."""
        from codeband.orchestration.runner import _run_agent_forever

        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_BASE_DELAY_SECONDS", 0.0,
        )
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_MAX_DELAY_SECONDS", 0.0,
        )

        started = asyncio.Event()

        async def run_once():
            started.set()
            await asyncio.sleep(60)

        activity = _make_activity()
        task = asyncio.create_task(
            _run_agent_forever(
                lambda _ctx=None: self._agent(run_once),
                "test-agent", activity,
            ),
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        reconnected = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_RECONNECTED"]
        assert len(reconnected) == 0, (
            f"AGENT_RECONNECTED must not fire on initial start, got {reconnected}"
        )

    @pytest.mark.asyncio
    async def test_emitted_once_per_reconnect_cycle(self, monkeypatch) -> None:
        """Each reconnect cycle emits AGENT_RECONNECTED after its first successful run."""
        from codeband.orchestration.runner import _run_agent_forever

        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_BASE_DELAY_SECONDS", 0.0,
        )
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_MAX_DELAY_SECONDS", 0.0,
        )

        call_count = 0
        target = asyncio.Event()

        async def run_and_exit():
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                target.set()
            # calls 1–3 return cleanly; call 4 will be cancelled before it matters

        activity = _make_activity()
        task = asyncio.create_task(
            _run_agent_forever(
                lambda _ctx=None: self._agent(run_and_exit),
                "test-agent", activity,
            ),
        )
        try:
            await asyncio.wait_for(target.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        reconnected = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_RECONNECTED"]
        # Attempt 1: no marker. Attempts 2 and 3 each succeed → one marker each.
        assert len(reconnected) == 2, (
            f"expected 2 AGENT_RECONNECTED (attempts 2+3 succeeded), got {reconnected}"
        )

    @pytest.mark.asyncio
    async def test_emitted_after_crash_reconnect(self, monkeypatch) -> None:
        """Reconnect after a crash emits AGENT_RECONNECTED once the next run succeeds."""
        from codeband.orchestration.runner import _run_agent_forever

        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_BASE_DELAY_SECONDS", 0.0,
        )
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_MAX_DELAY_SECONDS", 0.0,
        )

        call_count = 0
        target = asyncio.Event()

        async def crash_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")
            # attempt 2: return cleanly → AGENT_RECONNECTED fires after this
            target.set()

        activity = _make_activity()
        task = asyncio.create_task(
            _run_agent_forever(
                lambda _ctx=None: self._agent(crash_then_succeed),
                "test-agent", activity,
            ),
        )
        try:
            await asyncio.wait_for(target.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        reconnected = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_RECONNECTED"]
        assert len(reconnected) == 1, (
            f"expected 1 AGENT_RECONNECTED after crash→successful reconnect, got {reconnected}"
        )

    @pytest.mark.asyncio
    async def test_not_emitted_when_first_post_reconnect_run_fails(self, monkeypatch) -> None:
        """A failed first post-reconnect run must NOT fire the marker; the next success does."""
        from codeband.orchestration.runner import _run_agent_forever

        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_BASE_DELAY_SECONDS", 0.0,
        )
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_MAX_DELAY_SECONDS", 0.0,
        )

        call_count = 0
        target = asyncio.Event()

        async def clean_then_crash_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # attempt 1: clean exit, no marker
            if call_count == 2:
                raise RuntimeError("post-reconnect crash")  # attempt 2: must NOT emit
            # attempt 3: succeeds → marker fires now
            target.set()

        activity = _make_activity()
        task = asyncio.create_task(
            _run_agent_forever(
                lambda _ctx=None: self._agent(clean_then_crash_then_succeed),
                "test-agent", activity,
            ),
        )
        try:
            await asyncio.wait_for(target.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        reconnected = [c for c in activity.log.call_args_list if c.args[0] == "AGENT_RECONNECTED"]
        assert len(reconnected) == 1, (
            f"expected 1 AGENT_RECONNECTED (only on attempt 3 success, not attempt 2 crash), "
            f"got {reconnected}"
        )
