"""Tests for codeband.workspace module."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codeband.workspace.git import (
    branch_name,
    clone_bare,
    commit_and_push,
    create_worktree,
    list_worktrees,
    remove_worktree,
)
from codeband.workspace.init import (
    _validate_workspace_root,
    initialize_agent_workspace,
    resolve_layout,
)


class TestBranchName:
    """Tests for branch name generation."""

    def test_basic(self):
        assert branch_name("codeband", "player-0", "add auth") == "codeband/player-0/add-auth"

    def test_truncation(self):
        long_slug = "a" * 100
        result = branch_name("bs", "p0", long_slug)
        # Should be truncated to 50 chars
        assert len(result.split("/")[-1]) <= 50

    def test_special_chars(self):
        result = branch_name("codeband", "player-0", "Fix Bug #123")
        assert result == "codeband/player-0/fix-bug-#123"


class TestGitOperations:
    """Integration tests for git workspace operations (require git)."""

    @pytest.fixture
    def source_repo(self, tmp_path: Path) -> Path:
        """Create a source repo with an initial commit."""
        repo = tmp_path / "source"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        # Create initial commit
        (repo / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Initial commit"],
            check=True, capture_output=True,
        )
        return repo

    def test_clone_bare(self, source_repo: Path, tmp_path: Path):
        """Clone a repo as bare."""
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)
        assert bare.exists()
        assert (bare / "HEAD").exists()

    def test_clone_bare_idempotent(self, source_repo: Path, tmp_path: Path):
        """Cloning twice doesn't error."""
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)
        clone_bare(str(source_repo), bare)  # should not raise

    def test_create_and_list_worktrees(self, source_repo: Path, tmp_path: Path):
        """Create worktrees and list them."""
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt1 = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt1, "codeband/player-0/test")
        assert wt1.exists()
        assert (wt1 / "README.md").exists()

        worktrees = list_worktrees(bare)
        assert any("player-0" in wt for wt in worktrees)

    def test_remove_worktree(self, source_repo: Path, tmp_path: Path):
        """Remove a worktree."""
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")
        remove_worktree(bare, wt)
        assert not wt.exists()

    def test_create_worktree_after_external_deletion(self, source_repo: Path, tmp_path: Path):
        """Recreate a worktree whose directory was externally deleted."""
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")
        assert wt.exists()

        # Simulate external deletion (e.g., container restart, manual rm)
        shutil.rmtree(wt)
        assert not wt.exists()

        # Should succeed — prune clears stale record, then re-adds
        create_worktree(bare, wt, "codeband/player-0/test")
        assert wt.exists()
        assert (wt / "README.md").exists()

    def test_commit_no_changes(self, source_repo: Path, tmp_path: Path):
        """Commit with no changes is a no-op."""
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")
        # Should not raise
        commit_and_push(wt, "No changes", remote="origin")


class TestWorktreeClaudeSettings:
    """Tests for .claude/settings.json written into worktrees."""

    @pytest.fixture
    def source_repo(self, tmp_path: Path) -> Path:
        """Create a source repo with an initial commit."""
        repo = tmp_path / "source"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (repo / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Initial commit"],
            check=True, capture_output=True,
        )
        return repo

    def test_player_worktrees_have_claude_settings(self, source_repo: Path, tmp_path: Path):
        """Player worktrees should have .claude/settings.json with git permissions."""
        import json

        from codeband.workspace.init import write_claude_settings

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")

        write_claude_settings(wt)

        settings_path = wt / ".claude" / "settings.json"
        assert settings_path.exists()

        settings = json.loads(settings_path.read_text())
        allowed = settings["permissions"]["allow"]
        assert "Bash(*)" in allowed
        assert "Read" in allowed
        assert "Edit" in allowed

    def test_plan_reviewer_worktrees_have_read_only_settings(
        self, source_repo: Path, tmp_path: Path
    ):
        """Plan reviewer worktrees should only allow read-only tools."""
        import json

        from codeband.workspace.init import write_claude_settings

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "plan-reviewer"
        create_worktree(bare, wt, "main", detach=True)

        write_claude_settings(wt, profile="plan_reviewer")

        settings_path = wt / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        allowed = settings["permissions"]["allow"]
        assert allowed == ["Read", "Glob", "Grep"]

    def test_code_reviewer_workspace_has_full_settings(self, tmp_path: Path):
        """Code reviewer uses coding profile (bypassPermissions handles restriction)."""
        import json

        from codeband.workspace.init import write_claude_settings

        reviewer_dir = tmp_path / "scratch" / "code_reviewer"
        reviewer_dir.mkdir(parents=True)

        write_claude_settings(reviewer_dir, profile="coding")

        settings_path = reviewer_dir / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        allowed = settings["permissions"]["allow"]
        assert "Bash(*)" in allowed

    def test_codeband_files_excluded_via_git_info(self, source_repo: Path, tmp_path: Path):
        """Codeband working files excluded via .git/info/exclude."""
        from codeband.workspace.init import write_claude_settings

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")

        write_claude_settings(wt)

        # .claude/, .codeband_state.json, TASK.md should all be ignored
        for pattern in [".claude/", ".codeband_state.json", "TASK.md"]:
            result = subprocess.run(
                ["git", "-C", str(wt), "check-ignore", pattern],
                capture_output=True, text=True,
            )
            assert result.returncode == 0, f"{pattern} not excluded"

        # Working tree stays clean
        result = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == ""


class TestAgentWorkspaceInit:
    """Tests for per-agent workspace initialization (distributed mode)."""

    @pytest.fixture
    def source_repo(self, tmp_path: Path) -> Path:
        """Create a source repo with an initial commit."""
        repo = tmp_path / "source"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (repo / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Initial commit"],
            check=True, capture_output=True,
        )
        return repo

    def test_player_workspace(self, source_repo: Path, tmp_path: Path):
        """Coder agent gets its own clone and worktree."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws_root = tmp_path / "workspace"
        config = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws_root)),
        )
        layout = initialize_agent_workspace(config, "coder-claude_sdk-0", "coder")

        assert layout.bare_repo.exists()
        assert layout.worktree is not None
        assert layout.worktree.exists()
        assert (layout.worktree / "README.md").exists()
        assert layout.state_dir.exists()
        assert layout.notes_dir is None

    def test_conductor_workspace(self, source_repo: Path, tmp_path: Path):
        """Conductor gets notes dir but no clone or worktree."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws_root = tmp_path / "workspace"
        config = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws_root)),
        )
        layout = initialize_agent_workspace(config, "conductor", "conductor")

        assert not layout.bare_repo.exists()
        assert layout.worktree is None
        assert layout.notes_dir is not None
        assert layout.notes_dir.exists()
        assert layout.state_dir.exists()
        assert layout.reviewer_workspace is None

    def test_code_reviewer_workspace(self, source_repo: Path, tmp_path: Path):
        """Code reviewer gets isolated scratch space but no repo clone."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws_root = tmp_path / "workspace"
        config = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws_root)),
        )
        layout = initialize_agent_workspace(config, "reviewer-claude_sdk-0", "reviewer")

        assert not layout.bare_repo.exists()
        assert layout.worktree is None
        assert layout.reviewer_workspace is not None
        assert layout.reviewer_workspace.exists()
        assert (layout.reviewer_workspace / ".claude" / "settings.json").exists()
        assert layout.notes_dir is None

    def test_mergemaster_workspace(self, source_repo: Path, tmp_path: Path):
        """Mergemaster gets a worktree on the main branch."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws_root = tmp_path / "workspace"
        config = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws_root)),
        )
        layout = initialize_agent_workspace(config, "mergemaster", "mergemaster")

        assert layout.bare_repo.exists()
        assert layout.worktree is not None
        assert layout.worktree.exists()
        assert (layout.worktree / "README.md").exists()
        assert layout.notes_dir is None

    def test_watchdog_workspace(self, source_repo: Path, tmp_path: Path):
        """Watchdog gets minimal workspace: no clone, no worktree, no notes."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws_root = tmp_path / "workspace"
        config = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws_root)),
        )
        layout = initialize_agent_workspace(config, "watchdog", "watchdog")

        assert not layout.bare_repo.exists()
        assert layout.worktree is None
        assert layout.notes_dir is None
        assert layout.state_dir.exists()

    def test_independent_clones(self, source_repo: Path, tmp_path: Path):
        """Two agents on separate workspace roots get independent clones."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws1 = tmp_path / "ws1"
        ws2 = tmp_path / "ws2"
        config1 = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws1)),
        )
        config2 = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws2)),
        )

        layout1 = initialize_agent_workspace(config1, "coder-claude_sdk-0", "coder")
        layout2 = initialize_agent_workspace(config2, "coder-claude_sdk-1", "coder")

        assert layout1.bare_repo != layout2.bare_repo
        assert layout1.worktree != layout2.worktree
        # Both have the repo content
        assert (layout1.worktree / "README.md").exists()
        assert (layout2.worktree / "README.md").exists()


class TestResolveLayout:
    """Tests for workspace layout resolution."""

    def test_layout_paths(self, sample_config):
        """Layout has expected paths keyed by worker_id."""
        layout = resolve_layout(sample_config)
        assert layout.bare_repo.name == "repo.git"
        # Default sample_config has 1 Claude + 1 Codex coder.
        assert "coder-claude_sdk-0" in layout.coder_worktrees
        assert "coder-codex-0" in layout.coder_worktrees
        assert "reviewer-claude_sdk-0" in layout.reviewer_scratch
        assert "reviewer-codex-0" in layout.reviewer_scratch
        assert "planner-claude_sdk-0" in layout.planner_worktrees
        assert "plan_reviewer-codex-0" in layout.plan_reviewer_worktrees
        assert layout.mergemaster_worktree is not None
        assert layout.notes_dir.name == "notes"
        assert layout.state_dir.name == "state"

    def test_all_worktrees_includes_mergemaster(self, sample_config):
        """all_worktrees dict includes coders, planners, and mergemaster."""
        layout = resolve_layout(sample_config)
        all_wt = layout.all_worktrees
        assert "coder-claude_sdk-0" in all_wt
        assert "coder-codex-0" in all_wt
        assert "planner-claude_sdk-0" in all_wt
        assert "plan_reviewer-codex-0" in all_wt
        assert "mergemaster" in all_wt


class TestValidateWorkspaceRoot:
    """Tests for workspace root validation."""

    def test_writable_path_passes(self, tmp_path: Path):
        """Validation passes for a writable (but non-existent) path."""
        target = tmp_path / "new_workspace"
        _validate_workspace_root(target)  # should not raise

    def test_read_only_ancestor_raises(self):
        """Validation raises RuntimeError when the nearest ancestor is not writable."""
        # /nonexistent walks up to /, which is read-only on macOS
        with pytest.raises(RuntimeError, match="not writable"):
            _validate_workspace_root(Path("/nonexistent_workspace"))
