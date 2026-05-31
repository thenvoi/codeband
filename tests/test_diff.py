"""Tests for `cb diff` — WorkerDiff computation and CLI wiring."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeband.cli import _resolve_worker_id, cli
from codeband.workspace.diff import DiffError, compute_worker_diff
from codeband.workspace.git import clone_bare, create_worktree


@pytest.fixture
def diff_setup(tmp_path: Path) -> dict:
    """Source repo on `main`, bare clone, and a coder worktree on a feature branch."""
    source = tmp_path / "source"
    source.mkdir()
    _git_init(source)
    (source / "README.md").write_text("# Test Repo\n")
    (source / "app.py").write_text("def f():\n    return 1\n")
    _git_commit_all(source, "initial commit")
    # Ensure the default branch is named `main` — matches Codeband's default.
    subprocess.run(
        ["git", "-C", str(source), "branch", "-M", "main"],
        check=True, capture_output=True,
    )

    bare = tmp_path / "bare.git"
    clone_bare(str(source), bare)

    worktree = tmp_path / "worktrees" / "coder-claude_sdk-0"
    create_worktree(bare, worktree, "codeband/coder-claude_sdk-0/workspace", base_branch="main")
    _git_config(worktree)

    return {"source": source, "bare": bare, "worktree": worktree}


def test_fresh_worktree_has_no_changes(diff_setup):
    wd = compute_worker_diff(diff_setup["worktree"], "coder-claude_sdk-0", "main")
    assert wd.has_changes is False
    assert wd.stat == ""
    assert wd.untracked == []
    assert wd.patch == ""
    assert wd.base_ref == "origin/main"


def test_committed_changes_show_in_stat(diff_setup):
    wt = diff_setup["worktree"]
    (wt / "app.py").write_text("def f():\n    return 2\n")
    _git_commit_all(wt, "update f")

    wd = compute_worker_diff(wt, "coder-claude_sdk-0", "main")
    assert wd.has_changes is True
    assert "app.py" in wd.stat


def test_uncommitted_and_untracked_are_surfaced(diff_setup):
    wt = diff_setup["worktree"]
    (wt / "app.py").write_text("def f():\n    return 3\n")  # unstaged
    (wt / "new_file.md").write_text("# new\n")              # untracked

    wd = compute_worker_diff(wt, "coder-claude_sdk-0", "main")
    assert wd.has_changes is True
    assert "app.py" in wd.stat
    assert "new_file.md" in wd.untracked


def test_patch_mode_includes_full_diff(diff_setup):
    wt = diff_setup["worktree"]
    (wt / "app.py").write_text("def f():\n    return 99\n")
    _git_commit_all(wt, "change return")

    wd = compute_worker_diff(wt, "coder-claude_sdk-0", "main", include_patch=True)
    assert "return 99" in wd.patch
    assert "diff --git" in wd.patch


def test_falls_back_to_local_branch_when_origin_ref_missing(tmp_path: Path):
    """If origin/<base> is absent but <base> exists locally, the fallback fires."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.txt").write_text("a\n")
    _git_commit_all(repo, "base")
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-M", "main"],
        check=True, capture_output=True,
    )
    # Create a divergent branch with a change so merge-base ≠ HEAD.
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-b", "feat"],
        check=True, capture_output=True,
    )
    (repo / "a.txt").write_text("a\nb\n")
    _git_commit_all(repo, "divergent")

    wd = compute_worker_diff(repo, "coder-claude_sdk-0", "main")
    assert wd.base_ref == "main"  # fallback path
    assert wd.has_changes is True
    assert "a.txt" in wd.stat


def test_missing_base_branch_raises(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "a.txt").write_text("a\n")
    _git_commit_all(repo, "base")

    with pytest.raises(DiffError):
        compute_worker_diff(repo, "coder-claude_sdk-0", "does-not-exist")


def test_missing_worktree_raises(tmp_path: Path):
    with pytest.raises(DiffError):
        compute_worker_diff(tmp_path / "nope", "coder-claude_sdk-0", "main")


# --- _resolve_worker_id ------------------------------------------------------

@pytest.fixture
def candidates(tmp_path: Path) -> dict[str, Path]:
    return {
        "coder-claude_sdk-0": tmp_path / "a",
        "coder-codex-0": tmp_path / "b",
        "mergemaster": tmp_path / "c",
    }


def test_resolve_exact(candidates):
    assert _resolve_worker_id("coder-claude_sdk-0", candidates) == "coder-claude_sdk-0"
    assert _resolve_worker_id("mergemaster", candidates) == "mergemaster"


def test_resolve_case_insensitive(candidates):
    assert _resolve_worker_id("Coder-Claude_SDK-0", candidates) == "coder-claude_sdk-0"
    assert _resolve_worker_id("MERGEMASTER", candidates) == "mergemaster"


def test_resolve_unique_substring(candidates):
    assert _resolve_worker_id("claude", candidates) == "coder-claude_sdk-0"
    assert _resolve_worker_id("codex", candidates) == "coder-codex-0"
    assert _resolve_worker_id("merge", candidates) == "mergemaster"


def test_resolve_ambiguous_raises(candidates):
    import click as click_mod
    with pytest.raises(click_mod.UsageError, match="ambiguous"):
        _resolve_worker_id("coder", candidates)


def test_resolve_no_match_raises(candidates):
    import click as click_mod
    with pytest.raises(click_mod.UsageError, match="No worker matches"):
        _resolve_worker_id("phantom", candidates)


# --- CLI wiring --------------------------------------------------------------

def test_cli_diff_missing_arg_lists_workers(diff_setup, tmp_path: Path, monkeypatch):
    project = _write_project_yaml(tmp_path, diff_setup["worktree"])
    monkeypatch.chdir(project)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(cli, ["diff"])
    assert result.exit_code == 1
    assert "Available workers" in result.stderr
    assert "coder-claude_sdk-0" in result.stderr


def test_cli_diff_resolves_and_renders(diff_setup, tmp_path: Path, monkeypatch):
    wt = diff_setup["worktree"]
    (wt / "app.py").write_text("def f():\n    return 42\n")
    _git_commit_all(wt, "change")

    project = _write_project_yaml(tmp_path, wt)
    monkeypatch.chdir(project)

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(cli, ["diff", "claude"])  # substring match
    assert result.exit_code == 0, result.output + result.stderr
    assert "coder-claude_sdk-0" in result.output
    assert "app.py" in result.output


# --- helpers -----------------------------------------------------------------

def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git_config(repo)


def _git_config(repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )


def _git_commit_all(repo: Path, msg: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", msg], check=True, capture_output=True,
    )


def _write_project_yaml(tmp_path: Path, worktree: Path) -> Path:
    """Create a minimal project directory with a codeband.yaml pointing at `worktree`.

    The CLI resolves the layout from config; we construct a config whose
    `workspace.path` places `coder-claude_sdk-0` at exactly `worktree`.
    """
    project = tmp_path / "project"
    project.mkdir()
    workspace_root = worktree.parent.parent  # worktrees/<id> → workspace
    (project / "codeband.yaml").write_text(
        "repo:\n"
        "  url: https://github.com/example/repo.git\n"
        "  branch: main\n"
        f"workspace:\n"
        f"  path: {workspace_root}\n"
        "agents:\n"
        "  conductor:\n"
        "    framework: claude_sdk\n"
        "    model: claude-sonnet-4-6\n"
        "  mergemaster: {}\n"
        "  planners:\n"
        "    claude_sdk:\n"
        "      count: 1\n"
        "      model: claude-sonnet-4-6\n"
        "  plan_reviewers:\n"
        "    codex:\n"
        "      count: 1\n"
        "      model: gpt-5.5\n"
        "  coders:\n"
        "    claude_sdk:\n"
        "      count: 1\n"
        "      model: claude-sonnet-4-6\n"
        "  reviewers:\n"
        "    claude_sdk:\n"
        "      count: 1\n"
        "      model: claude-sonnet-4-6\n"
        "  watchdog: {}\n"
    )
    return project
