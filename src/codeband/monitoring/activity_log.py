"""Persistent JSONL activity log — append-only structured event history."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


class EventType:
    """Well-known activity ``event_type`` values."""

    LLM_USAGE: Final[str] = "LLM_USAGE"


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
        """Append an event to the activity log.

        The append holds an exclusive ``fcntl.flock`` for the write — the
        same discipline ``LocalMemoryStore`` uses (S6-F8). Every agent task
        in the process (plus the watchdog) shares this one file; without the
        lock, concurrent appends can interleave into a torn line that then
        poisons every future read.
        """
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "agent": agent,
            "summary": summary,
            "details": details if details else None,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(event) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


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
            # One torn/malformed line (a crash mid-append, a corrupted byte)
            # must not kill `cb log` forever — skip it with a warning, the
            # same policy as LocalMemoryStore._iter_records (S6-F8).
            try:
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
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed activity log line: %s", exc)
        return events
