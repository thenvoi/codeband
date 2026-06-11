"""SQLite-backed durable state store (RFC Workstream 1).

A single local SQLite database at ``{workspace_path}/state/orchestration.db``
holds three tables:

* ``tasks`` — one row per task (keyed by ``room_id``).
* ``subtask_states`` — one row per ``(task_id, subtask_id)``, its current FSM
  state plus assignment / PR / metadata. Subtask identity is task-scoped:
  planners number subtasks ``st-1``, ``st-2``, … fresh per plan, so a bare
  ``subtask_id`` is NOT unique across tasks in a reused DB.
* ``transition_log`` — append-only audit of every state transition, keyed by
  ``(task_id, subtask_id)`` like the rows it audits.

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
(``state/fsm.py``, Workstream 2), which also promotes ``tasks.status`` to
``'completed'`` when a task's last subtask merges; this module never enforces
transitions.
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
    # 'active' on registration; 'superseded' when a different task replaces it
    # (state/registration.py); 'completed' when the FSM merges the task's last
    # subtask (state/fsm.py — same single-writer transaction as the subtask).
    status: str = "active"
    # Band participant id of the task initiator (whoever held BAND_API_KEY at
    # kickoff, or whoever seeded the room via ``cb register-task``). Nullable —
    # predates the column on older DBs. The watchdog reads it to @mention the
    # initiator when one of the subtask's caps trips it into ``blocked``.
    # ``register_task`` (state/registration.py) requires it on every new row.
    owner_id: str | None = None
    # Human-readable handle/display name for the owner (e.g. a jam bridge
    # handle like ``yoni/claude-lyra-5ebd4a``). Informational only — mentions
    # use ``owner_id``; nullable like ``owner_id`` and for the same reasons.
    owner_handle: str | None = None
    # Verdict legs this task requires before merge, resolved from config at
    # registration time and snapshotted here (JSON list in the DB) so a
    # mid-task config edit cannot change an in-flight task's requirements.
    # Nullable — predates the column on older DBs. ``register_task`` writes it
    # on every registration, including re-registration (snapshot refresh); the
    # merge-eligibility gate (state/fsm.py) reads it.
    required_verdicts: list[str] | None = None
    # Who approves a ``cb-phase merge`` for this task — ``"owner"`` or
    # ``"human:<handle>"``, resolved and validated from config at registration
    # time and snapshotted here exactly like ``required_verdicts``. Nullable —
    # predates the column on older DBs; the merge leg treats ``NULL`` as the
    # default ``"owner"`` (approval is never silently skipped).
    merge_approval: str | None = None


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
    # Count of completed review rounds — incremented by the FSM each time the
    # subtask *enters* ``review_failed`` (one failed review = one round). Durable
    # so the per-subtask review-round cap survives a crash/reopen mid-loop and
    # cannot be reset by rehydration. Distinct from the watchdog's stall counter.
    review_round: int = 0
    # Count of *rejected* ``cb-phase verify`` attempts over the subtask's whole
    # life — incremented by the handoff CLI each time a verify gate (clean tree /
    # open PR / verify command) fails, never on success. Durable and cumulative
    # (never reset on rework), so the per-subtask verify-attempt cap survives a
    # crash/reopen and cannot be gamed by bouncing through review. Bounds the
    # productive verify-rejection loop the watchdog's stall cap cannot catch —
    # the coder commits real code each attempt, so git HEAD keeps advancing.
    # Distinct from both ``review_round`` and the watchdog's stall counter.
    verify_attempts: int = 0
    # Count of merge-gate send-backs — incremented by the FSM each time the
    # subtask *enters* ``needs_rebase`` (one send-back = one rebase round).
    # Durable, like ``review_round``, so the per-subtask rebase-round cap
    # survives a crash/reopen mid-loop. Bounds the rebase loop neither sibling
    # cap can see: each round writes fresh transition rows (so the watchdog's
    # stall cap by construction never fires) and never enters ``review_failed``
    # (so the review-round cap never counts it).
    rebase_rounds: int = 0
    # ── Merge-approval grant (Stage-2 merge leg) ─────────────────────────────
    # The durable record ``cb-phase merge`` queries before executing a merge.
    # ``cb approve`` writes the grant, SHA-pinned to the PR head it approved
    # (``merge_approved_sha``); the merge leg proceeds only when the grant's
    # SHA equals the SHA recorded on the ``merge_pending`` transition, so a
    # grant from a pre-rebase round can never authorize a different commit.
    # ``merge_approved_by`` records on whose authority the grant stands (the
    # task's snapshotted approver spec, e.g. ``owner`` / ``human:<handle>``).
    merge_approved_by: str | None = None
    merge_approved_sha: str | None = None
    # SHA the most recent approval *request* was sent for — the send-once
    # marker, burned only after a successful send (marker-after-send, same
    # policy as the watchdog escalations). SHA-scoped so a needs_rebase round
    # trip (new merge_pending SHA) naturally re-requests approval.
    merge_approval_requested_sha: str | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id           TEXT PRIMARY KEY,
    description       TEXT NOT NULL,
    room_id           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',
    owner_id          TEXT,
    owner_handle      TEXT,
    required_verdicts TEXT,
    merge_approval    TEXT
);

CREATE TABLE IF NOT EXISTS subtask_states (
    subtask_id      TEXT NOT NULL,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id),
    state           TEXT NOT NULL DEFAULT 'planned',
    assigned_worker TEXT,
    pr_number       INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    metadata        TEXT,
    review_round    INTEGER NOT NULL DEFAULT 0,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    rebase_rounds   INTEGER NOT NULL DEFAULT 0,
    merge_approved_by             TEXT,
    merge_approved_sha            TEXT,
    merge_approval_requested_sha  TEXT,
    PRIMARY KEY (task_id, subtask_id)
);

CREATE TABLE IF NOT EXISTS transition_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subtask_id  TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    caller_role TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    reason      TEXT,
    head_sha    TEXT
);

-- Every hot transition_log query (merge-eligibility legs, the watchdog's
-- recency/blocked-reason readers, the merge leg's pending-SHA anchor) filters
-- on exactly (task_id, subtask_id). ``IF NOT EXISTS`` + running on every
-- ``StateStore`` construction makes this both the fresh-DB path and the
-- migration path for pre-index DBs.
CREATE INDEX IF NOT EXISTS idx_transition_log_task_subtask
    ON transition_log(task_id, subtask_id);
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
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply additive column migrations to a pre-existing schema.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op against a DB created by an
        older version, so a column added after first release must be patched in
        with ``ALTER TABLE``. Each migration is guarded on ``PRAGMA
        table_info`` so it runs at most once and never on a fresh DB. The
        ``DEFAULT 0`` backfills existing rows, so a subtask that predates the
        review-round cap simply starts the loop at round 0.

        Structural changes are NOT migrated: the task-scoped composite key on
        ``subtask_states`` / ``transition_log`` only applies to freshly created
        tables (``CREATE TABLE IF NOT EXISTS`` cannot rewrite a PRIMARY KEY),
        so pre-existing dev ``orchestration.db`` files must be deleted.
        """
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(subtask_states)").fetchall()
        }
        if "review_round" not in cols:
            conn.execute(
                "ALTER TABLE subtask_states "
                "ADD COLUMN review_round INTEGER NOT NULL DEFAULT 0"
            )
        if "verify_attempts" not in cols:
            conn.execute(
                "ALTER TABLE subtask_states "
                "ADD COLUMN verify_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "rebase_rounds" not in cols:
            conn.execute(
                "ALTER TABLE subtask_states "
                "ADD COLUMN rebase_rounds INTEGER NOT NULL DEFAULT 0"
            )
        if "merge_approved_by" not in cols:
            conn.execute(
                "ALTER TABLE subtask_states ADD COLUMN merge_approved_by TEXT"
            )
        if "merge_approved_sha" not in cols:
            conn.execute(
                "ALTER TABLE subtask_states ADD COLUMN merge_approved_sha TEXT"
            )
        if "merge_approval_requested_sha" not in cols:
            conn.execute(
                "ALTER TABLE subtask_states "
                "ADD COLUMN merge_approval_requested_sha TEXT"
            )
        task_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "owner_id" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT")
        if "owner_handle" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner_handle TEXT")
        if "required_verdicts" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN required_verdicts TEXT")
        if "merge_approval" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN merge_approval TEXT")
        log_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(transition_log)").fetchall()
        }
        if "head_sha" not in log_cols:
            conn.execute("ALTER TABLE transition_log ADD COLUMN head_sha TEXT")

    # ── tasks ──────────────────────────────────────────────────────────────

    def create_task(
        self,
        task_id: str,
        description: str,
        room_id: str,
        *,
        created_at: str | None = None,
        status: str = "active",
        owner_id: str | None = None,
    ) -> None:
        """Insert a task row (idempotent on ``task_id``).

        Internal to task registration — do not call directly; use
        :func:`codeband.state.registration.register_task`, the sole writer of
        "a task exists". Re-creating the same task is a no-op rather than an
        error, so a retried registration against an existing DB stays safe.
        """
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(task_id, description, room_id, created_at, status, owner_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    description,
                    room_id,
                    created_at or _now_iso(),
                    status,
                    owner_id,
                ),
            )

    def register_task_atomic(
        self,
        *,
        task_id: str,
        description: str,
        room_id: str,
        owner_id: str,
        required_verdicts: list[str],
        merge_approval: str = "owner",
        owner_handle: str | None = None,
        supersede_task_id: str | None = None,
    ) -> str:
        """Apply one task registration's DB mutations in a single transaction.

        Status-update + upsert support for ``register_task``
        (``state/registration.py``) — the supersede of the previously active
        task and the insert/update of the registered one must land atomically,
        so a crash between them cannot leave two active tasks or none:

        * If ``supersede_task_id`` is given, that row's status is set to
          ``'superseded'`` (idempotent UPDATE; a missing row is a no-op).
        * If a row for ``task_id`` already exists, ``owner_id`` /
          ``owner_handle`` / ``required_verdicts`` / ``merge_approval`` are
          updated AND ``status`` is restored to ``'active'`` — description and
          created_at are deliberately left untouched (re-registration changes
          ownership and refreshes the verdict + approver snapshots from
          *current* config, not history). Restoring ``'active'`` is what makes
          re-registering a previously ``'superseded'`` room (the sanctioned
          identity-rotation path) leave exactly one active task — without it
          the system ends with ZERO active tasks: the watchdog patrols
          nothing and completion promotion never fires while ``cb-phase``
          keeps advancing subtasks. Re-registering a ``'completed'`` task's
          room reactivates it too — intended continue-work semantics: the
          owner is deliberately pointing new work at the finished task's room.
        * Otherwise a fresh ``'active'`` row is inserted.

        ``required_verdicts`` and ``merge_approval`` are already resolved and
        validated by ``register_task`` — this method only persists them
        (the verdict list JSON-encoded, the approver spec verbatim).

        Returns ``"inserted"`` or ``"updated"`` so the caller can report the
        outcome without a second read.
        """
        verdicts_json = json.dumps(required_verdicts)
        with self._transaction() as conn:
            if supersede_task_id is not None:
                conn.execute(
                    "UPDATE tasks SET status = 'superseded' WHERE task_id = ?",
                    (supersede_task_id,),
                )
            existing = conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE tasks SET owner_id = ?, owner_handle = ?, "
                    "required_verdicts = ?, merge_approval = ?, "
                    "status = 'active' "
                    "WHERE task_id = ?",
                    (owner_id, owner_handle, verdicts_json, merge_approval,
                     task_id),
                )
                return "updated"
            conn.execute(
                "INSERT INTO tasks "
                "(task_id, description, room_id, created_at, status, "
                "owner_id, owner_handle, required_verdicts, merge_approval) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (
                    task_id,
                    description,
                    room_id,
                    _now_iso(),
                    owner_id,
                    owner_handle,
                    verdicts_json,
                    merge_approval,
                ),
            )
            return "inserted"

    def get_task(self, task_id: str) -> TaskRow | None:
        """Return the task row, or ``None`` if it does not exist."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def list_active_task_room_ids(self) -> list[str]:
        """Room ids of all ``'active'`` tasks.

        Powers the local-mode startup room sweep (``runner``): rooms tied to
        an active task are rejoined on reconnect, everything else is skipped.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT room_id FROM tasks WHERE status = 'active'"
            ).fetchall()
        return [row["room_id"] for row in rows]

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

    def get_subtask(self, subtask_id: str, task_id: str) -> SubtaskRow | None:
        """Return the subtask row for ``(task_id, subtask_id)``, or ``None``.

        Subtask ids are only unique *within* a task (planners emit ``st-1``,
        ``st-2``, … fresh per plan), so the lookup requires both keys — there
        is deliberately no unscoped fallback.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM subtask_states "
                "WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
        return _subtask_from_row(row) if row is not None else None

    def increment_verify_attempts(self, subtask_id: str, task_id: str) -> int:
        """Atomically bump ``verify_attempts`` for a subtask; return the new count.

        Called by the ``cb-phase`` handoff CLI on each *rejected* verify attempt
        (a failed gate), never on success. The bump is a single ``UPDATE`` inside
        a short transaction, so it is durable the instant it commits and survives
        a crash/reopen — a coder that crashes mid-loop cannot reset its budget.
        Treats an absent ``verify_attempts`` value as 0 via ``COALESCE`` for
        defence against any pre-migration NULL. Returns the post-increment count
        (``0`` if the subtask row does not exist, i.e. nothing was updated) so the
        caller can compare against the cap without a second read.
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE subtask_states "
                "SET verify_attempts = COALESCE(verify_attempts, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (_now_iso(), task_id, subtask_id),
            )
            row = conn.execute(
                "SELECT verify_attempts FROM subtask_states "
                "WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
        return row["verify_attempts"] if row is not None else 0

    def set_pr_number(self, subtask_id: str, task_id: str, pr_number: int) -> None:
        """Persist the subtask's PR number (idempotent UPDATE).

        Written by ``cb-phase merge`` on its first invocation (the only leg
        that needs the binding durably): the crash-reconcile path re-invokes
        with no arguments and reads this back to query the PR's state. Also
        feeds the watchdog's existing PR-activity progress signal.
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE subtask_states SET pr_number = ?, updated_at = ? "
                "WHERE task_id = ? AND subtask_id = ?",
                (pr_number, _now_iso(), task_id, subtask_id),
            )

    def record_merge_approval(
        self,
        subtask_id: str,
        task_id: str,
        *,
        approved_by: str,
        approved_sha: str,
    ) -> None:
        """Durably record a merge-approval grant, SHA-pinned.

        Written by ``cb approve`` (the single human-facing approval entry
        point). ``approved_sha`` is the PR head SHA at approval time;
        ``cb-phase merge`` executes only when it equals the SHA recorded on
        the ``merge_pending`` transition, so a push after approval (or a grant
        from a pre-rebase round) can never authorize a different commit.
        Re-approval overwrites the previous grant (latest grant wins).
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE subtask_states "
                "SET merge_approved_by = ?, merge_approved_sha = ?, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (approved_by, approved_sha, _now_iso(), task_id, subtask_id),
            )

    def mark_merge_approval_requested(
        self, subtask_id: str, task_id: str, requested_sha: str,
    ) -> None:
        """Burn the send-once marker for an approval request at ``requested_sha``.

        Called by ``cb-phase merge`` strictly *after* a successful request
        send (marker-after-send — a failed send retries on the next
        invocation). SHA-scoped: a later ``merge_pending`` round at a new SHA
        compares unequal and re-requests.
        """
        with self._transaction() as conn:
            conn.execute(
                "UPDATE subtask_states "
                "SET merge_approval_requested_sha = ?, updated_at = ? "
                "WHERE task_id = ? AND subtask_id = ?",
                (requested_sha, _now_iso(), task_id, subtask_id),
            )

    def find_subtasks_by_pr(self, task_id: str, pr_number: int) -> list[SubtaskRow]:
        """Return this task's subtasks bound to ``pr_number``, oldest first.

        Used by ``cb approve <pr>`` to resolve which subtask a PR-keyed grant
        lands on. Only finds subtasks whose ``pr_number`` was persisted (i.e.
        ones that have entered the ``cb-phase merge`` leg) — legacy chat-only
        flows match nothing and record no grant.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM subtask_states "
                "WHERE task_id = ? AND pr_number = ? "
                "ORDER BY created_at ASC, subtask_id ASC",
                (task_id, pr_number),
            ).fetchall()
        return [_subtask_from_row(row) for row in rows]

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
    # ``owner_id`` / ``owner_handle`` may be absent on rows fetched before the
    # migration ran (or in a hand-built row in tests); tolerate their absence
    # rather than KeyError.
    keys = row.keys()
    raw_verdicts = row["required_verdicts"] if "required_verdicts" in keys else None
    return TaskRow(
        task_id=row["task_id"],
        description=row["description"],
        room_id=row["room_id"],
        created_at=row["created_at"],
        status=row["status"],
        owner_id=row["owner_id"] if "owner_id" in keys else None,
        owner_handle=row["owner_handle"] if "owner_handle" in keys else None,
        required_verdicts=json.loads(raw_verdicts) if raw_verdicts else None,
        merge_approval=row["merge_approval"] if "merge_approval" in keys else None,
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
        review_round=row["review_round"],
        verify_attempts=row["verify_attempts"],
        rebase_rounds=row["rebase_rounds"],
        merge_approved_by=row["merge_approved_by"],
        merge_approved_sha=row["merge_approved_sha"],
        merge_approval_requested_sha=row["merge_approval_requested_sha"],
    )
