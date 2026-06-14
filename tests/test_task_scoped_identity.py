"""Task-scoped subtask identity — composite key ``(task_id, subtask_id)``.

Planners number subtasks ``st-1``, ``st-2``, … fresh per plan, so a bare
``subtask_id`` PRIMARY KEY guaranteed cross-task collisions in any reused
``orchestration.db``. Found live in the Task 2 shakedown: a stale
``review_passed`` st-1 from a prior task shadowed the new task's st-1 and
rejected ``cb-phase verify`` at entry-state validation.

These tests mirror the existing store / handoff / watchdog test patterns:
LLM-free, real sqlite via ``StateStore``, mocked REST for the watchdog.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.cli import handoff
from codeband.config import WatchdogConfig
from codeband.state.fsm import transition
from codeband.state.store import StateStore

TASK_A = "room-aaaa"
TASK_B = "room-bbbb"


@pytest.fixture
def store(tmp_path) -> StateStore:
    """One shared DB with two tasks — the reused-orchestration.db scenario."""
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(TASK_A, "task A", TASK_A, owner_id="owner-a")
    s.create_task(TASK_B, "task B", TASK_B, owner_id="owner-b")
    return s


def _drive_to_review_passed(store: StateStore, task_id: str) -> None:
    for new_state, role in [
        ("assigned", "conductor"),
        ("in_progress", "coder"),
        ("verify_pending", "coder"),
        ("review_pending", "coder"),
        ("review_passed", "reviewer"),
    ]:
        transition("st-1", task_id, new_state, caller_role=role, store=store)


# ── (a) same subtask id, two tasks — fully independent state ─────────────────

def test_same_subtask_id_in_two_tasks_is_independent(store):
    transition("st-1", TASK_A, "assigned", caller_role="conductor", store=store)
    transition("st-1", TASK_B, "assigned", caller_role="conductor", store=store)
    transition("st-1", TASK_A, "in_progress", caller_role="coder", store=store)

    # Advancing task A's st-1 does not touch task B's st-1.
    assert store.get_subtask("st-1", TASK_A).state == "in_progress"
    assert store.get_subtask("st-1", TASK_B).state == "assigned"

    # Counters are independent too.
    assert store.increment_verify_attempts("st-1", TASK_A) == 1
    assert store.get_subtask("st-1", TASK_A).verify_attempts == 1
    assert store.get_subtask("st-1", TASK_B).verify_attempts == 0


def test_ensure_subtask_creates_one_row_per_task(store):
    store.ensure_subtask("st-1", TASK_A, state="in_progress")
    store.ensure_subtask("st-1", TASK_B, state="planned")

    with sqlite3.connect(store.db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM subtask_states WHERE subtask_id = ?", ("st-1",)
        ).fetchone()
    assert count == 2
    assert store.get_subtask("st-1", TASK_A).state == "in_progress"
    assert store.get_subtask("st-1", TASK_B).state == "planned"


# ── (b) THE EXACT TASK-2 REPRO ───────────────────────────────────────────────

def test_task2_repro_stale_review_passed_st1_does_not_shadow_new_task(store):
    """Task A's st-1 rests at ``review_passed``; task B then creates its own
    st-1. ``cb-phase verify`` entry-state validation on task B's st-1 must
    succeed (the walk lands it at ``verify_pending``) and task A's row must be
    untouched — exactly the case that rejected with "not a valid entry state"
    when the lookup was unscoped.
    """
    _drive_to_review_passed(store, TASK_A)
    store.ensure_subtask("st-1", TASK_B)  # the new task's fresh st-1

    walk_result = handoff._walk_to_verify_pending(
        "st-1", TASK_B, store, max_review_rounds=3,
    )

    assert walk_result is None  # entry-state validation succeeded
    assert store.get_subtask("st-1", TASK_B).state == "verify_pending"
    assert store.get_subtask("st-1", TASK_A).state == "review_passed"  # untouched


def test_task2_repro_through_cb_phase_main(store, monkeypatch):
    """The same repro end-to-end through ``cb-phase verify`` with all gates
    passing: task B's st-1 advances through verify_pending → review_pending
    while task A's stale review_passed st-1 stays put.
    """
    _drive_to_review_passed(store, TASK_A)

    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, s, task_arg: (TASK_B, None),
    )
    monkeypatch.setattr(handoff, "_verify_command", lambda project_dir, worktree: "true")
    monkeypatch.setattr(handoff, "_max_verify_attempts", lambda project_dir: 20)
    monkeypatch.setattr(handoff, "_max_review_rounds", lambda project_dir: 3)
    monkeypatch.setattr(handoff, "_uncommitted_files", lambda worktree: [])
    monkeypatch.setattr(handoff, "_current_branch", lambda worktree: "feat-x")
    # PR-pinned verify outcomes: the PR head must match the worktree HEAD
    # (and the PR head branch the worktree branch) — the one gh snapshot and
    # the git seam stubbed to agree here.
    monkeypatch.setattr(handoff, "_git_head", lambda worktree: "cafe1234")
    monkeypatch.setattr(
        handoff, "_verify_pr_snapshot",
        lambda project_dir, pr: {
            "state": "OPEN", "headRefName": "feat-x", "headRefOid": "cafe1234",
        },
    )

    assert handoff.main(["verify", "st-1", "--pr", "42"]) == 0
    assert store.get_subtask("st-1", TASK_B).state == "review_pending"
    assert store.get_subtask("st-1", TASK_A).state == "review_passed"


# ── (c) transition_log rows are distinguishable by task_id ───────────────────

def test_transition_log_rows_distinguishable_by_task(store):
    transition("st-1", TASK_A, "assigned", caller_role="conductor", store=store)
    transition("st-1", TASK_B, "assigned", caller_role="conductor", store=store)
    transition("st-1", TASK_B, "in_progress", caller_role="coder", store=store)

    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT task_id, to_state FROM transition_log "
            "WHERE subtask_id = ? ORDER BY id",
            ("st-1",),
        ).fetchall()
    finally:
        conn.close()

    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r["to_state"])
    assert by_task == {TASK_A: ["assigned"], TASK_B: ["assigned", "in_progress"]}


# ── (d) watchdog: one escalation per (task, subtask) ─────────────────────────

def _mock_rest():
    rest = MagicMock()
    rest.agent_api_messages = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    return rest


@pytest.mark.asyncio
async def test_blocked_st1_in_two_tasks_escalates_once_per_task(store):
    """Blocked st-1 in task A and blocked st-1 in task B → exactly two owner
    escalations, one per task owner; escalate-once still holds within each
    task across repeated patrols.
    """
    from codeband.agents.watchdog import WatchdogDaemon

    for task_id in (TASK_A, TASK_B):
        transition("st-1", task_id, "assigned", caller_role="conductor", store=store)
        transition("st-1", task_id, "in_progress", caller_role="coder", store=store)
        transition("st-1", task_id, "blocked", caller_role="coder",
                   reason=f"cap reached in {task_id}", store=store)

    rest = _mock_rest()
    daemon = WatchdogDaemon(
        config=WatchdogConfig(),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-cond",
        state_store=store,
    )

    now = datetime.now(UTC)
    await daemon._check_blocked_subtasks(now)
    await daemon._check_blocked_subtasks(now)  # escalate-once: no re-fire

    calls = rest.agent_api_messages.create_agent_chat_message.call_args_list
    assert len(calls) == 2  # one per task, not one global st-1
    mentioned = {m.id for c in calls for m in c.kwargs["message"].mentions}
    assert mentioned == {"owner-a", "owner-b"}  # each task's own owner
    # Each message carries its own task's durable blocked reason.
    contents = {c.kwargs["message"].content for c in calls}
    assert any(f"cap reached in {TASK_A}" in c for c in contents)
    assert any(f"cap reached in {TASK_B}" in c for c in contents)
    assert daemon._owner_escalated == {(TASK_A, "st-1"), (TASK_B, "st-1")}


@pytest.mark.asyncio
async def test_progress_tracking_keyed_per_task(store, monkeypatch):
    """The mechanical-progress health map tracks each task's st-1 separately —
    progress on task A's st-1 must not reset task B's stall counter.
    """
    import subprocess as _subprocess

    from codeband.agents.watchdog import WatchdogDaemon

    for task_id in (TASK_A, TASK_B):
        store.ensure_subtask("st-1", task_id, state="in_progress")

    # No git branch / PR metadata: the transition log is the only signal.
    monkeypatch.setattr(
        _subprocess, "run",
        lambda *a, **k: _subprocess.CompletedProcess(a, 1, stdout="", stderr=""),
    )

    daemon = WatchdogDaemon(
        config=WatchdogConfig(max_phase_visits=10),
        rest_client=_mock_rest(),
        agent_id="agent-wd",
        conductor_id="agent-cond",
        state_store=store,
    )

    now = datetime.now(UTC)
    await daemon._check_subtask_progress(now)  # baseline → both at 1 (no signals)
    await daemon._check_subtask_progress(now)  # both at 2

    # Progress on task A's st-1 only (a new transition_log row for TASK_A).
    transition("st-1", TASK_A, "verify_pending", caller_role="coder", store=store)
    await daemon._check_subtask_progress(now)

    health_a = daemon._subtask_state[(TASK_A, "st-1")]
    health_b = daemon._subtask_state[(TASK_B, "st-1")]
    assert health_a.patrol_visits_without_progress == 0  # reset by progress
    assert health_b.patrol_visits_without_progress == 3  # still stalling
