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
