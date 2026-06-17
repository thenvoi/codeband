"""Configuration models for Codeband."""

from __future__ import annotations

import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


# Role names the watchdog resolves thresholds for — the universe of valid
# `role_stale_thresholds` keys. Matches the AGENT_ROLE values the runner
# registers in its agent_id→role map.
_WATCHDOG_ROLE_KEYS = {
    "coder", "reviewer", "planner", "plan_reviewer",
    "conductor", "mergemaster", "watchdog",
}


class WatchdogConfig(_StrictModel):
    """Configuration for the watchdog agent."""

    # All interval/threshold knobs require >= 1: a zero here doesn't disable
    # the feature, it bricks the swarm silently (check_interval_seconds: 0
    # hot-loops the Band API; a zero threshold marks every agent stale on
    # every patrol).
    check_interval_seconds: int = Field(default=120, ge=1)
    stale_threshold_seconds: int = Field(default=300, ge=1)
    nudge_grace_seconds: int = Field(default=60, ge=1)
    # After an agent responds to a nudge, suppress further nudges for this
    # long. Without it, a legitimately-idle agent (e.g. Planner waiting on
    # human approval) gets re-nudged every `stale_threshold_seconds` forever,
    # because the old logic wiped the per-agent state the moment the agent
    # replied. Escalation (nudged-but-no-response) is unaffected.
    nudge_suppression_seconds: int = Field(default=1800, ge=1)
    # Per-role threshold overrides. Coders and the Mergemaster do long-running
    # work and are instructed to stay silent in chat while working — a uniform
    # 5-minute threshold nudges them mid-task. Roles not listed here fall back
    # to `stale_threshold_seconds`.
    role_stale_thresholds: dict[str, int] = Field(
        default_factory=lambda: {"coder": 900, "mergemaster": 900},
    )

    @field_validator("role_stale_thresholds")
    @classmethod
    def _known_role_keys(cls, v: dict[str, int]) -> dict[str, int]:
        """Reject unknown role keys — a typo'd key is otherwise silently
        ignored at threshold lookup, defeating the override's purpose."""
        unknown = sorted(set(v) - _WATCHDOG_ROLE_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown role key(s) in role_stale_thresholds: {unknown}. "
                f"Valid roles: {sorted(_WATCHDOG_ROLE_KEYS)}"
            )
        return v
    # When the Conductor records that the user-facing task is complete or
    # waiting on human merge approval via a `swarm status …` memory envelope,
    # suppress all nudging for this long. Prevents the watchdog from poking
    # correctly-idle agents between actionable steps. Falls back to time-based
    # behavior if no envelope is present (e.g. Conductor crashed before writing
    # one).
    swarm_idle_grace_seconds: int = Field(default=1800, ge=1)
    # Cycle/stall cap (RFC WS4). When a subtask makes no mechanical progress —
    # no git-HEAD change on its branch and no new transition-log entry — for
    # this many consecutive patrols, the watchdog marks it blocked and escalates
    # to the Conductor + human. Catches stalls that chat-recency alone misses
    # (e.g. a timed-out turn that produces no commit and no transition).
    # ge=1: a zero would mark every subtask blocked on its first patrol.
    max_phase_visits: int = Field(default=10, ge=1)
    # Toggle for the mechanical (git-HEAD / PR-state / transition-log) progress
    # signals. When False the watchdog falls back to chat-recency-only behavior.
    git_progress_check: bool = True
    # Deep full-history integrity sweep cadence (Stage-3 PR3). The incremental
    # integrity rung runs every patrol but, by construction, only re-reads rows
    # PAST its remembered tip — it cannot see an in-place edit of an interior,
    # already-verified row. A separate, longer-cadence rung walks BOTH hash
    # chains from row 1 (like `cb verify-log`) every this-many patrols to close
    # that blind spot. Code-driven: it runs regardless of whether a verifier LLM
    # seat is allocated, because integrity is a safety sweep, not an LLM
    # behavior. ge=1: a zero would mark the modulo undefined / never run.
    # Default 30 patrols ≈ hourly at the default 120s patrol interval —
    # proportionate to ledger size at this scale.
    full_integrity_interval_patrols: int = Field(default=30, ge=1)
    # Transport-health heal rung: detect and heal turn-boundary 422 pins. When
    # a mark-processed POST 422s at the turn boundary, the delivery stays stuck
    # in ``processing`` and the agent's cursor is pinned — a chat nudge cannot
    # reach it. The watchdog re-asserts ``processing → processed`` on the
    # stuck delivery, which advances the cursor so the agent's next poll
    # flows. ``transport_pin_threshold_seconds`` MUST be conservatively longer
    # than any plausible real turn so mid-turn 422s are never touched — the
    # default 1800s (30 min) is 2× the longest role threshold (coder /
    # mergemaster at 900s). After ``transport_heal_max_attempts`` failed
    # heals on the same delivery the pin escalates to the owner once and no
    # further heals fire (no infinite heal storm on a server-side rejection).
    # ``transport_heal_enabled`` is the kill switch.
    transport_heal_enabled: bool = True
    transport_pin_threshold_seconds: int = Field(default=1800, ge=1)
    transport_heal_max_attempts: int = Field(default=3, ge=1)
    # Approval→merge backstop rung: re-@mention the Mergemaster when a
    # merge_pending subtask has a recorded human approval at the current HEAD
    # but LLM dispatch has stalled, instead of letting the watchdog escalate
    # an already-approved PR to blocked.
    # ``merge_approval_backstop_seconds``: staleness window (seconds since the
    # last grant or backstop nudge) before the first renudge fires.
    # ``merge_approval_backstop_max_renudges``: number of backstop re-@mentions
    # the rung may send per approved-SHA (0 disables the send leg entirely
    # while still owning the patrol; 1 = the default, sends once then releases
    # so a genuinely hung Mergemaster still surfaces as blocked).
    merge_approval_backstop_seconds: int = Field(default=240, ge=1)
    merge_approval_backstop_max_renudges: int = Field(default=1, ge=0)
    # Acceptance-advance rung: re-@mention the Mergemaster when a subtask has
    # passed acceptance (``acceptance_passed``) but merge dispatch has stalled,
    # instead of letting the watchdog escalate a verified-and-accepted PR to
    # blocked.  Mirrors the approval→merge backstop but targets the state BEFORE
    # ``merge_pending`` in the verifier-enabled path.
    # ``acceptance_advance_backstop_seconds``: staleness window (seconds since
    # acceptance_passed entry, or since the last renudge) before the rung fires.
    # ``acceptance_advance_max_renudges``: re-@mentions per acceptance_passed
    # entry (0 disables the send leg; 1 = default, sends once then releases so
    # a genuinely hung Mergemaster still surfaces as blocked).
    acceptance_advance_backstop_seconds: int = Field(default=240, ge=1)
    acceptance_advance_max_renudges: int = Field(default=1, ge=0)


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
    restart_delay_seconds: float = Field(default=5.0, ge=0.0)


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
        codex=PoolEntry(count=1, model="gpt-5.5"),
    )


def _default_coders_pool() -> FrameworkPool:
    # Coders get the heavier model by default — coding is the role where
    # reasoning depth pays off most. Planner / reviewers / conductor /
    # mergemaster stay on Sonnet, which is a better cost/latency fit for
    # their lighter workloads.
    return FrameworkPool(
        claude_sdk=PoolEntry(count=1, model="claude-opus-4-7"),
        codex=PoolEntry(count=1, model="gpt-5.5"),
    )


def _default_reviewers_pool() -> ReviewersConfig:
    return ReviewersConfig(
        claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
        codex=PoolEntry(count=1, model="gpt-5.5"),
    )


class VerifiersConfig(_StrictModel):
    """Verifier pool — dual-mandate governance gate (PR #63).

    The Verifier carries a dual mandate: (1) contract conformance — the solution
    actually satisfies the registered acceptance criteria; and (2) evidence
    integrity — the durable evidence (PR body, collected counts, SHAs) is
    truthful and matches reality. Both axes are governance duties, opposite-vendor
    by design; a legitimate veto on either axis is authoritative.

    Mirrors ReviewersConfig in shape (no review_guidelines). The verdict leg is
    wired (``cb-phase verify-acceptance``) and the Verifier runtime + dispatch
    exist, so the seat is ACTIVE by default (count=1 per vendor): a configured
    verifier makes ``verify_acceptance`` a required, SHA-pinned merge verdict
    (see ``state/registration.py``). Setting both counts to 0 opts back out —
    the verdict leaves the required snapshot and tasks merge straight from
    ``review_passed``. Opposite-vendor pairing (verifier.vendor != coder.vendor)
    is the adversarial signal; single-vendor configs degrade gracefully
    (same-vendor checking; cb doctor warns, never fails).
    """

    claude_sdk: PoolEntry = PoolEntry()
    codex: PoolEntry = PoolEntry()

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


def _default_verifiers_pool() -> VerifiersConfig:
    # count=1 per vendor activates the seat by default now that the Verifier
    # runtime + dispatch exist to produce the verdict. One Claude + one Codex
    # verifier gives every default coder (Claude + Codex) an opposite-vendor
    # acceptance checker — the adversarial signal — and lands the default swarm
    # at exactly 10 Band seats (the free-tier cap). The iff-configured coupling
    # in ``state/registration.py`` makes ``verify_acceptance`` a required,
    # SHA-pinned merge verdict for this default; a user who sets both counts to
    # 0 opts back out (merges straight from ``review_passed``, no acceptance).
    return VerifiersConfig(
        claude_sdk=PoolEntry(count=1, model="claude-opus-4-7"),
        codex=PoolEntry(count=1, model="gpt-5.5"),
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
    verifiers: VerifiersConfig = Field(default_factory=_default_verifiers_pool)

    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)

    # Optional verify command run by the ``cb-phase`` handoff gate (RFC WS3).
    # When set, the command must exit 0 before a subtask may advance to
    # ``review_pending``. ``None`` skips the verify gate.
    handoff_verify_command: str | None = None

    # Verdict legs a subtask must clear before its PR may merge (Stage-2).
    # Resolved and validated at task-registration time by
    # ``state/registration.py`` — the single writer of "a task exists" — and
    # snapshotted onto the tasks row, so a mid-task config edit cannot change
    # what an in-flight task requires. ``None`` (key absent) resolves to the
    # default ``["verify", "review"]`` — plus ``"verify_acceptance"`` whenever a
    # verifier is configured (the iff-configured coupling in
    # ``state/registration.py``; verifiers are count=0 by default, so the
    # default snapshot stays the verify/review pair); an explicit ``[]`` is a
    # loud error unless
    # ``allow_ungated_merge`` is also set. Known verdicts: ``verify`` (requires
    # ``handoff_verify_command``), ``review``, and ``verify_acceptance``
    # (requires a configured verifier). The merge-eligibility gate
    # (``state/fsm.py``) reads the snapshot.
    required_verdicts: list[str] | None = None

    # Escape hatch for ``required_verdicts: []`` — the name is deliberately
    # ugly so "every PR merges with zero verdicts" can never be configured by
    # accident or typo. Without it, an empty list fails registration.
    allow_ungated_merge: bool = False

    # Who must approve a ``cb-phase merge`` before it executes (Stage-2).
    # ``"owner"`` (default) routes the approval request to the task owner;
    # ``"human:<handle>"`` routes it to the named human. Resolved and
    # validated at task-registration time by ``state/registration.py`` —
    # exactly like ``required_verdicts`` — and snapshotted onto the tasks row,
    # so a mid-task config edit cannot change an in-flight task's approver.
    # ``"none"`` is reserved and rejected (unapproved merges are not supported
    # in V1); any other value fails registration loudly.
    merge_approval: str = "owner"

    # Per-subtask review-round cap (RFC two-level model). Once a subtask has
    # entered ``review_failed`` this many times, the FSM refuses to send it back
    # to ``in_progress`` for another rework cycle — the only legal move is
    # ``blocked`` (escalation). Bounds a *productive* review loop (real commits
    # each round, HEAD advancing) that the watchdog's stall cap
    # (``watchdog.max_phase_visits``) by design never fires on. Default 6 was
    # validated in dogfood: the previous default of 3 false-blocked productive PRs
    # on their third round; 6 reflects the observed 4-6-round plateau for
    # non-trivial changes. Wired into ``fsm.transition`` via ``max_review_rounds``
    # (default ``fsm.MAX_REVIEW_ROUNDS``); the live caller lands with P5 activation.
    # ge=1: a zero would block every subtask at its first review.
    max_review_rounds: int = Field(default=6, ge=1)

    # Per-subtask verify-attempt cap (RFC two-level model). Once a subtask has
    # had this many ``cb-phase verify`` attempts *rejected* (a failed gate: dirty
    # tree / PR not open / verify command non-zero), the handoff CLI refuses a
    # further attempt and escalates the subtask to ``blocked``. Bounds a verify
    # loop where the coder commits real code each attempt — git HEAD advances, so
    # the watchdog's stall cap (``watchdog.max_phase_visits``) by design never
    # fires, and the review-round cap (``max_review_rounds``) never sees it (the
    # subtask never reaches ``review_failed``). Read by ``cli/handoff.py`` (the
    # already-live enforcement seam); default 20 matches ``fsm`` /
    # ``cli.handoff.MAX_VERIFY_ATTEMPTS``. ge=1: a zero would block every
    # subtask on its first verify attempt.
    max_verify_attempts: int = Field(default=20, ge=1)

    # Per-subtask rebase-round cap (S2-1). Once a subtask has *entered*
    # ``needs_rebase`` this many times (merge-gate send-backs: a moved head
    # while queued, a conflicted PR), the merge leg escalates the next
    # send-back to ``blocked`` (``BLOCKED [rebase_cap_reached]``) instead of
    # another rework cycle. Bounds the one loop neither sibling cap can see:
    # each rebase round writes fresh transition rows, so the watchdog's stall
    # cap (``watchdog.max_phase_visits``) by construction never fires, and the
    # loop never enters ``review_failed``, so ``max_review_rounds`` never
    # counts it. Live-read by ``cli/merge.py`` like the sibling caps; default
    # matches ``fsm.MAX_REBASE_ROUNDS``. ge=1: a zero would block every
    # subtask on its first send-back.
    max_rebase_rounds: int = Field(default=3, ge=1)

    # Exit codes from the verify command that classify as an infrastructure
    # failure rather than a test failure. When the command exits with one of
    # these codes the verify attempt is NOT counted against the coder's budget.
    # ``None`` resolves to the module-level default in ``cli/handoff.py``
    # (``_DEFAULT_INFRA_EXIT_CODES``): {124, 126, 127, 137, 143}. Can also be
    # overridden per-repo via ``verify_infra_exit_codes`` in the worktree's
    # ``.codeband.yaml``.
    verify_infra_exit_codes: list[int] | None = None

    # How quickly an idle agent re-polls its pending message queue — the
    # SDK's Phase-2 idle resync, the delivery backstop for missed websocket
    # pushes. Passed to every role uniformly (coders included — same intake
    # stack) as ``SessionConfig(idle_resync_seconds=...)`` at Agent.create
    # (``runner._create_band_agent``). Lower values recover faster from
    # missed pushes but generate more REST traffic: each resync fires one
    # /next poll per subscribed room. ge=1: the SDK rejects values <= 0
    # (they would turn the resync into a REST hot loop).
    idle_resync_seconds: int = Field(default=30, ge=1)

    # Whole-turn budget for every Codex role (finding 22 mitigation 4a; also
    # the root of the original shakedown finding 4). band-sdk's
    # ``CodexAdapterConfig.turn_timeout_s`` defaults to 180s and is NOT
    # activity-extended — the remaining budget shrinks from turn start no
    # matter how much real work streams by — so every coding turn longer
    # than 3 minutes was abandoned mid-flight: the adapter sends
    # turn/interrupt and stops listening while the Codex CLI keeps working,
    # the primary desync behind dormant Codex threads. Wired into
    # ``CodexAdapterConfig(turn_timeout_s=...)`` at every Codex
    # role-constructor seam (all six runners). ge=60: anything shorter
    # cannot fit even a trivial tool-using turn.
    #
    # Do NOT set this to 0 expecting "unlimited": band-sdk 0.2.11 has no
    # no-timeout special case for this knob. The event loop computes
    # ``max(0.0, turn_timeout_s - elapsed)`` (thenvoi/adapters/codex.py:683)
    # and feeds it straight to ``asyncio.wait_for`` via
    # ``recv_event(timeout_s=...)`` — only ``timeout_s=None`` means "wait
    # forever" (rpc_base.py:191-193), and the adapter never passes None — so
    # 0 means an IMMEDIATE TimeoutError on the first event wait: instant
    # turn abandon. 0-as-unlimited semantics arrive with the upstream
    # band-sdk fix; until that lands the ge=60 floor deliberately makes 0
    # unrepresentable.
    codex_turn_timeout_seconds: int = Field(default=3600, ge=60)

    # Per-message retry budget for the SDK session
    # (``SessionConfig.max_message_retries``, SDK default 1) — same plumbing
    # as ``idle_resync_seconds`` at ``runner._create_band_agent``. With the
    # SDK default, ONE transient turn failure permanently retires an
    # @mention: the client-side retry tracker marks it failed with no
    # server-side resolution, and the room's idle-resync backstop then
    # re-fetches the same poisoned head and stops cold (upstream band-sdk
    # defect, reported separately). Honest scope: raising this REDUCES the
    # frequency of that silent-permafail/head-of-line failure; it does not
    # eliminate it. ge=1: the SDK needs at least one attempt.
    max_message_retries: int = Field(default=3, ge=1)

    def total_agent_count(self) -> int:
        """Band.ai seats used (excluding Watchdog — reuses Conductor creds)."""
        return (
            2  # conductor + mergemaster
            + self.planners.total_count()
            + self.plan_reviewers.total_count()
            + self.coders.total_count()
            + self.reviewers.total_count()
            + self.verifiers.total_count()
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
        """Load configuration from a YAML file.

        ``yaml.safe_load`` yields ``None`` for a zero-byte / comments-only
        file; normalize to ``{}`` (as ``AgentConfigFile.from_yaml`` already
        does) so an empty ``codeband.yaml`` fails with the actionable
        "repo: Field required" instead of the opaque "Input should be a
        valid dictionary".
        """
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
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
        """Write agent credentials to YAML, private (0600) and atomic."""
        data = self.model_dump(mode="json")
        if path.exists():
            os.chmod(path, 0o600)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def get(self, key: str) -> AgentCredentials:
        """Get credentials for an agent key, raising if not found."""
        if key not in self.agents:
            raise KeyError(
                f"Agent '{key}' not found in agent_config.yaml. "
                f"Available: {list(self.agents.keys())}. "
                "Run 'codeband setup-agents' to register agents."
            )
        return self.agents[key]


def resolve_workspace_path(config: CodebandConfig, project_dir: Path) -> Path:
    """Resolve ``workspace.path`` to an absolute path — the ONE shared rule.

    A relative ``workspace.path`` resolves against ``$WORKSPACE`` when that
    env var is set (the Docker images set it to ``/workspace``, the shared
    volume every container mounts), otherwise against ``project_dir``. An
    absolute path is returned as-is. The runner, ``cb-phase``/``cb approve``
    (via ``cli/handoff.py:_resolve_store``), task registration
    (``state/registration.py:resolve_state_dir``) and ``cb doctor`` all route
    through this helper: two implementations of this rule is how containers
    ended up with the runner reading ``/workspace/state/`` while ``cb-phase``
    looked in ``/app/config/.codeband/state/``.
    """
    import os

    ws_path = Path(config.workspace.path)
    if ws_path.is_absolute():
        return ws_path
    workspace_env = os.environ.get("WORKSPACE")
    base = Path(workspace_env) if workspace_env else project_dir
    return base / ws_path


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


_SCALABLE_POOLS = {"planners", "plan_reviewers", "coders", "reviewers", "verifiers"}


def scale_pool(
    config_path: Path, pool: str, framework: Framework, count: int,
) -> CodebandConfig:
    """Set the capacity of a (pool, framework) entry in an existing config.

    `pool` must be one of "planners" / "plan_reviewers" / "coders" /
    "reviewers" / "verifiers". Preserves model/restart settings on the pool
    entry. Saves the updated config back to disk and returns it.
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
