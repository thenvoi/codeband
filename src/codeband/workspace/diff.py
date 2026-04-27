"""Compute a worker's diff against the base branch fork-point.

Two callers:
- Local mode: each git op runs as a host subprocess in the worker's worktree.
- Distributed mode: each git op runs via ``docker compose exec`` inside an
  agent container that has the shared worktrees volume mounted.

The git logic is identical; only the way commands are executed differs.
``compute_worker_diff_with_runner`` takes a ``GitRunner`` callable so the
two modes share one implementation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class DiffError(Exception):
    """Raised when a diff cannot be computed (missing worktree, missing base ref)."""


# A GitRunner runs ``git <args>`` in some pre-bound working directory and
# returns stdout. Should raise ``DiffError`` on non-zero exit.
GitRunner = Callable[[list[str]], str]


@dataclass
class WorkerDiff:
    worker_id: str
    worktree: Path
    base_branch: str
    base_ref: str
    merge_base: str
    has_changes: bool
    stat: str = ""
    patch: str = ""
    untracked: list[str] = field(default_factory=list)


def compute_worker_diff(
    worktree: Path,
    worker_id: str,
    base_branch: str,
    *,
    include_patch: bool = False,
) -> WorkerDiff:
    """Compute diff using a host-subprocess git runner bound to ``worktree``."""
    if not worktree.exists():
        raise DiffError(f"Worktree does not exist: {worktree}")

    runner: GitRunner = lambda args: _run_local(args, worktree)  # noqa: E731
    return compute_worker_diff_with_runner(
        runner,
        worker_id=worker_id,
        worktree=worktree,
        base_branch=base_branch,
        include_patch=include_patch,
    )


def compute_worker_diff_with_runner(
    runner: GitRunner,
    *,
    worker_id: str,
    worktree: Path,
    base_branch: str,
    include_patch: bool = False,
) -> WorkerDiff:
    """Compute diff using a caller-supplied git runner.

    ``worktree`` is recorded on the result for display; the runner already
    knows where to execute. In distributed mode pass the in-container path
    (e.g. ``/workspace/worktrees/coder-claude_sdk-0``) — it is never opened
    on the host, only printed.
    """
    base_ref = _resolve_base_ref_with_runner(runner, base_branch)
    merge_base = runner(["merge-base", "HEAD", base_ref]).strip()
    head = runner(["rev-parse", "HEAD"]).strip()

    if not merge_base:
        raise DiffError(f"Could not compute merge-base between HEAD and {base_ref}")

    stat = runner(["diff", "--stat", merge_base]).strip()
    untracked = [
        line for line in runner(
            ["ls-files", "--others", "--exclude-standard"],
        ).splitlines() if line
    ]

    patch = ""
    if include_patch:
        patch = runner(["diff", merge_base])

    has_changes = bool(stat) or bool(untracked) or head != merge_base

    return WorkerDiff(
        worker_id=worker_id,
        worktree=worktree,
        base_branch=base_branch,
        base_ref=base_ref,
        merge_base=merge_base,
        has_changes=has_changes,
        stat=stat,
        patch=patch,
        untracked=untracked,
    )


def _resolve_base_ref_with_runner(runner: GitRunner, base_branch: str) -> str:
    """Prefer origin/<base>, fall back to <base>."""
    for candidate in (f"origin/{base_branch}", base_branch):
        try:
            runner(["rev-parse", "--verify", candidate])
            return candidate
        except DiffError:
            continue
    raise DiffError(
        f"Base branch ref '{base_branch}' not found (tried 'origin/{base_branch}' "
        f"and '{base_branch}')."
    )


def _run_local(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd, capture_output=True, text=True, check=True, timeout=120,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise DiffError(
            f"git {' '.join(args)} failed: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise DiffError(f"git {' '.join(args)} timed out") from e
