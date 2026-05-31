"""``cb-phase`` — the verify-gated handoff CLI (RFC Workstream 3).

This is the enforcement seam. Coding agents (Claude *and* Codex) request a
phase advance by shelling out to ``cb-phase verify``; the effect only happens
if every gate passes, regardless of what the Conductor intended.

    cb-phase verify <subtask_id> --task <task_id> --pr <n> [--worktree <path>]

Gate sequence:

0. **Verify-attempt cap.** If the subtask has already had
   ``agents.max_verify_attempts`` (default :data:`MAX_VERIFY_ATTEMPTS`) attempts
   *rejected*, escalate it ``verify_pending → blocked`` and exit non-zero —
   before running any gate, so the escalating call writes nothing but the
   ``blocked`` transition.
1. ``git -C <worktree> status --porcelain`` must be empty (clean tree).
2. ``gh pr view <n> --json state`` must report ``OPEN``.
3. If ``agents.handoff_verify_command`` is configured, run it in the worktree;
   exit 0 is required.
4. On success, ``fsm.transition(..., "review_pending", caller_role="coder")``.

Any failed gate increments the subtask's durable ``verify_attempts`` count,
prints a clear message and exits non-zero; a *success* never increments. The
count is cumulative over the subtask's life (never reset on rework), so a coder
cannot game the cap by bouncing through review. This bounds a *productive*
verify loop — the coder commits real code each attempt, so git HEAD advances and
the watchdog's stall cap by design never fires — mirroring the review-round cap
in ``state/fsm.py`` on a different loop. This module imports **no Band SDK and no
asyncio** — it is a fast, pure subprocess callable by both frameworks.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from codeband.config import load_config
from codeband.state import StateStore
from codeband.state.fsm import InvalidTransitionError, transition

# Per-subtask verify-attempt cap default (RFC two-level model). After this many
# *rejected* ``cb-phase verify`` attempts, the subtask is escalated
# ``verify_pending → blocked`` instead of being allowed another attempt. This is
# the *default*; ``config.AgentsConfig.max_verify_attempts`` (read per run) may
# override it — mirroring how ``fsm.MAX_REVIEW_ROUNDS`` /
# ``config.AgentsConfig.max_review_rounds`` wire the review-round cap.
#
# It is a DISTINCT mechanism from both the watchdog's ``max_phase_visits`` stall
# cap (which fires on the *absence* of git-HEAD progress, so it never trips on a
# verify loop that commits real code each attempt) and the FSM's
# ``MAX_REVIEW_ROUNDS`` (which counts ``review_failed`` re-entries — a subtask
# stuck failing *verify* never reaches ``review_failed`` at all).
MAX_VERIFY_ATTEMPTS = 20


def _resolve_store(project_dir: Path) -> StateStore:
    """Build the StateStore from the project's codeband.yaml workspace path.

    Mirrors ``kickoff.py`` / ``runner.py``: the DB lives at
    ``{workspace_path}/state/orchestration.db``.
    """
    config = load_config(project_dir)
    workspace_path = Path(config.workspace.path)
    if not workspace_path.is_absolute():
        workspace_path = project_dir / workspace_path
    store = StateStore(workspace_path / "state" / "orchestration.db")
    return store


def _verify_command(project_dir: Path) -> str | None:
    """Return the configured ``agents.handoff_verify_command`` (or ``None``)."""
    config = load_config(project_dir)
    return config.agents.handoff_verify_command


def _max_verify_attempts(project_dir: Path) -> int:
    """Return the configured per-subtask verify-attempt cap.

    Reads ``agents.max_verify_attempts`` (default
    :data:`MAX_VERIFY_ATTEMPTS`) — the cap the handoff gate enforces.
    """
    return load_config(project_dir).agents.max_verify_attempts


def _git_tree_clean(worktree: Path) -> bool:
    """Return ``True`` if ``git status --porcelain`` is empty in ``worktree``."""
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return result.stdout.strip() == ""


def _pr_is_open(pr_number: int) -> bool:
    """Return ``True`` if ``gh pr view <n>`` reports state ``OPEN``."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "state"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return json.loads(result.stdout).get("state") == "OPEN"
    except (ValueError, AttributeError):
        return False


def _run_verify_command(command: str, cwd: Path) -> int:
    """Run the configured verify command in ``cwd``; return its exit code."""
    result = subprocess.run(command, shell=True, cwd=str(cwd))
    return result.returncode


def _reject(store: StateStore, subtask_id: str, message: str) -> int:
    """Record one rejected verify attempt and return the failure exit code.

    Bumps the subtask's durable ``verify_attempts`` (this is the *only* place a
    rejection is counted), prints ``message`` to stderr, and returns ``1``. No
    ``transition_log`` row is written — a rejection is a non-event for the FSM;
    only the cumulative attempt count advances.
    """
    store.increment_verify_attempts(subtask_id)
    print(message, file=sys.stderr)
    return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    worktree = Path(args.worktree).resolve()
    store = _resolve_store(project_dir)

    # Gate 0 — verify-attempt cap. If this subtask has already burned its budget
    # of rejected attempts, escalate to ``blocked`` and stop *before* running any
    # gate, so the escalating call writes nothing but the ``blocked`` transition
    # (no further increment). The count is read from durable state, so the cap
    # holds across a crash/reopen mid-loop. Mirrors the FSM review-round cap.
    max_attempts = _max_verify_attempts(project_dir)
    subtask = store.get_subtask(args.subtask_id)
    attempts = subtask.verify_attempts if subtask is not None else 0
    if attempts >= max_attempts:
        try:
            transition(
                args.subtask_id,
                args.task,
                "blocked",
                caller_role="coder",
                reason=f"verify-attempt cap {max_attempts} reached",
                store=store,
            )
        except InvalidTransitionError as exc:
            print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
            return 1
        print(
            f"cb-phase: verify-attempt cap {max_attempts} reached for subtask "
            f"{args.subtask_id} ({attempts} rejected attempts); escalating to "
            f"human — subtask → blocked.",
            file=sys.stderr,
        )
        return 1

    if not _git_tree_clean(worktree):
        return _reject(
            store,
            args.subtask_id,
            f"cb-phase: gate failed — working tree at {worktree} is not clean "
            "(commit or stash changes before handoff).",
        )

    if not _pr_is_open(args.pr):
        return _reject(
            store,
            args.subtask_id,
            f"cb-phase: gate failed — PR #{args.pr} is not OPEN.",
        )

    verify_command = _verify_command(project_dir)
    if verify_command:
        code = _run_verify_command(verify_command, worktree)
        if code != 0:
            return _reject(
                store,
                args.subtask_id,
                f"cb-phase: gate failed — verify command exited {code}: "
                f"{verify_command!r}",
            )

    try:
        transition(
            args.subtask_id,
            args.task,
            "review_pending",
            caller_role="coder",
            reason="cb-phase verify",
            store=store,
        )
    except InvalidTransitionError as exc:
        print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
        return 1

    print(
        f"cb-phase: subtask {args.subtask_id} → review_pending "
        f"(PR #{args.pr}, task {args.task})."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cb-phase",
        description="Verify-gated phase handoffs for codeband subtasks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser(
        "verify",
        help="Gate a subtask into review_pending (clean tree + open PR + verify).",
    )
    verify.add_argument("subtask_id", help="Subtask identifier.")
    verify.add_argument("--task", required=True, help="Task identifier (room_id).")
    verify.add_argument("--pr", type=int, required=True, help="Pull request number.")
    verify.add_argument(
        "--worktree",
        default=".",
        help="Path to the git worktree to check (default: cwd).",
    )
    verify.add_argument(
        "--project-dir",
        default=".",
        help="Project directory containing codeband.yaml (default: cwd).",
    )
    verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``cb-phase``. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
