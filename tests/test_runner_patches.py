"""Tests for the memory-backend patching logic in `orchestration.runner`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import (
    AgentsConfig,
    BandConfig,
    CodebandConfig,
    Framework,
    FrameworkPool,
    PoolEntry,
    RepoConfig,
    WorkspaceConfig,
)
from codeband.memory import LocalMemoryStore, reset_memory_mode
from codeband.orchestration.runner import (
    _create_coder,
    _install_memory_backend,
    _patch_agent_tools_to_local_store,
    _patch_band_local_runtime,
    _run_agent_forever,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.delenv("BAND_MEMORY_MODE", raising=False)
    reset_memory_mode()
    yield
    reset_memory_mode()


@pytest.fixture(autouse=True)
def _restore_agent_tools():
    """Each test may monkey-patch `AgentTools`; restore originals afterwards."""
    from thenvoi.runtime import tools as _tools_mod

    cls = getattr(_tools_mod, "AgentToolsRuntime", None) or getattr(
        _tools_mod, "AgentTools", None,
    )
    saved = {
        name: getattr(cls, name)
        for name in ("store_memory", "list_memories", "archive_memory")
    }
    yield
    for name, fn in saved.items():
        setattr(cls, name, fn)


def _make_config(tmp_path: Path, *, memory_mode: str = "auto") -> CodebandConfig:
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        band=BandConfig(memory_mode=memory_mode),
    )


def _fake_client(*, fail: bool = False):
    client = type("FakeClient", (), {})()
    client.agent_api_memories = type("FakeMemAPI", (), {})()
    if fail:
        class _Err(Exception):
            status_code = 403

        client.agent_api_memories.list_agent_memories = AsyncMock(side_effect=_Err())
    else:
        client.agent_api_memories.list_agent_memories = AsyncMock(return_value=object())
    return client


class _FakeAgentToolsInstance:
    """Mimics the shape `AgentTools` methods see — just needs a `.rest` attr."""

    def __init__(self):
        self.rest = None


class TestInstallBackend:
    async def test_local_mode_patches_and_writes_to_jsonl(self, tmp_path: Path):
        config = _make_config(tmp_path)
        client = _fake_client(fail=True)

        mode = await _install_memory_backend(
            config, Path(config.workspace.path), client,
        )
        assert mode == "local"

        # Exercise the patched classmethod path.
        from thenvoi.runtime import tools as _tools_mod
        cls = getattr(_tools_mod, "AgentToolsRuntime", None) or getattr(
            _tools_mod, "AgentTools",
        )
        instance = _FakeAgentToolsInstance()
        rec = await cls.store_memory(
            instance,
            "protocol plan cid plan_r1 state ready from planner to conductor",
            "working", "episodic", "agent",
            "plan ready",
            scope="organization",
        )
        assert rec.id.startswith("mem_")

        # And the file exists where we expect.
        jsonl = Path(config.workspace.path) / "state" / "memories.jsonl"
        assert jsonl.exists()
        assert "plan_r1" in jsonl.read_text()

        # list and archive should also work via the patched tools.
        resp = await cls.list_memories(
            instance, system="working", type="episodic",
            segment="agent", scope="organization",
        )
        assert len(resp.data) == 1

        archived = await cls.archive_memory(instance, rec.id)
        assert archived.status == "archived"

    async def test_band_mode_does_not_create_jsonl(self, tmp_path: Path):
        config = _make_config(tmp_path)
        client = _fake_client(fail=False)

        mode = await _install_memory_backend(
            config, Path(config.workspace.path), client,
        )
        assert mode == "band"
        jsonl = Path(config.workspace.path) / "state" / "memories.jsonl"
        assert not jsonl.exists()

    async def test_config_override_local_skips_probe(self, tmp_path: Path):
        config = _make_config(tmp_path, memory_mode="local")
        client = _fake_client(fail=False)  # would succeed but we force local
        mode = await _install_memory_backend(
            config, Path(config.workspace.path), client,
        )
        assert mode == "local"
        client.agent_api_memories.list_agent_memories.assert_not_awaited()

    async def test_env_override_band_forces_band_mode(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("BAND_MEMORY_MODE", "band")
        config = _make_config(tmp_path)
        client = _fake_client(fail=True)  # would fail but env forces band
        mode = await _install_memory_backend(
            config, Path(config.workspace.path), client,
        )
        assert mode == "band"


class TestLocalStorePatching:
    async def test_patched_tools_share_one_store_instance(self, tmp_path: Path):
        store_path = tmp_path / "state" / "mem.jsonl"
        store = LocalMemoryStore(store_path)
        _patch_agent_tools_to_local_store(store)

        from thenvoi.runtime import tools as _tools_mod
        cls = getattr(_tools_mod, "AgentToolsRuntime", None) or getattr(
            _tools_mod, "AgentTools",
        )
        a = _FakeAgentToolsInstance()
        b = _FakeAgentToolsInstance()

        await cls.store_memory(
            a, "from a", "working", "episodic", "agent", "",
            scope="organization",
        )
        await cls.store_memory(
            b, "from b", "working", "episodic", "agent", "",
            scope="organization",
        )

        # Both writes land in the same shared JSONL.
        resp = await cls.list_memories(
            a, system="working", type="episodic",
            segment="agent", scope="organization",
        )
        contents = {rec.content for rec in resp.data}
        assert contents == {"from a", "from b"}


class TestCoderModelWiring:
    """`_create_coder` must thread `coders.<framework>.model` from config
    through to the underlying runner. The bug this covers: earlier versions
    ignored the configured model, so `codeband.yaml` customizations were
    silently dropped."""

    def test_claude_coder_uses_configured_model(self, tmp_path: Path):
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=1, model="claude-opus-4-7"),
                ),
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        )
        adapter = _create_coder(
            Framework.CLAUDE_SDK, config, str(tmp_path),
        )
        # ClaudeSDKAdapter exposes the model via the `model` attribute.
        assert adapter.model == "claude-opus-4-7"

    def test_codex_coder_uses_configured_model(self, tmp_path: Path):
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=0),
                    codex=PoolEntry(count=1, model="gpt-5.5"),
                ),
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        )
        adapter = _create_coder(
            Framework.CODEX, config, str(tmp_path),
        )
        # CodexAdapter stores config; the runner passes `model=` to the SDK
        # which sets it on `adapter.config.model`.
        assert adapter.config.model == "gpt-5.5"

    def test_claude_coder_without_model_uses_runner_default(self, tmp_path: Path):
        """When PoolEntry.model is None, fall through to the runner default."""
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=1, model=None),
                ),
            ),
            workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        )
        adapter = _create_coder(
            Framework.CLAUDE_SDK, config, str(tmp_path),
        )
        # ClaudePlayerRunner default — coders use the heavier model.
        assert adapter.model == "claude-opus-4-7"


class TestRunAgentForever:
    """``_run_agent_forever`` must not terminate on its own.

    Contract: each cycle builds a fresh Agent via the supplied factory and
    tears it down in ``finally`` — so the SDK's internal PHX reconnect task
    cannot leak into the next cycle. Both crashes and clean exits trigger
    another cycle. Only ``CancelledError`` ends the loop.
    """

    @staticmethod
    def _make_agent_with_run(run_coro_factory):
        """Build a MagicMock Agent whose ``run`` is bound to a fresh coroutine
        and whose ``stop`` is an awaitable mock. Returns ``(agent, stop_mock)``.
        """
        agent = MagicMock()
        agent.run = run_coro_factory
        stop_mock = AsyncMock(return_value=True)
        agent.stop = stop_mock
        return agent, stop_mock

    @pytest.mark.asyncio
    async def test_reconnects_on_clean_exit_until_cancelled(
        self, monkeypatch
    ) -> None:
        # Collapse the backoff so 10 cycles happen in milliseconds.
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_BASE_DELAY_SECONDS", 0.0,
        )
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_MAX_DELAY_SECONDS", 0.0,
        )

        call_count = 0
        target_reached = asyncio.Event()
        produced_agents: list[MagicMock] = []

        async def clean_exit_run() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 5:
                target_reached.set()
                await asyncio.sleep(60)  # block until cancelled

        def make_agent(recovery_context: str | None = None) -> MagicMock:
            agent, _ = self._make_agent_with_run(clean_exit_run)
            produced_agents.append(agent)
            return agent

        activity = MagicMock()
        activity.log = MagicMock()

        task = asyncio.create_task(
            _run_agent_forever(make_agent, "test-agent", activity),
        )
        try:
            await asyncio.wait_for(target_reached.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count == 5, (
            "reconnect loop terminated before reaching 5 cycles — "
            "clean exits must trigger reconnection, not return"
        )
        # Each cycle must build a fresh Agent — that's the whole point of
        # the factory contract (regression against zombie PHX reconnect tasks).
        assert len(produced_agents) == 5, (
            f"expected 5 fresh agents from factory, got {len(produced_agents)}"
        )
        # Every completed cycle must have torn down its agent. The 5th cycle
        # is still running when we cancel, but cycles 1–4 must all be stopped.
        stopped = [a for a in produced_agents if a.stop.await_count >= 1]
        assert len(stopped) >= 4, (
            f"expected ≥4 agents to have stop() awaited between cycles, "
            f"got {len(stopped)} — PHX reconnect tasks would leak"
        )
        restart_events = [
            c.args[0] for c in activity.log.call_args_list
            if c.args[0] == "AGENT_RESTART"
        ]
        assert len(restart_events) >= 4, (
            f"expected ≥4 AGENT_RESTART events across 5 cycles, got {restart_events}"
        )

    @pytest.mark.asyncio
    async def test_reconnects_on_exception_until_cancelled(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_BASE_DELAY_SECONDS", 0.0,
        )
        monkeypatch.setattr(
            "codeband.orchestration.runner._RECONNECT_MAX_DELAY_SECONDS", 0.0,
        )

        call_count = 0
        target_reached = asyncio.Event()
        produced_agents: list[MagicMock] = []

        async def crashing_run() -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                target_reached.set()
                await asyncio.sleep(60)
            raise RuntimeError("simulated agent crash")

        def make_agent(recovery_context: str | None = None) -> MagicMock:
            agent, _ = self._make_agent_with_run(crashing_run)
            produced_agents.append(agent)
            return agent

        activity = MagicMock()
        activity.log = MagicMock()

        task = asyncio.create_task(
            _run_agent_forever(make_agent, "test-agent", activity),
        )
        try:
            await asyncio.wait_for(target_reached.wait(), timeout=2.0)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count == 3
        assert len(produced_agents) == 3
        # Even on the crash path, stop() must run in finally — otherwise
        # a crashed-then-restarted agent leaks its PHX client tasks.
        stopped = [a for a in produced_agents if a.stop.await_count >= 1]
        assert len(stopped) >= 2, (
            f"expected ≥2 crashed agents to have stop() awaited, got {len(stopped)}"
        )
        crash_events = [
            c.args[0] for c in activity.log.call_args_list
            if c.args[0] == "AGENT_CRASH"
        ]
        assert len(crash_events) >= 2, (
            f"expected ≥2 AGENT_CRASH events across 3 cycles, got {crash_events}"
        )


class TestPhoenixReconnectOwnership:
    """Local mode must not let PHX run a hidden reconnect loop under Codeband."""

    @pytest.mark.asyncio
    async def test_local_patch_disables_phx_auto_reconnect_and_signal_handlers(
        self, monkeypatch,
    ):
        from thenvoi.client.streaming import client as streaming_client

        original_aenter = streaming_client.WebSocketClient.__aenter__
        original_run_forever = streaming_client.PHXChannelsClient.run_forever
        created = []

        class FakePHXChannelsClient:
            run_forever = original_run_forever

            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.channel_socket_url = f"{args[0]}?api_key={args[1]}"
                self._message_routing_task = None
                created.append(self)

            async def __aenter__(self):
                return self

        monkeypatch.setattr(
            streaming_client, "PHXChannelsClient", FakePHXChannelsClient,
        )
        try:
            _patch_band_local_runtime()
            patched_run_forever = streaming_client.PHXChannelsClient.run_forever
            ws = streaming_client.WebSocketClient(
                "wss://example.test/socket", "api-key", agent_id="agent-123",
            )
            await ws.__aenter__()
        finally:
            streaming_client.WebSocketClient.__aenter__ = original_aenter
            streaming_client.PHXChannelsClient.run_forever = original_run_forever

        assert len(created) == 1
        assert created[0].kwargs["auto_reconnect"] is False
        assert created[0].channel_socket_url.endswith("&agent_id=agent-123")
        assert getattr(
            patched_run_forever,
            "_codeband_no_signal_handlers", False,
        ) is True

    @pytest.mark.asyncio
    async def test_local_patch_skips_existing_room_subscriptions_by_default(
        self, monkeypatch,
    ):
        from thenvoi.runtime.presence import RoomPresence

        original_subscribe = RoomPresence._subscribe_to_existing_rooms
        calls = []

        class FakeLink:
            async def subscribe_room(self, room_id):
                calls.append(("subscribe", room_id))

        try:
            _patch_band_local_runtime()
            presence = object.__new__(RoomPresence)
            presence.link = FakeLink()
            presence.rooms = set()
            presence.on_room_joined = None
            monkeypatch.delenv("CODEBAND_LOCAL_SUBSCRIBE_EXISTING", raising=False)
            presence._list_existing_rooms = AsyncMock(return_value=[
                ("room-1", {}),
                ("room-2", {}),
            ])
            monkeypatch.setattr(
                "codeband.orchestration.runner.asyncio.sleep", AsyncMock(),
            )

            await RoomPresence._subscribe_to_existing_rooms(presence)
        finally:
            RoomPresence._subscribe_to_existing_rooms = original_subscribe

        presence._list_existing_rooms.assert_not_awaited()
        assert calls == []
        assert presence.rooms == set()

    @pytest.mark.asyncio
    async def test_local_patch_can_serialize_existing_room_subscriptions(
        self, monkeypatch,
    ):
        from thenvoi.runtime.presence import RoomPresence

        original_subscribe = RoomPresence._subscribe_to_existing_rooms
        calls = []

        class FakeLink:
            async def subscribe_room(self, room_id):
                calls.append(("subscribe", room_id))

        try:
            _patch_band_local_runtime()
            presence = object.__new__(RoomPresence)
            presence.link = FakeLink()
            presence.rooms = set()
            presence.on_room_joined = None
            monkeypatch.setenv("CODEBAND_LOCAL_SUBSCRIBE_EXISTING", "1")
            presence._list_existing_rooms = AsyncMock(return_value=[
                ("room-1", {}),
                ("room-2", {}),
            ])
            monkeypatch.setattr(
                "codeband.orchestration.runner.asyncio.sleep", AsyncMock(),
            )

            await RoomPresence._subscribe_to_existing_rooms(presence)
        finally:
            RoomPresence._subscribe_to_existing_rooms = original_subscribe

        assert calls == [("subscribe", "room-1"), ("subscribe", "room-2")]
        assert presence.rooms == {"room-1", "room-2"}
