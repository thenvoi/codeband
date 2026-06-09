"""Conductor agent — task routing and orchestration.

The Conductor coordinates other agents (Planner, Coders, Reviewers,
Mergemaster) but does no coding itself. The Claude variant runs with
chat + memory tools only; the Codex variant is constrained to a
read-only scratch directory outside the repo.

The previous implementation called the Anthropic SDK directly and rebuilt
the message list on every turn; with adapter-managed sessions the SDK keeps
the real conversation history, which is strictly more information than the
regex summary, so that scaffolding is gone.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "conductor.md"


def _compose_prompt(
    custom_prompt: str | None,
    worker_roster: str | None,
    auto_merge: str | None,
    repo_pin: str | None,
) -> str:
    from codeband.agents.prompts import load_prompt

    if custom_prompt is not None:
        return custom_prompt
    prompt = load_prompt(_DEFAULT_PROMPT)
    if auto_merge is not None:
        prompt += f"\n\n## Current Configuration\nauto_merge: {auto_merge}\n"
    if repo_pin:
        prompt += f"\n{repo_pin}\n"
    if worker_roster:
        prompt += f"\n{worker_roster}\n"
    return prompt


class ClaudeConductorRunner:
    """Conductor backed by Claude Code SDK — coordination only, no coding tools."""

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-6",
        custom_prompt: str | None = None,
        worker_roster: str | None = None,
        auto_merge: str | None = None,
        repo_pin: str | None = None,
        recovery_context: str | None = None,
    ):
        from thenvoi.adapters import ClaudeSDKAdapter
        from thenvoi.core.types import AdapterFeatures, Capability, Emit

        prompt = _compose_prompt(custom_prompt, worker_roster, auto_merge, repo_pin)
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"

        # See planner.py for why `dontAsk` + `approval_mode=None`. The
        # Conductor has no workspace, so there's no .claude/settings.json to
        # consult — MCP tools (registered via `allowed_tools`) bypass the
        # permission system and remain available; anything else is denied.
        self._adapter = ClaudeSDKAdapter(
            model=model,
            custom_section=prompt,
            permission_mode="dontAsk",  # type: ignore[arg-type]
            approval_mode=None,
            features=AdapterFeatures(
                capabilities={Capability.MEMORY},
                emit={Emit.EXECUTION, Emit.THOUGHTS},
            ),
        )

    @property
    def adapter(self):
        """Return the underlying adapter for ``Agent.create()``."""
        return self._adapter


class CodexConductorRunner:
    """Conductor backed by OpenAI Codex — coordination only, no coding tools.

    Codex does not currently expose a native "disable filesystem tools"
    switch through the Thenvoi adapter. To keep the Conductor away from
    the repo, we run it in an isolated temporary directory with a
    read-only sandbox and no approval prompts.
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5.4",
        custom_prompt: str | None = None,
        worker_roster: str | None = None,
        auto_merge: str | None = None,
        repo_pin: str | None = None,
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

        prompt = _compose_prompt(custom_prompt, worker_roster, auto_merge, repo_pin)
        if recovery_context:
            prompt = f"{recovery_context}\n\n---\n\n{prompt}"
        self._scratch_dir = tempfile.TemporaryDirectory(
            prefix="codeband-conductor-",
        )

        config = CodexAdapterConfig(
            model=model,
            system_prompt=prompt,
            approval_policy="never",
            approval_mode=None,
            cwd=self._scratch_dir.name,
            sandbox="read-only",
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
        """Return the underlying adapter for ``Agent.create()``."""
        return self._adapter
