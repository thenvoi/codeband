"""Tests for the memory-backend patching logic in `orchestration.runner`."""

from __future__ import annotations

import asyncio
import logging
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


class TestNativeSubjectIdStripping:
    """The old `_patch_band_subject_id_bug` workaround was removed because
    band-sdk >=0.2.11 strips `subject_id=None` natively before the API call.
    This pins the BEHAVIOR (not the patch) so an SDK regression resurfaces
    here instead of as runtime 422s from the memory API."""

    @pytest.mark.asyncio
    async def test_native_store_memory_strips_subject_id_none(self):
        from thenvoi.runtime import tools as _tools_mod

        cls = getattr(_tools_mod, "AgentToolsRuntime", None) or getattr(
            _tools_mod, "AgentTools",
        )
        instance = _FakeAgentToolsInstance()
        instance.rest = MagicMock()
        response = MagicMock()
        response.data = {"id": "mem_1"}
        instance.rest.agent_api_memories.create_agent_memory = AsyncMock(
            return_value=response,
        )

        await cls.store_memory(
            instance,
            "protocol code_review cid cr_1_r1 state findings_posted",
            "working", "episodic", "agent",
            "review done",
            scope="organization",
            subject_id=None,
        )

        call = instance.rest.agent_api_memories.create_agent_memory.await_args
        request = call.kwargs["memory"]
        assert "subject_id" not in request.model_fields_set, (
            "native band-sdk store_memory no longer strips subject_id=None — "
            "the removed _patch_band_subject_id_bug workaround is needed again"
        )


class TestLocalRuntimePatchFailsLoud:
    """An SDK whose local-runtime hooks can't even be imported must abort
    startup — silently skipping the patch runs the fleet with PHX
    auto-reconnect enabled, which corrupts the reconnect lifecycle."""

    def test_import_error_raises_runtime_error(self, monkeypatch):
        import sys

        # `None` in sys.modules makes `from thenvoi.client.streaming import
        # client` raise ImportError — the same failure shape as an installed
        # band-sdk 1.0.0, which renamed the thenvoi.* namespace away.
        monkeypatch.setitem(sys.modules, "thenvoi.client.streaming", None)

        with pytest.raises(RuntimeError, match="band-sdk"):
            _patch_band_local_runtime()


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

class TestSubscribeExistingSweep:
    """Local-mode startup sweep: subscribe-by-default, store-scoped (finding 9).

    Matrix: fresh run / mid-task resume / stale rooms / store failure /
    --fresh opt-out / deprecated env var. Filtering happens inside OUR
    patched sweep — never via the SDK's ``RoomPresence(room_filter=...)``,
    which also gates live ``room_added`` joins and would block new tasks.
    """

    @pytest.fixture(autouse=True)
    def _sweep_settings(self, monkeypatch):
        """Isolate the module-level sweep settings per test."""
        from codeband.orchestration import runner

        monkeypatch.setattr(
            runner, "_local_sweep_settings", runner._LocalSweepSettings(),
        )
        monkeypatch.delenv("CODEBAND_LOCAL_SUBSCRIBE_EXISTING", raising=False)
        return runner._local_sweep_settings

    def _make_store(self, tmp_path: Path, active_rooms: list[str]) -> Path:
        from codeband.state import StateStore

        db_path = tmp_path / "state" / "orchestration.db"
        store = StateStore(db_path)
        for room_id in active_rooms:
            store.create_task(
                task_id=room_id, description="task", room_id=room_id,
            )
        return db_path

    async def _run_sweep(self, monkeypatch, participant_rooms: list[str]):
        """Run the patched sweep against fake link + rooms; return subscribe calls."""
        from thenvoi.runtime.presence import RoomPresence

        original_subscribe = RoomPresence._subscribe_to_existing_rooms
        calls: list[str] = []

        class FakeLink:
            async def subscribe_room(self, room_id):
                calls.append(room_id)

        try:
            _patch_band_local_runtime()
            presence = object.__new__(RoomPresence)
            presence.link = FakeLink()
            presence.rooms = set()
            presence.on_room_joined = None
            presence._list_existing_rooms = AsyncMock(
                return_value=[(room_id, {}) for room_id in participant_rooms],
            )
            monkeypatch.setattr(
                "codeband.orchestration.runner.asyncio.sleep", AsyncMock(),
            )
            await RoomPresence._subscribe_to_existing_rooms(presence)
        finally:
            RoomPresence._subscribe_to_existing_rooms = original_subscribe
        return calls, presence

    @pytest.mark.asyncio
    async def test_fresh_run_subscribes_nothing_without_warning(
        self, monkeypatch, tmp_path, caplog, _sweep_settings,
    ):
        """Store readable, zero active tasks, no rooms — normal, no warning."""
        _sweep_settings.state_db_path = self._make_store(tmp_path, [])

        with caplog.at_level("INFO", logger="codeband.orchestration.runner"):
            calls, presence = await self._run_sweep(monkeypatch, [])

        assert calls == []
        assert presence.rooms == set()
        assert not [
            r for r in caplog.records
            if r.name.startswith("codeband") and r.levelno >= logging.WARNING
        ]

    @pytest.mark.asyncio
    async def test_mid_task_resume_rejoins_active_room(
        self, monkeypatch, tmp_path, _sweep_settings,
    ):
        _sweep_settings.state_db_path = self._make_store(tmp_path, ["room-1"])

        calls, presence = await self._run_sweep(monkeypatch, ["room-1"])

        assert calls == ["room-1"]
        assert presence.rooms == {"room-1"}

    @pytest.mark.asyncio
    async def test_stale_rooms_skipped_with_info_line(
        self, monkeypatch, tmp_path, caplog, _sweep_settings,
    ):
        """Rooms not tied to an active task are skipped, with one INFO count."""
        _sweep_settings.state_db_path = self._make_store(tmp_path, ["room-1"])

        with caplog.at_level("INFO", logger="codeband.orchestration.runner"):
            calls, presence = await self._run_sweep(
                monkeypatch, ["room-1", "stale-a", "stale-b"],
            )

        assert calls == ["room-1"]
        assert presence.rooms == {"room-1"}
        skip_lines = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "Skipping 2" in r.getMessage()
        ]
        assert len(skip_lines) == 1

    @pytest.mark.asyncio
    async def test_store_failure_subscribes_all_with_error(
        self, monkeypatch, tmp_path, caplog, _sweep_settings,
    ):
        """Unreadable store → loud ERROR, subscribe ALL (fail toward connectivity)."""
        corrupt = tmp_path / "state" / "orchestration.db"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_text("this is not a sqlite database, not even close")
        _sweep_settings.state_db_path = corrupt

        with caplog.at_level("ERROR", logger="codeband.orchestration.runner"):
            calls, _ = await self._run_sweep(monkeypatch, ["room-1", "room-2"])

        assert calls == ["room-1", "room-2"]
        errors = [
            r for r in caplog.records
            if r.name == "codeband.orchestration.runner"
            and r.levelno == logging.ERROR
        ]
        assert len(errors) == 1
        assert "ALL participant rooms" in errors[0].getMessage()

    @pytest.mark.asyncio
    async def test_unresolved_state_dir_subscribes_all_with_error(
        self, monkeypatch, caplog, _sweep_settings,
    ):
        assert _sweep_settings.state_db_path is None

        with caplog.at_level("ERROR", logger="codeband.orchestration.runner"):
            calls, _ = await self._run_sweep(monkeypatch, ["room-1"])

        assert calls == ["room-1"]
        errors = [
            r for r in caplog.records
            if r.name == "codeband.orchestration.runner"
            and r.levelno == logging.ERROR
        ]
        assert len(errors) == 1
        assert "state dir unresolved" in errors[0].getMessage()

    @pytest.mark.asyncio
    async def test_fresh_flag_skips_sweep_even_with_active_rooms(
        self, monkeypatch, tmp_path, _sweep_settings,
    ):
        _sweep_settings.state_db_path = self._make_store(tmp_path, ["room-1"])
        _sweep_settings.fresh = True

        calls, presence = await self._run_sweep(monkeypatch, ["room-1"])

        assert calls == []
        assert presence.rooms == set()
        presence._list_existing_rooms.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deprecated_env_var_warns_and_changes_nothing(
        self, monkeypatch, tmp_path, caplog, _sweep_settings,
    ):
        """CODEBAND_LOCAL_SUBSCRIBE_EXISTING is ignored: one deprecation warning,
        behavior identical to the store-scoped default."""
        monkeypatch.setenv("CODEBAND_LOCAL_SUBSCRIBE_EXISTING", "1")
        _sweep_settings.state_db_path = self._make_store(tmp_path, ["room-1"])

        with caplog.at_level("WARNING", logger="codeband.orchestration.runner"):
            calls, _ = await self._run_sweep(monkeypatch, ["room-1", "stale-a"])

        assert calls == ["room-1"]  # stale room still skipped — default semantics
        deprecations = [
            r for r in caplog.records if "deprecated" in r.getMessage()
        ]
        assert len(deprecations) == 1
        assert "--fresh" in deprecations[0].getMessage()


class TestSessionConfigWiring:
    """agents.idle_resync_seconds reaches the SDK at the Agent.create seam."""

    def test_create_band_agent_passes_idle_resync_seconds(self, tmp_path, monkeypatch):
        import thenvoi

        from codeband.config import AgentCredentials
        from codeband.orchestration.runner import _create_band_agent

        config = _make_config(tmp_path)
        config.agents.idle_resync_seconds = 7
        captured = {}

        def fake_create(cls, **kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr(
            thenvoi.Agent, "create", classmethod(fake_create),
        )
        _create_band_agent(
            adapter=MagicMock(),
            creds=AgentCredentials(agent_id="agent-1", api_key="key-1"),
            config=config,
        )

        session_config = captured["session_config"]
        assert session_config.idle_resync_seconds == 7

    def test_default_is_30(self, tmp_path):
        config = _make_config(tmp_path)
        assert config.agents.idle_resync_seconds == 30


class TestSafeStopAgentFailsLoud:
    """Teardown failures log at ERROR and verify closure — never raise."""

    @pytest.mark.asyncio
    async def test_stop_failure_logs_error_and_does_not_raise(self, caplog):
        from codeband.orchestration.runner import _safe_stop_agent

        class FailingAgent:
            _runtime = None

            async def stop(self, timeout=None):
                raise RuntimeError("socket exploded")

        with caplog.at_level("ERROR", logger="codeband.orchestration.runner"):
            await _safe_stop_agent(FailingAgent(), "conductor")

        errors = [
            r for r in caplog.records
            if r.name == "codeband.orchestration.runner"
            and r.levelno == logging.ERROR
        ]
        assert len(errors) == 1
        message = errors[0].getMessage()
        assert "conductor" in message
        assert "socket exploded" in message

    @pytest.mark.asyncio
    async def test_leaked_connection_logs_second_error(self, caplog):
        from codeband.orchestration.runner import _safe_stop_agent

        class LeakyLink:
            _ws = object()  # still holding a websocket after stop()
            is_connected = True

        class LeakyRuntime:
            _link = LeakyLink()

        class LeakyAgent:
            _runtime = LeakyRuntime()

            async def stop(self, timeout=None):
                return True  # claims success but leaves the socket open

        with caplog.at_level("ERROR", logger="codeband.orchestration.runner"):
            await _safe_stop_agent(LeakyAgent(), "mergemaster")

        errors = [
            r for r in caplog.records
            if r.name == "codeband.orchestration.runner"
            and r.levelno == logging.ERROR
        ]
        assert len(errors) == 1
        assert "mergemaster" in errors[0].getMessage()
        assert "leaked" in errors[0].getMessage()

    @pytest.mark.asyncio
    async def test_clean_stop_logs_nothing(self, caplog):
        from codeband.orchestration.runner import _safe_stop_agent

        class ClosedLink:
            _ws = None
            is_connected = False

        class ClosedRuntime:
            _link = ClosedLink()

        class CleanAgent:
            _runtime = ClosedRuntime()

            async def stop(self, timeout=None):
                return True

        with caplog.at_level("ERROR", logger="codeband.orchestration.runner"):
            await _safe_stop_agent(CleanAgent(), "conductor")

        assert not [
            r for r in caplog.records
            if r.name.startswith("codeband") and r.levelno >= logging.ERROR
        ]

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        from codeband.orchestration.runner import _safe_stop_agent

        class CancelledAgent:
            _runtime = None

            async def stop(self, timeout=None):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await _safe_stop_agent(CancelledAgent(), "conductor")
