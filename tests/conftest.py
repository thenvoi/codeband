"""Shared test fixtures for Codeband."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeband.config import (
    AgentConfigFile,
    AgentCredentials,
    AgentsConfig,
    CodebandConfig,
    ConductorConfig,
    FrameworkPool,
    MergemasterConfig,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
    WatchdogConfig,
    WorkspaceConfig,
)


@pytest.fixture
def sample_config(tmp_path: Path) -> CodebandConfig:
    """A minimal CodebandConfig for testing.

    Cross-model defaults: 1 Claude coder + 1 Codex coder, 1 of each
    reviewer, 1 Claude planner + 1 Codex plan-reviewer. Conductor +
    Mergemaster default to Claude. Total: 8 Band.ai agents.
    """
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git", branch="main"),
        agents=AgentsConfig(
            conductor=ConductorConfig(model="claude-sonnet-4-6"),
            mergemaster=MergemasterConfig(),
            planners=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
            ),
            plan_reviewers=PlanReviewersConfig(
                codex=PoolEntry(count=1, model="gpt-5.4"),
            ),
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=1, model="gpt-5.4"),
            ),
            reviewers=ReviewersConfig(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=1, model="gpt-5.4"),
            ),
            watchdog=WatchdogConfig(),
        ),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
    )


@pytest.fixture
def sample_agent_config() -> AgentConfigFile:
    """Sample agent credentials matching the default cross-model pool.

    Watchdog is intentionally absent — it reuses Conductor credentials.
    Keys follow the `{role}-{framework}-{index}` convention that Phase B
    introduces alongside the pool schema.
    """
    return AgentConfigFile(agents={
        "conductor":          AgentCredentials(agent_id="cond-0",  api_key="key-cond"),
        "mergemaster":        AgentCredentials(agent_id="mm-0",    api_key="key-mm"),
        "planner-claude_sdk-0":    AgentCredentials(agent_id="pl-c-0",  api_key="key-pl-c0"),
        "plan_reviewer-codex-0":   AgentCredentials(agent_id="pr-x-0",  api_key="key-pr-x0"),
        "coder-claude_sdk-0":      AgentCredentials(agent_id="co-c-0",  api_key="key-co-c0"),
        "coder-codex-0":           AgentCredentials(agent_id="co-x-0",  api_key="key-co-x0"),
        "reviewer-claude_sdk-0":   AgentCredentials(agent_id="re-c-0",  api_key="key-re-c0"),
        "reviewer-codex-0":        AgentCredentials(agent_id="re-x-0",  api_key="key-re-x0"),
    })
