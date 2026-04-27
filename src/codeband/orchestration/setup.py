"""Agent registration on the Band.ai platform.

Expected agents are derived from the worker-pool configuration:
- Two singletons: `conductor`, `mergemaster`.
- Four pools: `planners`, `plan_reviewers`, `coders`, `reviewers`.
  Each pool contributes `count` agents per active framework. Agent
  config keys follow `{role}-{framework}-{index}` (e.g. `coder-claude_sdk-0`);
  Band.ai display names are the friendlier `Coder-Claude-0`.

Watchdog is intentionally omitted — it runs as an in-process daemon and
reuses the Conductor's credentials.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from codeband.config import (
    AgentConfigFile,
    AgentCredentials,
    CodebandConfig,
    Framework,
    FrameworkPool,
)
from codeband.workers import WorkerId, WorkerRole

if TYPE_CHECKING:
    from thenvoi_rest import AsyncRestClient

logger = logging.getLogger(__name__)


# ─── naming ─────────────────────────────────────────────────────────────────

_FRAMEWORK_DISPLAY = {
    Framework.CLAUDE_SDK: "Claude",
    Framework.CODEX: "Codex",
}

_SINGLETON_AGENTS = {
    "conductor": ("Conductor", "Codeband task coordinator"),
    "mergemaster": ("Mergemaster", "Codeband merge and test agent"),
}

_POOL_ROLES: tuple[tuple[WorkerRole, str, str], ...] = (
    (WorkerRole.PLANNER, "planners", "Codeband task planner and decomposer"),
    (WorkerRole.PLAN_REVIEWER, "plan_reviewers", "Codeband plan review and validation agent"),
    (WorkerRole.CODER, "coders", "Codeband coding worker"),
    (WorkerRole.REVIEWER, "reviewers", "Codeband code review agent"),
)


def worker_display_name(worker_id: WorkerId) -> str:
    """Band.ai display name for a pool worker (e.g. `Coder-Claude-0`)."""
    role_label = worker_id.role.value.replace("_", "-").title()
    fw_label = _FRAMEWORK_DISPLAY[worker_id.framework]
    return f"{role_label}-{fw_label}-{worker_id.index}"


def _expected_agents(config: CodebandConfig) -> dict[str, tuple[str, str]]:
    """Build the full map of agent config-key -> (display_name, description)."""
    expected: dict[str, tuple[str, str]] = {}

    # Singletons
    for key, (display, desc) in _SINGLETON_AGENTS.items():
        expected[key] = (display, desc)

    # Pools — walk (role, attr, base_desc)
    for role, attr, base_desc in _POOL_ROLES:
        pool: FrameworkPool = getattr(config.agents, attr)
        for framework in (Framework.CLAUDE_SDK, Framework.CODEX):
            entry = pool.entry_for(framework)
            for i in range(entry.count):
                wid = WorkerId(role=role, framework=framework, index=i)
                key = str(wid)
                display = worker_display_name(wid)
                desc = f"{base_desc} ({_FRAMEWORK_DISPLAY[framework]})"
                expected[key] = (display, desc)

    return expected


# ─── identification of codeband agents on Band.ai ──────────────────────────

_LEGACY_AGENT_NAMES = frozenset({
    "Watchdog",         # previously registered
    "Planner",          # legacy single-planner name
    "Plan Reviewer",    # legacy singleton
    "Code Reviewer",    # legacy singleton
})
_CODEBAND_PREFIXES = (
    "Planner-", "Plan-Reviewer-", "Coder-", "Reviewer-", "Player-",
)


def _is_codeband_agent(name: str) -> bool:
    """Check if a platform agent name matches Codeband naming conventions."""
    if name in {"Conductor", "Mergemaster"}:
        return True
    if name in _LEGACY_AGENT_NAMES:
        return True
    return any(name.startswith(p) for p in _CODEBAND_PREFIXES)


# ─── main registration ─────────────────────────────────────────────────────

async def register_all_agents(
    config: CodebandConfig,
    project_dir: Path,
    client: "AsyncRestClient | None" = None,
) -> None:
    """Register Codeband agents, reusing existing ones where possible.

    Walks the expected map from config, reuses agents whose Band.ai ID
    matches the locally-persisted agent_id, deletes stale Codeband-prefixed
    agents (e.g., old player names or reduced pool counts), and registers
    any missing ones.
    """
    if client is None:
        api_key = os.environ.get("BAND_API_KEY")
        if not api_key:
            raise ValueError(
                "BAND_API_KEY environment variable is required. "
                "Get one from https://platform.band.ai"
            )
        from thenvoi_rest import AsyncRestClient
        client = AsyncRestClient(api_key=api_key, base_url=config.band.rest_url)

    # Load existing credentials if available
    config_path = project_dir / "agent_config.yaml"
    existing_config = (
        AgentConfigFile.from_yaml(config_path) if config_path.exists() else AgentConfigFile()
    )

    # Fetch agents currently on the platform
    try:
        platform_response = await client.human_api_agents.list_my_agents()
        platform_agents = {a.name: a for a in (platform_response.data or [])}
    except Exception as e:
        logger.warning("Could not list existing agents: %s — registering fresh", e)
        platform_agents = {}

    expected = _expected_agents(config)
    expected_names = {display for display, _ in expected.values()}

    # Delete stale codeband agents before registering to avoid name conflicts
    for name, agent in list(platform_agents.items()):
        if not _is_codeband_agent(name):
            continue
        if name not in expected_names:
            _delete = True
        else:
            # Expected name exists on platform — reuse only if local creds match
            matching_key = next(
                (k for k, (n, _) in expected.items() if n == name), None
            )
            existing_creds = (
                existing_config.agents.get(matching_key) if matching_key else None
            )
            _delete = not existing_creds or existing_creds.agent_id != agent.id
        if _delete:
            try:
                await client.human_api_agents.delete_my_agent(agent.id, force=True)
                logger.info("Deleted stale agent %s: %s", name, agent.id)
            except Exception as e:
                logger.warning("Failed to delete agent %s: %s", name, e)

    agent_config = AgentConfigFile()

    # Reuse or register each expected agent
    for key, (display_name, description) in expected.items():
        existing_creds = existing_config.agents.get(key)
        platform_agent = platform_agents.get(display_name)

        if platform_agent and existing_creds and existing_creds.agent_id == platform_agent.id:
            agent_config.agents[key] = existing_creds
            logger.info("Reusing %s: %s", key, existing_creds.agent_id)
        else:
            creds = await _register_agent(client, display_name, description)
            agent_config.agents[key] = creds
            logger.info("Registered %s: %s", key, creds.agent_id)

    # Write credentials
    agent_config.to_yaml(config_path)
    logger.info("Credentials written to %s", config_path)

    # Write agent IDs for cleanup
    ids_path = project_dir / ".agent_ids.txt"
    with open(ids_path, "w", encoding="utf-8") as f:
        for cred in agent_config.agents.values():
            f.write(f"{cred.agent_id}\n")

    registered = sum(
        1 for k in agent_config.agents
        if k not in existing_config.agents
        or existing_config.agents[k].agent_id != agent_config.agents[k].agent_id
    )
    reused = len(agent_config.agents) - registered
    print(f"\n{len(agent_config.agents)} agents ready ({reused} reused, {registered} registered).")
    print(f"Credentials: {config_path}")


async def _register_agent(
    client: "AsyncRestClient",
    name: str,
    description: str,
) -> AgentCredentials:
    """Register a single agent and return credentials."""
    from thenvoi_rest.types import AgentRegisterRequest

    response = await client.human_api_agents.register_my_agent(
        agent=AgentRegisterRequest(name=name, description=description)
    )
    agent = response.data.agent
    credentials = response.data.credentials

    return AgentCredentials(
        agent_id=agent.id,
        api_key=credentials.api_key,
    )
