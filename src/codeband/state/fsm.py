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
    ("verify_pending", "coder"): frozenset({"review_pending"}),
    ("review_pending", "reviewer"): frozenset({"review_passed", "review_failed"}),
    ("review_failed", "coder"): frozenset({"in_progress"}),
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
) -> None:
    """Atomically advance a subtask to ``new_state``.

    Auto-creates the subtask row, then — under ``BEGIN EXCLUSIVE`` against the
    store's SQLite file — re-reads the current state, validates
    ``(current_state, caller_role)`` against :data:`VALID_TRANSITIONS`, writes
    the new state and appends a ``transition_log`` row. Raises
    :class:`InvalidTransitionError` (writing nothing) on an illegal edge or a
    wrong caller role.

    ``store`` is keyword-only so the positional signature matches the RFC while
    still letting callers (and tests) inject the concrete store.
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
                "SELECT state FROM subtask_states WHERE subtask_id = ?",
                (subtask_id,),
            ).fetchone()
            current_state = row["state"] if row is not None else "planned"

            if not _is_allowed(current_state, caller_role, new_state):
                raise InvalidTransitionError(
                    f"Illegal transition for subtask {subtask_id!r}: "
                    f"({current_state!r}, role={caller_role!r}) → {new_state!r}"
                )

            now = _now_iso()
            conn.execute(
                "UPDATE subtask_states SET state = ?, updated_at = ? "
                "WHERE subtask_id = ?",
                (new_state, now, subtask_id),
            )
            conn.execute(
                "INSERT INTO transition_log "
                "(subtask_id, from_state, to_state, caller_role, timestamp, reason) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (subtask_id, current_state, new_state, caller_role, now, reason),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
