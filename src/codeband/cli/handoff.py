"""``cb-phase`` — the verify-gated handoff CLI (RFC Workstream 3).

This is the enforcement seam. Coding agents (Claude *and* Codex) request a
phase advance by shelling out to ``cb-phase verify``; the effect only happens
if every gate passes, regardless of what the Conductor intended.

    cb-phase verify <subtask_id> --pr <n> [--worktree <path>] [--project-dir <p>]

The authoritative ``task_id`` (the FK target of every subtask row) is *not*
taken from the command line: it is resolved from the active-room pointer
``<project_dir>/.codeband_room`` that ``kickoff.send_task`` writes (where
``tasks.task_id == room_id``). The ``--task`` flag is accepted for readability
but is a non-authoritative label only — agents pass the semantic ``task_key``
there, which would FK-fail if trusted (see :func:`_resolve_task_id`).

A coder also runs ``cb-phase start <subtask_id>`` at pickup to
seed the subtask into ``in_progress`` — the Conductor never drives the FSM
directly, so without this nothing would advance the subtask off ``planned`` and
the first ``verify`` would dead-end. ``verify`` self-seeds from a
missing/``planned``/``assigned`` subtask as a backstop, so a skipped ``start``
degrades gracefully rather than failing. Neither path touches the gates that
matter (``verify → review_pending``, the review verdict) or the cap counters.

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
import logging
import subprocess
import sys
from pathlib import Path

from codeband.config import load_config
from codeband.state import StateStore
from codeband.state.fsm import InvalidTransitionError, transition

logger = logging.getLogger(__name__)

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
# No active task could be resolved from ``<project_dir>/.codeband_room`` (the
# pointer is missing/empty, or names a room with no matching ``tasks`` row).
# Distinct from the gate rejections above: nothing was attempted and nothing
# written — the caller cannot proceed because the authoritative task_id (the FK
# target of every subtask row) is unknown.
EXIT_NO_ACTIVE_TASK = 6

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


def _resolve_task_id(
    project_dir: Path,
    store: StateStore,
    task_arg: str | None,
) -> tuple[str | None, int | None]:
    """Resolve the authoritative ``task_id`` from the active-room pointer.

    ``kickoff.send_task`` sets ``tasks.task_id == room_id`` and writes that room
    UUID to ``<project_dir>/.codeband_room``. Every ``subtask_states`` row FKs to
    ``tasks.task_id``, so the room UUID — not whatever label an agent passes — is
    the only value that satisfies the constraint. Agents are trained on the
    semantic ``task_key`` (e.g. ``add-redact-helper``) and pass *that* to
    ``--task``; using it for the FK is exactly the bug this resolves. So the
    authoritative id is read from ``.codeband_room`` and ``--task`` is treated as
    a non-authoritative label only.

    Returns ``(task_id, None)`` on success. On failure returns
    ``(None, EXIT_NO_ACTIVE_TASK)`` after printing a clear, actionable error —
    never an FK crash, never a silent proceed. The two failure modes (no pointer,
    or a pointer with no matching ``tasks`` row) both mean the same thing to the
    caller: there is no seeded task to attach work to.
    """
    room_file = project_dir / ".codeband_room"
    room_id = ""
    if room_file.is_file():
        room_id = room_file.read_text(encoding="utf-8").strip()

    if not room_id:
        print(
            f"cb-phase: no active task — {room_file} missing or empty; "
            "was the task seeded via `cb task`?",
            file=sys.stderr,
        )
        return None, EXIT_NO_ACTIVE_TASK

    if store.get_task(room_id) is None:
        print(
            f"cb-phase: no active task — no tasks row matches active room "
            f"{room_id} (from {room_file}); was the task seeded via `cb task`?",
            file=sys.stderr,
        )
        return None, EXIT_NO_ACTIVE_TASK

    # ``--task`` is a non-authoritative label. If it disagrees with the active
    # room, the room wins — log the discrepancy at debug for traceability and
    # never let the label reach the FK.
    if task_arg is not None and task_arg != room_id:
        logger.debug(
            "cb-phase: ignoring non-authoritative --task %r; "
            "using active room %r from %s",
            task_arg, room_id, room_file,
        )
    return room_id, None


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


def _git_head(worktree: Path) -> str | None:
    """Return ``git rev-parse HEAD`` of ``worktree``, or ``None`` if unknown.

    Captured at record-write time so the verify and review outcome records pin
    the exact commit the verdict was rendered against. Best-effort by design:
    a failure yields ``None`` (stored as ``NULL``, same as legacy rows) rather
    than blocking the transition — SHA pinning is additive/shadow in this
    chunk; nothing reads it yet.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


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


def _reject(
    store: StateStore,
    subtask_id: str,
    task_id: str,
    message: str,
    exit_code: int,
) -> int:
    """Record one rejected verify attempt and return its failure exit code.

    Bumps the subtask's durable ``verify_attempts`` (this is the *only* place a
    rejection is counted), prints the structured ``message`` to stderr, and
    returns ``exit_code`` (a distinct non-zero per failure mode). No
    ``transition_log`` row is written — a rejection is a non-event for the FSM;
    only the cumulative attempt count advances.
    """
    store.increment_verify_attempts(subtask_id, task_id)
    print(message, file=sys.stderr)
    return exit_code


def _max_review_rounds(project_dir: Path) -> int:
    """Return the configured per-subtask review-round cap."""
    return load_config(project_dir).agents.max_review_rounds


# States from which ``cb-phase start`` (and verify's self-seed) walks the
# subtask up to ``in_progress``. A missing subtask reads as ``planned``. Any
# other state is already underway, escalated, or terminal: start is a no-op
# there and must never move it backward.
_PRE_START_STATES = frozenset({"planned", "assigned"})


def _walk_to_in_progress(
    subtask_id: str,
    task_id: str,
    store: StateStore,
) -> tuple[str, int | None]:
    """Bring a subtask to ``in_progress``, walking only legal FSM edges.

    Seeds the lifecycle the Conductor never drives directly: a missing subtask
    is auto-created (``transition`` calls ``ensure_subtask``) and walked
    ``planned → assigned → in_progress`` using the caller role each edge
    requires (``conductor`` for ``planned → assigned``, ``coder`` for
    ``assigned → in_progress``). This registers "work began"; it touches none
    of the gates that matter (verify → review_pending, the review verdict) and
    none of the verify-attempt / review-round counters.

    Returns ``(state, error_code)``. ``error_code`` is ``None`` on success and
    ``state`` is the resulting state — ``in_progress`` after a walk, or the
    subtask's current state when it was already at/past ``in_progress``
    (idempotent, non-regressing). A non-``None`` ``error_code`` means a
    transition was rejected and the caller should return it.
    """
    subtask = store.get_subtask(subtask_id, task_id)
    current = subtask.state if subtask is not None else "planned"

    # Already underway, escalated, or terminal — never move backward.
    if current not in _PRE_START_STATES:
        return current, None

    if current == "planned":
        try:
            transition(
                subtask_id, task_id, "assigned",
                caller_role="conductor",
                reason="cb-phase start: seed planned → assigned",
                store=store,
            )
        except InvalidTransitionError as exc:
            print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
            return current, 1
        current = "assigned"

    try:
        transition(
            subtask_id, task_id, "in_progress",
            caller_role="coder",
            reason="cb-phase start: assigned → in_progress (work began)",
            store=store,
        )
    except InvalidTransitionError as exc:
        print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
        return current, 1
    return "in_progress", None


def _walk_to_verify_pending(
    subtask_id: str,
    task_id: str,
    store: StateStore,
    max_review_rounds: int,
) -> int | None:
    """Auto-walk the subtask to ``verify_pending`` from its current state.

    Returns ``None`` on success (the subtask is now at ``verify_pending`` and
    gates can proceed). Returns an exit code on failure (the caller should
    return it immediately).

    Legal entry states:

    * *missing* / ``planned`` / ``assigned`` — a skipped ``cb-phase start``.
      Self-seed the subtask up to ``in_progress`` first (start's path), then
      fall through to the ``in_progress`` walk. This is the backstop that keeps
      a first verify from dead-ending when nothing ran ``start``.
    * ``verify_pending`` — already there, no transitions needed.
    * ``in_progress`` — walk ``in_progress → verify_pending``.
    * ``review_failed`` — check the review-round cap first; if at cap,
      escalate to ``blocked``. Otherwise walk
      ``review_failed → in_progress → verify_pending``.

    Any other state prints a clear error and returns exit code 1.
    """
    subtask = store.get_subtask(subtask_id, task_id)
    current = subtask.state if subtask is not None else "planned"

    # Backstop for a skipped ``cb-phase start``: a missing/planned/assigned
    # subtask self-seeds up to in_progress, then runs the existing gate exactly
    # as today. This never touches the verify-attempt or review-round counters.
    if current in _PRE_START_STATES:
        seeded, error_code = _walk_to_in_progress(subtask_id, task_id, store)
        if error_code is not None:
            return error_code
        current = seeded

    if current == "verify_pending":
        return None

    if current == "in_progress":
        try:
            transition(
                subtask_id, task_id, "verify_pending",
                caller_role="coder",
                reason="cb-phase verify: auto-walk in_progress → verify_pending",
                store=store,
            )
        except InvalidTransitionError as exc:
            print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
            return 1
        return None

    if current == "review_failed":
        review_round = subtask.review_round if subtask is not None else 0
        if review_round >= max_review_rounds:
            try:
                transition(
                    subtask_id, task_id, "blocked",
                    caller_role="coder",
                    reason="review-round cap reached",
                    store=store,
                )
            except InvalidTransitionError as exc:
                print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
                return 1
            print(
                f"BLOCKED [review_cap_reached]: {review_round} review rounds. "
                "Escalated to human; stop and await.",
                file=sys.stderr,
            )
            return EXIT_CAP_REACHED

        try:
            transition(
                subtask_id, task_id, "in_progress",
                caller_role="coder",
                reason="cb-phase verify: auto-walk review_failed → in_progress (rework)",
                store=store,
                max_review_rounds=max_review_rounds,
            )
        except InvalidTransitionError as exc:
            print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
            return 1
        try:
            transition(
                subtask_id, task_id, "verify_pending",
                caller_role="coder",
                reason="cb-phase verify: auto-walk in_progress → verify_pending",
                store=store,
            )
        except InvalidTransitionError as exc:
            print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
            return 1
        return None

    print(
        f"cb-phase: subtask {subtask_id!r} is in state {current!r}, "
        "which is not a valid entry state for cb-phase verify. "
        "Expected in_progress, verify_pending, or review_failed.",
        file=sys.stderr,
    )
    return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    worktree = Path(args.worktree).resolve()
    store = _resolve_store(project_dir)

    task_id, error_code = _resolve_task_id(project_dir, store, args.task)
    if error_code is not None:
        return error_code

    # Walk the subtask to verify_pending from its current state. This handles
    # first-submit (in_progress), rework (review_failed), and retry
    # (verify_pending) entry paths, walking only legal FSM edges. The
    # review-round cap is checked proactively before attempting the
    # review_failed → in_progress transition.
    walk_result = _walk_to_verify_pending(
        args.subtask_id, task_id, store,
        max_review_rounds=_max_review_rounds(project_dir),
    )
    if walk_result is not None:
        return walk_result

    # Gate 0 — verify-attempt cap. If this subtask has already burned its budget
    # of rejected attempts, escalate to ``blocked`` and stop *before* running any
    # other gate, so the escalating call writes nothing but the ``blocked``
    # transition (no further increment). The count is read from durable state, so
    # the cap holds across a crash/reopen mid-loop.
    max_attempts = _max_verify_attempts(project_dir)
    subtask = store.get_subtask(args.subtask_id, task_id)
    attempts = subtask.verify_attempts if subtask is not None else 0
    if attempts >= max_attempts:
        try:
            transition(
                args.subtask_id,
                task_id,
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
            task_id,
            f"REJECTED [dirty_tree]: {len(dirty)} uncommitted files. "
            "Commit or stash, then re-run cb-phase verify.",
            EXIT_DIRTY_TREE,
        )

    if not _pr_is_open(args.pr):
        branch = _current_branch(worktree) or f"PR #{args.pr}"
        return _reject(
            store,
            args.subtask_id,
            task_id,
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
                task_id,
                f"REJECTED [verify_failed] (exit {code}): {tail}. Fix and re-run.",
                EXIT_VERIFY_FAILED,
            )

    try:
        transition(
            args.subtask_id,
            task_id,
            "review_pending",
            caller_role="coder",
            reason="cb-phase verify",
            store=store,
            # Pin the verify outcome to the exact commit the gates ran against
            # (the tree is clean, so HEAD is precisely what was verified).
            head_sha=_git_head(worktree),
        )
    except InvalidTransitionError as exc:
        print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
        return 1

    print(
        f"cb-phase: subtask {args.subtask_id} → review_pending "
        f"(PR #{args.pr}, task {task_id})."
    )
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    """Seed a subtask into ``in_progress`` at coder pickup.

    Marks "work began" on the subtask the Conductor never advances directly,
    walking ``planned → assigned → in_progress`` (auto-creating the row if it
    does not yet exist). Idempotent and non-regressing: a subtask already at or
    past ``in_progress`` is reported and left untouched. No PR exists yet at
    start, so no gate runs — the gates that matter (verify → review_pending,
    the review verdict) stay downstream and untouched.
    """
    project_dir = Path(args.project_dir).resolve()
    store = _resolve_store(project_dir)

    task_id, error_code = _resolve_task_id(project_dir, store, args.task)
    if error_code is not None:
        return error_code

    # Was the subtask already underway/past before we touched it? Only a subtask
    # that is missing/planned/assigned is actually moved by start; anything else
    # is a non-regressing no-op and is reported as such.
    pre = store.get_subtask(args.subtask_id, task_id)
    already_underway = pre is not None and pre.state not in _PRE_START_STATES

    state, error_code = _walk_to_in_progress(args.subtask_id, task_id, store)
    if error_code is not None:
        return error_code

    if already_underway:
        print(
            f"cb-phase: subtask {args.subtask_id} already at {state} "
            f"(task {task_id}); start is a no-op."
        )
    else:
        print(
            f"cb-phase: subtask {args.subtask_id} → in_progress "
            f"(task {task_id})."
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

    task_id, error_code = _resolve_task_id(project_dir, store, args.task)
    if error_code is not None:
        return error_code

    new_state = "review_passed" if args.approve else "review_failed"

    try:
        transition(
            args.subtask_id,
            task_id,
            new_state,
            caller_role="reviewer",
            reason="cb-phase review --approve" if args.approve else "cb-phase review --reject",
            store=store,
            # Pin the verdict to the commit it was rendered against — HEAD of
            # the reviewer's worktree (``--worktree``, default cwd).
            head_sha=_git_head(Path(args.worktree).resolve()),
        )
    except InvalidTransitionError as exc:
        print(f"cb-phase: review verdict rejected — {exc}", file=sys.stderr)
        return 1

    print(
        f"cb-phase: subtask {args.subtask_id} → {new_state} (task {task_id})."
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cb-phase",
        description="Verify-gated phase handoffs for codeband subtasks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start",
        help="Seed a subtask into in_progress at coder pickup (no PR yet).",
    )
    start.add_argument("subtask_id", help="Subtask identifier.")
    start.add_argument(
        "--task",
        required=False,
        help="Task label (non-authoritative; active room resolved from "
        ".codeband_room).",
    )
    start.add_argument(
        "--worktree",
        default=".",
        help="Path to the git worktree (default: cwd).",
    )
    start.add_argument(
        "--project-dir",
        default=".",
        help="Project directory containing codeband.yaml (default: cwd).",
    )
    start.set_defaults(func=_cmd_start)

    verify = sub.add_parser(
        "verify",
        help="Gate a subtask into review_pending (clean tree + open PR + verify).",
    )
    verify.add_argument("subtask_id", help="Subtask identifier.")
    verify.add_argument(
        "--task",
        required=False,
        help="Task label (non-authoritative; active room resolved from "
        ".codeband_room).",
    )
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
    review.add_argument(
        "--task",
        required=False,
        help="Task label (non-authoritative; active room resolved from "
        ".codeband_room).",
    )
    verdict = review.add_mutually_exclusive_group(required=True)
    verdict.add_argument(
        "--approve", action="store_true", help="Pass review → review_passed.",
    )
    verdict.add_argument(
        "--reject", action="store_true", help="Fail review → review_failed.",
    )
    review.add_argument(
        "--worktree",
        default=".",
        help="Path to the reviewed checkout — its HEAD is pinned onto the "
        "verdict record (default: cwd).",
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
