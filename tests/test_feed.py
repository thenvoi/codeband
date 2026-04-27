"""Tests for live feed formatter."""

from __future__ import annotations

import pytest

from codeband.monitoring.feed import FeedFormatter, LiveFeed


class TestFeedFormatter:
    """Tests for message formatting."""

    @pytest.fixture
    def formatter(self) -> FeedFormatter:
        """Formatter with agent ID → name mapping."""
        return FeedFormatter(
            agent_names={"id-cond": "conductor", "id-p0": "player-0", "id-mm": "mergemaster"},
        )

    def test_format_text_message(self, formatter: FeedFormatter):
        """TEXT messages show sender and content."""
        msg = {
            "sender_id": "id-p0",
            "message_type": "text",
            "content": "@conductor: Done. Branch codeband/player-0/auth",
            "inserted_at": "2026-03-28T14:02:45+00:00",
        }
        line = formatter.format(msg)
        assert "player-0" in line
        assert "Done. Branch" in line

    def test_format_tool_call(self, formatter: FeedFormatter):
        """TOOL_CALL messages show tool name."""
        msg = {
            "sender_id": "id-p0",
            "message_type": "tool_call",
            "content": '{"name": "write_file", "args": {"path": "src/auth.py"}}',
            "inserted_at": "2026-03-28T14:02:35+00:00",
        }
        line = formatter.format(msg)
        assert "player-0" in line
        assert "write_file" in line

    def test_format_tool_result(self, formatter: FeedFormatter):
        """TOOL_RESULT messages show brief output."""
        msg = {
            "sender_id": "id-p0",
            "message_type": "tool_result",
            "content": '{"name": "bash", "output": "5 passed"}',
            "inserted_at": "2026-03-28T14:02:42+00:00",
        }
        line = formatter.format(msg)
        assert "player-0" in line
        assert "5 passed" in line

    def test_format_thought(self, formatter: FeedFormatter):
        """THOUGHT messages show thinking text."""
        msg = {
            "sender_id": "id-cond",
            "message_type": "thought",
            "content": "I should decompose this into 3 subtasks",
            "inserted_at": "2026-03-28T14:02:30+00:00",
        }
        line = formatter.format(msg)
        assert "conductor" in line
        assert "decompose" in line

    def test_format_thought_hidden(self):
        """Thoughts return None when show_thoughts=False."""
        formatter = FeedFormatter(
            agent_names={"id-cond": "conductor"},
            show_thoughts=False,
        )
        msg = {
            "sender_id": "id-cond",
            "message_type": "thought",
            "content": "thinking...",
            "inserted_at": "2026-03-28T14:02:30+00:00",
        }
        assert formatter.format(msg) is None

    def test_unknown_agent_shows_id(self):
        """Unknown agent IDs are shown as-is."""
        formatter = FeedFormatter(agent_names={})
        msg = {
            "sender_id": "unknown-uuid",
            "message_type": "text",
            "content": "hello",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        line = formatter.format(msg)
        assert "unknown-uuid" in line

    def test_agent_filter(self):
        """Agent filter excludes non-matching agents."""
        formatter = FeedFormatter(
            agent_names={"id-p0": "player-0", "id-p1": "player-1"},
            agent_filter="player-0",
        )
        msg_p0 = {
            "sender_id": "id-p0",
            "message_type": "text",
            "content": "done",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        msg_p1 = {
            "sender_id": "id-p1",
            "message_type": "text",
            "content": "done",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        assert formatter.format(msg_p0) is not None
        assert formatter.format(msg_p1) is None

    def test_resolves_known_mention_token(self, formatter: FeedFormatter):
        """``@[[uuid]]`` tokens render as ``@<display name>`` when known."""
        msg = {
            "sender_id": "id-p0",
            "message_type": "text",
            "content": "@[[id-cond]] please review",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        line = formatter.format(msg)
        assert "@conductor" in line
        assert "@[[" not in line

    def test_resolves_unknown_mention_to_short_prefix(self, formatter: FeedFormatter):
        """Unknown UUIDs collapse to an 8-char prefix instead of staying 36 chars."""
        unknown = "063e2ccf-fd30-419f-ac8f-b6d4374e2332"
        msg = {
            "sender_id": "id-p0",
            "message_type": "text",
            "content": f"hi @[[{unknown}]]",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        line = formatter.format(msg)
        assert "@063e2ccf" in line
        assert unknown not in line

    def test_human_user_mapped_to_friendly_name(self):
        """Human user UUIDs map through the same dict as agents."""
        human_id = "6e6f4376-18ae-4ec1-a905-6bf2f8224de8"
        formatter = FeedFormatter(agent_names={human_id: "you"})
        msg = {
            "sender_id": human_id,
            "message_type": "text",
            "content": "add dark/light mode",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        line = formatter.format(msg)
        assert "you" in line
        assert human_id not in line

    def test_live_feed_skips_history_by_default(self):
        """A fresh LiveFeed primes _last_seen so old messages don't dump on first poll."""
        feed = LiveFeed(rest_client=None, formatter=FeedFormatter({}))
        assert feed._session_start is not None  # noqa: SLF001 — internal-state assertion

    def test_live_feed_show_history_keeps_full_replay(self):
        """Opt-in: callers that *want* history pass show_history=True."""
        feed = LiveFeed(rest_client=None, formatter=FeedFormatter({}), show_history=True)
        assert feed._session_start is None  # noqa: SLF001

    def test_type_filter(self):
        """Type filter excludes non-matching message types."""
        formatter = FeedFormatter(
            agent_names={"id-p0": "player-0"},
            type_filter={"tool_call"},
        )
        msg_text = {
            "sender_id": "id-p0",
            "message_type": "text",
            "content": "hello",
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        msg_tool = {
            "sender_id": "id-p0",
            "message_type": "tool_call",
            "content": '{"name": "bash"}',
            "inserted_at": "2026-03-28T14:00:00+00:00",
        }
        assert formatter.format(msg_text) is None
        assert formatter.format(msg_tool) is not None
