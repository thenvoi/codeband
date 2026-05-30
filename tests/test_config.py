"""Tests for codeband.config module."""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from codeband.config import (
    AgentConfigFile,
    AgentCredentials,
    AgentsConfig,
    ClaudeConfig,
    CodebandConfig,
    ConductorConfig,
    DeploymentMode,
    Framework,
    FrameworkPool,
    MergemasterConfig,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
    WorkspaceConfig,
    load_config,
    scale_pool,
)


class TestCodebandConfig:
    """Tests for CodebandConfig model."""

    def test_minimal_config_has_default_pools(self):
        """Config with only required field (repo.url) emits cross-model defaults."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.repo.url == "https://github.com/a/b.git"
        assert config.repo.branch == "main"
        # Default: 1 Claude + 1 Codex coder.
        assert config.agents.coders.total_count() == 2
        assert config.agents.reviewers.total_count() == 2
        # Cross-model by default: planner=Claude, plan_reviewer=Codex.
        assert config.agents.planners.active_frameworks() == [Framework.CLAUDE_SDK]
        assert config.agents.plan_reviewers.active_frameworks() == [Framework.CODEX]

    def test_default_model_split_coders_opus_rest_sonnet(self):
        """Coders default to the heavier Opus model; other Claude roles stay on Sonnet.

        Coding is the reasoning-heavy role where paying for Opus pays off.
        Planners, reviewers, and coordinators handle lighter work and stay
        on Sonnet for better cost/latency.
        """
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.agents.coders.claude_sdk.model == "claude-opus-4-7"
        assert config.agents.coders.codex.model == "gpt-5.4"
        assert config.agents.reviewers.claude_sdk.model == "claude-sonnet-4-6"
        assert config.agents.planners.claude_sdk.model == "claude-sonnet-4-6"
        assert config.agents.conductor.model == "claude-sonnet-4-6"
        assert config.agents.mergemaster.model == "claude-sonnet-4-6"

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Config survives YAML serialization."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)

        assert loaded.repo.url == config.repo.url
        assert loaded.repo.branch == config.repo.branch
        assert loaded.agents.coders.total_count() == config.agents.coders.total_count()
        assert (
            loaded.agents.reviewers.total_count()
            == config.agents.reviewers.total_count()
        )

    def test_custom_pools(self):
        """AgentsConfig accepts custom pool capacities."""
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=3, model="claude-opus-4-7"),
                    codex=PoolEntry(count=0),
                ),
            ),
        )
        assert config.agents.coders.claude_sdk.count == 3
        assert config.agents.coders.claude_sdk.model == "claude-opus-4-7"
        assert config.agents.coders.codex.count == 0
        assert config.agents.coders.active_frameworks() == [Framework.CLAUDE_SDK]

    def test_load_config_missing_file(self, tmp_path: Path):
        """load_config raises on missing file."""
        with pytest.raises(FileNotFoundError, match="codeband.yaml not found"):
            load_config(tmp_path)


class TestAgentConfigFile:
    """Tests for agent credentials file."""

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Credentials survive YAML serialization."""
        config = AgentConfigFile(agents={
            "conductor": AgentCredentials(agent_id="abc", api_key="key-1"),
            "coder-claude_sdk-0": AgentCredentials(agent_id="def", api_key="key-2"),
        })
        yaml_path = tmp_path / "agent_config.yaml"
        config.to_yaml(yaml_path)
        loaded = AgentConfigFile.from_yaml(yaml_path)

        assert loaded.agents["conductor"].agent_id == "abc"
        assert loaded.agents["coder-claude_sdk-0"].api_key == "key-2"

    def test_get_missing_key_raises(self):
        """Getting a missing key raises with helpful message."""
        config = AgentConfigFile(agents={
            "conductor": AgentCredentials(agent_id="abc", api_key="key"),
        })
        with pytest.raises(KeyError, match="coder-claude_sdk-0"):
            config.get("coder-claude_sdk-0")

    def test_get_existing_key(self):
        """Getting an existing key returns credentials."""
        config = AgentConfigFile(agents={
            "conductor": AgentCredentials(agent_id="abc", api_key="key"),
        })
        creds = config.get("conductor")
        assert creds.agent_id == "abc"


class TestPoolEntry:
    """Tests for PoolEntry primitive."""

    def test_defaults(self):
        e = PoolEntry()
        assert e.count == 0
        assert e.model is None
        assert e.max_restarts == 5

    def test_with_overrides(self):
        e = PoolEntry(count=3, model="x", max_restarts=10)
        assert e.count == 3
        assert e.model == "x"
        assert e.max_restarts == 10

    def test_ignores_legacy_description_field_for_back_compat(self):
        """0.1.0 wrote a `description` field here; 0.1.1 removed it. Old
        codeband.yaml files must still load without raising — the field is
        silently dropped, not preserved."""
        e = PoolEntry(count=1, description="old 0.1.0 description")
        assert e.count == 1
        assert not hasattr(e, "description")
        # Round-tripping through model_dump() also drops the legacy field, so
        # the next `to_yaml` write produces a clean 0.1.1 file.
        assert "description" not in e.model_dump()

    def test_ignores_unknown_legacy_fields_yaml_roundtrip(self):
        """End-to-end: yaml with a `description: null` (the most common 0.1.0
        shape) round-trips through FrameworkPool without raising."""
        import yaml as _yaml

        legacy_yaml = """
claude_sdk:
  count: 2
  model: claude-x
  description: null
codex:
  count: 1
  model: gpt-5
  description: Some old prose from 0.1.0
"""
        loaded = FrameworkPool.model_validate(_yaml.safe_load(legacy_yaml))
        assert loaded.claude_sdk.count == 2
        assert loaded.claude_sdk.model == "claude-x"
        assert loaded.codex.count == 1
        # Round-trip dumps a clean 0.1.1 file with no `description` key.
        dumped = _yaml.safe_dump(loaded.model_dump(mode="json"))
        assert "description" not in dumped


class TestFrameworkPool:
    """Tests for FrameworkPool."""

    def test_defaults_are_empty(self):
        fp = FrameworkPool()
        assert fp.total_count() == 0
        assert fp.active_frameworks() == []

    def test_active_frameworks_preserves_deterministic_order(self):
        fp = FrameworkPool(
            codex=PoolEntry(count=2),
            claude_sdk=PoolEntry(count=1),
        )
        # Always Claude-first regardless of construction order.
        assert fp.active_frameworks() == [Framework.CLAUDE_SDK, Framework.CODEX]

    def test_entry_for_returns_correct_framework(self):
        fp = FrameworkPool(
            claude_sdk=PoolEntry(count=1, model="claude-x"),
            codex=PoolEntry(count=2, model="gpt-5"),
        )
        assert fp.entry_for(Framework.CLAUDE_SDK).model == "claude-x"
        assert fp.entry_for(Framework.CODEX).model == "gpt-5"

    def test_yaml_roundtrip(self):
        import yaml

        fp = FrameworkPool(
            claude_sdk=PoolEntry(count=2, model="claude-x"),
            codex=PoolEntry(count=1),
        )
        dumped = yaml.safe_dump(fp.model_dump(mode="json"))
        loaded = FrameworkPool.model_validate(yaml.safe_load(dumped))
        assert loaded.claude_sdk.count == 2
        assert loaded.claude_sdk.model == "claude-x"
        assert loaded.codex.count == 1


class TestReviewersConfig:
    """Tests for ReviewersConfig — FrameworkPool + project-wide review policy."""

    def test_review_guidelines_defaults_to_none(self):
        rc = ReviewersConfig()
        assert rc.review_guidelines is None
        assert rc.total_count() == 0

    def test_review_guidelines_roundtrip(self, tmp_path: Path):
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                reviewers=ReviewersConfig(
                    claude_sdk=PoolEntry(count=1),
                    review_guidelines="All functions need docstrings",
                ),
            ),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert (
            loaded.agents.reviewers.review_guidelines
            == "All functions need docstrings"
        )


class TestTotalAgentCount:
    """Tests for AgentsConfig.total_agent_count — drives tier-cap warnings."""

    def test_default_is_eight(self):
        """Default cross-model config uses exactly 8 agents (fits free-tier 10 cap)."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.agents.total_agent_count() == 8

    def test_scales_with_pool_counts(self):
        agents = AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=3),
                codex=PoolEntry(count=3),
            ),
            reviewers=ReviewersConfig(
                claude_sdk=PoolEntry(count=2),
                codex=PoolEntry(count=2),
            ),
        )
        # 2 singletons + 1 Claude planner + 1 Codex plan-reviewer + 6 coders + 4 reviewers
        assert agents.total_agent_count() == 2 + 1 + 1 + 6 + 4

    def test_zero_count_reduces_total(self):
        agents = AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),  # Claude-only user
            ),
            reviewers=ReviewersConfig(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
            plan_reviewers=PlanReviewersConfig(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
        )
        # 2 singletons + 1 planner + 1 plan_reviewer + 1 coder + 1 reviewer
        assert agents.total_agent_count() == 6


class TestScalePool:
    """Tests for the scale_pool helper (replaces scale_players)."""

    def _fresh(self, tmp_path: Path) -> Path:
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        config_path = tmp_path / "codeband.yaml"
        config.to_yaml(config_path)
        return config_path

    def test_scale_up_claude_coders(self, tmp_path: Path):
        path = self._fresh(tmp_path)
        updated = scale_pool(path, "coders", Framework.CLAUDE_SDK, 4)
        assert updated.agents.coders.claude_sdk.count == 4
        # Codex side preserved at default 1.
        assert updated.agents.coders.codex.count == 1
        # Persisted.
        reloaded = CodebandConfig.from_yaml(path)
        assert reloaded.agents.coders.claude_sdk.count == 4

    def test_scale_to_zero_opts_out(self, tmp_path: Path):
        path = self._fresh(tmp_path)
        updated = scale_pool(path, "coders", Framework.CODEX, 0)
        assert updated.agents.coders.codex.count == 0
        assert updated.agents.coders.active_frameworks() == [Framework.CLAUDE_SDK]

    def test_preserves_model(self, tmp_path: Path):
        """Scaling keeps the existing entry's model setting."""
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=1, model="claude-opus-4-7"),
                ),
            ),
        )
        path = tmp_path / "codeband.yaml"
        config.to_yaml(path)

        updated = scale_pool(path, "coders", Framework.CLAUDE_SDK, 3)
        assert updated.agents.coders.claude_sdk.model == "claude-opus-4-7"
        assert updated.agents.coders.claude_sdk.count == 3

    def test_negative_count_rejected(self, tmp_path: Path):
        path = self._fresh(tmp_path)
        with pytest.raises(ValueError, match=">= 0"):
            scale_pool(path, "coders", Framework.CLAUDE_SDK, -1)

    def test_unknown_pool_rejected(self, tmp_path: Path):
        path = self._fresh(tmp_path)
        with pytest.raises(ValueError, match="Unknown pool"):
            scale_pool(path, "not_a_pool", Framework.CLAUDE_SDK, 1)


class TestDeploymentMode:
    """Tests for deployment mode configuration."""

    def test_default_is_local(self):
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.workspace.mode == DeploymentMode.LOCAL

    def test_distributed_yaml_roundtrip(self, tmp_path: Path):
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            workspace=WorkspaceConfig(mode=DeploymentMode.DISTRIBUTED),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.workspace.mode == DeploymentMode.DISTRIBUTED


class TestConductorConfig:
    """Tests for conductor (single-instance coordinator)."""

    def test_defaults(self):
        c = ConductorConfig()
        assert c.framework == Framework.CLAUDE_SDK
        assert c.model == "claude-sonnet-4-6"

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Conductor model override survives YAML serialization.

        Note: Conductor framework must be claude_sdk today — the validator
        rejects Codex because CodexConductor isn't implemented yet. This
        test exercises the other tunables (model).
        """
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                conductor=ConductorConfig(
                    framework=Framework.CLAUDE_SDK,
                    model="claude-opus-4-7",
                ),
            ),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.agents.conductor.framework == Framework.CLAUDE_SDK
        assert loaded.agents.conductor.model == "claude-opus-4-7"


class TestCodexFrameworkSupport:
    """Every role now supports both Claude and Codex frameworks.

    Planner was the last role to gain Codex support (the validator that
    forbade ``planners.codex.count > 0`` has been removed). These tests
    pin the acceptance contract so a future change can't silently re-add
    a per-role gate.
    """

    def test_planners_codex_accepted(self):
        """Codex Planner is now supported — the config-load gate is gone."""
        cfg = AgentsConfig(
            planners=FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=1, model="gpt-5.4"),
            ),
        )
        assert cfg.planners.codex.count == 1
        assert cfg.planners.codex.model == "gpt-5.4"

    def test_planners_claude_only_still_fine(self):
        """The default shape (Claude planner, Codex plan-reviewer) keeps working."""
        cfg = AgentsConfig(
            planners=FrameworkPool(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
        )
        assert cfg.planners.codex.count == 0
        assert cfg.planners.claude_sdk.count == 1

    def test_conductor_codex_accepted(self):
        cfg = AgentsConfig(conductor=ConductorConfig(framework=Framework.CODEX))
        assert cfg.conductor.framework == Framework.CODEX

    def test_mergemaster_codex_accepted(self):
        from codeband.config import MergemasterConfig

        cfg = AgentsConfig(mergemaster=MergemasterConfig(framework=Framework.CODEX))
        assert cfg.mergemaster.framework == Framework.CODEX

    def test_scale_pool_accepts_planners_codex(self, tmp_path):
        """scale_pool now mutates planners.codex to count>0 successfully."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        config_path = tmp_path / "codeband.yaml"
        config.to_yaml(config_path)

        scale_pool(config_path, "planners", Framework.CODEX, 1)

        reloaded = CodebandConfig.from_yaml(config_path)
        assert reloaded.agents.planners.codex.count == 1


class TestMergemasterConfig:
    """Tests for mergemaster (single-instance coordinator with merge policy)."""

    def test_defaults(self):
        mm = MergemasterConfig()
        assert mm.framework == Framework.CLAUDE_SDK
        assert mm.test_command is None
        assert mm.review_guidelines is None

    def test_review_guidelines_roundtrip(self, tmp_path: Path):
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                mergemaster=MergemasterConfig(
                    review_guidelines="Must have tests",
                ),
            ),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.agents.mergemaster.review_guidelines == "Must have tests"


class TestClaudeConfig:
    """Claude auth policy — API-key-first by default, subscription as opt-in."""

    def test_default_is_api_key(self):
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.claude.auth_mode == "api_key"

    def test_legacy_config_without_claude_block_loads(self, tmp_path: Path):
        """Existing codeband.yaml files predate the `claude:` block."""
        yaml_path = tmp_path / "codeband.yaml"
        yaml_path.write_text(
            "repo:\n  url: https://github.com/a/b.git\n", encoding="utf-8",
        )
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.claude.auth_mode == "api_key"

    def test_subscription_roundtrip(self, tmp_path: Path):
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            claude=ClaudeConfig(auth_mode="subscription"),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.claude.auth_mode == "subscription"

    def test_init_default_yaml_emits_api_key(self, tmp_path: Path):
        """A freshly written default config persists auth_mode: api_key."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        assert "auth_mode: api_key" in yaml_path.read_text(encoding="utf-8")

    def test_invalid_auth_mode_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            CodebandConfig.model_validate({
                "repo": {"url": "https://github.com/a/b.git"},
                "claude": {"auth_mode": "subscriptions"},
            })
