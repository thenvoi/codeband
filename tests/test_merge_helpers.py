"""Tests for Bors-style merge helper functions in workspace/git.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeband.workspace.git import (
    clone_bare,
    create_integration_branch,
    create_worktree,
    delete_branch,
    fast_forward_branch,
    merge_branch,
)


@pytest.fixture
def merge_setup(tmp_path: Path) -> dict:
    """Set up a source repo, bare clone, and mergemaster worktree for merge testing.

    Returns dict with keys: source, bare, worktree, and a helper to create feature branches.
    """
    # Create source repo with initial commit
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(source), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (source / "README.md").write_text("# Test Repo")
    (source / "main.py").write_text("def main(): pass\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "Initial commit"],
        check=True, capture_output=True,
    )

    # Bare clone
    bare = tmp_path / "bare.git"
    clone_bare(str(source), bare)

    # Mergemaster worktree on main
    worktree = tmp_path / "worktrees" / "mergemaster"
    create_worktree(bare, worktree, "main")
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )

    def make_feature_branch(name: str, filename: str, content: str):
        """Create a feature branch with a single file change in a temporary worktree."""
        ft_wt = tmp_path / "worktrees" / name
        create_worktree(bare, ft_wt, name)
        subprocess.run(
            ["git", "-C", str(ft_wt), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(ft_wt), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (ft_wt / filename).write_text(content)
        subprocess.run(
            ["git", "-C", str(ft_wt), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(ft_wt), "commit", "-m", f"Add {filename}"],
            check=True, capture_output=True,
        )
        return ft_wt

    return {
        "source": source,
        "bare": bare,
        "worktree": worktree,
        "make_feature_branch": make_feature_branch,
    }


class TestCreateIntegrationBranch:
    """Tests for creating temporary integration branches."""

    def test_creates_branch(self, merge_setup):
        """Integration branch is created from base."""
        wt = merge_setup["worktree"]
        create_integration_branch(wt, "integration/test-001", base="main")

        result = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        )
        assert result.stdout.strip() == "integration/test-001"

    def test_branch_starts_from_base(self, merge_setup):
        """Integration branch has same HEAD as base."""
        wt = merge_setup["worktree"]

        main_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "main"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        create_integration_branch(wt, "integration/test-002", base="main")

        branch_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        assert branch_sha == main_sha


class TestMergeBranch:
    """Tests for merge_branch helper."""

    def test_merge_success(self, merge_setup):
        """Clean merge returns (True, output)."""
        merge_setup["make_feature_branch"]("feat/auth", "auth.py", "def auth(): pass\n")
        wt = merge_setup["worktree"]

        create_integration_branch(wt, "integration/test", base="main")
        success, output = merge_branch(wt, "feat/auth")

        assert success is True
        assert (wt / "auth.py").exists()

    def test_merge_conflict_returns_false(self, merge_setup):
        """Conflicting merge returns (False, conflict_info) and aborts."""
        # Create two branches that modify the same file
        merge_setup["make_feature_branch"](
            "feat/a", "main.py", "def main(): return 'A'\n"
        )
        merge_setup["make_feature_branch"](
            "feat/b", "main.py", "def main(): return 'B'\n"
        )

        wt = merge_setup["worktree"]
        create_integration_branch(wt, "integration/conflict-test", base="main")

        # First merge succeeds
        success_a, _ = merge_branch(wt, "feat/a")
        assert success_a is True

        # Second merge conflicts
        success_b, output = merge_branch(wt, "feat/b")
        assert success_b is False
        assert "main.py" in output

        # Verify merge was aborted (no .git/MERGE_HEAD)
        merge_head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "MERGE_HEAD"],
            capture_output=True, text=True,
        )
        assert merge_head.returncode != 0  # MERGE_HEAD should not exist


class TestFastForwardBranch:
    """Tests for fast_forward_branch helper."""

    def test_fast_forward_succeeds(self, merge_setup):
        """Fast-forward main to integration tip."""
        merge_setup["make_feature_branch"]("feat/ff", "ff.py", "def ff(): pass\n")
        wt = merge_setup["worktree"]

        create_integration_branch(wt, "integration/ff-test", base="main")
        merge_branch(wt, "feat/ff")

        integration_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        fast_forward_branch(wt, target="main", source="integration/ff-test")

        main_sha = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "main"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        assert main_sha == integration_sha


class TestDeleteBranch:
    """Tests for branch deletion."""

    def test_delete_branch(self, merge_setup):
        """Branch is deleted after cleanup."""
        wt = merge_setup["worktree"]
        create_integration_branch(wt, "integration/to-delete", base="main")

        # Switch back to main before deleting
        subprocess.run(
            ["git", "-C", str(wt), "checkout", "main"],
            check=True, capture_output=True,
        )
        delete_branch(wt, "integration/to-delete")

        # Verify branch no longer exists
        result = subprocess.run(
            ["git", "-C", str(wt), "branch", "--list", "integration/to-delete"],
            check=True, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""
