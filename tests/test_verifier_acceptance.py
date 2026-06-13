"""Tests for the Verifier acceptance gate (PR2).

The activation of the Verifier seat: the ``cb-phase verify-acceptance`` leg, the
``verify_acceptance`` merge verdict, the broken-chain interlock, the
claim-vs-store audit, the role gate, and the registration coupling that makes
acceptance on-by-default exactly when a verifier is configured.

Deterministic throughout — real SQLite, real FSM, no network. The ``cb-phase``
leg tests monkeypatch only the store/task/PR-head resolvers, exactly like the
review-leg tests in ``test_handoff.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

from codeband.cli import handoff
from codeband.config import AgentsConfig, Framework, PoolEntry, VerifiersConfig
from codeband.state.fsm import check_merge_eligibility, transition
from codeband.state.registration import register_task, resolve_required_verdicts
from codeband.state.store import StateStore
from codeband.workers import WorkerPool, WorkerRole


# ─── fixtures / helpers ───────────────────────────────────────────────────────


def _verifier_agents(**overrides) -> AgentsConfig:
    """AgentsConfig with an executable verify leg and an active verifier."""
    overrides.setdefault("handoff_verify_command", "true")
    return AgentsConfig(**overrides)


@pytest.fixture
def store(tmp_path) -> StateStore:
    """A store with ``st-1`` at ``review_passed``, verify+review pinned to sha-1.

    The acceptance verdict (and the merge gate) read these same
    ``transition_log`` rows, so the fixture drives real transitions with
    ``head_sha`` pinned exactly as ``cb-phase`` does.
    """
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.register_task_atomic(
        task_id="room-1",
        description="demo",
        room_id="room-1",
        owner_id="owner-1",
        required_verdicts=["verify", "review", "verify_acceptance"],
    )
    for new_state, role, sha in [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", "sha-1"),
        ("review_passed", "reviewer", "sha-1"),
    ]:
        transition("st-1", "room-1", new_state, caller_role=role, store=s, head_sha=sha)
    return s


def _run_acceptance(monkeypatch, store, *flags, claim=None, head="sha-1"):
    monkeypatch.setattr(handoff, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda project_dir, s, task_arg: ("room-1", None),
    )
    monkeypatch.setattr(handoff, "_pr_head_sha", lambda project_dir, pr: head)
    argv = ["verify-acceptance", "st-1", "--task", "room-1", "--pr", "42", *flags]
    if claim is not None:
        argv += ["--claim", claim]
    return handoff.main(argv)


# ─── merge gating: verify_acceptance is a required, SHA-pinned verdict ────────


def test_acceptance_gates_merge_like_review(store):
    """Without a passing acceptance verdict, a verifier task is not merge-eligible."""
    # verify + review are pinned to sha-1, but no acceptance verdict yet.
    before = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert before.eligible is False
    assert any(r.startswith("missing_verdict verify_acceptance") for r in before.reasons)

    # The Verifier accepts at the same head → all three verdicts pinned to sha-1.
    transition(
        "st-1", "room-1", "acceptance_passed",
        caller_role="verifier", store=store, head_sha="sha-1",
    )
    after = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert after.eligible is True
    assert after.reasons == []


def test_acceptance_pinned_to_wrong_sha_is_stale(store):
    """An acceptance verdict at a different head does not satisfy the merge SHA."""
    transition(
        "st-1", "room-1", "acceptance_passed",
        caller_role="verifier", store=store, head_sha="sha-2",
    )
    result = check_merge_eligibility("room-1", "st-1", "sha-1", store=store)
    assert result.eligible is False
    assert any(r.startswith("stale_verdict verify_acceptance") for r in result.reasons)


# ─── the cb-phase verify-acceptance leg ───────────────────────────────────────


def test_accept_advances_to_acceptance_passed(store, monkeypatch):
    assert _run_acceptance(monkeypatch, store, "--accept") == 0
    assert store.get_subtask("st-1", "room-1").state == "acceptance_passed"


def test_accept_pins_the_pr_head_sha(store, monkeypatch):
    _run_acceptance(monkeypatch, store, "--accept", head="sha-1")
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM transition_log WHERE to_state = 'acceptance_passed' "
            "ORDER BY id DESC LIMIT 1",
        ).fetchone()
    finally:
        conn.close()
    assert row["head_sha"] == "sha-1"
    assert row["caller_role"] == "verifier"


def test_reject_advances_to_review_failed_riding_the_cap(store, monkeypatch):
    assert _run_acceptance(monkeypatch, store, "--reject") == 0
    sub = store.get_subtask("st-1", "room-1")
    assert sub.state == "review_failed"
    assert sub.review_round == 1  # an acceptance reject is one review round


def test_accept_illegal_from_non_review_passed_writes_nothing(store, monkeypatch, capsys):
    # Move the subtask off review_passed via a legal edge (a merge-gate
    # send-back), so the acceptance edge is no longer available.
    transition(
        "st-1", "room-1", "needs_rebase",
        caller_role="mergemaster", store=store, head_sha="sha-1",
    )
    assert _run_acceptance(monkeypatch, store, "--accept") == 1
    assert store.get_subtask("st-1", "room-1").state == "needs_rebase"
    assert "acceptance verdict rejected" in capsys.readouterr().err


def test_requires_an_explicit_verdict():
    with pytest.raises(SystemExit):
        handoff.main(["verify-acceptance", "st-1", "--task", "room-1", "--pr", "42"])


def test_head_unresolved_records_nothing(store, monkeypatch, capsys):
    code = _run_acceptance(monkeypatch, store, "--accept", head=None)
    assert code == handoff.EXIT_HEAD_UNRESOLVED
    assert store.get_subtask("st-1", "room-1").state == "review_passed"
    assert "head_unresolved" in capsys.readouterr().err


# ─── broken-chain interlock ───────────────────────────────────────────────────


def _break_chain(store: StateStore) -> None:
    """Tamper a hashed business column without recomputing row_hash."""
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            "UPDATE transition_log SET reason = 'TAMPERED' WHERE id = "
            "(SELECT MIN(id) FROM transition_log)",
        )
        conn.commit()
    finally:
        conn.close()


def test_broken_chain_blocks_passing_verdict(store, monkeypatch, capsys):
    _break_chain(store)
    code = _run_acceptance(monkeypatch, store, "--accept")
    assert code == handoff.EXIT_CHAIN_BROKEN
    # The verdict was NOT issued — subtask still rests at review_passed.
    assert store.get_subtask("st-1", "room-1").state == "review_passed"
    assert "chain_broken" in capsys.readouterr().err


def test_broken_chain_does_not_block_a_reject(store, monkeypatch):
    # A reject is not a passing verdict, so the interlock does not apply — the
    # subtask can still be sent back even over a compromised ledger.
    _break_chain(store)
    assert _run_acceptance(monkeypatch, store, "--reject") == 0
    assert store.get_subtask("st-1", "room-1").state == "review_failed"


# ─── claim-vs-store audit ─────────────────────────────────────────────────────


def test_claim_divergence_blocks_acceptance(store, monkeypatch, capsys):
    # The agent claimed "merged" but the store FSM state is review_passed.
    code = _run_acceptance(monkeypatch, store, "--accept", claim="merged")
    assert code == handoff.EXIT_CLAIM_MISMATCH
    assert store.get_subtask("st-1", "room-1").state == "review_passed"
    assert "claim_divergence" in capsys.readouterr().err


def test_claim_approved_without_grant_diverges(store, monkeypatch):
    # "approved" asserts a SHA-pinned merge grant exists; none does here.
    code = _run_acceptance(monkeypatch, store, "--accept", claim="approved")
    assert code == handoff.EXIT_CLAIM_MISMATCH
    assert store.get_subtask("st-1", "room-1").state == "review_passed"


def test_matching_claim_passes(store, monkeypatch):
    # A truthful claim at acceptance time is review_passed.
    assert _run_acceptance(monkeypatch, store, "--accept", claim="review_passed") == 0
    assert store.get_subtask("st-1", "room-1").state == "acceptance_passed"


def test_no_claim_skips_the_audit(store, monkeypatch):
    assert _run_acceptance(monkeypatch, store, "--accept") == 0
    assert store.get_subtask("st-1", "room-1").state == "acceptance_passed"


# ─── role gate: only the verifier role may run the leg ────────────────────────


def test_role_gate_allows_verifier(store, monkeypatch):
    monkeypatch.setenv("CODEBAND_ROLE", "verifier")
    assert _run_acceptance(monkeypatch, store, "--accept") == 0
    assert store.get_subtask("st-1", "room-1").state == "acceptance_passed"


def test_role_gate_blocks_non_verifier(store, monkeypatch, capsys):
    monkeypatch.setenv("CODEBAND_ROLE", "reviewer")
    code = _run_acceptance(monkeypatch, store, "--accept")
    assert code == handoff.EXIT_ROLE_MISMATCH
    # The gate fires before dispatch — nothing written.
    assert store.get_subtask("st-1", "room-1").state == "review_passed"
    assert "role_mismatch" in capsys.readouterr().err


def test_role_gate_unset_role_allowed(store, monkeypatch):
    monkeypatch.delenv("CODEBAND_ROLE", raising=False)
    assert _run_acceptance(monkeypatch, store, "--accept") == 0


def test_verify_acceptance_in_role_allowlist():
    assert handoff._ROLE_ALLOWED["verify-acceptance"] == frozenset({"verifier"})


# ─── registration coupling: on-by-default with a verifier, loud without ───────


def test_explicit_acceptance_without_verifier_fails(tmp_path):
    store = StateStore(tmp_path / "state" / "orchestration.db")
    agents = _verifier_agents(
        required_verdicts=["verify", "review", "verify_acceptance"],
        verifiers=VerifiersConfig(
            claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
        ),
    )
    with pytest.raises(ValueError, match="no verifier is configured"):
        register_task(
            room_id="room-1", description="t", owner_id="owner-1",
            agents=agents, project_dir=tmp_path, store=store,
        )
    assert store.get_task("room-1") is None


def test_acceptance_snapshotted_when_verifier_active(tmp_path):
    store = StateStore(tmp_path / "state" / "orchestration.db")
    register_task(
        room_id="room-1", description="t", owner_id="owner-1",
        agents=_verifier_agents(), project_dir=tmp_path, store=store,
    )
    task = store.get_task("room-1")
    assert task is not None
    assert "verify_acceptance" in task.required_verdicts


def test_resolve_includes_acceptance_only_when_verifier_present():
    with_verifier = resolve_required_verdicts(_verifier_agents())
    assert "verify_acceptance" in with_verifier

    without = resolve_required_verdicts(
        _verifier_agents(
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=0), codex=PoolEntry(count=0)
            ),
        )
    )
    assert "verify_acceptance" not in without


# ─── vendor pairing: opposite-vendor produces the verdict, single-vendor warns ─


def _pool(claude: int, codex: int) -> WorkerPool:
    pool = WorkerPool()
    if claude:
        pool.register(WorkerRole.VERIFIER, Framework.CLAUDE_SDK, claude)
    if codex:
        pool.register(WorkerRole.VERIFIER, Framework.CODEX, codex)
    return pool


def test_opposite_vendor_verifier_acquired_then_records_verdict(store, monkeypatch):
    # A Claude coder pairs with the opposite-vendor (Codex) verifier...
    pool = _pool(claude=1, codex=1)
    vid = pool.acquire_verifier_for(Framework.CLAUDE_SDK)
    assert vid is not None
    assert vid.framework == Framework.CODEX

    # ...and that verifier renders the SHA-pinned verdict through the leg.
    monkeypatch.setenv("CODEBAND_ROLE", "verifier")
    assert _run_acceptance(monkeypatch, store, "--accept") == 0
    assert store.get_subtask("st-1", "room-1").state == "acceptance_passed"


def test_single_vendor_degrades_with_doctor_warn_not_failure(tmp_path):
    from codeband.config import CodebandConfig, FrameworkPool, RepoConfig
    from codeband.doctor import Context, Status, check_verifier_pairing

    # All-Claude coders + Claude-only verifiers: same-vendor checking. This is a
    # degrade (doctor WARN), never a hard fail — registration still succeeds.
    config = CodebandConfig(
        repo=RepoConfig(url="https://github.com/a/b.git"),
        agents=AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)
            ),
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)
            ),
        ),
    )
    result = check_verifier_pairing(Context(project_dir=tmp_path, config=config))
    assert result.status == Status.WARN

    # And the required-verdict resolution still couples on (no fail-loud).
    resolved = resolve_required_verdicts(
        _verifier_agents(
            verifiers=VerifiersConfig(
                claude_sdk=PoolEntry(count=1), codex=PoolEntry(count=0)
            ),
        )
    )
    assert "verify_acceptance" in resolved
