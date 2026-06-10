"""Tests for the durable SQLite state store (RFC Workstream 1 / Phase 1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codeband.state import StateStore, SubtaskRow, TaskRow


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """A StateStore backed by an isolated DB under tmp_path."""
    return StateStore(tmp_path / "state" / "orchestration.db")


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {name for (name,) in rows}


def test_schema_created_on_init(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "orchestration.db"
    StateStore(db_path)

    assert db_path.exists()
    tables = _table_names(db_path)
    assert {"tasks", "subtask_states", "transition_log"} <= tables


def test_init_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "orchestration.db"
    StateStore(db_path)
    # Re-opening the same DB must not raise (CREATE TABLE IF NOT EXISTS).
    StateStore(db_path)
    assert {"tasks", "subtask_states", "transition_log"} <= _table_names(db_path)


def test_create_task_then_get(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="do the thing", room_id="room-1")

    task = store.get_task("room-1")
    assert isinstance(task, TaskRow)
    assert task.task_id == "room-1"
    assert task.description == "do the thing"
    assert task.room_id == "room-1"
    assert task.status == "active"
    assert task.created_at  # ISO-8601 UTC timestamp populated


def test_create_task_with_owner_id_round_trips(store: StateStore) -> None:
    store.create_task(
        task_id="room-1",
        description="do the thing",
        room_id="room-1",
        owner_id="initiator-7",
    )

    task = store.get_task("room-1")
    assert task is not None
    assert task.owner_id == "initiator-7"


def test_create_task_owner_id_defaults_to_none(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="t", room_id="room-1")

    task = store.get_task("room-1")
    assert task is not None
    assert task.owner_id is None


def test_owner_id_migrated_onto_legacy_tasks_table(tmp_path: Path) -> None:
    """A pre-existing tasks table without ``owner_id`` is migrated in place.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op against the old table, so the
    guarded ALTER must add the column; legacy rows then read back with
    ``owner_id`` None and new writes can persist it.
    """
    db_path = tmp_path / "state" / "orchestration.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks ("
        "task_id TEXT PRIMARY KEY, description TEXT NOT NULL, "
        "room_id TEXT NOT NULL, created_at TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, description, room_id, created_at, status) "
        "VALUES ('old-1', 'legacy', 'old-1', '2020-01-01T00:00:00+00:00', 'active')"
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)  # runs the guarded migration

    legacy = store.get_task("old-1")
    assert legacy is not None
    assert legacy.owner_id is None  # backfilled, no KeyError on a pre-column row

    store.create_task(
        task_id="new-1", description="t", room_id="new-1", owner_id="owner-9",
    )
    assert store.get_task("new-1").owner_id == "owner-9"


def test_required_verdicts_and_head_sha_migrated_onto_legacy_schema(
    tmp_path: Path,
) -> None:
    """Pre-existing tasks / transition_log tables gain the Stage-2 columns.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op against old tables, so the
    guarded ALTERs must add ``tasks.required_verdicts`` and
    ``transition_log.head_sha``; legacy rows read back with both NULL (no
    KeyError, read path untouched) and new writes can persist them.
    """
    db_path = tmp_path / "state" / "orchestration.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE tasks ("
        "task_id TEXT PRIMARY KEY, description TEXT NOT NULL, "
        "room_id TEXT NOT NULL, created_at TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active', "
        "owner_id TEXT, owner_handle TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks (task_id, description, room_id, created_at, status) "
        "VALUES ('old-1', 'legacy', 'old-1', '2020-01-01T00:00:00+00:00', 'active')"
    )
    conn.execute(
        "CREATE TABLE transition_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "subtask_id TEXT NOT NULL, task_id TEXT NOT NULL, "
        "from_state TEXT NOT NULL, to_state TEXT NOT NULL, "
        "caller_role TEXT NOT NULL, timestamp TEXT NOT NULL, reason TEXT)"
    )
    conn.execute(
        "INSERT INTO transition_log "
        "(subtask_id, task_id, from_state, to_state, caller_role, timestamp) "
        "VALUES ('st-1', 'old-1', 'planned', 'assigned', 'conductor', "
        "'2020-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = StateStore(db_path)  # runs the guarded migrations

    legacy = store.get_task("old-1")
    assert legacy is not None
    assert legacy.required_verdicts is None  # legacy row tolerated as NULL

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM transition_log").fetchone()
        assert row["head_sha"] is None  # column added, legacy row NULL
    finally:
        conn.close()

    # New registration writes persist the snapshot through the migrated table.
    store.register_task_atomic(
        task_id="new-1",
        description="t",
        room_id="new-1",
        owner_id="owner-9",
        required_verdicts=["review"],
    )
    assert store.get_task("new-1").required_verdicts == ["review"]


def test_register_task_atomic_snapshot_roundtrip(store: StateStore) -> None:
    # Insert persists the resolved list; an update (re-register) overwrites it.
    store.register_task_atomic(
        task_id="room-1",
        description="t",
        room_id="room-1",
        owner_id="owner-1",
        required_verdicts=["verify", "review"],
    )
    assert store.get_task("room-1").required_verdicts == ["verify", "review"]

    outcome = store.register_task_atomic(
        task_id="room-1",
        description="ignored on update",
        room_id="room-1",
        owner_id="owner-2",
        required_verdicts=[],
    )
    assert outcome == "updated"
    task = store.get_task("room-1")
    assert task.required_verdicts == []  # empty snapshot is [] — not None
    assert task.owner_id == "owner-2"


def test_get_missing_task_returns_none(store: StateStore) -> None:
    assert store.get_task("nope") is None


def test_create_task_is_idempotent(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="first", room_id="room-1")
    # A retried kickoff against an existing DB must not raise or clobber.
    store.create_task(task_id="room-1", description="second", room_id="room-1")

    task = store.get_task("room-1")
    assert task is not None
    assert task.description == "first"  # INSERT OR IGNORE: original kept


def test_ensure_subtask_creates_row(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="t", room_id="room-1")
    store.ensure_subtask("sub-1", "room-1")

    sub = store.get_subtask("sub-1", "room-1")
    assert isinstance(sub, SubtaskRow)
    assert sub.subtask_id == "sub-1"
    assert sub.task_id == "room-1"
    assert sub.state == "planned"  # default
    assert sub.assigned_worker is None
    assert sub.pr_number is None
    assert sub.created_at and sub.updated_at


def test_ensure_subtask_is_idempotent(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="t", room_id="room-1")
    store.ensure_subtask("sub-1", "room-1")
    # Calling again is a no-op: no duplicate, no error.
    store.ensure_subtask("sub-1", "room-1")

    with sqlite3.connect(store.db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM subtask_states WHERE subtask_id = ?", ("sub-1",)
        ).fetchone()
    assert count == 1


def test_ensure_subtask_persists_metadata(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="t", room_id="room-1")
    store.ensure_subtask(
        "sub-1", "room-1", assigned_worker="coder-claude-1", metadata={"files": 3}
    )

    sub = store.get_subtask("sub-1", "room-1")
    assert sub is not None
    assert sub.assigned_worker == "coder-claude-1"
    assert sub.metadata == {"files": 3}


def test_get_missing_subtask_returns_none(store: StateStore) -> None:
    assert store.get_subtask("nope", "room-1") is None


def test_list_active_subtasks_excludes_terminal(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="t", room_id="room-1")
    store.ensure_subtask("active-1", "room-1", state="in_progress")
    store.ensure_subtask("active-2", "room-1", state="review_pending")
    store.ensure_subtask("done-1", "room-1", state="merged")
    store.ensure_subtask("dropped-1", "room-1", state="abandoned")

    active_ids = {s.subtask_id for s in store.list_active_subtasks()}
    assert active_ids == {"active-1", "active-2"}


def test_list_active_subtasks_scoped_by_task(store: StateStore) -> None:
    store.create_task(task_id="room-1", description="t", room_id="room-1")
    store.create_task(task_id="room-2", description="t2", room_id="room-2")
    store.ensure_subtask("a", "room-1", state="in_progress")
    store.ensure_subtask("b", "room-2", state="in_progress")

    scoped = {s.subtask_id for s in store.list_active_subtasks(task_id="room-1")}
    assert scoped == {"a"}


def test_concurrent_writers_do_not_corrupt(tmp_path: Path) -> None:
    """Two StateStore handles on the same DB interleave writes without error."""
    db_path = tmp_path / "state" / "orchestration.db"
    store_a = StateStore(db_path)
    store_b = StateStore(db_path)

    store_a.create_task(task_id="room-1", description="t", room_id="room-1")

    # Interleave writes from both handles against the shared WAL-mode file.
    for i in range(50):
        store_a.ensure_subtask(f"a-{i}", "room-1", state="in_progress")
        store_b.ensure_subtask(f"b-{i}", "room-1", state="in_progress")

    # Either handle sees every committed row; the DB is intact.
    all_ids = {s.subtask_id for s in store_b.list_active_subtasks()}
    expected = {f"a-{i}" for i in range(50)} | {f"b-{i}" for i in range(50)}
    assert all_ids == expected

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
