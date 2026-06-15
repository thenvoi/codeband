"""Tests for the stall->blocked mailbox discriminator (PR-2 of 2).

The watchdog stall rung counts an absent transition + an absent git-HEAD change
across N patrols as a substantive stall. A transport pin on the FSM-expected
actor presents the same symptoms, so without the discriminator a pinned agent
gets marked ``blocked`` — poisoning recovery because ``blocked`` has no auto-
resume edge. These tests cover the discriminator and the active-notify
upgrade for unhealable-pin escalation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import WatchdogConfig

SUBTASK_ID = "sub-1"
TASK_ID = "task-1"
ROOM_ID = "room-1"


# ── seeding helpers ─────────────────────────────────────────────────────────

def _seed_store(tmp_path, *, state: str = "in_progress"):
    """Store with one subtask in ``state`` — no PR, no branch (so the stall
    rung's mechanical probes don't fire git/gh subprocess.run during tests)."""
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id="owner-1")
    store.ensure_subtask(SUBTASK_ID, TASK_ID, state=state)
    return store


def _mock_rest() -> MagicMock:
    rest = MagicMock()
    rest.agent_api_messages = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    return rest


def _make_message(message_id: str, inserted_at: datetime) -> MagicMock:
    msg = MagicMock()
    msg.id = message_id
    msg.inserted_at = inserted_at
    return msg


def _make_messages_response(messages: list) -> MagicMock:
    resp = MagicMock()
    resp.data = messages
    return resp


def _agent_rest_with(
    *,
    processing: list | None = None,
    pending: list | None = None,
    raise_on_pending: Exception | None = None,
    raise_on_processing: Exception | None = None,
) -> MagicMock:
    """Build a per-agent REST client whose list_agent_messages returns the
    given processing / pending payloads (or raises). Mirrors the heal-rung
    helper in test_watchdog_transport_heal.py but trimmed to what the
    discriminator probe needs (no verify re-list)."""
    client = MagicMock()
    client.agent_api_messages = MagicMock()

    async def _list_side_effect(*args: object, **kwargs: object) -> MagicMock:
        status = kwargs.get("status", "")
        if status == "processing":
            if raise_on_processing is not None:
                raise raise_on_processing
            return _make_messages_response(processing or [])
        if status == "pending":
            if raise_on_pending is not None:
                raise raise_on_pending
            return _make_messages_response(pending or [])
        return _make_messages_response([])

    client.agent_api_messages.list_agent_messages = AsyncMock(
        side_effect=_list_side_effect,
    )
    return client


def _daemon(
    store,
    rest,
    *,
    role_map: dict[str, str],
    agent_rest_clients: dict[str, MagicMock],
    owner_id: str | None = "owner-1",
    owner_handle: str | None = "Owner",
    config: WatchdogConfig | None = None,
):
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=config or WatchdogConfig(transport_pin_threshold_seconds=600),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        state_store=store,
        owner_id=owner_id,
        owner_handle=owner_handle,
        agent_id_to_role=role_map,
        agent_rest_clients=agent_rest_clients,
    )


# ── discriminator ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discriminator_defers_block_when_expected_actor_is_pinned(tmp_path):
    """Subtask state=in_progress (expected actor: coder); the coder's
    pending head in the room is older than threshold → block DEFERRED:
    no FSM transition, no escalation post."""
    store = _seed_store(tmp_path, state="in_progress")
    now = datetime.now(UTC)
    pinned_head = _make_message("msg-pinned", now - timedelta(seconds=900))  # > T
    coder_client = _agent_rest_with(pending=[pinned_head])

    rest = _mock_rest()
    daemon = _daemon(
        store, rest,
        role_map={"coder-1": "coder"},
        agent_rest_clients={"coder-1": coder_client},
    )

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    result = await daemon._send_blocked_escalation(sub)

    assert result is False
    # No FSM transition — subtask still in_progress.
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "in_progress"
    # No escalation post on the watchdog's REST client.
    rest.agent_api_messages.create_agent_chat_message.assert_not_called()


@pytest.mark.asyncio
async def test_discriminator_proceeds_when_expected_actor_mailbox_is_clear(tmp_path):
    """Subtask state=in_progress, coder's mailbox empty → block PROCEEDS as
    today: FSM transition to blocked and the alert posts."""
    store = _seed_store(tmp_path, state="in_progress")
    coder_client = _agent_rest_with(pending=[], processing=[])

    rest = _mock_rest()
    daemon = _daemon(
        store, rest,
        role_map={"coder-1": "coder"},
        agent_rest_clients={"coder-1": coder_client},
    )

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    result = await daemon._send_blocked_escalation(sub)

    assert result is True
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"
    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_discriminator_defers_on_probe_exception(tmp_path):
    """Probe raises (e.g. 429 storm) → fail toward defer: block DEFERRED, no
    FSM transition, no escalation post."""
    from thenvoi_rest.core.api_error import ApiError

    store = _seed_store(tmp_path, state="review_pending")
    # Reviewer is the expected actor for review_pending. Make its probe blow up.
    reviewer_client = _agent_rest_with(
        raise_on_processing=ApiError(status_code=429, headers={}, body="rate limited"),
    )

    rest = _mock_rest()
    daemon = _daemon(
        store, rest,
        role_map={"reviewer-1": "reviewer"},
        agent_rest_clients={"reviewer-1": reviewer_client},
    )

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    result = await daemon._send_blocked_escalation(sub)

    assert result is False
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "review_pending"
    rest.agent_api_messages.create_agent_chat_message.assert_not_called()


@pytest.mark.asyncio
async def test_discriminator_review_passed_defers_when_mergemaster_pinned(tmp_path):
    """review_passed expects {verifier, mergemaster} — the v1 approximation
    checks BOTH and defers if either is pinned. Verifier clear, mergemaster
    pinned → DEFERRED (documents the over-defer behavior; over-defer is a
    bounded delay, never a wrong block)."""
    store = _seed_store(tmp_path, state="review_passed")
    now = datetime.now(UTC)
    pinned_head = _make_message("msg-mm-pinned", now - timedelta(seconds=900))
    verifier_client = _agent_rest_with(pending=[], processing=[])
    mm_client = _agent_rest_with(pending=[pinned_head])

    rest = _mock_rest()
    daemon = _daemon(
        store, rest,
        role_map={
            "verifier-1": "verifier",
            "mergemaster-1": "mergemaster",
        },
        agent_rest_clients={
            "verifier-1": verifier_client,
            "mergemaster-1": mm_client,
        },
    )

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    result = await daemon._send_blocked_escalation(sub)

    assert result is False
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "review_passed"
    rest.agent_api_messages.create_agent_chat_message.assert_not_called()


# ── unhealable-pin escalation: active owner notify ──────────────────────────

@pytest.mark.asyncio
async def test_unhealable_pin_escalation_mentions_owner():
    """_escalate_unhealable_pin includes the owner in the structured mention
    list and the @handle prefix in the message text (active notify), so the
    human is woken instead of relying on them seeing a passive post."""
    from codeband.agents.watchdog import WatchdogDaemon

    rest = _mock_rest()
    daemon = WatchdogDaemon(
        config=WatchdogConfig(transport_pin_threshold_seconds=600),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        owner_id="owner-1",
        owner_handle="Owner",
    )

    await daemon._escalate_unhealable_pin(
        agent_id="coder-1", room_id=ROOM_ID, message_id="msg-stubborn", attempts=3,
    )

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert [m.id for m in msg.mentions] == ["owner-1"]
    assert "@Owner" in msg.content


@pytest.mark.asyncio
async def test_unhealable_pin_escalation_degrades_without_owner():
    """When no owner_id is configured the escalation degrades to mention-less
    (preserves the pre-upgrade behavior so no None id reaches the server)."""
    from codeband.agents.watchdog import WatchdogDaemon

    rest = _mock_rest()
    daemon = WatchdogDaemon(
        config=WatchdogConfig(transport_pin_threshold_seconds=600),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        owner_id=None,
        owner_handle=None,
    )

    await daemon._escalate_unhealable_pin(
        agent_id="coder-1", room_id=ROOM_ID, message_id="msg-stubborn", attempts=3,
    )

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert msg.mentions == []
