"""`cb doctor` — read-only environment health check.

Pure diagnosis: every check inspects state and reports status + a remediation
hint. Nothing is modified on disk or on the platform. Exit code is 1 if any
check FAILs, else 0.

Adding a new check is two lines: write a function returning `CheckResult`,
append it to `_CHECKS` with a category and (optional) predicate. Keep checks
small and independent — the abstraction is only worth it if they stay easy
to write.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

import click

from codeband.config import (
    AgentConfigFile,
    CodebandConfig,
    Framework,
    load_agent_config,
    load_config,
)


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"
    SKIP = "skip"


@dataclass
class CheckResult:
    status: Status
    message: str
    remediation: str | None = None


@dataclass
class Check:
    name: str
    category: str
    run: Callable[["Context"], CheckResult | Awaitable[CheckResult]]
    applies_when: Callable[["Context"], bool] | None = None


@dataclass
class Context:
    project_dir: Path
    config: CodebandConfig | None = None
    agent_config: AgentConfigFile | None = None
    config_error: str | None = None
    agent_config_error: str | None = None
    results: dict[str, CheckResult] = field(default_factory=dict)


# ─── individual checks ──────────────────────────────────────────────────────

def check_python_version(_ctx: Context) -> CheckResult:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) < (3, 11):
        return CheckResult(
            Status.FAIL,
            f"Python {version} (need >= 3.11)",
            remediation="Install Python 3.11+ and reinstall codeband: pip install -e '.[dev]'",
        )
    return CheckResult(Status.OK, f"Python {version}")


def check_git(_ctx: Context) -> CheckResult:
    path = shutil.which("git")
    if not path:
        return CheckResult(
            Status.FAIL,
            "git not found on PATH",
            remediation="Install git: https://git-scm.com/downloads",
        )
    try:
        out = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=5, check=True,
        )
        version = out.stdout.strip().replace("git version ", "")
        return CheckResult(Status.OK, f"git {version} ({path})")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return CheckResult(Status.FAIL, f"git installed but failed to run: {exc}")


def check_gh(_ctx: Context) -> CheckResult:
    path = shutil.which("gh")
    if not path:
        return CheckResult(
            Status.FAIL,
            "gh (GitHub CLI) not found on PATH",
            remediation="Install gh: https://cli.github.com — needed for PR/issue commands.",
        )
    return CheckResult(Status.OK, f"gh found ({path})")


def check_claude_cli(_ctx: Context) -> CheckResult:
    """Every default project needs `claude` — the Planner is always Claude (no Codex
    implementation yet), and the `cb init` defaults put Claude on the Conductor,
    Mergemaster, and one Coder/Reviewer pair. Users who deliberately flip every
    role to Codex can still ignore a FAIL here."""
    path = shutil.which("claude")
    if not path:
        return CheckResult(
            Status.FAIL,
            "claude CLI not found on PATH",
            remediation=(
                "Install the Claude Code CLI: npm install -g @anthropic-ai/claude-code\n"
                "Docs: https://docs.claude.com/en/docs/claude-code"
            ),
        )
    try:
        out = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        version = out.stdout.strip() or "(version unknown)"
        return CheckResult(Status.OK, f"claude {version} ({path})")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            Status.FAIL,
            f"claude installed but `claude --version` failed: {exc}",
            remediation="Reinstall with: npm install -g @anthropic-ai/claude-code",
        )


def check_codex_cli(_ctx: Context) -> CheckResult:
    """Gated by `_needs_codex` — only runs when any agent uses Codex."""
    path = shutil.which("codex")
    if not path:
        return CheckResult(
            Status.FAIL,
            "codex CLI not found on PATH (required by your Codex-framework agents)",
            remediation=(
                "Install the Codex CLI: npm install -g @openai/codex\n"
                "Or switch those agents to `framework: claude_sdk` in codeband.yaml."
            ),
        )
    try:
        out = subprocess.run(
            ["codex", "--version"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        version = out.stdout.strip() or "(version unknown)"
        return CheckResult(Status.OK, f"codex {version} ({path})")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            Status.FAIL,
            f"codex installed but `codex --version` failed: {exc}",
            remediation="Reinstall with: npm install -g @openai/codex",
        )


def check_gh_auth(_ctx: Context) -> CheckResult:
    if not shutil.which("gh"):
        return CheckResult(Status.SKIP, "gh not installed — skipping auth check")
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            Status.WARN, "gh auth status timed out",
            remediation="Check network; rerun `gh auth status` manually.",
        )
    if result.returncode != 0:
        return CheckResult(
            Status.FAIL,
            "gh is not authenticated",
            remediation="Run: gh auth login",
        )
    return CheckResult(Status.OK, "gh authenticated")


def _has_claude_subscription_oauth() -> bool:
    """Re-export of the host-side subscription-credential probe.

    Kept as a module-level function (rather than a direct import from
    ``codeband.cli``) so tests can monkeypatch it deterministically without
    reaching into the CLI module.
    """
    from codeband.cli import _has_claude_subscription_oauth as probe

    return probe()


def check_claude_auth(_ctx: Context) -> CheckResult:
    api = os.environ.get("ANTHROPIC_API_KEY")
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    has_sub = _has_claude_subscription_oauth()

    if not api and not oauth and not has_sub:
        return CheckResult(
            Status.FAIL,
            "No Claude auth — neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN set, "
            "and no subscription OAuth on host",
            remediation=(
                "Set one in your .env, or log in once on this host:\n"
                "  ANTHROPIC_API_KEY=sk-ant-...  (pay-per-token)\n"
                "  CLAUDE_CODE_OAUTH_TOKEN=...   (long-lived; `claude setup-token`)\n"
                "  Or run `claude` and log in (stores subscription OAuth locally)."
            ),
        )
    if api and oauth:
        return CheckResult(
            Status.INFO,
            "Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN set — "
            "Codeband will start with OAuth and keep the API key as a usage-limit fallback",
        )
    if api and has_sub and not oauth:
        prefer = os.environ.get("CODEBAND_CLAUDE_PREFER_API_KEY", "").strip().lower()
        if prefer in ("1", "true", "yes", "on"):
            return CheckResult(
                Status.OK,
                "Claude auth: ANTHROPIC_API_KEY (CODEBAND_CLAUDE_PREFER_API_KEY override active — "
                "subscription OAuth will not take precedence)",
            )
        return CheckResult(
            Status.WARN,
            "ANTHROPIC_API_KEY set alongside host subscription OAuth — "
            "Codeband will start with the subscription and keep the API key as a fallback",
            remediation=(
                "This is valid. ANTHROPIC_API_KEY is used only if the Claude "
                "Pro/Max subscription path reports a usage-limit error.\n"
                "Set CODEBAND_CLAUDE_PREFER_API_KEY=1 to force API-key precedence "
                "(e.g. when parallel coders would exhaust subscription rate limits)."
            ),
        )
    if oauth:
        which = "CLAUDE_CODE_OAUTH_TOKEN"
    elif api:
        which = "ANTHROPIC_API_KEY"
    else:
        which = "host subscription OAuth (keychain or ~/.claude/.credentials.json)"
    return CheckResult(Status.OK, f"Claude auth: {which}")


def _needs_codex(ctx: Context) -> bool:
    """True if any worker pool has a Codex entry with count > 0."""
    if ctx.config is None:
        return False
    agents = ctx.config.agents
    for pool_name in ("planners", "plan_reviewers", "coders", "reviewers", "verifiers"):
        pool = getattr(agents, pool_name)
        if pool.entry_for(Framework.CODEX).count > 0:
            return True
    return (
        agents.conductor.framework == Framework.CODEX
        or agents.mergemaster.framework == Framework.CODEX
    )


def check_codex_auth(ctx: Context) -> CheckResult:
    """Codex is authenticated via `OPENAI_API_KEY` or a logged-in `codex` CLI.

    The CLI stores credentials at `~/.codex/auth.json`. We require that
    specific file (not just the `~/.codex/` directory) because `cb up`
    creates `~/.codex/` unconditionally as a bind-mount target, so a bare
    directory is not evidence that `codex login` was ever run.
    """
    if os.environ.get("OPENAI_API_KEY"):
        return CheckResult(Status.OK, "OPENAI_API_KEY set")
    auth_path = Path.home() / ".codex" / "auth.json"
    if auth_path.exists():
        return CheckResult(
            Status.OK, f"Codex CLI login detected ({auth_path})",
        )
    return CheckResult(
        Status.FAIL,
        "Codex agent configured but no auth — set OPENAI_API_KEY or run `codex login --device-auth`",
        remediation=(
            "Either set OPENAI_API_KEY in .env, or run `codex login --device-auth` "
            "(writes ~/.codex/auth.json)."
        ),
    )


def check_band_api_key(_ctx: Context) -> CheckResult:
    if not os.environ.get("BAND_API_KEY"):
        return CheckResult(
            Status.WARN,
            "BAND_API_KEY not set — required to create task rooms (`cb task`)",
            remediation=(
                "Get a human API key from https://app.band.ai and set it in .env:\n"
                "  BAND_API_KEY=band_u_..."
            ),
        )
    return CheckResult(Status.OK, "BAND_API_KEY set")


def check_codeband_yaml(ctx: Context) -> CheckResult:
    path = ctx.project_dir / "codeband.yaml"
    if not path.exists():
        return CheckResult(
            Status.FAIL,
            f"codeband.yaml not found at {path}",
            remediation="Run: cb init --repo <git-url>",
        )
    if ctx.config_error:
        return CheckResult(
            Status.FAIL,
            f"codeband.yaml failed to parse: {ctx.config_error}",
            remediation="Fix the YAML syntax or re-run `cb init`.",
        )
    assert ctx.config is not None
    total = ctx.config.agents.total_agent_count()
    return CheckResult(
        Status.OK,
        f"codeband.yaml OK — {total} agents configured, repo {ctx.config.repo.url}",
    )


def check_agent_config_yaml(ctx: Context) -> CheckResult:
    if ctx.config is None:
        return CheckResult(Status.SKIP, "codeband.yaml not loaded")
    path = ctx.project_dir / "agent_config.yaml"
    if not path.exists():
        return CheckResult(
            Status.WARN,
            "agent_config.yaml not found — agents will be auto-registered on first `cb run`",
            remediation="Or register now: cb setup-agents",
        )
    if ctx.agent_config_error:
        return CheckResult(
            Status.FAIL,
            f"agent_config.yaml failed to parse: {ctx.agent_config_error}",
            remediation="Fix the YAML syntax or re-run `cb setup-agents`.",
        )
    assert ctx.agent_config is not None

    from codeband.orchestration.setup import _expected_agents

    expected = set(_expected_agents(ctx.config).keys())
    missing = expected - set(ctx.agent_config.agents.keys())
    if missing:
        return CheckResult(
            Status.FAIL,
            f"agent_config.yaml missing {len(missing)} agent(s): {', '.join(sorted(missing))}",
            remediation="Run: cb setup-agents",
        )
    return CheckResult(
        Status.OK,
        f"agent_config.yaml OK — {len(ctx.agent_config.agents)} registered",
    )


def check_cross_model_pairing(ctx: Context) -> CheckResult:
    """Warn when author/critic pools would force same-model review.

    Adversarial diversity is the primary benefit of multi-model Codeband.
    If every coder shares a framework with the only active reviewer
    framework (or planner/plan-reviewer do), cross-model review
    degrades to same-model. Also warn when opposite-framework reviewer
    capacity is lower than author capacity, because deterministic prompt
    pairing will make multiple authors share a reviewer slot.
    """
    if ctx.config is None:
        return CheckResult(Status.SKIP, "codeband.yaml not loaded")

    agents = ctx.config.agents
    issues: list[str] = []
    capacity_issues: list[str] = []

    def _check_pair(label: str, author_pool, critic_pool) -> None:
        author_fws = set(author_pool.active_frameworks())
        critic_fws = set(critic_pool.active_frameworks())
        if not author_fws or not critic_fws:
            return  # skip when one side is empty (caught by other checks)
        # If the only active critic framework is one that matches every author,
        # there's no way to form a cross-model pair.
        if len(critic_fws) == 1 and critic_fws <= author_fws:
            only = next(iter(critic_fws))
            issues.append(f"{label}: only {only.value} available → same-model review")
            return

        for fw in author_fws:
            author_count = author_pool.entry_for(fw).count
            opposite = Framework.CODEX if fw == Framework.CLAUDE_SDK else Framework.CLAUDE_SDK
            critic_count = critic_pool.entry_for(opposite).count
            if critic_count > 0 and author_count > critic_count:
                capacity_issues.append(
                    f"{label}: {author_count} {fw.value} authors share "
                    f"{critic_count} {opposite.value} reviewers",
                )

    _check_pair("Planner/Plan Reviewer", agents.planners, agents.plan_reviewers)
    _check_pair("Coder/Code Reviewer", agents.coders, agents.reviewers)

    all_issues = issues + capacity_issues
    if all_issues:
        return CheckResult(
            Status.WARN,
            "Cross-model pairing constrained: " + "; ".join(all_issues),
            remediation=(
                "For adversarial review, enable opposite-framework reviewers and "
                "keep reviewer capacity at least as large as matching author "
                "capacity — e.g. `cb scale reviewers.codex=2` for two Claude coders."
            ),
        )
    return CheckResult(Status.OK, "Cross-model pairing possible for all pools")


def check_verifier_pairing(ctx: Context) -> CheckResult:
    """Warn when the verifier pool can't pair opposite-vendor to any active coder.

    Fires only when at least one verifier is configured (count > 0). When the
    verifier seat is INERT (default count=0) this check is skipped — no noise
    for users who haven't enabled verifiers yet.
    """
    if ctx.config is None:
        return CheckResult(Status.SKIP, "codeband.yaml not loaded")

    agents = ctx.config.agents
    if agents.verifiers.total_count() == 0:
        return CheckResult(Status.SKIP, "verifier seat not active (count=0)")

    coder_fws = set(agents.coders.active_frameworks())
    verifier_fws = set(agents.verifiers.active_frameworks())

    if not coder_fws:
        return CheckResult(Status.SKIP, "no coders configured")

    if len(verifier_fws) == 1 and verifier_fws <= coder_fws:
        only = next(iter(verifier_fws))
        return CheckResult(
            Status.WARN,
            f"Verifier pairing degraded: only {only.value} verifiers → "
            "same-vendor checking (reduced adversarial value)",
            remediation=(
                "For adversarial evidence verification, enable opposite-framework "
                "verifiers — e.g. add `verifiers.codex: {{count: 1}}` when all "
                "coders are claude_sdk."
            ),
        )
    return CheckResult(Status.OK, "Verifier opposite-vendor pairing possible")


def check_agent_count_vs_tier(ctx: Context) -> CheckResult:
    """Warn when total agents > 10 (Band.ai free-tier cap)."""
    if ctx.config is None:
        return CheckResult(Status.SKIP, "codeband.yaml not loaded")
    total = ctx.config.agents.total_agent_count()
    if total > 10:
        return CheckResult(
            Status.WARN,
            f"{total} agents configured — exceeds Band.ai free-tier 10-agent cap",
            remediation=(
                "Either upgrade Band.ai to paid, or reduce pool counts with "
                "`cb scale <pool>.<framework>=<count>`."
            ),
        )
    return CheckResult(Status.OK, f"{total} agents configured (fits free-tier 10-cap)")


def check_workspace_writable(ctx: Context) -> CheckResult:
    if ctx.config is None:
        return CheckResult(Status.SKIP, "codeband.yaml not loaded")
    ws_path = Path(ctx.config.workspace.path)
    if not ws_path.is_absolute():
        ws_path = ctx.project_dir / ws_path
    try:
        ws_path.mkdir(parents=True, exist_ok=True)
        probe = ws_path / ".doctor_write_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            Status.FAIL,
            f"Workspace {ws_path} not writable: {exc}",
            remediation=f"Check permissions on {ws_path.parent} or change `workspace.path` in codeband.yaml.",
        )
    return CheckResult(Status.OK, f"Workspace writable ({ws_path})")


def _conductor_rest_client(ctx: Context):
    """Return an AsyncRestClient using the Conductor's creds, or a CheckResult to short-circuit.

    Three checks below need the same setup: config + agent_config + conductor creds
    + the deferred thenvoi_rest import. Caller does `isinstance(r, CheckResult)`
    to decide whether to return early.
    """
    if ctx.config is None or ctx.agent_config is None:
        return CheckResult(Status.SKIP, "codeband.yaml or agent_config.yaml not loaded")
    conductor = ctx.agent_config.agents.get("conductor")
    if conductor is None:
        return CheckResult(Status.SKIP, "Conductor creds not registered")
    try:
        from thenvoi_rest import AsyncRestClient
    except ImportError as exc:
        return CheckResult(Status.FAIL, f"thenvoi_rest not importable: {exc}")
    return AsyncRestClient(
        api_key=conductor.api_key, base_url=ctx.config.band.rest_url,
    )


async def check_band_rest(ctx: Context) -> CheckResult:
    client = _conductor_rest_client(ctx)
    if isinstance(client, CheckResult):
        return client
    try:
        identity = await asyncio.wait_for(
            client.agent_api_identity.get_agent_me(), timeout=5,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            Status.WARN,
            f"Band.ai REST at {ctx.config.band.rest_url} timed out after 5s",
            remediation="Check network or band.rest_url in codeband.yaml.",
        )
    except Exception as exc:
        return CheckResult(
            Status.FAIL,
            f"Band.ai REST auth failed: {type(exc).__name__}: {exc}",
            remediation=(
                "Check that agent_config.yaml has a valid Conductor api_key. "
                "Re-register with: cb setup-agents"
            ),
        )
    return CheckResult(
        Status.OK,
        f"Band.ai REST OK — Conductor is '{identity.data.name}'",
    )


async def check_active_room_membership(ctx: Context) -> CheckResult:
    """INFO: report which configured agents are present in the active task room.

    With lazy invites, agents are added to the room on demand by the inviting
    agent (Conductor → Planner; Planner → Plan Reviewer; Coder → Reviewer; …).
    A fresh task room contains only the Conductor; everyone else appears as the
    workflow recruits them. This check makes that visible while debugging.
    """
    # Same dual-location read as cb-phase: canonical {workspace}/state/
    # pointer first, legacy <project_dir>/ fallback — a fresh post-relocation
    # registration writes only the canonical location, and this check must
    # not SKIP on it. Without a loaded config the workspace (and canonical
    # pointer) cannot be resolved, so only the legacy location is readable.
    from codeband.state.registration import read_room_pointer, resolve_state_dir

    if ctx.config is not None:
        state_dir = resolve_state_dir(ctx.config, ctx.project_dir)
    else:
        state_dir = ctx.project_dir
    room_id = read_room_pointer(ctx.project_dir, state_dir, warn_legacy=False)
    if not room_id:
        return CheckResult(Status.SKIP, "No active task room (.codeband_room not found)")

    client = _conductor_rest_client(ctx)
    if isinstance(client, CheckResult):
        return client
    try:
        resp = await asyncio.wait_for(
            client.agent_api_participants.list_agent_chat_participants(chat_id=room_id),
            timeout=5,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            Status.WARN,
            f"Timed out listing participants for room {room_id[:8]}…",
            remediation="Check Band.ai REST connectivity (band.rest_url in codeband.yaml).",
        )
    except Exception as exc:
        return CheckResult(
            Status.WARN,
            f"Could not list participants for room {room_id[:8]}…: "
            f"{type(exc).__name__}: {exc}",
            remediation="The room may have been deleted on Band.ai. Run `cb reset` to clear .codeband_room.",
        )

    present_ids = {p.id for p in (resp.data or []) if getattr(p, "id", None)}
    present, pending = [], []
    for key, creds in ctx.agent_config.agents.items():
        (present if creds.agent_id in present_ids else pending).append(key)
    msg = (
        f"Room {room_id[:8]}… — in: {', '.join(sorted(present)) or '(none)'}; "
        f"not yet invited: {', '.join(sorted(pending)) or '(none)'}"
    )
    return CheckResult(Status.INFO, msg)


async def check_memory_mode(ctx: Context) -> CheckResult:
    client = _conductor_rest_client(ctx)
    if isinstance(client, CheckResult):
        return client
    try:
        from codeband.memory import probe_memory_backend, reset_memory_mode
    except ImportError as exc:
        return CheckResult(Status.FAIL, f"Memory module not importable: {exc}")

    reset_memory_mode()  # ensure the doctor probes fresh even if another command cached
    override = (
        ctx.config.band.memory_mode
        if ctx.config.band.memory_mode != "auto"
        else None
    )
    mode = await probe_memory_backend(client, config_override=override)
    if mode == "band":
        return CheckResult(Status.OK, "Memory: Band.ai remote API (paid tier)")
    return CheckResult(
        Status.INFO,
        "Memory: local JSONL store (Band.ai memory unavailable — free tier)",
        remediation=(
            "Free-tier mode works fine on a single machine. Multi-host deployments "
            "require paid Band.ai."
        ),
    )


def check_jam_delivery_sdk_coupling(ctx: Context) -> CheckResult:
    """Tripwire for the jam-delivery transport's coupling to band-sdk internals.

    The opt-in ``CODEBAND_DELIVERY=jam`` path (``codeband/transport/jam_runtime.py``)
    deliberately bypasses the SDK's public ``Agent.create(...).run()`` facade —
    that facade IS the wedging ``/next`` path — and reassembles the pieces beneath
    it. So it depends on internal (and one private) band-sdk surfaces that carry no
    stability promise. This check imports each one and reports clearly if a band-sdk
    upgrade moved or renamed it, surfacing the coupling at upgrade time instead of
    at first jam run. The default ``sdk`` delivery path depends on NONE of this.
    """
    import importlib

    # (module, attribute) pairs the jam runtime reassembles. See jam_runtime.py.
    required = [
        ("thenvoi.platform.link", "ThenvoiLink"),
        ("thenvoi.preprocessing.default", "DefaultPreprocessor"),
        ("thenvoi.runtime.execution", "ExecutionContext"),
        ("thenvoi.runtime.retry_tracker", "MessageRetryTracker"),
        ("thenvoi.client.streaming", "MessageCreatedPayload"),
        ("thenvoi.client.streaming", "MessageMetadata"),
        ("thenvoi.platform.event", "MessageEvent"),
    ]
    missing: list[str] = []
    for mod_name, attr in required:
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001 - any import failure = moved/renamed
            missing.append(f"{mod_name} ({type(exc).__name__})")
            continue
        if not hasattr(mod, attr):
            missing.append(f"{mod_name}.{attr}")

    # The private method the per-room worker calls + the private adapter attr the
    # handshake sets — the most fragile couplings (no underscore-stability promise).
    try:
        from thenvoi.runtime.execution import ExecutionContext as _EC

        if not hasattr(_EC, "_ensure_fresh_context"):
            missing.append("thenvoi.runtime.execution.ExecutionContext._ensure_fresh_context")
    except Exception:  # noqa: BLE001 - already recorded by the import loop above
        pass

    if not missing:
        return CheckResult(
            Status.OK,
            "jam delivery SDK internals present (transport coupling intact)",
        )

    # Severity tracks exposure: FAIL if jam delivery is actually selected (the path
    # in use is broken); WARN otherwise (the opt-in path won't work, but the active
    # sdk path is fine — don't trip the exit code for sdk users).
    jam_selected = os.environ.get("CODEBAND_DELIVERY", "").strip().lower() == "jam" or (
        ctx.config is not None
        and getattr(ctx.config.agents, "delivery", "sdk") == "jam"
    )
    return CheckResult(
        Status.FAIL if jam_selected else Status.WARN,
        "jam delivery transport is incompatible with the installed band-sdk — "
        f"missing/moved internal symbol(s): {', '.join(missing)}",
        remediation=(
            "The opt-in CODEBAND_DELIVERY=jam path reassembles band-sdk internals "
            "(see codeband/transport/jam_runtime.py); a band-sdk upgrade moved one. "
            "Pin band-sdk to the supported range (>=0.2.8,<0.3) or update "
            "jam_runtime.py to the new SDK shapes. The default CODEBAND_DELIVERY=sdk "
            "delivery path is unaffected."
        ),
    )


# ─── registry ───────────────────────────────────────────────────────────────

_CHECKS: list[Check] = [
    Check("Python version", "Environment", check_python_version),
    Check("Claude auth", "Environment", check_claude_auth),
    Check("BAND_API_KEY", "Environment", check_band_api_key),
    Check("Codex auth", "Environment", check_codex_auth, applies_when=_needs_codex),
    Check("git", "Tools", check_git),
    Check("claude CLI", "Tools", check_claude_cli),
    Check("codex CLI", "Tools", check_codex_cli, applies_when=_needs_codex),
    Check("gh CLI", "Tools", check_gh),
    Check("gh authenticated", "Tools", check_gh_auth),
    Check("codeband.yaml", "Config", check_codeband_yaml),
    Check("agent_config.yaml", "Config", check_agent_config_yaml),
    Check("Workspace writable", "Config", check_workspace_writable),
    Check("Cross-model pairing", "Config", check_cross_model_pairing),
    Check("Verifier pairing", "Config", check_verifier_pairing),
    Check("Agent count vs Band.ai tier", "Config", check_agent_count_vs_tier),
    Check("jam delivery SDK coupling", "Environment", check_jam_delivery_sdk_coupling),
    Check("Band.ai REST reachable", "Connectivity", check_band_rest),
    Check("Memory backend", "Connectivity", check_memory_mode),
    Check("Active room membership", "Connectivity", check_active_room_membership),
]


def _load_context(project_dir: Path) -> Context:
    ctx = Context(project_dir=project_dir)
    try:
        ctx.config = load_config(project_dir)
    except FileNotFoundError:
        pass  # check_codeband_yaml will FAIL with a remediation
    except Exception as exc:
        ctx.config_error = str(exc)
    if ctx.config is not None:
        try:
            ctx.agent_config = load_agent_config(project_dir)
        except FileNotFoundError:
            pass
        except Exception as exc:
            ctx.agent_config_error = str(exc)
    return ctx


async def run_all(project_dir: Path) -> tuple[Context, int]:
    """Run every applicable check. Returns (context, exit_code)."""
    ctx = _load_context(project_dir)
    exit_code = 0
    for check in _CHECKS:
        if check.applies_when is not None and not check.applies_when(ctx):
            ctx.results[check.name] = CheckResult(
                Status.SKIP, "Not applicable to this project",
            )
            continue
        try:
            result = check.run(ctx)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:
            result = CheckResult(
                Status.FAIL,
                f"Check raised {type(exc).__name__}: {exc}",
                remediation="This is a bug in cb doctor. Report it.",
            )
        ctx.results[check.name] = result
        if result.status == Status.FAIL:
            exit_code = 1
    return ctx, exit_code


# ─── reporter ───────────────────────────────────────────────────────────────

_STATUS_GLYPH = {
    Status.OK: ("✓", "green"),
    Status.WARN: ("⚠", "yellow"),
    Status.FAIL: ("✗", "red"),
    Status.INFO: ("•", "cyan"),
    Status.SKIP: ("○", "white"),
}


def report(ctx: Context) -> None:
    """Print a grouped, colored report to stdout."""
    groups: dict[str, list[tuple[str, CheckResult]]] = {}
    for check in _CHECKS:
        result = ctx.results.get(check.name)
        if result is None:
            continue
        groups.setdefault(check.category, []).append((check.name, result))

    click.secho("Codeband doctor", bold=True)
    click.echo(f"  project: {ctx.project_dir}\n")

    for category, items in groups.items():
        click.secho(category, bold=True)
        for name, result in items:
            glyph, color = _STATUS_GLYPH[result.status]
            click.echo(
                f"  {click.style(glyph, fg=color)} "
                f"{click.style(name, bold=True)}: {result.message}",
            )
            if result.status in (Status.FAIL, Status.WARN) and result.remediation:
                for line in result.remediation.splitlines():
                    click.echo(f"      → {line}")
        click.echo()

    totals = {s: 0 for s in Status}
    for result in ctx.results.values():
        totals[result.status] += 1
    parts = []
    for status in (Status.OK, Status.WARN, Status.FAIL, Status.INFO, Status.SKIP):
        if totals[status]:
            glyph, color = _STATUS_GLYPH[status]
            parts.append(click.style(f"{totals[status]} {glyph}", fg=color))
    click.echo("Summary: " + "  ".join(parts))
