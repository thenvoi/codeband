"""Claude Code coder agent — coding worker."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "coder.md"


class ClaudePlayerRunner:
    """
    Coder agent backed by Claude Code.

    Provides full coding capabilities: file read/write, shell execution,
    git operations — all within an isolated git worktree.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        custom_prompt: str | None = None,
        workspace: str | None = None,
        recovery_context: str | None = None,
    ):
        from thenvoi.adapters import ClaudeSDKAdapter

        self.model = model
        from codeband.agents.prompts import load_prompt

        prompt = custom_prompt or load_prompt(_DEFAULT_PROMPT)
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"

        self._adapter = ClaudeSDKAdapter(
            model=model,
            custom_section=prompt,
            permission_mode="bypassPermissions",
            approval_mode=None,
            enable_execution_reporting=True,
            enable_memory_tools=True,
            cwd=workspace,
        )

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter


