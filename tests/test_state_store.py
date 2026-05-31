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

    sub = store.get_subtask("sub-1")
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

    sub = store.get_subtask("sub-1")
    assert sub is not None
    assert sub.assigned_worker == "coder-claude-1"
    assert sub.metadata == {"files": 3}


def test_get_missing_subtask_returns_none(store: StateStore) -> None:
    assert store.get_subtask("nope") is None


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
