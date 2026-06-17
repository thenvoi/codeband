"""Single source of truth for the agent roster.

Every per-role fact — Band.ai display name + description, default Claude model,
workspace kind, Claude permission profile, cross-model review pairing — lives in
``_ROLE_SPECS`` here, keyed by the ``AgentsConfig`` field name. Consumers
(preflight, doctor, runner spawn, workspace layout, setup registration, agent
counts) derive from this instead of hand-listing the roster.

The anti-rot guarantee: :func:`iter_agents` discovers agent fields by introspecting
``AgentsConfig`` and asserts every one has a ``RoleSpec``. Adding a role to
``AgentsConfig`` without registering it here raises at startup — it can no longer
be silently skipped by spawn/workspace/preflight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Literal

from codeband.config import AgentsConfig, CodebandConfig, Framework
from codeband.models import CLAUDE_OPUS, CLAUDE_SONNET, CODEX_GPT
from codeband.workers.pool import WorkerId, WorkerRole

Kind = Literal["singleton", "pool"]
WorkspaceKind = Literal["worktree", "scratch", "none"]
ClaudeProfile = Literal["coding", "planner", "plan_reviewer"]

# Human-facing framework labels used in Band.ai display names ("Coder-Claude-0").
FRAMEWORK_DISPLAY = {Framework.CLAUDE_SDK: "Claude", Framework.CODEX: "Codex"}
_BAND_DESCRIPTION_MAX = 500


@dataclass(frozen=True)
class RoleSpec:
    """Declarative metadata for one agent role (singleton or worker pool)."""

    config_attr: str  # field name on AgentsConfig (e.g. "coders")
    kind: Kind
    roster_label: str  # prose label for prompt rosters (e.g. "Code Reviewer")
    description: str  # Band.ai registration description (base text for pools)
    worker_role: WorkerRole | None = None  # None for singletons
    default_claude_model: str = CLAUDE_SONNET  # used when a Claude entry omits model
    default_codex_model: str = CODEX_GPT
    workspace_kind: WorkspaceKind = "none"
    claude_profile: ClaudeProfile | None = None
    review_pair: str | None = None  # partner config_attr for cross-model review

    @property
    def display_token(self) -> str:
        """Band.ai display prefix stem (e.g. "Coder", "Plan-Reviewer", "Conductor")."""
        if self.worker_role is not None:
            return self.worker_role.value.replace("_", "-").title()
        return self.config_attr.title()


# Deterministic order: singletons first, then pools (Claude-before-Codex order is
# applied per-framework inside iter_agents). Descriptions are intentionally NOT
# user-overridable — kept consistent across deployments so prompts can rely on the
# wording, and each carries a stable `role=...` discovery token.
_ROLE_SPECS: tuple[RoleSpec, ...] = (
    RoleSpec(
        "conductor", "singleton", "Conductor",
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
        workspace_kind="none",
    ),
    RoleSpec(
        "mergemaster", "singleton", "Mergemaster",
        (
            "Codeband Mergemaster — integrates approved PRs into the repository "
            "base branch using a batch-then-bisect strategy. Runs integration "
            "tests, handles merge conflicts and test failures by routing back "
            "to the originating Coder via the Conductor. Singleton role; the "
            "last gate before code reaches main. Discovery: role=merge_agent."
        ),
        workspace_kind="worktree", claude_profile="coding",
    ),
    RoleSpec(
        "planners", "pool", "Planner",
        (
            "Codeband Planner — analyzes the codebase and decomposes user "
            "tasks into independent parallelizable subtasks with acceptance "
            "criteria. Cross-model paired with a Plan Reviewer on the opposite "
            "framework. Discovery: role=planning_agent"
        ),
        worker_role=WorkerRole.PLANNER, default_claude_model=CLAUDE_SONNET,
        workspace_kind="worktree", claude_profile="planner",
        review_pair="plan_reviewers",
    ),
    RoleSpec(
        "plan_reviewers", "pool", "Plan Reviewer",
        (
            "Codeband Plan Reviewer — validates implementation plans before "
            "Coders begin work. Checks decomposition quality, file conflict "
            "risk, and acceptance criteria. Cross-model paired with Planners "
            "on the opposite framework. Discovery: role=plan_review_agent"
        ),
        worker_role=WorkerRole.PLAN_REVIEWER, default_claude_model=CLAUDE_SONNET,
        workspace_kind="worktree", claude_profile="plan_reviewer",
        review_pair="planners",
    ),
    RoleSpec(
        "coders", "pool", "Coder",
        (
            "Codeband Coder — implements assigned subtasks in an isolated git "
            "worktree, opens a PR, and dispatches it directly to a Code "
            "Reviewer on the opposite framework. Cross-model code review is "
            "the primary value prop of the swarm. Discovery: role=coding_agent"
        ),
        worker_role=WorkerRole.CODER, default_claude_model=CLAUDE_OPUS,
        workspace_kind="worktree", claude_profile="coding",
        review_pair="reviewers",
    ),
    RoleSpec(
        "reviewers", "pool", "Code Reviewer",
        (
            "Codeband Code Reviewer — reviews PRs from Coders before merge. "
            "Cross-model: reviews PRs from Coders on the opposite framework. "
            "Reports a PASS/FAIL verdict with a risk level "
            "(low/medium/high/critical) that the Conductor uses to decide "
            "auto-merge vs human approval. Discovery: role=code_review_agent"
        ),
        worker_role=WorkerRole.REVIEWER, default_claude_model=CLAUDE_SONNET,
        workspace_kind="scratch", claude_profile="coding",
        review_pair="coders",
    ),
)

_SPEC_BY_ATTR: dict[str, RoleSpec] = {spec.config_attr: spec for spec in _ROLE_SPECS}
# Legacy display-name prefix kept for recognizing older registered pool agents.
_LEGACY_PREFIXES: tuple[str, ...] = ("Player-",)


def _agent_field_names(agents: AgentsConfig) -> list[str]:
    """AgentsConfig field names that describe agents (singletons or pools).

    Duck-typed on the two shapes the rest of the code uses, so a non-agent field
    (e.g. ``watchdog``, which has neither) is naturally excluded.
    """
    names: list[str] = []
    for field_name in type(agents).model_fields:
        value = getattr(agents, field_name)
        is_singleton = hasattr(value, "framework") and hasattr(value, "model")
        is_pool = hasattr(value, "entry_for")
        if is_singleton or is_pool:
            names.append(field_name)
    return names


def _assert_registered(agents: AgentsConfig) -> None:
    """Fail loud if an agent-shaped config field has no RoleSpec.

    This is the anti-rot guard: a new role added to ``AgentsConfig`` must also be
    registered here, or startup fails with a precise pointer instead of the role
    being silently skipped by spawn/workspace/preflight.
    """
    missing = [name for name in _agent_field_names(agents) if name not in _SPEC_BY_ATTR]
    if missing:
        raise ValueError(
            f"AgentsConfig field(s) {missing} have no RoleSpec in roster.py. "
            "Add a RoleSpec to _ROLE_SPECS so the role is spawned, given a "
            "workspace, probed by preflight, and counted."
        )


@dataclass(frozen=True)
class AgentInfo:
    """One configured agent instance (a singleton, or one pool slot)."""

    key: str  # agent_config key: "conductor" or "coder-claude_sdk-0"
    spec: RoleSpec
    framework: Framework
    index: int | None  # None for singletons
    resolved_model: str  # model after applying per-role defaults
    worker_id: WorkerId | None  # None for singletons


def _resolved_model(spec: RoleSpec, framework: Framework, model: str | None) -> str:
    if model:
        return model
    if framework == Framework.CLAUDE_SDK:
        return spec.default_claude_model
    return spec.default_codex_model


def iter_agents(config: CodebandConfig) -> Iterator[AgentInfo]:
    """Yield every configured agent (singletons + pool slots), with resolved models.

    The single roster enumeration: spawn, workspace layout, registration, counts,
    and preflight all build on this. Asserts the registry is complete first.
    """
    agents = config.agents
    _assert_registered(agents)
    for spec in _ROLE_SPECS:
        value = getattr(agents, spec.config_attr)
        if spec.kind == "singleton":
            yield AgentInfo(
                key=spec.config_attr, spec=spec, framework=value.framework,
                index=None, resolved_model=value.model, worker_id=None,
            )
        else:
            for framework in (Framework.CLAUDE_SDK, Framework.CODEX):
                entry = value.entry_for(framework)
                for i in range(entry.count):
                    wid = WorkerId(role=spec.worker_role, framework=framework, index=i)
                    yield AgentInfo(
                        key=str(wid), spec=spec, framework=framework, index=i,
                        resolved_model=_resolved_model(spec, framework, entry.model),
                        worker_id=wid,
                    )


# ─── derived helpers (thin views consumers use instead of hardcoded lists) ───

def pool_specs() -> list[RoleSpec]:
    return [s for s in _ROLE_SPECS if s.kind == "pool"]


def singleton_specs() -> list[RoleSpec]:
    return [s for s in _ROLE_SPECS if s.kind == "singleton"]


def pool_attrs() -> set[str]:
    """Config attr names of scalable pools (replaces hardcoded pool-name sets)."""
    return {s.config_attr for s in pool_specs()}


def pool_role_values() -> set[str]:
    """`WorkerRole` value strings for the pools (e.g. {'coder','reviewer',...})."""
    return {s.worker_role.value for s in pool_specs()}


def singleton_keys() -> tuple[str, ...]:
    return tuple(s.config_attr for s in singleton_specs())


def spec_for_role(role_value: str) -> RoleSpec | None:
    """Look up a RoleSpec by role string (pool WorkerRole value or singleton key)."""
    for spec in _ROLE_SPECS:
        if spec.config_attr == role_value or (
            spec.worker_role is not None and spec.worker_role.value == role_value
        ):
            return spec
    return None


def display_prefixes() -> tuple[str, ...]:
    """Display-name prefixes for pool workers (e.g. 'Coder-'), plus legacy aliases."""
    return tuple(f"{s.display_token}-" for s in pool_specs()) + _LEGACY_PREFIXES


def review_pairs() -> list[tuple[str, str]]:
    """Distinct (config_attr, partner_attr) cross-model review pairs."""
    seen: set[frozenset[str]] = set()
    pairs: list[tuple[str, str]] = []
    for spec in _ROLE_SPECS:
        if spec.review_pair:
            key = frozenset({spec.config_attr, spec.review_pair})
            if key not in seen:
                seen.add(key)
                pairs.append((spec.config_attr, spec.review_pair))
    return pairs


def claude_models(config: CodebandConfig) -> list[str]:
    """Distinct Claude models configured across all roles, order-preserving."""
    seen: set[str] = set()
    distinct: list[str] = []
    for info in iter_agents(config):
        if info.framework == Framework.CLAUDE_SDK and info.resolved_model:
            if info.resolved_model not in seen:
                seen.add(info.resolved_model)
                distinct.append(info.resolved_model)
    return distinct


def uses_codex(config: CodebandConfig) -> bool:
    """True if any configured agent runs on the Codex framework."""
    return any(info.framework == Framework.CODEX for info in iter_agents(config))


def total_agent_count(config: CodebandConfig) -> int:
    """Band.ai seats used (every configured singleton + pool slot)."""
    return sum(1 for _ in iter_agents(config))
