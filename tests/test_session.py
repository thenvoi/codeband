"""Tests for session recovery: identity persistence, context rebuilding, supervisor."""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeband.session.identity import WorkerIdentity


class TestWorkerIdentity:
    """Tests for WorkerIdentity persistence."""

    def test_save_load_roundtrip(self, tmp_path: Path):
        """Identity survives save/load cycle."""
        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path="/workspace/worktrees/player-0",
            session_count=3,
            last_session_started_at=datetime(2026, 3, 27, 12, 0, 0, tzinfo=UTC),
            last_session_error="context limit exceeded",
        )
        identity.save(tmp_path)

        loaded = WorkerIdentity.load(tmp_path, "coder-claude_sdk-0")
        assert loaded.worker_id == "coder-claude_sdk-0"
        assert loaded.agent_id == "agent-123"
        assert loaded.session_count == 3
        assert loaded.last_session_error == "context limit exceeded"

    def test_load_returns_none_when_missing(self, tmp_path: Path):
        """Loading a non-existent identity returns None."""
        assert WorkerIdentity.load(tmp_path, "coder-claude_sdk-99") is None

    def test_save_is_atomic(self, tmp_path: Path):
        """Save writes to temp file then renames (no partial writes)."""
        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path="/workspace/worktrees/player-0",
        )
        identity.save(tmp_path)

        # The final file should exist and be valid JSON
        path = tmp_path / "coder-claude_sdk-0.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["worker_id"] == "coder-claude_sdk-0"

    def test_save_overwrites_existing(self, tmp_path: Path):
        """Saving again overwrites the previous identity."""
        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path="/workspace/worktrees/player-0",
            session_count=1,
        )
        identity.save(tmp_path)

        identity.session_count = 5
        identity.save(tmp_path)

        loaded = WorkerIdentity.load(tmp_path, "coder-claude_sdk-0")
        assert loaded.session_count == 5


class TestBuildRecoveryContext:
    """Tests for recovery context building."""

    @pytest.fixture
    def worktree_with_history(self, tmp_path: Path) -> Path:
        """Create a git worktree with some commit history."""
        repo = tmp_path / "worktree"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (repo / "README.md").write_text("# Test")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Initial commit"],
            check=True, capture_output=True,
        )
        (repo / "feature.py").write_text("def hello(): pass")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "Add feature"],
            check=True, capture_output=True,
        )
        return repo

    def test_includes_git_log(self, worktree_with_history: Path):
        """Recovery context includes recent git history."""
        from codeband.session.context import build_recovery_context

        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path=str(worktree_with_history),
            session_count=1,
        )
        context = build_recovery_context("coder-claude_sdk-0", worktree_with_history, identity)
        assert "Add feature" in context
        assert "Initial commit" in context

    def test_includes_uncommitted_changes(self, worktree_with_history: Path):
        """Recovery context shows uncommitted work."""
        from codeband.session.context import build_recovery_context

        # Create uncommitted changes
        (worktree_with_history / "new_file.py").write_text("print('hello')")

        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path=str(worktree_with_history),
            session_count=1,
        )
        context = build_recovery_context("coder-claude_sdk-0", worktree_with_history, identity)
        assert "new_file.py" in context

    def test_includes_task_from_file(self, worktree_with_history: Path):
        """Recovery context reads TASK.md if present."""
        from codeband.session.context import build_recovery_context

        (worktree_with_history / "TASK.md").write_text("Implement auth middleware")

        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path=str(worktree_with_history),
            session_count=2,
        )
        context = build_recovery_context("coder-claude_sdk-0", worktree_with_history, identity)
        assert "Implement auth middleware" in context

    def test_context_without_task_file(self, worktree_with_history: Path):
        """Recovery context works when no TASK.md exists."""
        from codeband.session.context import build_recovery_context

        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path=str(worktree_with_history),
            session_count=1,
        )
        context = build_recovery_context("coder-claude_sdk-0", worktree_with_history, identity)
        assert "Session Recovery" in context
        # Should still have git log even without task file
        assert "Add feature" in context

    def test_includes_session_metadata(self, worktree_with_history: Path):
        """Recovery context mentions session count."""
        from codeband.session.context import build_recovery_context

        identity = WorkerIdentity(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            worktree_path=str(worktree_with_history),
            session_count=3,
        )
        context = build_recovery_context("coder-claude_sdk-0", worktree_with_history, identity)
        assert "session" in context.lower()
        assert "coder-claude_sdk-0" in context

    def test_recovery_context_latest_assignment_overrides_stale_state(
        self, worktree_with_history: Path,
    ):
        """Latest chat assignment must beat stale persisted branch state."""
        from codeband.session.context import build_recovery_context

        identity = WorkerIdentity(
            worker_id="coder-codex-0",
            agent_id="agent-123",
            worktree_path=str(worktree_with_history),
            session_count=1,
        )

        context = build_recovery_context("coder-codex-0", worktree_with_history, identity)

        assert "newer Conductor assignment names a different branch" in context
        assert "latest Conductor assignment" in context
        assert "reset/clean the worktree from the requested repo base branch" in context

    def test_returns_none_when_worktree_missing(self, tmp_path: Path):
        """If the worktree directory is gone (e.g. recreate failed mid-flight),
        return None instead of letting subprocess raise FileNotFoundError.

        Regression: previously the supervisor caught the FileNotFoundError but
        logged a 30-line traceback every restart cycle, flooding stdout.
        """
        from codeband.session.context import build_recovery_context

        missing = tmp_path / "does-not-exist"
        identity = WorkerIdentity(
            worker_id="coder-codex-0",
            agent_id="agent-123",
            worktree_path=str(missing),
            session_count=2,
        )
        assert build_recovery_context("coder-codex-0", missing, identity) is None


class TestWorkerSupervisor:
    """Tests for the coder restart supervisor.

    Contract: the supervisor runs forever until cancelled. Both crashes and
    clean exits from ``agent.run()`` trigger a restart; only a
    ``CancelledError`` (driven by the runner's shutdown path) ends the loop.
    """

    @staticmethod
    def _build_supervisor(mock_run, *, activity=None):
        from codeband.session.supervisor import WorkerSupervisor

        return WorkerSupervisor(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            create_agent_fn=AsyncMock(
                return_value=MagicMock(run=mock_run, close=AsyncMock()),
            ),
            state_dir=Path("/tmp/test-state"),
            worktree_path=Path("/tmp/test-worktree"),
            restart_delay_seconds=0.0,  # no delay in tests
            activity=activity,
        )

    @staticmethod
    async def _run_until(supervisor, *, until_calls: asyncio.Event):
        """Launch supervisor.run() and cancel it once ``until_calls`` is set."""
        with patch("codeband.session.supervisor.build_recovery_context", return_value="ctx"):
            with patch.object(WorkerIdentity, "save"):
                with patch.object(WorkerIdentity, "load", return_value=None):
                    task = asyncio.create_task(supervisor.run())
                    try:
                        await asyncio.wait_for(until_calls.wait(), timeout=2.0)
                    finally:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

    @pytest.mark.asyncio
    async def test_restarts_on_crash(self):
        """Supervisor restarts coder after a crash."""
        call_count = 0
        target_reached = asyncio.Event()

        async def mock_run():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                target_reached.set()
                # Block here until cancelled so we don't race past the target.
                await asyncio.sleep(60)
            raise RuntimeError("Session crashed")

        supervisor = self._build_supervisor(mock_run)
        await self._run_until(supervisor, until_calls=target_reached)

        assert call_count == 3  # two crashes followed by the blocking third call

    @pytest.mark.asyncio
    async def test_clean_exit_triggers_restart(self):
        """A clean return from ``agent.run()`` must restart, not terminate.

        Regression: before the reconnect-forever rewrite the supervisor's
        clean-exit branch was ``return``, which completed the supervisor task
        and triggered a full-swarm shutdown in the runner.
        """
        call_count = 0
        target_reached = asyncio.Event()
        activity = MagicMock()
        activity.log = MagicMock()

        async def mock_run():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                target_reached.set()
                await asyncio.sleep(60)
            # Clean exit on first two calls.

        supervisor = self._build_supervisor(mock_run, activity=activity)
        await self._run_until(supervisor, until_calls=target_reached)

        assert call_count == 3, (
            "supervisor must restart after a clean exit, not terminate after the first call"
        )
        # SESSION_RESTART must fire for the cycles that ended in clean exit.
        event_types = [call.args[0] for call in activity.log.call_args_list]
        assert event_types.count("SESSION_RESTART") >= 2, (
            f"expected ≥2 SESSION_RESTART events (one per clean exit), got {event_types}"
        )

    @pytest.mark.asyncio
    async def test_clean_exit_stops_agent_before_restart(self):
        """Each restarted coder session must stop its SDK Agent.

        Regression: the supervisor used to look only for ``close()``, but
        Band SDK Agents expose ``stop()``. That left websocket reconnect tasks
        alive across coder restarts in local mode.
        """
        from codeband.session.supervisor import WorkerSupervisor

        call_count = 0
        target_reached = asyncio.Event()
        produced_agents = []

        async def mock_run():
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                target_reached.set()
                await asyncio.sleep(60)
            # Clean exit on the first two calls.

        async def create_agent(*, recovery_context=None):
            agent = MagicMock()
            agent.run = mock_run
            agent.stop = AsyncMock(return_value=True)
            produced_agents.append(agent)
            return agent

        supervisor = WorkerSupervisor(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-123",
            create_agent_fn=create_agent,
            state_dir=Path("/tmp/test-state"),
            worktree_path=Path("/tmp/test-worktree"),
            restart_delay_seconds=0.0,
        )
        await self._run_until(supervisor, until_calls=target_reached)

        assert len(produced_agents) == 3
        stopped = [a for a in produced_agents if a.stop.await_count >= 1]
        assert len(stopped) >= 2, (
            f"expected ≥2 completed coder sessions to call stop(), got {len(stopped)}"
        )

    @pytest.mark.asyncio
    async def test_close_agent_prefers_sdk_stop_over_legacy_close(self):
        """The supervisor should use Band SDK ``stop()``, not stale ``close()``."""
        supervisor = self._build_supervisor(AsyncMock())
        agent = MagicMock()
        agent.stop = AsyncMock(return_value=True)
        agent.close = AsyncMock()

        await supervisor._close_agent(agent)

        agent.stop.assert_awaited_once_with(timeout=2.0)
        agent.close.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_infinite_restart_until_cancelled(self):
        """Supervisor keeps restarting until the task is cancelled.

        Replaces the old ``test_respects_max_restarts``. The contract is now
        "only a cancellation stops the supervisor" — there is no restart
        ceiling.
        """
        call_count = 0
        target_reached = asyncio.Event()

        async def mock_run():
            nonlocal call_count
            call_count += 1
            if call_count >= 10:
                target_reached.set()
                await asyncio.sleep(60)
            raise RuntimeError("Session crashed")

        supervisor = self._build_supervisor(mock_run)
        await self._run_until(supervisor, until_calls=target_reached)

        assert call_count == 10, (
            "supervisor stopped before reaching 10 restarts — "
            "max_restarts ceiling must not exist anymore"
        )

    def test_compute_backoff_scales_and_caps(self):
        """Exponential backoff doubles per consecutive identical failure
        and caps at 60s. With base_delay=0 (test mode) the backoff stays 0
        so existing tests don't slow down.

        Regression: the supervisor used to restart every ``restart_delay``
        seconds regardless of how many times the same crash occurred,
        flooding stdout with hundreds of identical tracebacks during a
        deterministic failure (missing worktree, missing CLI binary).
        """
        from codeband.session.supervisor import WorkerSupervisor

        sup_zero = self._build_supervisor(AsyncMock())  # base 0
        assert sup_zero._compute_backoff(0) == 0.0
        assert sup_zero._compute_backoff(5) == 0.0

        sup = WorkerSupervisor(
            worker_id="x", agent_id="y",
            create_agent_fn=AsyncMock(),
            state_dir=Path("/tmp"), worktree_path=Path("/tmp"),
            restart_delay_seconds=5.0,
        )
        assert sup._compute_backoff(0) == 5.0
        assert sup._compute_backoff(1) == 10.0
        assert sup._compute_backoff(2) == 20.0
        assert sup._compute_backoff(3) == 40.0
        assert sup._compute_backoff(4) == 60.0  # capped
        assert sup._compute_backoff(20) == 60.0  # still capped

    @pytest.mark.asyncio
    async def test_recovery_context_traceback_logged_once_then_suppressed(
        self, caplog,
    ):
        """Repeat ``build_recovery_context`` failures with the same signature
        should log the full traceback only on first occurrence; subsequent
        repeats log a single suppressed-repeat line.

        Regression: the user's log showed the same FileNotFoundError
        traceback dumped >100 times in a row, ~30 lines each.
        """
        import logging
        from codeband.session.supervisor import WorkerSupervisor

        call_count = 0
        target_reached = asyncio.Event()

        async def mock_run():
            nonlocal call_count
            call_count += 1
            if call_count >= 5:
                target_reached.set()
                await asyncio.sleep(60)

        def raising_recovery(*args, **kwargs):
            raise FileNotFoundError("worktree gone")

        supervisor = WorkerSupervisor(
            worker_id="coder-codex-0", agent_id="agent-x",
            create_agent_fn=AsyncMock(
                return_value=MagicMock(run=mock_run, close=AsyncMock()),
            ),
            state_dir=Path("/tmp/test-state"),
            worktree_path=Path("/tmp/test-worktree"),
            restart_delay_seconds=0.0,
        )

        caplog.set_level(logging.WARNING, logger="codeband.session.supervisor")
        with patch("codeband.session.supervisor.build_recovery_context",
                   side_effect=raising_recovery):
            with patch.object(WorkerIdentity, "save"):
                with patch.object(WorkerIdentity, "load", return_value=None):
                    task = asyncio.create_task(supervisor.run())
                    try:
                        await asyncio.wait_for(target_reached.wait(), timeout=2.0)
                    finally:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

        # session 1 has no recovery context (it's the first cycle), so
        # build_recovery_context is invoked starting at cycle 2. Of those,
        # exactly one log entry should carry exc_info (the first occurrence).
        recovery_records = [
            r for r in caplog.records
            if "recovery context" in r.getMessage().lower()
        ]
        with_traceback = [r for r in recovery_records if r.exc_info]
        assert len(with_traceback) == 1, (
            f"expected exactly one traceback-bearing log; got "
            f"{len(with_traceback)} of {len(recovery_records)} total"
        )
        assert len(recovery_records) >= 2, (
            "expected at least 2 cycles to fail recovery context"
        )
