"""Codex (OpenAI) coder agent — coding worker using Codex."""

from __future__ import annotations

import logging
from pathlib import Path

from codeband.models import CODEX_GPT

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "coder.md"


class CodexPlayerRunner:
    """
    Coder agent backed by OpenAI Codex.

    Uses the Codex runner pattern from the Band.ai SDK for coding tasks
    within an isolated git worktree. Requires danger-full-access sandbox
    because git operations (branch, commit, push) write to .git/ metadata
    and need network access to push to the remote.
    """

    def __init__(
        self,
        *,
        model: str = CODEX_GPT,
        custom_prompt: str | None = None,
        workspace: str | None = None,
        recovery_context: str | None = None,
        worker_roster: str | None = None,
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

        self.model = model
        from codeband.agents.prompts import load_prompt

        prompt = custom_prompt or load_prompt(_DEFAULT_PROMPT)
        if worker_roster:
            prompt += f"\n\n{worker_roster}"
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"

        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            cwd=workspace,
            approval_policy="never",
            approval_mode=None,
            sandbox="danger-full-access",
        )
        self._adapter = CodexAdapter(config=config)

    @property
    def adapter(self):
        """Return the underlying adapter for Agent.create()."""
        return self._adapter

