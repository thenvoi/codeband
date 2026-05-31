"""Planner agent — codebase analysis and task decomposition."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "planner.md"


def _build_prompt(custom_prompt: str | None, worker_roster: str | None) -> str:
    """Compose the system prompt — shared by both framework runners."""
    from codeband.agents.prompts import load_prompt

    prompt = custom_prompt or load_prompt(_DEFAULT_PROMPT)
    if worker_roster:
        prompt += f"\n\n{worker_roster}"
    return prompt


class ClaudePlannerRunner:
    """
    Planner agent backed by Claude Code.

    Analyzes the codebase, decomposes tasks into parallelizable subtasks,
    and writes structured plans for the Conductor to execute. Tool access
    is constrained by the planner worktree's .claude/settings.json.
    """

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        custom_prompt: str | None = None,
        workspace: str | None = None,
        worker_roster: str | None = None,
        recovery_context: str | None = None,
    ):
        from thenvoi.adapters import ClaudeSDKAdapter

        prompt = _build_prompt(custom_prompt, worker_roster)
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"

        # `dontAsk` is a bundled Claude CLI mode not yet in the SDK's
        # PermissionMode Literal, but forwarded verbatim via --permission-mode.
        # It honors .claude/settings.json allow rules and deterministically
        # denies everything else — no interactive prompt, no hang, and
        # (critically) no override of settings.json via a can_use_tool hook
        # like `approval_mode="auto_decline"` would install.
        self._adapter = ClaudeSDKAdapter(
            model=model,
            custom_section=prompt,
            permission_mode="dontAsk",  # type: ignore[arg-type]
            approval_mode=None,
            enable_execution_reporting=True,
            enable_memory_tools=True,
            cwd=workspace,
        )

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter


class CodexPlannerRunner:
    """Planner agent backed by OpenAI Codex in a read-only sandbox.

    Mirrors :class:`ClaudePlannerRunner` for cross-model adversarial
    pairing. Planning is read-only — analyze the repo, write a plan into
    chat — so Codex's ``read-only`` sandbox is the right shape: native
    file tools can inspect the worktree but cannot mutate it.
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        custom_prompt: str | None = None,
        workspace: str | None = None,
        worker_roster: str | None = None,
        recovery_context: str | None = None,
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

        prompt = _build_prompt(custom_prompt, worker_roster)
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"
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
