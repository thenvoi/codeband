"""Integration tests for the FSM lifecycle gap fix (WS1).

Tests that ``cb-phase verify`` works from ``in_progress`` (first submit),
``review_failed`` (rework), and ``verify_pending`` (retry), walking only
legal FSM edges. Also tests cap escalation from both entry paths.

Uses the same real-git + real-sqlite pattern as ``test_rails_integration.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeband.cli import handoff
from codeband.config import AgentsConfig, CodebandConfig, RepoConfig, WorkspaceConfig
from codeband.state import StateStore
from codeband.state.fsm import MAX_REVIEW_ROUNDS, transition


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True, capture_output=True, text=True,
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial commit")
    return path


def _new_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state" / "orchestration.db")


def _project(tmp_path, *, verify_command=None):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    workspace = tmp_path / "workspace"
    cfg = CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git", branch="main"),
        agents=AgentsConfig(handoff_verify_command=verify_command),
        workspace=WorkspaceConfig(path=str(workspace)),
    )
    cfg.to_yaml(project_dir / "codeband.yaml")
    store = StateStore(workspace / "state" / "orchestration.db")
    store.create_task("room-1", "demo", "room-1")
    return project_dir, store


def _run_verify(project_dir: Path, worktree: Path) -> int:
    return handoff.main([
        "verify", "st-1",
        "--task", "room-1",
        "--pr", "42",
        "--worktree", str(worktree),
        "--project-dir", str(project_dir),
    ])


def _seed_in_progress(store: StateStore) -> None:
    transition("st-1", "room-1", "assigned", caller_role="conductor", store=store)
    transition("st-1", "room-1", "in_progress", caller_role="coder", store=store)


def _seed_review_failed(store: StateStore) -> None:
    _seed_in_progress(store)
    transition("st-1", "room-1", "verify_pending", caller_role="coder", store=store)
    transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
    transition("st-1", "room-1", "review_failed", caller_role="reviewer", store=store)


class TestVerifyFromInProgress:
    """``cb-phase verify`` from ``in_progress`` (first submit)."""

    def test_happy_path_advances_to_review_pending(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path, verify_command="exit 0")
        _seed_in_progress(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)

        assert _run_verify(project_dir, repo) == 0
        assert store.get_subtask("st-1").state == "review_pending"

    def test_dirty_tree_rejects_at_verify_pending(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path)
        _seed_in_progress(store)
        repo = _init_repo(tmp_path / "repo")
        (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        assert _run_verify(project_dir, repo) == handoff.EXIT_DIRTY_TREE
        assert store.get_subtask("st-1").state == "verify_pending"

    def test_no_pr_rejects_at_verify_pending(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path)
        _seed_in_progress(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: False)

        assert _run_verify(project_dir, repo) == handoff.EXIT_NO_PR
        assert store.get_subtask("st-1").state == "verify_pending"

    def test_verify_command_failure_rejects(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path, verify_command="exit 1")
        _seed_in_progress(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)

        assert _run_verify(project_dir, repo) == handoff.EXIT_VERIFY_FAILED
        assert store.get_subtask("st-1").state == "verify_pending"


class TestVerifyFromReviewFailed:
    """``cb-phase verify`` from ``review_failed`` (rework)."""

    def test_rework_advances_to_review_pending(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path, verify_command="exit 0")
        _seed_review_failed(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)

        assert _run_verify(project_dir, repo) == 0
        assert store.get_subtask("st-1").state == "review_pending"

    def test_rework_gate_rejection_lands_at_verify_pending(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path)
        _seed_review_failed(store)
        repo = _init_repo(tmp_path / "repo")
        (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

        assert _run_verify(project_dir, repo) == handoff.EXIT_DIRTY_TREE
        assert store.get_subtask("st-1").state == "verify_pending"


class TestVerifyAttemptCapFromInProgress:
    """Verify-attempt cap fires correctly when entering from ``in_progress``."""

    def test_cap_fires_after_walk(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path, verify_command="exit 0")
        _seed_in_progress(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)
        monkeypatch.setattr(handoff, "_max_verify_attempts", lambda project_dir: 3)

        for _ in range(3):
            store.increment_verify_attempts("st-1")

        assert _run_verify(project_dir, repo) == handoff.EXIT_CAP_REACHED
        assert store.get_subtask("st-1").state == "blocked"


class TestReviewRoundCapEscalation:
    """Review-round cap during ``review_failed → in_progress`` walk."""

    def test_review_cap_escalates_to_blocked(self, tmp_path, monkeypatch, capsys):
        project_dir, store = _project(tmp_path, verify_command="exit 0")
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)

        _seed_review_failed(store)
        for _ in range(MAX_REVIEW_ROUNDS - 1):
            transition("st-1", "room-1", "in_progress", caller_role="coder", store=store)
            transition("st-1", "room-1", "verify_pending", caller_role="coder", store=store)
            transition("st-1", "room-1", "review_pending", caller_role="coder", store=store)
            transition("st-1", "room-1", "review_failed", caller_role="reviewer", store=store)

        assert store.get_subtask("st-1").review_round == MAX_REVIEW_ROUNDS
        assert store.get_subtask("st-1").state == "review_failed"

        assert _run_verify(project_dir, repo) == handoff.EXIT_CAP_REACHED
        assert store.get_subtask("st-1").state == "blocked"
        err = capsys.readouterr().err
        assert "BLOCKED [review_cap_reached]" in err


class TestVerifyCountDurability:
    """Verify-attempt count survives store reopen."""

    def test_count_survives_store_reopen(self, tmp_path, monkeypatch):
        project_dir, store = _project(tmp_path, verify_command="exit 1")
        _seed_in_progress(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)

        assert _run_verify(project_dir, repo) != 0
        assert store.get_subtask("st-1").verify_attempts == 1

        db_path = store.db_path
        del store
        reopened = StateStore(db_path)
        assert reopened.get_subtask("st-1").verify_attempts == 1
        assert reopened.get_subtask("st-1").state == "verify_pending"


class TestNonCapTransitionErrorNotMisclassified:
    """A non-cap ``InvalidTransitionError`` on ``review_failed → in_progress``
    must NOT escalate to ``blocked`` with ``review_cap_reached``."""

    def test_non_cap_error_does_not_block(self, tmp_path, monkeypatch, capsys):
        from unittest.mock import patch as mock_patch

        from codeband.state.fsm import InvalidTransitionError as ITE

        project_dir, store = _project(tmp_path, verify_command="exit 0")
        _seed_review_failed(store)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)

        assert store.get_subtask("st-1").review_round == 1
        assert store.get_subtask("st-1").review_round < MAX_REVIEW_ROUNDS

        original_transition = handoff.transition

        def _failing_transition(subtask_id, task_id, new_state, **kwargs):
            if new_state == "in_progress":
                raise ITE("concurrent state mutation (simulated)")
            return original_transition(subtask_id, task_id, new_state, **kwargs)

        with mock_patch.object(handoff, "transition", side_effect=_failing_transition):
            exit_code = _run_verify(project_dir, repo)

        assert exit_code == 1
        assert exit_code != handoff.EXIT_CAP_REACHED
        sub = store.get_subtask("st-1")
        assert sub.state == "review_failed"
        assert sub.state != "blocked"
        err = capsys.readouterr().err
        assert "review_cap_reached" not in err
        assert "transition rejected" in err


class TestInvalidEntryState:
    """``cb-phase verify`` from an unexpected state prints error and exits 1."""

    def test_planned_state_rejected(self, tmp_path, monkeypatch, capsys):
        project_dir, store = _project(tmp_path)
        store.ensure_subtask("st-1", "room-1")
        repo = _init_repo(tmp_path / "repo")

        assert _run_verify(project_dir, repo) == 1
        err = capsys.readouterr().err
        assert "not a valid entry state" in err
