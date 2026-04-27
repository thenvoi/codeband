"""Live feed — polls Band.ai API and prints agent activity in real time."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Band.ai serializes mentions inside message bodies as ``@[[<uuid>]]``.
# Resolve them to human-readable handles at display time.
_MENTION_RE = re.compile(r"@\[\[([^\[\]]+)\]\]")

# ANSI color codes for agent roles
_COLORS = {
    "conductor": "\033[36m",   # cyan
    "mergemaster": "\033[33m", # yellow
    "watchdog": "\033[35m",    # magenta
}
# Pool workers have dynamic names (coder-claude_sdk-0, reviewer-codex-1, etc.)
# — color by role prefix.
_ROLE_PREFIX_COLORS = {
    "coder": "\033[32m",           # green
    "reviewer": "\033[34m",        # blue
    "planner": "\033[96m",         # bright cyan
    "plan_reviewer": "\033[94m",   # bright blue
}
_RESET = "\033[0m"
_DIM = "\033[2m"

# Icons per message type
_ICONS = {
    "text": "",
    "tool_call": "\U0001f527",      # wrench
    "tool_result": "\u2705",        # checkmark
    "thought": "\U0001f4ad",        # thought balloon
    "error": "\u274c",              # red X
}


def _agent_color(name: str) -> str:
    """Return ANSI color for an agent name.

    Singletons (`conductor`, `mergemaster`, `watchdog`) have fixed colors.
    Pool workers are keyed by their role prefix so `coder-claude_sdk-0`,
    `coder-codex-1`, `reviewer-claude_sdk-0`, etc. all share a color by
    role. Longer prefixes (`plan_reviewer-`) must match before shorter
    substrings.
    """
    if name in _COLORS:
        return _COLORS[name]
    # Longest-prefix first so `plan_reviewer-` wins over a bare `planner` test.
    for prefix in sorted(_ROLE_PREFIX_COLORS, key=len, reverse=True):
        if name.startswith(prefix):
            return _ROLE_PREFIX_COLORS[prefix]
    return ""


class FeedFormatter:
    """Formats Band.ai messages for terminal display."""

    def __init__(
        self,
        agent_names: dict[str, str],
        *,
        show_thoughts: bool = True,
        agent_filter: str | None = None,
        type_filter: set[str] | None = None,
        verbose: bool = False,
    ):
        self._names = agent_names
        self._show_thoughts = show_thoughts
        self._agent_filter = agent_filter
        self._type_filter = type_filter
        self._verbose = verbose

    def format(self, msg: dict[str, Any]) -> str | None:
        """Format a message for terminal output. Returns None to skip."""
        sender_id = msg.get("sender_id", "")
        msg_type = msg.get("message_type", "text").lower()
        content = msg.get("content", "")
        timestamp = msg.get("inserted_at", "")

        agent_name = self._names.get(sender_id, sender_id)

        # Apply filters
        if self._agent_filter and agent_name != self._agent_filter:
            return None
        if self._type_filter and msg_type not in self._type_filter:
            return None
        if msg_type == "thought" and not self._show_thoughts:
            return None

        # Format timestamp
        time_str = ""
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp)
                time_str = dt.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                time_str = timestamp[:8]

        # Format content based on type
        icon = _ICONS.get(msg_type, "")
        display = self._format_content(msg_type, content)

        color = _agent_color(agent_name)
        name_padded = agent_name.ljust(14)

        if msg_type == "thought":
            return f"{_DIM}[{time_str}] {color}{name_padded}{_RESET}{_DIM} {icon} {display}{_RESET}"

        return f"[{time_str}] {color}{name_padded}{_RESET} {icon} {display}"

    def _resolve_mentions(self, text: str) -> str:
        """Replace ``@[[uuid]]`` tokens with ``@<display name>``.

        Unknown UUIDs fall back to an 8-char prefix so the line stays
        readable even when a participant isn't in the local name map.
        """
        return _MENTION_RE.sub(
            lambda m: f"@{self._names.get(m.group(1), m.group(1)[:8])}",
            text,
        )

    def _format_content(self, msg_type: str, content: str) -> str:
        """Extract display text from message content."""
        if msg_type == "tool_call":
            return self._format_tool_call(content)
        if msg_type == "tool_result":
            return self._format_tool_result(content)
        # text, thought, error: resolve mentions first, then truncate so we
        # don't cut a UUID mid-token and so truncation matches what the user
        # actually sees.
        content = self._resolve_mentions(content)
        if not self._verbose and len(content) > 120:
            return content[:117] + "..."
        return content

    def _format_tool_call(self, content: str) -> str:
        """Format tool call: show tool name and brief args."""
        try:
            data = json.loads(content)
            name = data.get("name", "unknown")
            args = data.get("args", {})
            if self._verbose:
                return f"{name} {json.dumps(args)}"
            # Compact: show tool name + first arg value hint
            hint = ""
            if isinstance(args, dict):
                for v in args.values():
                    hint = f" {str(v)[:60]}"
                    break
            return f"{name}{hint}"
        except (json.JSONDecodeError, TypeError):
            return content[:80]

    def _format_tool_result(self, content: str) -> str:
        """Format tool result: show brief output."""
        try:
            data = json.loads(content)
            output = data.get("output", content)
            name = data.get("name", "")
            prefix = f"{name}: " if name else ""
            text = str(output)
            if not self._verbose and len(text) > 100:
                text = text[:97] + "..."
            return f"{prefix}{text}"
        except (json.JSONDecodeError, TypeError):
            if not self._verbose and len(content) > 100:
                return content[:97] + "..."
            return content


class LiveFeed:
    """Polls Band.ai human API for new messages and prints them.

    The feed shows only messages produced *after* it was started — the
    session start timestamp is the default ``since`` for every room on
    the first poll. Historical messages live in ``cb log``; the feed is
    strictly real-time.
    """

    def __init__(
        self,
        rest_client: Any,
        formatter: FeedFormatter,
        *,
        show_history: bool = False,
    ):
        self._rest = rest_client
        self._formatter = formatter
        self._last_seen: dict[str, str] = {}
        # When False (default), prime `_last_seen` to session start so the
        # very first poll for a room only returns messages produced after
        # `cb` launched. Avoids dumping minutes/hours of stale chat history
        # into the terminal on every shell startup.
        self._session_start: str | None = (
            None if show_history else datetime.now(UTC).isoformat()
        )

    async def run(self, poll_interval: float = 2.0) -> None:
        """Poll loop: fetch new messages, format, print."""
        logger.info("Live feed started (poll every %.1fs)", poll_interval)
        try:
            while True:
                await self._poll()
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return

    async def _poll(self) -> None:
        """Single poll cycle: fetch and display new messages from all rooms."""
        try:
            response = await self._rest.human_api_chats.list_my_chats()
        except Exception:
            logger.debug("Failed to list chats", exc_info=True)
            return

        for room in (response.data or []):
            room_id = room.id
            since = self._last_seen.get(room_id, self._session_start)

            try:
                kwargs: dict[str, Any] = {"chat_id": room_id}
                if since:
                    kwargs["since"] = since
                msg_response = await self._rest.human_api_messages.list_my_chat_messages(
                    **kwargs
                )
            except Exception:
                logger.debug("Failed to fetch messages for room %s", room_id, exc_info=True)
                continue

            for msg in (msg_response.data or []):
                msg_dict = {
                    "sender_id": getattr(msg, "sender_id", ""),
                    "message_type": getattr(msg, "message_type", "text"),
                    "content": getattr(msg, "content", ""),
                    "inserted_at": str(msg.inserted_at) if msg.inserted_at else "",
                }
                line = self._formatter.format(msg_dict)
                if line is not None:
                    print(line, flush=True)

                ts = msg_dict.get("inserted_at")
                if ts:
                    current = self._last_seen.get(room_id, "")
                    if ts > current:
                        self._last_seen[room_id] = ts
