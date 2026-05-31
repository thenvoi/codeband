"""Mergemaster agent — branch integration and testing."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "mergemaster.md"


def _compose_prompt(
    custom_prompt: str | None,
    test_command: str | None,
    review_guidelines: str | None,
) -> str:
    from codeband.agents.prompts import load_prompt

    prompt = custom_prompt or load_prompt(_DEFAULT_PROMPT)
    test_cmd_display = test_command or "auto-detect (look for pytest, npm test, make test)"
    config_section = f"\n\n## Configuration\n- Test command: {test_cmd_display}\n"
    if review_guidelines:
        config_section += f"- Review guidelines: {review_guidelines}\n"
    return prompt + config_section


class ClaudeMergemasterRunner:
    """
    Mergemaster using Claude Code for git merge + test execution.

    Works in the mergemaster worktree (checked out to main branch).
    """

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        custom_prompt: str | None = None,
        workspace: str | None = None,
        test_command: str | None = None,
        review_guidelines: str | None = None,
        recovery_context: str | None = None,
    ):
        from thenvoi.adapters import ClaudeSDKAdapter

        prompt = _compose_prompt(custom_prompt, test_command, review_guidelines)
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


class CodexMergemasterRunner:
    """
    Mergemaster using OpenAI Codex for git merge + test execution.

    Requires `danger-full-access` sandbox because git operations write
    `.git/` metadata and need network access to push to the remote.
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        custom_prompt: str | None = None,
        workspace: str | None = None,
        test_command: str | None = None,
        review_guidelines: str | None = None,
        recovery_context: str | None = None,
    ):
        try:
            from thenvoi.adapters import CodexAdapter
            from thenvoi.adapters.codex import CodexAdapterConfig
            from thenvoi.core.types import AdapterFeatures, Capability, Emit
        except ImportError as e:
            raise ImportError(
                "Codex adapter unavailable — band-sdk's codex extras failed to import. "
                "Reinstall codeband (`pip install -U codeband`) to restore bundled "
                "Codex support."
            ) from e

        prompt = _compose_prompt(custom_prompt, test_command, review_guidelines)
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"

        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            approval_policy="never",
            approval_mode=None,
            cwd=workspace,
            sandbox="danger-full-access",
        )
        self._adapter = CodexAdapter(
            config=config,
            features=AdapterFeatures(
                capabilities={Capability.MEMORY},
                emit={Emit.EXECUTION, Emit.TASK_EVENTS},
            ),
        )

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter
