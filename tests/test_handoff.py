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
    monkeypatch.setattr(handoff, "_git_tree_clean", lambda worktree: True)
    monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: 0)
    return store


def _run():
    return handoff.main(["verify", "st-1", "--task", "room-1", "--pr", "42"])


def test_verify_success_advances_to_review_pending(patch_gates):
    store = patch_gates
    assert _run() == 0
    assert store.get_subtask("st-1").state == "review_pending"


def test_verify_fails_on_dirty_tree(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_git_tree_clean", lambda worktree: False)
    assert _run() != 0
    assert store.get_subtask("st-1").state == "verify_pending"


def test_verify_fails_on_non_open_pr(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: False)
    assert _run() != 0
    assert store.get_subtask("st-1").state == "verify_pending"


def test_verify_fails_on_failing_verify_command(patch_gates, monkeypatch):
    store = patch_gates
    monkeypatch.setattr(handoff, "_run_verify_command", lambda cmd, cwd: 1)
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


def test_git_tree_clean_reads_porcelain(monkeypatch, tmp_path):
    calls = {}

    class _Result:
        returncode = 0
        stdout = "  \n"

    def _fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(handoff.subprocess, "run", _fake_run)
    assert handoff._git_tree_clean(tmp_path) is True
    assert calls["cmd"][:2] == ["git", "-C"]


def test_pr_is_open_parses_state(monkeypatch):
    class _Result:
        returncode = 0
        stdout = '{"state": "OPEN"}'

    monkeypatch.setattr(handoff.subprocess, "run", lambda cmd, **kw: _Result())
    assert handoff._pr_is_open(7) is True
