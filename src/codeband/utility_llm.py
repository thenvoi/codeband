"""One-shot LLM helper for CLI commands (--smart ranking, etc.).

Uses Claude Code SDK so the same auth that runs coding agents
(``ANTHROPIC_API_KEY`` or ``CLAUDE_CODE_OAUTH_TOKEN``) also runs these
utility calls — no separate auth path or direct Anthropic SDK use.
"""

from __future__ import annotations

import json
import re

# Strips a leading/trailing ```json ... ``` fence in case the model adds it
# despite the prompt. Tolerant of extra whitespace.
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


async def one_shot_text(prompt: str, *, model: str = "claude-sonnet-4-6") -> str:
    """Run a single-turn prompt through Claude Code SDK and return assistant text.

    Disables tool use so the call is a plain prompt → text round trip.
    """
    from claude_agent_sdk import query
    from claude_agent_sdk.types import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
    )

    # ``tools=[]`` emits ``--tools ""`` (empty base tool set); an empty
    # ``allowed_tools`` would be falsy and silently fall back to defaults.
    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        tools=[],
    )

    chunks: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)


def parse_json_array(text: str) -> list:
    """Parse a JSON array from text, tolerating an optional ```json fence."""
    stripped = text.strip()
    match = _FENCE.match(stripped)
    if match:
        stripped = match.group(1).strip()
    return json.loads(stripped)
