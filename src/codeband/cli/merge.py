"""``cb-phase merge`` — the gated merge-execution leg (Stage-2 chunk 2b).

This is the ONLY sanctioned merge path: agents *request* a merge by shelling
out to ``cb-phase merge``; the merge itself is executed by this code, behind
the FSM's SHA-pinned eligibility gate (Stage-2 chunk 2a) and a durable,
SHA-pinned approval grant. The Mergemaster never runs ``gh pr merge`` itself.

    cb-phase merge <subtask_id> [--pr <n>] [--worktree <path>] [--project-dir <p>]

``--pr`` is required on the first invocation and is persisted onto the
subtask row (``subtask_states.pr_number``), so every later invocation —
including the argument-less crash-reconcile re-run — derives the PR number
from durable state. A ``--pr`` that disagrees with the persisted binding is
**rejected**: rebinding a queued subtask to a different (possibly
already-merged) PR would let the reconcile path record a phantom ``merged``.
``--worktree`` is the directory ``gh`` runs in (repo resolution), defaulting
to the cwd like the sibling legs.

Invocation flow (each step fail-closed):

a. Resolve the task (active-room pointer, same as verify/review), the
   subtask, the PR number, and one PR snapshot (state / mergeable / head SHA /
   head branch name).
b. **Reconcile first** (idempotency): a subtask already at ``merge_pending``
   whose PR is already ``MERGED`` records the ``merged`` transition and exits
   0 — the crash-recovery path, working with no arguments.
c. From ``review_passed``: attempt the gated
   ``review_passed → merge_pending`` transition at the PR head SHA. The 2a
   eligibility check runs *inside* the transition; a rejection exits non-zero
   echoing every machine-readable reason. This leg never duplicates the check.
d. **Execution-time SHA re-check** — BEFORE any approval logic. The PR head
   must still equal the SHA on the ``merge_pending`` transition. A push while
   waiting → ``needs_rebase``, non-zero, naming old and new SHA — before any
   grant evaluation or approval-request send, so a head-moved subtask is
   never left permanently un-approvable (the grant could never equal the
   stale ``pending_sha``, and the request marker would already be burned).
   Only applies when a queued SHA exists (NULL-pending legacy rows keep their
   previous behavior).
e. **Approval**: the task's snapshotted ``merge_approval`` approver must have
   granted a SHA-pinned approval (written by ``cb approve`` onto the subtask
   row) matching the SHA recorded on the ``merge_pending`` transition. If not
   yet granted, the approval request is sent to the resolved approver (task
   owner, or the named human) in the task room and the leg exits 0 — the
   subtask RESTS at ``merge_pending``; re-invocation after approval proceeds.
   The request is sent once per ``merge_pending`` SHA (marker-after-send: the
   ``merge_approval_requested_sha`` marker burns only on a successful send).
f. **Mergeability pre-check**: a ``CONFLICTING`` PR → ``needs_rebase``.
g. **Execute** ``gh pr merge <n> --merge``, pinned to the approved commit
   via ``--match-head-commit <pending_sha>`` so a push between snapshot and
   execution can never merge unverified code. Success records the ``merged``
   transition (the 2a task-level ``completed`` promotion fires on its own
   inside the FSM). After a merge is *recorded* merged, the remote branch is
   deleted best-effort (``git push origin --delete <headRefName>``) — never
   ``--delete-branch``, which would also delete the *local* branch out from
   under a coder worktree; a delete failure is a warning only.
h. **Failure classification — verify effects first.** On a non-zero ``gh``
   exit the PR is re-snapshotted before classifying: an actually-``MERGED``
   PR records ``merged`` and exits 0 (the merge landed; only the report
   failed); a moved head → ``needs_rebase`` (covers ``--match-head-commit``
   rejections); a ``CONFLICTING`` mergeable field (preferred over the
   ``_CONFLICT_RE`` text fallback) → ``needs_rebase``. Anything else
   (permissions, API error, required status check) → ``blocked`` with the
   reason recorded. An unavailable re-snapshot classifies nothing — the
   subtask rests at ``merge_pending`` for the next reconcile rather than
   risking a phantom ``blocked`` over a merged PR. The watchdog's existing
   blocked-subtask patrol (escalate-once, marker-after-send — PR #24)
   delivers the owner escalation; this leg sends nothing itself, so a
   re-failure can never double-escalate.

Like the sibling legs, rejections are structured: a stable machine-greppable
tag plus a distinct exit code per failure mode. All ``gh`` and Band
interactions live behind thin module-level functions so tests monkeypatch
them; Band SDK imports are deferred inside the send function, keeping the
module import-light for the common (no-send) paths.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from codeband.cli.handoff import _output_tail, _resolve_store, _resolve_task_id
from codeband.state.fsm import (
    InvalidTransitionError,
    MergeNotEligibleError,
    transition,
)
from codeband.state.store import StateStore, TaskRow

logger = logging.getLogger(__name__)

# Distinct exit codes per failure mode, continuing the ``cli/handoff.py``
# numbering (2–6 are taken by the verify leg) so a caller holding either leg's
# codes can branch on the *kind* of failure without parsing stderr.
EXIT_NO_PR_NUMBER = 7
EXIT_PR_QUERY_FAILED = 8
EXIT_NOT_ELIGIBLE = 9
EXIT_NEEDS_REBASE = 10
EXIT_MERGE_FAILED = 11
# ``--pr`` disagrees with the subtask's persisted PR binding. Rebinding is
# refused outright: pointing a queued subtask at a different (possibly
# already-merged) PR would let the reconcile path record a phantom ``merged``.
EXIT_PR_REBIND = 12

# Entry states this leg accepts. ``review_passed`` is the normal first
# invocation; ``merge_pending`` is the resting/awaiting-approval state and the
# crash-reconcile entry. Anything else is a clear error — in particular there
# is no path that re-merges a ``merged`` subtask or revives a ``blocked`` one.
_ENTRY_STATES = frozenset({"review_passed", "merge_pending"})

# Classifies a failed ``gh pr merge`` as a conflict (→ ``needs_rebase``)
# rather than a residual failure (→ ``blocked``). GitHub phrases conflicts as
# "Pull request ... is not mergeable: the merge commit cannot be cleanly
# created" / "...conflicts must be resolved...".
_CONFLICT_RE = re.compile(r"conflict|not mergeable", re.IGNORECASE)


def _pr_snapshot(pr_number: int, cwd: Path) -> dict | None:
    """Return one ``gh pr view`` snapshot: state, mergeable, head SHA + branch.

    A single query per invocation supplies every PR-derived decision input
    (reconcile state, mergeability, execution-time SHA, the head branch name
    for the post-merge remote cleanup), so the leg cannot contradict itself
    mid-run. Returns ``None`` when ``gh`` fails or returns unparseable
    output — callers fail closed.
    """
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number),
         "--json", "state,mergeable,headRefOid,headRefName"],
        capture_output=True, text=True, cwd=str(cwd),
    )
    if result.returncode != 0:
        logger.debug("gh pr view %s failed: %s", pr_number, result.stderr)
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _gh_merge(
    pr_number: int, cwd: Path, pending_sha: str | None,
) -> tuple[int, str]:
    """Execute the merge: ``gh pr merge <n> --merge``, pinned to ``pending_sha``.

    ``--match-head-commit <pending_sha>`` (whenever a queued SHA exists) makes
    GitHub itself refuse the merge if the head moved between our snapshot and
    the execution — the last unguarded window. No ``--delete-branch``: that
    flag also deletes the *local* branch, which belongs to a coder worktree;
    remote cleanup is :func:`_delete_remote_branch`'s job, after the merge is
    recorded. Returns ``(exit_code, combined_output)`` for failure
    classification.
    """
    cmd = ["gh", "pr", "merge", str(pr_number), "--merge"]
    if pending_sha is not None:
        cmd += ["--match-head-commit", pending_sha]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(cwd),
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _delete_remote_branch(pr: dict | None, cwd: Path) -> None:
    """Best-effort REMOTE-only delete of a merged PR's head branch.

    ``git push origin --delete <headRefName>`` (branch name from the PR
    snapshot) — never a local delete: local branches belong to coder
    worktrees. Called only *after* a merge is recorded ``merged``; any
    failure (missing branch name, git error, already deleted by GitHub's
    auto-delete) is a printed warning and never affects classification or
    the exit code.
    """
    branch = (pr or {}).get("headRefName") or None
    if branch is None:
        print(
            "cb-phase: warning — no head branch name in the PR snapshot; "
            "skipping remote branch cleanup.",
            file=sys.stderr,
        )
        return
    try:
        result = subprocess.run(
            ["git", "push", "origin", "--delete", branch],
            capture_output=True, text=True, cwd=str(cwd),
        )
    except OSError as exc:
        print(
            f"cb-phase: warning — remote branch delete failed for "
            f"{branch!r}: {exc}",
            file=sys.stderr,
        )
        return
    if result.returncode != 0:
        print(
            f"cb-phase: warning — could not delete remote branch {branch!r} "
            f"(exit {result.returncode}): "
            f"{_output_tail((result.stdout or '') + (result.stderr or ''))}",
            file=sys.stderr,
        )


def _merge_pending_sha(store: StateStore, subtask_id: str, task_id: str) -> str | None:
    """Return the ``head_sha`` recorded on the latest ``→ merge_pending`` row.

    The SHA the merge was *queued* at — the anchor for both the approval
    grant and the execution-time re-check. Reads the store's SQLite file
    directly (read-only), mirroring the watchdog's transition-log readers;
    task-scoped, since subtask ids repeat across tasks.
    """
    conn = sqlite3.connect(store.db_path, timeout=30.0)
    try:
        row = conn.execute(
            "SELECT head_sha FROM transition_log "
            "WHERE task_id = ? AND subtask_id = ? AND to_state = 'merge_pending' "
            "ORDER BY id DESC LIMIT 1",
            (task_id, subtask_id),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row is not None else None


def _approver_display(task: TaskRow, approver_spec: str) -> tuple[str | None, str]:
    """Resolve ``(mention_id, handle)`` for the snapshotted approver spec.

    ``"owner"`` resolves to the task row's owner (structured mention via
    ``owner_id``, display via ``owner_handle``); ``"human:<handle>"`` carries
    only a display handle — no Band participant id is known for an arbitrary
    human, so the message mentions them by text alone.
    """
    if approver_spec.startswith("human:"):
        return None, approver_spec[len("human:"):]
    return task.owner_id, (task.owner_handle or task.owner_id or "owner")


def _send_approval_request(
    project_dir: Path,
    task: TaskRow,
    subtask_id: str,
    pr_number: int,
    head_sha: str | None,
    approver_spec: str,
) -> None:
    """Post the merge-approval request to the task room, @mentioning the approver.

    The existing room-message mechanism (`cb approve` / `cb reject` use the
    same plumbing in ``orchestration/kickoff.py``): a Band REST chat message
    in the task's room. Sent with the Mergemaster's credentials — the merge
    leg is the Mergemaster's seam — and a structured mention when the
    approver has a known participant id (the owner). Raises on any failure;
    the caller owns the send-once marker (marker-after-send).
    """
    import asyncio

    from codeband.config import load_agent_config, load_config

    config = load_config(project_dir)
    creds = load_agent_config(project_dir).get("mergemaster")

    mention_id, handle = _approver_display(task, approver_spec)
    content = (
        f"@{handle} PR #{pr_number} (subtask {subtask_id}) is awaiting your "
        f"merge approval at head {head_sha or 'unknown'}. "
        f"Approve with: cb approve {pr_number}"
    )

    async def _send() -> None:
        from thenvoi_rest import AsyncRestClient, ChatMessageRequest
        from thenvoi_rest.types import ChatMessageRequestMentionsItem as Mention

        client = AsyncRestClient(api_key=creds.api_key, base_url=config.band.rest_url)
        mentions = [Mention(id=mention_id)] if mention_id else []
        await client.agent_api_messages.create_agent_chat_message(
            chat_id=task.room_id,
            message=ChatMessageRequest(content=content, mentions=mentions),
        )

    asyncio.run(_send())


def _transition_or_fail(
    subtask_id: str,
    task_id: str,
    new_state: str,
    reason: str,
    *,
    store: StateStore,
    head_sha: str | None = None,
) -> int | None:
    """Apply a mergemaster transition; return an exit code only on rejection."""
    try:
        transition(
            subtask_id, task_id, new_state,
            caller_role="mergemaster", reason=reason, store=store,
            head_sha=head_sha,
        )
    except InvalidTransitionError as exc:
        print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
        return 1
    return None


def _cmd_merge(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir).resolve()
    worktree = Path(args.worktree).resolve()
    store = _resolve_store(project_dir)

    task_id, error_code = _resolve_task_id(project_dir, store, args.task)
    if error_code is not None:
        return error_code

    subtask = store.get_subtask(args.subtask_id, task_id)
    current = subtask.state if subtask is not None else "planned"
    if current not in _ENTRY_STATES:
        print(
            f"cb-phase: subtask {args.subtask_id!r} is in state {current!r}, "
            "which is not a valid entry state for cb-phase merge. "
            "Expected review_passed or merge_pending.",
            file=sys.stderr,
        )
        return 1

    # PR number: the persisted binding is authoritative once set. An explicit
    # --pr binds on first use (and is persisted — the durable binding the
    # argument-less reconcile path reads back), is an idempotent no-op when it
    # matches, and is REJECTED when it disagrees: rebinding a queued subtask
    # to a different (possibly already-merged) PR would let the reconcile
    # path record a phantom ``merged``. Neither given nor persisted → nothing
    # to merge against, fail closed.
    pr_number = args.pr if args.pr is not None else subtask.pr_number
    if pr_number is None:
        print(
            f"REJECTED [no_pr_number]: subtask {args.subtask_id} has no "
            "recorded PR. Pass --pr <n> on the first cb-phase merge invocation.",
            file=sys.stderr,
        )
        return EXIT_NO_PR_NUMBER
    if args.pr is not None and subtask.pr_number is not None and subtask.pr_number != args.pr:
        print(
            f"REJECTED [pr_rebind]: subtask {args.subtask_id} is already "
            f"bound to PR #{subtask.pr_number}; refusing to rebind to "
            f"#{args.pr}.",
            file=sys.stderr,
        )
        return EXIT_PR_REBIND
    if args.pr is not None and subtask.pr_number is None:
        store.set_pr_number(args.subtask_id, task_id, args.pr)

    # One PR snapshot drives every PR-derived decision this invocation.
    pr = _pr_snapshot(pr_number, worktree)
    if pr is None:
        print(
            f"REJECTED [pr_query_failed]: could not query PR #{pr_number} "
            "via gh. Check gh auth/network, then re-run.",
            file=sys.stderr,
        )
        return EXIT_PR_QUERY_FAILED
    pr_state = pr.get("state")
    head_sha = pr.get("headRefOid") or None

    # (b) Reconcile first — the crash-recovery path. A merge that executed
    # but crashed before recording lands here on re-invocation: the PR is
    # already MERGED, so record the transition and exit 0. Works with no
    # arguments (PR number read back from the subtask row).
    if current == "merge_pending":
        if pr_state == "MERGED":
            code = _transition_or_fail(
                args.subtask_id, task_id, "merged",
                f"cb-phase merge: reconciled — PR #{pr_number} already merged",
                store=store, head_sha=head_sha,
            )
            if code is not None:
                return code
            print(
                f"cb-phase: reconciled — PR #{pr_number} was already merged; "
                f"subtask {args.subtask_id} → merged (task {task_id})."
            )
            _delete_remote_branch(pr, worktree)
            return 0
    else:
        # (c) The gated review_passed → merge_pending transition, at the PR
        # head SHA. Eligibility (2a) is enforced INSIDE the transition — this
        # leg never duplicates the check.
        try:
            transition(
                args.subtask_id, task_id, "merge_pending",
                caller_role="mergemaster",
                reason=f"cb-phase merge: queue PR #{pr_number} for merge",
                store=store, head_sha=head_sha,
            )
        except MergeNotEligibleError as exc:
            detail = "; ".join(exc.eligibility.reasons)
            print(
                f"REJECTED [not_eligible]: subtask {args.subtask_id} cannot "
                f"enter merge_pending at {head_sha!r} — {detail}",
                file=sys.stderr,
            )
            return EXIT_NOT_ELIGIBLE
        except InvalidTransitionError as exc:
            print(f"cb-phase: transition rejected — {exc}", file=sys.stderr)
            return 1
        current = "merge_pending"

    # The SHA the merge was queued at — anchor for the grant and the
    # execution-time re-check.
    pending_sha = _merge_pending_sha(store, args.subtask_id, task_id)

    # (d) Execution-time SHA re-check — BEFORE any approval logic. A push
    # while queued invalidates the queue entry: fail-closed, no execution,
    # and crucially no grant evaluation and no approval-request send — a
    # grant can never equal the stale pending_sha, and burning the request
    # marker for a SHA that will never merge would strand the subtask
    # permanently un-approvable. Guarded on a recorded queue SHA: NULL-pending
    # legacy rows keep their previous behavior.
    if pending_sha is not None and head_sha != pending_sha:
        code = _transition_or_fail(
            args.subtask_id, task_id, "needs_rebase",
            f"cb-phase merge: head moved while queued "
            f"({pending_sha} → {head_sha})",
            store=store,
        )
        if code is not None:
            return code
        print(
            f"REJECTED [sha_moved]: PR #{pr_number} head moved while queued — "
            f"merge_pending was recorded at {pending_sha}, head is now "
            f"{head_sha}. Subtask → needs_rebase; rework and re-earn verdicts.",
            file=sys.stderr,
        )
        return EXIT_NEEDS_REBASE

    # A PR that can never merge (closed without merging) is a residual
    # failure: block before bothering the approver about it.
    if pr_state != "OPEN":
        code = _transition_or_fail(
            args.subtask_id, task_id, "blocked",
            f"cb-phase merge: PR #{pr_number} is {pr_state} — cannot merge",
            store=store,
        )
        if code is not None:
            return code
        print(
            f"BLOCKED [merge_failed]: PR #{pr_number} is {pr_state} — cannot "
            "merge. Escalation via watchdog; stop and await.",
            file=sys.stderr,
        )
        return EXIT_MERGE_FAILED

    # (e) Approval — required for every task in V1 ('none' is rejected at
    # registration; a NULL snapshot defaults to 'owner', never to skipped).
    # Runs strictly AFTER the SHA re-check above, so a request is only ever
    # sent (and its send-once marker only ever burned) for a SHA that can
    # still merge.
    task = store.get_task(task_id)
    approver_spec = (task.merge_approval if task is not None else None) or "owner"
    subtask = store.get_subtask(args.subtask_id, task_id)
    granted = (
        subtask.merge_approved_sha is not None
        and pending_sha is not None
        and subtask.merge_approved_sha == pending_sha
    )
    if not granted:
        if subtask.merge_approved_sha is not None:
            print(
                f"cb-phase: recorded approval is pinned to "
                f"{subtask.merge_approved_sha}, not the queued "
                f"{pending_sha} — re-approval required.",
                file=sys.stderr,
            )
        if subtask.merge_approval_requested_sha == pending_sha:
            print(
                f"cb-phase: awaiting approval for PR #{pr_number} "
                f"(subtask {args.subtask_id}, approver {approver_spec}) — "
                "request already sent. Re-run after cb approve."
            )
            return 0
        try:
            _send_approval_request(
                project_dir, task, args.subtask_id, pr_number,
                pending_sha, approver_spec,
            )
        except Exception:
            # Marker-after-send: the send-once marker stays unburned, so the
            # next invocation retries the request. The subtask legitimately
            # rests at merge_pending either way.
            logger.exception("approval-request send failed")
            print(
                f"cb-phase: awaiting approval for PR #{pr_number} "
                f"(subtask {args.subtask_id}, approver {approver_spec}) — "
                "request send FAILED; will retry on next invocation.",
                file=sys.stderr,
            )
            return 0
        store.mark_merge_approval_requested(
            args.subtask_id, task_id, pending_sha,
        )
        print(
            f"cb-phase: awaiting approval for PR #{pr_number} "
            f"(subtask {args.subtask_id}) — request sent to {approver_spec}. "
            f"Re-run after cb approve {pr_number}."
        )
        return 0

    # (f) Mergeability pre-check: a conflicted PR can never land — send it
    # back for rebase without attempting the merge.
    if pr.get("mergeable") == "CONFLICTING":
        code = _transition_or_fail(
            args.subtask_id, task_id, "needs_rebase",
            f"cb-phase merge: PR #{pr_number} is conflicted against its base",
            store=store,
        )
        if code is not None:
            return code
        print(
            f"REJECTED [conflicted]: PR #{pr_number} has merge conflicts. "
            "Subtask → needs_rebase; rebase and re-earn verdicts.",
            file=sys.stderr,
        )
        return EXIT_NEEDS_REBASE

    # (g) Execute, pinned to the approved commit. GitHub itself rejects the
    # merge if the head moved between our snapshot and the execution.
    merge_code, output = _gh_merge(pr_number, worktree, pending_sha)
    if merge_code == 0:
        code = _transition_or_fail(
            args.subtask_id, task_id, "merged",
            f"cb-phase merge: PR #{pr_number} merged",
            store=store, head_sha=head_sha,
        )
        if code is not None:
            return code
        print(
            f"cb-phase: PR #{pr_number} merged; subtask {args.subtask_id} "
            f"→ merged (task {task_id})."
        )
        _delete_remote_branch(pr, worktree)
        return 0

    # (h) Failure classification — verify effects first. ``gh`` exiting
    # non-zero does NOT mean the merge did not happen (a timeout after the
    # merge API call landed produced exactly this misclassification), so the
    # PR is re-snapshotted before anything is recorded:
    #
    #   MERGED              → record merged, exit 0 (only the report failed)
    #   head ≠ pending_sha  → needs_rebase (covers --match-head-commit
    #                         rejections)
    #   CONFLICTING         → needs_rebase (structured field preferred; the
    #                         _CONFLICT_RE text match is the fallback only)
    #   otherwise           → blocked, reason recorded — the watchdog's
    #                         blocked-subtask patrol escalates to the owner
    #                         once.
    #
    # An unavailable re-snapshot classifies nothing: the subtask rests at
    # ``merge_pending`` (re-invocation reconciles) rather than risking a
    # phantom ``blocked`` over a PR that actually merged.
    tail = _output_tail(output)
    resnap = _pr_snapshot(pr_number, worktree)
    if resnap is None:
        print(
            f"REJECTED [pr_query_failed]: gh pr merge #{pr_number} failed "
            f"(exit {merge_code}): {tail} — and the post-failure PR snapshot "
            "is unavailable, so the outcome cannot be classified. Subtask "
            "rests at merge_pending; re-run to reconcile.",
            file=sys.stderr,
        )
        return EXIT_PR_QUERY_FAILED

    if resnap.get("state") == "MERGED":
        code = _transition_or_fail(
            args.subtask_id, task_id, "merged",
            f"cb-phase merge: post-failure reconcile: gh exited "
            f"{merge_code} but PR #{pr_number} is MERGED",
            store=store, head_sha=resnap.get("headRefOid") or None,
        )
        if code is not None:
            return code
        print(
            f"cb-phase: PR #{pr_number} merged (gh exited {merge_code} but "
            f"the merge landed); subtask {args.subtask_id} → merged "
            f"(task {task_id})."
        )
        _delete_remote_branch(resnap, worktree)
        return 0

    resnap_head = resnap.get("headRefOid") or None
    if pending_sha is not None and resnap_head != pending_sha:
        code = _transition_or_fail(
            args.subtask_id, task_id, "needs_rebase",
            f"cb-phase merge: head moved during execution "
            f"({pending_sha} → {resnap_head}); gh exited {merge_code}: {tail}",
            store=store,
        )
        if code is not None:
            return code
        print(
            f"REJECTED [sha_moved]: PR #{pr_number} head moved during "
            f"execution — merge was pinned to {pending_sha}, head is now "
            f"{resnap_head}. Subtask → needs_rebase; rework and re-earn "
            "verdicts.",
            file=sys.stderr,
        )
        return EXIT_NEEDS_REBASE

    if resnap.get("mergeable") == "CONFLICTING" or _CONFLICT_RE.search(output):
        code = _transition_or_fail(
            args.subtask_id, task_id, "needs_rebase",
            f"cb-phase merge: gh reported a conflict merging PR "
            f"#{pr_number}: {tail}",
            store=store,
        )
        if code is not None:
            return code
        print(
            f"REJECTED [conflicted]: gh could not merge PR #{pr_number} "
            f"(conflict): {tail}. Subtask → needs_rebase.",
            file=sys.stderr,
        )
        return EXIT_NEEDS_REBASE

    code = _transition_or_fail(
        args.subtask_id, task_id, "blocked",
        f"cb-phase merge: gh pr merge #{pr_number} failed "
        f"(exit {merge_code}): {tail}",
        store=store,
    )
    if code is not None:
        return code
    print(
        f"BLOCKED [merge_failed] (exit {merge_code}): {tail}. "
        "Reason recorded; escalation via watchdog. Stop and await.",
        file=sys.stderr,
    )
    return EXIT_MERGE_FAILED


def add_merge_subparser(sub: argparse._SubParsersAction) -> None:
    """Register the ``merge`` subcommand on the ``cb-phase`` parser."""
    merge = sub.add_parser(
        "merge",
        help="Execute a gated, approved merge (the only sanctioned merge path).",
    )
    merge.add_argument("subtask_id", help="Subtask identifier.")
    merge.add_argument(
        "--task",
        required=False,
        help="Task label (non-authoritative; active room resolved from "
        ".codeband_room).",
    )
    merge.add_argument(
        "--pr",
        type=int,
        required=False,
        help="Pull request number — required on the first invocation, "
        "persisted for argument-less reconcile re-runs.",
    )
    merge.add_argument(
        "--worktree",
        default=".",
        help="Directory gh runs in for repo resolution (default: cwd).",
    )
    merge.add_argument(
        "--project-dir",
        default=".",
        help="Project directory containing codeband.yaml (default: cwd).",
    )
    merge.set_defaults(func=_cmd_merge)


def record_approval_grant(project_dir: Path, pr_number: int) -> list[str]:
    """Record a SHA-pinned merge-approval grant for ``pr_number``'s subtask(s).

    The store half of ``cb approve <pr>`` (the chat half is unchanged):
    resolves the active task, finds the subtask(s) bound to the PR (bound by
    ``cb-phase merge`` persisting ``--pr``), reads the PR's current head SHA,
    and writes the grant. Returns a human-readable line per grant recorded —
    empty when no subtask is bound to the PR (the legacy chat-only flow,
    which records nothing and changes nothing).

    Raises :class:`RuntimeError` when a bound subtask exists but the PR head
    cannot be read — a grant that silently pins nothing would strand the
    merge leg in awaiting-approval forever.
    """
    store = _resolve_store(project_dir)
    task_id, error_code = _resolve_task_id(project_dir, store, None)
    if error_code is not None:
        return []

    subtasks = [
        s for s in store.find_subtasks_by_pr(task_id, pr_number)
        if s.state not in {"merged", "abandoned"}
    ]
    if not subtasks:
        logger.debug(
            "cb approve: no subtask bound to PR #%s — no grant recorded",
            pr_number,
        )
        return []

    pr = _pr_snapshot(pr_number, project_dir)
    head_sha = (pr or {}).get("headRefOid") or None
    if head_sha is None:
        raise RuntimeError(
            f"cb approve: could not read PR #{pr_number}'s head SHA via gh — "
            "approval grant not recorded. Check gh auth/network and re-run."
        )

    task = store.get_task(task_id)
    approved_by = (task.merge_approval if task is not None else None) or "owner"

    recorded = []
    for sub in subtasks:
        store.record_merge_approval(
            sub.subtask_id, task_id,
            approved_by=approved_by, approved_sha=head_sha,
        )
        recorded.append(
            f"Merge approval recorded for subtask {sub.subtask_id} "
            f"at {head_sha} (approver: {approved_by})."
        )
    return recorded
