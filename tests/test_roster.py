"""Tests for the canonical agent roster registry."""

from __future__ import annotations

import pytest

from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    Framework,
    FrameworkPool,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
)
from codeband.models import CLAUDE_OPUS, CLAUDE_SONNET, CODEX_GPT
from codeband import roster


def _config(agents: AgentsConfig | None = None) -> CodebandConfig:
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/x/y"),
        agents=agents or AgentsConfig(),
    )


class TestCompletenessGuard:
    def test_every_agent_field_is_registered(self):
        """Anti-rot: each agent-shaped AgentsConfig field must have a RoleSpec.
        Add a field to AgentsConfig without a RoleSpec and this fails."""
        agents = AgentsConfig()
        for name in roster._agent_field_names(agents):
            assert name in roster._SPEC_BY_ATTR, f"{name} has no RoleSpec"

    def test_watchdog_is_not_treated_as_an_agent(self):
        # watchdog has neither the singleton nor the pool shape.
        assert "watchdog" not in roster._agent_field_names(AgentsConfig())

    def test_iter_agents_raises_on_unregistered_field(self, monkeypatch):
        trimmed = dict(roster._SPEC_BY_ATTR)
        trimmed.pop("coders")
        monkeypatch.setattr(roster, "_SPEC_BY_ATTR", trimmed)
        with pytest.raises(ValueError, match="coders"):
            list(roster.iter_agents(_config()))


class TestIterAgents:
    def test_default_config_yields_expected_keys(self):
        keys = {info.key for info in roster.iter_agents(_config())}
        assert keys == {
            "conductor",
            "mergemaster",
            "planner-claude_sdk-0",
            "plan_reviewer-codex-0",
            "coder-claude_sdk-0",
            "coder-codex-0",
            "reviewer-claude_sdk-0",
            "reviewer-codex-0",
        }

    def test_pool_model_defaults_match_spawner(self):
        # Claude coder/reviewer with model unset → Opus for coders, Sonnet for reviewers.
        agents = AgentsConfig(
            coders=FrameworkPool(claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)),
            reviewers=ReviewersConfig(claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)),
        )
        by_key = {i.key: i for i in roster.iter_agents(_config(agents))}
        assert by_key["coder-claude_sdk-0"].resolved_model == CLAUDE_OPUS
        assert by_key["reviewer-claude_sdk-0"].resolved_model == CLAUDE_SONNET

    def test_explicit_model_is_honored(self):
        agents = AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-opus-4-7"),
                codex=PoolEntry(count=0),
            ),
        )
        by_key = {i.key: i for i in roster.iter_agents(_config(agents))}
        assert by_key["coder-claude_sdk-0"].resolved_model == "claude-opus-4-7"

    def test_codex_model_default(self):
        info = next(
            i for i in roster.iter_agents(_config()) if i.key == "coder-codex-0"
        )
        assert info.framework == Framework.CODEX
        assert info.resolved_model == CODEX_GPT


class TestDerivedHelpers:
    def test_claude_models_distinct_and_ordered(self):
        # Default: conductor/mergemaster/planner/reviewer Sonnet + coder Opus.
        assert roster.claude_models(_config()) == [CLAUDE_SONNET, CLAUDE_OPUS]

    def test_uses_codex(self):
        assert roster.uses_codex(_config()) is True
        claude_only = AgentsConfig(
            planners=FrameworkPool(claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)),
            plan_reviewers=PlanReviewersConfig(
                claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)
            ),
            coders=FrameworkPool(claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)),
            reviewers=ReviewersConfig(claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)),
        )
        assert roster.uses_codex(_config(claude_only)) is False

    def test_total_agent_count_matches_iteration(self):
        cfg = _config()
        assert roster.total_agent_count(cfg) == len(list(roster.iter_agents(cfg)))
        assert roster.total_agent_count(cfg) == 8  # 2 singletons + 1+1+2+2 pools

    def test_pool_and_singleton_views(self):
        assert roster.pool_attrs() == {"planners", "plan_reviewers", "coders", "reviewers"}
        assert roster.singleton_keys() == ("conductor", "mergemaster")
        assert roster.pool_role_values() == {"planner", "plan_reviewer", "coder", "reviewer"}

    def test_review_pairs(self):
        pairs = {frozenset(p) for p in roster.review_pairs()}
        assert pairs == {
            frozenset({"planners", "plan_reviewers"}),
            frozenset({"coders", "reviewers"}),
        }

    def test_display_prefixes(self):
        prefixes = roster.display_prefixes()
        for expected in ("Planner-", "Plan-Reviewer-", "Coder-", "Reviewer-", "Player-"):
            assert expected in prefixes
