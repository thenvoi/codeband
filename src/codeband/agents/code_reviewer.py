"""Code reviewer agent — standalone code review before merge."""

from __future__ import annotations

import logging
from pathlib import Path

from codeband.models import CLAUDE_SONNET, CODEX_GPT

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "code_reviewer.md"


class CodexCodeReviewerRunner:
    """
    Code reviewer backed by OpenAI Codex.

    Runs in an isolated scratch directory. Requires danger-full-access
    sandbox because the gh CLI needs network access to reach the GitHub API.
    """

    def __init__(
        self,
        *,
        model: str = CODEX_GPT,
        custom_prompt: str | None = None,
        review_guidelines: str | None = None,
        workspace: str | None = None,
    ):
        try:
            from thenvoi.adapters import CodexAdapter
            from thenvoi.adapters.codex import CodexAdapterConfig
        except ImportError as e:
            raise ImportError(
                "Codex adapter unavailable — band-sdk's codex extras failed to import. "
                "Reinstall codeband (`pip install -U codeband`) to restore bundled "
                "Codex support."
            ) from e

        from codeband.agents.prompts import build_review_prompt

        prompt = build_review_prompt(custom_prompt, review_guidelines, _DEFAULT_PROMPT)
        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            approval_policy="never",
            approval_mode=None,
            cwd=workspace,
            sandbox="danger-full-access",
        )
        self._adapter = CodexAdapter(config=config)

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter


class ClaudeCodeReviewerRunner:
    """
    Code reviewer backed by Claude Code.

    Runs in an isolated scratch directory with only the gh pr commands
    required for review allowlisted in .claude/settings.json.
    """

    def __init__(
        self,
        *,
        model: str = CLAUDE_SONNET,
        custom_prompt: str | None = None,
        review_guidelines: str | None = None,
        workspace: str | None = None,
    ):
        from thenvoi.adapters import ClaudeSDKAdapter

        from codeband.agents.prompts import build_review_prompt

        prompt = build_review_prompt(custom_prompt, review_guidelines, _DEFAULT_PROMPT)
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
