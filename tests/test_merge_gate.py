"""Tests for the Stage-2 merge edge: SHA-pinned eligibility gate,
``needs_rebase`` send-back, and task-level completion.

All deterministic — real SQLite, real FSM, no subprocesses. The verdict
records the eligibility check reads are the same ``transition_log`` rows the
FSM writes on the verify (``→ review_pending``) and review
(``→ review_passed``) outcomes, so the fixtures drive real transitions with
``head_sha`` pinned exactly as ``cb-phase`` does.
"""

from __future__ import annotations

import sqlite3

import pytest

from codeband.state.fsm import (
    InvalidTransitionError,
    MergeNotEligibleError,
    check_merge_eligibility,
    transition,
)
from codeband.state.store import StateStore


@pytest.fixture
def store(tmp_path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    # create_task leaves required_verdicts NULL — a pre-snapshot task, which
    # must resolve to the DEFAULT pair (never to ungated).
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


def _task_status(store: StateStore, task_id: str) -> str:
    return store.get_task(task_id).status


def _drive(store, sid, steps, task="room-1"):
    for new_state, role, sha in steps:
        transition(sid, task, new_state, caller_role=role, store=store,
                   head_sha=sha)


def _to_review_passed(store, sid, *, verify_sha, review_sha, task="room-1"):
    """Walk a subtask to ``review_passed``, pinning each outcome's SHA.

    ``verify_sha`` lands on the ``→ review_pending`` record (the verify leg),
    ``review_sha`` on the ``→ review_passed`` record (the review leg) — split
    so stale/NULL-pin scenarios can be staged per leg.
    """
    _drive(store, sid, [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", verify_sha),
        ("review_passed", "reviewer", review_sha),
    ], task=task)


def _register_ungated_task(store, task_id):
    """A tasks row with the explicit [] snapshot (allow_ungated_merge opt-out)."""
    store.register_task_atomic(
        task_id=task_id,
        description="ungated demo",
        room_id=task_id,
        owner_id="owner-1",
        required_verdicts=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# check_merge_eligibility — the SHA-pinned rules, fail-closed throughout
# ─────────────────────────────────────────────────────────────────────────────


def test_all_verdicts_pinned_at_matching_sha_is_eligible(store):
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")

    result = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert result.eligible is True
    assert result.reasons == []  # gated pass carries no reasons


def test_missing_verdict_is_named(store):
    # Only the verify leg has a record (subtask sits at review_pending);
    # the review leg is missing entirely.
    _drive(store, "st-1", [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", "sha-1"),
    ])

    result = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert result.eligible is False
    assert len(result.reasons) == 1
    assert result.reasons[0].startswith("missing_verdict review")


def test_stale_verdict_is_named_per_leg(store):
    # Verify pinned to sha-1, review pinned to sha-2 — whichever SHA the merge
    # targets, exactly the other leg is stale and is named with its pin.
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-2")

    at_sha1 = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert at_sha1.eligible is False
    assert len(at_sha1.reasons) == 1
    assert at_sha1.reasons[0].startswith("stale_verdict review")
    assert "sha-2" in at_sha1.reasons[0]

    at_sha2 = check_merge_eligibility("room-1", "st-1", "sha-2", store=store)
    assert at_sha2.eligible is False
    assert len(at_sha2.reasons) == 1
    assert at_sha2.reasons[0].startswith("stale_verdict verify")
    assert "sha-1" in at_sha2.reasons[0]


def test_null_pinned_verdict_matches_nothing(store):
    # Both outcome records exist but carry NULL head_sha (legacy / best-effort
    # git failure) — fail-closed: NULL matches no SHA, each leg is named.
    _to_review_passed(store, "st-1", verify_sha=None, review_sha=None)

    result = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert result.eligible is False
    assert len(result.reasons) == 2
    tags = sorted(r.split(":")[0] for r in result.reasons)
    assert tags == ["unpinned_verdict review", "unpinned_verdict verify"]


def test_null_snapshot_enforces_default_pair(store):
    # room-1's required_verdicts is NULL (create_task, pre-snapshot task).
    # NEVER ungated: with no verdict records at all, BOTH default legs are
    # reported missing.
    store.ensure_subtask("st-1", "room-1", state="review_passed")

    result = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert result.eligible is False
    assert len(result.reasons) == 2
    tags = sorted(r.split(":")[0] for r in result.reasons)
    assert tags == ["missing_verdict review", "missing_verdict verify"]


def test_empty_snapshot_is_vacuously_eligible_and_says_so(store):
    _register_ungated_task(store, "room-2")
    store.ensure_subtask("st-1", "room-2", state="review_passed")

    # No verdict records, not even a head_sha — vacuously eligible, and the
    # reasons say explicitly that this is the ungated opt-out, so a log
    # reader can never mistake it for a checked pass.
    result = check_merge_eligibility("room-2", "st-1", None, store=store)
    assert result.eligible is True
    assert len(result.reasons) == 1
    assert result.reasons[0].startswith("ungated_merge")
    assert "allow_ungated_merge" in result.reasons[0]


def test_missing_head_sha_fails_closed_on_gated_task(store):
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")

    result = check_merge_eligibility("room-1", "st-1", None, store=store)
    assert result.eligible is False
    assert len(result.reasons) == 1
    assert result.reasons[0].startswith("no_head_sha")


def test_unknown_task_fails_closed(store):
    result = check_merge_eligibility("no-such-task", "st-1", "sha-1", store=store)
    assert result.eligible is False
    assert result.reasons[0].startswith("unknown_task")


# ─────────────────────────────────────────────────────────────────────────────
# The gate inside the transition — review_passed → merge_pending
# ─────────────────────────────────────────────────────────────────────────────


def test_eligible_merge_transition_succeeds(store):
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")

    transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
               store=store, head_sha="sha-1")

    assert store.get_subtask("st-1", "room-1").state == "merge_pending"
    last = _log_rows(store, "st-1")[-1]
    assert (last["from_state"], last["to_state"]) == (
        "review_passed", "merge_pending",
    )
    assert last["head_sha"] == "sha-1"  # the merge is itself SHA-pinned


def test_ineligible_merge_transition_rejected_state_unchanged(store):
    # Verdicts pinned to sha-1; the mergemaster tries to merge sha-2 (e.g. a
    # commit pushed after review). Rejected loudly, nothing written.
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")
    rows_before = len(_log_rows(store, "st-1"))

    with pytest.raises(MergeNotEligibleError) as exc_info:
        transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
                   store=store, head_sha="sha-2")

    # Loud, machine-readable: the exception names every stale leg.
    message = str(exc_info.value)
    assert "stale_verdict verify" in message
    assert "stale_verdict review" in message
    assert exc_info.value.eligibility.eligible is False

    assert store.get_subtask("st-1", "room-1").state == "review_passed"
    assert len(_log_rows(store, "st-1")) == rows_before


def test_merge_transition_without_head_sha_rejected(store):
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")

    with pytest.raises(MergeNotEligibleError, match="no_head_sha"):
        transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
                   store=store)
    assert store.get_subtask("st-1", "room-1").state == "review_passed"


def test_merge_not_eligible_is_an_invalid_transition_error(store):
    # Callers that catch the broad FSM rejection keep working.
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")
    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
                   store=store, head_sha="other")


def test_ungated_task_merges_without_any_verdict_match(store):
    _register_ungated_task(store, "room-2")
    # Even the outcome records carry no SHA — the [] snapshot opts out of the
    # check entirely, so the merge transition passes (vacuously eligible).
    _to_review_passed(store, "st-1", verify_sha=None, review_sha=None,
                      task="room-2")

    transition("st-1", "room-2", "merge_pending", caller_role="mergemaster",
               store=store)
    assert store.get_subtask("st-1", "room-2").state == "merge_pending"


# ─────────────────────────────────────────────────────────────────────────────
# The needs_rebase send-back leg
# ─────────────────────────────────────────────────────────────────────────────


def test_needs_rebase_round_trip_re_earns_verdicts_at_new_sha(store):
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")

    # Mergemaster finds the branch stale → send back for a rebase.
    transition("st-1", "room-1", "needs_rebase", caller_role="mergemaster",
               store=store)
    assert store.get_subtask("st-1", "room-1").state == "needs_rebase"

    # Coder picks the rework up — the same return-to-coder state the
    # review-fail loop uses, and it does NOT count as a review round.
    transition("st-1", "room-1", "in_progress", caller_role="coder",
               store=store)
    row = store.get_subtask("st-1", "room-1")
    assert row.state == "in_progress"
    assert row.review_round == 0

    # Re-earn both verdicts at the rebased commit (sha-2). A SHA no verdict
    # ever blessed (sha-3) is still rejected — the gate pins verdicts to
    # exact commits; which commit is fresh enough to merge stays the
    # mergemaster's needs_rebase call.
    _drive(store, "st-1", [
        ("verify_pending", "coder", None),
        ("review_pending", "coder", "sha-2"),
        ("review_passed", "reviewer", "sha-2"),
    ])
    with pytest.raises(MergeNotEligibleError):
        transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
                   store=store, head_sha="sha-3")

    # The re-earned pair at sha-2 merges.
    transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
               store=store, head_sha="sha-2")
    assert store.get_subtask("st-1", "room-1").state == "merge_pending"


@pytest.mark.parametrize(
    "new_state, role",
    [
        ("merge_pending", "mergemaster"),  # no shortcut back into the queue
        ("review_pending", "coder"),       # cannot skip the rework walk
        ("review_passed", "reviewer"),     # cannot re-approve in place
        ("merged", "mergemaster"),         # certainly cannot merge from here
    ],
)
def test_illegal_edges_out_of_needs_rebase_rejected(store, new_state, role):
    _to_review_passed(store, "st-1", verify_sha="sha-1", review_sha="sha-1")
    transition("st-1", "room-1", "needs_rebase", caller_role="mergemaster",
               store=store)
    rows_before = len(_log_rows(store, "st-1"))

    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", new_state, caller_role=role, store=store,
                   head_sha="sha-1")
    assert store.get_subtask("st-1", "room-1").state == "needs_rebase"
    assert len(_log_rows(store, "st-1")) == rows_before


def test_illegal_edges_into_needs_rebase_rejected(store):
    # Only the mergemaster, and only from review_passed, may send back.
    _drive(store, "st-1", [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
    ])
    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", "needs_rebase", caller_role="mergemaster",
                   store=store)

    _to_review_passed(store, "st-2", verify_sha="sha-1", review_sha="sha-1")
    with pytest.raises(InvalidTransitionError):
        transition("st-2", "room-1", "needs_rebase", caller_role="coder",
                   store=store)


def test_in_progress_cannot_jump_to_merge_pending(store):
    _drive(store, "st-1", [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
    ])
    with pytest.raises(InvalidTransitionError):
        transition("st-1", "room-1", "merge_pending", caller_role="mergemaster",
                   store=store, head_sha="sha-1")
    assert store.get_subtask("st-1", "room-1").state == "in_progress"


def test_wildcards_apply_to_needs_rebase(store):
    # needs_rebase is non-terminal — the conductor-abandon and watchdog-block
    # cross-cutting rules apply to it like any other in-flight state.
    for sid, target, role in [
        ("st-1", "abandoned", "conductor"),
        ("st-2", "blocked", "watchdog"),
    ]:
        _to_review_passed(store, sid, verify_sha="sha-1", review_sha="sha-1")
        transition(sid, "room-1", "needs_rebase", caller_role="mergemaster",
                   store=store)
        transition(sid, "room-1", target, caller_role=role, store=store)
        assert store.get_subtask(sid, "room-1").state == target


# ─────────────────────────────────────────────────────────────────────────────
# Task-level completion — last merged subtask promotes the task
# ─────────────────────────────────────────────────────────────────────────────


def _merge(store, sid, sha, task="room-1"):
    _to_review_passed(store, sid, verify_sha=sha, review_sha=sha, task=task)
    transition(sid, task, "merge_pending", caller_role="mergemaster",
               store=store, head_sha=sha)
    transition(sid, task, "merged", caller_role="mergemaster", store=store)


def test_last_merged_subtask_promotes_task_partial_does_not(store):
    store.ensure_subtask("st-1", "room-1")
    store.ensure_subtask("st-2", "room-1")

    _merge(store, "st-1", "sha-a")
    assert _task_status(store, "room-1") == "active"  # partial → no promotion

    _merge(store, "st-2", "sha-b")
    assert _task_status(store, "room-1") == "completed"


def test_superseded_task_is_never_promoted(store):
    store.create_task(task_id="room-old", description="old", room_id="room-old",
                      status="superseded")
    _merge(store, "st-1", "sha-a", task="room-old")

    # All subtasks merged, but the task was superseded — its status is owned
    # by registration semantics and must not be overwritten.
    assert _task_status(store, "room-old") == "superseded"


def test_abandoned_sibling_blocks_promotion(store):
    # Strict rule: 'completed' means every subtask MERGED. A task whose plan
    # was partially abandoned never reads as completed (Stage-2 chunk 2b may
    # revisit; pinned here so any change is deliberate).
    store.ensure_subtask("st-1", "room-1")
    transition("st-1", "room-1", "abandoned", caller_role="conductor",
               store=store)
    _merge(store, "st-2", "sha-a")

    assert _task_status(store, "room-1") == "active"
