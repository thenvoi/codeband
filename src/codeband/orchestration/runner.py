"""Local runner — start pool-driven workers + coordinators in-process."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
from dataclasses import dataclass
from functools import partial
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


@dataclass
class _LocalSweepSettings:
    """Per-process inputs for the patched startup room sweep (local mode).

    ``run_local`` fills these in before agents start; the patched
    ``_subscribe_to_existing_rooms`` reads them at sweep time so every
    reconnect cycle sees the current values.
    """

    state_db_path: Path | None = None
    fresh: bool = False


_local_sweep_settings = _LocalSweepSettings()


def _agent_connection_open(agent: object) -> bool:
    """True when the agent's websocket link is verifiably still open.

    Walks the SDK teardown chain ``Agent._runtime`` (PlatformRuntime) →
    ``_link`` (ThenvoiLink) → ``_ws`` (WebSocketClient). A successful
    ``link.disconnect()`` sets ``_ws = None`` and ``_is_connected = False``
    (thenvoi/platform/link.py), so a non-None ``_ws`` or a truthy
    ``is_connected`` after stop means the connection leaked. Unknown shapes
    report closed — verification must never produce false alarms on fakes.
    """
    runtime = getattr(agent, "_runtime", None)
    link = getattr(runtime, "_link", None)
    if link is None:
        return False
    return getattr(link, "_ws", None) is not None or bool(
        getattr(link, "is_connected", False)
    )


def _read_active_room_ids(state_db_path: Path | None) -> set[str] | None:
    """Room ids of ``active`` tasks in the StateStore, or ``None`` on failure.

    ``None`` is the loud-but-connected degradation [decision (b′)]: when the
    state dir is unresolved or the store is unreadable, the sweep subscribes
    to ALL participant rooms rather than risk skipping a live task's room.
    An empty set is the normal fresh-run answer (store readable, no active
    tasks) and is NOT a failure.
    """
    if state_db_path is None:
        logger.error(
            "Subscribe-existing: state dir unresolved — subscribing to ALL "
            "participant rooms (fail toward connectivity)",
        )
        return None
    try:
        from codeband.state import StateStore

        return set(StateStore(state_db_path).list_active_task_room_ids())
    except Exception as exc:  # noqa: BLE001 - any store failure degrades the same way
        logger.error(
            "Subscribe-existing: StateStore read failed at %s (%s: %s) — "
            "subscribing to ALL participant rooms (fail toward connectivity)",
            state_db_path, type(exc).__name__, exc,
        )
        return None


async def _safe_stop_agent(agent: object, name: str = "unknown-agent") -> None:
    """Loud best-effort teardown of a Band.ai Agent between reconnect cycles.

    Why: PHXChannelsClient owns its own auto-reconnect task. Without an
    explicit stop, that task survives ``agent.run()`` returning and races
    the next cycle, producing ``PHXTopicError: already subscribed`` and
    ``cannot call recv while another coroutine is already running recv``.

    Failures log at ERROR (a silently-swallowed stop is the classic
    CLOSE_WAIT/socket-leak source) and closure is verified afterwards, but
    nothing raises — teardown is loud, never fatal.
    """
    stop = getattr(agent, "stop", None)
    if stop is None:
        return
    try:
        await stop(timeout=2.0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Teardown of agent %s failed: %s: %s",
            name, type(exc).__name__, exc, exc_info=True,
        )
    if _agent_connection_open(agent):
        logger.error(
            "Agent %s leaked its websocket connection: still open after stop()",
            name,
        )


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
    one event loop. In local mode Codeband owns signals, and the startup
    room sweep is replaced with a store-scoped serial sweep: rooms tied to
    an ``active`` task in the StateStore are rejoined (mid-task recovery —
    the SDK then drains their backlog through rehydrated context), stale
    rooms are skipped (caps the backlog-storm blast radius), and a store
    failure subscribes ALL participant rooms — fail toward connectivity,
    never toward deafness. ``cb run --fresh`` skips the sweep entirely.

    Scope: this patch exists for the IN-PROCESS fleet — competing lifecycle
    owners and shared signal handlers — and is local-mode only. Distributed
    mode (``run_agent``) intentionally runs the SDK-native reconnect and
    subscribe-existing behavior unpatched.
    """
    if os.environ.get("CODEBAND_LOCAL_SUBSCRIBE_EXISTING") is not None:
        logger.warning(
            "CODEBAND_LOCAL_SUBSCRIBE_EXISTING is deprecated and ignored — "
            "subscribe-existing is now the default; use `cb run --fresh` "
            "to opt out.",
        )
    try:
        from thenvoi.client.streaming import client as streaming_client
        from thenvoi.runtime.presence import RoomPresence
    except ImportError as exc:
        # An SDK we cannot patch must fail loud at startup: running the fleet
        # with PHX auto-reconnect enabled silently corrupts the reconnect
        # lifecycle (duplicate subscriptions, recv races). The usual cause is
        # a band-sdk version conflict — 1.0.0 renamed thenvoi.* to band.*.
        raise RuntimeError(
            "Cannot patch the Band SDK local runtime: importing its hooks "
            f"failed ({exc}). This usually means an incompatible band-sdk "
            "version is installed — Codeband requires band-sdk>=0.2.8,<0.3 "
            "(1.0.0 renamed the thenvoi.* module namespace). "
            "Reinstall with: pip install 'band-sdk[codex,claude-sdk]>=0.2.8,<0.3'"
        ) from exc

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
        if _local_sweep_settings.fresh:
            logger.info(
                "--fresh: skipping existing-room websocket subscriptions"
            )
            return

        # Store-scoped filter, applied in OUR sweep — not via the SDK's
        # RoomPresence(room_filter=...), which also gates live room_added
        # joins (thenvoi/runtime/presence.py:203) and would block the new
        # task's room. None means "subscribe everything" (decision b′:
        # fail toward connectivity, never toward deafness).
        allowed = _read_active_room_ids(_local_sweep_settings.state_db_path)

        logger.debug("Subscribing to existing rooms serially")
        try:
            rooms_to_join = await self._list_existing_rooms()
            if allowed is not None:
                kept = [
                    (room_id, payload)
                    for room_id, payload in rooms_to_join
                    if room_id in allowed
                ]
                skipped = len(rooms_to_join) - len(kept)
                if skipped:
                    logger.info(
                        "Skipping %d existing room(s) not tied to an active "
                        "task", skipped,
                    )
                rooms_to_join = kept
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


def _reset_dead_codex_client(adapter: object) -> None:
    """Reset a Codex adapter whose subprocess died BETWEEN turns (finding 22).

    The SDK only notices a dead ``codex app-server`` mid-turn: the
    ``transport/closed`` sentinel that ``_fail_pending`` enqueues
    (thenvoi/integrations/codex/stdio_client.py:119-133,
    rpc_base.py:227-245) is consumed exclusively inside
    ``_process_turn_events`` (thenvoi/adapters/codex.py:895-916), and
    ``_ensure_client_ready`` (codex.py:1011-1024) checks only
    ``_client is None`` / ``_initialized``. A between-turns death therefore
    either raises at the next ``turn/start`` or — worse — hangs forever,
    because ``BaseJsonRpcClient.request()`` has no timeout
    (rpc_base.py:199-225) and the reader loop that would resolve the future
    is gone, wedging the adapter-wide ``_rpc_lock``.

    Detection is shape-tolerant (unknown shapes do nothing): a recorded
    subprocess ``returncode`` or a queued ``transport/closed`` sentinel
    means dead. The reset mirrors what the SDK's own mid-turn handler does
    (codex.py:898-910): drop ``_client`` / ``_initialized`` /
    ``_room_threads`` so the next ``_ensure_client_ready`` rebuilds a fresh
    subprocess (thread continuity is re-established via the bootstrap
    ``thread/resume`` path where room history carries a thread id).
    """
    client = getattr(adapter, "_client", None)
    if client is None:
        return

    dead = getattr(getattr(client, "_proc", None), "returncode", None) is not None
    if not dead:
        # asyncio.Queue has no peek; inspect its internal deque read-only.
        queue = getattr(getattr(client, "_events", None), "_queue", None)
        if queue is not None:
            try:
                dead = any(
                    getattr(event, "method", None) == "transport/closed"
                    for event in list(queue)
                )
            except TypeError:
                dead = False
    if not dead:
        return

    logger.error(
        "Codex subprocess found dead between turns — resetting the adapter "
        "client state so the next turn rebuilds it",
    )
    close = getattr(client, "close", None)
    if callable(close):
        try:
            result = close()
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)
        except Exception:  # noqa: BLE001 - the old client is already dead
            logger.debug("Closing the dead Codex client failed", exc_info=True)
    adapter._client = None
    adapter._initialized = False
    room_threads = getattr(adapter, "_room_threads", None)
    if isinstance(room_threads, dict):
        room_threads.clear()


def _wrap_codex_on_message(original: Callable) -> Callable:
    """Wrap ``CodexAdapter.on_message`` with liveness + visible-error handling.

    Two seams, both finding-22 mitigations:

    * before the turn: :func:`_reset_dead_codex_client` — a dead subprocess
      is rebuilt instead of raising (or hanging) at ``turn/start``;
    * around the turn: ``turn/start`` runs BEFORE any chat output exists
      (thenvoi/adapters/codex.py:568 sits outside the try block at :629),
      so a failure there was previously invisible — the room saw nothing
      while the message was retired. A pre-output exception now posts a
      visible chat error, then re-raises so the SDK's retry accounting
      (``max_message_retries``) still applies.
    """

    async def _codeband_on_message(self, msg, tools, *args, **kwargs):
        _reset_dead_codex_client(self)
        try:
            return await original(self, msg, tools, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await tools.send_message(
                    content=(
                        "⚠️ Codex turn could not start: "
                        f"{type(exc).__name__}: {exc}. The triggering message "
                        "was NOT processed — re-send it after the Codex "
                        "session recovers."
                    ),
                )
            except Exception:  # noqa: BLE001 - the error report must not mask the error
                logger.debug(
                    "Could not post the Codex turn-failure notice", exc_info=True,
                )
            raise

    _codeband_on_message._codeband_codex_resilience = True  # type: ignore[attr-defined]
    return _codeband_on_message


def _patch_codex_adapter_resilience() -> None:
    """Wrap the Codex adapter's message entry point (finding 22 mitigations).

    ``_patch_band_local_runtime``-style, but applied in BOTH local and
    distributed modes: the dormancy defect lives in the adapter itself, not
    in the lifecycle ownership that keeps the PHX patch local-only. Codex
    extras absent (Claude-only install) → nothing to patch. Idempotent.
    """
    try:
        from thenvoi.adapters import CodexAdapter
    except ImportError:
        logger.debug("Codex adapter not importable — resilience patch skipped")
        return
    if getattr(CodexAdapter.on_message, "_codeband_codex_resilience", False):
        return
    CodexAdapter.on_message = _wrap_codex_on_message(CodexAdapter.on_message)
    logger.debug("Codex adapter resilience patch installed")


def _log_activity_safe(
    activity: object, event_type: str, name: str, summary: str,
) -> None:
    """Best-effort activity append inside a supervision loop (S6-F9).

    The reconnect-forever loop is the thing being reported on — its own
    bookkeeping must never kill it. Without this, the crash handler's
    AGENT_CRASH log line raising ``OSError`` (full disk, unwritable state
    dir) would take down the very loop it reports on.
    """
    try:
        activity.log(event_type, name, summary)
    except OSError:
        logger.warning(
            "Activity-log write (%s for %s) failed — continuing",
            event_type, name, exc_info=True,
        )


async def _run_agent_forever(
    make_agent: Callable[..., object],
    name: str,
    activity: object,
    *,
    agent_key: str | None = None,
    workspace_path: Path | str | None = None,
) -> None:
    """Run an unsupervised agent under an infinite reconnect loop.

    Each cycle builds a fresh Agent via ``make_agent(recovery_context)`` and
    tears it down in ``finally`` so the underlying PHXChannelsClient's
    reconnect/heartbeat tasks cannot leak into the next cycle. Both crashes and
    clean exits trigger another cycle after exponential backoff. The loop ends
    only when the enclosing task is cancelled by the shutdown path.

    On every (re)connect, when ``agent_key`` and ``workspace_path`` are set, we
    rebuild per-role recovery context from the durable StateStore (RFC WS5) and
    pass it into the factory so it can be prepended to the system prompt.
    Rehydration is fully guarded — any failure falls back to ``None`` and never
    breaks the reconnect loop.
    """
    attempt = 0
    while True:
        attempt += 1
        recovery_context = None
        if agent_key is not None and workspace_path is not None:
            try:
                from codeband.state.rehydration import recover_for_reconnect

                recovery_context = await recover_for_reconnect(agent_key, workspace_path)
            except Exception:
                logger.warning(
                    "Rehydration wiring failed for %s — continuing without it",
                    name, exc_info=True,
                )
                recovery_context = None
        agent = make_agent(recovery_context)
        reconnect_pending = attempt > 1
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
                _log_activity_safe(
                    activity, "AGENT_CRASH", name,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                if reconnect_pending:
                    _log_activity_safe(
                        activity, "AGENT_RECONNECTED", name,
                        f"Reconnect attempt #{attempt} — first successful turn",
                    )
                logger.warning(
                    "%s run() returned cleanly — reconnecting (attempt %d)",
                    name, attempt,
                )
                _log_activity_safe(
                    activity, "AGENT_RESTART", name,
                    f"Clean exit — reconnect #{attempt}",
                )
        finally:
            await _safe_stop_agent(agent, name)
        delay = min(
            _RECONNECT_BASE_DELAY_SECONDS * (2 ** min(attempt - 1, 5)),
            _RECONNECT_MAX_DELAY_SECONDS,
        )
        await asyncio.sleep(random.random() * delay)


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


def _watchdog_repo_slug(config: CodebandConfig) -> str | None:
    """Resolve the ``owner/repo`` slug for the watchdog's gh probes.

    From config ``repo.url`` — cwd-independent (S9-1). ``None`` for
    non-GitHub URLs: the watchdog's PR probe then degrades to the historical
    cwd-based resolution rather than blocking startup.
    """
    try:
        from codeband.github.prs import repo_slug

        return repo_slug(config.repo.url)
    except ValueError:
        return None


def _build_watchdog_state_store(workspace_path: Path) -> Any:
    """Construct the durable ``StateStore`` for the watchdog (RFC WS4).

    Points at the same SQLite file the shadow-mode store uses. Best-effort —
    returns ``None`` on any failure so the watchdog degrades to chat-recency
    behavior rather than blocking startup.
    """
    try:
        from codeband.state import StateStore

        return StateStore(workspace_path / "state" / "orchestration.db")
    except Exception:  # noqa: BLE001 - mechanical-progress path is optional
        logger.warning("Watchdog StateStore unavailable", exc_info=True)
        return None


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
        # band-sdk >=0.2.11 strips subject_id=None natively before the API
        # call (thenvoi/runtime/tools.py), so the old _patch_band_subject_id_bug
        # workaround is no longer needed.
        print(status_line)
        print("Memory: Band.ai remote API")

    # Shadow mode (RFC WS1 / Phase 1): initialise the durable state store
    # alongside the memory backend so its schema exists for later phases.
    # Record-only — its result is never used to drive orchestration, and a
    # failure here must not change observable behaviour of `cb run`, so it is
    # fully guarded. The swarm behaves identically whether or not this succeeds.
    try:
        from codeband.state import StateStore

        StateStore(Path(workspace_path) / "state" / "orchestration.db")
    except Exception:  # noqa: BLE001 - shadow mode must never break the swarm
        logger.warning("StateStore init skipped (shadow mode)", exc_info=True)

    return mode


# ─── workspace path helpers ─────────────────────────────────────────────────

def _export_project_dir_env(project_dir: Path, *, role: str | None = None) -> None:
    """Export ``CODEBAND_PROJECT_DIR`` for every session this process spawns.

    ``role`` (Stage-3 attribution): when given, also exports
    ``CODEBAND_ROLE=<role>`` on this process so every spawned session inherits
    it. This is the same seam that #46 used for ``CODEBAND_AGENT_SESSION``. It
    is only meaningful in **distributed mode** (``run_agent``), where the
    process IS a single role — local ``run_local`` runs every role in one
    process, so there is no single role to export and ``CODEBAND_ROLE`` stays
    unset (the operator-like, ungated path; cb-phase role gating treats unset
    as allowed). Like the session marker, this is an accident guard / forensic
    marker, not authentication.

    The coder/reviewer/mergemaster CLI sessions (Claude Code / Codex
    subprocesses spawned by the adapters, which already receive their ``cwd``
    from the runner) inherit this process's environment — exporting here is
    the one seam that injects the resolved project dir into every spawned
    session, local and supervised alike. ``cb-phase`` and ``cb approve``
    resolve their project dir from this variable when no explicit
    ``--project-dir`` is given (see ``cli/handoff.py:resolve_project_dir``),
    so prompts pass no new flags and agents stop depending on their cwd
    happening to be the project dir. Docker sets the same variable to
    ``/app/config`` in the compose env block.

    ``CODEBAND_AGENT_SESSION`` rides the same seam: every spawned agent
    session inherits it, and ``cb approve`` refuses to record a grant when it
    is set. This is an ACCIDENT GUARD, not authentication — it stops an agent
    from reflexively shelling out to the human-approval primitive (finding
    18); a motivated process can trivially unset it. Real identity binding is
    a design-session concern. The interactive shell's ``/approve`` runs in
    this same process (bare ``cb`` hosts the orchestrator in-process), so the
    guard exempts ``command_style="slash"`` — that path is only reachable
    from the human at the REPL prompt.
    """
    import os

    os.environ["CODEBAND_PROJECT_DIR"] = str(Path(project_dir).resolve())
    os.environ["CODEBAND_AGENT_SESSION"] = "1"
    if role is not None:
        os.environ["CODEBAND_ROLE"] = role


def _resolve_workspace_config(config: CodebandConfig, project_dir: Path) -> CodebandConfig:
    """Resolve workspace path relative to project_dir, returning updated config.

    Delegates to ``config.resolve_workspace_path`` — the one shared
    ``$WORKSPACE``-aware rule, also used by ``cb-phase``/``cb approve``,
    task registration and ``cb doctor`` — so every consumer agrees on where
    the workspace (and its ``state/`` dir) lives.
    """
    from codeband.config import resolve_workspace_path

    ws_path = Path(config.workspace.path)
    resolved = str(resolve_workspace_path(config, project_dir))
    if ws_path.is_absolute() and not ws_path.exists():
        logger.info("Creating workspace directory at %s", resolved)
    return config.model_copy(
        update={"workspace": config.workspace.model_copy(update={"path": resolved})}
    )


def _create_band_agent(adapter, creds: AgentCredentials, config: CodebandConfig):
    """Create a Band.ai Agent with standard connection args.

    The session config tunes the SDK's Phase-2 idle resync — how quickly an
    idle agent re-polls its pending queue. It is the delivery backstop for
    missed websocket pushes and applies to every role uniformly (all roles
    funnel through this factory, local and distributed alike).

    ``max_message_retries`` (finding 22 mitigation 4b) rides the same seam:
    the SDK default of 1 means a single transient turn failure permanently
    retires an @mention client-side AND pins the room's resync backstop on
    the poisoned head-of-queue. Raising it reduces how often that upstream
    defect fires; it does not eliminate it (see ``config.AgentsConfig``).
    """
    from thenvoi import Agent
    from thenvoi.runtime.types import SessionConfig

    return Agent.create(
        adapter=adapter,
        agent_id=creds.agent_id,
        api_key=creds.api_key,
        ws_url=config.band.ws_url,
        rest_url=config.band.rest_url,
        session_config=SessionConfig(
            idle_resync_seconds=config.agents.idle_resync_seconds,
            max_message_retries=config.agents.max_message_retries,
        ),
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
            # Auto-bootstrap during `cb run` — `detect_drift=False` so that
            # starting a swarm cannot rotate credentials of agents another
            # swarm (in another terminal / on another machine) is currently
            # using. Drift correction is reserved for explicit `cb setup-agents`.
            await register_all_agents(config, project_dir, detect_drift=False)
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
            # Same reasoning as above — auto-bootstrap must not rotate
            # credentials of agents that may be in use elsewhere.
            await register_all_agents(config, project_dir, detect_drift=False)
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

def _make_watchdog_done_callback(activity: Any) -> Callable[[asyncio.Task], None]:
    """Return a done-callback that surfaces unexpected watchdog task deaths loudly."""

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "Watchdog task died unexpectedly — %s: %s",
                type(exc).__name__, exc,
                exc_info=exc,
            )
            activity.log("WATCHDOG_CRASH", "watchdog", f"{type(exc).__name__}: {exc}")

    return _on_done


async def run_local(
    config: CodebandConfig,
    project_dir: Path,
    *,
    shutdown_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
    fresh: bool = False,
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

    ``fresh=True`` (``cb run --fresh``) skips rejoining existing rooms and
    their backlog at startup; the default rejoins rooms tied to active
    tasks in the StateStore (mid-task recovery).
    """

    agent_config = await _ensure_agents_registered(config, project_dir)
    resolved_config = _resolve_workspace_config(config, project_dir)
    layout = initialize_workspace(resolved_config)
    # Inputs for the patched startup sweep, read at sweep time. The store
    # path uses the same post-#36/#40 resolution every state consumer shares
    # (resolve_workspace_path via _resolve_workspace_config above).
    _local_sweep_settings.state_db_path = (
        Path(resolved_config.workspace.path) / "state" / "orchestration.db"
    )
    _local_sweep_settings.fresh = fresh
    _patch_band_local_runtime()
    _patch_codex_adapter_resilience()
    # Every agent session spawned below inherits the resolved project dir so
    # cb-phase / cb approve resolve config + state from any cwd.
    _export_project_dir_env(project_dir)

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
    #
    # ``make_adapter`` is a partial over a ``_create_*`` factory with everything
    # bound except ``recovery_context``; ``_run_agent_forever`` supplies fresh
    # per-role recovery context (RFC WS5) on every reconnect, so the adapter —
    # and the system prompt it carries — is rebuilt each cycle.
    def _band_agent_factory(make_adapter, creds):
        config = resolved_config

        def factory(recovery_context: str | None = None):
            adapter = make_adapter(recovery_context=recovery_context)
            return _create_band_agent(adapter, creds, config)

        return factory

    unsupervised: list[tuple[Callable[..., object], str]] = []

    # --- Conductor (singleton) ---
    unsupervised.append((
        _band_agent_factory(
            partial(_create_conductor, resolved_config, worker_roster=worker_roster),
            conductor_creds,
        ),
        "conductor",
    ))
    logger.info("Created Conductor agent")

    # --- Mergemaster (singleton) ---
    mm_creds = agent_config.get("mergemaster")
    mm_workspace = (
        str(layout.mergemaster_worktree) if layout.mergemaster_worktree else None
    )
    unsupervised.append((
        _band_agent_factory(
            partial(_create_mergemaster, resolved_config, mm_workspace),
            mm_creds,
        ),
        "mergemaster",
    ))
    logger.info("Created Mergemaster agent")

    # --- Planner pool ---
    for wid, _entry in _iter_pool(resolved_config.agents.planners, WorkerRole.PLANNER):
        key = str(wid)
        creds = agent_config.get(key)
        wt_path = layout.planner_worktrees.get(key)
        make_adapter = partial(
            _create_planner,
            resolved_config,
            workspace=str(wt_path) if wt_path else None,
            framework=wid.framework,
            worker_roster=worker_roster,
        )
        unsupervised.append((_band_agent_factory(make_adapter, creds), key))
        logger.info("Created %s", key)

    # --- Plan Reviewer pool ---
    for wid, _entry in _iter_pool(
        resolved_config.agents.plan_reviewers, WorkerRole.PLAN_REVIEWER,
    ):
        key = str(wid)
        creds = agent_config.get(key)
        wt_path = layout.plan_reviewer_worktrees.get(key)
        make_adapter = partial(
            _create_plan_reviewer,
            resolved_config,
            workspace=str(wt_path) if wt_path else None,
            framework=wid.framework,
        )
        unsupervised.append((_band_agent_factory(make_adapter, creds), key))
        logger.info("Created %s", key)

    # --- Reviewer pool (code reviewers) ---
    for wid, _entry in _iter_pool(resolved_config.agents.reviewers, WorkerRole.REVIEWER):
        key = str(wid)
        creds = agent_config.get(key)
        scratch_path = layout.reviewer_scratch.get(key)
        make_adapter = partial(
            _create_code_reviewer,
            resolved_config,
            workspace=str(scratch_path) if scratch_path else None,
            framework=wid.framework,
        )
        unsupervised.append((_band_agent_factory(make_adapter, creds), key))
        logger.info("Created %s", key)

    # --- Verifier pool (evidence-integrity acceptance gate) ---
    # Mirrors the reviewer pool exactly: isolated scratch dir, no supervisor.
    # INERT (no slots) when verifiers are count=0; the iterator yields nothing.
    for wid, _entry in _iter_pool(resolved_config.agents.verifiers, WorkerRole.VERIFIER):
        key = str(wid)
        creds = agent_config.get(key)
        scratch_path = layout.verifier_scratch.get(key)
        make_adapter = partial(
            _create_verifier,
            resolved_config,
            workspace=str(scratch_path) if scratch_path else None,
            framework=wid.framework,
        )
        unsupervised.append((_band_agent_factory(make_adapter, creds), key))
        logger.info("Created %s", key)

    # --- Watchdog (deterministic daemon, not a Band.ai Agent) ---
    from codeband.agents.watchdog import WatchdogDaemon

    wd_rest = _create_rest_client(conductor_creds.api_key, resolved_config.band.rest_url)
    role_map, wd_human_rest = await _build_watchdog_extras(
        agent_config, resolved_config,
    )
    # Per-agent REST clients for the transport-heal rung: keyed by agent_id,
    # each authenticated as THAT agent so the rung's
    # `list_agent_messages(status="processing")` + `mark_agent_message_processed`
    # calls act on its own delivery row. The Conductor's client only sees/heals
    # the Conductor's deliveries, so without this map the heal would be a
    # no-op for every other agent.
    agent_rest_clients: dict[str, Any] = {
        creds.agent_id: _create_rest_client(
            creds.api_key, resolved_config.band.rest_url,
        )
        for creds in agent_config.agents.values()
    }
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
        state_store=_build_watchdog_state_store(
            Path(resolved_config.workspace.path),
        ),
        # Repo context for the mechanical-progress probes (S9-1): the
        # workspace's bare clone + the config-derived slug, so git/gh probes
        # are cwd-independent.
        bare_repo=layout.bare_repo,
        repo_slug=_watchdog_repo_slug(resolved_config),
        agent_rest_clients=agent_rest_clients,
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
                worker_roster=worker_roster,
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

    workspace_path = Path(resolved_config.workspace.path)
    unsupervised_tasks = []
    for i, (make_agent, name) in enumerate(unsupervised):
        if i > 0:
            await asyncio.sleep(_STARTUP_DELAY)
        task = asyncio.create_task(
            _run_agent_forever(
                make_agent, name, activity,
                agent_key=name, workspace_path=workspace_path,
            )
        )
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
    watchdog_task.add_done_callback(_make_watchdog_done_callback(activity))
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    # Session-agent heartbeat — only when CODEBAND_SESSION_AGENT_KEY is set.
    # Refreshes the local liveness marker on a ~5-min timer, tied to this
    # process's lifecycle so the marker stays fresh as long as the orchestrator
    # is alive and goes stale when it dies.
    heartbeat_task: asyncio.Task | None = None
    session_agent_key = os.environ.get("CODEBAND_SESSION_AGENT_KEY") or None
    if session_agent_key:
        try:
            from codeband.orchestration.session_agent import start_heartbeat_loop
            from thenvoi_rest import AsyncRestClient as _ARC
            _sa_client = _ARC(
                api_key=session_agent_key, base_url=resolved_config.band.rest_url,
            )
            _sa_identity = await _sa_client.agent_api_identity.get_agent_me()
            _sa_id = _sa_identity.data.id
            _sa_name = _sa_identity.data.name
            _sa_repo = _watchdog_repo_slug(resolved_config) or "repo"
            heartbeat_task = asyncio.create_task(
                start_heartbeat_loop(_sa_id, _sa_name, _sa_repo)
            )
            task_names[heartbeat_task] = "session-heartbeat"
            logger.info("Session heartbeat started for agent %s", _sa_id)
        except Exception:
            logger.warning(
                "Could not resolve session agent identity — heartbeat skipped",
                exc_info=True,
            )

    all_tasks = unsupervised_tasks + supervisor_tasks + [watchdog_task]
    if heartbeat_task is not None:
        all_tasks.append(heartbeat_task)
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
    # Adapter-defect mitigation (finding 22), NOT a lifecycle patch: unlike
    # _patch_band_local_runtime, this applies in distributed mode too.
    _patch_codex_adapter_resilience()

    agent_config = await _ensure_agents_registered(config, project_dir)

    if agent_key == "watchdog":
        role = "watchdog"
        creds = agent_config.get("conductor")
    else:
        creds = agent_config.get(agent_key)
        role = _role_from_key(agent_key)

    resolved_config = _resolve_workspace_config(config, project_dir)
    layout = initialize_agent_workspace(resolved_config, agent_key, role)
    # Same seam as run_local: the agent session spawned below inherits the
    # resolved project dir so cb-phase / cb approve work from any cwd. In
    # Docker the compose env block already pins this to /app/config — the
    # re-export resolves to the identical path (project_dir IS that dir).
    # Distributed mode IS a single role per process, so we also export
    # CODEBAND_ROLE here (Stage-3 attribution / cb-phase role gating).
    _export_project_dir_env(project_dir, role=role)

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

    # Distributed-mode rehydration (RFC WS5): rebuild per-role recovery context
    # from the durable StateStore before building the adapter. Guarded — any
    # failure falls back to None, identical to today's blank reconnect. The
    # coder path (handled below) rehydrates from git via WorkerSupervisor.
    recovery_context: str | None = None
    if role in (
        "conductor", "mergemaster", "planner", "plan_reviewer", "reviewer", "verifier",
    ):
        from codeband.state.rehydration import recover_for_reconnect

        recovery_context = await recover_for_reconnect(
            agent_key, Path(resolved_config.workspace.path),
        )

    if role == "conductor":
        roster = _build_worker_roster(resolved_config)
        adapter = _create_conductor(
            resolved_config, worker_roster=roster, recovery_context=recovery_context,
        )
        await _run_band_agent(adapter)

    elif role == "mergemaster":
        workspace = str(layout.worktree) if layout.worktree else None
        adapter = _create_mergemaster(
            resolved_config, workspace, recovery_context=recovery_context,
        )
        await _run_band_agent(adapter)

    elif role == "planner":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.worktree) if layout.worktree else None
        roster = _build_worker_roster(resolved_config)
        adapter = _create_planner(
            resolved_config, workspace=workspace,
            framework=framework, worker_roster=roster,
            recovery_context=recovery_context,
        )
        await _run_band_agent(adapter)

    elif role == "plan_reviewer":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.worktree) if layout.worktree else None
        adapter = _create_plan_reviewer(
            resolved_config, workspace=workspace, framework=framework,
            recovery_context=recovery_context,
        )
        await _run_band_agent(adapter)

    elif role == "reviewer":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.reviewer_workspace) if layout.reviewer_workspace else None
        adapter = _create_code_reviewer(
            resolved_config, workspace=workspace, framework=framework,
            recovery_context=recovery_context,
        )
        await _run_band_agent(adapter)

    elif role == "verifier":
        framework = _framework_from_key(agent_key)
        workspace = str(layout.verifier_workspace) if layout.verifier_workspace else None
        adapter = _create_verifier(
            resolved_config, workspace=workspace, framework=framework,
            recovery_context=recovery_context,
        )
        await _run_band_agent(adapter)

    elif role == "coder":
        framework = _framework_from_key(agent_key)
        from codeband.session.supervisor import WorkerSupervisor

        # Look up pool entry for restart settings.
        entry = resolved_config.agents.coders.entry_for(framework)
        roster = _build_worker_roster(resolved_config)

        supervisor = WorkerSupervisor(
            worker_id=agent_key,
            agent_id=creds.agent_id,
            create_agent_fn=_coder_factory(
                framework, resolved_config, layout.worktree, creds,
                worker_roster=roster,
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
            state_store=_build_watchdog_state_store(
                Path(resolved_config.workspace.path),
            ),
            # Same repo context as run_local (S9-1): cwd-independent probes.
            bare_repo=layout.bare_repo,
            repo_slug=_watchdog_repo_slug(resolved_config),
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
        if role in {"planner", "plan_reviewer", "coder", "reviewer", "verifier"}:
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
    """Build a worker-pool roster for the Planner/Conductor/Coder prompts.

    Describes available capacity in the coder and reviewer pools so the
    Planner can emit framework hints and the Conductor can route to the
    right pool. Concrete display names let Coders and Planners @mention a
    deterministic opposite-framework reviewer without relying on a relay.
    """
    lines = ["## Worker Pool Roster", ""]
    lines.append("| Role | Framework | Count | Workers |")
    lines.append("|------|-----------|-------|---------|")

    display_role = {
        WorkerRole.CODER: "Coder",
        WorkerRole.REVIEWER: "Reviewer",
        WorkerRole.PLANNER: "Planner",
        WorkerRole.PLAN_REVIEWER: "Plan-Reviewer",
        WorkerRole.VERIFIER: "Verifier",
    }

    def _display_name(role: WorkerRole, fw: Framework, index: int) -> str:
        fw_label = "Claude" if fw == Framework.CLAUDE_SDK else "Codex"
        return f"{display_role[role]}-{fw_label}-{index}"

    def _rows(role: WorkerRole, role_label: str, pool: FrameworkPool) -> None:
        for fw in (Framework.CLAUDE_SDK, Framework.CODEX):
            entry: PoolEntry = pool.entry_for(fw)
            if entry.count == 0:
                continue
            workers = ", ".join(_display_name(role, fw, i) for i in range(entry.count))
            lines.append(
                f"| {role_label} | {fw.value} | {entry.count} | {workers} |",
            )

    _rows(WorkerRole.CODER, "Coder", config.agents.coders)
    _rows(WorkerRole.REVIEWER, "Code Reviewer", config.agents.reviewers)
    _rows(WorkerRole.PLANNER, "Planner", config.agents.planners)
    _rows(WorkerRole.PLAN_REVIEWER, "Plan Reviewer", config.agents.plan_reviewers)
    _rows(WorkerRole.VERIFIER, "Verifier", config.agents.verifiers)
    return "\n".join(lines)


# ─── per-role adapter factories ─────────────────────────────────────────────

def _create_planner(
    config: CodebandConfig,
    workspace: str | None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
    worker_roster: str | None = None,
    recovery_context: str | None = None,
) -> "FrameworkAdapter":
    """Create a Planner adapter for the given framework."""
    entry = config.agents.planners.entry_for(framework)

    kwargs = dict(
        workspace=workspace,
        worker_roster=worker_roster,
        recovery_context=recovery_context,
    )
    if entry.model:
        kwargs["model"] = entry.model

    if framework == Framework.CODEX:
        from codeband.agents.planner import CodexPlannerRunner
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        return CodexPlannerRunner(**kwargs).adapter

    from codeband.agents.planner import ClaudePlannerRunner
    return ClaudePlannerRunner(**kwargs).adapter


def _build_repo_pin(config: CodebandConfig) -> str | None:
    """Build the Conductor's "Configured Repository" prompt section.

    The Conductor uses this to verify every reported PR URL lands in the
    configured repo — and to close + reroute any PR that does not. Returns
    None for non-GitHub repo URLs (the verification rule does not apply).
    """
    try:
        from codeband.github.prs import repo_slug
        slug = repo_slug(config.repo.url)
    except ValueError:
        return None
    return (
        "## Configured Repository\n"
        "\n"
        f"- URL: {config.repo.url}\n"
        f"- Slug: `{slug}`\n"
        "\n"
        "**PR destination invariant.** Every PR that any agent in this swarm "
        "opens MUST land in the repo above. When a Coder reports a PR URL, "
        "you MUST run\n"
        "\n"
        f"  `gh pr view <num> --repo <pr-url-derived-slug> --json url,headRepository,headRepositoryOwner,baseRefName,state`\n"
        "\n"
        "and verify that `headRepositoryOwner.login + \"/\" + headRepository.name` "
        f"equals `{slug}`. If it does not, immediately close the wrong PR with\n"
        "\n"
        "  `gh pr close <num> --repo <wrong-owner>/<wrong-repo> --comment \"Closed by Codeband: wrong destination. Configured repo is "
        f"{slug}.\"`\n"
        "\n"
        f"and notify the originating Coder: \"PR #X targeted <wrong>; configured repo is `{slug}`. Re-open against the configured repo from the same branch.\" Do NOT route the wrong PR to the Code Reviewer or Mergemaster under any circumstances."
    )


def _create_conductor(
    config: CodebandConfig,
    *,
    worker_roster: str | None = None,
    recovery_context: str | None = None,
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
        repo_pin=_build_repo_pin(config),
        recovery_context=recovery_context,
    )

    if config.agents.conductor.framework == Framework.CODEX:
        from codeband.agents.conductor import CodexConductorRunner
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        return CodexConductorRunner(**kwargs).adapter

    from codeband.agents.conductor import ClaudeConductorRunner
    return ClaudeConductorRunner(**kwargs).adapter


def _create_code_reviewer(
    config: CodebandConfig,
    workspace: str | None = None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
    recovery_context: str | None = None,
) -> "FrameworkAdapter":
    """Create a code-reviewer adapter for the given framework."""
    reviewers = config.agents.reviewers
    entry = reviewers.entry_for(framework)

    kwargs = dict(
        model=entry.model or "claude-sonnet-4-6",
        review_guidelines=reviewers.review_guidelines,
        workspace=workspace,
        recovery_context=recovery_context,
    )

    if framework == Framework.CODEX:
        from codeband.agents.code_reviewer import CodexCodeReviewerRunner
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        return CodexCodeReviewerRunner(**kwargs).adapter

    from codeband.agents.code_reviewer import ClaudeCodeReviewerRunner
    return ClaudeCodeReviewerRunner(**kwargs).adapter


def _create_verifier(
    config: CodebandConfig,
    workspace: str | None = None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
    recovery_context: str | None = None,
) -> "FrameworkAdapter":
    """Create a verifier adapter for the given framework.

    A clean mirror of :func:`_create_code_reviewer` — the Verifier is a
    reviewer-shaped seat (isolated scratch dir + gh network) whose verdict is
    the SHA-pinned ``verify_acceptance`` acceptance gate. ``VerifiersConfig``
    carries no ``review_guidelines``, so the prompt is ``verifier.md`` verbatim.
    """
    verifiers = config.agents.verifiers
    entry = verifiers.entry_for(framework)

    kwargs = dict(
        model=entry.model or "claude-sonnet-4-6",
        workspace=workspace,
        recovery_context=recovery_context,
    )

    if framework == Framework.CODEX:
        from codeband.agents.verifier import CodexVerifierRunner
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        return CodexVerifierRunner(**kwargs).adapter

    from codeband.agents.verifier import ClaudeVerifierRunner
    return ClaudeVerifierRunner(**kwargs).adapter


def _create_plan_reviewer(
    config: CodebandConfig,
    workspace: str | None = None,
    *,
    framework: Framework = Framework.CLAUDE_SDK,
    recovery_context: str | None = None,
) -> "FrameworkAdapter":
    """Create a plan-reviewer adapter for the given framework."""
    plan_reviewers = config.agents.plan_reviewers
    entry = plan_reviewers.entry_for(framework)

    kwargs = dict(
        model=entry.model or "claude-sonnet-4-6",
        review_guidelines=plan_reviewers.review_guidelines,
        workspace=workspace,
        recovery_context=recovery_context,
    )

    if framework == Framework.CODEX:
        from codeband.agents.plan_reviewer import CodexPlanReviewerRunner
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        return CodexPlanReviewerRunner(**kwargs).adapter

    from codeband.agents.plan_reviewer import ClaudePlanReviewerRunner
    return ClaudePlanReviewerRunner(**kwargs).adapter


def _create_coder(
    framework: Framework,
    config: CodebandConfig,
    workspace: str | None,
    *,
    recovery_context: str | None = None,
    worker_roster: str | None = None,
) -> "FrameworkAdapter":
    """Create a coder adapter for the given framework.

    Reads `agents.coders.<framework>.model` from the config so user
    customizations in `codeband.yaml` (e.g., `coders.claude_sdk.model:
    claude-opus-4-7`) are respected at runtime. When `model` is unset on
    the pool entry, we pass None to the runner and let its default apply.

    `worker_roster` is the same Worker Pool Roster injected into the
    Conductor and Planner prompts (see ``_build_worker_roster``). The Coder
    needs it so that at PR completion time it can pick an opposite-framework
    Code Reviewer from the pool and @mention them directly — instead of
    routing every review through the Conductor as a relay.
    """
    entry = config.agents.coders.entry_for(framework)

    if framework == Framework.CLAUDE_SDK:
        from codeband.agents.player_claude import ClaudePlayerRunner

        kwargs: dict = dict(
            workspace=workspace,
            recovery_context=recovery_context,
            worker_roster=worker_roster,
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
            worker_roster=worker_roster,
        )
        if entry.model:
            kwargs["model"] = entry.model
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        runner = CodexPlayerRunner(**kwargs)
        return runner.adapter

    raise ValueError(f"Unknown framework: {framework}")


def _create_mergemaster(
    config: CodebandConfig,
    workspace: str | None,
    *,
    recovery_context: str | None = None,
) -> "FrameworkAdapter":
    """Create the mergemaster adapter — singleton coordinator, framework-selectable."""
    kwargs = dict(
        model=config.agents.mergemaster.model,
        workspace=workspace,
        test_command=config.agents.mergemaster.test_command,
        review_guidelines=config.agents.mergemaster.review_guidelines,
        recovery_context=recovery_context,
    )

    if config.agents.mergemaster.framework == Framework.CODEX:
        from codeband.agents.mergemaster import CodexMergemasterRunner
        kwargs["turn_timeout_seconds"] = config.agents.codex_turn_timeout_seconds
        return CodexMergemasterRunner(**kwargs).adapter

    from codeband.agents.mergemaster import ClaudeMergemasterRunner
    return ClaudeMergemasterRunner(**kwargs).adapter


# ─── coder factory (for WorkerSupervisor) ──────────────────────────────────

def _coder_factory(
    framework: Framework,
    config: CodebandConfig,
    worktree_path: Path | None,
    creds: "AgentCredentials",
    *,
    worker_roster: str | None = None,
):
    """Return an async callable that creates a fresh coder Agent on each restart."""
    async def create(*, recovery_context: str | None = None):
        workspace = str(worktree_path) if worktree_path else None
        adapter = _create_coder(
            framework, config, workspace,
            recovery_context=recovery_context,
            worker_roster=worker_roster,
        )
        return _create_band_agent(adapter, creds, config)

    return create
