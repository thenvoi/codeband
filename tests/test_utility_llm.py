"""Tests for the one-shot Claude Code SDK utility helper."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from codeband.utility_llm import one_shot_text, parse_json_array


class TestParseJsonArray:
    def test_parses_bare_json(self):
        assert parse_json_array('[{"x": 1}, {"x": 2}]') == [{"x": 1}, {"x": 2}]

    def test_strips_json_fence(self):
        text = '```json\n[{"x": 1}]\n```'
        assert parse_json_array(text) == [{"x": 1}]

    def test_strips_unlabelled_fence(self):
        text = '```\n[{"x": 1}]\n```'
        assert parse_json_array(text) == [{"x": 1}]

    def test_tolerates_surrounding_whitespace(self):
        assert parse_json_array('   [1, 2, 3]   ') == [1, 2, 3]


class TestOneShotText:
    @pytest.mark.asyncio
    async def test_collects_text_blocks(self):
        """one_shot_text concatenates TextBlock content from AssistantMessages."""
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        async def fake_query(*, prompt, options):
            yield AssistantMessage(
                content=[TextBlock(text="Hello "), TextBlock(text="world")],
                model="claude-sonnet-4-6",
            )

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            result = await one_shot_text("ignored")
        assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_ignores_non_text_blocks(self):
        """Non-text blocks (tool use, thinking) are skipped."""
        from claude_agent_sdk.types import (
            AssistantMessage,
            TextBlock,
            ThinkingBlock,
        )

        async def fake_query(*, prompt, options):
            yield AssistantMessage(
                content=[
                    ThinkingBlock(thinking="reasoning...", signature="sig"),
                    TextBlock(text="answer"),
                ],
                model="claude-sonnet-4-6",
            )

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            result = await one_shot_text("ignored")
        assert result == "answer"

    @pytest.mark.asyncio
    async def test_captures_stderr_on_failure(self):
        """When the SDK raises, surface the CLI's real stderr in the exception.

        The SDK's own ProcessError replaces stderr with a hardcoded
        ``"Check stderr output for details"`` placeholder unless the caller
        wires an ``options.stderr`` callback — preflight needs the real text
        to classify usage-limit / auth failures.
        """
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        captured_options = {}

        async def fake_query(*, prompt, options):
            captured_options["stderr"] = options.stderr
            # Simulate the CLI emitting stderr before crashing.
            if options.stderr is not None:
                options.stderr("You've hit your limit · resets 1:10am (America/Los_Angeles)")
            # Yield nothing; raise to mimic the SDK's ProcessError path.
            raise RuntimeError("Command failed with exit code 1")
            yield AssistantMessage(content=[TextBlock(text="")], model="")  # pragma: no cover

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            with pytest.raises(Exception) as exc_info:
                await one_shot_text("hi")

        assert captured_options["stderr"] is not None, "stderr callback must be wired"
        assert "hit your limit" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_surfaces_rate_limit_event_on_failure(self):
        """When the CLI emits a ``rate_limit_event`` JSON before exiting 1,
        the structured info — not stderr — is the only signal of why we
        failed. ``one_shot_text`` must surface it in the raised exception.
        """
        from claude_agent_sdk.types import RateLimitEvent, RateLimitInfo

        async def fake_query(*, prompt, options):
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected",
                    resets_at="2026-04-28T08:10:00Z",
                    rate_limit_type="five_hour",
                    utilization=None,
                    overage_status=None,
                    overage_resets_at=None,
                    overage_disabled_reason=None,
                    raw={},
                ),
                uuid="u",
                session_id="s",
            )
            raise RuntimeError("Command failed with exit code 1")

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            with pytest.raises(Exception) as exc_info:
                await one_shot_text("hi")

        assert "status=rejected" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_surfaces_assistant_message_error_on_failure(self):
        """``AssistantMessage.error`` is the API's structured failure code
        ("rate_limit", "billing_error", ...). If the SDK then exits 1 it
        must reach the caller — preflight pattern-matches on it."""
        from claude_agent_sdk.types import AssistantMessage

        async def fake_query(*, prompt, options):
            yield AssistantMessage(
                content=[],
                model="claude-sonnet-4-6",
                error="rate_limit",
            )
            raise RuntimeError("Command failed with exit code 1")

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            with pytest.raises(Exception) as exc_info:
                await one_shot_text("hi")

        assert "assistant_message_error=rate_limit" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_surfaces_rate_limit_event_on_success(self):
        """The CLI may emit a ``rate_limit_event`` warning and still exit
        cleanly. The event content must be folded into the returned text
        so preflight's pattern matcher (which runs on ``result.lower()``)
        can still classify it."""
        from claude_agent_sdk.types import (
            AssistantMessage,
            RateLimitEvent,
            RateLimitInfo,
            TextBlock,
        )

        async def fake_query(*, prompt, options):
            yield RateLimitEvent(
                rate_limit_info=RateLimitInfo(
                    status="rejected",
                    resets_at="2026-04-28T08:10:00Z",
                    rate_limit_type="five_hour",
                    utilization=None,
                    overage_status=None,
                    overage_resets_at=None,
                    overage_disabled_reason=None,
                    raw={},
                ),
                uuid="u",
                session_id="s",
            )
            yield AssistantMessage(
                content=[TextBlock(text="ok")],
                model="claude-sonnet-4-6",
            )

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            result = await one_shot_text("hi")

        assert "ok" in result
        assert "status=rejected" in result

    @pytest.mark.asyncio
    async def test_passes_model_through_options(self):
        """Caller-supplied model lands in ClaudeAgentOptions."""
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        captured = {}

        async def fake_query(*, prompt, options):
            captured["model"] = options.model
            captured["max_turns"] = options.max_turns
            captured["tools"] = options.tools
            yield AssistantMessage(
                content=[TextBlock(text="ok")], model=options.model or "",
            )

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            await one_shot_text("hi", model="claude-haiku-4-5")

        assert captured["model"] == "claude-haiku-4-5"
        assert captured["max_turns"] == 1
        # Must be an explicit empty list (not None) so the CLI gets ``--tools ""``
        # rather than falling back to the default tool set.
        assert captured["tools"] == []
