"""Tests for token/usage cost tracking."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from codeband.monitoring.activity_log import ActivityLogger, ActivityReader


@pytest.fixture()
def activity(tmp_path):
    """Create an ActivityLogger writing to a temp file."""
    return ActivityLogger(tmp_path / "activity.jsonl")


@pytest.fixture()
def reader(tmp_path):
    """Create an ActivityReader for the same temp file."""
    return ActivityReader(tmp_path / "activity.jsonl")


class TestSDKUsageHandler:
    """Test the logging handler that captures ClaudeSDK usage from log lines."""

    def test_captures_completion_log(self, activity, reader):
        from codeband.monitoring.usage import SDKUsageHandler

        handler = SDKUsageHandler(activity)
        sdk_logger = logging.getLogger("band.adapters.claude_sdk.test_capture")
        sdk_logger.addHandler(handler)
        sdk_logger.setLevel(logging.DEBUG)

        try:
            sdk_logger.info(
                "Room %s: Complete - %sms, $%.4f",
                "room_abc123", 12340, 1.4520,
            )

            events = reader.read(event_type="LLM_USAGE")
            assert len(events) == 1
            e = events[0]
            assert e.agent == "room_abc123"  # falls back to room_id when no agent_name
            assert e.details["cost_usd"] == pytest.approx(1.4520, abs=1e-4)
            assert e.details["duration_ms"] == 12340
            assert e.details["source"] == "claude_sdk"
            assert e.details["room_id"] == "room_abc123"
        finally:
            sdk_logger.removeHandler(handler)

    def test_uses_agent_name_when_provided(self, activity, reader):
        from codeband.monitoring.usage import SDKUsageHandler

        handler = SDKUsageHandler(activity, agent_name="player-0")
        sdk_logger = logging.getLogger("band.adapters.claude_sdk.test_named")
        sdk_logger.addHandler(handler)
        sdk_logger.setLevel(logging.DEBUG)

        try:
            sdk_logger.info(
                "Room %s: Complete - %sms, $%.4f",
                "room_xyz", 5000, 0.5000,
            )

            events = reader.read(event_type="LLM_USAGE")
            assert len(events) == 1
            assert events[0].agent == "player-0"
        finally:
            sdk_logger.removeHandler(handler)

    def test_ignores_other_log_lines(self, activity, reader):
        from codeband.monitoring.usage import SDKUsageHandler

        handler = SDKUsageHandler(activity)
        sdk_logger = logging.getLogger("band.adapters.claude_sdk.test_ignore")
        sdk_logger.addHandler(handler)
        sdk_logger.setLevel(logging.DEBUG)

        try:
            sdk_logger.info("Room %s: Connected", "room_xyz")
            sdk_logger.info("Some other message entirely")
            sdk_logger.debug("Room %s: Captured session_id %s", "room_abc", "sess_123")

            events = reader.read(event_type="LLM_USAGE")
            assert len(events) == 0
        finally:
            sdk_logger.removeHandler(handler)


class TestUsageSummary:
    """Test aggregation of usage events."""

    def _write_events(self, log_path, events):
        """Write raw event dicts to the JSONL file."""
        with open(log_path, "a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")

    def test_aggregation(self, tmp_path):
        from codeband.monitoring.usage import UsageSummary

        log_path = tmp_path / "activity.jsonl"
        now = datetime.now(UTC).isoformat()
        self._write_events(log_path, [
            {
                "timestamp": now,
                "event_type": "LLM_USAGE",
                "agent": "conductor",
                "summary": "test",
                "details": {
                    "input_tokens": 1000, "output_tokens": 200,
                    "cost_usd": 0.01, "source": "anthropic_api",
                },
            },
            {
                "timestamp": now,
                "event_type": "LLM_USAGE",
                "agent": "conductor",
                "summary": "test",
                "details": {
                    "input_tokens": 500, "output_tokens": 100,
                    "cost_usd": 0.005, "source": "anthropic_api",
                },
            },
            {
                "timestamp": now,
                "event_type": "LLM_USAGE",
                "agent": "player-0",
                "summary": "test",
                "details": {
                    "cost_usd": 1.50, "source": "claude_sdk",
                },
            },
            {
                "timestamp": now,
                "event_type": "SYSTEM_START",
                "agent": "codeband",
                "summary": "ignored",
                "details": None,
            },
        ])

        reader = ActivityReader(log_path)
        summary = UsageSummary.from_activity_reader(reader)

        assert summary.total_cost_usd == pytest.approx(1.515, abs=1e-6)
        assert summary.total_input_tokens == 1500
        assert summary.total_output_tokens == 300
        assert summary.call_count == 3
        assert len(summary.by_agent) == 2
        assert summary.by_agent["conductor"].call_count == 2
        assert summary.by_agent["conductor"].total_cost_usd == pytest.approx(0.015)
        assert summary.by_agent["player-0"].call_count == 1
        assert summary.by_agent["player-0"].total_cost_usd == pytest.approx(1.50)

    def test_filter_by_agent(self, tmp_path):
        from codeband.monitoring.usage import UsageSummary

        log_path = tmp_path / "activity.jsonl"
        now = datetime.now(UTC).isoformat()
        self._write_events(log_path, [
            {
                "timestamp": now,
                "event_type": "LLM_USAGE",
                "agent": "conductor",
                "summary": "test",
                "details": {"cost_usd": 0.01, "source": "anthropic_api"},
            },
            {
                "timestamp": now,
                "event_type": "LLM_USAGE",
                "agent": "watchdog",
                "summary": "test",
                "details": {"cost_usd": 0.002, "source": "anthropic_api"},
            },
        ])

        reader = ActivityReader(log_path)
        summary = UsageSummary.from_activity_reader(reader, agent="watchdog")

        assert summary.call_count == 1
        assert summary.total_cost_usd == pytest.approx(0.002)
        assert "watchdog" in summary.by_agent
        assert "conductor" not in summary.by_agent

    def test_filter_by_since(self, tmp_path):
        from codeband.monitoring.usage import UsageSummary

        log_path = tmp_path / "activity.jsonl"
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        recent = datetime.now(UTC).isoformat()
        self._write_events(log_path, [
            {
                "timestamp": old,
                "event_type": "LLM_USAGE",
                "agent": "conductor",
                "summary": "old",
                "details": {"cost_usd": 0.01, "source": "anthropic_api"},
            },
            {
                "timestamp": recent,
                "event_type": "LLM_USAGE",
                "agent": "conductor",
                "summary": "new",
                "details": {"cost_usd": 0.02, "source": "anthropic_api"},
            },
        ])

        reader = ActivityReader(log_path)
        since = datetime.now(UTC) - timedelta(hours=1)
        summary = UsageSummary.from_activity_reader(reader, since=since)

        assert summary.call_count == 1
        assert summary.total_cost_usd == pytest.approx(0.02)

    def test_empty_log(self, tmp_path):
        from codeband.monitoring.usage import UsageSummary

        reader = ActivityReader(tmp_path / "nonexistent.jsonl")
        summary = UsageSummary.from_activity_reader(reader)

        assert summary.call_count == 0
        assert summary.total_cost_usd == 0.0
        assert summary.total_input_tokens == 0
        assert summary.total_output_tokens == 0
        assert len(summary.by_agent) == 0
