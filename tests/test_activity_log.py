"""Tests for persistent JSONL activity log."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeband.monitoring.activity_log import ActivityEvent, ActivityLogger, ActivityReader


class TestActivityLogger:
    """Tests for the append-only JSONL logger."""

    def test_log_creates_file(self, tmp_path: Path):
        """Logging creates the JSONL file if it doesn't exist."""
        log_path = tmp_path / "activity.jsonl"
        logger = ActivityLogger(log_path)
        logger.log("SYSTEM_START", "codeband", "Starting 5 agents")
        assert log_path.exists()

    def test_log_appends(self, tmp_path: Path):
        """Multiple writes append, not overwrite."""
        log_path = tmp_path / "activity.jsonl"
        logger = ActivityLogger(log_path)
        logger.log("SYSTEM_START", "codeband", "Starting")
        logger.log("SESSION_START", "player-0", "Session #1")
        logger.log("AGENT_NUDGED", "watchdog", "Nudged player-1")

        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_log_writes_valid_json(self, tmp_path: Path):
        """Each line is valid JSON."""
        log_path = tmp_path / "activity.jsonl"
        logger = ActivityLogger(log_path)
        logger.log("TASK_ASSIGNED", "conductor", "Assigned auth to player-0",
                    branch="codeband/player-0/auth")

        line = log_path.read_text().strip()
        data = json.loads(line)
        assert data["event_type"] == "TASK_ASSIGNED"
        assert data["agent"] == "conductor"
        assert data["summary"] == "Assigned auth to player-0"
        assert data["details"]["branch"] == "codeband/player-0/auth"
        assert "timestamp" in data


class TestActivityReader:
    """Tests for reading and filtering the activity log."""

    @pytest.fixture
    def populated_log(self, tmp_path: Path) -> Path:
        """Create a log with several events."""
        log_path = tmp_path / "activity.jsonl"
        logger = ActivityLogger(log_path)

        logger.log("SYSTEM_START", "codeband", "Starting 4 agents")
        logger.log("SESSION_START", "player-0", "Session #1")
        logger.log("SESSION_START", "player-1", "Session #1")
        logger.log("TASK_ASSIGNED", "conductor", "Assigned auth to player-0")
        logger.log("AGENT_NUDGED", "watchdog", "Nudged player-1")
        logger.log("SESSION_CRASH", "player-1", "Context limit exceeded")
        logger.log("SESSION_RESTART", "player-1", "Session #2")
        logger.log("MERGE_COMPLETED", "mergemaster", "Merged player-0 branch")

        return log_path

    def test_read_all(self, populated_log: Path):
        """Reading without filters returns all events."""
        reader = ActivityReader(populated_log)
        events = reader.read()
        assert len(events) == 8

    def test_filter_by_agent(self, populated_log: Path):
        """Filter returns only matching agent."""
        reader = ActivityReader(populated_log)
        events = reader.read(agent="player-1")
        assert len(events) == 3
        assert all(e.agent == "player-1" for e in events)

    def test_filter_by_event_type(self, populated_log: Path):
        """Filter returns only matching event types."""
        reader = ActivityReader(populated_log)
        events = reader.read(event_type="SESSION_START")
        assert len(events) == 2

    def test_filter_by_since(self, populated_log: Path):
        """Time-based filter excludes older events."""
        reader = ActivityReader(populated_log)
        # All events were just written, so filtering since 1 hour ago should return all
        since = datetime.now(UTC) - timedelta(hours=1)
        events = reader.read(since=since)
        assert len(events) == 8

        # Filtering since the future should return none
        future = datetime.now(UTC) + timedelta(hours=1)
        events = reader.read(since=future)
        assert len(events) == 0

    def test_empty_log(self, tmp_path: Path):
        """Reading a nonexistent log returns empty list."""
        reader = ActivityReader(tmp_path / "nonexistent.jsonl")
        events = reader.read()
        assert events == []

    def test_combined_filters(self, populated_log: Path):
        """Multiple filters combine (AND logic)."""
        reader = ActivityReader(populated_log)
        events = reader.read(agent="player-1", event_type="SESSION_CRASH")
        assert len(events) == 1
        assert events[0].summary == "Context limit exceeded"


class TestActivityEvent:
    """Tests for the ActivityEvent dataclass."""

    def test_from_dict(self):
        """ActivityEvent can be constructed from a dict."""
        data = {
            "timestamp": "2026-03-28T14:00:00+00:00",
            "event_type": "MERGE_COMPLETED",
            "agent": "mergemaster",
            "summary": "Merged to main",
            "details": {"branch": "codeband/player-0/auth"},
        }
        event = ActivityEvent(**data)
        assert event.event_type == "MERGE_COMPLETED"
        assert event.details["branch"] == "codeband/player-0/auth"
