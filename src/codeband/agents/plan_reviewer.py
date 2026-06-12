"""Plan reviewer agent — validates implementation plans before execution."""

from __future__ import annotations

import logging
from pathlib import Path

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
        prompt += load_knowledge("testing")
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"
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
                capabilities={Capability.MEMORY},
                emit={Emit.EXECUTION, Emit.THOUGHTS},
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
        prompt += load_knowledge("testing")
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"
        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            approval_policy="never",
            approval_mode=None,
            cwd=workspace,
            sandbox="read-only",
            turn_timeout_s=float(turn_timeout_seconds),
        )
        self._adapter = CodexAdapter(config=config)

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter
