"""Tests for the separate Code Reviewer agent — pool config + permission wiring."""

from __future__ import annotations

import os
from pathlib import Path

from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    Framework,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
)


class TestReviewersPoolConfig:
    """Tests for the new ReviewersConfig pool shape."""

    def test_default_has_both_frameworks(self):
        """Default config has both Claude and Codex reviewer capacity."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        reviewers = config.agents.reviewers
        assert reviewers.claude_sdk.count == 1
        assert reviewers.codex.count == 1
        assert reviewers.active_frameworks() == [Framework.CLAUDE_SDK, Framework.CODEX]

    def test_review_guidelines_stored(self):
        reviewers = ReviewersConfig(
            claude_sdk=PoolEntry(count=1),
            review_guidelines="No TODO comments. All functions need docstrings.",
        )
        assert "TODO" in reviewers.review_guidelines

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Reviewer pool + guidelines survive YAML serialization."""
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                reviewers=ReviewersConfig(
                    claude_sdk=PoolEntry(count=0),
                    codex=PoolEntry(count=2, model="gpt-5.4"),
                    review_guidelines="Must have tests",
                ),
            ),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.agents.reviewers.claude_sdk.count == 0
        assert loaded.agents.reviewers.codex.count == 2
        assert loaded.agents.reviewers.codex.model == "gpt-5.4"
        assert loaded.agents.reviewers.review_guidelines == "Must have tests"


class TestReviewerAgentRegistration:
    """Reviewer pool entries are included in setup registration."""

    def test_reviewers_in_expected_agents(self):
        """_expected_agents includes reviewer pool workers with display names."""
        from codeband.orchestration.setup import _expected_agents

        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        expected = _expected_agents(config)

        # Default config has one reviewer per framework.
        assert "reviewer-claude_sdk-0" in expected
        assert "reviewer-codex-0" in expected

        claude_name, _ = expected["reviewer-claude_sdk-0"]
        codex_name, _ = expected["reviewer-codex-0"]
        assert claude_name == "Reviewer-Claude-0"
        assert codex_name == "Reviewer-Codex-0"

    def test_pool_agents_detected_as_codeband_agents(self):
        """_is_codeband_agent recognizes the new pool naming conventions."""
        from codeband.orchestration.setup import _is_codeband_agent

        assert _is_codeband_agent("Reviewer-Claude-0")
        assert _is_codeband_agent("Reviewer-Codex-2")
        assert _is_codeband_agent("Coder-Claude-0")
        assert _is_codeband_agent("Planner-Claude-0")
        assert _is_codeband_agent("Plan-Reviewer-Codex-0")
        assert _is_codeband_agent("Conductor")
        assert _is_codeband_agent("Mergemaster")
        # Legacy names still matched so old accounts can be cleaned up.
        assert _is_codeband_agent("Player-0")
        assert _is_codeband_agent("Watchdog")
        # Non-Codeband agent shouldn't match.
        assert not _is_codeband_agent("UnrelatedAgent")


class TestRunnerPermissions:
    """Permission wiring for reviewer and planning runners (unchanged by pool refactor)."""

    def test_claude_code_reviewer_bypasses_permissions(self, tmp_path: Path):
        """Claude reviewer bypasses global settings to avoid deny overrides."""
        from codeband.agents.code_reviewer import ClaudeCodeReviewerRunner

        runner = ClaudeCodeReviewerRunner(workspace=str(tmp_path))
        adapter = runner.adapter

        assert adapter.permission_mode == "bypassPermissions"
        assert adapter.cwd == str(tmp_path)

    def test_codex_code_reviewer_uses_full_access_sandbox(self, tmp_path: Path):
        """Codex reviewer needs full access for gh CLI network calls."""
        from codeband.agents.code_reviewer import CodexCodeReviewerRunner

        runner = CodexCodeReviewerRunner(workspace=str(tmp_path))
        config = runner.adapter.config

        assert config.cwd == str(tmp_path)
        assert config.sandbox == "danger-full-access"
        assert config.approval_policy == "never"

    def test_claude_planner_denies_outside_allowlist(self, tmp_path: Path):
        """Claude planner uses the CLI's ``dontAsk`` mode so `.claude/settings.json`
        is the source of truth — no ``approval_mode`` callback overriding it.
        """
        from codeband.agents.planner import ClaudePlannerRunner

        runner = ClaudePlannerRunner(workspace=str(tmp_path))
        adapter = runner.adapter

        assert adapter.permission_mode == "dontAsk"
        assert adapter.approval_mode is None

    def test_codex_planner_uses_read_only_sandbox(self, tmp_path: Path):
        """Codex planner runs read-only — it analyzes the repo, never mutates it.

        Symmetric with the Codex plan reviewer below. Cross-model adversarial
        pairing means a Codex planner's plan can be reviewed by a Claude
        plan reviewer, and vice versa.
        """
        from codeband.agents.planner import CodexPlannerRunner

        runner = CodexPlannerRunner(workspace=str(tmp_path))
        config = runner.adapter.config

        assert config.cwd == str(tmp_path)
        assert config.sandbox == "read-only"
        assert config.approval_policy == "never"

    def test_codex_plan_reviewer_uses_read_only_sandbox(self, tmp_path: Path):
        """Codex plan reviewer runs in a read-only sandbox."""
        from codeband.agents.plan_reviewer import CodexPlanReviewerRunner

        runner = CodexPlanReviewerRunner(workspace=str(tmp_path))
        config = runner.adapter.config

        assert config.cwd == str(tmp_path)
        assert config.sandbox == "read-only"
        assert config.approval_policy == "never"

    def test_codex_conductor_uses_isolated_scratch_cwd(self, tmp_path: Path, monkeypatch):
        """Codex conductor must not inherit the repo cwd."""
        from codeband.agents.conductor import CodexConductorRunner

        monkeypatch.chdir(tmp_path)
        runner = CodexConductorRunner()
        config = runner.adapter.config

        assert config.cwd is not None
        assert config.cwd != os.getcwd()
        assert Path(config.cwd).is_dir()
        assert Path(config.cwd).name.startswith("codeband-conductor-")
        assert config.sandbox == "read-only"
        assert config.approval_policy == "never"

    def test_codex_player_uses_full_access_sandbox(self, tmp_path: Path):
        """Codex coder needs full access for git operations and network."""
        from codeband.agents.player_codex import CodexPlayerRunner

        runner = CodexPlayerRunner(workspace=str(tmp_path))
        config = runner.adapter.config

        assert config.cwd == str(tmp_path)
        assert config.sandbox == "danger-full-access"
        assert config.approval_policy == "never"
