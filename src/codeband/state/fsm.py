"""Per-subtask finite-state machine (RFC Workstream 2).

The FSM gates *effects*, not the Conductor's routing. A subtask advances
through a fixed lifecycle:

    planned → assigned → in_progress → verify_pending → review_pending
            → review_passed → merge_pending → merged
                            ↘ acceptance_passed → merge_pending → merged
                            ↘ needs_rebase → in_progress (rebase rework)
                            ↘ review_failed → in_progress
                            ↘ blocked → in_progress (conductor resume)
                            ↘ abandoned

    (``review_passed`` exits to ``acceptance_passed`` via the Verifier's
    ``cb-phase verify-acceptance`` gate — the last gate before merge when a
    verifier is configured; a verifier-less task merges straight from
    ``review_passed``, the merge-eligibility gate deciding which path a task's
    verdict snapshot actually permits. ``merge_pending`` may also exit to
    ``needs_rebase`` — execution-time SHA drift or a conflicted PR — or to
    ``blocked`` on a residual merge failure; both are driven by ``cb-phase
    merge``, the sole sanctioned merge path.)

:data:`VALID_TRANSITIONS` encodes every legal edge keyed by
``(current_state, caller_role)`` — exactly the RFC table plus the Stage-2
merge edge (``review_passed → needs_rebase → in_progress``, the Mergemaster's
stale-branch send-back). Two cross-cutting wildcards are enforced in
:func:`_is_allowed` rather than enumerated per state: the Conductor may
*abandon*, and the Watchdog may *block*, any non-terminal subtask regardless
of its current state.

The ``review_passed → merge_pending`` edge is additionally gated (Stage-2):
inside the transition's exclusive transaction, :func:`check_merge_eligibility`
must pass for the exact ``head_sha`` being merged — every verdict leg in the
task's ``required_verdicts`` snapshot needs a passing record pinned to that
SHA (see :data:`_VERDICT_PASS_STATES`). An ineligible attempt raises
:class:`MergeNotEligibleError` and writes nothing. The FSM also owns task
completion: the transition that merges a task's *last* subtask promotes
``tasks.status`` to ``'completed'`` in the same transaction.

:func:`transition` is the only mutation path. It auto-creates the subtask row
(via :meth:`StateStore.ensure_subtask`), then — inside a single
``BEGIN EXCLUSIVE`` transaction against the same SQLite file — re-reads the
current state, validates ``(current_state, caller_role)``, writes the new
state and appends a ``transition_log`` row. An illegal edge or a wrong caller
role raises :class:`InvalidTransitionError` and writes nothing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from codeband.state.registration import DEFAULT_REQUIRED_VERDICTS
from codeband.state.store import (
    StateStore,
    TERMINAL_STATES,
    _now_iso,
    write_chained_transition,
)

logger = logging.getLogger(__name__)

# Per-subtask review-round cap (RFC two-level model). A subtask may cycle
# ``review_failed → in_progress → … → review_pending → review_failed`` at most
# this many times; the next attempt to re-enter ``in_progress`` is rejected and
# the only legal move becomes ``review_failed → blocked`` (escalation). This is
# the *default*; callers (and ``config.AgentsConfig.max_review_rounds``) may
# override it via the ``max_review_rounds`` argument to :func:`transition`.
#
# It is a DISTINCT mechanism from the watchdog's ``max_phase_visits`` stall cap:
# that one fires on the *absence* of mechanical progress (no git-HEAD change, no
# new transition), so it never trips on a loop that commits real code every
# round. The review-round cap bounds exactly that productive-but-circular loop.
MAX_REVIEW_ROUNDS = 6

# Per-subtask rebase-round cap (S2-1). A subtask may *enter* ``needs_rebase``
# at most this many times; the next attempt is rejected and the merge leg
# escalates the subtask to ``blocked`` instead (``BLOCKED [rebase_cap_reached]``
# in ``cli/merge.py``). This is the *default*; callers (and
# ``config.AgentsConfig.max_rebase_rounds``) may override it via the
# ``max_rebase_rounds`` argument to :func:`transition`.
#
# It is a DISTINCT mechanism from both siblings: an active rebase loop writes
# fresh transition rows every cycle, so the watchdog's ``max_phase_visits``
# stall cap BY CONSTRUCTION never fires on it, and it never enters
# ``review_failed``, so the review-round cap never counts it.
MAX_REBASE_ROUNDS = 3


class InvalidTransitionError(Exception):
    """Raised when a requested transition is not permitted.

    Either the ``(current_state, caller_role)`` pair has no entry in
    :data:`VALID_TRANSITIONS`, or it does but ``new_state`` is not among the
    allowed targets. The store is left unchanged when this is raised.
    """


class MergeNotEligibleError(InvalidTransitionError):
    """Raised when ``review_passed → merge_pending`` fails the eligibility gate.

    The edge itself is legal for the Mergemaster, but the task's verdict
    snapshot was not satisfied at the ``head_sha`` being merged — see
    :func:`check_merge_eligibility`. Subclasses
    :class:`InvalidTransitionError` so existing callers that catch the broad
    rejection keep working; the message (and the ``reasons`` on the attached
    :class:`MergeEligibility`) names every missing/stale/unpinned leg. The
    store is left unchanged when this is raised.
    """

    def __init__(self, message: str, eligibility: MergeEligibility) -> None:
        super().__init__(message)
        self.eligibility = eligibility


# Maps each verdict leg name (as snapshotted in ``tasks.required_verdicts``)
# to the ``transition_log.to_state`` that records its *pass*: the verify gate
# is the only edge into ``review_pending`` (``cb-phase verify``, coder), an
# approving review verdict is the only edge into ``review_passed`` (``cb-phase
# review --approve``, reviewer), and an accepting verify-acceptance verdict is
# the only edge into ``acceptance_passed`` (``cb-phase verify-acceptance
# --accept``, verifier) — so a log row with that ``to_state`` and a matching
# ``head_sha`` IS the SHA-pinned passing record for the leg.
_VERDICT_PASS_STATES: dict[str, str] = {
    "verify": "review_pending",
    "review": "review_passed",
    "verify_acceptance": "acceptance_passed",
}


@dataclass
class MergeEligibility:
    """Outcome of one merge-eligibility evaluation.

    ``reasons`` is machine-readable: each entry starts with a stable tag
    (``missing_verdict`` / ``stale_verdict`` / ``unpinned_verdict`` /
    ``unknown_verdict`` / ``unknown_task`` / ``no_head_sha`` /
    ``ungated_merge``) followed by the leg it names — the same
    greppable-tag contract as the ``cb-phase`` rejection lines. An eligible
    *gated* result has no reasons; the vacuously eligible ungated opt-out
    carries an explicit ``ungated_merge`` reason so a log reader can never
    mistake it for a checked pass.
    """

    eligible: bool
    reasons: list[str]


def _evaluate_merge_eligibility(
    conn: sqlite3.Connection,
    task_id: str,
    subtask_id: str,
    head_sha: str | None,
) -> MergeEligibility:
    """Evaluate merge eligibility on an already-open connection.

    Shared by the public :func:`check_merge_eligibility` and the gate inside
    :func:`transition` (which must evaluate on its own ``BEGIN EXCLUSIVE``
    connection so the decision is race-safe against concurrent verdict
    writes). Read-only; every rule fails closed:

    * a missing tasks row is ineligible (``unknown_task``);
    * a ``NULL`` ``required_verdicts`` snapshot (pre-snapshot task) resolves
      to the default pair — never to ungated;
    * an empty-list snapshot (the ``allow_ungated_merge`` opt-out) is
      vacuously eligible, stated explicitly (``ungated_merge``);
    * with verdicts required, a missing ``head_sha`` is ineligible
      (``no_head_sha``) — there is nothing to pin against;
    * per leg, a passing record must exist whose ``head_sha`` exactly equals
      the one being merged: a record pinned to a different SHA is stale, a
      record with ``NULL`` ``head_sha`` matches nothing.
    """
    row = conn.execute(
        "SELECT required_verdicts FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return MergeEligibility(
            False, [f"unknown_task {task_id}: no tasks row (fail-closed)"]
        )

    raw = row["required_verdicts"]
    # NULL snapshot (task registered before verdict snapshots existed) → the
    # default pair. NEVER ungated: only an *explicit* [] opts out.
    required = list(DEFAULT_REQUIRED_VERDICTS) if raw is None else json.loads(raw)
    if not required:
        return MergeEligibility(
            True,
            [
                "ungated_merge: required_verdicts snapshot is [] "
                "(allow_ungated_merge opt-out) — no verdicts checked"
            ],
        )

    if not head_sha:
        return MergeEligibility(
            False,
            [
                "no_head_sha: merge eligibility requires the head SHA being "
                "merged; nothing to pin verdicts against (fail-closed)"
            ],
        )

    reasons: list[str] = []
    for leg in required:
        pass_state = _VERDICT_PASS_STATES.get(leg)
        if pass_state is None:
            # Registration validates legs against KNOWN_VERDICTS, so this only
            # fires on a hand-edited row — still fail closed, never skip.
            reasons.append(
                f"unknown_verdict {leg}: no passing record can satisfy it "
                "(fail-closed)"
            )
            continue
        shas = [
            r["head_sha"]
            for r in conn.execute(
                "SELECT head_sha FROM transition_log "
                "WHERE task_id = ? AND subtask_id = ? AND to_state = ?",
                (task_id, subtask_id, pass_state),
            ).fetchall()
        ]
        if head_sha in shas:
            continue  # a passing record pinned to exactly this SHA exists
        pinned = sorted({s for s in shas if s is not None})
        if pinned:
            reasons.append(
                f"stale_verdict {leg}: pinned to {', '.join(pinned)}, "
                f"not {head_sha}"
            )
        elif shas:
            reasons.append(
                f"unpinned_verdict {leg}: passing record has NULL head_sha "
                "(fail-closed)"
            )
        else:
            reasons.append(
                f"missing_verdict {leg}: no passing {leg} record for this subtask"
            )
    return MergeEligibility(not reasons, reasons)


def check_merge_eligibility(
    task_id: str,
    subtask_id: str,
    head_sha: str | None,
    *,
    store: StateStore,
) -> MergeEligibility:
    """Return whether ``(task_id, subtask_id)`` may merge at ``head_sha``.

    The SHA-pinned merge-eligibility check (Stage-2): every verdict leg in the
    task's ``required_verdicts`` snapshot must have a passing record pinned to
    exactly ``head_sha`` — see :func:`_evaluate_merge_eligibility` for the
    fail-closed rules. This public form is read-only and advisory (a caller
    may use it to *report* eligibility); the enforcing copy of the same
    evaluation runs inside :func:`transition` on the
    ``review_passed → merge_pending`` edge, so there is no mutation path into
    ``merge_pending`` that skips it.
    """
    conn = sqlite3.connect(store.db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    with closing(conn):
        return _evaluate_merge_eligibility(conn, task_id, subtask_id, head_sha)


# Static transition table, keyed by ``(current_state, caller_role)`` → the set
# of states that role may move the subtask to from that state. This is the RFC
# Workstream 2 table verbatim. The ``(any, conductor) → abandoned`` and
# ``(any, watchdog) → blocked`` rules are cross-cutting and handled in
# :func:`_is_allowed` (they would otherwise need an entry for every state).
VALID_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("planned", "conductor"): frozenset({"assigned"}),
    ("assigned", "coder"): frozenset({"in_progress"}),
    ("in_progress", "coder"): frozenset({"verify_pending", "blocked"}),
    # A coder advances a verified subtask to ``review_pending`` (via the
    # ``cb-phase verify`` gate) OR, once the per-subtask verify-attempt cap is
    # hit, escalates it to ``blocked`` — the same escalation outcome the watchdog
    # and review-round cap produce. The ``cb-phase`` CLI drives the ``blocked``
    # edge directly (it is a deterministic subprocess, not an LLM that can be
    # *told* to escalate); the runtime cap guard lives in ``cli/handoff.py``.
    ("verify_pending", "coder"): frozenset({"review_pending", "blocked"}),
    ("review_pending", "reviewer"): frozenset({"review_passed", "review_failed"}),
    # A coder may rework a failed review (back to ``in_progress``) OR, once the
    # review-round cap is hit, escalate the subtask to ``blocked`` — the same
    # terminal-ish escalation outcome the watchdog produces on a stall. The
    # ``in_progress`` edge is additionally guarded at runtime by the round cap
    # in :func:`transition`; ``blocked`` is always available as the escape.
    ("review_failed", "coder"): frozenset({"in_progress", "blocked"}),
    # The Mergemaster either queues an approved subtask for integration
    # (``merge_pending`` — additionally gated at runtime by the SHA-pinned
    # eligibility check in :func:`transition`), sends it back because the
    # branch/verdicts are stale against the integration target
    # (``needs_rebase``), or — at the rebase-round cap, where another
    # send-back is no longer legal — escalates it (``blocked``), mirroring
    # the ``merge_pending`` row below (the cap can fire at either gate).
    ("review_passed", "mergemaster"): frozenset(
        {"merge_pending", "needs_rebase", "blocked"}
    ),
    # The Verifier's evidence-integrity gate (``cb-phase verify-acceptance``,
    # verifier role) runs AFTER review passes, the last gate before merge.
    # ``--accept`` records ``acceptance_passed`` (the verify_acceptance verdict
    # pass-state); ``--reject`` sends the subtask back via ``review_failed`` —
    # reusing the review-round counter + cap so an acceptance dispute "rides
    # the existing review-round-cap → blocked → owner escalation" with no new
    # mechanism and no Conductor adjudication. The edge into ``merge_pending``
    # from ``review_passed`` (above) is left intact: a verifier-LESS config
    # (no ``verify_acceptance`` in its snapshot) merges straight from
    # ``review_passed``; a verifier config's merge-eligibility gate rejects
    # that path (missing ``verify_acceptance``) until ``acceptance_passed`` is
    # recorded, so the seat is a gate, not a re-route.
    ("review_passed", "verifier"): frozenset(
        {"acceptance_passed", "review_failed"}
    ),
    # From ``acceptance_passed`` the Mergemaster has the same three moves it
    # has from ``review_passed`` — queue (gated), send back for rebase, or
    # escalate — so acceptance slots in as a pre-merge state without changing
    # the merge leg's behavior beyond its entry-state set.
    ("acceptance_passed", "mergemaster"): frozenset(
        {"merge_pending", "needs_rebase", "blocked"}
    ),
    # From the merge queue the Mergemaster (via ``cb-phase merge``, the sole
    # sanctioned merge executor) either lands the PR (``merged``), discovers
    # the branch moved/conflicted at execution time and sends it back
    # (``needs_rebase`` — the execution-time SHA re-check and the mergeability
    # pre-check), or records a residual execution failure (``blocked`` —
    # permissions, API error, required status check; the watchdog's
    # blocked-subtask patrol escalates it to the owner).
    ("merge_pending", "mergemaster"): frozenset(
        {"merged", "needs_rebase", "blocked"}
    ),
    # Rebase rework returns to ``in_progress`` — the same state the
    # review-fail feedback loop targets — so the rebased commit must re-earn
    # both verdicts (verify gate + re-review) at its new SHA before the
    # eligibility check can pass again.
    ("needs_rebase", "coder"): frozenset({"in_progress"}),
    # Conductor recovery: ``cb-phase resume`` un-blocks a subtask whose block
    # turned out to be spurious (watchdog false positive, infra hiccup) and
    # returns it to the SAME worker mid-flight. Deliberately preserves every
    # durable counter (``review_round`` / ``rebase_rounds`` /
    # ``verify_attempts``) — that is the whole point versus
    # abandon-and-redispatch; resume is NOT a cap reset, so a subtask blocked
    # *at* a cap re-blocks on its next capped action. Conductor-only: the
    # mirror of the ``(any, conductor) → abandoned`` wildcard.
    ("blocked", "conductor"): frozenset({"in_progress"}),
}


def _is_allowed(current_state: str, caller_role: str, new_state: str) -> bool:
    """Return ``True`` if the transition is permitted.

    Encodes the static table plus two cross-cutting wildcards — the Conductor
    may abandon, and the Watchdog may block, any non-terminal subtask.
    Transitions out of a terminal state are never allowed.
    """
    if current_state in TERMINAL_STATES:
        return False
    if new_state == "abandoned" and caller_role == "conductor":
        return True
    if new_state == "blocked" and caller_role == "watchdog":
        return True
    return new_state in VALID_TRANSITIONS.get((current_state, caller_role), frozenset())


def transition(
    subtask_id: str,
    task_id: str,
    new_state: str,
    caller_role: str,
    reason: str = "",
    *,
    store: StateStore,
    max_review_rounds: int = MAX_REVIEW_ROUNDS,
    max_rebase_rounds: int = MAX_REBASE_ROUNDS,
    head_sha: str | None = None,
) -> None:
    """Atomically advance a subtask to ``new_state``.

    Auto-creates the subtask row, then — under ``BEGIN EXCLUSIVE`` against the
    store's SQLite file — re-reads the current state, validates
    ``(current_state, caller_role)`` against :data:`VALID_TRANSITIONS`, writes
    the new state and appends a ``transition_log`` row. Raises
    :class:`InvalidTransitionError` (writing nothing) on an illegal edge or a
    wrong caller role.

    Four effects are intrinsic to the FSM (not the caller's responsibility):

    * **Review-round counting.** Entering ``review_failed`` increments the
      subtask's durable ``review_round`` in the same transaction — one failed
      review is one round.
    * **The review-round cap.** A ``review_failed → in_progress`` rework is
      rejected once ``review_round`` has reached ``max_review_rounds``; the
      subtask must instead go to ``blocked`` (escalation). The check reads the
      committed count inside the exclusive transaction, so it is race-safe and
      survives a crash/reopen (the count is durable). This bounds a productive
      loop that the watchdog's stall cap never catches.
    * **Rebase-round counting + cap.** Entering ``needs_rebase`` increments the
      subtask's durable ``rebase_rounds`` in the same transaction (one
      merge-gate send-back = one rebase round) — and is rejected once the count
      has reached ``max_rebase_rounds``; the subtask must instead go to
      ``blocked``. An active rebase loop writes fresh transition rows every
      cycle, so the watchdog's stall cap by construction never fires on it; this
      counter is what bounds it. The merge leg (``cli/merge.py``) checks the cap
      proactively and escalates with ``BLOCKED [rebase_cap_reached]``.
    * **The merge-eligibility gate (Stage-2).** Entering ``merge_pending``
      additionally requires :func:`check_merge_eligibility` to pass for the
      ``head_sha`` argument — every verdict leg in the task's
      ``required_verdicts`` snapshot must have a passing record pinned to
      exactly that SHA. The evaluation runs on this transaction's exclusive
      connection (race-safe against concurrent verdict writes); an ineligible
      attempt raises :class:`MergeNotEligibleError`, is logged with its
      machine-readable reasons, and writes nothing. Because
      :func:`transition` is the only mutation path, there is no way into
      ``merge_pending`` that skips the check.
    * **Task completion (Stage-2).** The transition that moves a task's *last*
      subtask to ``merged`` promotes the task itself to
      ``tasks.status = 'completed'`` in the same transaction — strictly
      *every* subtask row must be ``merged`` (an ``abandoned`` sibling blocks
      promotion), and only an ``'active'`` task is promoted (``'superseded'``
      keeps its status).

    ``store``, ``max_review_rounds`` and ``max_rebase_rounds`` are keyword-only
    so the positional signature matches the RFC while still letting callers
    (and tests) inject the concrete store and override the caps (e.g. from
    ``config.AgentsConfig.max_review_rounds`` / ``max_rebase_rounds``).

    ``head_sha`` (keyword-only, default ``None``) pins the transition to the
    exact commit it was recorded against — ``cb-phase`` passes the worktree's
    ``git rev-parse HEAD`` on the verify and review outcome transitions, so a
    verdict can later be checked against what the PR actually merges. Stored
    verbatim in the ``transition_log`` row; ``NULL`` for every other caller
    and for legacy rows. The merge-eligibility gate reads it: a transition to
    ``merge_pending`` must pass the SHA it is merging at (``None`` fails
    closed unless the task's snapshot is the explicit ungated opt-out).
    """
    store.ensure_subtask(subtask_id, task_id)

    conn = sqlite3.connect(store.db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    with closing(conn):
        conn.execute("BEGIN EXCLUSIVE")
        try:
            row = conn.execute(
                "SELECT state, review_round, rebase_rounds FROM subtask_states "
                "WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            current_state = row["state"] if row is not None else "planned"
            review_round = row["review_round"] if row is not None else 0
            rebase_rounds = row["rebase_rounds"] if row is not None else 0

            if not _is_allowed(current_state, caller_role, new_state):
                raise InvalidTransitionError(
                    f"Illegal transition for subtask {subtask_id!r}: "
                    f"({current_state!r}, role={caller_role!r}) → {new_state!r}"
                )

            # Runtime cap guard: a rework cycle is only legal while the subtask
            # has rounds left. At the cap, reject with an actionable error —
            # ``blocked`` remains the legal escape (see VALID_TRANSITIONS).
            if (
                current_state == "review_failed"
                and caller_role == "coder"
                and new_state == "in_progress"
                and review_round >= max_review_rounds
            ):
                raise InvalidTransitionError(
                    f"Review-round cap reached for subtask {subtask_id!r}: "
                    f"{review_round} of max {max_review_rounds} failed reviews. "
                    "No further rework is permitted — escalate by transitioning "
                    "this subtask to 'blocked'."
                )

            # Runtime cap guard (rebase): another merge-gate send-back is only
            # legal while the subtask has rebase rounds left. At the cap,
            # reject with an actionable error — ``blocked`` remains the legal
            # escape (the merge leg escalates there proactively).
            if new_state == "needs_rebase" and rebase_rounds >= max_rebase_rounds:
                raise InvalidTransitionError(
                    f"Rebase-round cap reached for subtask {subtask_id!r}: "
                    f"{rebase_rounds} of max {max_rebase_rounds} merge-gate "
                    "send-backs. No further rebase rework is permitted — "
                    "escalate by transitioning this subtask to 'blocked'."
                )

            # Merge-eligibility gate: the only edge into ``merge_pending``
            # additionally requires every required verdict to be pinned to
            # exactly the SHA being merged. Evaluated on THIS exclusive
            # connection so the decision cannot race a concurrent verdict
            # write; an ineligible attempt raises before anything is written.
            if new_state == "merge_pending":
                eligibility = _evaluate_merge_eligibility(
                    conn, task_id, subtask_id, head_sha
                )
                if not eligibility.eligible:
                    detail = "; ".join(eligibility.reasons)
                    logger.warning(
                        "merge-eligibility gate rejected subtask %r (task %r) "
                        "at head_sha %r: %s",
                        subtask_id, task_id, head_sha, detail,
                    )
                    raise MergeNotEligibleError(
                        f"Merge-ineligible transition for subtask "
                        f"{subtask_id!r}: ({current_state!r} → 'merge_pending') "
                        f"at head_sha {head_sha!r} — {detail}",
                        eligibility,
                    )

            now = _now_iso()
            # One failed review = one round. Increment on *entry* to
            # review_failed so the cap reflects how many times this subtask has
            # bounced back from review.
            if new_state == "review_failed":
                conn.execute(
                    "UPDATE subtask_states "
                    "SET state = ?, updated_at = ?, review_round = review_round + 1 "
                    "WHERE task_id = ? AND subtask_id = ?",
                    (new_state, now, task_id, subtask_id),
                )
            # One merge-gate send-back = one rebase round. Increment on *entry*
            # to needs_rebase, in the same exclusive transaction (durable, like
            # review_round above).
            elif new_state == "needs_rebase":
                conn.execute(
                    "UPDATE subtask_states "
                    "SET state = ?, updated_at = ?, "
                    "rebase_rounds = rebase_rounds + 1 "
                    "WHERE task_id = ? AND subtask_id = ?",
                    (new_state, now, task_id, subtask_id),
                )
            else:
                conn.execute(
                    "UPDATE subtask_states SET state = ?, updated_at = ? "
                    "WHERE task_id = ? AND subtask_id = ?",
                    (new_state, now, task_id, subtask_id),
                )
            # Append the hash-chained transition_log row (Stage-3). The
            # canonical serialization + chain link live in store.py so the
            # writer and verify_chain recompute identically; this runs inside
            # the BEGIN EXCLUSIVE transaction, so the read-prev-head → insert →
            # hash sequence is race-free.
            write_chained_transition(
                conn,
                subtask_id=subtask_id,
                task_id=task_id,
                from_state=current_state,
                to_state=new_state,
                caller_role=caller_role,
                timestamp=now,
                reason=reason,
                head_sha=head_sha,
            )
            # Task completion: merging the LAST subtask promotes the task to
            # 'completed' in the same transaction (single-writer path). The
            # rule is strict — every subtask row must be 'merged'; an
            # 'abandoned' sibling blocks promotion. Only an 'active' task is
            # promoted, so 'superseded' keeps its status untouched.
            if new_state == "merged":
                remaining = conn.execute(
                    "SELECT COUNT(*) AS n FROM subtask_states "
                    "WHERE task_id = ? AND state != 'merged'",
                    (task_id,),
                ).fetchone()["n"]
                if remaining == 0:
                    conn.execute(
                        "UPDATE tasks SET status = 'completed' "
                        "WHERE task_id = ? AND status = 'active'",
                        (task_id,),
                    )
                    logger.info(
                        "task %r completed: all subtasks merged", task_id
                    )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
