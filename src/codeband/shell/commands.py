"""Slash-command registry for the interactive shell.

Each slash command is a tiny adapter that parses its args and either:
1. Delegates to the existing Click command's ``.callback`` (most cases) —
   click.echo writes go through prompt_toolkit's ``patch_stdout`` so they
   render above the prompt cleanly.
2. Goes through :class:`codeband.shell.fs.FSBackend` (``/diff``, ``/log``,
   ``/usage``) so the same operation works in local and distributed mode.

Click handlers may ``sys.exit()`` or raise ``click.ClickException``; we
catch both so the REPL stays alive and just prints the error.
"""

from __future__ import annotations

import asyncio
import shlex
import sys
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from codeband.config import CodebandConfig
from codeband.monitoring.activity_log import EventType, parse_type_filter
from codeband.shell.fs import FSBackend
from codeband.shell.render import (
    println,
    render_activity_events,
    render_diff,
    section,
)
from codeband.workspace.diff import DiffError


# ─── Context and registry types ───────────────────────────────────────────


@dataclass
class SlashContext:
    """Per-session state passed to every slash handler."""

    config: CodebandConfig
    project_dir: Path
    backend: FSBackend
    # Shutdown signal for the in-process orchestrator (local mode only).
    # ``None`` in distributed mode — /quit just breaks the prompt loop.
    shutdown_event: asyncio.Event | None
    # Compose file for /down (distributed mode only).
    compose_file: Path | None


# A slash handler returns ``"quit"`` to break the prompt loop, ``"down"`` to
# stop the docker stack and quit, or ``None`` to continue.
HandlerResult = str | None
Handler = Callable[[str, SlashContext], Awaitable[HandlerResult]]


@dataclass
class SlashCommand:
    name: str
    description: str
    handler: Handler


# Filled in below; exposed for /help and tab completion.
REGISTRY: dict[str, SlashCommand] = {}


def register(name: str, description: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = SlashCommand(name=name, description=description, handler=fn)
        return fn
    return deco


# ─── Helpers ──────────────────────────────────────────────────────────────


def parse_line(line: str) -> tuple[str, str]:
    """Split a raw input line into ``(command, args_string)``.

    Leading whitespace and the leading ``/`` are stripped. The args string
    is returned verbatim (not split) so handlers can decide their own
    parsing — ``/task`` wants free text, ``/diff`` wants flag-aware split.
    """
    stripped = line.strip()
    if not stripped.startswith("/"):
        return ("", "")
    body = stripped[1:].lstrip()
    if not body:
        return ("", "")
    head, _, rest = body.partition(" ")
    return (head, rest.strip())


def _safe_callback_call(callback: Callable, **kwargs) -> None:
    """Run a Click command callback, catching exits/exceptions."""
    try:
        callback(**kwargs)
    except click.exceptions.UsageError as e:
        println(f"Error: {e.format_message()}")
    except click.ClickException as e:
        println(f"Error: {e.format_message()}")
    except SystemExit as e:
        if e.code not in (0, None):
            println(f"(command exited with code {e.code})")
    except Exception as e:  # noqa: BLE001
        println(f"Unexpected error: {e}")
        traceback.print_exc(file=sys.stderr)


def _parse_since(value: str):
    """Parse a /log or /usage --since value. Raises ValueError with a friendly
    message on malformed input (callers catch it and println cleanly)."""
    value = value.strip()
    units = {"h": "hours", "m": "minutes", "d": "days"}
    unit = value[-1:] if value else ""
    if unit in units:
        try:
            amount = float(value[:-1])
        except ValueError:
            raise ValueError(
                f"{value!r} — expected a number before '{unit}', e.g. 1h, 30m, 2d."
            ) from None
        return datetime.now(UTC) - timedelta(**{units[unit]: amount})
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{value!r} — use a relative span (1h, 30m, 2d) or an ISO date "
            "(YYYY-MM-DD)."
        ) from None


def _parse_kv_args(args: str, known_flags: set[str]) -> tuple[list[str], dict[str, str | bool]]:
    """Split args into positional + flags. Flags may be ``--name`` or ``--name=val``."""
    tokens = shlex.split(args) if args else []
    positional: list[str] = []
    flags: dict[str, str | bool] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key, _, val = t[2:].partition("=")
            if val:
                flags[key] = val
            elif key in known_flags:
                flags[key] = True
            else:
                # Take next token as value if not another flag.
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                    flags[key] = tokens[i + 1]
                    i += 1
                else:
                    flags[key] = True
        elif t in ("-p",):
            flags["patch"] = True
        else:
            positional.append(t)
        i += 1
    return positional, flags


# ─── Handlers ─────────────────────────────────────────────────────────────


@register("task", "Send a task description to the Conductor")
async def _task(args: str, ctx: SlashContext) -> HandlerResult:
    if not args.strip():
        println("Usage: /task <description>")
        return None
    from codeband.cli import task as task_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        task_cmd.callback,
        description=args,
        project_dir=str(ctx.project_dir),
    )
    return None


@register("issue", "Send a GitHub issue to agents (e.g. /issue 42)")
async def _issue(args: str, ctx: SlashContext) -> HandlerResult:
    positional, flags = _parse_kv_args(args, known_flags={"auto"})
    if not positional or not positional[0].isdigit():
        println("Usage: /issue <number> [--auto]")
        return None
    from codeband.cli import issue as issue_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        issue_cmd.callback,
        number=int(positional[0]),
        auto=bool(flags.get("auto", False)),
        project_dir=str(ctx.project_dir),
    )
    return None


@register("issues", "Browse open GitHub issues and pick one")
async def _issues(args: str, ctx: SlashContext) -> HandlerResult:
    _, flags = _parse_kv_args(args, known_flags={"smart", "auto"})
    from codeband.cli import issues as issues_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        issues_cmd.callback,
        sort_mode=str(flags.get("sort", "newest")),
        smart=bool(flags.get("smart", False)),
        limit=int(flags.get("limit", 5)),
        pick=int(flags["pick"]) if "pick" in flags else None,
        label=flags.get("label") if isinstance(flags.get("label"), str) else None,
        auto=bool(flags.get("auto", False)),
        project_dir=str(ctx.project_dir),
    )
    return None


@register("prs", "Browse open PRs and pick one as a task")
async def _prs(args: str, ctx: SlashContext) -> HandlerResult:
    _, flags = _parse_kv_args(args, known_flags={"smart"})
    from codeband.cli import prs as prs_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        prs_cmd.callback,
        sort_mode=str(flags.get("sort", "newest")),
        smart=bool(flags.get("smart", False)),
        limit=int(flags.get("limit", 5)),
        pick=int(flags["pick"]) if "pick" in flags else None,
        project_dir=str(ctx.project_dir),
    )
    return None


@register("diff", "Show a worker's changes since fork-point: /diff <worker> [-p]")
async def _diff(args: str, ctx: SlashContext) -> HandlerResult:
    positional, flags = _parse_kv_args(args, known_flags={"patch"})
    candidates = ctx.backend.list_worktrees()
    if not candidates:
        println("No coder or mergemaster worktrees configured — run `cb init` first.")
        return None
    if not positional:
        println("Usage: /diff <worker> [-p]")
        println("Available workers:")
        for k in sorted(candidates):
            println(f"  {k}")
        return None

    typed = positional[0]
    resolved = _resolve_worker_id(typed, list(candidates.keys()))
    if resolved is None:
        return None

    include_patch = bool(flags.get("patch", False))
    try:
        wd = await asyncio.to_thread(
            ctx.backend.worktree_diff,
            resolved, ctx.config.repo.branch,
            include_patch=include_patch,
        )
    except DiffError as e:
        println(f"Error: {e}")
        return None
    render_diff(wd, include_patch=include_patch)
    return None


def _resolve_worker_id(candidate: str, keys: list[str]) -> str | None:
    lc = candidate.lower()
    for k in keys:
        if k.lower() == lc:
            return k
    hits = [k for k in keys if lc in k.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        println(f"No worker matches '{candidate}'.")
        println("Available workers:")
        for k in sorted(keys):
            println(f"  {k}")
        return None
    println(f"'{candidate}' is ambiguous — matches:")
    for k in sorted(hits):
        println(f"  {k}")
    return None


@register("status", "Query task status from Band.ai memory")
async def _status(args: str, ctx: SlashContext) -> HandlerResult:
    from codeband.orchestration.kickoff import query_status
    await query_status(ctx.config, ctx.project_dir, command_style="slash")
    return None


@register("pending", "Show open PRs with risk + merge eligibility")
async def _pending(args: str, ctx: SlashContext) -> HandlerResult:
    from codeband.cli import pending as pending_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        pending_cmd.callback,
        project_dir=str(ctx.project_dir),
        command_style="slash",
    )
    return None


@register("approve", "Approve a PR for merge: /approve <number>")
async def _approve(args: str, ctx: SlashContext) -> HandlerResult:
    positional, _ = _parse_kv_args(args, known_flags=set())
    if not positional or not positional[0].isdigit():
        println("Usage: /approve <number>")
        return None
    from codeband.cli import approve as approve_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        approve_cmd.callback,
        number=int(positional[0]),
        project_dir=str(ctx.project_dir),
        command_style="slash",
    )
    return None


@register("reject", "Reject a PR: /reject <number> [--reason \"...\"]")
async def _reject(args: str, ctx: SlashContext) -> HandlerResult:
    positional, flags = _parse_kv_args(args, known_flags=set())
    if not positional or not positional[0].isdigit():
        println("Usage: /reject <number> [--reason \"...\"]")
        return None
    from codeband.cli import reject as reject_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        reject_cmd.callback,
        number=int(positional[0]),
        reason=flags.get("reason") if isinstance(flags.get("reason"), str) else None,
        project_dir=str(ctx.project_dir),
        command_style="slash",
    )
    return None


@register("log", "View activity history: /log [--agent x] [--type A,B] [--since 1h] [--all]")
async def _log(args: str, ctx: SlashContext) -> HandlerResult:
    _, flags = _parse_kv_args(args, known_flags={"all"})
    agent = flags.get("agent") if isinstance(flags.get("agent"), str) else None
    event_type = flags.get("type") if isinstance(flags.get("type"), str) else None
    type_filter = parse_type_filter(event_type)
    since_str = flags.get("since") if isinstance(flags.get("since"), str) else None
    try:
        since_dt = _parse_since(since_str) if since_str else None
    except ValueError as e:
        println(f"Error: {e}")
        return None
    show_all = bool(flags.get("all", False))

    events = await asyncio.to_thread(
        ctx.backend.read_activity_events,
        agent=agent, since=since_dt,
    )
    if type_filter is not None:
        events = [e for e in events if e.event_type in type_filter]
    elif not show_all:
        events = [e for e in events if e.event_type != EventType.LLM_USAGE]
    render_activity_events(events)
    return None


@register("usage", "Token + cost summary: /usage [--agent x] [--since 1h]")
async def _usage(args: str, ctx: SlashContext) -> HandlerResult:
    _, flags = _parse_kv_args(args, known_flags=set())
    agent = flags.get("agent") if isinstance(flags.get("agent"), str) else None
    since_str = flags.get("since") if isinstance(flags.get("since"), str) else None
    try:
        since_dt = _parse_since(since_str) if since_str else None
    except ValueError as e:
        println(f"Error: {e}")
        return None

    events = await asyncio.to_thread(
        ctx.backend.read_activity_events,
        agent=agent, event_type=EventType.LLM_USAGE, since=since_dt,
    )
    if not events:
        println("No LLM usage recorded yet.")
        return None

    total_cost = 0.0
    total_in = 0
    total_out = 0
    by_agent: dict[str, dict] = {}
    for e in events:
        d = e.details or {}
        cost = float(d.get("cost_usd", 0))
        ti = int(d.get("input_tokens", 0))
        to = int(d.get("output_tokens", 0))
        total_cost += cost
        total_in += ti
        total_out += to
        a = by_agent.setdefault(e.agent, {"cost": 0.0, "in": 0, "out": 0, "calls": 0})
        a["cost"] += cost
        a["in"] += ti
        a["out"] += to
        a["calls"] += 1

    section("USAGE")
    println(f"  Total cost:          ${total_cost:.4f}")
    if total_in or total_out:
        println(f"  Total input tokens:  {total_in:,}")
        println(f"  Total output tokens: {total_out:,}")
    println(f"  LLM calls:           {len(events)}")
    if by_agent:
        println("")
        println("  Per agent:")
        for name, a in sorted(by_agent.items()):
            tokens = ""
            if a["in"] or a["out"]:
                tokens = f"  ({a['in']:,} in / {a['out']:,} out)"
            println(f"    {name:<20s} ${a['cost']:.4f}  ({a['calls']} calls){tokens}")
    return None


@register("scale", "Scale a worker pool: /scale coders.claude_sdk=4")
async def _scale(args: str, ctx: SlashContext) -> HandlerResult:
    if not args.strip():
        println("Usage: /scale <pool>.<framework>=<count>")
        return None
    from codeband.cli import scale as scale_cmd
    await asyncio.to_thread(
        _safe_callback_call,
        scale_cmd.callback,
        spec=args.strip(),
        project_dir=str(ctx.project_dir),
        command_style="slash",
    )
    # Apply hint depends on what's actually running, not what the yaml
    # says — same reasoning as /down: cb up may have attached this shell
    # to a docker stack while leaving workspace.mode at the local default.
    if ctx.compose_file is not None:
        println("Next:")
        println("  cb setup-agents                       # register any new pool identities")
        println("  docker compose up -d --build          # rebuild + restart containers")
    else:
        println("Next:")
        println("  cb setup-agents                       # register any new pool identities")
        println("  /quit and re-run `cb`                 # restart the shell to apply")
    return None


@register("doctor", "Check environment, config, and connectivity")
async def _doctor(args: str, ctx: SlashContext) -> HandlerResult:
    from codeband.cli import doctor as doctor_cmd
    await asyncio.to_thread(
        _safe_callback_call, doctor_cmd.callback, project_dir=str(ctx.project_dir),
    )
    return None


@register("down", "Stop the docker stack and exit (only when attached to one)")
async def _down(args: str, ctx: SlashContext) -> HandlerResult:
    # Gate on the runtime signal — "do we have a compose file pointing at
    # a stack we can stop?" — not on workspace.mode. cb up forces attach
    # mode while leaving workspace.mode at its default 'local', so a
    # mode-based check would refuse legitimate /down requests there.
    if ctx.compose_file is None:
        println("/down has nothing to stop — no docker stack is attached. "
                "Use /quit to exit the shell.")
        return None
    return "down"


@register("help", "List slash commands")
async def _help(args: str, ctx: SlashContext) -> HandlerResult:
    section("slash commands")
    width = max(len(name) for name in REGISTRY)
    for name in sorted(REGISTRY):
        cmd = REGISTRY[name]
        println(f"  /{name:<{width}}  {cmd.description}")
    println("")
    println("  Ctrl-D / Ctrl-C also quit.")
    return None


@register("quit", "Exit the shell (and stop in-process agents in local mode)")
async def _quit(args: str, ctx: SlashContext) -> HandlerResult:
    return "quit"


# ─── Public dispatch ──────────────────────────────────────────────────────


async def dispatch(line: str, ctx: SlashContext) -> HandlerResult:
    """Parse and run one slash-command line. Returns ``"quit"``/``"down"`` or ``None``."""
    name, args = parse_line(line)
    if not name:
        return None
    cmd = REGISTRY.get(name)
    if cmd is None:
        println(f"Unknown command: /{name}. Type /help for the list.")
        return None
    return await cmd.handler(args, ctx)
