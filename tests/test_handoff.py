"""Tests for the ``cb-phase`` verify-gated handoff CLI (RFC Workstream 3)."""

from __future__ import annotations

import pytest

from codeband.cli import handoff
from codeband.state.fsm import transition
from codeband.state.store import StateStore


@pytest.fixture
def store(tmp_path) -> StateStore:
    """A store with a subtask already advanced to ``verify_pending``."""
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(task_id="room-1", description="demo", room_id="room-1")
    transition("st-1", "room-1", "assigned", caller_role="conductor", store=s)
    transition("st-1", "room-1", "in_progress", caller_role="coder", store=s)
    transition("st-1", "room-1", "verify_pending", caller_role="coder", store=s)
    return s


@pytest.fixture
def patch_gates(monkeypatch, store):
    """Wire the handoff helpers to controllable defaults (all gates pass)."""
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    # task_id resolution is its own seam (tested for real below); here the active
    # room is always the fixture's ``room-1`` regardless of the ``--task`` label.
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, store, task_arg: ("room-1", None),
    )
    monkeypatch.setattr(handoff, "_verify_command", lambda project_dir: "verify-cmd")
    monkeypatch.setattr(handoff, "_max_verify_attempts", lambda project_dir: 20)
    monkeypatch.setattr(handoff, "_max_review_rounds", lambda project_dir: 3)
    monkeypatch.setattr(handoff, "_uncommitted_files", lambda worktree: [])
    monkeypatch.setattr(handoff, "_current_branch", lambda worktree: "feat-x")
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: (0, ""))
    monkeypatch.setattr(handoff, "_git_head", lambda worktree: "cafe1234")
    # Verify's ONE gh snapshot: OPEN, on the worktree's branch, with the PR
    # head matching the worktree HEAD (the coder pushed) by default.
    monkeypatch.setattr(
        handoff, "_verify_pr_snapshot",
        lambda project_dir, pr: {
            "state": "OPEN", "headRefName": "feat-x", "headRefOid": "cafe1234",
        },
    )
    return store


def _snapshot(monkeypatch, **overrides):
    """Re-stub the verify snapshot with specific fields overridden."""
    base = {"state": "OPEN", "headRefName": "feat-x", "headRefOid": "cafe1234"}
    base.update(overrides)
    monkeypatch.setattr(
        handoff, "_verify_pr_snapshot", lambda project_dir, pr: dict(base),
    )


def _run():
    return handoff.main(["verify", "st-1", "--task", "room-1", "--pr", "42"])


def test_verify_success_advances_to_review_pending(patch_gates):
    store = patch_gates
    assert _run() == 0
    assert store.get_subtask("st-1", "room-1").state == "review_pending"


def test_verify_fails_on_dirty_tree(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_uncommitted_files", lambda worktree: ["M a.py"])
    assert _run() != 0
    assert store.get_subtask("st-1", "room-1").state == "verify_pending"


def test_verify_fails_on_non_open_pr(patch_gates, monkeypatch):
    store = patch_gates
    _snapshot(monkeypatch, state="CLOSED")
    assert _run() != 0
    assert store.get_subtask("st-1", "room-1").state == "verify_pending"


def test_verify_fails_on_failing_verify_command(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: (1, "boom"))
    assert _run() != 0
    assert store.get_subtask("st-1", "room-1").state == "verify_pending"


def test_verify_skips_command_when_unconfigured(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_verify_command", lambda project_dir: None)

    def _boom(cmd, cwd):  # pragma: no cover - must not be called
        raise AssertionError("verify command should not run when unconfigured")

    monkeypatch.setattr(handoff, "_run_verify_command", _boom)
    assert _run() == 0
    assert store.get_subtask("st-1", "room-1").state == "review_pending"


# ── rebase rework: verify re-entry from the merge gate's send-back ──────────

def _send_back_for_rebase(store):
    """Drive the fixture subtask to ``needs_rebase`` via legal FSM edges."""
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    transition("st-1", "room-1", "review_passed", caller_role="reviewer", store=store)
    transition("st-1", "room-1", "needs_rebase", caller_role="mergemaster", store=store)


def test_verify_from_needs_rebase_walks_to_review_pending(patch_gates):
    """The rebased commit re-enters the normal verify walk — the coder's only
    exit from the merge gate's send-back is ``cb-phase verify``."""
    store = patch_gates
    _send_back_for_rebase(store)
    assert _run() == 0
    assert store.get_subtask("st-1", "room-1").state == "review_pending"


def test_verify_from_needs_rebase_ignores_review_round_cap(patch_gates, monkeypatch):
    """A merge-gate send-back is not a review round: the walk must succeed even
    when the review-round cap would reject a ``review_failed`` rework."""
    store = patch_gates
    monkeypatch.setattr(handoff, "_max_review_rounds", lambda project_dir: 0)
    _send_back_for_rebase(store)
    assert _run() == 0
    assert store.get_subtask("st-1", "room-1").state == "review_pending"


def test_verify_from_needs_rebase_does_not_increment_review_round(patch_gates):
    store = patch_gates
    _send_back_for_rebase(store)
    before = store.get_subtask("st-1", "room-1").review_round
    assert _run() == 0
    assert store.get_subtask("st-1", "room-1").review_round == before


# ── structured, actionable rejections (one stable tag + exit code per mode) ──

def test_dirty_tree_emits_tag_and_exit_code(patch_gates, monkeypatch, capsys):
    monkeypatch.setattr(
        handoff, "_uncommitted_files", lambda worktree: ["M a.py", "?? b.py"],
    )
    assert _run() == handoff.EXIT_DIRTY_TREE
    err = capsys.readouterr().err
    assert "REJECTED [dirty_tree]: 2 uncommitted files." in err
    assert "Commit or stash, then re-run cb-phase verify." in err


def test_no_pr_emits_tag_branch_and_exit_code(patch_gates, monkeypatch, capsys):
    _snapshot(monkeypatch, state="CLOSED")
    monkeypatch.setattr(handoff, "_current_branch", lambda worktree: "feat/login")
    assert _run() == handoff.EXIT_NO_PR
    err = capsys.readouterr().err
    assert "REJECTED [no_pr]: no open PR for branch feat/login." in err
    assert "Push and open a PR, then re-run." in err


def test_verify_failed_emits_tag_exitcode_and_tail(patch_gates, monkeypatch, capsys):
    tail = "line-a\nline-b\nFAILED: assertion"
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: (7, tail))
    assert _run() == handoff.EXIT_VERIFY_FAILED
    err = capsys.readouterr().err
    assert "REJECTED [verify_failed] (exit 7):" in err
    assert "FAILED: assertion" in err
    assert "Fix and re-run." in err


def test_verify_failed_tail_is_truncated(patch_gates, monkeypatch, capsys):
    big = "\n".join(f"row-{i}" for i in range(100))
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: (1, big))
    assert _run() == handoff.EXIT_VERIFY_FAILED
    err = capsys.readouterr().err
    assert "row-99" in err  # the tail is kept
    assert "row-0\n" not in err  # the head is dropped
    # Only the last N lines of the command output are surfaced.
    assert err.count("row-") <= handoff._VERIFY_OUTPUT_TAIL_LINES


def test_cap_reached_emits_blocked_tag_and_exit_code(patch_gates, monkeypatch, capsys):
    store = patch_gates
    # Force the subtask to the cap so the next call escalates.
    monkeypatch.setattr(handoff, "_max_verify_attempts", lambda project_dir: 3)
    for _ in range(3):
        store.increment_verify_attempts("st-1", "room-1")
    assert _run() == handoff.EXIT_CAP_REACHED
    err = capsys.readouterr().err
    assert "BLOCKED [cap_reached]: 3 verify attempts." in err
    assert "Escalated to human; stop and await." in err
    assert store.get_subtask("st-1", "room-1").state == "blocked"


def test_each_failure_mode_has_a_distinct_exit_code():
    codes = {
        handoff.EXIT_DIRTY_TREE,
        handoff.EXIT_NO_PR,
        handoff.EXIT_VERIFY_FAILED,
        handoff.EXIT_CAP_REACHED,
        handoff.EXIT_NO_ACTIVE_TASK,
        handoff.EXIT_HEAD_UNRESOLVED,
        handoff.EXIT_HEAD_MISMATCH,
        handoff.EXIT_PR_QUERY_FAILED,
        handoff.EXIT_WRONG_PR,
        handoff.EXIT_INVALID_SUBTASK_ID,
    }
    assert len(codes) == 10  # all distinct
    assert 0 not in codes  # never collide with success
    # 7–12 belong to the merge leg (cli/merge.py) — never reuse them here.
    assert codes.isdisjoint(range(7, 13))


def test_uncommitted_files_reads_porcelain(monkeypatch, tmp_path):
    calls = {}

    class _Result:
        returncode = 0
        stdout = " M a.py\n?? b.py\n"

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(handoff.subprocess, "run", _fake_run)
    files = handoff._uncommitted_files(tmp_path)
    assert files == [" M a.py", "?? b.py"]
    assert calls["cmd"][:2] == ["git", "-C"]


def test_uncommitted_files_clean_tree_is_empty(monkeypatch, tmp_path):
    class _Result:
        returncode = 0
        stdout = "  \n"

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Result())
    assert handoff._uncommitted_files(tmp_path) == []


def test_uncommitted_files_treats_git_failure_as_dirty(monkeypatch, tmp_path):
    class _Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Result())
    assert handoff._uncommitted_files(tmp_path) != []  # non-empty → gate rejects


def test_verify_pr_snapshot_queries_gh_once_with_repo_slug(monkeypatch, tmp_path):
    """ONE query, cwd-independent by construction: --repo from config repo.url,
    and all three decision fields requested together."""
    from types import SimpleNamespace

    calls = []

    class _Result:
        returncode = 0
        stdout = '{"state": "OPEN", "headRefName": "feat-x", "headRefOid": "cafe1234"}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(
        handoff, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
        ),
    )
    monkeypatch.setattr(handoff.subprocess, "run", _fake_run)
    snap = handoff._verify_pr_snapshot(tmp_path, 7)
    assert snap == {
        "state": "OPEN", "headRefName": "feat-x", "headRefOid": "cafe1234",
    }
    assert calls == [[
        "gh", "pr", "view", "7",
        "--json", "state,headRefName,headRefOid", "--repo", "acme/widgets",
    ]]


def test_verify_pr_snapshot_returns_none_on_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(
        handoff, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
        ),
    )

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "no such PR"

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Fail())
    assert handoff._verify_pr_snapshot(tmp_path, 7) is None

    class _Garbage:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Garbage())
    assert handoff._verify_pr_snapshot(tmp_path, 7) is None


def test_verify_pr_snapshot_returns_none_on_non_github_url(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(
        handoff, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://gitlab.example.com/g/p.git"),
        ),
    )

    def _boom(cmd, **kw):  # pragma: no cover - must not be called
        raise AssertionError("gh must not run without a resolvable slug")

    monkeypatch.setattr(handoff.subprocess, "run", _boom)
    assert handoff._verify_pr_snapshot(tmp_path, 7) is None


# ── cb-phase start — seed the subtask lifecycle into in_progress ─────────────

def _start(store, monkeypatch, subtask_id):
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, store, task_arg: ("room-1", None),
    )
    return handoff.main(["start", subtask_id, "--task", "room-1"])


def test_start_creates_nonexistent_subtask_in_progress(store, monkeypatch, capsys):
    # st-2 does not exist yet — start must create it and land it in_progress.
    assert store.get_subtask("st-2", "room-1") is None
    assert _start(store, monkeypatch, "st-2") == 0
    assert store.get_subtask("st-2", "room-1").state == "in_progress"
    out = capsys.readouterr().out
    assert "subtask st-2 → in_progress (task room-1)." in out


def test_start_is_idempotent(store, monkeypatch, capsys):
    # Starting twice is a no-op the second time — never moves backward.
    assert _start(store, monkeypatch, "st-2") == 0
    assert _start(store, monkeypatch, "st-2") == 0
    assert store.get_subtask("st-2", "room-1").state == "in_progress"
    assert "already at in_progress" in capsys.readouterr().out


def test_start_non_regressing_on_verify_pending(store, monkeypatch, capsys):
    # The `store` fixture leaves st-1 at verify_pending — start must not rewind.
    assert _start(store, monkeypatch, "st-1") == 0
    assert store.get_subtask("st-1", "room-1").state == "verify_pending"
    assert "already at verify_pending" in capsys.readouterr().out


def test_start_non_regressing_on_review_failed(store, monkeypatch, capsys):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    transition("st-1", "room-1", "review_failed", caller_role="reviewer", store=store)
    assert _start(store, monkeypatch, "st-1") == 0
    assert store.get_subtask("st-1", "room-1").state == "review_failed"
    assert "already at review_failed" in capsys.readouterr().out


def test_start_from_assigned_walks_to_in_progress(monkeypatch, tmp_path, capsys):
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(task_id="room-1", description="demo", room_id="room-1")
    transition("st-1", "room-1", "assigned", caller_role="conductor", store=s)
    assert _start(s, monkeypatch, "st-1") == 0
    assert s.get_subtask("st-1", "room-1").state == "in_progress"


def test_start_task_label_is_optional(store, monkeypatch):
    # ``--task`` is now an optional, non-authoritative label: omitting it must
    # not error at the parser. The active room is resolved from .codeband_room
    # (stubbed here), so start still seeds the subtask.
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, s, task_arg: ("room-1", None),
    )
    assert handoff.main(["start", "st-2"]) == 0
    assert store.get_subtask("st-2", "room-1").state == "in_progress"


# ── cb-phase review — reviewer verdict routed through the FSM ────────────────

def _review(monkeypatch, store, verdict: str):
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, s, task_arg: ("room-1", None),
    )
    # The verdict SHA comes from the PR head — never the invoker's cwd HEAD
    # (the shipped reviewer runs in a repo-less scratch dir).
    monkeypatch.setattr(handoff, "_pr_head_sha", lambda project_dir, pr: "beef5678")
    monkeypatch.setattr(handoff, "_git_head", lambda worktree: "cwd-head-must-not-be-used")
    return handoff.main(["review", "st-1", "--task", "room-1", "--pr", "42", verdict])


def test_review_approve_advances_to_review_passed(store, monkeypatch):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    assert _review(monkeypatch, store, "--approve") == 0
    assert store.get_subtask("st-1", "room-1").state == "review_passed"


def test_review_reject_advances_to_review_failed(store, monkeypatch):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    assert _review(monkeypatch, store, "--reject") == 0
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "review_failed"
    assert sub.review_round == 1  # a reject is one failed review round


def test_review_illegal_from_verify_pending_writes_nothing(store, monkeypatch, capsys):
    # The `store` fixture leaves st-1 at verify_pending (no review yet).
    assert _review(monkeypatch, store, "--approve") == 1
    assert store.get_subtask("st-1", "room-1").state == "verify_pending"
    assert "review verdict rejected" in capsys.readouterr().err


def test_review_requires_an_explicit_verdict():
    # Mutually-exclusive --approve/--reject is required → argparse exits.
    with pytest.raises(SystemExit):
        handoff.main(["review", "st-1", "--task", "room-1"])


# ── head_sha — SHA-pinned verify / review outcome records (additive) ─────────

def _last_transition_row(store, to_state: str):
    """Fetch the newest transition_log row landing in ``to_state``."""
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM transition_log WHERE to_state = ? ORDER BY id DESC LIMIT 1",
            (to_state,),
        ).fetchone()
    finally:
        conn.close()


def test_verify_outcome_records_head_sha(patch_gates):
    store = patch_gates
    assert _run() == 0
    row = _last_transition_row(store, "review_pending")
    assert row is not None
    assert row["head_sha"] == "cafe1234"


def test_review_outcome_records_head_sha_on_approve(store, monkeypatch):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    assert _review(monkeypatch, store, "--approve") == 0
    row = _last_transition_row(store, "review_passed")
    assert row is not None
    assert row["head_sha"] == "beef5678"


def test_review_outcome_records_head_sha_on_reject(store, monkeypatch):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    assert _review(monkeypatch, store, "--reject") == 0
    row = _last_transition_row(store, "review_failed")
    assert row is not None
    assert row["head_sha"] == "beef5678"


def test_transitions_without_head_sha_store_null(store):
    # Every non-outcome transition (and any legacy caller) leaves head_sha
    # NULL — the field is additive and the read path is untouched.
    row = _last_transition_row(store, "verify_pending")  # from the fixture walk
    assert row is not None
    assert row["head_sha"] is None


def test_git_head_returns_none_outside_a_repo(monkeypatch, tmp_path):
    class _Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Result())
    # Best-effort by design: an unresolvable HEAD pins nothing (NULL), it
    # never blocks the transition.
    assert handoff._git_head(tmp_path) is None


def test_git_head_parses_rev_parse_output(monkeypatch, tmp_path):
    class _Result:
        returncode = 0
        stdout = "a3f9c2e8b1d4567890abcdef12345678deadbeef\n"

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Result())
    assert handoff._git_head(tmp_path) == "a3f9c2e8b1d4567890abcdef12345678deadbeef"


# ── verdict SHA from the PR head — fail loud on unresolvable, never NULL ──────
#
# Root cause pinned here: the verdict head_sha used to be `git rev-parse HEAD`
# of the invoker's cwd. The shipped Code Reviewer has NO git repo (scratch
# dir), so the prompted review flow could only ever record NULL — and every
# gated merge rejected not_eligible (the 2026-06-10 Scenario A incident).


def _count_transition_rows(store) -> int:
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM transition_log").fetchone()[0]
    finally:
        conn.close()


def test_review_pr_argument_is_required(store, monkeypatch):
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    with pytest.raises(SystemExit):
        handoff.main(["review", "st-1", "--task", "room-1", "--approve"])


def test_review_verdict_pins_pr_head_not_cwd_head(store, monkeypatch):
    """The recorded SHA is the PR head from gh — the invoker's cwd HEAD (which
    the repo-less reviewer cannot even produce) must play no part."""
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    assert _review(monkeypatch, store, "--approve") == 0
    row = _last_transition_row(store, "review_passed")
    assert row["head_sha"] == "beef5678"  # the PR head…
    assert row["head_sha"] != "cwd-head-must-not-be-used"  # …not the cwd HEAD


def test_review_head_unresolved_records_nothing_and_fails_loud(
    store, monkeypatch, capsys,
):
    """A verdict that pins nothing must never report success: gh failure →
    loud rejection, non-zero exit, NO transition row (today's silent NULL
    poisoned the merge gate invisibly)."""
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, s, task_arg: ("room-1", None),
    )
    monkeypatch.setattr(handoff, "_pr_head_sha", lambda project_dir, pr: None)
    rows_before = _count_transition_rows(store)

    code = handoff.main(["review", "st-1", "--task", "room-1", "--pr", "42", "--approve"])

    assert code == handoff.EXIT_HEAD_UNRESOLVED
    err = capsys.readouterr().err
    assert "REJECTED [head_unresolved]" in err
    assert "verdict NOT recorded" in err
    assert _count_transition_rows(store) == rows_before  # nothing written
    assert store.get_subtask("st-1", "room-1").state == "review_pending"


def test_verify_head_mismatch_burns_attempt_and_records_nothing(
    patch_gates, monkeypatch, capsys,
):
    """Worktree HEAD ≠ PR head: the coder forgot to push — a legitimate coder
    error that counts as one verify attempt and writes no review_pending row."""
    store = patch_gates
    _snapshot(monkeypatch, headRefOid="feed0042")
    attempts_before = store.get_subtask("st-1", "room-1").verify_attempts

    assert _run() == handoff.EXIT_HEAD_MISMATCH
    err = capsys.readouterr().err
    assert "REJECTED [head_mismatch]" in err
    assert "cafe1234" in err and "feed0042" in err  # names both SHAs
    assert "push your commits" in err
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "verify_pending"  # no review_pending row
    assert sub.verify_attempts == attempts_before + 1  # one attempt burned


def test_verify_pr_head_unresolved_fails_loud_without_burning_attempt(
    patch_gates, monkeypatch, capsys,
):
    """The snapshot resolved (OPEN, right branch) but carries no head SHA —
    still an infra failure: loud, nothing recorded, no attempt burned."""
    store = patch_gates
    _snapshot(monkeypatch, headRefOid=None)
    attempts_before = store.get_subtask("st-1", "room-1").verify_attempts

    assert _run() == handoff.EXIT_HEAD_UNRESOLVED
    err = capsys.readouterr().err
    assert "REJECTED [head_unresolved]" in err
    assert "verify outcome NOT recorded" in err
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "verify_pending"
    # Infra failure, not a coder error — the attempt budget is untouched.
    assert sub.verify_attempts == attempts_before


def test_verify_worktree_head_unresolved_fails_loud_instead_of_null(
    patch_gates, monkeypatch, capsys,
):
    """Previously an unresolvable worktree HEAD silently recorded NULL — now
    it is a loud head_unresolved rejection that records nothing."""
    store = patch_gates
    monkeypatch.setattr(handoff, "_git_head", lambda worktree: None)

    assert _run() == handoff.EXIT_HEAD_UNRESOLVED
    assert "REJECTED [head_unresolved]" in capsys.readouterr().err
    assert store.get_subtask("st-1", "room-1").state == "verify_pending"


# ── one-snapshot verify matrix (C1): infra/no-burn, closed, wrong-PR, bind ──

def test_verify_pr_query_failed_does_not_burn_attempt(
    patch_gates, monkeypatch, capsys,
):
    """gh infra failure (snapshot is None): loud tagged rejection, nothing
    recorded, no verify attempt burned — infra never burns durable budget."""
    store = patch_gates
    monkeypatch.setattr(handoff, "_verify_pr_snapshot", lambda project_dir, pr: None)
    attempts_before = store.get_subtask("st-1", "room-1").verify_attempts

    assert _run() == handoff.EXIT_PR_QUERY_FAILED
    err = capsys.readouterr().err
    assert "REJECTED [pr_query_failed]" in err
    assert "no attempt burned" in err
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "verify_pending"
    assert sub.verify_attempts == attempts_before


def test_verify_wrong_pr_burns_attempt_and_names_both_branches(
    patch_gates, monkeypatch, capsys,
):
    """An OPEN PR whose head branch is not the worktree's branch is some
    OTHER PR's number — a coder error that burns one attempt and writes no
    transition. Closes the any-open-PR-number gate hole."""
    store = patch_gates
    _snapshot(monkeypatch, headRefName="feat-other")
    attempts_before = store.get_subtask("st-1", "room-1").verify_attempts

    assert _run() == handoff.EXIT_WRONG_PR
    err = capsys.readouterr().err
    assert "REJECTED [wrong_pr]" in err
    assert "feat-other" in err and "feat-x" in err  # names both branches
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "verify_pending"
    assert sub.verify_attempts == attempts_before + 1


def test_verify_wrong_pr_rejects_before_running_the_verify_command(
    patch_gates, monkeypatch,
):
    """The wrong-PR check precedes the (expensive) verify command — a wrong
    PR number must not buy a free test run."""
    _snapshot(monkeypatch, headRefName="feat-other")

    def _boom(cmd, cwd):  # pragma: no cover - must not be called
        raise AssertionError("verify command must not run for a wrong PR")

    monkeypatch.setattr(handoff, "_run_verify_command", _boom)
    assert _run() == handoff.EXIT_WRONG_PR


def test_verify_unresolvable_worktree_branch_fails_loud_without_burn(
    patch_gates, monkeypatch, capsys,
):
    """A detached/broken worktree (no branch name) is an infra failure, not a
    coder error: loud rejection, no burn — the wrong-PR check must never
    pass-by-default on a missing branch."""
    store = patch_gates
    monkeypatch.setattr(handoff, "_current_branch", lambda worktree: None)
    attempts_before = store.get_subtask("st-1", "room-1").verify_attempts

    assert _run() == handoff.EXIT_HEAD_UNRESOLVED
    assert "REJECTED [head_unresolved]" in capsys.readouterr().err
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "verify_pending"
    assert sub.verify_attempts == attempts_before


def test_verify_pass_persists_the_pr_binding(patch_gates):
    """On PASS the subtask↔PR binding is created by the coder who knows the
    PR — not first at merge time."""
    store = patch_gates
    assert store.get_subtask("st-1", "room-1").pr_number is None

    assert _run() == 0
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "review_pending"
    assert sub.pr_number == 42


def test_verify_rejection_does_not_bind_the_pr(patch_gates, monkeypatch):
    """A failed gate must not bind: only a PROVEN PR is persisted."""
    store = patch_gates
    _snapshot(monkeypatch, headRefOid="feed0042")  # head mismatch → reject
    assert _run() == handoff.EXIT_HEAD_MISMATCH
    assert store.get_subtask("st-1", "room-1").pr_number is None


def test_pr_head_sha_queries_gh_with_repo_slug(monkeypatch, tmp_path):
    """cwd-independent by construction: --repo comes from config's repo.url."""
    from types import SimpleNamespace

    calls = {}

    class _Result:
        returncode = 0
        stdout = '{"headRefOid": "a3f9c2e8"}'
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(
        handoff, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
        ),
    )
    monkeypatch.setattr(handoff.subprocess, "run", _fake_run)
    assert handoff._pr_head_sha(tmp_path, 42) == "a3f9c2e8"
    assert calls["cmd"] == [
        "gh", "pr", "view", "42", "--json", "headRefOid", "--repo", "acme/widgets",
    ]


def test_pr_head_sha_returns_none_on_failure(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(
        handoff, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
        ),
    )

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "no such PR"

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Fail())
    assert handoff._pr_head_sha(tmp_path, 42) is None

    class _Empty:
        returncode = 0
        stdout = '{"headRefOid": ""}'
        stderr = ""

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Empty())
    assert handoff._pr_head_sha(tmp_path, 42) is None

    class _Garbage:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Garbage())
    assert handoff._pr_head_sha(tmp_path, 42) is None


def test_pr_head_sha_returns_none_on_non_github_url(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(
        handoff, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://gitlab.example.com/g/p.git"),
        ),
    )

    def _boom(cmd, **kw):  # pragma: no cover - must not be called
        raise AssertionError("gh must not run without a resolvable slug")

    monkeypatch.setattr(handoff.subprocess, "run", _boom)
    assert handoff._pr_head_sha(tmp_path, 42) is None


# --- claim-time guard ---


@pytest.mark.parametrize("bad_id", ["claim-time-guard", "subtask-1"])
def test_invalid_subtask_id_rejected_with_tagged_stderr(bad_id, capsys):
    """The validator refuses anything that is not ``st-N``."""
    assert handoff._validate_subtask_id(bad_id) == handoff.EXIT_INVALID_SUBTASK_ID
    err = capsys.readouterr().err
    assert "REJECTED [invalid_subtask_id]" in err
    assert repr(bad_id) in err


@pytest.mark.parametrize("good_id", ["st-1", "st-99"])
def test_valid_subtask_id_passes_validator(good_id):
    """``st-N`` ids return None — no rejection."""
    assert handoff._validate_subtask_id(good_id) is None


@pytest.mark.parametrize(
    "sneaky_id",
    [
        "st-1\n",            # trailing newline — Python's $ matches before \n
        "st-1\r\n",          # CRLF
        "st-1\r",            # bare CR
        "\nst-1",            # leading newline
        "st-1\nst-2",        # embedded newline before another valid id
        " st-1",             # leading whitespace
        "st-1 ",             # trailing whitespace
        "st-",               # no digits
        "",                  # empty
        "st-1a",             # trailing non-digit
        "ST-1",              # wrong case
        "st-١",         # Arabic-Indic digit U+0661 — \d accepts, [0-9] does not
        "st-１２",   # full-width digits U+FF11 U+FF12 — same class
        "st-1١",        # mixed ASCII + Unicode digit
    ],
)
def test_validator_rejects_sneaky_inputs(sneaky_id, capsys):
    """Whitespace/newline/empty/case/Unicode-digit bypass attempts must be rejected.

    The trailing-newline case is the round-1 review finding: Python's ``$``
    matches *before* a final ``\\n``, so ``re.match(r"^st-\\d+$", "st-1\\n")``
    returns a match. ``re.fullmatch`` is the structural fix.

    The Unicode-digit cases are the round-2 review finding: ``\\d`` in Python
    matches any Unicode decimal digit by default (e.g. Arabic-Indic
    ``\\u0661`` or full-width ``\\uff11``), so ``st-\\d+`` would accept
    ``"st-\\u0661"``. ``[0-9]+`` restricts to ASCII digits — the only shape
    the Planner ever emits.
    """
    assert (
        handoff._validate_subtask_id(sneaky_id)
        == handoff.EXIT_INVALID_SUBTASK_ID
    )
    err = capsys.readouterr().err
    assert "REJECTED [invalid_subtask_id]" in err
    assert repr(sneaky_id) in err


def _build_guard_args(subcommand: str, bad_id: str) -> list[str]:
    """The minimum required-flags argv for each subcommand under guard test."""
    common = ["--project-dir", "."]
    if subcommand == "start":
        return ["start", bad_id, *common]
    if subcommand == "verify":
        return ["verify", bad_id, "--pr", "42", "--worktree", ".", *common]
    if subcommand == "review":
        return ["review", bad_id, "--pr", "42", "--approve", *common]
    if subcommand == "verify-acceptance":
        return ["verify-acceptance", bad_id, "--pr", "42", "--accept", *common]
    if subcommand == "abandon":
        return ["abandon", bad_id, *common]
    if subcommand == "resume":
        return ["resume", bad_id, *common]
    raise AssertionError(f"unhandled subcommand: {subcommand}")


@pytest.mark.parametrize(
    "subcommand",
    ["start", "verify", "review", "verify-acceptance", "abandon", "resume"],
)
@pytest.mark.parametrize("bad_id", ["claim-time-guard", "subtask-1"])
def test_guard_fires_before_store_in_every_subcommand(
    subcommand, bad_id, monkeypatch, capsys,
):
    """Guard runs BEFORE store / task-id resolution in all six legs.

    If the guard ever runs after ``_resolve_store`` / ``_resolve_task_id``, a
    malformed id can already have opened the DB or read the active-room
    pointer — both of which the guard exists to prevent. Monkeypatching them
    to raise makes the ordering structural, not a comment.
    """
    def _must_not_call(*args, **kwargs):
        raise AssertionError(
            "guard must reject before store / task-id resolution",
        )

    monkeypatch.setattr(handoff, "_resolve_store", _must_not_call)
    monkeypatch.setattr(handoff, "_resolve_task_id", _must_not_call)

    argv = _build_guard_args(subcommand, bad_id)
    assert handoff.main(argv) == handoff.EXIT_INVALID_SUBTASK_ID
    err = capsys.readouterr().err
    assert "REJECTED [invalid_subtask_id]" in err
    assert repr(bad_id) in err


def test_guard_regression_valid_id_proceeds_through_start(tmp_path, monkeypatch):
    """A valid ``st-N`` id is not blocked — the normal start path still works.

    Uses a real codeband.yaml + tmp_path-backed store via
    ``CODEBAND_PROJECT_DIR`` (no resolver stubs) so this exercises the guard
    in series with the real store — the one round-trip that proves the guard
    does not over-reject the happy path.
    """
    from codeband.config import (
        AgentsConfig,
        CodebandConfig,
        RepoConfig,
        WorkspaceConfig,
    )

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    workspace = tmp_path / "workspace"
    cfg = CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git", branch="main"),
        agents=AgentsConfig(),
        workspace=WorkspaceConfig(path=str(workspace)),
    )
    cfg.to_yaml(project_dir / "codeband.yaml")
    (project_dir / ".codeband_room").write_text("room-1", encoding="utf-8")
    store = StateStore(workspace / "state" / "orchestration.db")
    store.create_task("room-1", "demo", "room-1")

    monkeypatch.setenv("CODEBAND_PROJECT_DIR", str(project_dir))

    assert handoff.main(["start", "st-1", "--project-dir", str(project_dir)]) == 0
    assert store.get_subtask("st-1", "room-1").state == "in_progress"
