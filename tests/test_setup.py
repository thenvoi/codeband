"""Tests for codeband.orchestration.setup — agent registration with reuse."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from codeband.config import (
    AgentConfigFile,
    AgentCredentials,
    AgentsConfig,
    CodebandConfig,
    FrameworkPool,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
)


# --- Fakes for Band.ai REST responses ---


@dataclass
class FakeAgent:
    id: str
    name: str
    description: str = ""
    is_external: bool = True


@dataclass
class FakeCredentials:
    api_key: str


@dataclass
class FakeRegisterData:
    agent: FakeAgent
    credentials: FakeCredentials


@dataclass
class FakeRegisterResponse:
    data: FakeRegisterData


@dataclass
class FakeListResponse:
    data: list[FakeAgent] = field(default_factory=list)


@dataclass
class FakeDeleteResponse:
    id: str
    name: str


def _make_config(
    *,
    claude_coders: int = 1,
    codex_coders: int = 1,
    claude_reviewers: int = 1,
    codex_reviewers: int = 1,
) -> CodebandConfig:
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git"),
        agents=AgentsConfig(
            planners=FrameworkPool(claude_sdk=PoolEntry(count=1)),
            plan_reviewers=PlanReviewersConfig(codex=PoolEntry(count=1)),
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=claude_coders),
                codex=PoolEntry(count=codex_coders),
            ),
            reviewers=ReviewersConfig(
                claude_sdk=PoolEntry(count=claude_reviewers),
                codex=PoolEntry(count=codex_reviewers),
            ),
        ),
    )


# The default cross-model config has 8 agents:
# conductor, mergemaster, planner-claude_sdk-0, plan_reviewer-codex-0,
# coder-claude_sdk-0, coder-codex-0, reviewer-claude_sdk-0, reviewer-codex-0.
_DEFAULT_PLATFORM_AGENTS = [
    FakeAgent(id="cond-0", name="Conductor"),
    FakeAgent(id="mm-0", name="Mergemaster"),
    FakeAgent(id="pl-c-0", name="Planner-Claude-0"),
    FakeAgent(id="pr-x-0", name="Plan-Reviewer-Codex-0"),
    FakeAgent(id="co-c-0", name="Coder-Claude-0"),
    FakeAgent(id="co-x-0", name="Coder-Codex-0"),
    FakeAgent(id="re-c-0", name="Reviewer-Claude-0"),
    FakeAgent(id="re-x-0", name="Reviewer-Codex-0"),
]

_DEFAULT_AGENT_CONFIG = AgentConfigFile(agents={
    "conductor": AgentCredentials(agent_id="cond-0", api_key="key-cond"),
    "mergemaster": AgentCredentials(agent_id="mm-0", api_key="key-mm"),
    "planner-claude_sdk-0": AgentCredentials(agent_id="pl-c-0", api_key="key-pl-c0"),
    "plan_reviewer-codex-0": AgentCredentials(agent_id="pr-x-0", api_key="key-pr-x0"),
    "coder-claude_sdk-0": AgentCredentials(agent_id="co-c-0", api_key="key-co-c0"),
    "coder-codex-0": AgentCredentials(agent_id="co-x-0", api_key="key-co-x0"),
    "reviewer-claude_sdk-0": AgentCredentials(agent_id="re-c-0", api_key="key-re-c0"),
    "reviewer-codex-0": AgentCredentials(agent_id="re-x-0", api_key="key-re-x0"),
})


class TestSetupReusesExistingAgents:
    """When agents already exist on the platform and in agent_config.yaml, reuse them."""

    @pytest.mark.asyncio
    async def test_no_registration_when_all_exist(self, tmp_path):
        """If all required agents exist on platform with valid credentials, register nothing."""
        from codeband.orchestration.setup import register_all_agents

        config = _make_config()
        _DEFAULT_AGENT_CONFIG.to_yaml(tmp_path / "agent_config.yaml")

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(
            data=list(_DEFAULT_PLATFORM_AGENTS),
        )

        await register_all_agents(config, tmp_path, client=client)

        client.human_api_agents.register_my_agent.assert_not_called()
        client.human_api_agents.delete_my_agent.assert_not_called()

        result = AgentConfigFile.from_yaml(tmp_path / "agent_config.yaml")
        assert result.agents["conductor"].agent_id == "cond-0"
        assert result.agents["coder-claude_sdk-0"].api_key == "key-co-c0"

    @pytest.mark.asyncio
    async def test_registers_missing_agents(self, tmp_path):
        """If some agents are missing, register only those."""
        from codeband.orchestration.setup import register_all_agents

        config = _make_config()

        # Only conductor and mergemaster exist
        existing_agents = [
            FakeAgent(id="cond-0", name="Conductor"),
            FakeAgent(id="mm-0", name="Mergemaster"),
        ]
        existing_config = AgentConfigFile(agents={
            "conductor": AgentCredentials(agent_id="cond-0", api_key="key-cond"),
            "mergemaster": AgentCredentials(agent_id="mm-0", api_key="key-mm"),
        })
        existing_config.to_yaml(tmp_path / "agent_config.yaml")

        register_count = 0

        async def fake_register(*, agent, **kwargs):
            nonlocal register_count
            register_count += 1
            return FakeRegisterResponse(
                data=FakeRegisterData(
                    agent=FakeAgent(id=f"new-{register_count}", name=agent.name),
                    credentials=FakeCredentials(api_key=f"newkey-{register_count}"),
                )
            )

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(
            data=existing_agents
        )
        client.human_api_agents.register_my_agent.side_effect = fake_register

        await register_all_agents(config, tmp_path, client=client)

        # Should register 6: planner, plan_reviewer, 2 coders, 2 reviewers.
        assert client.human_api_agents.register_my_agent.call_count == 6


class TestSetupDeletesExcess:
    """When config shrinks, delete the excess agents."""

    @pytest.mark.asyncio
    async def test_excess_coders_deleted(self, tmp_path):
        """Reducing from 2 Claude coders to 1 should delete coder-claude_sdk-1."""
        from codeband.orchestration.setup import register_all_agents

        config = _make_config(claude_coders=1, codex_coders=0, codex_reviewers=0)

        # Platform has extras: extra Claude coder, and legacy Watchdog
        existing_agents = [
            FakeAgent(id="cond-0", name="Conductor"),
            FakeAgent(id="mm-0", name="Mergemaster"),
            FakeAgent(id="pl-c-0", name="Planner-Claude-0"),
            FakeAgent(id="pr-x-0", name="Plan-Reviewer-Codex-0"),
            FakeAgent(id="co-c-0", name="Coder-Claude-0"),
            FakeAgent(id="co-c-1", name="Coder-Claude-1"),  # excess
            FakeAgent(id="re-c-0", name="Reviewer-Claude-0"),
            FakeAgent(id="wd-0", name="Watchdog"),          # legacy
        ]
        existing_config = AgentConfigFile(agents={
            "conductor": AgentCredentials(agent_id="cond-0", api_key="key-cond"),
            "mergemaster": AgentCredentials(agent_id="mm-0", api_key="key-mm"),
            "planner-claude_sdk-0": AgentCredentials(agent_id="pl-c-0", api_key="k"),
            "plan_reviewer-codex-0": AgentCredentials(agent_id="pr-x-0", api_key="k"),
            "coder-claude_sdk-0": AgentCredentials(agent_id="co-c-0", api_key="k"),
            "coder-claude_sdk-1": AgentCredentials(agent_id="co-c-1", api_key="k"),
            "reviewer-claude_sdk-0": AgentCredentials(agent_id="re-c-0", api_key="k"),
        })
        existing_config.to_yaml(tmp_path / "agent_config.yaml")

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(
            data=existing_agents
        )
        client.human_api_agents.delete_my_agent.return_value = FakeDeleteResponse(
            id="deleted", name="deleted"
        )

        await register_all_agents(config, tmp_path, client=client)

        # Should delete the excess Coder-Claude-1 and the legacy Watchdog.
        deleted_ids = {
            call.args[0] for call in client.human_api_agents.delete_my_agent.call_args_list
        }
        assert deleted_ids == {"co-c-1", "wd-0"}

        result = AgentConfigFile.from_yaml(tmp_path / "agent_config.yaml")
        assert "coder-claude_sdk-0" in result.agents
        assert "coder-claude_sdk-1" not in result.agents


class TestSetupFreshInstall:
    """When no agents exist, register everything from scratch."""

    @pytest.mark.asyncio
    async def test_registers_all_from_scratch(self, tmp_path):
        """Fresh install with no existing agents registers all."""
        from codeband.orchestration.setup import register_all_agents

        config = _make_config()

        register_count = 0

        async def fake_register(*, agent, **kwargs):
            nonlocal register_count
            register_count += 1
            return FakeRegisterResponse(
                data=FakeRegisterData(
                    agent=FakeAgent(id=f"new-{register_count}", name=agent.name),
                    credentials=FakeCredentials(api_key=f"newkey-{register_count}"),
                )
            )

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(data=[])
        client.human_api_agents.register_my_agent.side_effect = fake_register

        await register_all_agents(config, tmp_path, client=client)

        # Default config has 8 agents total.
        assert client.human_api_agents.register_my_agent.call_count == 8

        result = AgentConfigFile.from_yaml(tmp_path / "agent_config.yaml")
        assert len(result.agents) == 8
