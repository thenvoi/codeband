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
    prepare_task_branch,
    remove_worktree,
)
from codeband.workspace.init import (
    _validate_workspace_root,
    initialize_agent_workspace,
    initialize_workspace,
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

    def test_prepare_task_branch_recreates_checked_out_stale_branch(
        self, source_repo: Path, tmp_path: Path,
    ):
        """Fresh task prep must discard stale task state before coding starts.

        Regression: a coder could resume in a stale checked-out task branch
        with uncommitted and untracked files. ``git branch -D`` then failed
        because the task branch was checked out, and untracked stale files
        survived the reset.
        """
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        default = subprocess.run(
            ["git", "-C", str(source_repo), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        wt = tmp_path / "wt" / "coder-codex-0"
        create_worktree(
            bare, wt, "codeband/coder-codex-0/workspace",
            base_branch=default,
        )

        task_branch = "codeband/coder-codex-0/test-redact-helper"
        prepare_task_branch(wt, task_branch, default)

        # Simulate stale local state from a previous interrupted assignment.
        (wt / "README.md").write_text("# Dirty\n")
        (wt / "stale-test.py").write_text("stale = True\n")

        prepare_task_branch(wt, task_branch, default)

        current_branch = subprocess.run(
            ["git", "-C", str(wt), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        base = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", f"origin/{default}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        assert current_branch == task_branch
        assert head == base
        assert status == ""
        assert not (wt / "stale-test.py").exists()

    def test_recreate_worktree_preserves_when_base_ref_missing(
        self, source_repo: Path, tmp_path: Path,
    ):
        """If the base ref cannot be verified, recreate must not destroy the worktree.

        Regression: previously ``_recreate_worktree`` did
        ``remove_worktree`` -> ``git fetch origin`` -> ``create_worktree``.
        When fetch failed (e.g. network down), the worktree was already
        gone and never restored, putting the supervisor in a tight crash
        loop.
        """
        from codeband.workspace.git import WorkspaceError, _recreate_worktree

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")
        assert wt.exists()

        # Recreate against a base branch that exists in NEITHER origin nor
        # local refs simulates the "fetch couldn't bring in what we need"
        # case. Recreate should raise rather than silently deleting.
        with pytest.raises(WorkspaceError):
            _recreate_worktree(wt, base_branch="branch-that-does-not-exist")

        assert wt.exists(), (
            "worktree was deleted when recreate could not proceed — "
            "this is the data-loss bug"
        )

    def test_recreate_worktree_tolerates_fetch_failure_with_cached_base(
        self, source_repo: Path, tmp_path: Path,
    ):
        """Recreate should still succeed when fetch fails but base ref is cached."""
        from codeband.workspace.git import _recreate_worktree

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        # Discover the actual default branch name.
        default = subprocess.run(
            ["git", "-C", str(source_repo), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        wt = tmp_path / "wt" / "player-0"
        create_worktree(bare, wt, "codeband/player-0/test")

        # Break the origin so fetch fails, but origin/<default> is already
        # cached in the bare repo from clone_bare.
        subprocess.run(
            ["git", "-C", str(bare), "remote", "set-url", "origin",
             "/nonexistent/path/to/repo.git"],
            check=True, capture_output=True,
        )

        # Should recreate successfully using the cached ref.
        _recreate_worktree(wt, base_branch=default)
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

    def test_branch_exists_finds_default_branch(
        self, source_repo: Path, tmp_path: Path
    ):
        """Knows the source repo's default branch is present in the bare clone."""
        from codeband.workspace.git import branch_exists

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        # Discover the actual default branch (master or main, depending on
        # the host's git config).
        default = subprocess.run(
            ["git", "-C", str(source_repo), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        assert branch_exists(bare, default) is True

    def test_branch_exists_returns_false_for_missing(
        self, source_repo: Path, tmp_path: Path
    ):
        from codeband.workspace.git import branch_exists

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        assert branch_exists(bare, "definitely-not-a-real-branch") is False

    def test_list_known_branches_returns_default(
        self, source_repo: Path, tmp_path: Path
    ):
        from codeband.workspace.git import list_known_branches

        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)

        default = subprocess.run(
            ["git", "-C", str(source_repo), "branch", "--show-current"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        branches = list_known_branches(bare)
        assert default in branches


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


class TestInitializeWorkspaceBranchValidation:
    """Pre-validate ``config.repo.branch`` against the cloned repo so the
    user gets a clean message naming codeband.yaml — not a raw
    ``git worktree add … fatal: invalid reference: main`` traceback.
    """

    @pytest.fixture
    def source_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "source"
        repo.mkdir()
        # Force ``master`` so the test is deterministic regardless of the
        # host's ``init.defaultBranch`` setting.
        subprocess.run(
            ["git", "init", "-b", "master", str(repo)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (repo / "README.md").write_text("# Test")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Initial"],
            check=True, capture_output=True,
        )
        return repo

    def _config_for(self, source_repo: Path, branch: str, tmp_path: Path):
        from codeband.config import (
            AgentsConfig,
            CodebandConfig,
            ConductorConfig,
            FrameworkPool,
            MergemasterConfig,
            PlanReviewersConfig,
            PoolEntry,
            RepoConfig,
            ReviewersConfig,
            WatchdogConfig,
            WorkspaceConfig,
        )
        return CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch=branch),
            agents=AgentsConfig(
                conductor=ConductorConfig(model="claude-sonnet-4-6"),
                mergemaster=MergemasterConfig(),
                planners=FrameworkPool(
                    claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                ),
                plan_reviewers=PlanReviewersConfig(),
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                ),
                reviewers=ReviewersConfig(
                    claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                ),
                watchdog=WatchdogConfig(),
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        )

    def test_invalid_branch_raises_friendly_error(
        self, source_repo: Path, tmp_path: Path
    ):
        from codeband.workspace.git import WorkspaceError

        config = self._config_for(source_repo, "main", tmp_path)
        with pytest.raises(WorkspaceError) as exc:
            initialize_workspace(config)

        msg = str(exc.value)
        assert "main" in msg, "must name the configured branch"
        assert "codeband.yaml" in msg, "must point user at the config field"
        assert "master" in msg, "must list the actual branch(es) so user knows what to set"
        # Negative: the raw git CLI noise should NOT be in the friendly message.
        assert "git worktree add" not in msg
        assert "fatal: invalid reference" not in msg

    def test_valid_branch_succeeds(self, source_repo: Path, tmp_path: Path):
        """Sanity-check: a correctly configured branch still works."""
        config = self._config_for(source_repo, "master", tmp_path)
        # Should not raise.
        initialize_workspace(config)


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
