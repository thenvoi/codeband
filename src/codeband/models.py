"""Canonical LLM model identifiers.

One place to bump model versions instead of editing string literals across
config defaults, agent runners, and the utility LLM helper. Change the value
here and every role default follows.

These are the exact identifiers passed to the Claude Code SDK / CLI
(``claude-*``) and the Codex CLI (``gpt-*``).
"""

from __future__ import annotations

# Anthropic (Claude Code)
CLAUDE_OPUS = "claude-opus-4-8"
CLAUDE_SONNET = "claude-sonnet-4-6"

# OpenAI (Codex)
CODEX_GPT = "gpt-5.5"
