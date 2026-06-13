"""Tests for Stage-3 evidence integrity (PR1).

The transition_log and audit_log hash chains, the ``cb verify-log`` command,
and the migration backfill over a populated legacy DB. The watchdog's
incremental integrity rung is exercised in ``test_watchdog_upgrade.py`` (it
reuses the owner-escalation scaffolding there).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeband.state import (
    AUDIT_HASH_COLS,
    GENESIS_PREV_HASH,
    StateStore,
    TRANSITION_HASH_COLS,
    verify_chain,
)
from codeband.state.fsm import transition
from codeband.state.store import write_chained_transition

TASK_ID = "task-1"
ROOM_ID = "room-1"
SUBTASK_ID = "st-1"


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(TASK_ID, "demo", ROOM_ID, owner_id="owner-1")
    return s


def _open(store: StateStore) -> sqlite3.Connection:
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _walk(store: StateStore) -> None:
    """Drive one subtask through a realistic FSM lifecycle (several rows)."""
    transition(SUBTASK_ID, TASK_ID, "assigned", caller_role="conductor", store=store)
    transition(SUBTASK_ID, TASK_ID, "in_progress", caller_role="coder", store=store)
    transition(SUBTASK_ID, TASK_ID, "verify_pending", caller_role="coder", store=store)
    transition(SUBTASK_ID, TASK_ID, "review_pending", caller_role="coder",
               store=store, head_sha="deadbeef")
    transition(SUBTASK_ID, TASK_ID, "review_failed", caller_role="reviewer",
               store=store, head_sha="deadbeef")


# ── transition_log chain ────────────────────────────────────────────────────

def test_chain_continuous_across_real_fsm_walk(store: StateStore) -> None:
    _walk(store)
    conn = _open(store)
    try:
        result = verify_chain(conn, "transition_log", TRANSITION_HASH_COLS)
    finally:
        conn.close()
    assert result.ok
    assert result.row_count == 5
    assert result.head_hash is not None


def test_first_row_links_to_genesis(store: StateStore) -> None:
    _walk(store)
    conn = _open(store)
    try:
        first = conn.execute(
            "SELECT prev_hash FROM transition_log ORDER BY id ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert first["prev_hash"] == GENESIS_PREV_HASH


def test_in_place_edit_breaks_chain_at_exact_row(store: StateStore) -> None:
    _walk(store)
    # Tamper with the third row's business column (reason) WITHOUT recomputing
    # its hash — exactly what an out-of-band sqlite3 edit looks like.
    conn = _open(store)
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM transition_log ORDER BY id ASC"
        ).fetchall()]
        target = ids[2]
        conn.execute(
            "UPDATE transition_log SET reason = 'tampered' WHERE id = ?",
            (target,),
        )
        conn.commit()
        result = verify_chain(conn, "transition_log", TRANSITION_HASH_COLS)
    finally:
        conn.close()
    assert not result.ok
    assert result.broken_id == target
    assert result.expected_hash != result.actual_hash


def test_empty_chain_is_vacuously_ok(store: StateStore) -> None:
    conn = _open(store)
    try:
        result = verify_chain(conn, "transition_log", TRANSITION_HASH_COLS)
    finally:
        conn.close()
    assert result.ok
    assert result.row_count == 0


def test_head_sha_participates_in_row_hash(tmp_path: Path) -> None:
    """Two rows identical in every column EXCEPT head_sha hash differently.

    Proves head_sha is inside the hashed business set: same id (1), same genesis
    prev_hash, same everything else — only head_sha varies, so an equal row_hash
    would mean head_sha was not being hashed. Uses fresh isolated stores so each
    is a genuine first/genesis row.
    """
    def _genesis_row_hash(head_sha: str, sub: str) -> str:
        s = StateStore(tmp_path / sub / "orchestration.db")
        conn = _open(s)
        try:
            write_chained_transition(
                conn,
                subtask_id=SUBTASK_ID,
                task_id=TASK_ID,
                from_state="review_pending",
                to_state="merge_pending",
                caller_role="coder",
                timestamp="2026-01-01T00:00:00+00:00",
                reason="ready to merge",
                head_sha=head_sha,
            )
            conn.commit()
            row = conn.execute(
                "SELECT row_hash FROM transition_log ORDER BY id ASC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return row["row_hash"]

    assert _genesis_row_hash("sha-aaaa", "a") != _genesis_row_hash("sha-bbbb", "b")


def test_in_place_head_sha_edit_breaks_chain(store: StateStore) -> None:
    """Editing only head_sha out-of-band is tamper-evident via verify_chain.

    The merge gate pins on head_sha, so a forged SHA must break the chain. _walk
    writes head_sha='deadbeef' on the review_pending row; we mutate it in place
    WITHOUT recomputing the hash (an out-of-band sqlite3 edit) and assert the
    chain breaks at exactly that row.
    """
    _walk(store)
    conn = _open(store)
    try:
        target = conn.execute(
            "SELECT id FROM transition_log WHERE to_state = 'review_pending'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE transition_log SET head_sha = 'forgedsha' WHERE id = ?",
            (target,),
        )
        conn.commit()
        result = verify_chain(conn, "transition_log", TRANSITION_HASH_COLS)
    finally:
        conn.close()
    assert not result.ok
    assert result.broken_id == target
    assert result.expected_hash != result.actual_hash


# ── audit_log chain + appends ────────────────────────────────────────────────

def test_pr_number_binding_appends_audit_row(store: StateStore) -> None:
    store.ensure_subtask(SUBTASK_ID, TASK_ID)
    store.set_pr_number(SUBTASK_ID, TASK_ID, 42)
    conn = _open(store)
    try:
        rows = conn.execute(
            "SELECT event_type, payload FROM audit_log"
        ).fetchall()
        result = verify_chain(conn, "audit_log", AUDIT_HASH_COLS)
    finally:
        conn.close()
    assert [r["event_type"] for r in rows] == ["pr_number_binding"]
    assert "42" in rows[0]["payload"]
    assert result.ok and result.row_count == 1


def test_marker_write_appends_audit_row(store: StateStore) -> None:
    store.ensure_subtask(SUBTASK_ID, TASK_ID)
    store.mark_merge_approval_requested(SUBTASK_ID, TASK_ID, "abc123")
    conn = _open(store)
    try:
        rows = conn.execute("SELECT event_type FROM audit_log").fetchall()
    finally:
        conn.close()
    assert [r["event_type"] for r in rows] == ["approval_request"]


def test_grant_appends_audit_row_and_reapproval_is_append_only(store: StateStore) -> None:
    """Re-approval appends a SECOND audit row but leaves ONE current grant."""
    store.ensure_subtask(SUBTASK_ID, TASK_ID)
    store.record_merge_approval(SUBTASK_ID, TASK_ID, approved_by="owner", approved_sha="sha-1")
    store.record_merge_approval(SUBTASK_ID, TASK_ID, approved_by="owner", approved_sha="sha-2")

    conn = _open(store)
    try:
        audit = conn.execute(
            "SELECT payload FROM audit_log WHERE event_type = 'approval_grant' "
            "ORDER BY id ASC"
        ).fetchall()
        result = verify_chain(conn, "audit_log", AUDIT_HASH_COLS)
    finally:
        conn.close()

    # Two audit rows (history is append-only)...
    assert len(audit) == 2
    assert "sha-1" in audit[0]["payload"]
    assert "sha-2" in audit[1]["payload"]
    # ...one current grant on the live columns (latest wins).
    sub = store.get_subtask(SUBTASK_ID, TASK_ID)
    assert sub.merge_approved_sha == "sha-2"
    assert result.ok


def test_audit_chain_break_detected(store: StateStore) -> None:
    store.ensure_subtask(SUBTASK_ID, TASK_ID)
    store.set_pr_number(SUBTASK_ID, TASK_ID, 1)
    store.set_pr_number(SUBTASK_ID, TASK_ID, 2)
    conn = _open(store)
    try:
        first = conn.execute("SELECT id FROM audit_log ORDER BY id ASC LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE audit_log SET payload = '{\"pr_number\": 999}' WHERE id = ?", (first,))
        conn.commit()
        result = verify_chain(conn, "audit_log", AUDIT_HASH_COLS)
    finally:
        conn.close()
    assert not result.ok
    assert result.broken_id == first


# ── migration backfill over a populated legacy DB ────────────────────────────

def test_migration_backfills_chain_over_legacy_rows(tmp_path: Path) -> None:
    """A pre-Stage-3 DB (no chain columns) gets a valid chain on next open."""
    db_path = tmp_path / "state" / "orchestration.db"
    db_path.parent.mkdir(parents=True)

    # Hand-build a legacy transition_log WITHOUT the chain columns.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE transition_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subtask_id  TEXT NOT NULL,
            task_id     TEXT NOT NULL,
            from_state  TEXT NOT NULL,
            to_state    TEXT NOT NULL,
            caller_role TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            reason      TEXT
        );
        """
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO transition_log "
            "(subtask_id, task_id, from_state, to_state, caller_role, timestamp, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("st-1", TASK_ID, "a", "b", "coder", f"2026-01-0{i+1}T00:00:00+00:00", f"r{i}"),
        )
    conn.commit()
    conn.close()

    # Opening the store runs the guarded migration + backfill.
    StateStore(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transition_log)").fetchall()}
        assert {"prev_hash", "row_hash"} <= cols
        result = verify_chain(conn, "transition_log", TRANSITION_HASH_COLS)
        first = conn.execute(
            "SELECT prev_hash FROM transition_log ORDER BY id ASC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert result.ok and result.row_count == 3
    assert first["prev_hash"] == GENESIS_PREV_HASH


# ── cb verify-log command ────────────────────────────────────────────────────

def _project_with_db(tmp_path: Path) -> tuple[Path, StateStore]:
    project = tmp_path / "proj"
    project.mkdir()
    ws = tmp_path / "ws"
    (project / "codeband.yaml").write_text(
        "repo:\n"
        "  url: https://github.com/o/r.git\n"
        "  branch: main\n"
        "workspace:\n"
        f"  path: {ws}\n"
    )
    store = StateStore(ws / "state" / "orchestration.db")
    store.create_task(TASK_ID, "demo", ROOM_ID, owner_id="owner-1")
    return project, store


def test_verify_log_ok_exit_zero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEBAND_PROJECT_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)
    project, store = _project_with_db(tmp_path)
    _walk(store)

    from codeband.cli import cli

    result = CliRunner().invoke(cli, ["verify-log", "--dir", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "transition_log" in result.output
    assert "audit_log" in result.output


def test_verify_log_break_names_row_and_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEBAND_PROJECT_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)
    project, store = _project_with_db(tmp_path)
    _walk(store)

    conn = sqlite3.connect(store.db_path)
    ids = [r[0] for r in conn.execute("SELECT id FROM transition_log ORDER BY id ASC").fetchall()]
    target = ids[1]
    conn.execute("UPDATE transition_log SET to_state = 'forged' WHERE id = ?", (target,))
    conn.commit()
    conn.close()

    from codeband.cli import cli

    result = CliRunner().invoke(cli, ["verify-log", "--dir", str(project)])
    assert result.exit_code != 0
    assert f"id={target}" in result.output
