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

    On failure, re-raises with structured error context appended. Two
    sources of failure context are captured because the CLI doesn't put
    them in the same place:

    1. **stderr** — wired via ``ClaudeAgentOptions(stderr=...)``. The SDK's
       ``ProcessError`` substitutes ``"Check stderr output for details"``
       unless a callback is set.
    2. **stream-json events** — usage-limit hits, API auth/billing errors,
       and the per-turn ``ResultMessage`` arrive as structured JSON on
       stdout, not stderr. Inspecting the message stream is the *only* way
       to surface them; ``ProcessError`` itself carries no payload.

    Either source can fire alone or together. We accumulate both and only
    re-wrap the exception when at least one yielded content; otherwise the
    original is re-raised unchanged.
    """
    from claude_agent_sdk import query
    from claude_agent_sdk.types import (
        AssistantMessage,
        ClaudeAgentOptions,
        RateLimitEvent,
        ResultMessage,
        TextBlock,
    )

    stderr_lines: list[str] = []
    context_lines: list[str] = []

    # ``tools=[]`` emits ``--tools ""`` (empty base tool set); an empty
    # ``allowed_tools`` would be falsy and silently fall back to defaults.
    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        tools=[],
        stderr=stderr_lines.append,
    )

    chunks: list[str] = []
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                if message.error:
                    context_lines.append(f"assistant_message_error={message.error}")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                context_lines.append(
                    f"rate_limit_event status={info.status} "
                    f"resets_at={info.resets_at} type={info.rate_limit_type}"
                )
            elif isinstance(message, ResultMessage) and message.is_error:
                context_lines.append(
                    f"result is_error subtype={message.subtype} "
                    f"result={message.result!r}"
                )
    except Exception as exc:
        extra = _format_failure_context(stderr_lines, context_lines)
        if extra:
            raise RuntimeError(f"{exc}\n{extra}") from exc
        raise

    text = "".join(chunks)
    # Surface structured signals on the success path too — preflight's
    # pattern matcher runs on the returned string.
    if context_lines:
        joined = "\n".join(context_lines)
        text = f"{text}\n{joined}" if text else joined
    return text


def _format_failure_context(
    stderr_lines: list[str], context_lines: list[str]
) -> str:
    """Combine stderr and stream-json signals into one human-readable block.

    Returns an empty string when neither source captured anything — caller
    re-raises the original exception in that case.
    """
    parts: list[str] = []
    if context_lines:
        parts.append("\n".join(context_lines))
    if stderr_lines:
        parts.append("claude stderr: " + "\n".join(stderr_lines).strip())
    return "\n".join(parts)


def parse_json_array(text: str) -> list:
    """Parse a JSON array from text, tolerating an optional ```json fence."""
    stripped = text.strip()
    match = _FENCE.match(stripped)
    if match:
        stripped = match.group(1).strip()
    return json.loads(stripped)
