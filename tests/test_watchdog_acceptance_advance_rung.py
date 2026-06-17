"""Tests for the acceptance_passed → Mergemaster auto-advance rung.

Covers:
- _maybe_acceptance_advance_renudge: no-store path, within-window path,
  window-elapsed-fires path, post-fire marker dedup, cap-hit path,
  re-entry re-arms independent of old markers.
- _check_one_subtask integration: acceptance_passed + window elapsed →
  fires once, writes marker, early-returns (no stall counter advance).
- Renudge cap reached → returns False (releases to stall→blocked path).
- merge_pending (#96) leg unaffected: merge_pending subtasks route through
  the original backstop, not the new rung.
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

TASK_ID = "task-aa-1"
SUBTASK_ID = "st-aa-1"
ROOM_ID = "room-aa-1"
PR_NUMBER = 55


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(TASK_ID, "acceptance advance test", ROOM_ID, owner_id="owner-1")
    s.ensure_subtask(SUBTASK_ID, TASK_ID, state="in_progress")
    return s


def _set_acceptance_passed(store: StateStore, *, minutes_ago: float = 10.0) -> str:
    """Put the subtask in acceptance_passed and return its updated_at string."""
    ts = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE subtask_states SET state = 'acceptance_passed', "
        "updated_at = ?, pr_number = ?, metadata = ? "
        "WHERE subtask_id = ? AND task_id = ?",
        (
            ts,
            PR_NUMBER,
            json.dumps({"branch": "feature-aa"}),
            SUBTASK_ID,
            TASK_ID,
        ),
    )
    conn.commit()
    conn.close()
    return ts


def _insert_audit_row(
    store: StateStore,
    event_type: str,
    *,
    ts: str,
    payload: dict | None = None,
) -> None:
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
            acceptance_advance_backstop_seconds=240,
            acceptance_advance_max_renudges=1,
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


def _sub_row(
    *,
    state: str = "acceptance_passed",
    updated_at: str | None = None,
    pr_number: int = PR_NUMBER,
) -> MagicMock:
    sub = MagicMock()
    sub.task_id = TASK_ID
    sub.subtask_id = SUBTASK_ID
    sub.state = state
    sub.pr_number = pr_number
    sub.updated_at = updated_at or _ts(10)  # 10 min ago by default
    sub.merge_approved_sha = None
    sub.metadata = {"branch": "feature-aa"}
    return sub


# ── _maybe_acceptance_advance_renudge ─────────────────────────────────────────


class TestMaybeAcceptanceAdvanceRenudge:
    @pytest.mark.asyncio
    async def test_no_store_returns_false(self) -> None:
        """When no state_store is injected the rung returns False (safe degradation)."""
        from codeband.agents.watchdog import WatchdogDaemon

        daemon = WatchdogDaemon(
            config=WatchdogConfig(acceptance_advance_max_renudges=1),
            rest_client=_mock_rest(),
            agent_id="agent-wd",
            conductor_id="agent-cond",
        )
        result = await daemon._maybe_acceptance_advance_renudge(
            _sub_row(), datetime.now(UTC),
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_within_window_returns_true_no_send(
        self, store: StateStore,
    ) -> None:
        """Entry < window → return True, no send (rung owns patrol)."""
        sub = _sub_row(updated_at=_ts(1))  # 1 min ago, window=4 min
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)
        result = await daemon._maybe_acceptance_advance_renudge(sub, datetime.now(UTC))
        assert result is True
        rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_window_elapsed_fires_once_writes_marker(
        self, store: StateStore,
    ) -> None:
        """Entry past staleness window → sends renudge, writes marker, returns True."""
        sub = _sub_row(updated_at=_ts(10))  # 10 min ago, window=4 min
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)
        now = datetime.now(UTC)
        result = await daemon._maybe_acceptance_advance_renudge(sub, now)
        assert result is True
        rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()

        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("acceptance_advance_nudge",),
        )
        assert len(rows) == 1
        assert rows[0][2]["pr_number"] == PR_NUMBER

    @pytest.mark.asyncio
    async def test_after_fire_within_inter_nudge_window_owns_patrol(
        self, store: StateStore,
    ) -> None:
        """After first fire, within inter-nudge window (cap=2): owns patrol, no send."""
        entry_ts = _ts(10)
        # One nudge marker already written 1 min ago (within 4-min window)
        _insert_audit_row(
            store, "acceptance_advance_nudge", ts=_ts(1),
            payload={"pr_number": PR_NUMBER},
        )
        config = WatchdogConfig(
            acceptance_advance_backstop_seconds=240,
            acceptance_advance_max_renudges=2,  # cap=2, 1 used → within-window
        )
        sub = _sub_row(updated_at=entry_ts)
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest, config=config)
        result = await daemon._maybe_acceptance_advance_renudge(sub, datetime.now(UTC))
        assert result is True  # anchor=1 min ago < 4 min window → no send
        rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cap_hit_returns_false(self, store: StateStore) -> None:
        """Nudge count >= cap → returns False (releases patrol to stall path)."""
        entry_ts = _ts(20)
        _insert_audit_row(
            store, "acceptance_advance_nudge", ts=_ts(10),
            payload={"pr_number": PR_NUMBER},
        )
        sub = _sub_row(updated_at=entry_ts)
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)  # cap=1
        result = await daemon._maybe_acceptance_advance_renudge(sub, datetime.now(UTC))
        assert result is False

    @pytest.mark.asyncio
    async def test_reentry_rearms_independent_of_old_markers(
        self, store: StateStore,
    ) -> None:
        """Markers from a prior acceptance_passed visit don't count against new cap."""
        old_entry = _ts(30)
        # Old nudge from the prior visit (before the new entry timestamp)
        _insert_audit_row(
            store, "acceptance_advance_nudge", ts=_ts(25),
            payload={"pr_number": PR_NUMBER},
        )
        # Subtask re-entered acceptance_passed 5 min ago (new visit)
        new_entry = _ts(5)
        sub = _sub_row(updated_at=new_entry)
        rest = _mock_rest()
        daemon = _daemon(store, rest=rest)  # cap=1
        now = datetime.now(UTC)
        result = await daemon._maybe_acceptance_advance_renudge(sub, now)
        # window=4min, new entry 5min ago → fires (old marker doesn't count)
        assert result is True
        rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_updated_at_returns_false(self, store: StateStore) -> None:
        """Unparseable updated_at → returns False (safe degradation)."""
        sub = _sub_row(updated_at="not-a-timestamp")
        daemon = _daemon(store)
        result = await daemon._maybe_acceptance_advance_renudge(sub, datetime.now(UTC))
        assert result is False


# ── _check_one_subtask integration ────────────────────────────────────────────


class TestCheckOneSubtaskAcceptanceAdvance:
    @pytest.mark.asyncio
    async def test_acceptance_passed_stalled_fires_nudge_writes_marker(
        self, store: StateStore, monkeypatch,
    ) -> None:
        """acceptance_passed + window elapsed → fires once, writes marker.

        The rung pre-empts the first stall patrol by returning True.  After the
        cap (1 nudge) is consumed the stall path resumes; with max_phase_visits=2
        the subtask eventually escalates to blocked.  The key invariants:
        - nudge sent exactly once (not multiple times);
        - durable audit marker written;
        - stall path resumes correctly after cap.
        """
        _set_acceptance_passed(store, minutes_ago=10)

        rest = _mock_rest()
        config = WatchdogConfig(
            max_phase_visits=5,
            git_progress_check=True,
            acceptance_advance_backstop_seconds=240,
            acceptance_advance_max_renudges=1,
        )
        daemon = _daemon(store, rest=rest, config=config)
        # git HEAD for branch "feature-aa" — unchanged across patrols
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "abc1234\n"})(),
        )

        now = datetime.now(UTC)
        # 3 patrols: patrol-1 fires nudge; patrol-2 cap-hit + git baseline seen
        # (progressed=True, visits=0); patrol-3 visits=1.
        # With max_phase_visits=5, no blocked escalation fires yet — isolates the
        # acceptance-advance nudge as the only message sent.
        for _ in range(3):
            await daemon._check_subtask_progress(now)

        # Exactly one renudge message sent (acceptance-advance only, not blocked)
        rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()

        # Durable marker written exactly once
        rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("acceptance_advance_nudge",),
        )
        assert len(rows) == 1
        assert rows[0][2]["pr_number"] == PR_NUMBER

        # Subtask stays in acceptance_passed (rung pre-empted first stall patrol;
        # stall path resumes after cap but max_phase_visits=5 not yet reached)
        sub = store.get_subtask(SUBTASK_ID, TASK_ID)
        assert sub.state == "acceptance_passed"

    @pytest.mark.asyncio
    async def test_within_window_no_fire_no_stall(
        self, store: StateStore, monkeypatch,
    ) -> None:
        """acceptance_passed within window → rung owns patrol, no send."""
        _set_acceptance_passed(store, minutes_ago=1)  # 1 min ago, window=4 min

        rest = _mock_rest()
        config = WatchdogConfig(
            max_phase_visits=5,
            git_progress_check=True,
            acceptance_advance_backstop_seconds=240,
            acceptance_advance_max_renudges=1,
        )
        daemon = _daemon(store, rest=rest, config=config)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "abc1234\n"})(),
        )

        now = datetime.now(UTC)
        for _ in range(3):
            await daemon._check_subtask_progress(now)

        rest.agent_api_messages.create_agent_chat_message.assert_not_awaited()
        sub = store.get_subtask(SUBTASK_ID, TASK_ID)
        assert sub.state == "acceptance_passed"

    @pytest.mark.asyncio
    async def test_cap_hit_falls_through_to_blocked(
        self, store: StateStore, monkeypatch,
    ) -> None:
        """After cap exhausted → stall counter advances → subtask blocked."""
        entry_ts = _set_acceptance_passed(store, minutes_ago=20)
        # Pre-exhaust the cap (1 nudge already sent)
        _insert_audit_row(
            store, "acceptance_advance_nudge", ts=_ts(10),
            payload={"pr_number": PR_NUMBER},
        )

        rest = _mock_rest()
        config = WatchdogConfig(
            max_phase_visits=2,
            git_progress_check=True,
            acceptance_advance_backstop_seconds=240,
            acceptance_advance_max_renudges=1,
        )
        daemon = _daemon(store, rest=rest, config=config)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "abc1234\n"})(),
        )

        now = datetime.now(UTC)
        for _ in range(5):
            await daemon._check_subtask_progress(now)

        sub = store.get_subtask(SUBTASK_ID, TASK_ID)
        assert sub.state == "blocked"

    @pytest.mark.asyncio
    async def test_merge_pending_rung_unaffected(
        self, store: StateStore, monkeypatch,
    ) -> None:
        """merge_pending + matching HEAD + grant present still routes through
        the original #96 backstop, not the new acceptance-advance rung."""
        approved_sha = "abc1234"
        conn = sqlite3.connect(store.db_path)
        conn.execute(
            "UPDATE subtask_states SET state = 'merge_pending', "
            "merge_approved_sha = ?, pr_number = ?, metadata = ? "
            "WHERE subtask_id = ? AND task_id = ?",
            (
                approved_sha,
                PR_NUMBER,
                json.dumps({"branch": "feature-aa"}),
                SUBTASK_ID,
                TASK_ID,
            ),
        )
        conn.commit()
        conn.close()

        _insert_audit_row(
            store, "approval_grant", ts=_ts(10),
            payload={"approved_sha": approved_sha},
        )

        rest = _mock_rest()
        config = WatchdogConfig(
            max_phase_visits=5,
            git_progress_check=True,
            merge_approval_backstop_seconds=240,
            merge_approval_backstop_max_renudges=1,
            acceptance_advance_backstop_seconds=240,
            acceptance_advance_max_renudges=1,
        )
        daemon = _daemon(store, rest=rest, config=config)
        monkeypatch.setattr(
            "subprocess.run",
            lambda cmd, **kw: type("R", (), {
                "returncode": 0, "stdout": approved_sha + "\n",
            })(),
        )

        now = datetime.now(UTC)
        for _ in range(4):
            await daemon._check_subtask_progress(now)

        # The #96 backstop owned the patrol — subtask stays merge_pending
        sub = store.get_subtask(SUBTASK_ID, TASK_ID)
        assert sub.state == "merge_pending"
        # acceptance_advance_nudge must NOT have been written
        aa_rows = store.latest_audit_events(
            task_id=TASK_ID, subtask_id=SUBTASK_ID,
            event_types=("acceptance_advance_nudge",),
        )
        assert aa_rows == []
