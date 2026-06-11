"""Tests for the watchdog mechanical-progress upgrade (RFC WS4 / Phase 3).

These exercise the new git-HEAD / PR-state / transition-log progress signals
and the per-subtask cycle cap. They seed a real ``StateStore`` directly (no
dependency on the Workstream-2 FSM) and mock ``subprocess.run`` for the git /
gh shell-outs.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import WatchdogConfig

SUBTASK_ID = "sub-1"
TASK_ID = "task-1"
ROOM_ID = "room-1"
BASELINE_PR_TS = "2026-05-31T00:00:00+00:00"


# ── seeding helpers ─────────────────────────────────────────────────────────

def _seed_store(
    tmp_path,
    *,
    state: str = "in_progress",
    branch: str | None = "feature-x",
    pr_number: int | None = 42,
):
    """Create a real StateStore with one in-flight subtask row."""
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID)
    metadata = {"branch": branch} if branch is not None else None
    store.ensure_subtask(SUBTASK_ID, TASK_ID, state=state, metadata=metadata)
    if pr_number is not None:
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE subtask_states SET pr_number = ? WHERE subtask_id = ?",
            (pr_number, SUBTASK_ID),
        )
        conn.commit()
        conn.close()
    return store


def _insert_transition(store, *, timestamp: str) -> None:
    """Append a transition_log row so MAX(timestamp) advances."""
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "INSERT INTO transition_log "
        "(subtask_id, task_id, from_state, to_state, caller_role, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (SUBTASK_ID, TASK_ID, "planned", "in_progress", "conductor", timestamp),
    )
    conn.commit()
    conn.close()


def _make_run(signals: dict):
    """Return a fake ``subprocess.run`` driven by a mutable signals dict.

    ``signals['head']`` is the git rev-parse output; ``signals['pr_updated']``
    is the PR ``updatedAt``. Mutate the dict between patrols to simulate
    progress.
    """
    def _run(cmd, *args, **kwargs):
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout=signals["head"], stderr="")
        if cmd[0] == "gh":
            payload = json.dumps(
                {"state": "OPEN", "updatedAt": signals["pr_updated"]},
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    return _run


def _mock_rest():
    rest = MagicMock()
    rest.agent_api_messages = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    return rest


def _daemon(store, *, config: WatchdogConfig, rest=None, activity=None):
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=config,
        rest_client=rest if rest is not None else _mock_rest(),
        agent_id="agent-wd",
        conductor_id="agent-cond",
        activity=activity,
        state_store=store,
    )


# ── cycle-cap escalation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cycle_cap_marks_blocked_after_no_progress(tmp_path, monkeypatch):
    """No git-HEAD change and no new transition across N patrols → blocked."""
    store = _seed_store(tmp_path)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    rest = _mock_rest()
    activity = MagicMock()
    daemon = _daemon(
        store, config=WatchdogConfig(max_phase_visits=3), rest=rest, activity=activity,
    )

    now = datetime.now(UTC)
    # Patrol 1 establishes the baseline (counts as progress); patrols 2-4 are
    # stale, so the cap (3) is crossed on the 4th patrol.
    for _ in range(6):
        await daemon._check_subtask_progress(now)

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    events = [call.args[0] for call in activity.log.call_args_list]
    assert "SUBTASK_BLOCKED" in events

    # The FSM applies the blocked transition, so the alert carries no deferral suffix
    # and the subtask is durably blocked.
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert SUBTASK_ID in msg.content
    assert "could not be applied" not in msg.content
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"


@pytest.mark.asyncio
async def test_merge_pending_subtask_is_patrolled(tmp_path, monkeypatch):
    """A stale ``merge_pending`` subtask escalates like any patrolled state.

    Stage-2 chunk 2b adds ``merge_pending`` to the patrolled set: a subtask
    resting in the merge queue with no mechanical progress (e.g. an approval
    request nobody acted on) crosses the standard stall cap and is blocked +
    escalated once. The watchdog performs no merge reconciliation — that is
    ``cb-phase merge``'s job.
    """
    store = _seed_store(tmp_path, state="merge_pending")
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    rest = _mock_rest()
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    now = datetime.now(UTC)
    for _ in range(5):
        await daemon._check_subtask_progress(now)

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"


@pytest.mark.asyncio
async def test_git_head_change_resets_counter(tmp_path, monkeypatch):
    """A git-HEAD change resets patrol_visits_without_progress to 0."""
    store = _seed_store(tmp_path)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=10))
    now = datetime.now(UTC)

    await daemon._check_subtask_progress(now)  # baseline
    await daemon._check_subtask_progress(now)  # stale → 1
    await daemon._check_subtask_progress(now)  # stale → 2
    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.patrol_visits_without_progress == 2

    signals["head"] = "def456"  # progress
    await daemon._check_subtask_progress(now)
    assert health.patrol_visits_without_progress == 0


@pytest.mark.asyncio
async def test_new_transition_resets_counter(tmp_path, monkeypatch):
    """A newer transition_log entry counts as progress and resets the counter."""
    store = _seed_store(tmp_path)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=10))
    now = datetime.now(UTC)

    await daemon._check_subtask_progress(now)  # baseline
    await daemon._check_subtask_progress(now)  # stale → 1
    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.patrol_visits_without_progress == 1

    _insert_transition(store, timestamp="2026-06-01T00:00:00+00:00")
    await daemon._check_subtask_progress(now)
    assert health.patrol_visits_without_progress == 0


@pytest.mark.asyncio
async def test_fsm_transition_called_when_present(tmp_path, monkeypatch):
    """The stall→blocked transition is applied by the REAL FSM, not a mock.

    No FSM mock here: this exercises the real caller-role authorization and the
    ``store=`` plumbing end-to-end. If the ``(any non-terminal, watchdog) →
    blocked`` edge or the ``store=`` argument is missing, the real transition
    raises, the subtask stays ``in_progress``, and this test fails — exactly the
    regression we want guarded.
    """
    store = _seed_store(tmp_path)  # subtask seeded 'in_progress'
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    rest = _mock_rest()
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    now = datetime.now(UTC)
    for _ in range(3):
        await daemon._check_subtask_progress(now)

    # Durable, real effect: the subtask is actually blocked and audit-logged.
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT to_state, caller_role FROM transition_log WHERE subtask_id = ?",
        (SUBTASK_ID,),
    ).fetchall()
    conn.close()
    assert any(
        r["to_state"] == "blocked" and r["caller_role"] == "watchdog" for r in rows
    )
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "could not be applied" not in msg.content


# ── graceful degradation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_store_skips_progress(monkeypatch):
    """With no store, the mechanical path is a no-op (no subprocess, no crash)."""
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))

    daemon = _daemon(None, config=WatchdogConfig())
    await daemon._check_subtask_progress(datetime.now(UTC))
    assert calls == []


@pytest.mark.asyncio
async def test_git_progress_check_disabled(tmp_path, monkeypatch):
    """git_progress_check=False disables the mechanical signals entirely."""
    store = _seed_store(tmp_path)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))

    daemon = _daemon(store, config=WatchdogConfig(git_progress_check=False))
    await daemon._check_subtask_progress(datetime.now(UTC))
    assert calls == []


@pytest.mark.asyncio
async def test_terminal_subtask_ignored(tmp_path, monkeypatch):
    """Merged/planned subtasks are not tracked for mechanical progress."""
    store = _seed_store(tmp_path, state="planned")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))

    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2))
    await daemon._check_subtask_progress(datetime.now(UTC))
    assert (TASK_ID, SUBTASK_ID) not in daemon._subtask_state
    assert calls == []


# ── existing chat-recency behavior preserved ────────────────────────────────

def _chats_resp(rooms):
    resp = MagicMock()
    resp.data = rooms
    return resp


def _msg(sender_id, minutes_ago):
    m = MagicMock()
    m.sender_id = sender_id
    m.inserted_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    m.content = "working"
    return m


def _participant(pid, name):
    p = MagicMock()
    p.id = pid
    p.name = name
    p.type = "Agent"
    return p


@pytest.mark.asyncio
async def test_patrol_still_nudges_stale_agent_with_store(tmp_path, monkeypatch):
    """The chat-recency nudge path is unaffected by the new subtask check.

    Runs a full ``_patrol`` with a store present but no in-flight subtasks; a
    stale agent must still get nudged exactly as before.
    """
    store = _seed_store(tmp_path, state="merged")  # terminal → no progress work
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    room = MagicMock()
    room.id = ROOM_ID

    rest = _mock_rest()
    rest.agent_api_chats = MagicMock()
    rest.agent_api_chats.list_agent_chats = AsyncMock(
        return_value=_chats_resp([room]),
    )
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[_msg("agent-p0", minutes_ago=30)]),
    )
    parts = MagicMock()
    parts.data = [
        _participant("agent-cond", "Conductor"),
        _participant("agent-p0", "Coder-Claude-0"),
    ]
    rest.agent_api_participants = MagicMock()
    rest.agent_api_participants.list_agent_chat_participants = AsyncMock(
        return_value=parts,
    )

    from codeband.agents.watchdog import WatchdogDaemon

    daemon = WatchdogDaemon(
        config=WatchdogConfig(stale_threshold_seconds=300),
        rest_client=rest,
        agent_id="agent-cond",
        conductor_id="agent-cond",
        state_store=store,
    )
    await daemon._patrol()

    rest.agent_api_messages.create_agent_chat_message.assert_awaited()
    sent = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "Status check" in sent.content


# ── owner escalation on blocked (RFC P5 stage-1b wiring; dormant by default) ──

def _seed_blocked(tmp_path, *, reason="verify-attempt cap 20 reached", owner_id=None):
    """A real store with one subtask driven to ``blocked`` via the FSM.

    Driving it through ``fsm.transition`` (not a bare ``ensure_subtask``) writes
    a real ``→ blocked`` transition_log row carrying ``reason`` so the owner
    escalation can surface it. ``owner_id`` is persisted on the task row so the
    watchdog can resolve the initiator without a constructor override.
    """
    from codeband.state import StateStore
    from codeband.state.fsm import transition

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id=owner_id)
    transition(SUBTASK_ID, TASK_ID, "assigned", caller_role="conductor", store=store)
    transition(SUBTASK_ID, TASK_ID, "in_progress", caller_role="coder", store=store)
    transition(SUBTASK_ID, TASK_ID, "blocked", caller_role="coder",
               reason=reason, store=store)
    return store


def _owner_daemon(store, rest, *, owner_id="owner-1", owner_handle="Owner",
                  activity=None):
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=WatchdogConfig(),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        activity=activity,
        state_store=store,
        owner_id=owner_id,
        owner_handle=owner_handle,
    )


@pytest.mark.asyncio
async def test_blocked_subtask_escalates_to_owner_mention(tmp_path):
    """A blocked subtask triggers a Band @mention to the owner carrying the
    owner handle, the subtask id, and the durable blocked reason."""
    store = _seed_blocked(tmp_path, reason="verify-attempt cap 20 reached")
    rest = _mock_rest()
    activity = MagicMock()
    daemon = _owner_daemon(store, rest, owner_id="owner-1", owner_handle="Owner",
                           activity=activity)

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    call = rest.agent_api_messages.create_agent_chat_message.call_args
    assert call.kwargs["chat_id"] == ROOM_ID
    msg = call.kwargs["message"]
    assert SUBTASK_ID in msg.content
    assert "@Owner" in msg.content                       # owner handle in text
    assert "verify-attempt cap 20 reached" in msg.content  # the durable reason
    assert [m.id for m in msg.mentions] == ["owner-1"]   # structured mention
    events = [c.args[0] for c in activity.log.call_args_list]
    assert "SUBTASK_BLOCKED_OWNER_ESCALATION" in events


@pytest.mark.asyncio
async def test_owner_escalation_is_once_per_subtask(tmp_path):
    """The owner is mentioned a single time even across repeated patrols."""
    store = _seed_blocked(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)

    now = datetime.now(UTC)
    await daemon._check_blocked_subtasks(now)
    await daemon._check_blocked_subtasks(now)
    await daemon._check_blocked_subtasks(now)

    assert rest.agent_api_messages.create_agent_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_owner_escalation_dormant_without_owner_id(tmp_path):
    """With no owner_id (the pre-activation default), the path is a no-op."""
    store = _seed_blocked(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id=None, owner_handle=None)

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_handle_falls_back_to_id(tmp_path):
    """When no display handle is supplied, the owner id is used in the text."""
    store = _seed_blocked(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id="owner-xyz", owner_handle=None)

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "@owner-xyz" in msg.content
    assert [m.id for m in msg.mentions] == ["owner-xyz"]


@pytest.mark.asyncio
async def test_non_blocked_subtasks_are_not_escalated(tmp_path):
    """Only ``blocked`` subtasks escalate to the owner; in-flight ones do not."""
    store = _seed_store(tmp_path, state="in_progress")  # not blocked
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_resolved_from_task_row_without_override(tmp_path):
    """The initiator persisted on the task row drives the escalation even when
    the watchdog carries no constructor owner override (the runner's default)."""
    store = _seed_blocked(tmp_path, owner_id="initiator-7")
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id=None, owner_handle=None)

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert SUBTASK_ID in msg.content
    assert "@initiator-7" in msg.content
    assert [m.id for m in msg.mentions] == ["initiator-7"]


@pytest.mark.asyncio
async def test_no_resolvable_owner_does_not_burn_escalate_once(tmp_path):
    """A blocked subtask with no row owner and no override is skipped without
    consuming its escalate-once marker, so it can escalate once an owner appears.
    """
    store = _seed_blocked(tmp_path, owner_id=None)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id=None, owner_handle=None)

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()
    # The marker must NOT be set — a later patrol can still escalate once an
    # owner is recorded on the task row.
    assert (TASK_ID, SUBTASK_ID) not in daemon._owner_escalated


# ── owner-awareness: nudge exclusion, marker-after-send, active-only patrol ──

def _supersede(store) -> None:
    """Mark the seeded task superseded, as #23's re-registration would."""
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE tasks SET status = 'superseded' WHERE task_id = ?", (TASK_ID,),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_owner_agent_participant_is_never_nudged(tmp_path, monkeypatch):
    """An Agent-typed participant whose id == task.owner_id is never nudged
    (mission control is not a stalled worker); a non-owner agent in the same
    room is still nudge-eligible.
    """
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id="owner-agent")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    room = MagicMock()
    room.id = ROOM_ID

    rest = _mock_rest()
    rest.agent_api_chats = MagicMock()
    rest.agent_api_chats.list_agent_chats = AsyncMock(
        return_value=_chats_resp([room]),
    )
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[
            _msg("owner-agent", minutes_ago=30),  # Agent-typed owner, very stale
            _msg("agent-p0", minutes_ago=30),     # non-owner agent, stale
        ]),
    )
    parts = MagicMock()
    parts.data = [
        _participant("agent-cond", "Conductor"),
        _participant("owner-agent", "MissionControl"),  # type="Agent"
        _participant("agent-p0", "Coder-Claude-0"),
    ]
    rest.agent_api_participants = MagicMock()
    rest.agent_api_participants.list_agent_chat_participants = AsyncMock(
        return_value=parts,
    )

    from codeband.agents.watchdog import WatchdogDaemon

    daemon = WatchdogDaemon(
        config=WatchdogConfig(stale_threshold_seconds=300),
        rest_client=rest,
        agent_id="agent-cond",
        conductor_id="agent-cond",
        state_store=store,
    )
    # Two patrols: if the owner were tracked, the second would escalate it.
    await daemon._patrol()
    await daemon._patrol()

    calls = rest.agent_api_messages.create_agent_chat_message.call_args_list
    assert calls, "the non-owner stale agent must still be nudged"
    for call in calls:
        msg = call.kwargs["message"]
        assert all(m.id != "owner-agent" for m in msg.mentions)
        assert "MissionControl" not in msg.content
    assert [m.id for m in calls[0].kwargs["message"].mentions] == ["agent-p0"]
    assert "owner-agent" not in daemon._state


@pytest.mark.asyncio
async def test_owner_escalation_transient_failure_retries(tmp_path):
    """A transient send failure leaves the escalate-once marker unburned; the
    next patrol retries and the successful send burns it — exactly one
    successful escalation total (marker-after-send).
    """
    store = _seed_blocked(tmp_path)
    rest = _mock_rest()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock(
        side_effect=[RuntimeError("simulated transient failure"), None],
    )
    activity = MagicMock()
    daemon = _owner_daemon(store, rest, activity=activity)

    now = datetime.now(UTC)
    await daemon._check_blocked_subtasks(now)
    assert (TASK_ID, SUBTASK_ID) not in daemon._owner_escalated, (
        "transient send failure must not burn the escalate-once marker"
    )

    await daemon._check_blocked_subtasks(now)  # retry succeeds → marker burns
    assert (TASK_ID, SUBTASK_ID) in daemon._owner_escalated

    await daemon._check_blocked_subtasks(now)  # escalate-once holds
    assert rest.agent_api_messages.create_agent_chat_message.await_count == 2
    events = [c.args[0] for c in activity.log.call_args_list]
    assert events.count("SUBTASK_BLOCKED_OWNER_ESCALATION") == 1, (
        "the activity log must record exactly one successful escalation"
    )


@pytest.mark.asyncio
async def test_owner_escalation_422_burns_and_logs_critical(tmp_path, caplog):
    """A 422 mention rejection (owner not mentionable in the room) is
    permanent: burn the marker anyway, log at CRITICAL, never retry.
    """
    import logging

    from thenvoi_rest.core.api_error import ApiError

    store = _seed_blocked(tmp_path)
    rest = _mock_rest()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock(
        side_effect=ApiError(
            status_code=422, headers={},
            body="mentioned_participant_not_in_room",
        ),
    )
    daemon = _owner_daemon(store, rest, owner_id="owner-1")

    with caplog.at_level(logging.CRITICAL, logger="codeband.agents.watchdog"):
        await daemon._check_blocked_subtasks(datetime.now(UTC))

    assert (TASK_ID, SUBTASK_ID) in daemon._owner_escalated
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(critical) == 1
    message = critical[0].getMessage()
    assert "owner owner-1" in message
    assert ROOM_ID in message
    assert "escalation undeliverable" in message

    # No retry on subsequent patrols.
    await daemon._check_blocked_subtasks(datetime.now(UTC))
    assert rest.agent_api_messages.create_agent_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_superseded_task_blocked_subtask_not_escalated(tmp_path):
    """A blocked subtask of a status='superseded' task is invisible to the
    owner-escalation scan: no send, no marker burn, no owner/room resolution.
    """
    store = _seed_blocked(tmp_path, owner_id="initiator-7")
    _supersede(store)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    get_task_spy = MagicMock(wraps=store.get_task)
    store.get_task = get_task_spy

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()
    assert (TASK_ID, SUBTASK_ID) not in daemon._owner_escalated
    get_task_spy.assert_not_called()


@pytest.mark.asyncio
async def test_superseded_task_room_not_patrolled(tmp_path, monkeypatch):
    """Chat-recency: a superseded task's room is skipped before any REST read,
    retiring the stale-room warning for superseded rows.
    """
    store = _seed_store(tmp_path, state="merged")
    _supersede(store)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    room = MagicMock()
    room.id = ROOM_ID

    rest = _mock_rest()
    rest.agent_api_chats = MagicMock()
    rest.agent_api_chats.list_agent_chats = AsyncMock(
        return_value=_chats_resp([room]),
    )
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[_msg("agent-p0", minutes_ago=30)]),
    )
    rest.agent_api_participants = MagicMock()
    rest.agent_api_participants.list_agent_chat_participants = AsyncMock()

    from codeband.agents.watchdog import WatchdogDaemon

    daemon = WatchdogDaemon(
        config=WatchdogConfig(stale_threshold_seconds=300),
        rest_client=rest,
        agent_id="agent-cond",
        conductor_id="agent-cond",
        state_store=store,
    )
    await daemon._patrol()

    rest.agent_api_messages.list_agent_messages.assert_not_awaited()
    rest.agent_api_participants.list_agent_chat_participants.assert_not_awaited()
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_superseded_task_subtask_progress_not_tracked(tmp_path, monkeypatch):
    """Mechanical-progress: in-flight subtasks of a superseded task are not
    tracked — no git/gh shell-outs, no health entry, no escalation.
    """
    store = _seed_store(tmp_path, state="in_progress")
    _supersede(store)
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1))

    rest = _mock_rest()
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    for _ in range(4):
        await daemon._check_subtask_progress(datetime.now(UTC))

    assert calls == []
    assert (TASK_ID, SUBTASK_ID) not in daemon._subtask_state
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


# ── probe repo context (S9-1): injected at construction, used in argv ────────

class TestProbeRepoContext:
    """The runner injects the bare-repo path + config repo slug; the probes
    must use them — cwd-independent git/gh — without changing how a ``None``
    result is counted (stall semantics belong to Batch 3)."""

    def _daemon(self, **kwargs):
        from codeband.agents.watchdog import WatchdogDaemon

        return WatchdogDaemon(
            config=WatchdogConfig(),
            rest_client=_mock_rest(),
            agent_id="agent-wd",
            conductor_id="agent-cond",
            **kwargs,
        )

    def test_git_head_runs_against_injected_bare_repo(self, tmp_path, monkeypatch):
        calls: list = []

        class _R:
            returncode = 0
            stdout = "abc123\n"

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R(),
        )
        daemon = self._daemon(bare_repo=tmp_path / "repo.git")
        assert daemon._git_head("feature-x") == "abc123"
        assert calls == [[
            "git", "-C", str(tmp_path / "repo.git"),
            "rev-parse", "--verify", "--end-of-options", "feature-x",
        ]]

    def test_git_head_without_context_keeps_cwd_resolution(self, monkeypatch):
        calls: list = []

        class _R:
            returncode = 0
            stdout = "abc123\n"

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R(),
        )
        daemon = self._daemon()
        assert daemon._git_head("feature-x") == "abc123"
        # No -C injection, but --verify/--end-of-options still applied (the
        # branch name can never be parsed as an option — sweep-4 F-6).
        assert calls == [[
            "git", "rev-parse", "--verify", "--end-of-options", "feature-x",
        ]]

    def test_git_head_unreadable_branch_is_none_as_before(
        self, tmp_path, monkeypatch,
    ):
        class _R:
            returncode = 128
            stdout = ""

        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _R())
        daemon = self._daemon(bare_repo=tmp_path / "repo.git")
        assert daemon._git_head("gone-branch") is None  # counting unchanged

    def test_pr_updated_at_pins_repo_slug(self, monkeypatch):
        calls: list = []

        class _R:
            returncode = 0
            stdout = json.dumps(
                {"state": "OPEN", "updatedAt": "2026-06-01T00:00:00+00:00"},
            )

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R(),
        )
        daemon = self._daemon(repo_slug="acme/widgets")
        assert daemon._pr_updated_at(42) is not None
        assert calls == [[
            "gh", "pr", "view", "42",
            "--json", "state,updatedAt", "--repo", "acme/widgets",
        ]]

    def test_pr_updated_at_without_slug_keeps_cwd_resolution(self, monkeypatch):
        calls: list = []

        class _R:
            returncode = 0
            stdout = json.dumps(
                {"state": "OPEN", "updatedAt": "2026-06-01T00:00:00+00:00"},
            )

        monkeypatch.setattr(
            subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _R(),
        )
        daemon = self._daemon()
        assert daemon._pr_updated_at(42) is not None
        assert calls == [["gh", "pr", "view", "42", "--json", "state,updatedAt"]]

# ── patrol coverage: resting states where dispatched work can die (S2-1/F12) ─

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["needs_rebase", "review_pending", "review_failed", "review_passed"],
)
async def test_resting_states_are_patrolled(tmp_path, monkeypatch, state):
    """A stale subtask in any resting state crosses the stall cap and blocks.

    These are the states where dispatched work can silently die (a reviewer
    that never renders a verdict, a coder that never picks up rework, a
    Mergemaster that never queues the approved PR, a rebase nobody starts).
    The mechanical signals are state-agnostic: transition recency applies to
    every state, and the PR signal applies because pr_number is set.
    """
    store = _seed_store(tmp_path, state=state)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    rest = _mock_rest()
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    now = datetime.now(UTC)
    for _ in range(5):
        await daemon._check_subtask_progress(now)

    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["needs_rebase", "review_pending", "review_failed", "review_passed"],
)
async def test_resting_state_progress_resets_counter(tmp_path, monkeypatch, state):
    """The standard progress signals (here: PR activity) reset the counter for
    the newly patrolled states, so an actively-progressing subtask never
    escalates."""
    store = _seed_store(tmp_path, state=state)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=10))
    now = datetime.now(UTC)
    await daemon._check_subtask_progress(now)  # baseline
    await daemon._check_subtask_progress(now)  # stale → 1
    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.patrol_visits_without_progress == 1

    signals["pr_updated"] = "2026-06-01T12:00:00+00:00"  # PR activity
    await daemon._check_subtask_progress(now)
    assert health.patrol_visits_without_progress == 0


# ── observation vs absence (S6-F6) ───────────────────────────────────────────

def _make_failing_run():
    """Every git/gh shell-out fails — the probe is fully degraded."""
    def _run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    return _run


@pytest.mark.asyncio
async def test_all_signal_reads_failed_does_not_count(tmp_path, monkeypatch):
    """A patrol where EVERY signal read failed observed nothing — it must not
    advance the stall counter (and so can never escalate a subtask it cannot
    see). The consecutive no-data counter tracks the degraded probe instead."""
    store = _seed_store(tmp_path)
    monkeypatch.setattr(subprocess, "run", _make_failing_run())

    rest = _mock_rest()
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    # Sever the one remaining signal: the transition-log read itself fails.
    monkeypatch.setattr(
        daemon, "_latest_transition", lambda subtask_id, task_id: (False, None),
    )

    now = datetime.now(UTC)
    for _ in range(5):
        await daemon._check_subtask_progress(now)

    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.patrol_visits_without_progress == 0
    assert health.no_data_patrols == 5
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "in_progress"


@pytest.mark.asyncio
async def test_returned_but_unchanged_still_counts(tmp_path, monkeypatch):
    """Failed git/gh reads with a SUCCESSFUL transition-log read still count:
    'queried fine, nothing new' is an observation of absence, not a failure —
    the stall cap must keep working when only the shell-outs are degraded."""
    store = _seed_store(tmp_path)
    monkeypatch.setattr(subprocess, "run", _make_failing_run())
    _insert_transition(store, timestamp="2026-05-31T00:00:00+00:00")

    rest = _mock_rest()
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    now = datetime.now(UTC)
    for _ in range(4):
        await daemon._check_subtask_progress(now)

    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.no_data_patrols == 0
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"
    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_data_counter_resets_on_recovery(tmp_path, monkeypatch):
    """Once any signal read succeeds again, the no-data counter resets."""
    store = _seed_store(tmp_path)
    failing = {"on": True}

    def _run(cmd, *args, **kwargs):
        if failing["on"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123", stderr="")
        payload = json.dumps({"state": "OPEN", "updatedAt": BASELINE_PR_TS})
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=10))
    monkeypatch.setattr(
        daemon, "_latest_transition", lambda subtask_id, task_id: (False, None),
    )

    now = datetime.now(UTC)
    await daemon._check_subtask_progress(now)
    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.no_data_patrols == 1

    failing["on"] = False
    await daemon._check_subtask_progress(now)
    assert health.no_data_patrols == 0


# ── rung-3 marker-after-send (S6-F7n) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_stall_escalation_marker_burns_only_on_success(tmp_path, monkeypatch):
    """health.escalated burns only when the escalation took effect (FSM
    transition or alert landed) — a patrol where both failed retries next
    patrol instead of silently never escalating. A double-send is acceptable;
    a silent permanent stall is not."""
    store = _seed_store(tmp_path)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    rest = _mock_rest()
    send = rest.agent_api_messages.create_agent_chat_message
    send.side_effect = RuntimeError("band is down")
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)
    # Both halves fail: no FSM effect, no alert.
    monkeypatch.setattr(daemon, "_mark_blocked_via_fsm", lambda sub: False)

    now = datetime.now(UTC)
    for _ in range(3):  # baseline + 2 stale → cap crossed, escalation fails
        await daemon._check_subtask_progress(now)
    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.escalated is False
    assert send.await_count == 1

    await daemon._check_subtask_progress(now)  # still failing → retried
    assert send.await_count == 2
    assert health.escalated is False

    send.side_effect = None  # Band recovers — the alert lands
    await daemon._check_subtask_progress(now)
    assert send.await_count == 3
    assert health.escalated is True

    await daemon._check_subtask_progress(now)  # escalate-once: no further send
    assert send.await_count == 3


@pytest.mark.asyncio
async def test_stall_escalation_fsm_success_burns_marker_despite_send_failure(
    tmp_path, monkeypatch,
):
    """The FSM transition landing IS an effect: the marker burns even when the
    room alert fails, because the durable blocked state already escalates via
    the owner patrol."""
    store = _seed_store(tmp_path)
    signals = {"head": "abc123", "pr_updated": BASELINE_PR_TS}
    monkeypatch.setattr(subprocess, "run", _make_run(signals))

    rest = _mock_rest()
    rest.agent_api_messages.create_agent_chat_message.side_effect = (
        RuntimeError("band is down")
    )
    daemon = _daemon(store, config=WatchdogConfig(max_phase_visits=2), rest=rest)

    now = datetime.now(UTC)
    for _ in range(3):
        await daemon._check_subtask_progress(now)

    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.escalated is True
    assert store.get_subtask(SUBTASK_ID, TASK_ID).state == "blocked"
