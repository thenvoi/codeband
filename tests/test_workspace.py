"""Tests for codeband.workspace module."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codeband.workspace.git import (
    WorkspaceError,
    branch_name,
    clone_bare,
    commit_and_push,
    create_worktree,
    list_worktrees,
    pin_gh_default_repo,
    prepare_task_branch,
    refresh_worktree,
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

    @staticmethod
    def _default_branch(repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    @staticmethod
    def _commit_upstream(repo: Path, name: str) -> None:
        (repo / name).write_text("x")
        subprocess.run(
            ["git", "-C", str(repo), "add", "."], check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", f"add {name}"],
            check=True, capture_output=True,
        )

    def test_refresh_fast_forwards_stale_detached_worktree(
        self, source_repo: Path, tmp_path: Path,
    ):
        """Finding 16: a reused planner worktree (create_worktree no-ops on
        existing) must fast-forward to origin/<branch> at session start."""
        branch = self._default_branch(source_repo)
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)
        wt = tmp_path / "wt" / "planner-0"
        create_worktree(bare, wt, branch, detach=True)

        self._commit_upstream(source_repo, "new-upstream-file.txt")

        # "Next session": create is a no-op, so the worktree is stale…
        create_worktree(bare, wt, branch, detach=True)
        assert not (wt / "new-upstream-file.txt").exists()
        # …until the session-start refresh fast-forwards it.
        refresh_worktree(bare, wt, branch, detach=True)
        assert (wt / "new-upstream-file.txt").exists()

    def test_refresh_fast_forwards_branch_worktree(
        self, source_repo: Path, tmp_path: Path,
    ):
        """The mergemaster shape: a branch (non-detached) worktree ffs and
        stays on its branch."""
        branch = self._default_branch(source_repo)
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)
        wt = tmp_path / "wt" / "mergemaster"
        create_worktree(bare, wt, branch)

        self._commit_upstream(source_repo, "post-session-file.txt")
        refresh_worktree(bare, wt, branch)

        assert (wt / "post-session-file.txt").exists()
        head_branch = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert head_branch == branch  # still ON the branch, not detached

    def test_refresh_fails_loud_on_dirty_worktree(
        self, source_repo: Path, tmp_path: Path,
    ):
        """Local state that prevents the ff must fail loud — never silently
        plan against a stale base."""
        branch = self._default_branch(source_repo)
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)
        wt = tmp_path / "wt" / "planner-0"
        create_worktree(bare, wt, branch, detach=True)

        (wt / "README.md").write_text("local edit")
        self._commit_upstream(source_repo, "newer.txt")

        with pytest.raises(WorkspaceError, match="local changes"):
            refresh_worktree(bare, wt, branch, detach=True)
        # The dirty state is left intact for the human to inspect.
        assert (wt / "README.md").read_text() == "local edit"
        assert not (wt / "newer.txt").exists()

    def test_refresh_fails_loud_on_diverged_branch(
        self, source_repo: Path, tmp_path: Path,
    ):
        """A branch worktree whose local branch diverged cannot ff — loud."""
        branch = self._default_branch(source_repo)
        bare = tmp_path / "bare.git"
        clone_bare(str(source_repo), bare)
        wt = tmp_path / "wt" / "mergemaster"
        create_worktree(bare, wt, branch)

        # Local commit on the branch…
        (wt / "local.txt").write_text("local")
        subprocess.run(
            ["git", "-C", str(wt), "add", "."], check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(wt), "commit", "-m", "local commit"],
            check=True, capture_output=True,
        )
        # …and a different upstream commit: diverged.
        self._commit_upstream(source_repo, "upstream.txt")

        with pytest.raises(WorkspaceError, match="fast-forward"):
            refresh_worktree(bare, wt, branch)

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

    def test_verifier_workspace(self, source_repo: Path, tmp_path: Path):
        """Verifier gets isolated scratch space but no repo clone — a clean
        mirror of the code reviewer (gh-only, repo-less)."""
        from codeband.config import CodebandConfig, RepoConfig, WorkspaceConfig

        ws_root = tmp_path / "workspace"
        config = CodebandConfig(
            repo=RepoConfig(url=str(source_repo), branch="main"),
            workspace=WorkspaceConfig(path=str(ws_root)),
        )
        layout = initialize_agent_workspace(config, "verifier-codex-0", "verifier")

        assert not layout.bare_repo.exists()
        assert layout.worktree is None
        assert layout.verifier_workspace is not None
        assert layout.verifier_workspace.exists()
        assert (layout.verifier_workspace / ".claude" / "settings.json").exists()
        assert layout.reviewer_workspace is None
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
        # sample_config pins verifiers inert → no verifier scratch slots.
        assert layout.verifier_scratch == {}

    def test_layout_includes_verifier_scratch_when_active(self, tmp_path: Path):
        """An active verifier pool gets scratch dirs keyed by worker id —
        mirroring the reviewer scratch layout."""
        from codeband.config import (
            AgentsConfig,
            CodebandConfig,
            PoolEntry,
            RepoConfig,
            VerifiersConfig,
            WorkspaceConfig,
        )

        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/example/repo.git", branch="main"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=1),
                ),
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        )
        layout = resolve_layout(config)
        assert "verifier-claude_sdk-0" in layout.verifier_scratch
        assert "verifier-codex-0" in layout.verifier_scratch
        # Scratch lives under the shared scratch dir, like the reviewer's.
        assert layout.verifier_scratch["verifier-codex-0"].parent.name == "scratch"

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


class TestPinGhDefaultRepo:
    """The PR-destination guarantee depends on this helper running once per
    worktree at workspace setup. If it ever stops being called (or stops
    pinning correctly), an agent's plain `gh pr create` will silently target
    the upstream parent of a fork — the bug that opened PR #1469 against
    Delgan/loguru."""

    def test_skips_when_gh_not_installed(self, tmp_path: Path, monkeypatch):
        """No `gh` binary on PATH → no-op, no error."""
        monkeypatch.setattr("codeband.workspace.git.shutil.which", lambda _: None)
        called = []
        monkeypatch.setattr(
            "codeband.workspace.git.subprocess.run",
            lambda *a, **kw: called.append(a) or None,
        )
        pin_gh_default_repo(tmp_path, "https://github.com/owner/repo.git")
        assert called == []

    def test_skips_for_non_github_url(self, tmp_path: Path, monkeypatch):
        """gh only knows GitHub. SSH-to-GitLab, self-hosted, etc. → no-op."""
        monkeypatch.setattr(
            "codeband.workspace.git.shutil.which", lambda _: "/usr/bin/gh",
        )
        called = []
        monkeypatch.setattr(
            "codeband.workspace.git.subprocess.run",
            lambda *a, **kw: called.append(a) or None,
        )
        pin_gh_default_repo(tmp_path, "https://gitlab.example.com/group/proj.git")
        assert called == []

    def test_invokes_gh_repo_set_default_with_correct_slug(
        self, tmp_path: Path, monkeypatch,
    ):
        """The happy path: gh available + GitHub URL → exactly one
        `gh repo set-default <owner>/<name>` invocation in the worktree's cwd."""
        monkeypatch.setattr(
            "codeband.workspace.git.shutil.which", lambda _: "/usr/bin/gh",
        )
        captured = {}

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["cwd"] = kwargs.get("cwd")
            return _Result()

        monkeypatch.setattr("codeband.workspace.git.subprocess.run", fake_run)
        pin_gh_default_repo(tmp_path, "https://github.com/ofermend/loguru.git")
        assert captured["args"] == ["gh", "repo", "set-default", "ofermend/loguru"]
        assert captured["cwd"] == tmp_path

    def test_warns_but_does_not_raise_on_gh_failure(
        self, tmp_path: Path, monkeypatch,
    ):
        """A failed `gh repo set-default` (e.g., gh not authed) must NOT
        block workspace setup. Worktree creation should complete; the Coder
        will see a clear error at PR-creation time instead."""
        monkeypatch.setattr(
            "codeband.workspace.git.shutil.which", lambda _: "/usr/bin/gh",
        )

        def fake_run(args, **kwargs):
            raise subprocess.CalledProcessError(
                1, args, output="", stderr="not authenticated",
            )

        monkeypatch.setattr("codeband.workspace.git.subprocess.run", fake_run)
        # Must not raise.
        pin_gh_default_repo(tmp_path, "https://github.com/ofermend/loguru.git")
