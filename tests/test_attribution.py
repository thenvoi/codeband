"""Tests for framework self-attribution on submitted artifacts.

Every agent must be told which framework it runs on and instructed to stamp
the artifacts it submits (plans, PRs, reviews, merge decisions) with a
``[From Claude Code]`` / ``[From Codex]`` tag. The runner injects a shared
"Your Identity & Attribution" section into each agent's system prompt; these
tests pin both the section content and that every runner carries it.
"""

from __future__ import annotations

from codeband.config import Framework
from codeband.orchestration.runner import _build_identity_section, framework_label


def _prompt_of(runner) -> str:
    """Return the composed system prompt regardless of adapter framework."""
    adapter = runner.adapter
    section = getattr(adapter, "custom_section", None)
    if section is not None:
        return section
    return adapter.config.system_prompt


class TestIdentitySection:
    def test_framework_label_mapping(self):
        assert framework_label(Framework.CLAUDE_SDK) == "Claude Code"
        assert framework_label(Framework.CODEX) == "Codex"

    def test_claude_section_carries_tag_and_guard(self):
        section = _build_identity_section(Framework.CLAUDE_SDK)
        assert "[From Claude Code]" in section
        assert "Claude Code" in section
        # Scope guard: routine coordination chatter stays untagged.
        assert "Do NOT tag routine coordination" in section

    def test_codex_section_carries_tag(self):
        section = _build_identity_section(Framework.CODEX)
        assert "[From Codex]" in section
        assert "[From Claude Code]" not in section


# (RunnerClass, framework, ctor kwargs) — every role, both frameworks.
def _runner_cases(tmp_path):
    from codeband.agents.code_reviewer import (
        ClaudeCodeReviewerRunner,
        CodexCodeReviewerRunner,
    )
    from codeband.agents.conductor import ClaudeConductorRunner, CodexConductorRunner
    from codeband.agents.mergemaster import (
        ClaudeMergemasterRunner,
        CodexMergemasterRunner,
    )
    from codeband.agents.plan_reviewer import (
        ClaudePlanReviewerRunner,
        CodexPlanReviewerRunner,
    )
    from codeband.agents.planner import ClaudePlannerRunner, CodexPlannerRunner
    from codeband.agents.player_claude import ClaudePlayerRunner
    from codeband.agents.player_codex import CodexPlayerRunner

    ws = {"workspace": str(tmp_path)}
    return [
        (ClaudePlannerRunner, Framework.CLAUDE_SDK, ws),
        (CodexPlannerRunner, Framework.CODEX, ws),
        (ClaudeConductorRunner, Framework.CLAUDE_SDK, {}),
        (CodexConductorRunner, Framework.CODEX, {}),
        (ClaudePlayerRunner, Framework.CLAUDE_SDK, ws),
        (CodexPlayerRunner, Framework.CODEX, ws),
        (ClaudeCodeReviewerRunner, Framework.CLAUDE_SDK, ws),
        (CodexCodeReviewerRunner, Framework.CODEX, ws),
        (ClaudePlanReviewerRunner, Framework.CLAUDE_SDK, ws),
        (CodexPlanReviewerRunner, Framework.CODEX, ws),
        (ClaudeMergemasterRunner, Framework.CLAUDE_SDK, ws),
        (CodexMergemasterRunner, Framework.CODEX, ws),
    ]


class TestRunnersCarryIdentity:
    """Each runner, given an identity_section, must surface it in its prompt."""

    def test_all_runners_inject_identity_tag(self, tmp_path):
        for runner_cls, fw, kwargs in _runner_cases(tmp_path):
            section = _build_identity_section(fw)
            runner = runner_cls(identity_section=section, **kwargs)
            prompt = _prompt_of(runner)
            expected = f"[From {framework_label(fw)}]"
            assert expected in prompt, f"{runner_cls.__name__} missing {expected}"

    def test_runner_without_identity_section_omits_the_section(self, tmp_path):
        """identity_section is optional — omitting it must not inject the section.

        (The base prompt still carries the ``[From <your framework>]`` template
        pointer, so we assert on the injected section header instead.)
        """
        from codeband.agents.planner import ClaudePlannerRunner

        runner = ClaudePlannerRunner(workspace=str(tmp_path))
        assert "## Your Identity & Attribution" not in _prompt_of(runner)
