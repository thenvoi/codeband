"""Local runner — start pool-driven workers + coordinators in-process."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from codeband.config import (
    AgentCredentials,
    CodebandConfig,
    Framework,
    FrameworkPool,
    PoolEntry,
    load_agent_config,
)
from codeband.workers import WorkerId, WorkerRole
from codeband.workspace.init import initialize_agent_workspace, initialize_workspace

if TYPE_CHECKING:
    from codeband.config import AgentConfigFile
    from thenvoi.core.protocols import FrameworkAdapter

logger = logging.getLogger(__name__)

# Roles that are always present exactly once per project.
_SINGLETON_KEYS = ("conductor", "mergemaster")

# Light stagger between local in-process agent starts. Docker naturally spreads
# each agent across container startup; plain ``cb`` otherwise opens and joins
# all websocket topics from one event loop in under a second, which can trigger
# gateway-side EOFs during startup.
_STARTUP_DELAY = 0.75  # seconds between agent starts

# Reconnect-forever loop tunables. Both crashes and clean exits from
# ``agent.run()`` trigger another cycle; backoff grows exponentially and is
# capped so persistent failures don't drift into hour-long sleeps.
_RECONNECT_BASE_DELAY_SECONDS = 2.0
_RECONNECT_MAX_DELAY_SECONDS = 60.0


async def _safe_stop_agent(agent: object) -> None:
    """Best-effort teardown of a Band.ai Agent between reconnect cycles.

    Why: PHXChannelsClient owns its own auto-reconnect task. Without an
    explicit stop, that task survives ``agent.run()`` returning and races
    the next cycle, producing ``PHXTopicError: already subscribed`` and
    ``cannot call recv while another coroutine is already running recv``.
    """
    stop = getattr(agent, "stop", None)
    if stop is None:
        return
    try:
        await stop(timeout=2.0)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("agent.stop() raised during teardown", exc_info=True)


def _patch_band_local_runtime() -> None:
    """Make the Band SDK safe for Codeband's in-process local runtime.

    ``thenvoi`` builds ``PHXChannelsClient`` with its default
    ``auto_reconnect=True``. In local mode Codeband already owns the outer
    reconnect loop and creates a fresh Agent per cycle. Letting the Phoenix
    client also reconnect in the background creates competing lifecycle
    owners: ``agent.run()`` returns while PHX reconnect tasks can still be
    alive, which causes duplicate reconnect attempts and topic subscription
    races. Until the SDK exposes this as a public option, patch the local
    process to create PHX clients with ``auto_reconnect=False``.

    The SDK also assumes each Agent owns its process: every PHX client
    registers process signal handlers, and RoomPresence auto-joins existing
    room topics during startup. Those are reasonable defaults for one agent
    per process, but unsafe/noisy when plain ``cb`` runs the full fleet in
    one event loop. In local mode Codeband owns signals, and agents subscribe
    to new rooms via ``agent_rooms`` instead of replaying old room state at
    startup. Set ``CODEBAND_LOCAL_SUBSCRIBE_EXISTING=1`` to restore startup
    backlog subscription for debugging.
    """
    try:
        from thenvoi.client.streaming import client as streaming_client
        from thenvoi.runtime.presence import RoomPresence
    except Exception:
        logger.debug("Could not import Band local runtime hooks", exc_info=True)
        return

    websocket_cls = getattr(streaming_client, "WebSocketClient", None)
    if websocket_cls is not None:
        current_aenter = getattr(websocket_cls, "__aenter__", None)
        if not getattr(current_aenter, "_codeband_no_auto_reconnect", False):
            async def _codeband_aenter(self):
                self.client = streaming_client.PHXChannelsClient(
                    self.ws_url,
                    self.api_key,
                    protocol_version=streaming_client.PhoenixChannelsProtocolVersion.V2,
                    auto_reconnect=False,
                    on_reconnect=self._on_reconnect,
                    on_disconnect=self._on_disconnect,
                )
                if self.agent_id:
                    self.client.channel_socket_url += f"&agent_id={self.agent_id}"
                await self.client.__aenter__()
                return self

            _codeband_aenter._codeband_no_auto_reconnect = True  # type: ignore[attr-defined]
            websocket_cls.__aenter__ = _codeband_aenter

    phx_cls = getattr(streaming_client, "PHXChannelsClient", None)
    current_run_forever = getattr(phx_cls, "run_forever", None)
    if phx_cls is not None and not getattr(
        current_run_forever, "_codeband_no_signal_handlers", False,
    ):
        async def _codeband_run_forever(self) -> None:
            if self._message_routing_task is None:
                raise RuntimeError("Client is not connected")
            await self._message_routing_task

        _codeband_run_forever._codeband_no_signal_handlers = True  # type: ignore[attr-defined]
        phx_cls.run_forever = _codeband_run_forever

    current_subscribe_existing = getattr(
        RoomPresence, "_subscribe_to_existing_rooms", None,
    )
    if getattr(current_subscribe_existing, "_codeband_serial_existing_rooms", False):
        return

    async def _codeband_subscribe_to_existing_rooms(self) -> None:
        import os

        if os.environ.get("CODEBAND_LOCAL_SUBSCRIBE_EXISTING") != "1":
            logger.info(
                "Skipping existing-room websocket subscriptions in local mode"
            )
            return

        logger.debug("Subscribing to existing rooms serially")
        try:
            rooms_to_join = await self._list_existing_rooms()
            if not rooms_to_join:
                return

            succeeded = 0
            failed = 0
            for room_id, payload in rooms_to_join:
                try:
                    await self.link.subscribe_room(room_id)
                    self.rooms.add(room_id)
                    if self.on_room_joined:
                        await self.on_room_joined(room_id, payload)
                    succeeded += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    failed += 1
                    logger.warning("Failed to subscribe to room %s: %s", room_id, e)
                    self.rooms.discard(room_id)

            if failed:
                logger.warning(
                    "Subscribed to %s existing rooms (%s failed)",
                    succeeded, failed,
                )
            else:
                logger.info("Subscribed to %s existing rooms", succeeded)
        except Exception as e:
            logger.warning("Failed to subscribe to existing rooms: %s", e)

    _codeband_subscribe_to_existing_rooms._codeband_serial_existing_rooms = True  # type: ignore[attr-defined]
    RoomPresence._subscribe_to_existing_rooms = _codeband_subscribe_to_existing_rooms


async def _run_agent_forever(
    make_agent: Callable[[], object], name: str, activity: object,
) -> None:
    """Run an unsupervised agent under an infinite reconnect loop.

    Each cycle builds a fresh Agent via ``make_agent()`` and tears it down
    in ``finally`` so the underlying PHXChannelsClient's reconnect/heartbeat
    tasks cannot leak into the next cycle. Both crashes and clean exits
    trigger another cycle after exponential backoff. The loop ends only
    when the enclosing task is cancelled by the shutdown path.
    """
    attempt = 0
    while True:
        attempt += 1
        agent = make_agent()
        try:
            try:
                await agent.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "%s crashed (attempt %d): %s: %s",
                    name, attempt, type(exc).__name__, exc,
                )
                activity.log(
                    "AGENT_CRASH", name,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                logger.warning(
                    "%s run() returned cleanly — reconnecting (attempt %d)",
                    name, attempt,
                )
                activity.log(
                    "AGENT_RESTART", name,
                    f"Clean exit — reconnect #{attempt}",
                )
        finally:
            await _safe_stop_agent(agent)
        delay = min(
            _RECONNECT_BASE_DELAY_SECONDS * (2 ** min(attempt - 1, 5)),
            _RECONNECT_MAX_DELAY_SECONDS,
        )
        await asyncio.sleep(delay)


# ─── memory backend patches ────────────────────────────────────────────────

def _get_tools_class():
    """Return the SDK's agent-tools class, or None if the import fails."""
    try:
        from thenvoi.runtime import tools as _tools_mod
    except Exception:
        return None
    return getattr(_tools_mod, "AgentToolsRuntime", None) or getattr(
        _tools_mod, "AgentTools", None,
    )


def _patch_band_subject_id_bug() -> None:
    """Work around band-sdk bug: strip subject_id=None before API call."""
    cls = _get_tools_class()
    if cls is None or getattr(cls.store_memory, "_codeband_patched", False):
        return

    async def _patched_store_memory(
        self,
        content,
        system,
        type,
        segment,
        thought,
        scope="subject",
        subject_id=None,
        metadata=None,
    ):
        from thenvoi.client.rest import MemoryCreateRequest

        kwargs = dict(
            content=content,
            system=system,
            type=type,
            segment=segment,
            thought=thought,
            scope=scope,
            metadata=metadata,
        )
        if subject_id is not None:
            kwargs["subject_id"] = subject_id

        response = await self.rest.agent_api_memories.create_agent_memory(
            memory=MemoryCreateRequest(**kwargs)
        )
        if not response.data:
            raise RuntimeError("Failed to store memory - no response data")
        return response.data

    _patched_store_memory._codeband_patched = True
    cls.store_memory = _patched_store_memory


def _patch_agent_tools_to_local_store(store) -> None:
    """Redirect AgentTools memory methods at `store` (a LocalMemoryStore)."""
    cls = _get_tools_class()
    if cls is None:
        logger.warning(
            "Could not locate AgentTools class — agents will still attempt "
            "Band.ai memory calls and fail.",
        )
        return

    async def _local_store_memory(
        self, content, system, type, segment, thought,
        scope="subject", subject_id=None, metadata=None,
    ):
        return await store.store(
            content=content, system=system, type=type, segment=segment,
            thought=thought, scope=scope, subject_id=subject_id, metadata=metadata,
        )

    async def _local_list_memories(
        self, subject_id=None, scope=None, system=None, type=None,
        segment=None, content_query=None, page_size=50, status=None,
    ):
        return await store.list(
            subject_id=subject_id, scope=scope, system=system, type=type,
            segment=segment, content_query=content_query, page_size=page_size,
            status=status,
        )

    async def _local_archive_memory(self, memory_id):
        record = await store.archive(memory_id)
        if record is None:
            raise RuntimeError(f"Memory {memory_id} not found in local store")
        return record

    for fn, name in (
        (_local_store_memory, "store_memory"),
        (_local_list_memories, "list_memories"),
        (_local_archive_memory, "archive_memory"),
    ):
        fn._codeband_patched = True  # noqa: SLF001
        fn._codeband_local = True    # noqa: SLF001
        setattr(cls, name, fn)


def _build_watchdog_memory_store(memory_mode: str, workspace_path: Path) -> Any:
    """Construct a ``LocalMemoryStore`` for the watchdog when memory is local.

    On paid tier the watchdog reads via ``rest_client.agent_api_memories``
    and this returns ``None``. On free tier or when Band.ai is unreachable,
    the runner installs a JSONL-backed local store under ``state/`` and
    monkey-patches the agent-tool memory API onto it; the watchdog needs a
    direct handle to that same file so its swarm-status gate sees the
    Conductor's writes.
    """
    if memory_mode != "local":
        return None
    from codeband.memory import LocalMemoryStore
    return LocalMemoryStore(workspace_path / "state" / "memories.jsonl")


async def _install_memory_backend(
    config: CodebandConfig, workspace_path: Path, rest_client,
):
    """Probe Band.ai memory availability once, then install patches accordingly."""
    from codeband.memory import LocalMemoryStore, probe_memory_backend
    from codeband.memory.probe import get_memory_mode_reason

    override = config.band.memory_mode if config.band.memory_mode != "auto" else None
    mode = await probe_memory_backend(rest_client, config_override=override)
    reason = get_memory_mode_reason() or "unknown"

    # Two-line banner: status first, then where memory actually lives.
    if reason == "paid tier":
        status_line = "Using Band paid tier"
    elif reason == "free tier":
        status_line = "Using Band free tier"
    elif reason == "Band.ai unreachable":
        status_line = "Band unreachable — using local fallback"
    else:
        # Forced via env/config or some other diagnostic — surface it as-is.
        status_line = f"Memory mode: {mode} ({reason})"

    if mode == "local":
        store_path = Path(workspace_path) / "state" / "memories.jsonl"
        store = LocalMemoryStore(store_path)
        _patch_agent_tools_to_local_store(store)
        print(status_line)
        print(f"Memory: local JSONL store at {store_path}")
    else:
        _patch_band_subject_id_bug()
        print(status_line)
        print("Memory: Band.ai remote API")

    return mode


# ─── workspace path helpers ─────────────────────────────────────────────────

def _resolve_workspace_config(config: CodebandConfig, project_dir: Path) -> CodebandConfig:
    """Resolve workspace path relative to project_dir, returning updated config."""
    import os

    ws_path = Path(config.workspace.path)
    if not ws_path.is_absolute():
        workspace_env = os.environ.get("WORKSPACE")
        base = Path(workspace_env) if workspace_env else project_dir
        resolved = str(base / ws_path)
    elif not ws_path.exists():
        resolved = str(ws_path)
        logger.info("Creating workspace directory at %s", resolved)
    else:
        resolved = str(ws_path)
    return config.model_copy(
        update={"workspace": config.workspace.model_copy(update={"path": resolved})}
    )


def _create_band_agent(adapter, creds: AgentCredentials, config: CodebandConfig):
    """Create a Band.ai Agent with standard connection args."""
    from thenvoi import Agent

    return Agent.create(
        adapter=adapter,
        agent_id=creds.agent_id,
        api_key=creds.api_key,
        ws_url=config.band.ws_url,
        rest_url=config.band.rest_url,
    )


def _create_rest_client(api_key: str, rest_url: str):
    """Create a Band.ai REST client (used for the memory probe and watchdog)."""
    from thenvoi.client.rest import AsyncRestClient

    return AsyncRestClient(api_key=api_key, base_url=rest_url)


async def _build_watchdog_extras(
    agent_config: AgentConfigFile,
    resolved_config: CodebandConfig,
) -> tuple[dict[str, str], object | None]:
    """Build the agent_id→role map and, when available, a human-API REST client.

    The role map powers per-role stale thresholds. The human REST client is
    only returned when the liveness probe resolves to `"human"` (enterprise
    tier); on free tier it's `None` and the watchdog falls back to the
    agent-API inbox read path. `BAND_API_KEY` must be set to even attempt the
    probe — without it there's no way to hit the human API.
    """
    import os

    from codeband.agents.watchdog_probe import probe_liveness_backend

    role_map: dict[str, str] = {}
    for key, creds in agent_config.agents.items():
        try:
            role_map[creds.agent_id] = _role_from_key(key)
        except ValueError:
            # Unknown/legacy key — skip it; the watchdog treats unmapped
            # agent ids as using the default threshold.
            continue

    human_rest: object | None = None
    human_key = os.environ.get("BAND_API_KEY")
    if human_key:
        candidate = _create_rest_client(human_key, resolved_config.band.rest_url)
        mode = resolved_config.band.liveness_mode
        tier = await probe_liveness_backend(
            candidate,
            config_override=mode if mode in ("human", "agent") else None,
        )
        if tier == "human":
            human_rest = candidate

    return role_map, human_rest


# ─── agent registration ─────────────────────────────────────────────────────

async def _ensure_agents_registered(
    config: CodebandConfig, project_dir: Path,
) -> "AgentConfigFile":
    """Load agent config, auto-registering any missing agents."""
    from codeband.orchestration.setup import _expected_agents

    config_path = project_dir / "agent_config.yaml"
    if not config_path.exists():
        logger.info("No agent_config.yaml found — registering all agents...")
        try:
            from codeband.orchestration.setup import register_all_agents
            await register_all_agents(config, project_dir)
            return load_agent_config(project_dir)
        except Exception as e:
            raise RuntimeError(
                f"Cannot auto-register agents: {e}\n"
                "Run 'codeband setup-agents' manually to register them."
            ) from e

    agent_config = load_agent_config(project_dir)

    # Check for missing agents
    expected_keys = set(_expected_agents(config).keys())
    missing = expected_keys - set(agent_config.agents.keys())

    if missing:
        logger.info(
            "Missing credentials for %s — auto-registering...",
            ", ".join(sorted(missing)),
        )
        try:
            from codeband.orchestration.setup import register_all_agents
            await register_all_agents(config, project_dir)
            agent_config = load_agent_config(project_dir)
        except Exception as e:
            raise RuntimeError(
                f"Cannot auto-register agents ({', '.join(sorted(missing))}): {e}\n"
                "Run 'codeband setup-agents' manually to register them."
            ) from e

    return agent_config


# ─── pool iteration ─────────────────────────────────────────────────────────

def _iter_pool(pool: FrameworkPool, role: WorkerRole):
    """Yield (WorkerId, PoolEntry) for each active slot in `pool`."""
    for framework in (Framework.CLAUDE_SDK, Framework.CODEX):
        entry = pool.entry_for(framework)
        for i in range(entry.count):
            yield WorkerId(role=role, framework=framework, index=i), entry


# ─── run_local: pool-driven in-process runtime ─────────────────────────────

async def run_local(
    config: CodebandConfig,
    project_dir: Path,
    *,
    shutdown_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
) -> None:
    """Run all Codeband agents in a single async process.

    If ``shutdown_event`` is supplied (e.g. by the interactive shell's
    ``/quit`` handler), the caller owns the signal-handler registration
    and is responsible for setting the event when shutdown is desired.
    Otherwise this function builds its own event and binds SIGINT/SIGTERM
    to it — the standard ``cb run`` behavior.

    If ``ready_event`` is supplied, it is set once the agents banner has
    been printed and all tasks have been spawned. Used by the shell to
    sequence the "Ready; use /help…" hint after the orchestrator banner.
    """

    agent_config = await _ensure_agents_registered(config, project_dir)
    resolved_config = _resolve_workspace_config(config, project_dir)
    layout = initialize_workspace(resolved_config)
    _patch_band_local_runtime()

    # Resolve memory backend once per process, using the Conductor's creds.
    conductor_creds = agent_config.get("conductor")
    probe_client = _create_rest_client(conductor_creds.api_key, resolved_config.band.rest_url)
    memory_mode = await _install_memory_backend(
        resolved_config, Path(resolved_config.workspace.path), probe_client,
    )

    # Activity logger
    from codeband.monitoring.activity_log import ActivityLogger
    activity = ActivityLogger(layout.state_dir / "activity.jsonl")

    # Attach SDK usage tracking — tag log records with agent names
    # based on which asyncio task emitted them.
    from codeband.monitoring.usage import AgentTaskFilter, SDKUsageHandler

    agent_task_filter = AgentTaskFilter()
    sdk_usage_handler = SDKUsageHandler(activity)
    sdk_logger = logging.getLogger("thenvoi.adapters")
    sdk_logger.addFilter(agent_task_filter)
    sdk_logger.addHandler(sdk_usage_handler)

    worker_roster = _build_worker_roster(resolved_config)

    # Build unsupervised agents. Each entry is ``(make_agent, display_name)``
    # — a factory, not an instance — so ``_run_agent_forever`` can spin up a
    # brand-new Agent (and a brand-new PHXChannelsClient) on every reconnect
    # cycle. Reusing an instance leaks the SDK's internal reconnect task into
    # the next cycle and produces "Topic ... already subscribed" cascades.
    def _band_agent_factory(adapter, creds):
        config = resolved_config
        return lambda: _create_band_agent(adapter, creds, config)

    unsupervised: list[tuple[Callable[[], object], str]] = []

    # --- Conductor (singleton) ---
    cond_adapter = _create_conductor(
        resolved_config, worker_roster=worker_roster,
    )
    unsupervised.append(
        (_band_agent_factory(cond_adapter, conductor_creds), "conductor"),
    )
    logger.info("Created Conductor agent")

    # --- Mergemaster (singleton) ---
    mm_creds = agent_config.get("mergemaster")
    mm_adapter = _create_mergemaster(
        resolved_config,
        str(layout.mergemaster_worktree) if layout.mergemaster_worktree else None,
    )
    unsupervised.append(
        (_band_agent_factory(mm_adapter, mm_creds), "mergemaster"),
    )
    logger.info("Created Mergemaster agent")

    # --- Planner pool ---
    for wid, _entry in _iter_pool(resolved_config.agents.planners, WorkerRole.PLANNER):
        key = str(wid)
        creds = agent_config.get(key)
        wt_path = layout.planner_worktrees.get(key)
        adapter = _create_planner(
            resolved_config,
            workspace=str(wt_path) if wt_path else None,
            framework=wid.framework,
            worker_roster=worker_roster,
        )
        unsupervised.append((_band_agent_factory(adapter, creds), key))
        logger.info("Created %s", key)

    # --- Plan Reviewer pool ---
    for wid, _entry in _iter_pool(
        resolved_config.agents.plan_reviewers, WorkerRole.PLAN_REVIEWER,
    ):
        key = str(wid)
        creds = agent_config.get(key)
        wt_path = layout.plan_reviewer_worktrees.get(key)
        adapter = _create_plan_reviewer(
            resolved_config,
            workspace=str(wt_path) if wt_path else None,
            framework=wid.framework,
        )
        unsupervised.append((_band_agent_factory(adapter, creds), key))
        logger.info("Created %s", key)

    # --- Reviewer pool (code reviewers) ---
    for wid, _entry in _iter_pool(resolved_config.agents.reviewers, WorkerRole.REVIEWER):
        key = str(wid)
        creds = agent_config.get(key)
        scratch_path = layout.reviewer_scratch.get(key)
        adapter = _create_code_reviewer(
            resolved_config,
            workspace=str(scratch_path) if scratch_path else None,
            framework=wid.framework,
        )
        unsupervised.append((_band_agent_factory(adapter, creds), key))
        logger.info("Created %s", key)

    # --- Watchdog (deterministic daemon, not a Band.ai Agent) ---
    from codeband.agents.watchdog import WatchdogDaemon

    wd_rest = _create_rest_client(conductor_creds.api_key, resolved_config.band.rest_url)
    role_map, wd_human_rest = await _build_watchdog_extras(
        agent_config, resolved_config,
    )
    watchdog = WatchdogDaemon(
        config=resolved_config.agents.watchdog,
        rest_client=wd_rest,
        agent_id=conductor_creds.agent_id,
        conductor_id=conductor_creds.agent_id,
        activity=activity,
        agent_id_to_role=role_map,
        human_rest_client=wd_human_rest,
        local_memory_store=_build_watchdog_memory_store(
            memory_mode, Path(resolved_config.workspace.path),
        ),
    )
    logger.info("Created Watchdog daemon")

    # --- Coder pool — supervised (crash = restart) ---
    from codeband.session.supervisor import WorkerSupervisor

    supervisors: list[WorkerSupervisor] = []
    for wid, entry in _iter_pool(resolved_config.agents.coders, WorkerRole.CODER):
        key = str(wid)
        creds = agent_config.get(key)
        wt_path = layout.coder_worktrees.get(key)
        supervisor = WorkerSupervisor(
            worker_id=key,
            agent_id=creds.agent_id,
            create_agent_fn=_coder_factory(
                wid.framework, resolved_config, wt_path, creds,
            ),
            state_dir=layout.state_dir,
            worktree_path=wt_path,
            restart_delay_seconds=entry.restart_delay_seconds,
            activity=activity,
        )
        supervisors.append(supervisor)
        logger.info("Created supervisor for %s", key)

    # Run all concurrently
    if shutdown_event is None:
        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_event.set)

    agent_count = len(unsupervised) + len(supervisors) + 1  # +1 for watchdog
    activity.log("SYSTEM_START", "codeband", f"Starting {agent_count} agents")
    logger.info("Starting %d agents...", agent_count)

    # task → human-readable name for shutdown-time diagnostics.
    task_names: dict[asyncio.Task, str] = {}

    unsupervised_tasks = []
    for i, (make_agent, name) in enumerate(unsupervised):
        if i > 0:
            await asyncio.sleep(_STARTUP_DELAY)
        task = asyncio.create_task(_run_agent_forever(make_agent, name, activity))
        agent_task_filter.register(task, name)
        task_names[task] = name
        unsupervised_tasks.append(task)

    supervisor_tasks = []
    for supervisor in supervisors:
        await asyncio.sleep(_STARTUP_DELAY)
        task = asyncio.create_task(supervisor.run())
        agent_task_filter.register(task, supervisor._worker_id)  # noqa: SLF001
        task_names[task] = supervisor._worker_id  # noqa: SLF001
        supervisor_tasks.append(task)

    watchdog_task = asyncio.create_task(watchdog.run())
    task_names[watchdog_task] = "watchdog"
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    all_tasks = unsupervised_tasks + supervisor_tasks + [watchdog_task]
    worker_keys = [name for _, name in unsupervised] + [s._worker_id for s in supervisors]  # noqa: SLF001
    print(f"Agents ({agent_count}): {', '.join(worker_keys)}, watchdog")
    if ready_event is not None:
        ready_event.set()

    # Agent tasks are infinite loops by design — the only way this await
    # returns is a SIGINT/SIGTERM setting ``shutdown_event``. Any task that
    # has somehow finished before shutdown is a bug we want to see; we log it
    # below in the defensive sweep.
    await shutdown_task
    logger.warning("Shutdown signal received — stopping all agents")

    for t in all_tasks:
        if not t.done():
            continue
        name = task_names.get(t, "unknown-task")
        if t.cancelled():
            continue
        if t.exception() is not None:
            exc = t.exception()
            logger.error(
                "Task %s had already died before shutdown: %s: %s",
                name, type(exc).__name__, exc,
            )
            activity.log(
                "AGENT_CRASH", name,
                f"{type(exc).__name__}: {exc}",
            )
        else:
            logger.warning("Task %s had already finished cleanly before shutdown", name)

    logger.info("Shutting down agents...")
    for t in all_tasks:
        if not t.done():
            t.cancel()
    for t in all_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Agent task raised on shutdown")

    for agent, _ in unsupervised:
        if hasattr(agent, "close"):
            try:
                await agent.close()
            except Exception:
                logger.exception("Error closing agent")

    await watchdog.close()
    activity.log("SYSTEM_STOP", "codeband", "All agents stopped")
    logger.info("All agents stopped.")


# ─── run_agent: distributed-mode single-agent entry point ──────────────────

async def run_agent(config: CodebandConfig, project_dir: Path, agent_key: str) -> None:
    """Run a single agent by key in distributed mode.

    `agent_key` is the agent_config.yaml key:
    - `conductor`, `mergemaster` — singletons
    - `{role}-{framework}-{index}` — pool workers (e.g., `coder-claude_sdk-0`)
    - `watchdog` — in-process daemon (reuses Conductor creds)
    """
    agent_config = await _ensure_agents_registered(config, project_dir)

    if agent_key == "watchdog":
        role = "watchdog"
        creds = agent_config.get("conductor")
    else:
        creds = agent_config.get(agent_key)
        role = _role_from_key(agent_key)

    resolved_config = _resolve_workspace_config(config, project_dir)
    layout = initialize_agent_workspace(resolved_config, agent_key, role)

    # Resolve memory backend per process.
    probe_client = _create_rest_client(creds.api_key, resolved_config.band.rest_url)
    memory_mode = await _install_memory_backend(
        resolved_config, Path(resolved_config.workspace.path), probe_client,
    )

    from codeband.monitoring.activity_log import ActivityLogger
    activity = ActivityLogger(layout.state_dir / "activity.jsonl")

    from codeband.monitoring.usage import SDKUsageHandler

    sdk_usage_handler = SDKUsageHandler(activity, agent_name=agent_key)
    logging.getLogger("thenvoi.adapters").addHandler(sdk_usage_handler)

    activity.log("AGENT_START", agent_key, f"Starting {agent_key} ({role})")
    logger.info("Starting agent %s (%s)", agent_key, role)

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    async def _run_until_shutdown(coro) -> None:
        task = asyncio.create_task(coro)
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            [task, shutdown_task], return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        for t in done:
            if t is not shutdown_task and not t.cancelled() and t.exception():
                exc = t.exception()
                logger.error("Agent %s crashed: %s", agent_key, exc)
                activity.log("AGENT_CRASH", agent_key, str(exc))
                raise SystemExit(1)

    async def _run_band_agent(adapter) -> None:
        agent = _create_band_agent(adapter, creds, resolved_config)
        try:
            await _run_until_shutdown(agent.run())
        finally:
            if hasattr(agent, "close"):
                await agent.close()

    if role == "conductor":
        roster = _build_worker_roster(resolved_config)
        adapter = _create_conductor(resolved_config, worker_roster=roster)
        await _run_band_agent(adapter)

    elif role == "mergemaster":
        workspace = str(layout.worktree) if layout.worktree else None
        adapter = _create_mergemaster(resolved_config, workspace)
        await _run_band_agent(adapter)

    elif role == "planner":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.worktree) if layout.worktree else None
        roster = _build_worker_roster(resolved_config)
        adapter = _create_planner(
            resolved_config, workspace=workspace,
            framework=framework, worker_roster=roster,
        )
        await _run_band_agent(adapter)

    elif role == "plan_reviewer":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.worktree) if layout.worktree else None
        adapter = _create_plan_reviewer(
            resolved_config, workspace=workspace, framework=framework,
        )
        await _run_band_agent(adapter)

    elif role == "reviewer":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.reviewer_workspace) if layout.reviewer_workspace else None
        adapter = _create_code_reviewer(
            resolved_config, workspace=workspace, framework=framework,
        )
        await _run_band_agent(adapter)

    elif role == "coder":
        framework = _framework_from_key(agent_key)
        from codeband.session.supervisor import WorkerSupervisor

        # Look up pool entry for restart settings.
        entry = resolved_config.agents.coders.entry_for(framework)

        supervisor = WorkerSupervisor(
            worker_id=agent_key,
            agent_id=creds.agent_id,
            create_agent_fn=_coder_factory(
                framework, resolved_config, layout.worktree, creds,
            ),
            state_dir=layout.state_dir,
            worktree_path=layout.worktree,
            restart_delay_seconds=entry.restart_delay_seconds,
            activity=activity,
        )
        await _run_until_shutdown(supervisor.run())

    elif role == "watchdog":
        from codeband.agents.watchdog import WatchdogDaemon
        conductor_creds = agent_config.get("conductor")
        wd_rest = _create_rest_client(conductor_creds.api_key, resolved_config.band.rest_url)
        role_map, wd_human_rest = await _build_watchdog_extras(
            agent_config, resolved_config,
        )
        watchdog = WatchdogDaemon(
            config=resolved_config.agents.watchdog,
            rest_client=wd_rest,
            agent_id=conductor_creds.agent_id,
            conductor_id=conductor_creds.agent_id,
            activity=activity,
            agent_id_to_role=role_map,
            human_rest_client=wd_human_rest,
            local_memory_store=_build_watchdog_memory_store(
                memory_mode, Path(resolved_config.workspace.path),
            ),
        )
        try:
            await _run_until_shutdown(watchdog.run())
        finally:
            await watchdog.close()

    else:
        raise ValueError(f"Unknown agent role for key: {agent_key}")

    activity.log("AGENT_STOP", agent_key, f"Agent {agent_key} stopped")
    logger.info("Agent %s stopped.", agent_key)


# ─── key parsing ────────────────────────────────────────────────────────────

def _role_from_key(key: str) -> str:
    """Map an agent_config key to its role name (for dispatch)."""
    if key in _SINGLETON_KEYS:
        return key
    # Pool keys: `{role}-{framework}-{index}` where role can contain underscores
    # (e.g. "plan_reviewer") and framework can too ("claude_sdk"). Parse from
    # the right so the trailing `-{index}` and `-{framework}` are unambiguous.
    parts = key.rsplit("-", 2)
    if len(parts) == 3:
        role = parts[0]
        if role in {"planner", "plan_reviewer", "coder", "reviewer"}:
            return role
    raise ValueError(f"Cannot derive role from agent key: {key}")


def _framework_from_key(key: str) -> Framework:
    """Map a pool-worker key to its Framework."""
    parts = key.rsplit("-", 2)
    if len(parts) != 3:
        raise ValueError(f"Key does not include a framework: {key}")
    try:
        return Framework(parts[1])
    except ValueError as exc:
        raise ValueError(f"Unknown framework in key {key}: {parts[1]}") from exc


# ─── prompt roster ──────────────────────────────────────────────────────────

def _build_worker_roster(config: CodebandConfig) -> str:
    """Build a worker-pool roster for the Planner/Conductor prompts.

    Describes available capacity in the coder and reviewer pools so the
    Planner can emit framework hints and the Conductor can route to the
    right pool. Reviewers appear as paired capacity for cross-model review.
    """
    lines = ["## Worker Pool Roster", ""]
    lines.append("| Role | Framework | Count | Description |")
    lines.append("|------|-----------|-------|-------------|")

    def _rows(role_label: str, pool: FrameworkPool) -> None:
        for fw in (Framework.CLAUDE_SDK, Framework.CODEX):
            entry: PoolEntry = pool.entry_for(fw)
            if entry.count == 0:
                continue
            desc = entry.description or ""
            lines.append(f"| {role_label} | {fw.value} | {entry.count} | {desc} |")

    _rows("Coder", config.agents.coders)
    _rows("Code Reviewer", config.agents.reviewers)
    _rows("Planner", config.agents.planners)
    _rows("Plan Reviewer", config.agents.plan_reviewers)
    return "\n".join(lines)


# ─── per-role adapter factories ─────────────────────────────────────────────

def _create_planner(
    config: CodebandConfig,
    workspace: str | None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
    worker_roster: str | None = None,
) -> "FrameworkAdapter":
    """Create a Planner adapter for the given framework."""
    entry = config.agents.planners.entry_for(framework)

    kwargs = dict(
        workspace=workspace,
        worker_roster=worker_roster,
    )
    if entry.model:
        kwargs["model"] = entry.model

    if framework == Framework.CODEX:
        from codeband.agents.planner import CodexPlannerRunner
        return CodexPlannerRunner(**kwargs).adapter

    from codeband.agents.planner import ClaudePlannerRunner
    return ClaudePlannerRunner(**kwargs).adapter


def _create_conductor(
    config: CodebandConfig,
    *,
    worker_roster: str | None = None,
) -> "FrameworkAdapter":
    """Create the conductor adapter — singleton coordinator, framework-selectable.

    The Conductor prompt references an appended Worker Pool Roster; we
    thread it in here so the LLM knows what pools + frameworks + counts
    are available when allocating coders and reviewers per task.
    """
    kwargs = dict(
        model=config.agents.conductor.model,
        worker_roster=worker_roster,
        auto_merge=config.agents.mergemaster.auto_merge.value,
    )

    if config.agents.conductor.framework == Framework.CODEX:
        from codeband.agents.conductor import CodexConductorRunner
        return CodexConductorRunner(**kwargs).adapter

    from codeband.agents.conductor import ClaudeConductorRunner
    return ClaudeConductorRunner(**kwargs).adapter


def _create_code_reviewer(
    config: CodebandConfig,
    workspace: str | None = None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
) -> "FrameworkAdapter":
    """Create a code-reviewer adapter for the given framework."""
    reviewers = config.agents.reviewers
    entry = reviewers.entry_for(framework)

    kwargs = dict(
        model=entry.model or "claude-sonnet-4-6",
        review_guidelines=reviewers.review_guidelines,
        workspace=workspace,
    )

    if framework == Framework.CODEX:
        from codeband.agents.code_reviewer import CodexCodeReviewerRunner
        return CodexCodeReviewerRunner(**kwargs).adapter

    from codeband.agents.code_reviewer import ClaudeCodeReviewerRunner
    return ClaudeCodeReviewerRunner(**kwargs).adapter


def _create_plan_reviewer(
    config: CodebandConfig,
    workspace: str | None = None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
) -> "FrameworkAdapter":
    """Create a plan-reviewer adapter for the given framework."""
    plan_reviewers = config.agents.plan_reviewers
    entry = plan_reviewers.entry_for(framework)

    kwargs = dict(
        model=entry.model or "claude-sonnet-4-6",
        review_guidelines=plan_reviewers.review_guidelines,
        workspace=workspace,
    )

    if framework == Framework.CODEX:
        from codeband.agents.plan_reviewer import CodexPlanReviewerRunner
        return CodexPlanReviewerRunner(**kwargs).adapter

    from codeband.agents.plan_reviewer import ClaudePlanReviewerRunner
    return ClaudePlanReviewerRunner(**kwargs).adapter


def _create_coder(
    framework: Framework,
    config: CodebandConfig,
    workspace: str | None,
    *,
    recovery_context: str | None = None,
) -> "FrameworkAdapter":
    """Create a coder adapter for the given framework.

    Reads `agents.coders.<framework>.model` from the config so user
    customizations in `codeband.yaml` (e.g., `coders.claude_sdk.model:
    claude-opus-4-7`) are respected at runtime. When `model` is unset on
    the pool entry, we pass None to the runner and let its default apply.
    """
    entry = config.agents.coders.entry_for(framework)

    if framework == Framework.CLAUDE_SDK:
        from codeband.agents.player_claude import ClaudePlayerRunner

        kwargs: dict = dict(
            workspace=workspace,
            recovery_context=recovery_context,
        )
        if entry.model:
            kwargs["model"] = entry.model
        runner = ClaudePlayerRunner(**kwargs)
        return runner.adapter

    if framework == Framework.CODEX:
        from codeband.agents.player_codex import CodexPlayerRunner

        kwargs = dict(
            workspace=workspace,
            recovery_context=recovery_context,
        )
        if entry.model:
            kwargs["model"] = entry.model
        runner = CodexPlayerRunner(**kwargs)
        return runner.adapter

    raise ValueError(f"Unknown framework: {framework}")


def _create_mergemaster(
    config: CodebandConfig,
    workspace: str | None,
) -> "FrameworkAdapter":
    """Create the mergemaster adapter — singleton coordinator, framework-selectable."""
    kwargs = dict(
        model=config.agents.mergemaster.model,
        workspace=workspace,
        test_command=config.agents.mergemaster.test_command,
        review_guidelines=config.agents.mergemaster.review_guidelines,
    )

    if config.agents.mergemaster.framework == Framework.CODEX:
        from codeband.agents.mergemaster import CodexMergemasterRunner
        return CodexMergemasterRunner(**kwargs).adapter

    from codeband.agents.mergemaster import ClaudeMergemasterRunner
    return ClaudeMergemasterRunner(**kwargs).adapter


# ─── coder factory (for WorkerSupervisor) ──────────────────────────────────

def _coder_factory(
    framework: Framework,
    config: CodebandConfig,
    worktree_path: Path | None,
    creds: "AgentCredentials",
):
    """Return an async callable that creates a fresh coder Agent on each restart."""
    async def create(*, recovery_context: str | None = None):
        workspace = str(worktree_path) if worktree_path else None
        adapter = _create_coder(
            framework, config, workspace,
            recovery_context=recovery_context,
        )
        return _create_band_agent(adapter, creds, config)

    return create
