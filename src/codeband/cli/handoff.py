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

Rejections are **structured and actionable** so an LLM (or telemetry) can route
on them: every failure prints a stable, machine-greppable tag plus a concrete
next step, and each failure mode exits with a distinct code.

    REJECTED [dirty_tree]: <n> uncommitted files. Commit or stash, then re-run …
    REJECTED [no_pr]: no open PR for branch <b>. Push and open a PR, then re-run.
    REJECTED [verify_failed] (exit <code>): <last ~20 lines>. Fix and re-run.
    BLOCKED [cap_reached]: <n> verify attempts. Escalated to human; stop and await.

The tags (``dirty_tree`` / ``no_pr`` / ``verify_failed`` / ``cap_reached``) are
part of the contract — they feed the verify-gate activation's telemetry later —
so keep them stable.
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

# Distinct exit codes per failure mode. ``cb-phase verify`` returns 0 on
# success; each rejection returns its own non-zero code so a caller can branch
# on the *kind* of failure without parsing stderr. These pair with the
# ``REJECTED [<tag>]`` / ``BLOCKED [<tag>]`` lines and are part of the contract.
EXIT_DIRTY_TREE = 2
EXIT_NO_PR = 3
EXIT_VERIFY_FAILED = 4
EXIT_CAP_REACHED = 5

# How many trailing lines of a failing verify command's output to surface in
# the ``REJECTED [verify_failed]`` message — enough to see the failure without
# dumping a whole test log into the chat relay.
_VERIFY_OUTPUT_TAIL_LINES = 20


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


def _uncommitted_files(worktree: Path) -> list[str]:
    """Return the porcelain status lines for ``worktree`` (empty == clean tree).

    Each element is one ``git status --porcelain`` entry, so ``len(...)`` is the
    count of uncommitted paths the ``dirty_tree`` message reports. A git failure
    (e.g. not a repo) is surfaced as a single synthetic entry so the caller
    treats the tree as un-verifiable — i.e. dirty — and rejects, exactly as the
    previous boolean gate did on a non-zero return.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ["<git status unavailable>"]
    return [line for line in result.stdout.splitlines() if line.strip()]


def _current_branch(worktree: Path) -> str | None:
    """Return the current branch name in ``worktree`` (or ``None`` if unknown).

    Used only to make the ``no_pr`` rejection actionable ("…for branch <b>").
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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


def _run_verify_command(command: str, cwd: Path) -> tuple[int, str]:
    """Run the configured verify command in ``cwd``.

    Returns ``(exit_code, combined_output)`` — stdout and stderr captured
    together so a failure's tail can be surfaced in the rejection message.
    """
    result = subprocess.run(
        command, shell=True, cwd=str(cwd), capture_output=True, text=True,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _output_tail(output: str, lines: int = _VERIFY_OUTPUT_TAIL_LINES) -> str:
    """Return the last ``lines`` non-empty lines of ``output`` as one string."""
    kept = [line for line in output.splitlines() if line.strip()]
    return "\n".join(kept[-lines:])


def _reject(store: StateStore, subtask_id: str, message: str, exit_code: int) -> int:
    """Record one rejected verify attempt and return its failure exit code.

    Bumps the subtask's durable ``verify_attempts`` (this is the *only* place a
    rejection is counted), prints the structured ``message`` to stderr, and
    returns ``exit_code`` (a distinct non-zero per failure mode). No
    ``transition_log`` row is written — a rejection is a non-event for the FSM;
    only the cumulative attempt count advances.
    """
    store.increment_verify_attempts(subtask_id)
    print(message, file=sys.stderr)
    return exit_code


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
            f"BLOCKED [cap_reached]: {attempts} verify attempts. "
            "Escalated to human; stop and await.",
            file=sys.stderr,
        )
        return EXIT_CAP_REACHED

    dirty = _uncommitted_files(worktree)
    if dirty:
        return _reject(
            store,
            args.subtask_id,
            f"REJECTED [dirty_tree]: {len(dirty)} uncommitted files. "
            "Commit or stash, then re-run cb-phase verify.",
            EXIT_DIRTY_TREE,
        )

    if not _pr_is_open(args.pr):
        branch = _current_branch(worktree) or f"PR #{args.pr}"
        return _reject(
            store,
            args.subtask_id,
            f"REJECTED [no_pr]: no open PR for branch {branch}. "
            "Push and open a PR, then re-run.",
            EXIT_NO_PR,
        )

    verify_command = _verify_command(project_dir)
    if verify_command:
        code, output = _run_verify_command(verify_command, worktree)
        if code != 0:
            tail = _output_tail(output)
            return _reject(
                store,
                args.subtask_id,
                f"REJECTED [verify_failed] (exit {code}): {tail}. Fix and re-run.",
                EXIT_VERIFY_FAILED,
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


def _cmd_review(args: argparse.Namespace) -> int:
    """Record a reviewer's verdict on a ``review_pending`` subtask via the FSM.

    ``--approve`` drives ``review_pending → review_passed``; ``--reject`` drives
    ``review_pending → review_failed`` (which the FSM counts as one review
    round). The verdict is *only* legal from ``review_pending`` — from any other
    state the FSM raises :class:`InvalidTransitionError` and writes nothing.

    This is the structural bind behind the non-bypassable verify gate:
    ``review_passed`` is reachable only from ``review_pending``, which in turn is
    reachable only via the ``cb-phase verify`` gate (``verify_pending →
    review_pending``). So there is no path to an *approved* subtask that skips
    verification — the route is enforced in code, not by an LLM following a
    prompt.
    """
    project_dir = Path(args.project_dir).resolve()
    store = _resolve_store(project_dir)
    new_state = "review_passed" if args.approve else "review_failed"

    try:
        transition(
            args.subtask_id,
            args.task,
            new_state,
            caller_role="reviewer",
            reason="cb-phase review --approve" if args.approve else "cb-phase review --reject",
            store=store,
        )
    except InvalidTransitionError as exc:
        print(f"cb-phase: review verdict rejected — {exc}", file=sys.stderr)
        return 1

    print(
        f"cb-phase: subtask {args.subtask_id} → {new_state} (task {args.task})."
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

    review = sub.add_parser(
        "review",
        help="Record a reviewer verdict (review_pending → review_passed/failed).",
    )
    review.add_argument("subtask_id", help="Subtask identifier.")
    review.add_argument("--task", required=True, help="Task identifier (room_id).")
    verdict = review.add_mutually_exclusive_group(required=True)
    verdict.add_argument(
        "--approve", action="store_true", help="Pass review → review_passed.",
    )
    verdict.add_argument(
        "--reject", action="store_true", help="Fail review → review_failed.",
    )
    review.add_argument(
        "--project-dir",
        default=".",
        help="Project directory containing codeband.yaml (default: cwd).",
    )
    review.set_defaults(func=_cmd_review)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Console entry point for ``cb-phase``. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
