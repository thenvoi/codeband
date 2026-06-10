"""Per-subtask finite-state machine (RFC Workstream 2).

The FSM gates *effects*, not the Conductor's routing. A subtask advances
through a fixed lifecycle:

    planned → assigned → in_progress → verify_pending → review_pending
            → review_passed → merge_pending → merged
                            ↘ review_failed → in_progress
                            ↘ blocked
                            ↘ abandoned

:data:`VALID_TRANSITIONS` encodes every legal edge keyed by
``(current_state, caller_role)`` — exactly the RFC table. Two cross-cutting
wildcards are enforced in :func:`_is_allowed` rather than enumerated per state:
the Conductor may *abandon*, and the Watchdog may *block*, any non-terminal
subtask regardless of its current state.

:func:`transition` is the only mutation path. It auto-creates the subtask row
(via :meth:`StateStore.ensure_subtask`), then — inside a single
``BEGIN EXCLUSIVE`` transaction against the same SQLite file — re-reads the
current state, validates ``(current_state, caller_role)``, writes the new
state and appends a ``transition_log`` row. An illegal edge or a wrong caller
role raises :class:`InvalidTransitionError` and writes nothing.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from codeband.state.store import StateStore, TERMINAL_STATES, _now_iso

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
MAX_REVIEW_ROUNDS = 3


class InvalidTransitionError(Exception):
    """Raised when a requested transition is not permitted.

    Either the ``(current_state, caller_role)`` pair has no entry in
    :data:`VALID_TRANSITIONS`, or it does but ``new_state`` is not among the
    allowed targets. The store is left unchanged when this is raised.
    """


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
    ("review_passed", "mergemaster"): frozenset({"merge_pending"}),
    ("merge_pending", "mergemaster"): frozenset({"merged"}),
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
    head_sha: str | None = None,
) -> None:
    """Atomically advance a subtask to ``new_state``.

    Auto-creates the subtask row, then — under ``BEGIN EXCLUSIVE`` against the
    store's SQLite file — re-reads the current state, validates
    ``(current_state, caller_role)`` against :data:`VALID_TRANSITIONS`, writes
    the new state and appends a ``transition_log`` row. Raises
    :class:`InvalidTransitionError` (writing nothing) on an illegal edge or a
    wrong caller role.

    Two effects are intrinsic to the FSM (not the caller's responsibility):

    * **Review-round counting.** Entering ``review_failed`` increments the
      subtask's durable ``review_round`` in the same transaction — one failed
      review is one round.
    * **The review-round cap.** A ``review_failed → in_progress`` rework is
      rejected once ``review_round`` has reached ``max_review_rounds``; the
      subtask must instead go to ``blocked`` (escalation). The check reads the
      committed count inside the exclusive transaction, so it is race-safe and
      survives a crash/reopen (the count is durable). This bounds a productive
      loop that the watchdog's stall cap never catches.

    ``store`` and ``max_review_rounds`` are keyword-only so the positional
    signature matches the RFC while still letting callers (and tests) inject the
    concrete store and override the cap (e.g. from
    ``config.AgentsConfig.max_review_rounds``).

    ``head_sha`` (keyword-only, default ``None``) pins the transition to the
    exact commit it was recorded against — ``cb-phase`` passes the worktree's
    ``git rev-parse HEAD`` on the verify and review outcome transitions, so a
    verdict can later be checked against what the PR actually merges. Stored
    verbatim in the ``transition_log`` row; ``NULL`` for every other caller
    and for legacy rows. Nothing reads it yet.
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
                "SELECT state, review_round FROM subtask_states "
                "WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            current_state = row["state"] if row is not None else "planned"
            review_round = row["review_round"] if row is not None else 0

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
            else:
                conn.execute(
                    "UPDATE subtask_states SET state = ?, updated_at = ? "
                    "WHERE task_id = ? AND subtask_id = ?",
                    (new_state, now, task_id, subtask_id),
                )
            conn.execute(
                "INSERT INTO transition_log "
                "(subtask_id, task_id, from_state, to_state, caller_role, "
                "timestamp, reason, head_sha) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    subtask_id, task_id, current_state, new_state,
                    caller_role, now, reason, head_sha,
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
