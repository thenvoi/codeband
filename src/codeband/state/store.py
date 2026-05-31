"""SQLite-backed durable state store (RFC Workstream 1).

A single local SQLite database at ``{workspace_path}/state/orchestration.db``
holds three tables:

* ``tasks`` — one row per task (keyed by ``room_id``).
* ``subtask_states`` — one row per subtask, its current FSM state plus
  assignment / PR / metadata.
* ``transition_log`` — append-only audit of every state transition.

Design notes:

* **stdlib only.** Built on :mod:`sqlite3`; no third-party dependency.
* **WAL + short atomic transactions.** Each public method opens a fresh,
  short-lived connection in WAL mode with a busy timeout, so multiple
  processes sharing one workspace (``run_local`` and the distributed
  ``agent_main`` path both point at the same file) can read and write
  concurrently without corruption. WAL lets readers proceed during a write.
* **Idempotent schema.** Tables are created with ``CREATE TABLE IF NOT
  EXISTS`` on init, so constructing a ``StateStore`` against a fresh or an
  existing DB is always safe.

Only the Workstream-1 surface lives here: ``create_task``,
``ensure_subtask``, ``get_task``, ``get_subtask`` and ``list_active_subtasks``.
The ``transition_log`` table is created but written by the FSM
(``state/fsm.py``, Workstream 2); this module never enforces transitions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Subtask states considered finished — excluded from ``list_active_subtasks``.
# Mirrors the FSM terminal states (Workstream 2): a merged subtask is done and
# an abandoned one was dropped by the Conductor. Everything else (including
# ``blocked``) is still in flight and worth surfacing on rehydration.
TERMINAL_STATES: frozenset[str] = frozenset({"merged", "abandoned"})


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRow:
    """A typed view of a ``tasks`` row."""

    task_id: str
    description: str
    room_id: str
    created_at: str
    status: str = "active"


@dataclass
class SubtaskRow:
    """A typed view of a ``subtask_states`` row."""

    subtask_id: str
    task_id: str
    state: str
    assigned_worker: str | None
    pr_number: int | None
    created_at: str
    updated_at: str
    metadata: dict[str, Any] | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS subtask_states (
    subtask_id      TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id),
    state           TEXT NOT NULL DEFAULT 'planned',
    assigned_worker TEXT,
    pr_number       INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    metadata        TEXT
);

CREATE TABLE IF NOT EXISTS transition_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subtask_id  TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    caller_role TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    reason      TEXT
);
"""


class StateStore:
    """Typed, durable SQLite store for task / subtask orchestration state.

    Construct against a DB path (created, with its parent directory, if
    missing); the schema is initialised idempotently. Methods are safe to call
    from multiple processes sharing the same file.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── connection plumbing ────────────────────────────────────────────────

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a short atomic transaction.

        Opens a fresh WAL-mode connection with a busy timeout, commits on
        success, rolls back on error, and always closes. Keeping connections
        short-lived (rather than one long-lived handle) is what makes
        cross-process concurrency safe.
        """
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level="DEFERRED",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with closing(conn):
                with conn:  # commit / rollback
                    yield conn
        except sqlite3.Error:
            logger.exception("StateStore transaction failed (db=%s)", self.db_path)
            raise

    def _init_schema(self) -> None:
        with self._transaction() as conn:
            conn.executescript(_SCHEMA)

    # ── tasks ──────────────────────────────────────────────────────────────

    def create_task(
        self,
        task_id: str,
        description: str,
        room_id: str,
        *,
        created_at: str | None = None,
        status: str = "active",
    ) -> None:
        """Insert a task row (idempotent on ``task_id``).

        Re-creating the same task is a no-op rather than an error, so a kickoff
        that is retried against an existing DB stays safe.
        """
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(task_id, description, room_id, created_at, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, description, room_id, created_at or _now_iso(), status),
            )

    def get_task(self, task_id: str) -> TaskRow | None:
        """Return the task row, or ``None`` if it does not exist."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    # ── subtasks ───────────────────────────────────────────────────────────

    def ensure_subtask(
        self,
        subtask_id: str,
        task_id: str,
        *,
        state: str = "planned",
        assigned_worker: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create the subtask row if absent; otherwise do nothing.

        Uses ``INSERT OR IGNORE`` so a caller can lazily ensure a subtask
        exists before writing to it (the FSM does this before every
        transition) without first checking — idempotent and race-safe.
        """
        now = _now_iso()
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO subtask_states "
                "(subtask_id, task_id, state, assigned_worker, pr_number, "
                "created_at, updated_at, metadata) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    subtask_id,
                    task_id,
                    state,
                    assigned_worker,
                    now,
                    now,
                    json.dumps(metadata) if metadata is not None else None,
                ),
            )

    def get_subtask(self, subtask_id: str) -> SubtaskRow | None:
        """Return the subtask row, or ``None`` if it does not exist."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM subtask_states WHERE subtask_id = ?", (subtask_id,)
            ).fetchone()
        return _subtask_from_row(row) if row is not None else None

    def list_active_subtasks(self, task_id: str | None = None) -> list[SubtaskRow]:
        """Return non-terminal subtasks, newest first.

        "Active" means any state not in :data:`TERMINAL_STATES`. Pass
        ``task_id`` to scope the query to one task.
        """
        placeholders = ",".join("?" * len(TERMINAL_STATES))
        params: list[Any] = list(TERMINAL_STATES)
        sql = f"SELECT * FROM subtask_states WHERE state NOT IN ({placeholders})"
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC, subtask_id DESC"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_subtask_from_row(row) for row in rows]


def _task_from_row(row: sqlite3.Row) -> TaskRow:
    return TaskRow(
        task_id=row["task_id"],
        description=row["description"],
        room_id=row["room_id"],
        created_at=row["created_at"],
        status=row["status"],
    )


def _subtask_from_row(row: sqlite3.Row) -> SubtaskRow:
    raw_metadata = row["metadata"]
    return SubtaskRow(
        subtask_id=row["subtask_id"],
        task_id=row["task_id"],
        state=row["state"],
        assigned_worker=row["assigned_worker"],
        pr_number=row["pr_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        metadata=json.loads(raw_metadata) if raw_metadata else None,
    )
