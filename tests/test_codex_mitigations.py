"""Tests for the finding-22 mitigations (Batch 4 addendum, PR 4).

Three layers: the config knobs (turn budget + retry budget), their plumbing
(role-constructor seam / SessionConfig at Agent.create), and the Codex
adapter-seam resilience patch (dead-client reset + visible-error wrap).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import (
    AgentsConfig,
    BandConfig,
    CodebandConfig,
    Framework,
    RepoConfig,
    WorkspaceConfig,
)


def _make_config(tmp_path: Path) -> CodebandConfig:
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        band=BandConfig(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Config knobs (4a / 4b)
# ─────────────────────────────────────────────────────────────────────────────


class TestCodexTurnTimeoutKnob:
    def test_default_is_one_hour(self):
        assert AgentsConfig().codex_turn_timeout_seconds == 3600

    def test_floor_is_60(self):
        with pytest.raises(ValueError) as excinfo:
            AgentsConfig(codex_turn_timeout_seconds=59)
        assert "codex_turn_timeout_seconds" in str(excinfo.value)
        assert AgentsConfig(
            codex_turn_timeout_seconds=60,
        ).codex_turn_timeout_seconds == 60


class TestMaxMessageRetriesKnob:
    def test_default_is_3(self):
        assert AgentsConfig().max_message_retries == 3

    def test_floor_is_1(self):
        with pytest.raises(ValueError) as excinfo:
            AgentsConfig(max_message_retries=0)
        assert "max_message_retries" in str(excinfo.value)
        assert AgentsConfig(max_message_retries=1).max_message_retries == 1


# ─────────────────────────────────────────────────────────────────────────────
# Plumbing (4a: role-constructor seam; 4b: SessionConfig)
# ─────────────────────────────────────────────────────────────────────────────


def test_create_band_agent_passes_max_message_retries(tmp_path, monkeypatch):
    import thenvoi

    from codeband.config import AgentCredentials
    from codeband.orchestration.runner import _create_band_agent

    config = _make_config(tmp_path)
    config.agents.max_message_retries = 5
    captured = {}

    def fake_create(cls, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(thenvoi.Agent, "create", classmethod(fake_create))
    _create_band_agent(
        adapter=MagicMock(),
        creds=AgentCredentials(agent_id="agent-1", api_key="key-1"),
        config=config,
    )

    assert captured["session_config"].max_message_retries == 5


def test_codex_runner_passes_turn_timeout_to_adapter_config(tmp_path):
    from codeband.agents.player_codex import CodexPlayerRunner

    runner = CodexPlayerRunner(workspace=str(tmp_path), turn_timeout_seconds=1234)
    assert runner.adapter.config.turn_timeout_s == 1234.0


def test_codex_runner_default_matches_config_default(tmp_path):
    from codeband.agents.player_codex import CodexPlayerRunner

    runner = CodexPlayerRunner(workspace=str(tmp_path))
    assert runner.adapter.config.turn_timeout_s == 3600.0


def test_every_codex_role_factory_wires_the_knob(tmp_path):
    """All six Codex roles get the configured turn budget — the 180s SDK
    default was the primary desync trigger for every long-running role."""
    from codeband.orchestration.runner import (
        _create_code_reviewer,
        _create_coder,
        _create_conductor,
        _create_mergemaster,
        _create_plan_reviewer,
        _create_planner,
    )

    config = _make_config(tmp_path)
    config.agents.codex_turn_timeout_seconds = 777
    config.agents.conductor.framework = Framework.CODEX
    config.agents.mergemaster.framework = Framework.CODEX
    ws = str(tmp_path)

    adapters = [
        _create_coder(Framework.CODEX, config, ws),
        _create_planner(config, ws, framework=Framework.CODEX),
        _create_plan_reviewer(config, ws, framework=Framework.CODEX),
        _create_code_reviewer(config, ws, framework=Framework.CODEX),
        _create_conductor(config),
        _create_mergemaster(config, ws),
    ]
    assert [a.config.turn_timeout_s for a in adapters] == [777.0] * 6


# ─────────────────────────────────────────────────────────────────────────────
# Adapter-seam resilience patch (4c)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeQueue:
    """Mimics asyncio.Queue's internal ``_queue`` deque shape."""

    def __init__(self, events):
        self._queue = list(events)


def _fake_adapter(*, returncode=None, queued_events=(), client=True):
    adapter = SimpleNamespace(
        _initialized=True,
        _room_threads={"room-1": "thread-1"},
    )
    if client:
        adapter._client = SimpleNamespace(
            _proc=SimpleNamespace(returncode=returncode),
            _events=_FakeQueue(queued_events),
            close=MagicMock(),
        )
    else:
        adapter._client = None
    return adapter


class TestResetDeadCodexClient:
    def test_dead_subprocess_resets_adapter_state(self):
        from codeband.orchestration.runner import _reset_dead_codex_client

        adapter = _fake_adapter(returncode=1)
        _reset_dead_codex_client(adapter)

        assert adapter._client is None
        assert adapter._initialized is False
        assert adapter._room_threads == {}

    def test_queued_transport_closed_sentinel_counts_as_dead(self):
        from codeband.orchestration.runner import _reset_dead_codex_client

        sentinel = SimpleNamespace(method="transport/closed")
        adapter = _fake_adapter(returncode=None, queued_events=[sentinel])
        _reset_dead_codex_client(adapter)

        assert adapter._client is None
        assert adapter._initialized is False

    def test_live_client_is_untouched(self):
        from codeband.orchestration.runner import _reset_dead_codex_client

        other = SimpleNamespace(method="item/completed")
        adapter = _fake_adapter(returncode=None, queued_events=[other])
        client = adapter._client
        _reset_dead_codex_client(adapter)

        assert adapter._client is client
        assert adapter._initialized is True
        assert adapter._room_threads == {"room-1": "thread-1"}

    def test_no_client_is_a_noop(self):
        from codeband.orchestration.runner import _reset_dead_codex_client

        adapter = _fake_adapter(client=False)
        _reset_dead_codex_client(adapter)  # must not raise
        assert adapter._client is None

    def test_unknown_shapes_do_nothing(self):
        from codeband.orchestration.runner import _reset_dead_codex_client

        adapter = SimpleNamespace(_client=object())  # no _proc, no _events
        _reset_dead_codex_client(adapter)  # shape-tolerant: no raise


class TestVisibleErrorWrap:
    @pytest.mark.asyncio
    async def test_pre_output_exception_posts_chat_error_and_reraises(self):
        from codeband.orchestration.runner import _wrap_codex_on_message

        async def boom(self, msg, tools, *args, **kwargs):
            raise RuntimeError("turn/start rejected")

        wrapped = _wrap_codex_on_message(boom)
        adapter = _fake_adapter(returncode=None)
        tools = SimpleNamespace(send_message=AsyncMock())

        with pytest.raises(RuntimeError, match="turn/start rejected"):
            await wrapped(adapter, MagicMock(), tools, is_session_bootstrap=False,
                          room_id="room-1")

        tools.send_message.assert_awaited_once()
        content = tools.send_message.call_args.kwargs["content"]
        assert "Codex turn could not start" in content
        assert "turn/start rejected" in content
        assert "NOT processed" in content

    @pytest.mark.asyncio
    async def test_success_passes_through_without_noise(self):
        from codeband.orchestration.runner import _wrap_codex_on_message

        async def fine(self, msg, tools, *args, **kwargs):
            return "ok"

        wrapped = _wrap_codex_on_message(fine)
        tools = SimpleNamespace(send_message=AsyncMock())

        result = await wrapped(
            _fake_adapter(), MagicMock(), tools,
            is_session_bootstrap=False, room_id="room-1",
        )
        assert result == "ok"
        tools.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dead_client_is_reset_before_the_turn(self):
        from codeband.orchestration.runner import _wrap_codex_on_message

        seen = {}

        async def record(self, msg, tools, *args, **kwargs):
            seen["client_at_turn"] = self._client

        wrapped = _wrap_codex_on_message(record)
        adapter = _fake_adapter(returncode=137)  # dead between turns
        await wrapped(adapter, MagicMock(), SimpleNamespace(),
                      is_session_bootstrap=False, room_id="room-1")

        assert seen["client_at_turn"] is None  # reset BEFORE the turn ran

    @pytest.mark.asyncio
    async def test_send_failure_never_masks_the_original_error(self):
        from codeband.orchestration.runner import _wrap_codex_on_message

        async def boom(self, msg, tools, *args, **kwargs):
            raise RuntimeError("original")

        wrapped = _wrap_codex_on_message(boom)
        tools = SimpleNamespace(
            send_message=AsyncMock(side_effect=ConnectionError("chat down")),
        )

        with pytest.raises(RuntimeError, match="original"):
            await wrapped(_fake_adapter(), MagicMock(), tools,
                          is_session_bootstrap=False, room_id="room-1")


def test_patch_is_idempotent_and_targets_the_real_adapter():
    from thenvoi.adapters import CodexAdapter

    from codeband.orchestration.runner import _patch_codex_adapter_resilience

    original = CodexAdapter.on_message
    try:
        _patch_codex_adapter_resilience()
        wrapped_once = CodexAdapter.on_message
        assert wrapped_once is not original
        assert getattr(wrapped_once, "_codeband_codex_resilience", False)

        _patch_codex_adapter_resilience()  # second call must not re-wrap
        assert CodexAdapter.on_message is wrapped_once
    finally:
        CodexAdapter.on_message = original


def test_wrapped_on_message_keeps_event_loop_semantics():
    """The wrapper must be a coroutine function (the SDK awaits it)."""
    from codeband.orchestration.runner import _wrap_codex_on_message

    async def original(self, msg, tools, *args, **kwargs):
        return None

    assert inspect.iscoroutinefunction(_wrap_codex_on_message(original))
