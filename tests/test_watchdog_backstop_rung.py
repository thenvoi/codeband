"""Tests for the approval→merge durable-state backstop rung.

Covers:
- StateStore.latest_audit_events: ordering, event_type filter, payload decode,
  empty/invalid payload handling.
- WatchdogDaemon._maybe_backstop_renudge: no-grant path, within-window path,
  window-elapsed-fires path, post-fire marker dedup, cap-hit path, new-SHA
  re-arm.
- _check_one_subtask integration: grant-present merge_pending early-returns
  (no stall counter advance), falls through after cap.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeband.config import WatchdogConfig
from codeband.state import StateStore

TASK_ID = "task-backstop-1"
SUBTASK_ID = "st-1"
ROOM_ID = "room-backstop-1"
APPROVED_SHA = "abc1234"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(TASK_ID, "backstop test task", ROOM_ID, owner_id="owner-1")
    s.ensure_subtask(SUBTASK_ID, TASK_ID, state="in_progress")
    return s


def _set_merge_pending(store: StateStore, *, approved_sha: str) -> None:
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE subtask_states SET state = 'merge_pending', "
        "merge_approved_sha = ?, metadata = ? WHERE subtask_id = ? AND task_id = ?",
        (approved_sha, json.dumps({"branch": "feature-backstop"}), SUBTASK_ID, TASK_ID),
    )
    conn.commit()
    conn.close()


def _insert_audit_row(
    store: StateStore,
    event_type: str,
    *,
    ts: str,
    payload: dict | None = None,
) -> None:
    """Directly insert an audit_log row (bypasses hash chain — for tests only)."""
    conn = sqlite3.connect(store.db_path)
    payload_json = json.dumps(payload) if payload is not None else None
    conn.execute(
        "INSERT INTO audit_log "
        "(ts, event_type, task_id, subtask_id, payload, "
        "actor_cwd, actor_pid, actor_role, prev_hash, row_hash) "
        "VALUES (?, ?, ?, ?, ?, '', 0, '', '', '')",
        (ts, event_type, TASK_ID, SUBTASK_ID, payload_json),
    )
    conn.commit()
    conn.close()


def _mock_rest(mm_id: str = "agent-mm") -> MagicMock:
    rest = MagicMock()
    rest.agent_api_messages = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    rest.agent_api_memories = MagicMock()
    rest.agent_api_memories.create_agent_memory = AsyncMock()
    return rest


def _daemon(
    store: StateStore,
    *,
    rest: MagicMock | None = None,
    config: WatchdogConfig | None = None,
    mm_id: str = "agent-mm",
) -> "WatchdogDaemon":  # noqa: F821
    from codeband.agents.watchdog import WatchdogDaemon

    return WatchdogDaemon(
        config=config or WatchdogConfig(
            max_phase_visits=5,
            git_progress_check=True,
            merge_approval_backstop_seconds=240,
            merge_approval_backstop_max_renudges=1,
        ),
        rest_client=rest or _mock_rest(mm_id),
        agent_id="agent-wd",
        conductor_id="agent-cond",
        state_store=store,
        agent_id_to_role={mm_id: "mergemaster"},
    )


def _ts(minutes_ago: float = 0.0) -> str:
    dt = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return dt.isoformat()


# ── StateStore.latest_audit_events ───────────────────────────────────────────


class TestLatestAuditEvents:
    def test_returns_empty_when_no_rows(self, store: StateStore) -> None:
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant",),
        )
        assert rows == []

    def test_filters_event_type(self, store: StateStore) -> None:
        _insert_audit_row(
            store, "approval_grant", ts=_ts(10),
            payload={"approved_sha": APPROVED_SHA},
        )
        _insert_audit_row(
            store, "other_event", ts=_ts(5),
            payload={"foo": "bar"},
        )
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant",),
        )
        assert len(rows) == 1
        assert rows[0][0] == "approval_grant"

    def test_returns_newest_first(self, store: StateStore) -> None:
        _insert_audit_row(
            store, "approval_grant", ts=_ts(20),
            payload={"approved_sha": "sha1"},
        )
        _insert_audit_row(
            store, "merge_backstop_nudge", ts=_ts(5),
            payload={"approved_sha": "sha2"},
        )
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant", "merge_backstop_nudge"),
        )
        assert len(rows) == 2
        assert rows[0][0] == "merge_backstop_nudge"  # newer first
        assert rows[1][0] == "approval_grant"

    def test_decodes_payload_dict(self, store: StateStore) -> None:
        _insert_audit_row(
            store, "approval_grant", ts=_ts(5),
            payload={"approved_sha": APPROVED_SHA, "approved_by": "human-1"},
        )
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant",),
        )
        assert rows[0][2] == {"approved_sha": APPROVED_SHA, "approved_by": "human-1"}

    def test_null_payload_returns_none(self, store: StateStore) -> None:
        _insert_audit_row(store, "approval_grant", ts=_ts(5), payload=None)
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant",),
        )
        assert rows[0][2] is None

    def test_invalid_json_payload_returns_none(self, store: StateStore) -> None:
        # Directly write a broken payload string
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "INSERT INTO audit_log "
            "(ts, event_type, task_id, subtask_id, payload, "
            "actor_cwd, actor_pid, actor_role, prev_hash, row_hash) "
            "VALUES (?, 'approval_grant', ?, ?, 'NOT JSON', '', 0, '', '', '')",
            (_ts(3), TASK_ID, SUBTASK_ID),
        )
        conn.commit()
        conn.close()
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant",),
        )
        assert rows[0][2] is None

    def test_empty_event_types_returns_empty(self, store: StateStore) -> None:
        _insert_audit_row(store, "approval_grant", ts=_ts(5))
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=(),
        )
        assert rows == []

    def test_scoped_to_task_and_subtask(self, store: StateStore) -> None:
        store.create_task("other-task", "other", "other-room")
        store.ensure_subtask("other-sub", "other-task", state="in_progress")
        _insert_audit_row(
            store, "approval_grant", ts=_ts(5),
            payload={"approved_sha": APPROVED_SHA},
        )
        # Rows for other task/subtask must not appear
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "INSERT INTO audit_log "
            "(ts, event_type, task_id, subtask_id, payload, "
            "actor_cwd, actor_pid, actor_role, prev_hash, row_hash) "
            "VALUES (?, 'approval_grant', 'other-task', 'other-sub', NULL, '', 0, '', '', '')",
            (_ts(2),),
        )
        conn.commit()
        conn.close()
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("approval_grant",),
        )
        assert len(rows) == 1
        assert rows[0][0] == "approval_grant"


# ── _maybe_backstop_renudge ───────────────────────────────────────────────────


class TestMaybeBackstopRenudge:
    @pytest.mark.asyncio
    async def test_no_grant_returns_false(self, store: StateStore) -> None:
        """No audit_grant row → rung returns False (stall path owns it)."""
        sub = _sub_row(approved_sha=APPROVED_SHA)
        daemon = _daemon(store)
        now = datetime.now(UTC)
        result = await daemon._maybe_backstop_renudge(sub, APPROVED_SHA, now)
        assert result is False

    @pytest.mark.asyncio
    async def test_within_window_returns_true_no_send(
        self, store: StateStore,
    ) -> None:
        """Grant present but within the staleness window → return True, no send."""
        _insert_audit_row(
            store, "approval_grant", ts=_ts(1),  # 1 min ago, window=4 min
            payload={"approved_sha": APPROVED_SHA},
        )
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)
        now = datetime.now(UTC)
        result = await daemon._maybe_backstop_renudge(sub=_sub_row(), approved_sha=APPROVED_SHA, now=now)
        assert result is True
        rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_window_elapsed_fires_once_writes_marker(
        self, store: StateStore,
    ) -> None:
        """Grant past the staleness window → sends renudge, writes marker, True."""
        _insert_audit_row(
            store, "approval_grant", ts=_ts(10),  # 10 min ago, window=4 min
            payload={"approved_sha": APPROVED_SHA},
        )
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)
        now = datetime.now(UTC)
        result = await daemon._maybe_backstop_renudge(_sub_row(), APPROVED_SHA, now)
        assert result is True
        rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()

        # durable marker written
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("merge_backstop_nudge",),
        )
        assert len(rows) == 1
        assert rows[0][2]["approved_sha"] == APPROVED_SHA

    @pytest.mark.asyncio
    async def test_after_fire_within_window_owns_patrol(
        self, store: StateStore,
    ) -> None:
        """After the first fire, a second call within the inter-nudge window
        returns True (no send) when the cap allows another nudge."""
        # cap=2: one nudge already sent, one remaining — within-window check applies
        _insert_audit_row(
            store, "approval_grant", ts=_ts(10),
            payload={"approved_sha": APPROVED_SHA},
        )
        # pre-insert one nudge marker from 1 min ago (within 4-min window)
        _insert_audit_row(
            store, "merge_backstop_nudge", ts=_ts(1),
            payload={"pr_number": 42, "approved_sha": APPROVED_SHA},
        )
        rest = _mock_rest()
        config = WatchdogConfig(
            merge_approval_backstop_seconds=240,
            merge_approval_backstop_max_renudges=2,  # cap=2, 1 used → within-window
        )
        daemon = _daemon(store, rest=rest, config=config)
        now = datetime.now(UTC)
        result = await daemon._maybe_backstop_renudge(_sub_row(), APPROVED_SHA, now)
        assert result is True  # anchor=1 min ago < 4 min window → own patrol, no send
        rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cap_hit_returns_false(self, store: StateStore) -> None:
        """Nudge count >= cap → returns False (releases patrol to stall path)."""
        _insert_audit_row(
            store, "approval_grant", ts=_ts(20),
            payload={"approved_sha": APPROVED_SHA},
        )
        # 1 nudge already sent (cap=1)
        _insert_audit_row(
            store, "merge_backstop_nudge", ts=_ts(10),
            payload={"pr_number": 42, "approved_sha": APPROVED_SHA},
        )
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)
        now = datetime.now(UTC)
        result = await daemon._maybe_backstop_renudge(_sub_row(), APPROVED_SHA, now)
        assert result is False

    @pytest.mark.asyncio
    async def test_new_sha_rearms_independent_of_old_markers(
        self, store: StateStore,
    ) -> None:
        """Markers for an old SHA don't count against a new SHA's nudge cap."""
        old_sha = "old1234"
        new_sha = "new5678"
        _insert_audit_row(
            store, "approval_grant", ts=_ts(30),
            payload={"approved_sha": old_sha},
        )
        # Nudge for old SHA already consumed
        _insert_audit_row(
            store, "merge_backstop_nudge", ts=_ts(20),
            payload={"pr_number": 42, "approved_sha": old_sha},
        )
        # New grant for new SHA
        _insert_audit_row(
            store, "approval_grant", ts=_ts(10),
            payload={"approved_sha": new_sha},
        )
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)
        now = datetime.now(UTC)
        result = await daemon._maybe_backstop_renudge(_sub_row(approved_sha=new_sha), new_sha, now)
        # window=4min, grant 10min ago → fires
        assert result is True
        rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_store_returns_false(self) -> None:
        """When no state_store is injected the rung returns False (safe degradation)."""
        from codeband.agents.watchdog import WatchdogDaemon

        daemon = WatchdogDaemon(
            config=WatchdogConfig(merge_approval_backstop_max_renudges=1),
            rest_client=_mock_rest(),
            agent_id="agent-wd",
            conductor_id="agent-cond",
            # state_store intentionally absent
        )
        result = await daemon._maybe_backstop_renudge(
            _sub_row(), APPROVED_SHA, datetime.now(UTC),
        )
        assert result is False


# ── _check_one_subtask integration ────────────────────────────────────────────


class TestCheckOneSubtaskIntegration:
    @pytest.mark.asyncio
    async def test_grant_present_no_stall_counter_advance(
        self, store: StateStore, monkeypatch,
    ) -> None:
        """merge_pending + matching HEAD + grant within window → early-return,
        patrol_visits_without_progress NOT incremented."""
        _set_merge_pending(store, approved_sha=APPROVED_SHA)
        _insert_audit_row(
            store, "approval_grant", ts=_ts(1),  # within 4-min window
            payload={"approved_sha": APPROVED_SHA},
        )

        rest = _mock_rest()
        config = WatchdogConfig(
            max_phase_visits=2,
            git_progress_check=True,
            merge_approval_backstop_seconds=240,
            merge_approval_backstop_max_renudges=1,
        )
        daemon = _daemon(store, rest=rest, config=config)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": APPROVED_SHA + "\n"})(),
        )

        now = datetime.now(UTC)
        # Drive multiple patrols — stall counter must stay 0
        for _ in range(4):
            await daemon._check_subtask_progress(now)

        key = (TASK_ID, SUBTASK_ID)
        health = daemon._subtask_state.get(key)
        # stall counter should be 0 (rung owned every patrol via early-return)
        assert health is None or health.patrol_visits_without_progress == 0
        # subtask must NOT have been escalated to blocked
        sub = store.get_subtask(SUBTASK_ID, TASK_ID)
        assert sub.state == "merge_pending"
        # no blocked escalation message sent
        rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_through_after_cap(
        self, store: StateStore, monkeypatch,
    ) -> None:
        """After renudge cap exhausted → falls through to stall counter → blocked."""
        _set_merge_pending(store, approved_sha=APPROVED_SHA)
        _insert_audit_row(
            store, "approval_grant", ts=_ts(20),
            payload={"approved_sha": APPROVED_SHA},
        )
        # Pre-exhaust the cap (1 nudge)
        _insert_audit_row(
            store, "merge_backstop_nudge", ts=_ts(10),
            payload={"pr_number": 42, "approved_sha": APPROVED_SHA},
        )

        rest = _mock_rest()
        config = WatchdogConfig(
            max_phase_visits=2,
            git_progress_check=True,
            merge_approval_backstop_seconds=240,
            merge_approval_backstop_max_renudges=1,
        )
        daemon = _daemon(store, rest=rest, config=config)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": APPROVED_SHA + "\n"})(),
        )

        now = datetime.now(UTC)
        # With cap=1 exhausted the backstop returns False; the stall counter
        # advances. Drive enough patrols to hit the cap (max_phase_visits=2)
        # and trigger a blocked escalation. patrol 1 establishes baseline
        # (counts as progress), patrols 2-3 are stale → cap crossed.
        for _ in range(5):
            await daemon._check_subtask_progress(now)

        sub = store.get_subtask(SUBTASK_ID, TASK_ID)
        assert sub.state == "blocked"


# ── helper ────────────────────────────────────────────────────────────────────


def _sub_row(
    *,
    approved_sha: str = APPROVED_SHA,
    pr_number: int = 42,
) -> MagicMock:
    sub = MagicMock()
    sub.task_id = TASK_ID
    sub.subtask_id = SUBTASK_ID
    sub.state = "merge_pending"
    sub.merge_approved_sha = approved_sha
    sub.pr_number = pr_number
    sub.branch = "feature-backstop"
    return sub
