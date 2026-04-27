"""Rich-based output helpers for slash command results.

Kept deliberately thin: most slash commands reuse the existing Click
handler's plain-text output (printed via :func:`println`); the helpers
below render the few shapes that are shell-native (``/diff``, ``/log``,
``/usage``).

Everything writes to a single shared :class:`rich.console.Console` so
output interleaves cleanly with prompt_toolkit's ``patch_stdout`` and the
live feed printed by :class:`codeband.monitoring.feed.LiveFeed`.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from codeband.monitoring.activity_log import EventType
from codeband.workspace.diff import WorkerDiff

_console = Console(highlight=False, soft_wrap=True)


def console() -> Console:
    """Return the shared Console instance."""
    return _console


def println(text: str = "") -> None:
    """Plain print through the Rich console (no markup interpretation)."""
    _console.print(text, markup=False, highlight=False)


def section(title: str) -> None:
    """Render a subdued section header above command output."""
    _console.print(Text(f"── {title} ─────────────", style="bold cyan"))


def render_diff(wd: WorkerDiff, *, include_patch: bool) -> None:
    """Render a WorkerDiff in the same shape as ``cb diff``."""
    section(f"diff: {wd.worker_id}")
    println(f"Worktree: {wd.worktree}")
    println(f"Base: {wd.base_ref} (fork-point {wd.merge_base[:12]})")
    println("")

    if not wd.has_changes:
        println(f"No changes yet on {wd.worker_id}.")
        return

    if include_patch:
        if wd.patch:
            println(wd.patch.rstrip("\n"))
        else:
            println("(no committed or staged changes)")
    else:
        if wd.stat:
            println(wd.stat)
        else:
            println("(no committed or staged changes)")

    if wd.untracked:
        println("")
        println("Untracked files:")
        for f in wd.untracked:
            println(f"  {f}")


def render_activity_events(events) -> None:
    """Render activity events as one row per line — same shape as ``cb log``."""
    if not events:
        println("No activity events found.")
        return
    for event in events:
        ts = event.timestamp[11:19]
        date = event.timestamp[:10]
        summary = event.summary
        if event.event_type == EventType.LLM_USAGE and event.details:
            cost = event.details.get("cost_usd", 0)
            source = event.details.get("source", "")
            duration = event.details.get("duration_ms")
            dur_str = f" {duration / 1000:.1f}s" if duration else ""
            summary = f"${cost:.4f}{dur_str} ({source})"
        println(f"{date} {ts}  {event.event_type:<18s} {event.agent:<16s} {summary}")
