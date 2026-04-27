"""Token usage tracking for Codeband agents.

All LLM calls now go through Claude Code SDK / Codex CLI, which emit cost
information in their own logs. ``SDKUsageHandler`` parses those log lines
into ``LLM_USAGE`` activity events; ``UsageSummary`` aggregates them.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from codeband.monitoring.activity_log import EventType

if TYPE_CHECKING:
    from codeband.monitoring.activity_log import ActivityLogger, ActivityReader

# Pattern: "Room <room_id>: Complete - <ms>ms, $<cost>"
_SDK_COMPLETE_RE = re.compile(
    r"Room (.+?): Complete - (\d+)ms, \$([0-9]+\.[0-9]+)"
)


class AgentTaskFilter(logging.Filter):
    """Logging filter that tags records with the agent name of the current asyncio task."""

    def __init__(self) -> None:
        super().__init__()
        self._task_to_agent: dict[int, str] = {}

    def register(self, task, agent_name: str) -> None:
        """Register an asyncio task → agent name mapping."""
        self._task_to_agent[id(task)] = agent_name

    def filter(self, record: logging.LogRecord) -> bool:
        import asyncio
        try:
            task = asyncio.current_task()
            if task and id(task) in self._task_to_agent:
                record.codeband_agent = self._task_to_agent[id(task)]  # type: ignore[attr-defined]
        except RuntimeError:
            pass  # No running event loop
        return True


class SDKUsageHandler(logging.Handler):
    """Logging handler that captures Claude SDK completion logs as usage events."""

    def __init__(self, activity: ActivityLogger, agent_name: str | None = None) -> None:
        super().__init__()
        self._activity = activity
        self._agent_name = agent_name

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
            m = _SDK_COMPLETE_RE.search(msg)
            if not m:
                return
            room_id = m.group(1)
            duration_ms = int(m.group(2))
            cost_usd = float(m.group(3))
            agent = (
                getattr(record, "codeband_agent", None)
                or self._agent_name
                or room_id
            )
            self._activity.log(
                EventType.LLM_USAGE,
                agent,
                "sdk_completion",
                room_id=room_id,
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                source="claude_sdk",
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "SDKUsageHandler.emit failed", exc_info=True,
            )


@dataclasses.dataclass
class UsageSummary:
    """Aggregated usage statistics from activity log events."""

    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    call_count: int = 0
    by_agent: dict[str, UsageSummary] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_activity_reader(
        cls,
        reader: ActivityReader,
        *,
        agent: str | None = None,
        since: datetime | None = None,
    ) -> UsageSummary:
        """Build a summary from LLM_USAGE events in the activity log."""
        events = reader.read(event_type=EventType.LLM_USAGE, agent=agent, since=since)
        summary = cls()
        for event in events:
            details = event.details or {}
            cost = details.get("cost_usd", 0.0)
            inp = details.get("input_tokens", 0)
            out = details.get("output_tokens", 0)
            summary.total_cost_usd += cost
            summary.total_input_tokens += inp
            summary.total_output_tokens += out
            summary.call_count += 1
            if event.agent not in summary.by_agent:
                summary.by_agent[event.agent] = cls()
            agent_summary = summary.by_agent[event.agent]
            agent_summary.total_cost_usd += cost
            agent_summary.total_input_tokens += inp
            agent_summary.total_output_tokens += out
            agent_summary.call_count += 1
        return summary
