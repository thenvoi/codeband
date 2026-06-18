"""Tests for the Verifier seat — config/pool/doctor wiring.

Covers:
- VerifiersConfig pool shape and the ACTIVE default (count=1 per vendor)
- WorkerRole.VERIFIER identity string
- WorkerPool.acquire_verifier_for opposite-vendor pairing + fallback
- cb doctor check_verifier_pairing
- verify_acceptance is a known verdict, coupled to a *configured* verifier

The seat is ACTIVE by default now that the Verifier runtime + dispatch exist to
produce the verdict (PR4). An explicitly-inert verifier pool (``VerifiersConfig()``,
count=0) opts back out — tested below alongside the active default. The verdict
leg itself, broken-chain interlock, claim-vs-store audit, merge gating, and role
gate live in test_verifier_acceptance.py.
"""

from __future__ import annotations

from pathlib import Path


from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    Framework,
    FrameworkPool,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
    VerifiersConfig,
)
from codeband.workers import WorkerId, WorkerPool, WorkerRole


def _claude_only_except_codex_verifier() -> CodebandConfig:
    """A config where the ONLY Codex agent is the verifier — used to prove the
    Codex-framework detectors (preflight + doctor) account for verifiers."""
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/a/b.git"),
        agents=AgentsConfig(
            coders=FrameworkPool(claude_sdk=PoolEntry(count=1)),
            reviewers=ReviewersConfig(claude_sdk=PoolEntry(count=1)),
            planners=FrameworkPool(claude_sdk=PoolEntry(count=1)),
            plan_reviewers=PlanReviewersConfig(claude_sdk=PoolEntry(count=1)),
            verifiers=VerifiersConfig(codex=PoolEntry(count=1)),
        ),
    )


# ─── VerifiersConfig ────────────────────────────────────────────────────────

class TestVerifiersConfig:
    def test_default_count_is_one_per_vendor(self):
        """Verifier seat is ACTIVE by default — one verifier per vendor."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        v = config.agents.verifiers
        assert v.claude_sdk.count == 1
        assert v.codex.count == 1

    def test_explicit_inert_count_is_zero(self):
        """A bare ``VerifiersConfig()`` is the explicit opt-out — both counts 0."""
        v = VerifiersConfig()
        assert v.claude_sdk.count == 0
        assert v.codex.count == 0

    def test_default_models_are_set(self):
        """Default models are pre-configured for when the seat is activated."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        v = config.agents.verifiers
        assert v.claude_sdk.model == "claude-opus-4-7"
        assert v.codex.model == "gpt-5.5"

    def test_active_frameworks_both_by_default(self):
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.agents.verifiers.active_frameworks() == [
            Framework.CLAUDE_SDK, Framework.CODEX,
        ]

    def test_active_frameworks_empty_when_explicitly_inert(self):
        assert VerifiersConfig().active_frameworks() == []

    def test_active_frameworks_when_enabled(self):
        v = VerifiersConfig(
            claude_sdk=PoolEntry(count=1),
            codex=PoolEntry(count=1),
        )
        assert v.active_frameworks() == [Framework.CLAUDE_SDK, Framework.CODEX]

    def test_entry_for(self):
        v = VerifiersConfig(
            claude_sdk=PoolEntry(count=2),
            codex=PoolEntry(count=3),
        )
        assert v.entry_for(Framework.CLAUDE_SDK).count == 2
        assert v.entry_for(Framework.CODEX).count == 3

    def test_total_count_two_by_default(self):
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.agents.verifiers.total_count() == 2

    def test_total_count_zero_when_explicitly_inert(self):
        assert VerifiersConfig().total_count() == 0

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Verifier pool survives YAML serialization."""
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=0, model="claude-opus-4-7"),
                    codex=PoolEntry(count=1, model="gpt-5.5"),
                ),
            ),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.agents.verifiers.claude_sdk.count == 0
        assert loaded.agents.verifiers.claude_sdk.model == "claude-opus-4-7"
        assert loaded.agents.verifiers.codex.count == 1
        assert loaded.agents.verifiers.codex.model == "gpt-5.5"

    def test_total_agent_count_reflects_verifier_seats(self):
        """total_agent_count counts active verifier seats (independent of the
        default: compare an explicitly-inert pool against an active one)."""
        inert = AgentsConfig(verifiers=VerifiersConfig())
        active = AgentsConfig(
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=1),
            ),
        )
        assert active.total_agent_count() == inert.total_agent_count() + 2


# ─── Verifier runtime (runner classes + factory) ─────────────────────────────

class TestVerifierRuntime:
    """Permission/sandbox wiring for the Verifier runner classes — a clean
    mirror of the Code Reviewer runtime (isolated scratch dir + gh network).
    """

    def test_claude_verifier_bypasses_permissions(self, tmp_path: Path):
        """Claude verifier bypasses global settings so its gh calls aren't denied."""
        from codeband.agents.verifier import ClaudeVerifierRunner

        adapter = ClaudeVerifierRunner(workspace=str(tmp_path)).adapter
        assert adapter.permission_mode == "bypassPermissions"
        assert adapter.cwd == str(tmp_path)

    def test_codex_verifier_uses_full_access_sandbox(self, tmp_path: Path):
        """Codex verifier needs full access for gh CLI network calls."""
        from codeband.agents.verifier import CodexVerifierRunner

        config = CodexVerifierRunner(workspace=str(tmp_path)).adapter.config
        assert config.cwd == str(tmp_path)
        assert config.sandbox == "danger-full-access"
        assert config.approval_policy == "never"

    def test_codex_verifier_threads_turn_timeout(self, tmp_path: Path):
        """The Codex whole-turn budget is carried through, like the reviewer."""
        from codeband.agents.verifier import CodexVerifierRunner

        runner = CodexVerifierRunner(workspace=str(tmp_path), turn_timeout_seconds=1234)
        assert runner.adapter.config.turn_timeout_s == 1234.0

    def test_factory_selects_runner_and_model_per_framework(self, tmp_path: Path):
        """_create_verifier maps each framework to its runner + the configured model."""
        from codeband.orchestration.runner import _create_verifier

        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        claude = _create_verifier(
            config, workspace=str(tmp_path), framework=Framework.CLAUDE_SDK,
        )
        assert claude.permission_mode == "bypassPermissions"
        assert claude.model == "claude-opus-4-7"  # default verifier model (config)

        codex_cfg = _create_verifier(
            config, workspace=str(tmp_path), framework=Framework.CODEX,
        ).config
        assert codex_cfg.sandbox == "danger-full-access"
        assert codex_cfg.model == "gpt-5.5"


# ─── distributed dispatch + framework-detection wiring ───────────────────────

class TestVerifierDispatchWiring:
    """The verifier is a first-class pool role in the runtime plumbing — role
    derivation (distributed ``run_agent``) and Codex-framework detection
    (preflight + doctor) must recognize it, or a verifier would either fail to
    dispatch or have its Codex auth left unchecked (silent-idle risk)."""

    def test_role_from_key_recognizes_verifier(self):
        from codeband.orchestration.runner import _role_from_key

        assert _role_from_key("verifier-claude_sdk-0") == "verifier"
        assert _role_from_key("verifier-codex-1") == "verifier"

    def test_framework_from_key_for_verifier(self):
        from codeband.orchestration.runner import _framework_from_key

        assert _framework_from_key("verifier-codex-0") == Framework.CODEX
        assert _framework_from_key("verifier-claude_sdk-0") == Framework.CLAUDE_SDK

    def test_preflight_detects_codex_verifier_only_config(self):
        """A Codex verifier alone must trigger the Codex auth preflight —
        otherwise a broken-auth verifier idles silently."""
        from codeband.preflight import _config_uses_codex

        assert _config_uses_codex(_claude_only_except_codex_verifier()) is True

    def test_doctor_needs_codex_for_codex_verifier_only_config(self):
        from codeband.doctor import Context, _needs_codex

        ctx = Context(
            project_dir=Path("/tmp"), config=_claude_only_except_codex_verifier(),
        )
        assert _needs_codex(ctx) is True


# ─── WorkerRole.VERIFIER identity ───────────────────────────────────────────

class TestVerifierWorkerId:
    def test_worker_role_value(self):
        assert WorkerRole.VERIFIER.value == "verifier"

    def test_worker_id_string_claude(self):
        wid = WorkerId(WorkerRole.VERIFIER, Framework.CLAUDE_SDK, 0)
        assert str(wid) == "verifier-claude_sdk-0"

    def test_worker_id_string_codex(self):
        wid = WorkerId(WorkerRole.VERIFIER, Framework.CODEX, 2)
        assert str(wid) == "verifier-codex-2"


# ─── WorkerPool.acquire_verifier_for ────────────────────────────────────────

class TestAcquireVerifierFor:
    def _pool_with_verifiers(self, claude_count=1, codex_count=1) -> WorkerPool:
        p = WorkerPool()
        if claude_count:
            p.register(WorkerRole.VERIFIER, Framework.CLAUDE_SDK, claude_count)
        if codex_count:
            p.register(WorkerRole.VERIFIER, Framework.CODEX, codex_count)
        return p

    def test_opposite_vendor_for_claude_coder(self):
        """Claude coder → Codex verifier (opposite framework)."""
        pool = self._pool_with_verifiers()
        wid = pool.acquire_verifier_for(Framework.CLAUDE_SDK)
        assert wid is not None
        assert wid.role == WorkerRole.VERIFIER
        assert wid.framework == Framework.CODEX

    def test_opposite_vendor_for_codex_coder(self):
        """Codex coder → Claude verifier (opposite framework)."""
        pool = self._pool_with_verifiers()
        wid = pool.acquire_verifier_for(Framework.CODEX)
        assert wid is not None
        assert wid.framework == Framework.CLAUDE_SDK

    def test_fallback_same_vendor_when_opposite_exhausted(self):
        """Falls back to same-framework when no opposite verifier is idle."""
        pool = WorkerPool()
        pool.register(WorkerRole.VERIFIER, Framework.CLAUDE_SDK, 1)
        # Only Claude verifiers — Codex coder falls back to Claude
        wid = pool.acquire_verifier_for(Framework.CODEX)
        assert wid is not None
        assert wid.framework == Framework.CLAUDE_SDK

    def test_returns_none_when_no_verifiers_registered(self):
        """Returns None when the verifier seat is INERT (count=0)."""
        pool = WorkerPool()
        assert pool.acquire_verifier_for(Framework.CLAUDE_SDK) is None
        assert pool.acquire_verifier_for(Framework.CODEX) is None

    def test_acquired_slot_is_busy(self):
        pool = self._pool_with_verifiers()
        wid = pool.acquire_verifier_for(Framework.CLAUDE_SDK)
        assert wid is not None
        # Acquiring again should find the other framework or None
        wid2 = pool.acquire_verifier_for(Framework.CLAUDE_SDK)
        # The Codex slot is now taken; no Claude slot for Claude coder
        assert wid2 is not None or pool.idle_count(WorkerRole.VERIFIER) == 0

    def test_release_makes_verifier_idle_again(self):
        pool = WorkerPool()
        pool.register(WorkerRole.VERIFIER, Framework.CODEX, 1)
        wid = pool.acquire_verifier_for(Framework.CLAUDE_SDK)
        assert wid is not None
        pool.release(wid)
        wid2 = pool.acquire_verifier_for(Framework.CLAUDE_SDK)
        assert wid2 is not None

    def test_task_id_attached(self):
        pool = self._pool_with_verifiers()
        wid = pool.acquire_verifier_for(Framework.CLAUDE_SDK, task_id="task-99")
        assert wid is not None
        snap = {s["worker_id"]: s for s in pool.snapshot()}
        assert snap[str(wid)]["current_task"] == "task-99"


# ─── setup.py registration ───────────────────────────────────────────────────

class TestVerifierAgentRegistration:
    def test_verifier_in_expected_agents_by_default(self):
        """With the active default, verifier keys appear in expected_agents."""
        from codeband.orchestration.setup import _expected_agents

        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        expected = _expected_agents(config)
        assert "verifier-claude_sdk-0" in expected
        assert "verifier-codex-0" in expected

    def test_verifier_absent_from_expected_agents_when_inert(self):
        """An explicitly-inert verifier pool registers no verifier agents."""
        from codeband.orchestration.setup import _expected_agents

        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(verifiers=VerifiersConfig()),
        )
        expected = _expected_agents(config)
        assert not any(k.startswith("verifier-") for k in expected)

    def test_verifier_in_expected_agents_when_enabled(self):
        """With count=1, verifier keys and display names appear."""
        from codeband.orchestration.setup import _expected_agents, _is_codeband_agent

        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=1),
                    codex=PoolEntry(count=1),
                ),
            ),
        )
        expected = _expected_agents(config)
        assert "verifier-claude_sdk-0" in expected
        assert "verifier-codex-0" in expected
        claude_name, _ = expected["verifier-claude_sdk-0"]
        codex_name, _ = expected["verifier-codex-0"]
        assert claude_name == "Verifier-Claude-0"
        assert codex_name == "Verifier-Codex-0"
        assert _is_codeband_agent(claude_name)
        assert _is_codeband_agent(codex_name)
        assert not _is_codeband_agent("Verification-Claude-0")
        assert not _is_codeband_agent("VerifierClaude-0")


# ─── cb doctor check_verifier_pairing ────────────────────────────────────────

class TestDoctorVerifierPairing:
    def _ctx(self, tmp_path, **agents_kwargs):
        from codeband.doctor import Context
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(**agents_kwargs),
        )
        return Context(project_dir=tmp_path, config=config)

    def test_ok_by_default(self, tmp_path):
        """The active default pairs both vendors → doctor reports OK."""
        from codeband.doctor import Status, check_verifier_pairing
        ctx = self._ctx(tmp_path)
        result = check_verifier_pairing(ctx)
        assert result.status == Status.OK

    def test_skips_when_verifiers_inert(self, tmp_path):
        """No warning when the verifier seat is explicitly inert (count=0)."""
        from codeband.doctor import Status, check_verifier_pairing
        ctx = self._ctx(tmp_path, verifiers=VerifiersConfig())
        result = check_verifier_pairing(ctx)
        assert result.status == Status.SKIP

    def test_ok_when_both_vendors_present(self, tmp_path):
        """OK when verifiers can pair opposite-vendor to any coder framework."""
        from codeband.doctor import Status, check_verifier_pairing
        ctx = self._ctx(
            tmp_path,
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=1),
            ),
        )
        result = check_verifier_pairing(ctx)
        assert result.status == Status.OK

    def test_warns_single_vendor_claude_only(self, tmp_path):
        """WARN when coders and verifiers share the only vendor (claude_sdk)."""
        from codeband.doctor import Status, check_verifier_pairing
        ctx = self._ctx(
            tmp_path,
            verifiers=VerifiersConfig(claude_sdk=PoolEntry(count=1)),
        )
        result = check_verifier_pairing(ctx)
        assert result.status == Status.WARN
        assert "claude_sdk" in result.message
        assert "same-vendor" in result.message
        assert result.remediation is not None

    def test_warns_single_vendor_codex_only(self, tmp_path):
        """WARN when verifiers and coders are both Codex-only."""
        from codeband.doctor import Status, check_verifier_pairing
        ctx = self._ctx(
            tmp_path,
            verifiers=VerifiersConfig(codex=PoolEntry(count=1)),
        )
        result = check_verifier_pairing(ctx)
        assert result.status == Status.WARN
        assert "codex" in result.message

    def test_skips_when_no_config(self, tmp_path):
        from codeband.doctor import Context, Status, check_verifier_pairing
        result = check_verifier_pairing(Context(project_dir=tmp_path, config=None))
        assert result.status == Status.SKIP

    def test_in_checks_registry(self):
        """Verifier pairing check is registered in _CHECKS."""
        from codeband.doctor import _CHECKS, check_verifier_pairing
        names = [c.name for c in _CHECKS]
        assert "Verifier pairing" in names
        fns = [c.run for c in _CHECKS]
        assert check_verifier_pairing in fns


# ─── verify_acceptance verdict coupling (leg wired, INERT default) ────────────

class TestVerifierVerdictCoupling:
    def test_known_verdicts_includes_verify_acceptance(self):
        """The verdict leg is wired → verify_acceptance is a known verdict."""
        from codeband.state.registration import KNOWN_VERDICTS
        assert KNOWN_VERDICTS == frozenset(
            {"verify", "review", "verify_acceptance"}
        )

    def test_default_required_verdicts_add_acceptance_when_verifier_configured(self):
        """Resolved verdicts add verify_acceptance iff a verifier is configured.

        The seat is INERT by default, so the gate is exercised by configuring a
        verifier explicitly here. resolve_required_verdicts enforces that
        handoff_verify_command is set when 'verify' is in the list, so we supply
        one to isolate the verdict content check from the precondition check.
        """
        from codeband.state.registration import resolve_required_verdicts
        agents = AgentsConfig(
            handoff_verify_command="make test",
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=1)
            ),
        )
        result = resolve_required_verdicts(agents)
        assert set(result) == {"verify", "review", "verify_acceptance"}

    def test_default_required_verdicts_include_acceptance(self):
        """The ACTIVE default couples verify_acceptance into the snapshot."""
        from codeband.state.registration import resolve_required_verdicts
        # Default AgentsConfig now has a verifier per vendor → acceptance couples.
        agents = AgentsConfig(handoff_verify_command="make test")
        result = resolve_required_verdicts(agents)
        assert set(result) == {"verify", "review", "verify_acceptance"}

    def test_required_verdicts_pair_when_verifiers_explicitly_inert(self):
        """An explicitly-inert verifier pool keeps the verify/review pair."""
        from codeband.state.registration import resolve_required_verdicts
        agents = AgentsConfig(
            handoff_verify_command="make test", verifiers=VerifiersConfig(),
        )
        result = resolve_required_verdicts(agents)
        assert set(result) == {"verify", "review"}
