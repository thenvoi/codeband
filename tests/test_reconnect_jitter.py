"""Tests for full-jitter reconnect backoff in runner and supervisor."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeband.session.supervisor import WorkerSupervisor


# ── shared fakes ──────────────────────────────────────────────────────────────

class _FakeAgent:
    """Minimal agent fake: run() returns cleanly, stop() is a no-op."""

    async def run(self) -> None:
        return None

    async def stop(self, timeout: float = 2.0) -> None:
        pass


class _Activity:
    def log(self, *args, **kwargs) -> None:
        pass


# ── runner ────────────────────────────────────────────────────────────────────

class TestRunnerJitter:
    """_run_agent_forever sleeps a jittered fraction of the computed delay."""

    @pytest.mark.asyncio
    async def test_sleep_within_bounds(self):
        """Sleep duration is random.random() * capped_delay, not always the full delay."""
        from codeband.orchestration import runner as runner_mod

        sleep_durations: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            raise asyncio.CancelledError

        make_agent = MagicMock(return_value=_FakeAgent())

        with (
            patch.object(runner_mod, "_RECONNECT_BASE_DELAY_SECONDS", 4.0),
            patch.object(runner_mod, "_RECONNECT_MAX_DELAY_SECONDS", 60.0),
            patch("codeband.orchestration.runner.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=0.3),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner_mod._run_agent_forever(
                    make_agent=make_agent,
                    name="test-agent",
                    activity=_Activity(),
                    agent_key=None,
                    workspace_path=None,
                )

        assert len(sleep_durations) == 1
        # attempt=1 → delay = min(4.0 * 2**0, 60) = 4.0; jitter = 0.3 * 4.0 = 1.2
        assert sleep_durations[0] == pytest.approx(1.2)

    @pytest.mark.asyncio
    async def test_sleep_not_always_full_delay(self):
        """Jitter is actually applied — sleep < computed delay when random < 1.0."""
        from codeband.orchestration import runner as runner_mod

        sleep_durations: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            raise asyncio.CancelledError

        make_agent = MagicMock(return_value=_FakeAgent())

        with (
            patch.object(runner_mod, "_RECONNECT_BASE_DELAY_SECONDS", 4.0),
            patch.object(runner_mod, "_RECONNECT_MAX_DELAY_SECONDS", 60.0),
            patch("codeband.orchestration.runner.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=0.5),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner_mod._run_agent_forever(
                    make_agent=make_agent,
                    name="test-agent",
                    activity=_Activity(),
                    agent_key=None,
                    workspace_path=None,
                )

        # 0.5 * 4.0 = 2.0, less than the full 4.0
        assert sleep_durations[0] == pytest.approx(2.0)
        assert sleep_durations[0] < 4.0

    @pytest.mark.asyncio
    async def test_zero_base_delay_unaffected(self):
        """With base delay=0, full jitter of 0 is still 0 (test-mode contract)."""
        from codeband.orchestration import runner as runner_mod

        sleep_durations: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            raise asyncio.CancelledError

        make_agent = MagicMock(return_value=_FakeAgent())

        with (
            patch.object(runner_mod, "_RECONNECT_BASE_DELAY_SECONDS", 0.0),
            patch.object(runner_mod, "_RECONNECT_MAX_DELAY_SECONDS", 60.0),
            patch("codeband.orchestration.runner.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=0.9),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner_mod._run_agent_forever(
                    make_agent=make_agent,
                    name="test-agent",
                    activity=_Activity(),
                    agent_key=None,
                    workspace_path=None,
                )

        assert sleep_durations[0] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_cap_respected(self):
        """Jitter upper bound is the 60s cap — no sleep exceeds it."""
        from codeband.orchestration import runner as runner_mod

        sleep_durations: list[float] = []
        call_count = [0]

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            call_count[0] += 1
            if call_count[0] >= 6:
                raise asyncio.CancelledError

        make_agent = MagicMock(side_effect=lambda _rc: _FakeAgent())

        with (
            patch.object(runner_mod, "_RECONNECT_BASE_DELAY_SECONDS", 4.0),
            patch.object(runner_mod, "_RECONNECT_MAX_DELAY_SECONDS", 60.0),
            patch("codeband.orchestration.runner.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=1.0),
        ):
            with pytest.raises(asyncio.CancelledError):
                await runner_mod._run_agent_forever(
                    make_agent=make_agent,
                    name="test-agent",
                    activity=_Activity(),
                    agent_key=None,
                    workspace_path=None,
                )

        # With random=1.0 the sleep equals the capped delay exactly; none exceed 60s
        assert len(sleep_durations) == 6
        for d in sleep_durations:
            assert d <= 60.0


# ── supervisor ────────────────────────────────────────────────────────────────

class TestSupervisorJitter:
    """WorkerSupervisor sleeps a jittered fraction of _compute_backoff output."""

    def _make_supervisor(self, tmp_path: Path, restart_delay: float = 5.0) -> WorkerSupervisor:
        return WorkerSupervisor(
            worker_id="coder-claude_sdk-0",
            agent_id="agent-42",
            create_agent_fn=AsyncMock(return_value=_FakeAgent()),
            state_dir=tmp_path,
            worktree_path=tmp_path,
            restart_delay_seconds=restart_delay,
            activity=None,
        )

    def test_compute_backoff_unchanged(self, tmp_path: Path):
        """_compute_backoff still returns the deterministic capped value."""
        sup = self._make_supervisor(tmp_path, restart_delay=5.0)
        assert sup._compute_backoff(0) == pytest.approx(5.0)
        assert sup._compute_backoff(1) == pytest.approx(10.0)
        assert sup._compute_backoff(6) == pytest.approx(60.0)
        assert sup._compute_backoff(10) == pytest.approx(60.0)

    def test_compute_backoff_zero_delay(self, tmp_path: Path):
        """Zero restart_delay keeps backoff at 0 regardless of consecutive_same."""
        sup = self._make_supervisor(tmp_path, restart_delay=0.0)
        assert sup._compute_backoff(5) == 0.0

    @pytest.mark.asyncio
    async def test_sleep_within_bounds(self, tmp_path: Path):
        """Sleep duration is random.random() * compute_backoff, not the full delay."""
        sup = self._make_supervisor(tmp_path, restart_delay=5.0)
        sup._create_agent_fn = AsyncMock(return_value=_FakeAgent())

        sleep_durations: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            raise asyncio.CancelledError

        with (
            patch("codeband.session.supervisor.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=0.4),
        ):
            with pytest.raises(asyncio.CancelledError):
                await sup.run()

        assert len(sleep_durations) == 1
        # consecutive_same=0 → backoff = 5.0; jitter = 0.4 * 5.0 = 2.0
        assert sleep_durations[0] == pytest.approx(2.0)

    @pytest.mark.asyncio
    async def test_sleep_not_always_full_delay(self, tmp_path: Path):
        """Jitter reduces sleep below the computed backoff."""
        sup = self._make_supervisor(tmp_path, restart_delay=5.0)
        sup._create_agent_fn = AsyncMock(return_value=_FakeAgent())

        sleep_durations: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            raise asyncio.CancelledError

        with (
            patch("codeband.session.supervisor.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=0.6),
        ):
            with pytest.raises(asyncio.CancelledError):
                await sup.run()

        assert sleep_durations[0] == pytest.approx(3.0)  # 0.6 * 5.0
        assert sleep_durations[0] < 5.0

    @pytest.mark.asyncio
    async def test_zero_restart_delay_unaffected(self, tmp_path: Path):
        """Zero restart_delay → sleep=0 even with jitter (test-mode contract)."""
        sup = self._make_supervisor(tmp_path, restart_delay=0.0)
        sup._create_agent_fn = AsyncMock(return_value=_FakeAgent())

        sleep_durations: list[float] = []

        async def fake_sleep(d: float) -> None:
            sleep_durations.append(d)
            raise asyncio.CancelledError

        with (
            patch("codeband.session.supervisor.asyncio.sleep", side_effect=fake_sleep),
            patch("random.random", return_value=0.99),
        ):
            with pytest.raises(asyncio.CancelledError):
                await sup.run()

        assert sleep_durations[0] == pytest.approx(0.0)
