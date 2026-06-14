"""Shared test fixtures for Codeband."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
    VerifiersConfig,
    WatchdogConfig,
    WorkspaceConfig,
)


_CONTAMINATING_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEBAND_FALLBACK_ANTHROPIC_API_KEY",
    "CODEBAND_FALLBACK_OPENAI_API_KEY",
    "CODEBAND_AGENT_SESSION",
)


@pytest.fixture(autouse=True)
def _isolate_dotenv(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Block host .env files from leaking into the test process.

    ``_load_project_dotenv`` in cli/__init__.py calls
    ``load_dotenv(find_dotenv(usecwd=True))`` which walks upward from CWD and
    can pick up the project's own .env (or any ancestor's) while tests are
    running. Patch ``load_dotenv`` to a no-op so CLI invocations inside tests
    never load real credentials, and pre-clear the known contaminating vars so
    no prior test or shell export bleeds through.

    Tests that specifically exercise real .env loading should opt out by marking
    with ``@pytest.mark.real_dotenv`` (or a module-level ``pytestmark``).
    """
    if request.node.get_closest_marker("real_dotenv"):
        yield
        return

    with patch("codeband.cli.load_dotenv"):
        for var in _CONTAMINATING_VARS:
            monkeypatch.delenv(var, raising=False)
        yield


@pytest.fixture
def sample_config(tmp_path: Path) -> CodebandConfig:
    """A minimal CodebandConfig for testing.

    Cross-model defaults: 1 Claude coder + 1 Codex coder, 1 of each
    reviewer, 1 Claude planner + 1 Codex plan-reviewer. Conductor +
    Mergemaster default to Claude. Verifiers pinned INERT so this fixture
    stays a coherent 8-agent pair with ``sample_agent_config`` (which carries
    no verifier creds); verifier wiring has its own dedicated tests. Total: 8
    Band.ai agents.
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
            verifiers=VerifiersConfig(),
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
