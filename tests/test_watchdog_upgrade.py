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
        "(subtask_id, from_state, to_state, caller_role, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (SUBTASK_ID, "planned", "in_progress", "conductor", timestamp),
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
    assert store.get_subtask(SUBTASK_ID).state == "blocked"


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
    health = daemon._subtask_state[SUBTASK_ID]
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
    health = daemon._subtask_state[SUBTASK_ID]
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
    assert store.get_subtask(SUBTASK_ID).state == "blocked"
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
    assert SUBTASK_ID not in daemon._subtask_state
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
