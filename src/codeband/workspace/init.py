"""Workspace initialization — set up bare repo, worktrees, and shared directories.

Worktree layout follows the worker-pool model: each pool worker gets its
own directory keyed by `{role}-{framework}-{index}`. Reviewers get
scratch directories (no repo), coders get workspace-branch worktrees,
planners and plan reviewers get detached-HEAD read-only worktrees.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from codeband.config import CodebandConfig, Framework, FrameworkPool, PoolEntry
from codeband.workers import WorkerId, WorkerRole
from codeband.workspace.git import (
    WorkspaceError,
    branch_exists,
    clone_bare,
    create_worktree,
    list_known_branches,
    pin_gh_default_repo,
    refresh_worktree,
)

logger = logging.getLogger(__name__)

ClaudeSettingsProfile = Literal["coding", "planner", "plan_reviewer", "code_reviewer"]


@dataclass
class WorkspaceLayout:
    """Resolved workspace paths for the full (single-process) runtime."""

    root: Path
    bare_repo: Path
    worktrees_dir: Path
    scratch_dir: Path
    notes_dir: Path
    state_dir: Path

    # Per-worker paths keyed by worker_id string (e.g. "coder-claude_sdk-0")
    planner_worktrees: dict[str, Path] = field(default_factory=dict)
    plan_reviewer_worktrees: dict[str, Path] = field(default_factory=dict)
    coder_worktrees: dict[str, Path] = field(default_factory=dict)
    reviewer_scratch: dict[str, Path] = field(default_factory=dict)

    # Single-instance coordinator
    mergemaster_worktree: Path | None = None

    @property
    def all_worktrees(self) -> dict[str, Path]:
        """All worktree paths keyed by worker_id string."""
        result: dict[str, Path] = {}
        result.update(self.planner_worktrees)
        result.update(self.plan_reviewer_worktrees)
        result.update(self.coder_worktrees)
        if self.mergemaster_worktree is not None:
            result["mergemaster"] = self.mergemaster_worktree
        return result


def _iter_pool_worker_ids(role: WorkerRole, pool: FrameworkPool) -> list[WorkerId]:
    """Expand a `(role, FrameworkPool)` into explicit per-slot worker IDs."""
    ids: list[WorkerId] = []
    for framework in (Framework.CLAUDE_SDK, Framework.CODEX):
        entry: PoolEntry = pool.entry_for(framework)
        for i in range(entry.count):
            ids.append(WorkerId(role=role, framework=framework, index=i))
    return ids


def resolve_layout(config: CodebandConfig) -> WorkspaceLayout:
    """Compute workspace paths from config (does not create anything)."""
    root = Path(config.workspace.path)
    worktrees_dir = root / "worktrees"

    layout = WorkspaceLayout(
        root=root,
        bare_repo=root / "repo.git",
        worktrees_dir=worktrees_dir,
        scratch_dir=root / "scratch",
        notes_dir=root / "notes",
        state_dir=root / "state",
        mergemaster_worktree=worktrees_dir / "mergemaster",
    )

    agents = config.agents
    for wid in _iter_pool_worker_ids(WorkerRole.PLANNER, agents.planners):
        layout.planner_worktrees[str(wid)] = worktrees_dir / str(wid)
    for wid in _iter_pool_worker_ids(WorkerRole.PLAN_REVIEWER, agents.plan_reviewers):
        layout.plan_reviewer_worktrees[str(wid)] = worktrees_dir / str(wid)
    for wid in _iter_pool_worker_ids(WorkerRole.CODER, agents.coders):
        layout.coder_worktrees[str(wid)] = worktrees_dir / str(wid)
    for wid in _iter_pool_worker_ids(WorkerRole.REVIEWER, agents.reviewers):
        layout.reviewer_scratch[str(wid)] = root / "scratch" / str(wid)

    return layout


def _validate_workspace_root(root: Path) -> None:
    """Check that the workspace root directory can be created."""
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    if not os.access(ancestor, os.W_OK):
        raise RuntimeError(
            f"Cannot create workspace at '{root}': '{ancestor}' is not writable.\n"
            "Update 'workspace.path' in codeband.yaml to a writable location "
            "(e.g., '.codeband' for a project-relative directory)."
        )


def initialize_workspace(config: CodebandConfig) -> WorkspaceLayout:
    """Create the full workspace: clone repo, create worktrees and directories."""
    layout = resolve_layout(config)
    _validate_workspace_root(layout.root)

    # Create shared directories
    for d in [
        layout.notes_dir,
        layout.state_dir,
        layout.worktrees_dir,
        layout.scratch_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)
        logger.info("Created directory: %s", d)

    # Clone bare repo
    clone_bare(config.repo.url, layout.bare_repo)

    # Validate ``config.repo.branch`` exists in the cloned repo before any
    # worktree creation. Without this, ``git worktree add -b … <branch>``
    # fails with an opaque ``fatal: invalid reference: <branch>`` deep in
    # the orchestrator stack — useless for a user who just wants to know
    # they typed the wrong branch in codeband.yaml (commonly ``main`` vs
    # ``master``).
    if not branch_exists(layout.bare_repo, config.repo.branch):
        available = list_known_branches(layout.bare_repo)
        if not available:
            available_str = "<none — repo appears empty>"
        elif len(available) <= 10:
            available_str = ", ".join(available)
        else:
            available_str = ", ".join(available[:10]) + f", … ({len(available) - 10} more)"
        raise WorkspaceError(
            f"Branch '{config.repo.branch}' does not exist in {config.repo.url}. "
            f"Update `repo.branch` in codeband.yaml. "
            f"Available: {available_str}."
        )

    # Coder worktrees — workspace branch per worker
    prefix = config.workspace.worktree_prefix
    for worker_id_str, wt_path in layout.coder_worktrees.items():
        branch = f"{prefix}/{worker_id_str}/workspace"
        create_worktree(
            layout.bare_repo, wt_path, branch, base_branch=config.repo.branch,
        )
        pin_gh_default_repo(wt_path, config.repo.url)

    # Planner worktrees — detached HEAD (read-only), fast-forwarded to
    # origin/<branch> every session start (finding 16: create_worktree no-ops
    # on an existing worktree, so a reused planner otherwise plans on a
    # stale base; a dirty worktree fails loud instead of planning stale).
    for wt_path in layout.planner_worktrees.values():
        create_worktree(
            layout.bare_repo, wt_path, config.repo.branch, detach=True,
        )
        refresh_worktree(
            layout.bare_repo, wt_path, config.repo.branch, detach=True,
        )
        pin_gh_default_repo(wt_path, config.repo.url)

    # Plan-reviewer worktrees — detached HEAD (read-only), same refresh
    for wt_path in layout.plan_reviewer_worktrees.values():
        create_worktree(
            layout.bare_repo, wt_path, config.repo.branch, detach=True,
        )
        refresh_worktree(
            layout.bare_repo, wt_path, config.repo.branch, detach=True,
        )
        pin_gh_default_repo(wt_path, config.repo.url)

    # Reviewer scratch directories — no repo, just a workspace for gh calls
    for scratch_path in layout.reviewer_scratch.values():
        scratch_path.mkdir(parents=True, exist_ok=True)

    # Mergemaster worktree on main branch — ff-only refreshed at session
    # start; coder worktrees are deliberately NOT refreshed here (their
    # workspace branches are reset by prepare_task_branch at task
    # assignment, and the supervisor's crash recovery depends on their
    # uncommitted state surviving a restart).
    if layout.mergemaster_worktree is not None:
        create_worktree(
            layout.bare_repo, layout.mergemaster_worktree, config.repo.branch,
        )
        refresh_worktree(
            layout.bare_repo, layout.mergemaster_worktree, config.repo.branch,
        )
        pin_gh_default_repo(layout.mergemaster_worktree, config.repo.url)

    # Write role-specific Claude Code permission settings.
    for wt_path in layout.coder_worktrees.values():
        write_claude_settings(wt_path, profile="coding")
    for wt_path in layout.planner_worktrees.values():
        write_claude_settings(wt_path, profile="planner")
    for wt_path in layout.plan_reviewer_worktrees.values():
        write_claude_settings(wt_path, profile="plan_reviewer")
    for scratch_path in layout.reviewer_scratch.values():
        write_claude_settings(scratch_path, profile="coding")
    if layout.mergemaster_worktree is not None:
        write_claude_settings(layout.mergemaster_worktree, profile="coding")

    logger.info("Workspace initialized at %s", layout.root)
    return layout


@dataclass
class AgentWorkspaceLayout:
    """Resolved workspace paths for a single agent in distributed mode."""

    root: Path
    bare_repo: Path
    worktree: Path | None
    reviewer_workspace: Path | None
    state_dir: Path
    notes_dir: Path | None


_WORKTREE_ROLES = {"planner", "plan_reviewer", "coder", "mergemaster"}


def initialize_agent_workspace(
    config: CodebandConfig,
    worker_id: str,
    agent_role: str,
) -> AgentWorkspaceLayout:
    """Create workspace for a single agent (distributed mode).

    `worker_id` is the full `{role}-{framework}-{index}` string for
    pool workers, or the plain role name ("conductor", "mergemaster")
    for singletons. `agent_role` is one of: planner, plan_reviewer,
    coder, reviewer, conductor, mergemaster, watchdog.

    Each agent gets its own independent clone and worktree — no shared
    volumes required.
    """
    root = Path(config.workspace.path)
    _validate_workspace_root(root)
    bare_repo = root / "repo.git"
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    notes_dir: Path | None = None
    worktree: Path | None = None
    reviewer_workspace: Path | None = None

    if agent_role == "planner":
        clone_bare(config.repo.url, bare_repo)
        worktree = root / "worktrees" / worker_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        create_worktree(bare_repo, worktree, config.repo.branch, detach=True)
        # Session-start fast-forward (finding 16) — same rationale as the
        # local-mode path in initialize_workspace.
        refresh_worktree(bare_repo, worktree, config.repo.branch, detach=True)
        pin_gh_default_repo(worktree, config.repo.url)
        notes_dir = root / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
    elif agent_role == "plan_reviewer":
        clone_bare(config.repo.url, bare_repo)
        worktree = root / "worktrees" / worker_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        create_worktree(bare_repo, worktree, config.repo.branch, detach=True)
        refresh_worktree(bare_repo, worktree, config.repo.branch, detach=True)
        pin_gh_default_repo(worktree, config.repo.url)
    elif agent_role == "conductor":
        notes_dir = root / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
    elif agent_role == "reviewer":
        reviewer_workspace = root / "scratch" / worker_id
        reviewer_workspace.mkdir(parents=True, exist_ok=True)
    elif agent_role == "coder":
        clone_bare(config.repo.url, bare_repo)
        prefix = config.workspace.worktree_prefix
        branch = f"{prefix}/{worker_id}/workspace"
        worktree = root / "worktrees" / worker_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        create_worktree(bare_repo, worktree, branch, base_branch=config.repo.branch)
        pin_gh_default_repo(worktree, config.repo.url)
    elif agent_role == "mergemaster":
        clone_bare(config.repo.url, bare_repo)
        worktree = root / "worktrees" / "mergemaster"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        create_worktree(bare_repo, worktree, config.repo.branch)
        refresh_worktree(bare_repo, worktree, config.repo.branch)
        pin_gh_default_repo(worktree, config.repo.url)
    # watchdog needs no repo or worktree

    if worktree:
        profile = _claude_profile_for_agent_role(agent_role)
        if profile:
            write_claude_settings(worktree, profile=profile)
    if reviewer_workspace:
        write_claude_settings(reviewer_workspace, profile="coding")

    logger.info("Agent workspace initialized for %s at %s", worker_id, root)
    return AgentWorkspaceLayout(
        root=root,
        bare_repo=bare_repo,
        worktree=worktree,
        reviewer_workspace=reviewer_workspace,
        state_dir=state_dir,
        notes_dir=notes_dir,
    )


def _claude_profile_for_agent_role(agent_role: str) -> ClaudeSettingsProfile | None:
    """Map an agent role to the Claude permission profile it should use."""
    if agent_role in {"coder", "mergemaster"}:
        return "coding"
    if agent_role == "planner":
        return "planner"
    if agent_role == "plan_reviewer":
        return "plan_reviewer"
    return None


_CLAUDE_SETTINGS_PROFILES: dict[ClaudeSettingsProfile, dict] = {
    "coding": {
        "permissions": {
            "allow": [
                "Read",
                "Edit",
                "Write",
                "Glob",
                "Grep",
                "Bash(*)",
            ]
        }
    },
    "planner": {
        "permissions": {
            "allow": [
                "Read",
                "Glob",
                "Grep",
                "Bash(gh issue view:*)",
            ]
        }
    },
    "plan_reviewer": {
        "permissions": {
            "allow": [
                "Read",
                "Glob",
                "Grep",
            ]
        }
    },
    "code_reviewer": {
        "permissions": {
            "allow": [
                "Bash(gh pr view:*)",
                "Bash(gh pr diff:*)",
                "Bash(gh pr checks:*)",
                "Bash(gh pr comment:*)",
            ]
        }
    },
}


def write_claude_settings(path: Path, *, profile: ClaudeSettingsProfile = "coding") -> None:
    """Write .claude/settings.json for a workspace or git worktree.

    If ``path`` is inside a git worktree, also excludes ``.claude/`` via
    git's ``info/exclude`` so local permission files are never tracked.
    """
    claude_dir = path / ".claude"
    claude_dir.mkdir(exist_ok=True)

    settings = _CLAUDE_SETTINGS_PROFILES[profile]
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    _exclude_codeband_files_from_git(path)
    logger.debug("Wrote .claude/settings.json to %s using %s profile", path, profile)


_GIT_EXCLUDE_PATTERNS = [".claude/", ".codeband_state.json", "TASK.md"]


def _exclude_codeband_files_from_git(path: Path) -> None:
    """Exclude codeband working files via git's info/exclude.

    Prevents .claude/, .codeband_state.json, and TASK.md from being
    committed to task branches. For worktrees, info/exclude lives in
    the common dir (the bare repo), so all worktrees share the same
    exclude list.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return

    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    exclude_file = common_dir / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude_file.read_text() if exclude_file.exists() else ""
    existing_lines = set(existing.splitlines())
    new_lines = [p for p in _GIT_EXCLUDE_PATTERNS if p not in existing_lines]
    if new_lines:
        exclude_file.write_text(
            existing.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
        )
