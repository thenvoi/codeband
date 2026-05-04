"""Recovery context builder — assembles resume prompt from git state and task files."""

from __future__ import annotations

import logging
from pathlib import Path

from codeband.session.identity import WorkerIdentity
from codeband.workspace.git import get_worktree_summary, prepare_task_branch

logger = logging.getLogger(__name__)


def build_recovery_context(
    worker_id: str,
    worktree_path: Path,
    identity: WorkerIdentity,
    base_branch: str = "main",
    max_log_entries: int = 20,
    assignment: dict | None = None,
) -> str | None:
    """Build a recovery prompt for a restarting coder session.

    If assignment contains a task_branch, checks out that branch
    deterministically before gathering git state.

    Returns None when the worktree directory no longer exists (e.g. an earlier
    ``_recreate_worktree`` removed it but couldn't recreate it after a network
    failure). The agent then starts fresh from chat instead of crashing the
    supervisor's recovery path.
    """
    if not worktree_path.exists():
        logger.info(
            "Worktree %s missing for %s — skipping recovery context",
            worktree_path, worker_id,
        )
        return None

    assignment = assignment or {}
    task_branch = assignment.get("task_branch")
    pr_number = assignment.get("pr_number")

    # Resume the persisted task branch if available
    if task_branch:
        try:
            prepare_task_branch(
                worktree_path, task_branch, base_branch, resume=True,
            )
        except Exception as exc:
            logger.warning(
                "Failed to resume branch %s for %s: %s",
                task_branch, worker_id, exc,
            )

    summary = get_worktree_summary(worktree_path, max_log=max_log_entries)

    task_file = worktree_path / "TASK.md"
    task_text = task_file.read_text(encoding="utf-8").strip() if task_file.exists() else None

    session_num = identity.session_count + 1
    error_line = ""
    if identity.last_session_error:
        error_line = f"Your previous session ended due to: {identity.last_session_error}\n"

    branch_line = f"Your assigned branch: {task_branch}\n" if task_branch else ""
    pr_line = f"Your PR: #{pr_number}\n" if pr_number else ""

    task_section = f"### Current Task\n{task_text}" if task_text else (
        "### Current Task\nCheck chat history for your latest assignment."
    )

    return f"""\
## Session Recovery — You are resuming work

You are worker {worker_id}, session #{session_num}. {error_line}{branch_line}{pr_line}
{summary}

{task_section}

### Instructions
- Review the git history to understand what you've already done
- Check the chat for any messages you may have missed
- If a newer Conductor assignment names a different branch than this recovery
  context, treat this recovery context as stale. The latest Conductor assignment
  wins: reset/clean the worktree from the requested repo base branch and create
  the assigned branch from that base before editing.
- Continue your work from where you left off
- Do NOT redo work that's already committed
"""
