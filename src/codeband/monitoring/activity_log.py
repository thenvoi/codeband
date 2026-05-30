"""Persistent JSONL activity log — append-only structured event history."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


class EventType:
    """Well-known activity ``event_type`` values."""

    LLM_USAGE: Final[str] = "LLM_USAGE"


def parse_type_filter(value: str | None) -> set[str] | None:
    """Parse a comma-separated ``--type`` value into a set of event types.

    Shared by ``cb log`` / ``/log`` and ``cb feed`` so the flag behaves
    identically: ``--type NUDGE,ERROR`` filters to either type. Whitespace
    around each token is stripped; an empty or all-blank value yields
    ``None`` (no filter).
    """
    if not value:
        return None
    tokens = {t.strip() for t in value.split(",") if t.strip()}
    return tokens or None


@dataclasses.dataclass
class ActivityEvent:
    """A single activity event."""

    timestamp: str
    event_type: str
    agent: str
    summary: str
    details: dict | None = None


class ActivityLogger:
    """Append-only JSONL logger for codeband activity events."""

    def __init__(self, log_path: Path):
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, agent: str, summary: str, **details) -> None:
        """Append an event to the activity log."""
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "agent": agent,
            "summary": summary,
            "details": details if details else None,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")


class ActivityReader:
    """Read and filter the activity log.

    Accepts either a path on the host filesystem (default) or a callable
    that returns the raw JSONL text — the latter lets distributed-mode
    callers fetch the log via ``docker compose exec`` without changing
    the parsing/filtering logic.
    """

    def __init__(
        self,
        log_path: Path | None = None,
        *,
        text_loader: Callable[[], str] | None = None,
    ):
        if (log_path is None) == (text_loader is None):
            raise ValueError("Provide exactly one of log_path or text_loader")
        self._path = log_path
        self._text_loader = text_loader

    def _load_text(self) -> str:
        if self._text_loader is not None:
            return self._text_loader()
        if self._path is None:
            raise RuntimeError("ActivityReader has neither log_path nor text_loader")
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")

    def read(
        self,
        *,
        agent: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[ActivityEvent]:
        """Read events, optionally filtered by agent, type, or time."""
        text = self._load_text().strip()
        if not text:
            return []

        events: list[ActivityEvent] = []
        for line in text.splitlines():
            if not line:
                continue
            data = json.loads(line)
            if agent and data["agent"] != agent:
                continue
            if event_type and data["event_type"] != event_type:
                continue
            if since:
                ts = datetime.fromisoformat(data["timestamp"])
                if ts < since:
                    continue
            events.append(ActivityEvent(**data))
        return events
