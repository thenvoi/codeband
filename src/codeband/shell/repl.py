"""Codeband interactive shell — main entry point.

Bare ``cb`` (no subcommand, TTY) calls :func:`start`. Three concurrent
asyncio tasks share one event loop:

1. **Orchestrator** (local mode only) — runs the agent fleet in-process.
2. **Live feed** — polls Band.ai for chat messages and prints them.
3. **REPL** — ``prompt_toolkit`` async prompt loop, dispatching slash
   commands.

``patch_stdout`` ensures any ``print(...)`` (from the feed or from agent
debug logging) renders *above* the prompt without disturbing the input
line. The prompt itself is rendered by ``prompt_toolkit``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding.bindings.named_commands import get_by_name
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from codeband import __version__
from codeband.config import CodebandConfig, load_config
from codeband.shell.commands import REGISTRY, SlashContext, dispatch
from codeband.shell.fs import make_backend
from codeband.shell.render import println, section


logger = logging.getLogger(__name__)


# ─── Public entry point ────────────────────────────────────────────────────


def run(
    project_dir: Path | str = ".",
    *,
    attach: bool = False,
    skip_preflight: bool = False,
) -> None:
    """Synchronous entry point used from the Click ``cb`` group."""
    project = Path(project_dir).resolve()
    config = load_config(project)
    asyncio.run(start(
        config, project, attach=attach, skip_preflight=skip_preflight,
    ))


async def start(
    config: CodebandConfig,
    project_dir: Path,
    *,
    attach: bool = False,
    skip_preflight: bool = False,
) -> None:
    """Async entry point — wires orchestrator + feed + REPL by mode.

    When ``attach`` is True, the shell does NOT start an in-process
    orchestrator: agents are assumed to already be running elsewhere
    (Docker, remote hosts). Set automatically by ``cb up`` via the
    ``CODEBAND_SHELL_ATTACH=1`` env var; can also be requested with the
    ``cb --attach`` flag.
    """
    api_key = os.environ.get("BAND_API_KEY")
    if not api_key:
        println("Error: BAND_API_KEY not set. Add it to .env or your shell environment.")
        return

    # The Band.ai SDK's link layer logs a verbose warning (full HTTP headers
    # and body) every time it can't mark a message as processed — typically
    # because the room is gone. The user already sees a clear "Run 'cb reset'"
    # hint; the headers dump is pure noise in an interactive shell.
    logging.getLogger("thenvoi.platform.link").setLevel(logging.ERROR)

    # Diagnostic: ``CODEBAND_DEBUG=phx`` enables protocol-level logging
    # for the Phoenix Channels client and the websockets library so we
    # can see why a WS drops (server close, heartbeat failure, etc.).
    # Logs go to a FileHandler at ``$CODEBAND_DEBUG_FILE`` (default
    # ``/tmp/cb-phx-debug.log``) so the interactive prompt isn't drowned
    # and the user doesn't need ``tee`` (which breaks the TTY check).
    if os.environ.get("CODEBAND_DEBUG") == "phx":
        debug_path = os.environ.get("CODEBAND_DEBUG_FILE", "/tmp/cb-phx-debug.log")
        fmt = logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        file_handler = logging.FileHandler(debug_path, mode="w")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        # Stderr handler at WARNING so the user sees flap events live in the
        # terminal, while the file captures full DEBUG-level protocol detail.
        import sys as _sys
        stream_handler = logging.StreamHandler(_sys.stderr)
        stream_handler.setLevel(logging.WARNING)
        stream_handler.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
        root.addHandler(stream_handler)
        for noisy in (
            "asyncio", "urllib3", "httpx", "httpcore", "mcp",
            "thenvoi.platform.link", "thenvoi.adapters",
        ):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        for verbose in (
            "phoenix_channels_python_client",
            "websockets.client",
            "websockets.protocol",
        ):
            logging.getLogger(verbose).setLevel(logging.DEBUG)
        println(f"[debug] Phoenix/websockets DEBUG → {debug_path}")

    # Standalone vs attach is the runtime decision: standalone owns an
    # in-process orchestrator, attach is a thin client. This is *not*
    # the same as workspace.mode (config). attach is set explicitly by
    # ``cb up`` (env var) or ``cb --attach``.
    standalone = not attach

    if standalone and not skip_preflight:
        if not await _run_preflight(config):
            return

    try:
        backend = make_backend(config, project_dir, attach=attach)
    except FileNotFoundError as e:
        # ComposeFileNotFound when attach mode can't find a compose file.
        println(f"Error: {e}")
        return
    from codeband.shell.fs import SharedComposeBackend
    compose_file = (
        backend.compose_file if isinstance(backend, SharedComposeBackend) else None
    )

    # Two events with distinct concerns:
    # - shell_exit_event: set when the prompt loop should return (Ctrl-C
    #   at the OS level, orchestrator dies, /quit, /down). Always present.
    # - shutdown_event:   set when run_local should drain agents.
    #   Standalone mode only — in attach mode we don't own any agents.
    shell_exit_event = asyncio.Event()
    shutdown_event: asyncio.Event | None = None
    orchestrator_task: asyncio.Task | None = None

    if standalone:
        shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_quiet_loop_exception_handler)

    def _on_signal() -> None:
        shell_exit_event.set()
        if shutdown_event is not None:
            shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows lacks add_signal_handler; KeyboardInterrupt during
            # prompt_async still works there.
            pass

    # Build the prompt session up front so background callbacks can call
    # ``session.app.exit()`` to break ``prompt_async`` early.
    ctx = SlashContext(
        config=config,
        project_dir=project_dir,
        backend=backend,
        shutdown_event=shutdown_event,
        compose_file=compose_file,
    )
    session: PromptSession = PromptSession(
        completer=_build_completer(),
        bottom_toolbar=_bottom_toolbar(ctx),
        complete_while_typing=True,
        style=_PROMPT_STYLE,
        key_bindings=_PROMPT_KB,
    )

    ready_event: asyncio.Event | None = None
    if standalone:
        from codeband.orchestration.runner import run_local

        section(f"Codeband v{__version__} — local mode")
        println("Starting agents in-process… (this may take a few seconds)")
        ready_event = asyncio.Event()
        orchestrator_task = asyncio.create_task(
            run_local(
                config, project_dir,
                shutdown_event=shutdown_event,
                ready_event=ready_event,
            ),
            name="orchestrator",
        )
        orchestrator_task.add_done_callback(
            _orchestrator_done_callback(session, shell_exit_event),
        )
    else:
        section(f"Codeband v{__version__} — docker mode")
        # Local mode gets its agent list from the running orchestrator's
        # banner. Docker mode never sees that banner (it's container
        # stdout, not user terminal), so surface the configured roster
        # here from agent_config.yaml — the shell's only authoritative
        # source for what *should* be running.
        _print_attached_roster(project_dir)
        println("Run /down to stop the Docker stack and exit.")

    # Live feed — same in both modes (always polls Band.ai).
    feed_task, rest_client = await _start_feed(config, project_dir, api_key)
    feed_task.add_done_callback(_log_task_failure)

    # Print the readiness hint once the orchestrator has finished booting
    # (standalone) or immediately (attached). Runs as a background task so
    # the prompt loop can start in parallel — agents finish coming up
    # while the user is typing.
    announce_task = asyncio.create_task(
        _announce_ready(ready_event), name="announce-ready",
    )

    try:
        await _prompt_loop(ctx, session, shell_exit_event)
    finally:
        # Order matters: stop the feed first so it doesn't print over
        # the shutdown banner; then wait for the orchestrator to drain.
        if not feed_task.done():
            feed_task.cancel()
            try:
                await feed_task
            except asyncio.CancelledError:
                pass

        # Cancel the readiness-announcement task if the user quit before
        # it printed (orchestrator hadn't signalled ready yet). Avoids
        # leaving an orphan task running its 120s timeout sleep.
        if not announce_task.done():
            announce_task.cancel()
            try:
                await announce_task
            except asyncio.CancelledError:
                pass

        await _close_rest_client(rest_client)

        if orchestrator_task is not None and shutdown_event is not None:
            shutdown_event.set()
            if not orchestrator_task.done():
                println("Stopping agents…")
            # Intentional shutdown often closes sockets before Phoenix can
            # send/ack leave messages. Those warnings are useful during
            # runtime, but just noise after /quit.
            logging.getLogger("phoenix_channels_python_client.client").setLevel(
                logging.ERROR
            )
            try:
                await asyncio.wait_for(orchestrator_task, timeout=30)
            except asyncio.TimeoutError:
                println("Orchestrator did not stop within 30s — cancelling.")
                orchestrator_task.cancel()
                try:
                    await orchestrator_task
                except asyncio.CancelledError:
                    pass
            except Exception as e:  # noqa: BLE001
                println(f"Orchestrator stopped with error: {e}")


# ─── Banner helpers ────────────────────────────────────────────────────────


def _print_attached_roster(project_dir: Path) -> None:
    """Print configured agents in attach mode (container stdout is hidden)."""
    from codeband.config import load_agent_config

    try:
        agent_config = load_agent_config(project_dir)
    except FileNotFoundError:
        println("Agents: (no agent_config.yaml — run `cb setup-agents`)")
        return
    except Exception as e:  # noqa: BLE001
        println(f"Agents: (could not load agent_config.yaml — {type(e).__name__}: {e})")
        return

    keys = list(agent_config.agents.keys())
    # Watchdog runs in its own container alongside the LLM agents; surface
    # it for parity with the standalone banner.
    keys_with_watchdog = [*keys, "watchdog"] if "watchdog" not in keys else keys
    println(f"Agents ({len(keys_with_watchdog)}): {', '.join(keys_with_watchdog)}")


async def _announce_ready(ready_event: asyncio.Event | None) -> None:
    """Print the "Ready; use /help…" hint once the orchestrator is up.

    In attached mode there's no orchestrator to wait on (``ready_event``
    is None), so the hint prints immediately. In standalone mode this
    waits on the runner's ready signal — bounded so a stuck startup
    doesn't hide the hint forever.
    """
    if ready_event is not None:
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            logger.warning("Orchestrator did not signal ready within 120s")
    println("")
    println("Ready; use /help to see all commands")


# ─── Preflight ─────────────────────────────────────────────────────────────


async def _run_preflight(config: CodebandConfig) -> bool:
    """Validate Claude (and optionally Codex) auth before starting agents.

    Mirrors the preflight ``cb run`` does (``cli.py``): we'd rather fail
    fast with a remediation hint than spawn agents that immediately die
    on missing credentials and confuse the user with a silent feed.
    Returns True on success, False (and prints) on failure.
    """
    from codeband.preflight import run_preflight

    err = await run_preflight(config)
    if err is not None:
        println(f"Error: {err.summary}")
        println("")
        println(err.remediation)
        return False
    return True


# ─── Live feed wiring ──────────────────────────────────────────────────────


async def _start_feed(
    config: CodebandConfig,
    project_dir: Path,
    api_key: str,
) -> tuple[asyncio.Task, "object"]:
    """Build a LiveFeed task using the same shape as ``cb feed``.

    Returns ``(task, rest_client)`` — the caller is responsible for
    closing the rest client on shutdown so its underlying httpx session
    doesn't leak.
    """
    from codeband.config import load_agent_config
    from codeband.monitoring.feed import FeedFormatter, LiveFeed
    from thenvoi.client.rest import AsyncRestClient

    try:
        agent_config = load_agent_config(project_dir)
        agent_names = {v.agent_id: k for k, v in agent_config.agents.items()}
    except FileNotFoundError:
        # Project not set up yet (no agent_config.yaml). Empty name
        # mapping is fine — the feed will show raw IDs.
        agent_names = {}
    except Exception as e:  # noqa: BLE001
        # Malformed yaml or schema mismatch — don't kill the shell, but
        # don't pretend everything's fine either: the user will see UUIDs
        # in the feed and needs to know why.
        println(
            f"Warning: agent_config.yaml could not be loaded ({type(e).__name__}: {e}). "
            "Feed will show raw agent IDs instead of friendly names."
        )
        logger.warning("Failed to load agent_config", exc_info=e)
        agent_names = {}

    rest = AsyncRestClient(api_key=api_key, base_url=config.band.rest_url)

    # Resolve the human user's identity once so the feed can display a
    # friendly name instead of their Band.ai UUID. Best-effort — failure
    # here just means messages from the human render with a raw UUID,
    # which is the previous behaviour.
    try:
        profile = await rest.human_api_profile.get_my_profile()
        human_id = getattr(profile.data, "id", None)
        human_name = (
            getattr(profile.data, "name", None)
            or getattr(profile.data, "handle", None)
            or "you"
        )
        if human_id:
            agent_names[human_id] = human_name
    except Exception:  # noqa: BLE001
        logger.debug("Could not resolve human profile for feed display", exc_info=True)

    formatter = FeedFormatter(
        agent_names,
        show_thoughts=False,  # default: keep the feed terse
        agent_filter=None,
        type_filter=None,
        verbose=False,
    )
    live_feed = LiveFeed(rest, formatter)
    task = asyncio.create_task(live_feed.run(), name="feed")
    return task, rest


# ─── Prompt loop ───────────────────────────────────────────────────────────


def _build_completer() -> WordCompleter:
    return WordCompleter(
        [f"/{name}" for name in REGISTRY],
        ignore_case=True,
        sentence=True,
    )


_PROMPT_STYLE = Style.from_dict({
    "completion-menu.completion":         "bg:#1f1f1f #d4d4d4",
    "completion-menu.completion.current": "bg:#005f87 #ffffff bold",
    "completion-menu.meta.completion":         "bg:#1f1f1f #888888",
    "completion-menu.meta.completion.current": "bg:#005f87 #ffffff",
    "scrollbar.background": "bg:#2a2a2a",
    "scrollbar.button":     "bg:#666666",
})


_PROMPT_KB = KeyBindings()
_DEFAULT_BACKSPACE = get_by_name("backward-delete-char").handler


@_PROMPT_KB.add("backspace")
def _(event):
    buf = event.current_buffer
    before = (buf.text, buf.cursor_position)
    _DEFAULT_BACKSPACE(event)
    after = (buf.text, buf.cursor_position)
    if after != before and buf.document.text_before_cursor.startswith("/"):
        buf.start_completion(select_first=False)


def _quiet_loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Log loop-level exceptions instead of pausing the terminal.

    prompt_toolkit's default handler prints the traceback and waits on
    ``Press ENTER to continue...`` — fine for a TUI app running alone, but
    actively harmful when our agents are throwing transient asyncio noise
    (cancelled tasks, websocket teardown). We log instead so the diagnostic
    feed and ``cb log`` keep flowing without user intervention.
    """
    exc = context.get("exception")
    msg = context.get("message", "Unhandled exception in event loop")
    if exc is not None:
        logger.warning("%s: %s", msg, exc, exc_info=exc)
    else:
        logger.debug("asyncio loop hint: %s", context)


def _bottom_toolbar(ctx: SlashContext):
    def _render():
        mode = ctx.config.workspace.mode.value.upper()
        n = ctx.config.agents.total_agent_count()
        return HTML(
            f"<b>{mode}</b> · {n} agents · /help for commands · Ctrl-D to quit"
        )
    return _render


async def _prompt_loop(
    ctx: SlashContext,
    session: PromptSession,
    shell_exit_event: asyncio.Event,
) -> None:
    with patch_stdout(raw=True):
        while True:
            if shell_exit_event.is_set():
                return

            try:
                line = await session.prompt_async(
                    "> ", set_exception_handler=False,
                )
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D / Ctrl-C inside the prompt → graceful exit.
                println("")
                return

            # ``session.app.exit()`` from a background callback returns
            # ``None`` from prompt_async — treat that as a clean exit.
            if line is None:
                return

            line = line.strip()
            if not line:
                # Re-check exit flag on idle returns too.
                if shell_exit_event.is_set():
                    return
                continue

            if not line.startswith("/"):
                println("Slash commands only here. Type /help for the list, or "
                        "/task <description> to send work.")
                continue

            try:
                result = await dispatch(line, ctx)
            except Exception as e:  # noqa: BLE001
                println(f"Command crashed: {e}")
                logger.exception("Slash command crashed")
                continue

            if result == "quit":
                shell_exit_event.set()
                return
            if result == "down":
                shell_exit_event.set()
                await _docker_down(ctx)
                return

            # SIGINT may have fired during dispatch — break out so we
            # don't keep accepting input after the user asked to quit.
            if shell_exit_event.is_set():
                println("Shutdown requested — exiting shell.")
                return


def _log_task_failure(task: asyncio.Task) -> None:
    """Surface a crashed background task — silent failures hurt the most."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    println(f"[{task.get_name()}] crashed: {type(exc).__name__}: {exc}")
    logger.error("Background task '%s' crashed", task.get_name(), exc_info=exc)


def _orchestrator_done_callback(
    session: PromptSession,
    shell_exit_event: asyncio.Event,
):
    """Build a done-callback that ends the shell when the orchestrator dies.

    Triggers in three cases:
    - Crash: print the exception and trip ``shell_exit_event``.
    - Clean exit while user hasn't asked to quit: warn and trip exit.
    - Clean exit during user-initiated shutdown: silent (the prompt loop
      is already on its way out).
    """
    def _on_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            println(f"[orchestrator] crashed: {type(exc).__name__}: {exc}")
            logger.error("Orchestrator crashed", exc_info=exc)
        elif not shell_exit_event.is_set():
            println("[orchestrator] exited unexpectedly — shutting down shell.")
        else:
            return

        shell_exit_event.set()
        # Break out of any in-progress ``prompt_async`` call.
        if session.app.is_running:
            try:
                session.app.exit()
            except Exception:  # noqa: BLE001
                logger.debug("session.app.exit() raised", exc_info=True)
    return _on_done


async def _close_rest_client(rest_client: object) -> None:
    """Close the Band.ai REST client at shell shutdown.

    The SDK's ``AsyncRestClient`` wraps an httpx session; without an
    explicit close the underlying connection pool warns at GC time.
    The exact close-coroutine name varies across SDK versions — try the
    common candidates and swallow if none exist.
    """
    for attr in ("aclose", "close"):
        closer = getattr(rest_client, attr, None)
        if closer is None:
            continue
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
            return
        except Exception:  # noqa: BLE001
            logger.debug("rest_client.%s() raised at shutdown", attr, exc_info=True)
            return


async def _docker_down(ctx: SlashContext) -> None:
    """``/down`` — stop the docker stack via the centralized compose helper."""
    if ctx.compose_file is None:
        println("No docker compose file resolved — cannot run /down.")
        return
    println("Running `docker compose down`…")
    from codeband.orchestration.compose import compose_run_async
    await compose_run_async(
        ctx.project_dir, ctx.compose_file, ["down", "--remove-orphans"],
    )
