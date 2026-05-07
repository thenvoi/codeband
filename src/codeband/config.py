"""Configuration models for Codeband."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Reject unknown fields by default. Catches YAML indentation bugs (e.g.
# `agents:` nested under `repo:` instead of at top level) that would
# otherwise be silently ignored, leaving the user running with defaults
# while thinking their overrides are taking effect.
_STRICT_CONFIG = ConfigDict(extra="forbid")


class DeploymentMode(str, Enum):
    """Workspace deployment mode."""

    LOCAL = "local"
    DISTRIBUTED = "distributed"


class Framework(str, Enum):
    """Supported coder frameworks."""

    CLAUDE_SDK = "claude_sdk"
    CODEX = "codex"


class _StrictModel(BaseModel):
    """Base for every config model — rejects unknown fields on load."""

    model_config = _STRICT_CONFIG


class AgentCredentials(_StrictModel):
    """Band.ai agent credentials."""

    agent_id: str
    api_key: str


class ConductorConfig(_StrictModel):
    """Configuration for the conductor agent (single-instance coordinator)."""

    framework: Framework = Framework.CLAUDE_SDK
    model: str = "claude-sonnet-4-6"


class AutoMergePolicy(str, Enum):
    """Controls which risk levels are auto-merged without human approval."""

    ALL = "all"        # Auto-merge everything that passes review
    LOW = "low"        # Auto-merge low-risk only; medium+ needs human approval
    MEDIUM = "medium"  # Auto-merge low and medium; high+ needs human approval
    NONE = "none"      # Human approves every merge


class MergemasterConfig(_StrictModel):
    """Configuration for the mergemaster agent (single-instance coordinator)."""

    framework: Framework = Framework.CLAUDE_SDK
    model: str = "claude-sonnet-4-6"
    test_command: str | None = None
    review_guidelines: str | None = None
    auto_merge: AutoMergePolicy = AutoMergePolicy.LOW


class WatchdogConfig(_StrictModel):
    """Configuration for the watchdog agent."""

    check_interval_seconds: int = 120
    stale_threshold_seconds: int = 300
    nudge_grace_seconds: int = 60
    # After an agent responds to a nudge, suppress further nudges for this
    # long. Without it, a legitimately-idle agent (e.g. Planner waiting on
    # human approval) gets re-nudged every `stale_threshold_seconds` forever,
    # because the old logic wiped the per-agent state the moment the agent
    # replied. Escalation (nudged-but-no-response) is unaffected.
    nudge_suppression_seconds: int = 1800
    # Per-role threshold overrides. Coders and the Mergemaster do long-running
    # work and are instructed to stay silent in chat while working — a uniform
    # 5-minute threshold nudges them mid-task. Roles not listed here fall back
    # to `stale_threshold_seconds`.
    role_stale_thresholds: dict[str, int] = Field(
        default_factory=lambda: {"coder": 900, "mergemaster": 900},
    )
    # When the Conductor records that the user-facing task is complete or
    # waiting on human merge approval via a `swarm status …` memory envelope,
    # suppress all nudging for this long. Prevents the watchdog from poking
    # correctly-idle agents between actionable steps. Falls back to time-based
    # behavior if no envelope is present (e.g. Conductor crashed before writing
    # one).
    swarm_idle_grace_seconds: int = 1800


# ─── Worker-pool config primitives ──────────────────────────────────────────
#
# `PoolEntry` and `FrameworkPool` are the building blocks used by
# `AgentsConfig` to declare capacity for each pool role (planners,
# plan_reviewers, coders, reviewers) as `{framework: {count, model, …}}`.

class PoolEntry(BaseModel):
    """Capacity for one (role, framework) combination in a worker pool.

    `count: 0` opts out of this framework for this role. `model=None`
    falls back to a framework-appropriate default at spawn time.
    """

    # `extra="ignore"` (instead of the project-default `extra="forbid"`) so
    # codeband.yaml files written by an older version still load. The 0.1.0
    # series wrote a `description` field here; 0.1.1 removed the field but
    # forbidding it on load would break every existing install. Unknown keys
    # are silently dropped on read and disappear from the file on next save.
    model_config = ConfigDict(extra="ignore")

    count: int = Field(default=0, ge=0)
    model: str | None = None
    # Deprecated — no longer honored. Coders now reconnect forever under
    # WorkerSupervisor; only SIGINT/SIGTERM ends a session. Kept for backward
    # compatibility so existing codeband.yaml files don't fail to parse.
    max_restarts: int = 5
    restart_delay_seconds: float = 5.0


class FrameworkPool(_StrictModel):
    """Per-framework capacity for one pool role (planners/coders/etc.)."""

    claude_sdk: PoolEntry = PoolEntry()
    codex: PoolEntry = PoolEntry()

    def total_count(self) -> int:
        return self.claude_sdk.count + self.codex.count

    def active_frameworks(self) -> list[Framework]:
        """Frameworks with count > 0, in deterministic order."""
        active = []
        if self.claude_sdk.count > 0:
            active.append(Framework.CLAUDE_SDK)
        if self.codex.count > 0:
            active.append(Framework.CODEX)
        return active

    def entry_for(self, framework: Framework) -> PoolEntry:
        return self.claude_sdk if framework == Framework.CLAUDE_SDK else self.codex


class ReviewersConfig(_StrictModel):
    """Code reviewer pool + project-wide review policy."""

    claude_sdk: PoolEntry = PoolEntry()
    codex: PoolEntry = PoolEntry()
    review_guidelines: str | None = None

    def total_count(self) -> int:
        return self.claude_sdk.count + self.codex.count

    def active_frameworks(self) -> list[Framework]:
        active = []
        if self.claude_sdk.count > 0:
            active.append(Framework.CLAUDE_SDK)
        if self.codex.count > 0:
            active.append(Framework.CODEX)
        return active

    def entry_for(self, framework: Framework) -> PoolEntry:
        return self.claude_sdk if framework == Framework.CLAUDE_SDK else self.codex


class PlanReviewersConfig(ReviewersConfig):
    """Plan reviewer pool + project-wide plan-review policy.

    Shares the shape of ReviewersConfig; kept as a distinct class for
    prompt/runner branching on role.
    """


def _default_planners_pool() -> FrameworkPool:
    return FrameworkPool(
        claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
    )


def _default_plan_reviewers_pool() -> PlanReviewersConfig:
    return PlanReviewersConfig(
        codex=PoolEntry(count=1, model="gpt-5.4"),
    )


def _default_coders_pool() -> FrameworkPool:
    # Coders get the heavier model by default — coding is the role where
    # reasoning depth pays off most. Planner / reviewers / conductor /
    # mergemaster stay on Sonnet, which is a better cost/latency fit for
    # their lighter workloads.
    return FrameworkPool(
        claude_sdk=PoolEntry(count=1, model="claude-opus-4-7"),
        codex=PoolEntry(count=1, model="gpt-5.4"),
    )


def _default_reviewers_pool() -> ReviewersConfig:
    return ReviewersConfig(
        claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
        codex=PoolEntry(count=1, model="gpt-5.4"),
    )


class AgentsConfig(_StrictModel):
    """Configuration for all agents — worker pool + coordination singletons."""

    # Single-instance coordination roles
    conductor: ConductorConfig = Field(default_factory=ConductorConfig)
    mergemaster: MergemasterConfig = Field(default_factory=MergemasterConfig)

    # Pools — framework × count
    planners: FrameworkPool = Field(default_factory=_default_planners_pool)
    plan_reviewers: PlanReviewersConfig = Field(default_factory=_default_plan_reviewers_pool)
    coders: FrameworkPool = Field(default_factory=_default_coders_pool)
    reviewers: ReviewersConfig = Field(default_factory=_default_reviewers_pool)

    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)

    def total_agent_count(self) -> int:
        """Band.ai seats used (excluding Watchdog — reuses Conductor creds)."""
        return (
            2  # conductor + mergemaster
            + self.planners.total_count()
            + self.plan_reviewers.total_count()
            + self.coders.total_count()
            + self.reviewers.total_count()
        )


class RepoConfig(_StrictModel):
    """Repository configuration."""

    url: str
    branch: str = "main"


class WorkspaceConfig(_StrictModel):
    """Workspace directory configuration."""

    path: str = ".codeband"
    worktree_prefix: str = "codeband"
    mode: DeploymentMode = DeploymentMode.LOCAL


class BandConfig(_StrictModel):
    """Band.ai platform connection settings."""

    rest_url: str = "https://app.band.ai"
    ws_url: str = "wss://app.band.ai/api/v1/socket/websocket"
    memory_mode: Literal["auto", "band", "local"] = "auto"
    # "human" uses the richer human-API liveness signal (text + thought +
    # tool_call + tool_result + error) but is enterprise-only. "agent" uses
    # the always-available agent-API inbox signal. "auto" probes once at
    # startup and falls back to "agent" on HTTP 402/403/404/501.
    liveness_mode: Literal["auto", "human", "agent"] = "auto"


class CodebandConfig(_StrictModel):
    """Root configuration for a Codeband project."""

    repo: RepoConfig
    agents: AgentsConfig = AgentsConfig()
    workspace: WorkspaceConfig = WorkspaceConfig()
    band: BandConfig = BandConfig()

    @classmethod
    def from_yaml(cls, path: Path) -> CodebandConfig:
        """Load configuration from a YAML file."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)

    def to_yaml(self, path: Path) -> None:
        """Write configuration to a YAML file."""
        data = self.model_dump(mode="json")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)


class AgentConfigFile(_StrictModel):
    """Agent credentials file (agent_config.yaml) — maps agent keys to credentials."""

    agents: dict[str, AgentCredentials] = Field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> AgentConfigFile:
        """Load agent credentials from YAML."""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def to_yaml(self, path: Path) -> None:
        """Write agent credentials to YAML."""
        data = self.model_dump(mode="json")
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def get(self, key: str) -> AgentCredentials:
        """Get credentials for an agent key, raising if not found."""
        if key not in self.agents:
            raise KeyError(
                f"Agent '{key}' not found in agent_config.yaml. "
                f"Available: {list(self.agents.keys())}. "
                "Run 'codeband setup-agents' to register agents."
            )
        return self.agents[key]


def load_config(project_dir: Path | None = None) -> CodebandConfig:
    """Load codeband.yaml from project directory (defaults to cwd)."""
    project_dir = project_dir or Path.cwd()
    config_path = project_dir / "codeband.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"codeband.yaml not found in {project_dir}. "
            "Run 'codeband init --repo <url>' to create one."
        )
    return CodebandConfig.from_yaml(config_path)


def load_agent_config(project_dir: Path | None = None) -> AgentConfigFile:
    """Load agent_config.yaml from project directory (defaults to cwd)."""
    project_dir = project_dir or Path.cwd()
    config_path = project_dir / "agent_config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"agent_config.yaml not found in {project_dir}. "
            "Run 'codeband setup-agents' to register agents."
        )
    return AgentConfigFile.from_yaml(config_path)


_SCALABLE_POOLS = {"planners", "plan_reviewers", "coders", "reviewers"}


def scale_pool(
    config_path: Path, pool: str, framework: Framework, count: int,
) -> CodebandConfig:
    """Set the capacity of a (pool, framework) entry in an existing config.

    `pool` must be one of "planners" / "plan_reviewers" / "coders" / "reviewers".
    Preserves model/restart settings on the pool entry. Saves the updated
    config back to disk and returns it.
    """
    if count < 0:
        raise ValueError("count must be >= 0")
    if pool not in _SCALABLE_POOLS:
        raise ValueError(
            f"Unknown pool '{pool}'. Must be one of: {sorted(_SCALABLE_POOLS)}",
        )

    config = CodebandConfig.from_yaml(config_path)
    pool_obj = getattr(config.agents, pool)
    entry: PoolEntry = pool_obj.entry_for(framework)
    entry.count = count

    # Re-run the validator to catch unsupported combinations (e.g., Codex
    # planner) before persisting. Pydantic re-validates on model_validate.
    CodebandConfig.model_validate(config.model_dump(mode="json"))

    config.to_yaml(config_path)
    return config
