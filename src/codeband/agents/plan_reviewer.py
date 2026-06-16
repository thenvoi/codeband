"""Plan reviewer agent — validates implementation plans before execution."""

from __future__ import annotations

import logging
from pathlib import Path

from codeband.models import CLAUDE_SONNET, CODEX_GPT

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "plan_reviewer.md"


class ClaudePlanReviewerRunner:
    """
    Plan reviewer backed by Claude Code.

    Uses a read-only allowlist in the worktree's .claude/settings.json; the
    ``dontAsk`` permission mode deterministically denies anything outside the
    allowlist without hanging on a prompt. The prompt instructs the agent to
    never report internal tool decline messages to the chat room.
    """

    def __init__(
        self,
        *,
        model: str = CLAUDE_SONNET,
        custom_prompt: str | None = None,
        review_guidelines: str | None = None,
        workspace: str | None = None,
        identity_section: str | None = None,
    ):
        from band.adapters import ClaudeSDKAdapter
        from band.core.types import AdapterFeatures, Capability, Emit

        from codeband.agents.prompts import build_review_prompt

        prompt = build_review_prompt(custom_prompt, review_guidelines, _DEFAULT_PROMPT)
        if identity_section:
            prompt += f"\n\n{identity_section}"
        # See planner.py for why `dontAsk` + `approval_mode=None` — this lets
        # .claude/settings.json own the allow list instead of an adapter-level
        # can_use_tool hook that would override it.
        self._adapter = ClaudeSDKAdapter(
            model=model,
            custom_section=prompt,
            permission_mode="dontAsk",  # type: ignore[arg-type]
            approval_mode=None,
            cwd=workspace,
            features=AdapterFeatures(
                emit={Emit.EXECUTION, Emit.THOUGHTS},
                capabilities={Capability.MEMORY},
            ),
        )

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter


class CodexPlanReviewerRunner:
    """Plan reviewer backed by OpenAI Codex in a read-only sandbox."""

    def __init__(
        self,
        *,
        model: str = CODEX_GPT,
        custom_prompt: str | None = None,
        review_guidelines: str | None = None,
        workspace: str | None = None,
        identity_section: str | None = None,
    ):
        try:
            from band.adapters import CodexAdapter
            from band.adapters.codex import CodexAdapterConfig
        except ImportError as e:
            raise ImportError(
                "Codex adapter unavailable — band-sdk's codex extras failed to import. "
                "Reinstall codeband (`pip install -U codeband`) to restore bundled "
                "Codex support."
            ) from e

        from codeband.agents.prompts import build_review_prompt

        prompt = build_review_prompt(custom_prompt, review_guidelines, _DEFAULT_PROMPT)
        if identity_section:
            prompt += f"\n\n{identity_section}"
        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            approval_policy="never",
            approval_mode=None,
            cwd=workspace,
            sandbox="read-only",
        )
        self._adapter = CodexAdapter(config=config)

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter
