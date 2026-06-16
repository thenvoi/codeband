"""Agent registration on the Band.ai platform.

Expected agents are derived from the worker-pool configuration:
- Two singletons: `conductor`, `mergemaster`.
- Five pools: `planners`, `plan_reviewers`, `coders`, `reviewers`, `verifiers`.
  Each pool contributes `count` agents per active framework. Agent
  config keys follow `{role}-{framework}-{index}` (e.g. `coder-claude_sdk-0`);
  Band.ai display names are the friendlier `Coder-Claude-0`.

Watchdog is intentionally omitted — it runs as an in-process daemon and
reuses the Conductor's credentials.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
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


class _DeleteReason(str, Enum):
    """Why the cleanup pre-pass deleted a platform agent. Logged verbatim."""

    NAME_NO_LONGER_EXPECTED = "name no longer expected"
    CREDENTIAL_MISMATCH = "credential mismatch"
    DESCRIPTION_DRIFT = "description drift"


# ─── naming ─────────────────────────────────────────────────────────────────

_FRAMEWORK_DISPLAY = {
    Framework.CLAUDE_SDK: "Claude",
    Framework.CODEX: "Codex",
}

# Agent descriptions — these are what `thenvoi_lookup_peers` returns for
# discovery-based invites, so each must be self-describing enough that another
# agent reading the description alone can decide whether to recruit it.
# Every description includes a stable `role=...` discovery token, and pool
# descriptions also include `framework=Claude|Codex` so adversarial cross-model
# pairing rules can filter on exact strings instead of prose.
#
# Descriptions are intentionally NOT user-overridable — keep them consistent
# across deployments so prompts can rely on the wording.

_SINGLETON_AGENTS = {
    "conductor": (
        "Conductor",
        (
            "Codeband Conductor — orchestration hub. Routes user tasks to a "
            "Planner, allocates Coders for subtasks, observes Code Reviewer "
            "verdicts, applies the auto-merge policy (auto-merge low-risk; "
            "route higher-risk to human approval), and forwards approved PRs "
            "to the Mergemaster. Relays cross-agent protocols (clarification, "
            "plan revision, merge conflicts, test failures); intervenes when "
            "dispatches stall. Singleton; does not plan, code, or review. "
            "Discovery: role=conductor_agent."
        ),
    ),
    "mergemaster": (
        "Mergemaster",
        (
            "Codeband Mergemaster — integrates approved PRs into the repository "
            "base branch using a batch-then-bisect strategy. Runs integration "
            "tests, handles merge conflicts and test failures by routing back "
            "to the originating Coder via the Conductor. Singleton role; the "
            "last gate before code reaches main. Discovery: role=merge_agent."
        ),
    ),
}

_POOL_ROLES: tuple[tuple[WorkerRole, str, str], ...] = (
    (
        WorkerRole.PLANNER,
        "planners",
        (
            "Codeband Planner — analyzes the codebase and decomposes user "
            "tasks into independent parallelizable subtasks with acceptance "
            "criteria. Cross-model paired with a Plan Reviewer on the opposite "
            "framework. Discovery: role=planning_agent"
        ),
    ),
    (
        WorkerRole.PLAN_REVIEWER,
        "plan_reviewers",
        (
            "Codeband Plan Reviewer — validates implementation plans before "
            "Coders begin work. Checks decomposition quality, file conflict "
            "risk, and acceptance criteria. Cross-model paired with Planners "
            "on the opposite framework. Discovery: role=plan_review_agent"
        ),
    ),
    (
        WorkerRole.CODER,
        "coders",
        (
            "Codeband Coder — implements assigned subtasks in an isolated git "
            "worktree, opens a PR, and dispatches it directly to a Code "
            "Reviewer on the opposite framework. Cross-model code review is "
            "the primary value prop of the swarm. Discovery: role=coding_agent"
        ),
    ),
    (
        WorkerRole.REVIEWER,
        "reviewers",
        (
            "Codeband Code Reviewer — reviews PRs from Coders before merge. "
            "Cross-model: reviews PRs from Coders on the opposite framework. "
            "Reports a PASS/FAIL verdict with a risk level "
            "(low/medium/high/critical) that the Conductor uses to decide "
            "auto-merge vs human approval. Discovery: role=code_review_agent"
        ),
    ),
    (
        WorkerRole.VERIFIER,
        "verifiers",
        (
            "Codeband Verifier — checks evidence integrity for completed "
            "subtasks before merge. Cross-model: verifies evidence from "
            "Coders on the opposite framework. Posts a PASS/FAIL verdict "
            "consumed by the merge gate. Seat is INERT (count=0) until "
            "the verdict leg is wired. Discovery: role=verification_agent"
        ),
    ),
)


def worker_display_name(worker_id: WorkerId) -> str:
    """Band.ai display name for a pool worker (e.g. `Coder-Claude-0`)."""
    role_label = worker_id.role.value.replace("_", "-").title()
    fw_label = _FRAMEWORK_DISPLAY[worker_id.framework]
    return f"{role_label}-{fw_label}-{worker_id.index}"


_BAND_DESCRIPTION_MAX = 500


def _expected_agents(config: CodebandConfig) -> dict[str, tuple[str, str]]:
    """Build the full map of agent config-key -> (display_name, description).

    Asserts each description fits within Band.ai's 500-char limit so that a
    too-long description fails at build time (with a precise pointer to the
    offending agent) rather than at registration time as an opaque 422 from
    the platform.
    """
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
                desc = (
                    f"{base_desc}; framework={_FRAMEWORK_DISPLAY[framework]} "
                    f"({_FRAMEWORK_DISPLAY[framework]})."
                )
                expected[key] = (display, desc)

    for key, (display, desc) in expected.items():
        if len(desc) > _BAND_DESCRIPTION_MAX:
            raise ValueError(
                f"Description for {display} ({key}) is {len(desc)} chars, "
                f"exceeds Band.ai's {_BAND_DESCRIPTION_MAX}-char limit. "
                "Trim the canonical description in `_SINGLETON_AGENTS` or "
                "`_POOL_ROLES` in setup.py."
            )

    return expected


# ─── identification of codeband agents on Band.ai ──────────────────────────

_LEGACY_AGENT_NAMES = frozenset(
    {
        "Watchdog",  # previously registered
        "Planner",  # legacy single-planner name
        "Plan Reviewer",  # legacy singleton
        "Code Reviewer",  # legacy singleton
    }
)
_CODEBAND_PREFIXES = (
    "Planner-",
    "Plan-Reviewer-",
    "Coder-",
    "Reviewer-",
    "Player-",
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
    *,
    detect_drift: bool = True,
) -> None:
    """Register Codeband agents, reusing existing ones where possible.

    Walks the expected map from config, reuses agents whose Band.ai ID
    matches the locally-persisted agent_id, deletes stale Codeband-prefixed
    agents (e.g., old player names or reduced pool counts), and registers
    any missing ones.

    `detect_drift=True` (default, for `cb setup-agents`): also re-register
    agents whose platform-side `description` no longer matches the canonical
    description in `_SINGLETON_AGENTS` / `_POOL_ROLES`. Re-registration
    rotates the agent's ID and api_key, so any session currently using the
    old credential will fail on next reconnect. The auto-bootstrap path in
    `runner.py:_ensure_agents_registered` passes `detect_drift=False` so that
    starting `cb run` while another swarm is alive in a different terminal
    cannot rotate that swarm's credentials out from under it.
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

    # Delete stale codeband agents before registering to avoid name conflicts.
    # Description drift is only checked under detect_drift=True (cb setup-agents);
    # the cb run auto-bootstrap path skips it so a starting swarm cannot rotate
    # credentials of agents another swarm is using.
    #
    # _delete_failures tracks CREDENTIAL_MISMATCH delete failures by display-name.
    # Only mismatch failures block registration (the platform still owns the name,
    # so a subsequent _register_agent call would 422). NAME_NO_LONGER_EXPECTED and
    # DESCRIPTION_DRIFT failures do not cause spurious registers so they are logged
    # but do not block the run.
    _delete_failures: dict[str, Exception] = {}

    for name, agent in list(platform_agents.items()):
        if not _is_codeband_agent(name):
            continue
        matching_key = next((k for k, (n, _) in expected.items() if n == name), None)
        existing_creds = existing_config.agents.get(matching_key) if matching_key else None
        delete_reason: _DeleteReason | None = None
        if name not in expected_names:
            delete_reason = _DeleteReason.NAME_NO_LONGER_EXPECTED
        elif not existing_creds or existing_creds.agent_id != agent.id:
            delete_reason = _DeleteReason.CREDENTIAL_MISMATCH
        elif detect_drift:
            expected_desc = expected[matching_key][1]
            platform_desc = agent.description or ""
            if platform_desc != expected_desc:
                delete_reason = _DeleteReason.DESCRIPTION_DRIFT
        if delete_reason:
            try:
                await client.human_api_agents.delete_my_agent(agent.id, force=True)
                logger.info(
                    "Deleted agent %s (%s): %s",
                    name,
                    delete_reason.value,
                    agent.id,
                )
                # Pop platform_agents so the main loop's reuse check fails and
                # re-registers. For drift, also pop the local cred — otherwise
                # it would still satisfy reuse and shadow the new key.
                platform_agents.pop(name, None)
                if delete_reason == _DeleteReason.DESCRIPTION_DRIFT and matching_key:
                    existing_config.agents.pop(matching_key, None)
            except Exception as e:
                logger.error("Failed to delete agent %s (%s): %s", name, delete_reason.value, e)
                if delete_reason is _DeleteReason.CREDENTIAL_MISMATCH:
                    # The platform still owns this name. Registering a new agent
                    # with the same name would 422. Track for skip + deferred raise.
                    _delete_failures[name] = e

    agent_config = AgentConfigFile()

    # Reuse or register each expected agent
    for key, (display_name, description) in expected.items():
        if display_name in _delete_failures:
            # A CREDENTIAL_MISMATCH delete failed for this name — the platform
            # agent still owns the slot. Attempting to register the same name
            # would produce a 422. Skip and surface the failure below.
            logger.error(
                "Skipping registration of %s: platform agent could not be deleted",
                display_name,
            )
            continue

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
        1
        for k in agent_config.agents
        if k not in existing_config.agents
        or existing_config.agents[k].agent_id != agent_config.agents[k].agent_id
    )
    reused = len(agent_config.agents) - registered
    print(f"\n{len(agent_config.agents)} agents ready ({reused} reused, {registered} registered).")
    print(f"Credentials: {config_path}")

    if _delete_failures:
        details = "; ".join(f"{n}: {exc!r}" for n, exc in _delete_failures.items())
        first_exc = next(iter(_delete_failures.values()))
        raise RuntimeError(
            f"Could not delete {len(_delete_failures)} stale platform agent(s) with credential "
            f"mismatches — their names are still occupied on the platform and registration was "
            f"skipped. Manual cleanup or re-run of 'cb setup-agents' may be required: {details}"
        ) from first_exc


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
