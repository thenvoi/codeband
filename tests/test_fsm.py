"""Tests for the per-subtask FSM (RFC Workstream 2)."""

from __future__ import annotations

import sqlite3

import pytest

from codeband.state.fsm import (
    VALID_TRANSITIONS,
    InvalidTransitionError,
    transition,
)
from codeband.state.store import StateStore


@pytest.fixture
def store(tmp_path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(task_id="room-1", description="demo", room_id="room-1")
    return s


def _log_rows(store: StateStore, subtask_id: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM transition_log WHERE subtask_id = ? ORDER BY id",
            (subtask_id,),
        ).fetchall()
    finally:
        conn.close()


def test_valid_transitions_matches_rfc_table():
    assert VALID_TRANSITIONS == {
        ("planned", "conductor"): frozenset({"assigned"}),
        ("assigned", "coder"): frozenset({"in_progress"}),
        ("in_progress", "coder"): frozenset({"verify_pending", "blocked"}),
        # ``blocked`` is the coder's escalation escape once the verify-attempt
        # cap is hit (the ``cb-phase`` CLI drives it; the ``review_pending``
        # advance is gated by the verify gates at runtime).
        ("verify_pending", "coder"): frozenset({"review_pending", "blocked"}),
        ("review_pending", "reviewer"): frozenset({"review_passed", "review_failed"}),
        # ``blocked`` is the coder's escalation escape once the review-round cap
        # is hit (the ``in_progress`` rework is then gated at runtime).
        ("review_failed", "coder"): frozenset({"in_progress", "blocked"}),
        # Stage-2 merge edge: queue for integration (gated at runtime by the
        # SHA-pinned eligibility check) or send back for a rebase.
        ("review_passed", "mergemaster"): frozenset({"merge_pending", "needs_rebase"}),
        ("merge_pending", "mergemaster"): frozenset({"merged"}),
        ("needs_rebase", "coder"): frozenset({"in_progress"}),
    }


def test_ensure_subtask_auto_creates_row(store):
    assert store.get_subtask("st-1", "room-1") is None
    transition("st-1", "room-1", "assigned", caller_role="conductor", store=store)
    row = store.get_subtask("st-1", "room-1")
    assert row is not None
    assert row.task_id == "room-1"


def test_legal_transition_writes_state_and_one_log_row(store):
    transition("st-1", "room-1", "assigned", caller_role="conductor", store=store)

    assert store.get_subtask("st-1", "room-1").state == "assigned"
    rows = _log_rows(store, "st-1")
    assert len(rows) == 1
    assert rows[0]["from_state"] == "planned"
    assert rows[0]["to_state"] == "assigned"
    assert rows[0]["caller_role"] == "conductor"


def test_illegal_target_raises_and_leaves_state_unchanged(store):
    # planned → merged is not a legal edge.
    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", "merged", caller_role="conductor", store=store)

    assert store.get_subtask("st-1", "room-1").state == "planned"
    assert _log_rows(store, "st-1") == []


def test_wrong_caller_role_is_rejected(store):
    # planned → assigned is legal for conductor, not for coder.
    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", "assigned", caller_role="coder", store=store)

    assert store.get_subtask("st-1", "room-1").state == "planned"
    assert _log_rows(store, "st-1") == []


def test_full_happy_path(store):
    # The verify and review outcomes pin head_sha (as cb-phase does); the
    # merge_pending step must then pass the eligibility gate at the same SHA.
    steps = [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", "sha-1"),
        ("review_passed", "reviewer", "sha-1"),
        ("merge_pending", "mergemaster", "sha-1"),
        ("merged", "mergemaster", None),
    ]
    for new_state, role, sha in steps:
        transition("st-1", "room-1", new_state, caller_role=role, store=store,
                   head_sha=sha)

    assert store.get_subtask("st-1", "room-1").state == "merged"
    assert len(_log_rows(store, "st-1")) == len(steps)


def test_review_failed_loops_back_to_in_progress(store):
    for new_state, role in [
        ("assigned", "conductor"),
        ("in_progress", "coder"),
        ("verify_pending", "coder"),
        ("review_pending", "coder"),
        ("review_failed", "reviewer"),
        ("in_progress", "coder"),
    ]:
        transition("st-1", "room-1", new_state, caller_role=role, store=store)
    assert store.get_subtask("st-1", "room-1").state == "in_progress"


def test_conductor_can_abandon_any_non_terminal_state(store):
    transition("st-1", "room-1", "assigned", caller_role="conductor", store=store)
    transition("st-1", "room-1", "in_progress", caller_role="coder", store=store)
    transition("st-1", "room-1", "abandoned", caller_role="conductor", store=store)
    assert store.get_subtask("st-1", "room-1").state == "abandoned"


def test_no_transition_out_of_terminal_state(store):
    for new_state, role, sha in [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", "sha-1"),
        ("review_passed", "reviewer", "sha-1"),
        ("merge_pending", "mergemaster", "sha-1"),
        ("merged", "mergemaster", None),
    ]:
        transition("st-1", "room-1", new_state, caller_role=role, store=store,
                   head_sha=sha)

    # merged is terminal — even the conductor cannot abandon it.
    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", "abandoned", caller_role="conductor", store=store)
    assert store.get_subtask("st-1", "room-1").state == "merged"
