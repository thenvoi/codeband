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

def _write_room_pointer(store, room_id: str = ROOM_ID) -> None:
    (store.db_path.parent / ".codeband_room").write_text(room_id, encoding="utf-8")


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
    _write_room_pointer(store)
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
    # 10 minutes: stale against the 300s threshold, comfortably inside the
    # client-side read window (2 x max threshold = 30min) the agent-API path
    # now applies after fetch.
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[_msg("agent-p0", minutes_ago=10)]),
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


# ── ledger integrity rung (Stage-3) ─────────────────────────────────────────

def _seed_chain(tmp_path, *, owner_id="owner-1"):
    """A real store with a few hash-chained transition_log rows + an owner."""
    from codeband.state import StateStore
    from codeband.state.fsm import transition

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id=owner_id)
    transition(SUBTASK_ID, TASK_ID, "assigned", caller_role="conductor", store=store)
    transition(SUBTASK_ID, TASK_ID, "in_progress", caller_role="coder", store=store)
    transition(SUBTASK_ID, TASK_ID, "verify_pending", caller_role="coder", store=store)
    return store


@pytest.mark.asyncio
async def test_integrity_baseline_then_chain_break_alerts(tmp_path):
    """A first patrol baselines the chain (no alert); a subsequent in-place edit
    of a NEW row breaks the chain forward and escalates a ledger-integrity alert."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)

    # Tamper with an existing row in place BEFORE the first patrol, so the
    # first (full) walk catches it.
    conn = sqlite3.connect(store.db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM transition_log ORDER BY id ASC").fetchall()]
    conn.execute("UPDATE transition_log SET to_state = 'forged' WHERE id = ?", (ids[1],))
    conn.commit()
    conn.close()

    await daemon._check_chain_integrity(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_awaited()
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "LEDGER INTEGRITY" in msg.content
    assert "chain break" in msg.content
    assert [m.id for m in msg.mentions] == ["owner-1"]


@pytest.mark.asyncio
async def test_integrity_head_regression_on_truncation(tmp_path):
    """Truncating the tail of a verified chain — which a forward walk cannot
    see — triggers a head-regression alert."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)

    # First patrol: clean baseline, no alert.
    await daemon._check_chain_integrity(datetime.now(UTC))
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    # Truncate the tail — delete the last (verified) row.
    conn = sqlite3.connect(store.db_path)
    last = conn.execute("SELECT MAX(id) FROM transition_log").fetchone()[0]
    conn.execute("DELETE FROM transition_log WHERE id = ?", (last,))
    conn.commit()
    conn.close()

    await daemon._check_chain_integrity(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_awaited()
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "head regression" in msg.content


@pytest.mark.asyncio
async def test_integrity_alert_is_once_per_room_chain_kind(tmp_path):
    """The same integrity break escalates a single time across repeated patrols."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)

    conn = sqlite3.connect(store.db_path)
    first = conn.execute("SELECT MIN(id) FROM transition_log").fetchone()[0]
    conn.execute("UPDATE transition_log SET reason = 'x' WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    now = datetime.now(UTC)
    await daemon._check_chain_integrity(now)
    await daemon._check_chain_integrity(now)
    await daemon._check_chain_integrity(now)

    assert rest.agent_api_messages.create_agent_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_integrity_clean_chain_never_alerts(tmp_path):
    """A clean chain produces no integrity escalation across patrols."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)

    now = datetime.now(UTC)
    await daemon._check_chain_integrity(now)
    await daemon._check_chain_integrity(now)

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_integrity_rung_dormant_without_store():
    """No store → the integrity rung is a no-op (no crash, no send)."""
    rest = _mock_rest()
    daemon = _daemon(None, config=WatchdogConfig(), rest=rest)

    await daemon._check_chain_integrity(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


# ── deep full-history integrity sweep (Stage-3 PR3) ─────────────────────────


@pytest.mark.asyncio
async def test_full_history_catches_interior_tamper_incremental_misses(tmp_path):
    """The blind-spot case. After the incremental rung baselines past the tip,
    an in-place edit of an INTERIOR old row (id below the remembered tip) is
    invisible to the incremental walk — it only re-reads rows past the tip, and
    the head-regression check inspects only the (unchanged) tip — yet the
    full-history sweep, walking from row 1, catches it."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    # interval=1 so the sweep runs on every call.
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=1)

    # Baseline the incremental rung: walks the whole chain, advances the
    # remembered tip past every current row.
    await daemon._check_chain_integrity(datetime.now(UTC))
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    # Tamper with an INTERIOR row (not the tip) in place.
    conn = sqlite3.connect(store.db_path)
    ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM transition_log ORDER BY id ASC"
        ).fetchall()
    ]
    interior_id = ids[1]  # strictly below the remembered tip (ids[-1])
    conn.execute(
        "UPDATE transition_log SET to_state = 'forged' WHERE id = ?",
        (interior_id,),
    )
    conn.commit()
    conn.close()

    # Incremental rung MISSES it: only walks rows past the tip; the tip row is
    # unchanged so head-regression stays silent.
    await daemon._check_chain_integrity(datetime.now(UTC))
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    # Full-history sweep CATCHES it and escalates to the owner.
    await daemon._check_chain_integrity_full(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_awaited()
    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "LEDGER INTEGRITY" in msg.content
    assert "chain break" in msg.content
    assert "verifier" in msg.content.lower()  # attributed to the verifier role
    assert [m.id for m in msg.mentions] == ["owner-1"]


@pytest.mark.asyncio
async def test_full_history_alert_is_once_per_room_chain_kind(tmp_path):
    """The full-history sweep escalates a single time per (room, chain, kind)
    across repeated patrols, reusing the rung's escalate-once pattern."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=1)

    conn = sqlite3.connect(store.db_path)
    first = conn.execute("SELECT MIN(id) FROM transition_log").fetchone()[0]
    conn.execute("UPDATE transition_log SET reason = 'forged' WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    now = datetime.now(UTC)
    await daemon._check_chain_integrity_full(now)
    await daemon._check_chain_integrity_full(now)
    await daemon._check_chain_integrity_full(now)

    assert rest.agent_api_messages.create_agent_chat_message.await_count == 1


@pytest.mark.asyncio
async def test_full_history_marker_independent_of_incremental_rung(tmp_path):
    """The two rungs keep separate escalate-once sets: an incremental alert in a
    room does not suppress the full-history sweep's own alert there."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=1)

    # Pre-mark the incremental rung as having already alerted this room/chain/kind.
    daemon._integrity_alerted.add((ROOM_ID, "transition_log", "chain_break"))

    conn = sqlite3.connect(store.db_path)
    first = conn.execute("SELECT MIN(id) FROM transition_log").fetchone()[0]
    conn.execute("UPDATE transition_log SET reason = 'forged' WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    await daemon._check_chain_integrity_full(datetime.now(UTC))

    # The full rung still fires despite the incremental marker being set.
    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    assert (ROOM_ID, "transition_log", "chain_break") in daemon._full_integrity_alerted


@pytest.mark.asyncio
async def test_full_history_runs_only_every_n_patrols(tmp_path):
    """With interval N, the full-history sweep verifies only on the Nth patrol;
    earlier patrols are cheap no-ops even when a break is present."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=3)

    conn = sqlite3.connect(store.db_path)
    first = conn.execute("SELECT MIN(id) FROM transition_log").fetchone()[0]
    conn.execute("UPDATE transition_log SET reason = 'forged' WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    now = datetime.now(UTC)
    # Patrols 1 and 2 are below the interval — no walk, no alert.
    await daemon._check_chain_integrity_full(now)
    await daemon._check_chain_integrity_full(now)
    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()
    # Patrol 3 hits the interval — the sweep fires.
    await daemon._check_chain_integrity_full(now)
    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_history_runs_independent_of_verifier_seat(tmp_path):
    """The sweep is code-driven: it escalates even with the verifier LLM seat
    INERT, because integrity is a safety sweep, not an LLM behavior. The
    watchdog takes no verifier-pool argument at all, so it cannot structurally
    depend on seat allocation — demonstrated here with the seat explicitly
    disabled (the product default activates it)."""
    from codeband.config import AgentsConfig, VerifiersConfig

    # Seat explicitly INERT (no allocated worker) — the sweep must still fire.
    assert AgentsConfig(verifiers=VerifiersConfig()).verifiers.total_count() == 0

    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=1)

    conn = sqlite3.connect(store.db_path)
    first = conn.execute("SELECT MIN(id) FROM transition_log").fetchone()[0]
    conn.execute("UPDATE transition_log SET reason = 'forged' WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    await daemon._check_chain_integrity_full(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_awaited()


@pytest.mark.asyncio
async def test_full_history_finding_attributed_to_verifier_role(tmp_path):
    """The activity-log actor for a full-history finding is the verifier role,
    not the watchdog."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    activity = MagicMock()
    daemon = _owner_daemon(store, rest, activity=activity)
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=1)

    conn = sqlite3.connect(store.db_path)
    first = conn.execute("SELECT MIN(id) FROM transition_log").fetchone()[0]
    conn.execute("UPDATE transition_log SET reason = 'forged' WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    await daemon._check_chain_integrity_full(datetime.now(UTC))

    actors = [
        c.args[1] for c in activity.log.call_args_list
        if c.args and c.args[0] == "LEDGER_INTEGRITY_ALERT"
    ]
    assert actors == ["verifier"]


@pytest.mark.asyncio
async def test_full_history_clean_chain_never_alerts(tmp_path):
    """A clean chain produces no full-history escalation."""
    store = _seed_chain(tmp_path)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest)
    daemon._config = WatchdogConfig(full_integrity_interval_patrols=1)

    now = datetime.now(UTC)
    await daemon._check_chain_integrity_full(now)
    await daemon._check_chain_integrity_full(now)

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_history_rung_dormant_without_store():
    """No store → the full-history rung is a no-op (no crash, no send)."""
    rest = _mock_rest()
    daemon = _daemon(
        None,
        config=WatchdogConfig(full_integrity_interval_patrols=1),
        rest=rest,
    )

    await daemon._check_chain_integrity_full(datetime.now(UTC))

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


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
    # 10 minutes: stale against the 300s threshold, inside the client-side
    # read window the agent-API path now applies after fetch.
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[
            _msg("owner-agent", minutes_ago=10),  # Agent-typed owner, stale
            _msg("agent-p0", minutes_ago=10),     # non-owner agent, stale
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
    # Sever the transition-log signal: both the batch path and the per-subtask
    # fallback must fail so the patrol sees a total-read-failure.
    monkeypatch.setattr(
        store, "batch_latest_transitions", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )
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
    # Sever both the batch path and the per-subtask fallback so the first patrol
    # has no transition-log data at all (total degraded read).
    monkeypatch.setattr(
        store, "batch_latest_transitions", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    monkeypatch.setattr(
        daemon, "_latest_transition", lambda subtask_id, task_id: (False, None),
    )

    now = datetime.now(UTC)
    await daemon._check_subtask_progress(now)
    health = daemon._subtask_state[(TASK_ID, SUBTASK_ID)]
    assert health.no_data_patrols == 1

    # Recovery: restore the DB so the batch can succeed on the next patrol.
    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", _run)
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


# ── patrol cost: mention-pattern cache + client-side read window (S8-F1) ────

class TestMentionPatternCache:
    def test_patterns_compiled_once_per_participant_set(self):
        from codeband.agents.watchdog import _mention_patterns

        participants = frozenset({("p1", "Coder-Claude-0"), ("p2", "Reviewer-Codex-0")})
        first = _mention_patterns(participants)
        second = _mention_patterns(frozenset(participants))
        assert first is second  # lru_cache hit — no recompilation

    def test_cached_patterns_keep_boundary_semantics(self):
        from codeband.agents.watchdog import _mentioned_participant_ids

        names = {"p1": "Coder-Claude-0", "p2": "Coder-Claude-01"}

        class _Msg:
            mentions = None
            content = "ping @Coder-Claude-0 please"

        assert _mentioned_participant_ids(_Msg(), names) == {"p1"}

        class _Email:
            mentions = None
            content = "mail me at me@Coder-Claude-0"

        assert _mentioned_participant_ids(_Email(), names) == set()

    def test_empty_names_excluded(self):
        from codeband.agents.watchdog import _mention_patterns

        patterns = _mention_patterns(frozenset({("p1", ""), ("p2", "Name")}))
        assert [pid for pid, _ in patterns] == ["p2"]


# ── @[[uuid]] inline-markup mentions (finding 17) ───────────────────────────

class TestUuidMentionMarkup:
    UUID = "0c69793a-58c8-4f6e-b167-2eb6cb2ad2e3"

    def _msg(self, content):
        class _Msg:
            mentions = None
        m = _Msg()
        m.content = content
        return m

    def test_uuid_markup_resolves_against_participant_set(self):
        from codeband.agents.watchdog import _mentioned_participant_ids

        names = {self.UUID: "Coder-Codex-0", "p2": "Reviewer-Claude-0"}
        msg = self._msg(f"please pick this back up @[[{self.UUID}]]")
        assert _mentioned_participant_ids(msg, names) == {self.UUID}

    def test_unknown_uuid_markup_is_ignored(self):
        from codeband.agents.watchdog import _mentioned_participant_ids

        names = {self.UUID: "Coder-Codex-0"}
        msg = self._msg("@[[11111111-2222-3333-4444-555555555555]] hello")
        assert _mentioned_participant_ids(msg, names) == set()

    def test_uuid_markup_is_case_insensitive(self):
        from codeband.agents.watchdog import _mentioned_participant_ids

        names = {self.UUID: "Coder-Codex-0"}
        msg = self._msg(f"@[[{self.UUID.upper()}]] re-dispatch")
        assert _mentioned_participant_ids(msg, names) == {self.UUID}

    def test_uuid_markup_combines_with_display_name_form(self):
        from codeband.agents.watchdog import _mentioned_participant_ids

        names = {self.UUID: "Coder-Codex-0", "p2": "Reviewer-Claude-0"}
        msg = self._msg(f"@Reviewer-Claude-0 and @[[{self.UUID}]] — sync up")
        assert _mentioned_participant_ids(msg, names) == {self.UUID, "p2"}


@pytest.mark.asyncio
async def test_terminal_message_does_not_untrack_agent_with_newer_uuid_mention(
    tmp_path, monkeypatch,
):
    """The compounding half of finding 17: an agent whose last own message is
    terminal-shaped but that has an UNANSWERED @[[uuid]] re-dispatch newer
    than it must stay tracked — and get nudged when the mention goes stale."""
    agent_uuid = "0c69793a-58c8-4f6e-b167-2eb6cb2ad2e3"
    store = _seed_store(tmp_path, state="merged")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    room = MagicMock()
    room.id = ROOM_ID

    done = _msg(agent_uuid, minutes_ago=60)
    done.content = "Review PASSED — risk low. Merged PR #12."  # terminal-shaped
    redispatch = _msg("agent-cond", minutes_ago=10)  # stale vs 300s threshold
    redispatch.content = f"@[[{agent_uuid}]] please pick up subtask st-9"

    rest = _mock_rest()
    rest.agent_api_chats = MagicMock()
    rest.agent_api_chats.list_agent_chats = AsyncMock(
        return_value=_chats_resp([room]),
    )
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[done, redispatch]),
    )
    parts = MagicMock()
    parts.data = [
        _participant("agent-cond", "Conductor"),
        _participant(agent_uuid, "Coder-Codex-0"),
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

    # The agent was NOT untracked: the unanswered re-dispatch keeps it on the
    # clock, and 10 minutes of silence after the mention earns a nudge.
    rest.agent_api_messages.create_agent_chat_message.assert_awaited()
    sent = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert "Status check" in sent.content


@pytest.mark.asyncio
async def test_terminal_message_still_untracks_without_newer_mention(
    tmp_path, monkeypatch,
):
    """Control: the same terminal-shaped last message with NO newer inbound
    mention keeps the historical untrack behavior — no nudge."""
    store = _seed_store(tmp_path, state="merged")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)

    room = MagicMock()
    room.id = ROOM_ID

    done = _msg("agent-p0", minutes_ago=60)
    done.content = "Review PASSED — risk low. Merged PR #12."

    rest = _mock_rest()
    rest.agent_api_chats = MagicMock()
    rest.agent_api_chats.list_agent_chats = AsyncMock(
        return_value=_chats_resp([room]),
    )
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[done]),
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

    rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_api_messages_bounded_client_side(tmp_path):
    """The free-tier agent-API path has no server-side `since` param, so the
    window bound is applied client-side after fetch — old history never
    reaches the per-message mention scan. Messages with no/unparseable
    timestamp are kept (the patrol loop already skips them)."""
    store = _seed_store(tmp_path)
    now = datetime.now(UTC)

    def _m(mid, ts):
        m = MagicMock()
        m.inserted_at = ts
        m.id = mid
        return m

    recent = _m("recent", now - timedelta(minutes=5))
    ancient = _m("ancient", now - timedelta(days=30))
    no_ts = _m("no-ts", None)

    rest = _mock_rest()
    rest.agent_api_messages.list_agent_messages = AsyncMock(
        return_value=MagicMock(data=[recent, ancient, no_ts]),
    )
    daemon = _daemon(store, config=WatchdogConfig(), rest=rest)

    since = now - timedelta(minutes=60)
    messages = await daemon._list_messages(ROOM_ID, since)
    assert [m.id for m in messages] == ["recent", "no-ts"]


@pytest.mark.asyncio
async def test_human_api_messages_not_filtered_client_side(tmp_path):
    """The human-API path passes `since` server-side; what comes back is
    returned as-is."""
    store = _seed_store(tmp_path)
    now = datetime.now(UTC)

    old_msg = MagicMock()
    old_msg.inserted_at = now - timedelta(days=30)

    human = MagicMock()
    human.human_api_messages = MagicMock()
    human.human_api_messages.list_my_chat_messages = AsyncMock(
        return_value=MagicMock(data=[old_msg]),
    )

    from codeband.agents.watchdog import WatchdogDaemon

    daemon = WatchdogDaemon(
        config=WatchdogConfig(),
        rest_client=_mock_rest(),
        agent_id="agent-wd",
        conductor_id="agent-cond",
        state_store=store,
        human_rest_client=human,
    )
    since = now - timedelta(minutes=60)
    messages = await daemon._list_messages(ROOM_ID, since)
    assert messages == [old_msg]
    human.human_api_messages.list_my_chat_messages.assert_awaited_once_with(
        chat_id=ROOM_ID, since=since,
    )


# ── free-tier recency probe pages to the window (follow-up to S8-F1) ────────

def _paged_messages_api(pages):
    """An ``list_agent_messages`` fake serving ``pages`` with real paging metadata.

    ``pages`` is oldest-first (page 1 = oldest), each page's messages
    oldest-first — the agent API's actual ordering. Returns ``(fetch,
    requested_pages)``; the recorded page numbers prove which pages the probe
    walked. ``total_pages`` is a real int so the probe's isinstance guard
    accepts it (unlike the MagicMock sentinels of the legacy mocks).
    """
    requested: list[int | None] = []

    async def fetch(*, chat_id, status="all", page=None, page_size=None):
        requested.append(page)
        p = page or 1
        resp = MagicMock()
        resp.data = list(pages[p - 1])
        resp.metadata = MagicMock()
        resp.metadata.page = p
        resp.metadata.total_pages = len(pages)
        return resp

    return fetch, requested


def _ts_msg(mid, ts):
    m = MagicMock()
    m.id = mid
    m.inserted_at = ts
    return m


@pytest.mark.asyncio
async def test_long_room_recent_activity_on_last_page_is_seen(tmp_path):
    """A 3-page room with all recent activity on the LAST page: the probe must
    see it. Before paging, the un-paged fetch returned page 1 (oldest), the
    window filtered it to empty, and the probe read an active room as silence.
    """
    store = _seed_store(tmp_path)
    now = datetime.now(UTC)
    old = now - timedelta(days=10)
    pages = [
        [_ts_msg(f"old-1-{i}", old + timedelta(minutes=i)) for i in range(3)],
        [_ts_msg(f"old-2-{i}", old + timedelta(hours=1, minutes=i)) for i in range(3)],
        # Last page mixed: oldest entry outside the window, newest inside —
        # the window boundary lies inside this page, so the walk stops here.
        [
            _ts_msg("stale-3-0", now - timedelta(hours=3)),
            _ts_msg("recent-3-1", now - timedelta(minutes=10)),
            _ts_msg("recent-3-2", now - timedelta(minutes=5)),
        ],
    ]
    fetch, requested = _paged_messages_api(pages)
    rest = _mock_rest()
    rest.agent_api_messages.list_agent_messages = fetch
    daemon = _daemon(store, config=WatchdogConfig(), rest=rest)

    since = now - timedelta(minutes=60)
    messages = await daemon._list_messages(ROOM_ID, since)

    assert [m.id for m in messages] == ["recent-3-1", "recent-3-2"]
    # One probe request (learns total_pages), then the LAST page; the window
    # boundary is inside it, so no further walk.
    assert requested == [1, 3]


@pytest.mark.asyncio
async def test_walk_continues_while_window_extends_into_earlier_pages(tmp_path):
    """A fully-in-window last page means the window may extend further back:
    the walk fetches the previous page too, and an all-recent page 1 is
    served from the probe request without a refetch."""
    store = _seed_store(tmp_path)
    now = datetime.now(UTC)
    pages = [
        [_ts_msg("p1-0", now - timedelta(minutes=30))],
        [_ts_msg("p2-0", now - timedelta(minutes=20))],
        [_ts_msg("p3-0", now - timedelta(minutes=10))],
    ]
    fetch, requested = _paged_messages_api(pages)
    rest = _mock_rest()
    rest.agent_api_messages.list_agent_messages = fetch
    daemon = _daemon(store, config=WatchdogConfig(), rest=rest)

    since = now - timedelta(minutes=60)
    messages = await daemon._list_messages(ROOM_ID, since)

    # Everything is inside the window, oldest→newest order preserved.
    assert [m.id for m in messages] == ["p1-0", "p2-0", "p3-0"]
    # Page 1 is reused from the probe request — never fetched twice.
    assert requested == [1, 3, 2]


@pytest.mark.asyncio
async def test_short_room_single_page_unchanged(tmp_path):
    """A one-page room: one fetch, window applied, behavior as before."""
    store = _seed_store(tmp_path)
    now = datetime.now(UTC)
    pages = [
        [
            _ts_msg("ancient", now - timedelta(days=30)),
            _ts_msg("recent", now - timedelta(minutes=5)),
            _ts_msg("no-ts", None),
        ],
    ]
    fetch, requested = _paged_messages_api(pages)
    rest = _mock_rest()
    rest.agent_api_messages.list_agent_messages = fetch
    daemon = _daemon(store, config=WatchdogConfig(), rest=rest)

    since = now - timedelta(minutes=60)
    messages = await daemon._list_messages(ROOM_ID, since)

    assert [m.id for m in messages] == ["recent", "no-ts"]
    assert requested == [1]


@pytest.mark.asyncio
async def test_page_walk_is_capped(tmp_path):
    """A very long room that is fully active stops after _MAX_PROBE_PAGES
    newest pages — beyond that the room is active by definition and older
    history cannot change any recency verdict."""
    from codeband.agents.watchdog import _MAX_PROBE_PAGES

    store = _seed_store(tmp_path)
    now = datetime.now(UTC)
    # 10 pages, every message inside the window (an extremely busy room).
    pages = [
        [_ts_msg(f"p{p}-0", now - timedelta(minutes=59 - p))]
        for p in range(1, 11)
    ]
    fetch, requested = _paged_messages_api(pages)
    rest = _mock_rest()
    rest.agent_api_messages.list_agent_messages = fetch
    daemon = _daemon(store, config=WatchdogConfig(), rest=rest)

    since = now - timedelta(minutes=60)
    messages = await daemon._list_messages(ROOM_ID, since)

    # Only the newest _MAX_PROBE_PAGES pages are walked (plus the probe
    # request for page 1 that learned total_pages).
    assert requested == [1, 10, 9, 8, 7, 6]
    assert len(requested) - 1 == _MAX_PROBE_PAGES
    # Newest five pages' contents, oldest→newest.
    assert [m.id for m in messages] == ["p6-0", "p7-0", "p8-0", "p9-0", "p10-0"]


# ── finding 26: stall-blocked alert uses assigned_worker or owner for mention ─

def _seed_stall_blocked(tmp_path, *, assigned_worker: str | None = None,
                        owner_id: str | None = None):
    """Store with one subtask in in_progress and optional worker/owner."""
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id=owner_id)
    _write_room_pointer(store)
    store.ensure_subtask(
        SUBTASK_ID, TASK_ID,
        state="in_progress",
        metadata={"branch": "feature-x"},
    )
    if assigned_worker is not None:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE subtask_states SET assigned_worker = ? WHERE subtask_id = ?",
            (assigned_worker, SUBTASK_ID),
        )
        conn.commit()
        conn.close()
    return store


@pytest.mark.asyncio
async def test_stall_blocked_alert_mentions_assigned_worker(tmp_path):
    """When assigned_worker is set the stall-blocked alert mentions them."""
    store = _seed_stall_blocked(tmp_path, assigned_worker="coder-42", owner_id="owner-1")
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id="owner-1")

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    await daemon._send_blocked_escalation(sub)

    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert [m.id for m in msg.mentions] == ["coder-42"]


@pytest.mark.asyncio
async def test_stall_blocked_alert_null_worker_routes_to_owner(tmp_path):
    """With assigned_worker=NULL the alert falls back to the task owner."""
    store = _seed_stall_blocked(tmp_path, assigned_worker=None, owner_id="owner-7")
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id="owner-7")

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    await daemon._send_blocked_escalation(sub)

    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert [m.id for m in msg.mentions] == ["owner-7"]


@pytest.mark.asyncio
async def test_stall_blocked_alert_null_worker_null_owner_no_422(tmp_path):
    """When both assigned_worker and owner are NULL the alert posts without a
    mention so no None id reaches ChatMessageRequestMentionsItem (→ 422)."""
    store = _seed_stall_blocked(tmp_path, assigned_worker=None, owner_id=None)
    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id=None, owner_handle=None)

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    await daemon._send_blocked_escalation(sub)

    msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs["message"]
    assert msg.mentions == []


# ── finding 28: merge_pending SHA-drift rung routes to needs_rebase ──────────

def _seed_merge_pending(tmp_path, *, approved_sha: str, owner_id: str = "owner-1"):
    """Store with a merge_pending subtask whose approved SHA is set."""
    import sqlite3 as _sqlite3
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id=owner_id)
    _write_room_pointer(store)
    store.ensure_subtask(
        SUBTASK_ID, TASK_ID,
        state="in_progress",
        metadata={"branch": "feature-x"},
    )
    conn = _sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE subtask_states SET state = 'merge_pending', "
        "merge_approved_sha = ? WHERE subtask_id = ?",
        (approved_sha, SUBTASK_ID),
    )
    conn.commit()
    conn.close()
    return store


@pytest.mark.asyncio
async def test_merge_pending_sha_drift_routes_to_needs_rebase(tmp_path, monkeypatch):
    """When the branch HEAD has moved past the approved SHA, the watchdog drives
    the merge_pending subtask to needs_rebase via the mergemaster FSM edge."""
    approved_sha = "aaa1111"
    live_sha = "bbb2222"
    store = _seed_merge_pending(tmp_path, approved_sha=approved_sha)
    rest = _mock_rest()

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": live_sha + "\n"})(),
    )

    config = WatchdogConfig(max_phase_visits=10, git_progress_check=True)
    daemon = _daemon(store, config=config, rest=rest)
    now = datetime.now(UTC)
    await daemon._check_subtask_progress(now)

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    assert sub.state == "needs_rebase"


@pytest.mark.asyncio
async def test_merge_pending_sha_stable_no_reroute(tmp_path, monkeypatch):
    """When the live HEAD matches the approved SHA no needs_rebase transition fires."""
    sha = "aaa1111"
    store = _seed_merge_pending(tmp_path, approved_sha=sha)
    rest = _mock_rest()

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": sha + "\n"})(),
    )

    config = WatchdogConfig(max_phase_visits=10, git_progress_check=True)
    daemon = _daemon(store, config=config, rest=rest)
    now = datetime.now(UTC)
    await daemon._check_subtask_progress(now)

    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    assert sub.state == "merge_pending"


# ── _owner_escalated marker resets after resume (batch4 adjacent finding #1) ─

def _drive_to_blocked(store, *, reason: str = "cap reached") -> None:
    """Advance the subtask through assigned→in_progress→blocked via real FSM."""
    from codeband.state.fsm import transition
    transition(SUBTASK_ID, TASK_ID, "assigned", caller_role="conductor", store=store)
    transition(SUBTASK_ID, TASK_ID, "in_progress", caller_role="coder", store=store)
    transition(SUBTASK_ID, TASK_ID, "blocked", caller_role="coder",
               reason=reason, store=store)


@pytest.mark.asyncio
async def test_owner_escalated_marker_cleared_after_resume(tmp_path):
    """After cb-phase resume (blocked → in_progress), the next patrol clears the
    _owner_escalated marker so a subsequent re-block can escalate again."""
    from codeband.state import StateStore
    from codeband.state.fsm import transition

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id="owner-1")
    _drive_to_blocked(store)

    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id="owner-1")

    # First patrol: subtask is blocked → escalates and burns the marker.
    await daemon._check_blocked_subtasks(datetime.now(UTC))
    assert (TASK_ID, SUBTASK_ID) in daemon._owner_escalated

    # Simulate cb-phase resume: drive blocked → in_progress.
    transition(SUBTASK_ID, TASK_ID, "in_progress", caller_role="conductor",
               store=store)

    # Next patrol: subtask is no longer blocked → marker should be cleared.
    await daemon._check_blocked_subtasks(datetime.now(UTC))
    assert (TASK_ID, SUBTASK_ID) not in daemon._owner_escalated


@pytest.mark.asyncio
async def test_owner_reescalates_after_resume_and_reblock(tmp_path):
    """After resume + re-block, a second escalation fires (marker was cleared)."""
    from codeband.state import StateStore
    from codeband.state.fsm import transition

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id="owner-1")
    _drive_to_blocked(store)

    rest = _mock_rest()
    daemon = _owner_daemon(store, rest, owner_id="owner-1")

    # First block + escalation.
    await daemon._check_blocked_subtasks(datetime.now(UTC))
    assert rest.agent_api_messages.create_agent_chat_message.await_count == 1

    # Resume to in_progress, then patrol — subtask not blocked, marker cleared.
    transition(SUBTASK_ID, TASK_ID, "in_progress", caller_role="conductor",
               store=store)
    await daemon._check_blocked_subtasks(datetime.now(UTC))  # clears marker
    assert (TASK_ID, SUBTASK_ID) not in daemon._owner_escalated

    # Re-block.
    transition(SUBTASK_ID, TASK_ID, "blocked", caller_role="coder",
               reason="stalled again", store=store)

    # Second escalation fires because the marker was cleared.
    await daemon._check_blocked_subtasks(datetime.now(UTC))

    assert rest.agent_api_messages.create_agent_chat_message.await_count == 2


# ── batch_latest_transitions used in progress patrol (T-06) ─────────────────

@pytest.mark.asyncio
async def test_progress_patrol_uses_batch_transitions(tmp_path, monkeypatch):
    """_check_subtask_progress pre-fetches all transition timestamps in one
    batch query instead of one per subtask (N+1 elimination). Verify the
    batch path is exercised by monkeypatching _latest_transition to fail if
    called — a patrol against a store with no transition rows must still
    complete without error via the batch (empty result → ok=True, ts=None).
    """
    store = _seed_store(tmp_path)
    # No git HEAD changes so we can focus purely on transition-log behaviour.
    monkeypatch.setattr(subprocess, "run", _make_run({"head": "abc", "pr_updated": BASELINE_PR_TS}))

    config = WatchdogConfig(max_phase_visits=10, git_progress_check=True)
    daemon = _daemon(store, config=config)

    # Patch _latest_transition to raise — if the batch path is working, this
    # method should never be called during the patrol.
    def _boom(*a, **kw):
        raise AssertionError("_latest_transition should not be called when batch succeeds")

    monkeypatch.setattr(daemon, "_latest_transition", _boom)

    # Should complete without raising.
    await daemon._check_subtask_progress(datetime.now(UTC))

    # Baseline patrol with no prior head recorded counts as progress (no stall).
    health = daemon._subtask_state.get((TASK_ID, SUBTASK_ID))
    assert health is not None
    assert health.patrol_visits_without_progress == 0


@pytest.mark.asyncio
async def test_progress_patrol_falls_back_when_batch_fails(tmp_path, monkeypatch):
    """When batch_latest_transitions raises, the patrol falls back to
    per-subtask _latest_transition calls so results are still correct."""
    store = _seed_store(tmp_path)
    monkeypatch.setattr(subprocess, "run", _make_run({"head": "abc", "pr_updated": BASELINE_PR_TS}))

    config = WatchdogConfig(max_phase_visits=10, git_progress_check=True)
    daemon = _daemon(store, config=config)

    # Simulate batch failure.
    monkeypatch.setattr(store, "batch_latest_transitions", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db error")))

    # Should complete without raising — fallback per-subtask path fires.
    await daemon._check_subtask_progress(datetime.now(UTC))

    health = daemon._subtask_state.get((TASK_ID, SUBTASK_ID))
    assert health is not None  # patrol still ran


# ── _check_blocked_subtasks uses task_rows batch (T-06) ─────────────────────

def _owner_daemon_no_override(store, rest):
    """Daemon with no owner_id override — owner must come from task row."""
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=WatchdogConfig(),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        activity=None,
        state_store=store,
    )


@pytest.mark.asyncio
async def test_blocked_subtask_escalation_uses_task_row_batch(tmp_path):
    """_check_blocked_subtasks resolves owner+room from the pre-fetched
    _task_rows dict without per-subtask get_task calls."""
    from codeband.state import StateStore

    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo task", ROOM_ID, owner_id="owner-99")
    _drive_to_blocked(store)

    rest = _mock_rest()
    daemon = _owner_daemon_no_override(store, rest)

    # Patch get_task to raise — if the batch path works, it should never be called.
    original_get_task = store.get_task
    calls = {"n": 0}

    def _spy(task_id):
        calls["n"] += 1
        return original_get_task(task_id)

    store.get_task = _spy

    await daemon._check_blocked_subtasks(datetime.now(UTC))

    # Escalation fired (owner resolved from task_rows, not per-subtask get_task).
    rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
    # get_task should NOT have been called (batch path used task_rows instead).
    assert calls["n"] == 0, f"get_task was called {calls['n']} times; expected 0"
