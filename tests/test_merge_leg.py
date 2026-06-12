"""Tests for the ``cb-phase merge`` execution leg (Stage-2 chunk 2b).

All deterministic: real SQLite + real FSM; every external interaction
(``gh pr view`` / ``gh pr merge`` via :func:`merge._pr_snapshot` /
:func:`merge._gh_merge`, the Band approval-request send via
:func:`merge._send_approval_request`) is monkeypatched at the module seam,
mirroring ``test_handoff.py``. The verdict records the gate reads are real
``transition_log`` rows driven through the FSM with pinned SHAs, exactly as
``cb-phase`` writes them.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeband.cli import handoff, merge
from codeband.config import AgentsConfig
from codeband.state.fsm import transition
from codeband.state.registration import (
    DEFAULT_MERGE_APPROVAL,
    register_task,
    resolve_merge_approval,
)
from codeband.state.store import StateStore

TASK = "room-1"
SHA = "sha-1"


def _drive_to_review_passed(
    store, sid, *, verify_sha=SHA, review_sha=SHA, task=TASK,
):
    for new_state, role, sha in [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", verify_sha),
        ("review_passed", "reviewer", review_sha),
    ]:
        transition(sid, task, new_state, caller_role=role, store=store, head_sha=sha)


def _log_rows(store, subtask_id, to_state=None):
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM transition_log WHERE subtask_id = ?"
        params = [subtask_id]
        if to_state is not None:
            sql += " AND to_state = ?"
            params.append(to_state)
        return conn.execute(sql + " ORDER BY id", params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path) -> StateStore:
    """A registered (owner-approved, fully gated) task with st-1 at review_passed."""
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.register_task_atomic(
        task_id=TASK, description="demo", room_id=TASK,
        owner_id="owner-1", owner_handle="yoni",
        required_verdicts=["verify", "review"], merge_approval="owner",
    )
    _drive_to_review_passed(s, "st-1")
    return s


@pytest.fixture
def env(monkeypatch, store):
    """Wire every external seam to controllable fakes (happy defaults)."""
    pr = {
        "state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": SHA,
        "headRefName": "codeband/coder-claude-0/feat-x",
    }
    gh_merges: list[int] = []
    gh_merge_pins: list[str | None] = []
    sends: list[tuple] = []
    branch_deletes: list[str | None] = []

    monkeypatch.setattr(merge, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        merge, "_resolve_task_id",
        lambda project_dir, store, task_arg: (TASK, None),
    )
    snapshot_repos: list[str | None] = []

    def _fake_snapshot(pr_number, cwd, repo=None):
        snapshot_repos.append(repo)
        return dict(pr)

    monkeypatch.setattr(merge, "_pr_snapshot", _fake_snapshot)
    # record_approval_grant derives its --repo slug from config repo.url —
    # stub the config load so no codeband.yaml is needed on disk.
    monkeypatch.setattr(
        merge, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
            agents=SimpleNamespace(max_rebase_rounds=3),
        ),
    )

    gh_merge_repos: list[str | None] = []

    def _fake_merge(pr_number, cwd, pending_sha, repo=None):
        gh_merges.append(pr_number)
        gh_merge_pins.append(pending_sha)
        gh_merge_repos.append(repo)
        return 0, "merged ok"

    monkeypatch.setattr(merge, "_gh_merge", _fake_merge)

    def _fake_send(project_dir, task, subtask_id, pr_number, head_sha, approver_spec):
        sends.append((subtask_id, pr_number, head_sha, approver_spec))

    monkeypatch.setattr(merge, "_send_approval_request", _fake_send)

    def _fake_delete(snapshot, cwd):
        branch_deletes.append((snapshot or {}).get("headRefName"))

    monkeypatch.setattr(merge, "_delete_remote_branch", _fake_delete)
    return SimpleNamespace(
        store=store, pr=pr, gh_merges=gh_merges, gh_merge_pins=gh_merge_pins,
        gh_merge_repos=gh_merge_repos, sends=sends,
        branch_deletes=branch_deletes, snapshot_repos=snapshot_repos,
    )


def _grant(store, sid="st-1", sha=SHA):
    store.record_merge_approval(sid, TASK, approved_by="owner", approved_sha=sha)


def _run(*argv):
    return handoff.main(["merge", *argv] if argv else ["merge", "st-1", "--pr", "42"])


# ─────────────────────────────────────────────────────────────────────────────
# Happy path + approval routing
# ─────────────────────────────────────────────────────────────────────────────


def test_happy_path_preapproved_merges_and_completes_task(env):
    _grant(env.store)

    assert _run() == 0
    assert env.store.get_subtask("st-1", TASK).state == "merged"
    assert env.gh_merges == [42]
    # The execution is pinned to the queued (approved) SHA…
    assert env.gh_merge_pins == [SHA]
    # …and the remote branch is cleaned up after the merge is recorded.
    assert env.branch_deletes == ["codeband/coder-claude-0/feat-x"]
    # --pr was persisted for argument-less reconcile re-runs.
    assert env.store.get_subtask("st-1", TASK).pr_number == 42
    # The queue + landing transitions are both recorded, pinned to the SHA.
    assert [r["head_sha"] for r in _log_rows(env.store, "st-1", "merge_pending")] == [SHA]
    assert [r["head_sha"] for r in _log_rows(env.store, "st-1", "merged")] == [SHA]
    # Last subtask merged → the 2a task-level promotion fires on its own.
    assert env.store.get_task(TASK).status == "completed"


def test_merge_leg_snapshot_and_merge_are_repo_pinned(env):
    """Every gh PR query in the merge leg carries --repo <slug> from config
    repo.url [S9-7] — completing the gate family's repo pinning (verify and
    the grant half already do this). Without it, a same-numbered PR in
    whatever repo the worktree cwd happens to be in could be
    reconciled/merged."""
    _grant(env.store)
    assert _run() == 0
    assert env.snapshot_repos == ["acme/widgets"]
    assert env.gh_merge_repos == ["acme/widgets"]


def test_gh_merge_argv_carries_repo_flag(monkeypatch):
    """The real _gh_merge passes --repo <slug> (and keeps --match-head-commit)."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(merge.subprocess, "run", fake_run)
    code, _ = merge._gh_merge(42, Path("."), "sha-1", repo="acme/widgets")

    assert code == 0
    cmd = captured["cmd"]
    assert cmd[:5] == ["gh", "pr", "merge", "42", "--merge"]
    assert cmd[cmd.index("--match-head-commit") + 1] == "sha-1"
    assert cmd[cmd.index("--repo") + 1] == "acme/widgets"


def test_unresolvable_slug_rejects_pr_query_failed(env, monkeypatch, capsys):
    """An underivable repo slug is an infra failure like a failed snapshot:
    fail closed before any PR query, no transition recorded."""
    _grant(env.store)
    monkeypatch.setattr(
        merge, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://gitlab.example.com/g/p.git"),
        ),
    )

    assert _run() == merge.EXIT_PR_QUERY_FAILED
    assert "[pr_query_failed]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "review_passed"
    assert env.snapshot_repos == []
    assert env.gh_merges == []


def test_approval_pending_rests_requests_once_then_executes(env):
    # 1st invocation: rests at merge_pending, one request to the owner, no merge.
    assert _run() == 0
    sub = env.store.get_subtask("st-1", TASK)
    assert sub.state == "merge_pending"
    assert env.sends == [("st-1", 42, SHA, "owner")]
    assert env.gh_merges == []
    assert sub.merge_approval_requested_sha == SHA  # send-once marker burned

    # 2nd invocation, still unapproved: no re-send, still resting, exit 0.
    assert _run() == 0
    assert len(env.sends) == 1
    assert env.gh_merges == []
    assert env.store.get_subtask("st-1", TASK).state == "merge_pending"

    # Approval lands → the next invocation executes.
    _grant(env.store)
    assert _run() == 0
    assert env.store.get_subtask("st-1", TASK).state == "merged"
    assert env.gh_merges == [42]
    assert len(env.sends) == 1  # never re-requested


def test_send_failure_leaves_marker_unburned_and_retries(env, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise RuntimeError("band unreachable")

    monkeypatch.setattr(merge, "_send_approval_request", _boom)
    assert _run() == 0  # resting at merge_pending is legitimate either way
    sub = env.store.get_subtask("st-1", TASK)
    assert sub.state == "merge_pending"
    assert sub.merge_approval_requested_sha is None  # marker-after-send
    assert "request send FAILED" in capsys.readouterr().err

    # Send works again → the retry actually sends and burns the marker.
    sends: list[tuple] = []
    monkeypatch.setattr(
        merge, "_send_approval_request",
        lambda *a: sends.append(a),
    )
    assert _run() == 0
    assert len(sends) == 1
    assert env.store.get_subtask("st-1", TASK).merge_approval_requested_sha == SHA


def test_stale_grant_from_earlier_round_does_not_authorize(env, capsys):
    # Grant pinned to a different SHA than the queued one (e.g. a pre-rebase
    # grant): not granted — the leg rests and reports the stale pin.
    _grant(env.store, sha="sha-0")
    assert _run() == 0
    assert env.store.get_subtask("st-1", TASK).state == "merge_pending"
    assert env.gh_merges == []
    assert "re-approval required" in capsys.readouterr().err
    assert len(env.sends) == 1  # re-requested for the new SHA


# ─────────────────────────────────────────────────────────────────────────────
# Gate rejection, SHA drift, mergeability, failure classification
# ─────────────────────────────────────────────────────────────────────────────


def test_ineligible_transition_exits_nonzero_with_reasons(env, capsys):
    _drive_to_review_passed(env.store, "st-2", verify_sha="sha-0")  # stale verify

    assert handoff.main(["merge", "st-2", "--pr", "43"]) == merge.EXIT_NOT_ELIGIBLE
    err = capsys.readouterr().err
    assert "REJECTED [not_eligible]" in err
    assert "stale_verdict verify" in err  # 2a's reasons echoed verbatim
    assert env.store.get_subtask("st-2", TASK).state == "review_passed"
    assert env.sends == []  # no approval request for an ineligible merge
    assert env.gh_merges == []  # and no merge attempt


def test_sha_moved_while_queued_goes_needs_rebase(env, capsys):
    assert _run() == 0  # queue at SHA (awaiting approval)
    _grant(env.store)
    env.pr["headRefOid"] = "sha-2"  # someone pushed while waiting

    assert _run() == merge.EXIT_NEEDS_REBASE
    err = capsys.readouterr().err
    assert "REJECTED [sha_moved]" in err
    assert SHA in err and "sha-2" in err  # names old and new SHA
    assert env.store.get_subtask("st-1", TASK).state == "needs_rebase"
    assert env.gh_merges == []  # fail-closed, no execution


def test_sha_moved_rechecked_before_approval_no_request_no_marker_burn(env, capsys):
    """The execution-time SHA re-check runs BEFORE the approval gate: a head
    that moved while queued goes needs_rebase without any grant evaluation or
    approval-request send — so the send-once marker is never burned for the
    new SHA and the subtask cannot be stranded permanently un-approvable."""
    assert _run() == 0  # queue at SHA; one request sent for SHA
    assert len(env.sends) == 1
    env.pr["headRefOid"] = "sha-2"  # someone pushed while resting

    assert _run() == merge.EXIT_NEEDS_REBASE
    assert "REJECTED [sha_moved]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "needs_rebase"
    assert len(env.sends) == 1  # NO approval request for the sha-2 round
    # The marker still names the original SHA — not burned for sha-2.
    assert env.store.get_subtask("st-1", TASK).merge_approval_requested_sha == SHA
    assert env.gh_merges == []


def test_conflicted_pr_goes_needs_rebase_without_merge_attempt(env, capsys):
    _grant(env.store)
    env.pr["mergeable"] = "CONFLICTING"

    assert _run() == merge.EXIT_NEEDS_REBASE
    assert "REJECTED [conflicted]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "needs_rebase"
    assert env.gh_merges == []


def test_residual_merge_failure_blocks_once_with_reason(env, monkeypatch, capsys):
    _grant(env.store)
    attempts: list[int] = []

    def _failing_merge(pr_number, cwd, pending_sha, repo=None):
        attempts.append(pr_number)
        return 1, "GraphQL: 2 of 3 required status checks are expected"

    monkeypatch.setattr(merge, "_gh_merge", _failing_merge)

    assert _run() == merge.EXIT_MERGE_FAILED
    assert "BLOCKED [merge_failed]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "blocked"
    blocked = _log_rows(env.store, "st-1", "blocked")
    assert len(blocked) == 1  # exactly one escalation trigger
    assert "required status checks" in blocked[0]["reason"]

    # Re-failure does not re-escalate: a blocked subtask is not a valid entry
    # state, so re-invocation writes nothing and never re-runs gh. (The single
    # owner escalation itself is the watchdog's blocked-subtask patrol —
    # escalate-once via its durable trigger, see test_watchdog_upgrade.py.)
    assert _run() == 1
    assert "not a valid entry state" in capsys.readouterr().err
    assert len(_log_rows(env.store, "st-1", "blocked")) == 1
    assert attempts == [42]


def test_execution_time_conflict_classified_as_needs_rebase(env, monkeypatch):
    _grant(env.store)
    monkeypatch.setattr(
        merge, "_gh_merge",
        lambda pr_number, cwd, pending_sha, repo=None: (
            1, "Pull request #42 is not mergeable: the merge commit cannot "
               "be cleanly created",
        ),
    )

    assert _run() == merge.EXIT_NEEDS_REBASE
    assert env.store.get_subtask("st-1", TASK).state == "needs_rebase"
    assert _log_rows(env.store, "st-1", "blocked") == []


# ─────────────────────────────────────────────────────────────────────────────
# Effect-verified failure classification (re-snapshot before classifying)
# ─────────────────────────────────────────────────────────────────────────────


def _snapshot_sequence(monkeypatch, *snaps):
    """Make _pr_snapshot return each snapshot in turn (initial, post-failure)."""
    remaining = list(snaps)
    monkeypatch.setattr(
        merge, "_pr_snapshot",
        lambda pr_number, cwd, repo=None: dict(remaining.pop(0)) if remaining else None,
    )


def test_gh_failure_with_pr_actually_merged_records_merged(env, monkeypatch, capsys):
    """gh exiting non-zero after the merge landed (timeout after the API call)
    must record merged, not blocked — the exact misclassification that
    produced Scenario A's unrecoverable blocked."""
    _grant(env.store)
    monkeypatch.setattr(
        merge, "_gh_merge",
        lambda pr, cwd, sha, repo=None: (1, "Post https://api.github.com: i/o timeout"),
    )
    _snapshot_sequence(monkeypatch, env.pr, {**env.pr, "state": "MERGED"})

    assert _run() == 0
    assert env.store.get_subtask("st-1", TASK).state == "merged"
    rows = _log_rows(env.store, "st-1", "merged")
    assert len(rows) == 1
    assert "post-failure reconcile" in rows[0]["reason"]
    assert "gh exited 1" in rows[0]["reason"]
    assert _log_rows(env.store, "st-1", "blocked") == []
    assert "merge landed" in capsys.readouterr().out
    # Task-level completion promotion fires off the recorded merge.
    assert env.store.get_task(TASK).status == "completed"


def test_gh_failure_with_moved_head_goes_needs_rebase(env, monkeypatch):
    """A --match-head-commit rejection shows up as a gh failure with a moved
    head in the re-snapshot — classified needs_rebase, never blocked."""
    _grant(env.store)
    monkeypatch.setattr(
        merge, "_gh_merge",
        lambda pr, cwd, sha, repo=None: (1, "head commit does not match expected SHA"),
    )
    _snapshot_sequence(monkeypatch, env.pr, {**env.pr, "headRefOid": "sha-2"})

    assert _run() == merge.EXIT_NEEDS_REBASE
    assert env.store.get_subtask("st-1", TASK).state == "needs_rebase"
    assert _log_rows(env.store, "st-1", "blocked") == []


def test_gh_failure_with_structured_conflicting_field_goes_needs_rebase(
    env, monkeypatch,
):
    """The re-snapshot's structured mergeable field classifies a conflict even
    when gh's error text matches no conflict regex."""
    _grant(env.store)
    monkeypatch.setattr(
        merge, "_gh_merge",
        lambda pr, cwd, sha, repo=None: (1, "GraphQL: something opaque went wrong"),
    )
    _snapshot_sequence(monkeypatch, env.pr, {**env.pr, "mergeable": "CONFLICTING"})

    assert _run() == merge.EXIT_NEEDS_REBASE
    assert env.store.get_subtask("st-1", TASK).state == "needs_rebase"
    assert _log_rows(env.store, "st-1", "blocked") == []


def test_gh_failure_with_unavailable_resnapshot_classifies_nothing(
    env, monkeypatch, capsys,
):
    """No post-failure snapshot → no classification: the subtask rests at
    merge_pending for the next reconcile instead of risking a phantom blocked
    over a PR that actually merged."""
    _grant(env.store)
    monkeypatch.setattr(
        merge, "_gh_merge", lambda pr, cwd, sha, repo=None: (1, "network is down"),
    )
    _snapshot_sequence(monkeypatch, env.pr)  # second call → None

    assert _run() == merge.EXIT_PR_QUERY_FAILED
    assert "cannot be classified" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "merge_pending"
    assert _log_rows(env.store, "st-1", "blocked") == []
    assert _log_rows(env.store, "st-1", "needs_rebase") == []


def test_closed_pr_blocks_before_approval(env, capsys):
    env.pr["state"] = "CLOSED"

    assert _run() == merge.EXIT_MERGE_FAILED
    assert "CLOSED" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "blocked"
    assert env.sends == []  # never bothers the approver about a dead PR
    assert env.gh_merges == []


# ─────────────────────────────────────────────────────────────────────────────
# Reconcile (crash recovery) + PR-number derivation
# ─────────────────────────────────────────────────────────────────────────────


def test_reconcile_already_merged_records_and_exits_zero(env, capsys):
    assert _run() == 0  # queue + persist --pr 42; rests awaiting approval
    env.pr["state"] = "MERGED"  # the merge landed but recording crashed

    # Argument-less re-invocation: PR number read back from the subtask row.
    assert handoff.main(["merge", "st-1"]) == 0
    assert "reconciled" in capsys.readouterr().out
    assert env.store.get_subtask("st-1", TASK).state == "merged"
    assert env.gh_merges == []  # recorded, never re-executed
    assert env.store.get_task(TASK).status == "completed"


def test_reconcile_not_merged_proceeds_per_approval_state(env):
    assert _run() == 0  # queue; request sent; resting
    assert len(env.sends) == 1

    # Still OPEN + unapproved: argument-less re-run keeps resting, no re-send.
    assert handoff.main(["merge", "st-1"]) == 0
    assert len(env.sends) == 1
    assert env.store.get_subtask("st-1", TASK).state == "merge_pending"

    # Approved: the same argument-less re-run executes.
    _grant(env.store)
    assert handoff.main(["merge", "st-1"]) == 0
    assert env.store.get_subtask("st-1", TASK).state == "merged"
    assert env.gh_merges == [42]


def test_first_invocation_without_pr_number_is_rejected(env, capsys):
    assert handoff.main(["merge", "st-1"]) == merge.EXIT_NO_PR_NUMBER
    assert "REJECTED [no_pr_number]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "review_passed"
    assert env.sends == [] and env.gh_merges == []


def test_rebind_to_a_different_pr_is_rejected(env, capsys):
    """A queued subtask bound to PR A, re-invoked with --pr of an already-
    MERGED PR B, must NOT record merged — rebinding would route the reconcile
    branch through the wrong PR's state (the phantom-merged path)."""
    assert _run() == 0  # binds st-1 to PR 42; rests at merge_pending
    env.pr["state"] = "MERGED"  # PR 99 (the rebind target) is already merged

    assert handoff.main(["merge", "st-1", "--pr", "99"]) == merge.EXIT_PR_REBIND
    err = capsys.readouterr().err
    assert "REJECTED [pr_rebind]" in err
    assert "already bound to PR #42" in err and "#99" in err

    sub = env.store.get_subtask("st-1", TASK)
    assert sub.state == "merge_pending"  # NOT merged
    assert sub.pr_number == 42  # binding unchanged
    assert _log_rows(env.store, "st-1", "merged") == []
    assert env.gh_merges == []


def test_rebind_guard_rejects_from_review_passed_too(env, capsys):
    """The guard is state-independent: once bound, only the bound PR is valid."""
    env.store.set_pr_number("st-1", TASK, 41)  # bound before any queueing

    assert handoff.main(["merge", "st-1", "--pr", "42"]) == merge.EXIT_PR_REBIND
    assert "REJECTED [pr_rebind]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "review_passed"
    assert env.store.get_subtask("st-1", TASK).pr_number == 41


def test_same_pr_reinvocation_is_idempotent(env):
    assert _run() == 0  # binds + queues
    assert _run() == 0  # same --pr again: proceeds (rests, unapproved)
    assert env.store.get_subtask("st-1", TASK).pr_number == 42
    assert env.store.get_subtask("st-1", TASK).state == "merge_pending"


def test_invalid_entry_state_is_a_clear_error(env, capsys):
    assert handoff.main(["merge", "st-9", "--pr", "44"]) == 1
    assert "not a valid entry state" in capsys.readouterr().err


# ─────────────────────────────────────────────────────────────────────────────
# Ungated opt-out: the gate is vacuous, the approval flow is not
# ─────────────────────────────────────────────────────────────────────────────


def test_ungated_task_merges_vacuously_but_approval_still_applies(
    tmp_path, monkeypatch,
):
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.register_task_atomic(
        task_id=TASK, description="ungated", room_id=TASK,
        owner_id="owner-1", required_verdicts=[], merge_approval="owner",
    )
    # No SHA-pinned verdicts at all — the [] snapshot makes the gate vacuous.
    _drive_to_review_passed(s, "st-1", verify_sha=None, review_sha=None)

    pr = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": SHA}
    gh_merges: list[int] = []
    sends: list[tuple] = []
    monkeypatch.setattr(merge, "_resolve_store", lambda project_dir: s)
    monkeypatch.setattr(
        merge, "_resolve_task_id",
        lambda project_dir, store, task_arg: (TASK, None),
    )
    monkeypatch.setattr(
        merge, "_pr_snapshot", lambda pr_number, cwd, repo=None: dict(pr),
    )
    monkeypatch.setattr(
        merge, "_gh_merge",
        lambda pr_number, cwd, sha, repo=None: (
            gh_merges.append(pr_number), (0, "ok"),
        )[1],
    )
    monkeypatch.setattr(
        merge, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
            agents=SimpleNamespace(max_rebase_rounds=3),
        ),
    )
    monkeypatch.setattr(
        merge, "_send_approval_request", lambda *a: sends.append(a),
    )
    monkeypatch.setattr(merge, "_delete_remote_branch", lambda snap, cwd: None)

    # The gated transition succeeds vacuously, but the leg rests on approval.
    assert _run() == 0
    assert s.get_subtask("st-1", TASK).state == "merge_pending"
    assert len(sends) == 1
    assert gh_merges == []

    _grant(s)
    assert _run() == 0
    assert s.get_subtask("st-1", TASK).state == "merged"
    assert gh_merges == [42]


# ─────────────────────────────────────────────────────────────────────────────
# cb approve — the durable grant writer
# ─────────────────────────────────────────────────────────────────────────────


def test_record_approval_grant_grants_at_requested_sha_only(env):
    """The grant is pinned to the SHA the approval REQUEST named — a grant
    can only ever exist for a SHA a request named (finding 21)."""
    env.store.set_pr_number("st-1", TASK, 42)
    env.store.mark_merge_approval_requested("st-1", TASK, SHA)

    lines = merge.record_approval_grant(Path("."), 42)

    assert len(lines) == 1 and "st-1" in lines[0]
    sub = env.store.get_subtask("st-1", TASK)
    assert sub.merge_approved_sha == SHA  # the requested SHA, not "live head"
    assert sub.merge_approved_by == "owner"  # the task's snapshotted approver


def test_record_approval_grant_refuses_when_head_moved_past_request(env, capsys):
    """The failed C1 probe as a regression test: request sent at SHA, branch
    pushed since — cb approve must REFUSE, not grant at the moved head."""
    env.store.set_pr_number("st-1", TASK, 42)
    env.store.mark_merge_approval_requested("st-1", TASK, "sha-old")
    # live PR head is SHA ("sha-1") — the request named "sha-old".

    with pytest.raises(RuntimeError, match="the branch moved"):
        merge.record_approval_grant(Path("."), 42)

    sub = env.store.get_subtask("st-1", TASK)
    assert sub.merge_approved_sha is None  # nothing recorded
    # untouched marker: the merge leg's re-queue owns the fresh request
    assert sub.merge_approval_requested_sha == "sha-old"


def test_record_approval_grant_never_grants_without_a_request(env, capsys):
    """Bound subtask but no approval request ever sent → no speculative
    grant. This kills the second-order path: granted==pending can only be
    satisfied by a SHA a request named."""
    env.store.set_pr_number("st-1", TASK, 42)

    assert merge.record_approval_grant(Path("."), 42) == []

    assert env.store.get_subtask("st-1", TASK).merge_approved_sha is None
    err = capsys.readouterr().err
    assert "NO durable merge grant was recorded" in err
    assert "no approval request has been sent" in err
    assert "cb approve 42" in err  # tells the human exactly what to re-run


def test_record_approval_grant_scopes_to_requesting_rows_only(env, capsys):
    """Multi-row PR (campaign Observation C): the grant write targets only
    the rows whose request matches the head — never every row referencing
    the PR."""
    for sid in ("st-2", "st-3"):
        _drive_to_review_passed(env.store, sid)
    for sid in ("st-1", "st-2", "st-3"):
        env.store.set_pr_number(sid, TASK, 42)
    env.store.mark_merge_approval_requested("st-1", TASK, SHA)  # matches head
    env.store.mark_merge_approval_requested("st-2", TASK, "sha-old")  # stale
    # st-3 never requested approval at all.

    lines = merge.record_approval_grant(Path("."), 42)

    assert len(lines) == 1 and "st-1" in lines[0]
    assert env.store.get_subtask("st-1", TASK).merge_approved_sha == SHA
    assert env.store.get_subtask("st-2", TASK).merge_approved_sha is None
    assert env.store.get_subtask("st-3", TASK).merge_approved_sha is None
    err = capsys.readouterr().err
    assert "st-2" in err and "NOT granted" in err  # the stale row is named


def test_record_approval_grant_pins_repo_via_config_slug(env):
    """The grant's PR snapshot carries --repo <slug> from config repo.url —
    repo identity never depends on what repo the cwd happens to be in."""
    env.store.set_pr_number("st-1", TASK, 42)
    env.store.mark_merge_approval_requested("st-1", TASK, SHA)
    merge.record_approval_grant(Path("."), 42)
    assert env.snapshot_repos == ["acme/widgets"]


def test_record_approval_grant_unbound_pr_warns_loud_and_records_nothing(
    env, capsys,
):
    # Approve-before-binding: nothing binds the PR yet — nothing is recorded,
    # and the human is TOLD so (a silent [] looked like success).
    assert merge.record_approval_grant(Path("."), 99) == []
    assert env.store.get_subtask("st-1", TASK).merge_approved_sha is None
    err = capsys.readouterr().err
    assert "NO durable merge grant was recorded" in err
    assert "cb approve 99" in err  # tells the human exactly what to re-run


def test_record_approval_grant_raises_when_no_active_task(env, monkeypatch):
    """Task-resolution failure must RAISE — an 'approval' recorded against
    nothing must never look like success."""
    monkeypatch.setattr(
        merge, "_resolve_task_id",
        lambda project_dir, store, task_arg: (None, 6),
    )
    with pytest.raises(RuntimeError, match="no active task"):
        merge.record_approval_grant(Path("."), 42)


def test_record_approval_grant_fails_loud_when_head_unreadable(env, monkeypatch):
    env.store.set_pr_number("st-1", TASK, 42)
    env.store.mark_merge_approval_requested("st-1", TASK, SHA)
    monkeypatch.setattr(
        merge, "_pr_snapshot", lambda pr_number, cwd, repo=None: None,
    )

    with pytest.raises(RuntimeError, match="head SHA"):
        merge.record_approval_grant(Path("."), 42)
    assert env.store.get_subtask("st-1", TASK).merge_approved_sha is None


def test_record_approval_grant_fails_loud_on_unresolvable_slug(env, monkeypatch):
    env.store.set_pr_number("st-1", TASK, 42)
    env.store.mark_merge_approval_requested("st-1", TASK, SHA)
    monkeypatch.setattr(
        merge, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://gitlab.example.com/g/p.git"),
        ),
    )
    with pytest.raises(RuntimeError, match="repo slug"):
        merge.record_approval_grant(Path("."), 42)
    assert env.store.get_subtask("st-1", TASK).merge_approved_sha is None


def test_second_order_pre_push_grant_refused_then_fresh_request_still_sent(
    env, capsys,
):
    """The full second-order scenario: a grant attempted after a push is
    refused; the merge leg re-queues; the fresh request still goes out; the
    grant then lands at the freshly requested SHA and the merge executes."""
    from codeband.cli import merge as merge_mod

    # Round 1: queue at SHA, approval request sent (marker burns at SHA).
    assert _run() == 0
    assert len(env.sends) == 1 and env.sends[0][2] == SHA

    # A push moves the head before the human approves.
    env.pr["headRefOid"] = "sha-2"

    # Pre-push grant attempt → refused, nothing recorded.
    with pytest.raises(RuntimeError, match="the branch moved"):
        merge.record_approval_grant(Path("."), 42)
    assert env.store.get_subtask("st-1", TASK).merge_approved_sha is None

    # Re-queue: the merge leg detects the drift and sends the subtask back.
    assert _run("st-1") == merge_mod.EXIT_NEEDS_REBASE

    # Rework re-earns both verdicts at the new SHA.
    for new_state, role, sha in [
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", "sha-2"),
        ("review_passed", "reviewer", "sha-2"),
    ]:
        transition(
            "st-1", TASK, new_state, caller_role=role,
            store=env.store, head_sha=sha,
        )

    # Fresh queue at sha-2 → a FRESH request goes out (new marker SHA).
    assert _run("st-1") == 0
    assert len(env.sends) == 2 and env.sends[1][2] == "sha-2"

    # The grant now lands, pinned to the requested sha-2 — and the merge runs.
    assert len(merge.record_approval_grant(Path("."), 42)) == 1
    assert env.store.get_subtask("st-1", TASK).merge_approved_sha == "sha-2"
    assert _run("st-1") == 0
    assert env.store.get_subtask("st-1", TASK).state == "merged"


def test_cb_approve_refuses_inside_agent_sessions(tmp_path):
    """Finding 18 accident guard: the runner marks every spawned agent
    session's env; cb approve refuses before any work (note: no codeband.yaml
    exists here — the guard fires before config is even loaded)."""
    from click.testing import CliRunner

    from codeband.cli import cli as cb_cli

    result = CliRunner(env={"CODEBAND_AGENT_SESSION": "1"}).invoke(
        cb_cli, ["approve", "42", "--dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    combined = result.output + result.stderr
    assert "human-approval primitive" in combined
    assert "merge leg" in combined


def test_shell_slash_approve_is_exempt_from_the_agent_guard(tmp_path, monkeypatch):
    """The interactive shell's /approve runs inside the orchestrator process
    (which sets the marker for its spawned agents); command_style="slash" is
    only reachable from the human at the REPL prompt, so it bypasses."""
    import codeband.cli.merge as merge_mod
    import codeband.orchestration.kickoff as kickoff_mod
    from codeband.cli import approve as approve_cmd

    monkeypatch.setenv("CODEBAND_AGENT_SESSION", "1")
    calls: list = []
    monkeypatch.setattr(
        merge_mod, "record_approval_grant",
        lambda project_dir, number: calls.append(number) or [],
    )

    async def _fake_send(config, project, message, command_style="cli"):
        calls.append("sent")

    monkeypatch.setattr(kickoff_mod, "send_room_message", _fake_send)
    (tmp_path / "codeband.yaml").write_text(
        "repo:\n  url: https://github.com/acme/widgets\n", encoding="utf-8",
    )

    approve_cmd.callback(
        number=42, project_dir=str(tmp_path), command_style="slash",
    )

    assert calls == [42, "sent"]


def test_cb_approve_command_renders_grant_failures_as_clean_errors(tmp_path):
    """The approve command wraps the grant half in ClickException: a human
    gets the message, not a traceback. No pointer/task exists here, so the
    grant half raises 'no active task'."""
    from click.testing import CliRunner

    from codeband.cli import cli as cb_cli

    (tmp_path / "codeband.yaml").write_text(
        "repo:\n  url: https://github.com/acme/widgets\n", encoding="utf-8",
    )
    result = CliRunner().invoke(
        cb_cli, ["approve", "42", "--dir", str(tmp_path)],
    )
    combined = result.output + result.stderr
    assert result.exit_code != 0
    assert "no active task" in combined
    assert "Traceback" not in combined


# ─────────────────────────────────────────────────────────────────────────────
# merge_approval — registration-time validation + snapshot
# ─────────────────────────────────────────────────────────────────────────────


def _agents(**overrides) -> AgentsConfig:
    return AgentsConfig(handoff_verify_command="true", **overrides)


class TestMergeApprovalValidation:
    def test_default_is_owner(self):
        assert DEFAULT_MERGE_APPROVAL == "owner"
        assert resolve_merge_approval(_agents()) == "owner"

    def test_human_handle_accepted(self):
        assert resolve_merge_approval(_agents(merge_approval="human:yoni")) == "human:yoni"

    def test_none_is_reserved_and_rejected(self):
        with pytest.raises(ValueError, match="not supported in V1"):
            resolve_merge_approval(_agents(merge_approval="none"))

    def test_unknown_value_rejected(self):
        with pytest.raises(ValueError, match="unknown merge_approval"):
            resolve_merge_approval(_agents(merge_approval="banana"))

    def test_empty_human_handle_rejected(self):
        with pytest.raises(ValueError, match="names no"):
            resolve_merge_approval(_agents(merge_approval="human:"))

    def test_registration_snapshots_approver(self, tmp_path):
        store = StateStore(tmp_path / "state" / "orchestration.db")
        register_task(
            room_id="room-7", description="d", owner_id="owner-1",
            agents=_agents(merge_approval="human:yoni"),
            project_dir=tmp_path, store=store,
        )
        assert store.get_task("room-7").merge_approval == "human:yoni"

    def test_bad_approver_fails_registration_and_writes_nothing(self, tmp_path):
        store = StateStore(tmp_path / "state" / "orchestration.db")
        with pytest.raises(ValueError, match="not supported in V1"):
            register_task(
                room_id="room-7", description="d", owner_id="owner-1",
                agents=_agents(merge_approval="none"),
                project_dir=tmp_path, store=store,
            )
        assert store.get_task("room-7") is None
        assert not (tmp_path / ".codeband_room").exists()

    def test_reregistration_refreshes_snapshot(self, tmp_path):
        store = StateStore(tmp_path / "state" / "orchestration.db")
        register_task(
            room_id="room-7", description="d", owner_id="owner-1",
            agents=_agents(), project_dir=tmp_path, store=store,
        )
        assert store.get_task("room-7").merge_approval == "owner"
        register_task(
            room_id="room-7", description="d", owner_id="owner-1",
            agents=_agents(merge_approval="human:yoni"),
            project_dir=tmp_path, store=store,
        )
        assert store.get_task("room-7").merge_approval == "human:yoni"


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess plumbing + exit-code contract
# ─────────────────────────────────────────────────────────────────────────────


def test_pr_snapshot_invokes_gh_with_one_combined_query(monkeypatch, tmp_path):
    calls = {}

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"state": "OPEN", "mergeable": "MERGEABLE", '
                   '"headRefOid": "sha-1"}',
            stderr="",
        )

    monkeypatch.setattr(merge.subprocess, "run", _fake_run)
    snap = merge._pr_snapshot(42, tmp_path)
    assert calls["cmd"] == [
        "gh", "pr", "view", "42",
        "--json", "state,mergeable,headRefOid,headRefName",
    ]
    assert calls["cwd"] == str(tmp_path)
    assert snap == {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "sha-1"}


def test_gh_merge_pins_head_commit_and_never_deletes_local(monkeypatch, tmp_path):
    calls = {}

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(merge.subprocess, "run", _fake_run)
    code, output = merge._gh_merge(42, tmp_path, "sha-1")
    assert calls["cmd"] == [
        "gh", "pr", "merge", "42", "--merge", "--match-head-commit", "sha-1",
    ]
    assert calls["cwd"] == str(tmp_path)
    assert (code, output) == (0, "ok")
    # Local branches belong to coder worktrees — never deleted by the leg.
    assert "--delete-branch" not in calls["cmd"]


def test_gh_merge_omits_head_pin_for_null_pending_sha(monkeypatch, tmp_path):
    calls = {}

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(merge.subprocess, "run", _fake_run)
    merge._gh_merge(42, tmp_path, None)
    assert calls["cmd"] == ["gh", "pr", "merge", "42", "--merge"]
    assert "--match-head-commit" not in calls["cmd"]
    assert "--delete-branch" not in calls["cmd"]


def test_delete_remote_branch_is_remote_only(monkeypatch, tmp_path):
    calls = {}

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(merge.subprocess, "run", _fake_run)
    merge._delete_remote_branch({"headRefName": "feat-x"}, tmp_path)
    # Remote-only: a push --delete, never `git branch -d/-D`.
    assert calls["cmd"] == ["git", "push", "origin", "--delete", "feat-x"]
    assert calls["cwd"] == str(tmp_path)


def test_delete_remote_branch_failure_is_warning_only(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        merge.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="remote ref does not exist",
        ),
    )
    # Never raises, never returns a failure — a warning is the whole effect.
    assert merge._delete_remote_branch({"headRefName": "feat-x"}, tmp_path) is None
    assert "warning" in capsys.readouterr().err


def test_delete_remote_branch_tolerates_missing_branch_name(tmp_path, capsys):
    assert merge._delete_remote_branch({}, tmp_path) is None
    assert merge._delete_remote_branch(None, tmp_path) is None
    assert "skipping remote branch cleanup" in capsys.readouterr().err


def test_exit_codes_distinct_across_both_legs():
    codes = {
        handoff.EXIT_DIRTY_TREE,
        handoff.EXIT_NO_PR,
        handoff.EXIT_VERIFY_FAILED,
        handoff.EXIT_CAP_REACHED,
        handoff.EXIT_NO_ACTIVE_TASK,
        merge.EXIT_NO_PR_NUMBER,
        merge.EXIT_PR_QUERY_FAILED,
        merge.EXIT_NOT_ELIGIBLE,
        merge.EXIT_NEEDS_REBASE,
        merge.EXIT_MERGE_FAILED,
        merge.EXIT_PR_REBIND,
    }
    assert len(codes) == 11  # all distinct
    assert 0 not in codes  # never collide with success


def test_pr_query_failure_is_fail_closed(env, monkeypatch, capsys):
    monkeypatch.setattr(
        merge, "_pr_snapshot", lambda pr_number, cwd, repo=None: None,
    )

    assert _run() == merge.EXIT_PR_QUERY_FAILED
    assert "REJECTED [pr_query_failed]" in capsys.readouterr().err
    assert env.store.get_subtask("st-1", TASK).state == "review_passed"
    assert env.sends == [] and env.gh_merges == []


# ─────────────────────────────────────────────────────────────────────────────
# Rebase-round cap (S2-1)


def _rework_to_review_passed(store, sid="st-1", sha=SHA):
    """Walk a needs_rebase subtask back to review_passed (legal edges only)."""
    for new_state, role, head in [
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", sha),
        ("review_passed", "reviewer", sha),
    ]:
        transition(sid, TASK, new_state, caller_role=role, store=store,
                   head_sha=head)


def test_rebase_loop_hits_cap_and_blocks(env, capsys):
    """An active rebase loop is bounded: the send-back past the cap escalates
    to blocked with the BLOCKED [rebase_cap_reached] tag instead of another
    needs_rebase round. The cap is the env-stubbed agents.max_rebase_rounds=3.
    """
    _grant(env.store)
    env.pr["mergeable"] = "CONFLICTING"  # every attempt classifies needs_rebase

    for expected_round in (1, 2, 3):
        assert _run() == merge.EXIT_NEEDS_REBASE
        sub = env.store.get_subtask("st-1", TASK)
        assert sub.state == "needs_rebase"
        assert sub.rebase_rounds == expected_round
        _rework_to_review_passed(env.store)

    # Round 4: at the cap — escalates instead of sending back again.
    assert _run() == merge.EXIT_REBASE_CAP_REACHED
    err = capsys.readouterr().err
    assert "BLOCKED [rebase_cap_reached]" in err
    sub = env.store.get_subtask("st-1", TASK)
    assert sub.state == "blocked"
    assert sub.rebase_rounds == 3  # the blocked escalation is not a round
    blocked = _log_rows(env.store, "st-1", "blocked")
    assert len(blocked) == 1
    assert "rebase-round cap 3 reached" in blocked[0]["reason"]


def test_rebase_round_counter_survives_restart(env):
    """The cap holds across a crash/reopen: a fresh StateStore on the same DB
    reads the committed rebase_rounds, so a restarted merge leg still blocks."""
    _grant(env.store)
    env.pr["mergeable"] = "CONFLICTING"

    assert _run() == merge.EXIT_NEEDS_REBASE
    reopened = StateStore(env.store.db_path)
    assert reopened.get_subtask("st-1", TASK).rebase_rounds == 1


def test_sha_moved_send_back_also_counts_toward_cap(env, capsys):
    """The execution-queue SHA re-check routes through the same capped helper."""
    _grant(env.store)

    # Queue at SHA, then move the head: sha_moved → needs_rebase round 1.
    transition("st-1", TASK, "merge_pending", caller_role="mergemaster",
               store=env.store, head_sha=SHA)
    env.pr["headRefOid"] = "sha-2"
    assert _run() == merge.EXIT_NEEDS_REBASE
    assert env.store.get_subtask("st-1", TASK).rebase_rounds == 1
