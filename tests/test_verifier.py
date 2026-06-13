"""Tests for the Verifier seat — config/pool/doctor wiring.

Covers:
- VerifiersConfig pool shape and ACTIVE-by-default counts (PR2)
- WorkerRole.VERIFIER identity string
- WorkerPool.acquire_verifier_for opposite-vendor pairing + fallback
- cb doctor check_verifier_pairing
- verify_acceptance is a known, on-by-default verdict (PR2)

The verdict leg, broken-chain interlock, claim-vs-store audit, merge gating,
and role gate live in test_verifier_acceptance.py.
"""

from __future__ import annotations

from pathlib import Path


from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    Framework,
    PoolEntry,
    RepoConfig,
    VerifiersConfig,
)
from codeband.workers import WorkerId, WorkerPool, WorkerRole


# ─── VerifiersConfig ────────────────────────────────────────────────────────

class TestVerifiersConfig:
    def test_default_count_is_one_per_vendor(self):
        """Verifier seat is ACTIVE by default (PR2) — one verifier per vendor."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        v = config.agents.verifiers
        assert v.claude_sdk.count == 1
        assert v.codex.count == 1

    def test_default_models_are_set(self):
        """Default models are pre-configured for when the seat is activated."""
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        v = config.agents.verifiers
        assert v.claude_sdk.model == "claude-opus-4-7"
        assert v.codex.model == "gpt-5.4"

    def test_active_frameworks_default_both_vendors(self):
        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        assert config.agents.verifiers.active_frameworks() == [
            Framework.CLAUDE_SDK,
            Framework.CODEX,
        ]

    def test_active_frameworks_empty_when_disabled(self):
        v = VerifiersConfig(
            claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
        )
        assert v.active_frameworks() == []

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

    def test_yaml_roundtrip(self, tmp_path: Path):
        """Verifier pool survives YAML serialization."""
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=0, model="claude-opus-4-7"),
                    codex=PoolEntry(count=1, model="gpt-5.4"),
                ),
            ),
        )
        yaml_path = tmp_path / "codeband.yaml"
        config.to_yaml(yaml_path)
        loaded = CodebandConfig.from_yaml(yaml_path)
        assert loaded.agents.verifiers.claude_sdk.count == 0
        assert loaded.agents.verifiers.claude_sdk.model == "claude-opus-4-7"
        assert loaded.agents.verifiers.codex.count == 1
        assert loaded.agents.verifiers.codex.model == "gpt-5.4"

    def test_total_agent_count_includes_verifiers(self):
        """total_agent_count reflects active verifier seats."""
        no_verifiers = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
                ),
            ),
        )
        baseline = no_verifiers.agents.total_agent_count()

        with_verifiers = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=2)
                ),
            ),
        )
        assert with_verifiers.agents.total_agent_count() == baseline + 2


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
    def test_verifier_present_in_expected_agents_by_default(self):
        """The default config (PR2) registers a verifier per vendor."""
        from codeband.orchestration.setup import _expected_agents

        config = CodebandConfig(repo=RepoConfig(url="https://github.com/a/b.git"))
        expected = _expected_agents(config)
        assert "verifier-claude_sdk-0" in expected
        assert "verifier-codex-0" in expected

    def test_verifier_absent_from_expected_agents_when_disabled(self):
        """With both counts 0, no verifier keys appear in expected_agents."""
        from codeband.orchestration.setup import _expected_agents

        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(
                verifiers=VerifiersConfig(
                    claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
                ),
            ),
        )
        expected = _expected_agents(config)
        assert not any(k.startswith("verifier-") for k in expected)

    def test_verifier_in_expected_agents_when_enabled(self):
        """With count=1, verifier keys and display names appear."""
        from codeband.orchestration.setup import _expected_agents

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


# ─── cb doctor check_verifier_pairing ────────────────────────────────────────

class TestDoctorVerifierPairing:
    def _ctx(self, tmp_path, **agents_kwargs):
        from codeband.doctor import Context
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/a/b.git"),
            agents=AgentsConfig(**agents_kwargs),
        )
        return Context(project_dir=tmp_path, config=config)

    def test_skips_when_verifiers_disabled(self, tmp_path):
        """No warning when both verifier counts are 0 (seat disabled)."""
        from codeband.doctor import Status, check_verifier_pairing
        ctx = self._ctx(
            tmp_path,
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
            ),
        )
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


# ─── ACTIVE (PR2): verify_acceptance is a known, on-by-default verdict ────────

class TestVerifierVerdictActivation:
    def test_known_verdicts_includes_verify_acceptance(self):
        """PR2 wires the verdict leg → verify_acceptance is a known verdict."""
        from codeband.state.registration import KNOWN_VERDICTS
        assert KNOWN_VERDICTS == frozenset(
            {"verify", "review", "verify_acceptance"}
        )

    def test_default_required_verdicts_include_acceptance_when_active(self):
        """Default resolved verdicts add verify_acceptance when a verifier is on.

        resolve_required_verdicts enforces that handoff_verify_command is set
        when 'verify' is in the list, so we supply one to isolate the verdict
        content check from the precondition check.
        """
        from codeband.state.registration import resolve_required_verdicts
        # Default AgentsConfig has verifiers active (1 per vendor).
        agents = AgentsConfig(handoff_verify_command="make test")
        result = resolve_required_verdicts(agents)
        assert set(result) == {"verify", "review", "verify_acceptance"}

    def test_default_required_verdicts_pair_when_verifiers_disabled(self):
        """With no verifier configured, the default stays the verify/review pair."""
        from codeband.state.registration import resolve_required_verdicts
        agents = AgentsConfig(
            handoff_verify_command="make test",
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
            ),
        )
        result = resolve_required_verdicts(agents)
        assert set(result) == {"verify", "review"}
