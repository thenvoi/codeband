"""Code reviewer agent — standalone code review before merge."""

from __future__ import annotations

import logging
from pathlib import Path

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
        model: str = "gpt-5.4",
        custom_prompt: str | None = None,
        review_guidelines: str | None = None,
        workspace: str | None = None,
        recovery_context: str | None = None,
        # Whole-turn budget (finding 22 / shakedown finding 4): the SDK's
        # 180s default abandons any longer turn mid-flight while the Codex
        # CLI keeps working. Wired from agents.codex_turn_timeout_seconds.
        turn_timeout_seconds: int = 3600,
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

        from codeband.agents.prompts import build_review_prompt, load_knowledge

        prompt = build_review_prompt(custom_prompt, review_guidelines, _DEFAULT_PROMPT)
        prompt += load_knowledge("coding-standards", "testing", "security")
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"
        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            approval_policy="never",
            approval_mode=None,
            cwd=workspace,
            sandbox="danger-full-access",
            turn_timeout_s=float(turn_timeout_seconds),
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
        model: str = "claude-sonnet-4-6",
        custom_prompt: str | None = None,
        review_guidelines: str | None = None,
        workspace: str | None = None,
        recovery_context: str | None = None,
    ):
        from thenvoi.adapters import ClaudeSDKAdapter
        from thenvoi.core.types import AdapterFeatures, Capability, Emit

        from codeband.agents.prompts import build_review_prompt, load_knowledge

        prompt = build_review_prompt(custom_prompt, review_guidelines, _DEFAULT_PROMPT)
        prompt += load_knowledge("coding-standards", "testing", "security")
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"
        self._adapter = ClaudeSDKAdapter(
            model=model,
            custom_section=prompt,
            permission_mode="bypassPermissions",
            approval_mode=None,
            features=AdapterFeatures(
                capabilities={Capability.MEMORY},
                emit={Emit.EXECUTION, Emit.THOUGHTS},
            ),
            cwd=workspace,
        )

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter
