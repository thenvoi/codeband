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
    VerifiersConfig,
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
            # Verifiers off so these setup/drift tests stay scoped to the
            # 8-agent set their fakes mirror; the verifier seat's expected-agent
            # wiring has dedicated tests in test_verifier.py.
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
            ),
        ),
    )


# The default cross-model config has 8 agents:
# conductor, mergemaster, planner-claude_sdk-0, plan_reviewer-codex-0,
# coder-claude_sdk-0, coder-codex-0, reviewer-claude_sdk-0, reviewer-codex-0.
#
# Build the platform-side fakes with the **expected** descriptions so drift
# detection does not see spurious drift in tests that exercise the happy path.
# Drift-specific tests construct their own fakes with intentionally divergent
# descriptions.

def _platform_agents_for(config: CodebandConfig) -> list[FakeAgent]:
    """Build FakeAgent records that mirror what `_expected_agents` produces."""
    from codeband.orchestration.setup import _expected_agents

    name_to_id = {
        "Conductor": "cond-0",
        "Mergemaster": "mm-0",
        "Planner-Claude-0": "pl-c-0",
        "Plan-Reviewer-Codex-0": "pr-x-0",
        "Coder-Claude-0": "co-c-0",
        "Coder-Codex-0": "co-x-0",
        "Reviewer-Claude-0": "re-c-0",
        "Reviewer-Codex-0": "re-x-0",
    }
    return [
        FakeAgent(id=name_to_id[name], name=name, description=desc)
        for _key, (name, desc) in _expected_agents(config).items()
        if name in name_to_id
    ]


def _expected_desc_for(config: CodebandConfig, agent_name: str) -> str:
    """Return the canonical platform description for `agent_name`, or "" if unknown."""
    from codeband.orchestration.setup import _expected_agents

    for _key, (name, desc) in _expected_agents(config).items():
        if name == agent_name:
            return desc
    return ""

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


def test_expected_agent_descriptions_have_discovery_tokens():
    """Peer discovery should match exact role/framework tokens, not prose."""
    from codeband.orchestration.setup import _expected_agents

    agents = _expected_agents(_make_config())

    assert "role=conductor_agent" in agents["conductor"][1]
    assert "role=merge_agent" in agents["mergemaster"][1]

    expected_role_tokens = {
        "planner-claude_sdk-0": "role=planning_agent",
        "plan_reviewer-codex-0": "role=plan_review_agent",
        "coder-claude_sdk-0": "role=coding_agent",
        "coder-codex-0": "role=coding_agent",
        "reviewer-claude_sdk-0": "role=code_review_agent",
        "reviewer-codex-0": "role=code_review_agent",
    }
    for key, token in expected_role_tokens.items():
        assert token in agents[key][1]

    assert "framework=Claude" in agents["coder-claude_sdk-0"][1]
    assert "framework=Codex" in agents["coder-codex-0"][1]
    assert "framework=Claude" in agents["reviewer-claude_sdk-0"][1]
    assert "framework=Codex" in agents["reviewer-codex-0"][1]


def test_expected_agent_descriptions_within_band_500_char_limit():
    """Band.ai's `register_my_agent` rejects descriptions over 500 chars with a
    422. `_expected_agents` must validate this at build time rather than at the
    API — fail fast with a precise pointer to the offending agent."""
    from codeband.orchestration.setup import _BAND_DESCRIPTION_MAX, _expected_agents

    # Build with the default test config — must succeed.
    agents = _expected_agents(_make_config())
    for key, (display, desc) in agents.items():
        assert len(desc) <= _BAND_DESCRIPTION_MAX, (
            f"{display} ({key}) is {len(desc)} chars > {_BAND_DESCRIPTION_MAX}"
        )


def test_oversized_description_raises_at_build_time(monkeypatch):
    """If a future edit makes a description too long, the build fails with a
    clear error rather than waiting until Band.ai returns 422."""
    from codeband.orchestration import setup as setup_mod

    # Inflate one singleton's description past the limit.
    bloated = ("X" * 600)
    monkeypatch.setitem(
        setup_mod._SINGLETON_AGENTS, "conductor", ("Conductor", bloated),
    )

    with pytest.raises(ValueError, match=r"exceeds Band.ai's 500-char limit"):
        setup_mod._expected_agents(_make_config())


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
            data=_platform_agents_for(config),
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

        # Only conductor and mergemaster exist — populate the canonical
        # descriptions so drift detection does not flag them for re-register.
        existing_agents = [
            FakeAgent(
                id="cond-0", name="Conductor",
                description=_expected_desc_for(config, "Conductor"),
            ),
            FakeAgent(
                id="mm-0", name="Mergemaster",
                description=_expected_desc_for(config, "Mergemaster"),
            ),
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

        # Platform has extras: extra Claude coder, and legacy Watchdog.
        # Expected agents are populated with the canonical descriptions so
        # drift detection only deletes the genuine excess (Coder-Claude-1
        # is excess regardless of description; Watchdog is legacy by name).
        existing_agents = [
            FakeAgent(
                id="cond-0", name="Conductor",
                description=_expected_desc_for(config, "Conductor"),
            ),
            FakeAgent(
                id="mm-0", name="Mergemaster",
                description=_expected_desc_for(config, "Mergemaster"),
            ),
            FakeAgent(
                id="pl-c-0", name="Planner-Claude-0",
                description=_expected_desc_for(config, "Planner-Claude-0"),
            ),
            FakeAgent(
                id="pr-x-0", name="Plan-Reviewer-Codex-0",
                description=_expected_desc_for(config, "Plan-Reviewer-Codex-0"),
            ),
            FakeAgent(
                id="co-c-0", name="Coder-Claude-0",
                description=_expected_desc_for(config, "Coder-Claude-0"),
            ),
            FakeAgent(id="co-c-1", name="Coder-Claude-1"),  # excess
            FakeAgent(
                id="re-c-0", name="Reviewer-Claude-0",
                description=_expected_desc_for(config, "Reviewer-Claude-0"),
            ),
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


class TestSetupDriftDetection:
    """When the registered description on Band.ai drifts from the canonical
    description in setup.py, the platform agent must be deleted and a fresh
    one registered (Band.ai has no in-place agent-update API, so re-registration
    is the only way to fix drift). The local cred for that key is also dropped
    so the new agent gets fresh credentials."""

    @pytest.mark.asyncio
    async def test_drift_triggers_delete_and_reregister(self, tmp_path):
        from codeband.orchestration.setup import register_all_agents

        config = _make_config()

        # Build the canonical platform set, then deliberately corrupt the
        # Coder-Codex-0 description so it no longer matches what setup.py
        # would register. Everything else is canonical.
        platform = _platform_agents_for(config)
        for i, agent in enumerate(platform):
            if agent.name == "Coder-Codex-0":
                platform[i] = FakeAgent(
                    id=agent.id,
                    name=agent.name,
                    description="OUTDATED — old description from a previous codeband version",
                )
                break

        _DEFAULT_AGENT_CONFIG.to_yaml(tmp_path / "agent_config.yaml")

        register_count = 0

        async def fake_register(*, agent, **kwargs):
            nonlocal register_count
            register_count += 1
            return FakeRegisterResponse(
                data=FakeRegisterData(
                    agent=FakeAgent(
                        id=f"re-registered-{register_count}",
                        name=agent.name,
                        description=agent.description,
                    ),
                    credentials=FakeCredentials(
                        api_key=f"new-key-{register_count}",
                    ),
                )
            )

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(
            data=platform,
        )
        client.human_api_agents.register_my_agent.side_effect = fake_register
        client.human_api_agents.delete_my_agent.return_value = FakeDeleteResponse(
            id="deleted", name="deleted",
        )

        await register_all_agents(config, tmp_path, client=client)

        # Exactly the drifting agent should be deleted from Band.ai.
        deleted_ids = {
            call.args[0]
            for call in client.human_api_agents.delete_my_agent.call_args_list
        }
        assert deleted_ids == {"co-x-0"}

        # And exactly one fresh registration happened (the replacement).
        assert client.human_api_agents.register_my_agent.call_count == 1
        registered_name = client.human_api_agents.register_my_agent.call_args[1][
            "agent"
        ].name
        assert registered_name == "Coder-Codex-0"

        # The local agent_config now has the new ID and key for that role.
        result = AgentConfigFile.from_yaml(tmp_path / "agent_config.yaml")
        assert result.agents["coder-codex-0"].agent_id != "co-x-0"
        assert result.agents["coder-codex-0"].agent_id.startswith("re-registered-")
        # The api_key must also be the freshly-issued one, not the old key.
        # This proves `existing_config.agents.pop(matching_key, None)` actually
        # ran. If a regression removed that pop, the agent_id assertion above
        # would still pass (the main loop overwrites on register), but the
        # api_key would silently leak through unchanged from the old creds.
        assert result.agents["coder-codex-0"].api_key.startswith("new-key-")
        assert result.agents["coder-codex-0"].api_key != "key-co-x0"
        # Other agents are unchanged.
        assert result.agents["conductor"].agent_id == "cond-0"
        assert result.agents["coder-claude_sdk-0"].agent_id == "co-c-0"
        assert result.agents["coder-claude_sdk-0"].api_key == "key-co-c0"

    @pytest.mark.asyncio
    async def test_no_drift_no_action(self, tmp_path):
        """Sanity: when every platform description matches, nothing happens."""
        from codeband.orchestration.setup import register_all_agents

        config = _make_config()
        _DEFAULT_AGENT_CONFIG.to_yaml(tmp_path / "agent_config.yaml")

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(
            data=_platform_agents_for(config),
        )

        await register_all_agents(config, tmp_path, client=client)

        client.human_api_agents.delete_my_agent.assert_not_called()
        client.human_api_agents.register_my_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_detect_drift_false_skips_drift_correction(self, tmp_path):
        """`detect_drift=False` (the `cb run` auto-bootstrap path) must not
        delete/re-register agents whose only sin is description drift. This
        is the safety guard against rotating credentials of agents that may
        be live in another swarm."""
        from codeband.orchestration.setup import register_all_agents

        config = _make_config()

        # Same setup as the positive drift test: Coder-Codex-0 has a corrupted
        # description on the platform.
        platform = _platform_agents_for(config)
        for i, agent in enumerate(platform):
            if agent.name == "Coder-Codex-0":
                platform[i] = FakeAgent(
                    id=agent.id,
                    name=agent.name,
                    description="OUTDATED — drift that the auto-bootstrap path must ignore",
                )
                break

        _DEFAULT_AGENT_CONFIG.to_yaml(tmp_path / "agent_config.yaml")

        client = AsyncMock()
        client.human_api_agents.list_my_agents.return_value = FakeListResponse(
            data=platform,
        )

        await register_all_agents(
            config, tmp_path, client=client, detect_drift=False,
        )

        # No delete and no re-register: the drifting agent stays as-is.
        client.human_api_agents.delete_my_agent.assert_not_called()
        client.human_api_agents.register_my_agent.assert_not_called()

        # Local creds are unchanged — including the api_key, which proves
        # the existing_config.agents pop did not run for this key.
        result = AgentConfigFile.from_yaml(tmp_path / "agent_config.yaml")
        assert result.agents["coder-codex-0"].agent_id == "co-x-0"
        assert result.agents["coder-codex-0"].api_key == "key-co-x0"


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
