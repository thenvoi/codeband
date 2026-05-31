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
    monkeypatch.setattr(handoff, "_verify_command", lambda project_dir: "verify-cmd")
    monkeypatch.setattr(handoff, "_max_verify_attempts", lambda project_dir: 20)
    monkeypatch.setattr(handoff, "_max_review_rounds", lambda project_dir: 3)
    monkeypatch.setattr(handoff, "_uncommitted_files", lambda worktree: [])
    monkeypatch.setattr(handoff, "_current_branch", lambda worktree: "feat-x")
    monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: (0, ""))
    return store


def _run():
    return handoff.main(["verify", "st-1", "--task", "room-1", "--pr", "42"])


def test_verify_success_advances_to_review_pending(patch_gates):
    store = patch_gates
    assert _run() == 0
    assert store.get_subtask("st-1").state == "review_pending"


def test_verify_fails_on_dirty_tree(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_uncommitted_files", lambda worktree: ["M a.py"])
    assert _run() != 0
    assert store.get_subtask("st-1").state == "verify_pending"


def test_verify_fails_on_non_open_pr(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: False)
    assert _run() != 0
    assert store.get_subtask("st-1").state == "verify_pending"


def test_verify_fails_on_failing_verify_command(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: (1, "boom"))
    assert _run() != 0
    assert store.get_subtask("st-1").state == "verify_pending"


def test_verify_skips_command_when_unconfigured(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_verify_command", lambda project_dir: None)

    def _boom(cmd, cwd):  # pragma: no cover - must not be called
        raise AssertionError("verify command should not run when unconfigured")

    monkeypatch.setattr(handoff, "_run_verify_command", _boom)
    assert _run() == 0
    assert store.get_subtask("st-1").state == "review_pending"


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
    monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: False)
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
        store.increment_verify_attempts("st-1")
    assert _run() == handoff.EXIT_CAP_REACHED
    err = capsys.readouterr().err
    assert "BLOCKED [cap_reached]: 3 verify attempts." in err
    assert "Escalated to human; stop and await." in err
    assert store.get_subtask("st-1").state == "blocked"


def test_each_failure_mode_has_a_distinct_exit_code():
    codes = {
        handoff.EXIT_DIRTY_TREE,
        handoff.EXIT_NO_PR,
        handoff.EXIT_VERIFY_FAILED,
        handoff.EXIT_CAP_REACHED,
    }
    assert len(codes) == 4  # all distinct
    assert 0 not in codes  # never collide with success


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


def test_pr_is_open_parses_state(monkeypatch):
    class _Result:
        returncode = 0
        stdout = '{"state": "OPEN"}'

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Result())
    assert handoff._pr_is_open(7) is True


# ── cb-phase review — reviewer verdict routed through the FSM ────────────────

def _review(verdict: str):
    return handoff.main(["review", "st-1", "--task", "room-1", verdict])


def test_review_approve_advances_to_review_passed(store, monkeypatch):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    assert _review("--approve") == 0
    assert store.get_subtask("st-1").state == "review_passed"


def test_review_reject_advances_to_review_failed(store, monkeypatch):
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    assert _review("--reject") == 0
    sub = store.get_subtask("st-1")
    assert sub.state == "review_failed"
    assert sub.review_round == 1  # a reject is one failed review round


def test_review_illegal_from_verify_pending_writes_nothing(store, monkeypatch, capsys):
    # The `store` fixture leaves st-1 at verify_pending (no review yet).
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    assert _review("--approve") == 1
    assert store.get_subtask("st-1").state == "verify_pending"
    assert "review verdict rejected" in capsys.readouterr().err


def test_review_requires_an_explicit_verdict():
    # Mutually-exclusive --approve/--reject is required → argparse exits.
    with pytest.raises(SystemExit):
        handoff.main(["review", "st-1", "--task", "room-1"])
