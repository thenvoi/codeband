"""Tests for Stage-3 peer governance (PR3).

The reconcile-requires-grant branch of ``cb-phase merge`` (3a), and the prompt
contracts for the universal scope rule (3b) and the Conductor's verify-claims
duty (3c).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codeband.cli import handoff, merge
from codeband.cli.merge import EXIT_MERGE_FAILED
from codeband.state.fsm import transition
from codeband.state.store import StateStore

TASK = "room-1"
SHA = "sha-merged"


def _drive_to_merge_pending(store, sid="st-1", *, head=SHA):
    for new_state, role, sha in [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", head),
        ("review_passed", "reviewer", head),
        ("merge_pending", "mergemaster", head),
    ]:
        transition(sid, TASK, new_state, caller_role=role, store=store, head_sha=sha)


@pytest.fixture
def store(tmp_path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.register_task_atomic(
        task_id=TASK, description="demo", room_id=TASK,
        owner_id="owner-1", owner_handle="yoni",
        required_verdicts=["verify", "review"], merge_approval="owner",
    )
    _drive_to_merge_pending(s, "st-1")
    s.set_pr_number("st-1", TASK, 42)
    return s


@pytest.fixture
def env(monkeypatch, store):
    """Reconcile env: PR already MERGED, plus an audit-hook capture."""
    pr = {"state": "MERGED", "mergeable": "MERGEABLE", "headRefOid": SHA,
          "headRefName": "feat-x"}
    audit_calls: list[tuple] = []

    monkeypatch.setattr(merge, "_resolve_store", lambda project_dir: store)
    monkeypatch.setattr(
        merge, "_resolve_task_id",
        lambda project_dir, store, task_arg: (TASK, None),
    )
    monkeypatch.setattr(merge, "_pr_snapshot", lambda *a, **k: dict(pr))
    monkeypatch.setattr(
        merge, "load_config",
        lambda project_dir: SimpleNamespace(
            repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
            agents=SimpleNamespace(max_rebase_rounds=3),
        ),
    )
    monkeypatch.setattr(merge, "_delete_remote_branch", lambda *a, **k: None)

    # Capture the ungated_external_merge audit hook (the append_audit_event
    # primitive ships in PR1; we inject a fake so the hook wiring is testable
    # on this branch and after rebase alike).
    def _fake_audit(event_type, *, task_id=None, subtask_id=None, payload=None):
        audit_calls.append((event_type, task_id, subtask_id, payload))

    store.append_audit_event = _fake_audit  # type: ignore[attr-defined]
    return SimpleNamespace(store=store, pr=pr, audit_calls=audit_calls)


def _run():
    return handoff.main(["merge", "st-1"])


# ── 3a: reconcile requires a grant ───────────────────────────────────────────

def test_reconcile_grant_match_records_merged(env):
    """Our own merge raced/crashed: a grant matching the merged head → merged."""
    env.store.record_merge_approval("st-1", TASK, approved_by="owner", approved_sha=SHA)

    assert _run() == 0
    assert env.store.get_subtask("st-1", TASK).state == "merged"
    # No ungated event for the sanctioned path.
    assert env.audit_calls == []


def test_reconcile_grant_absent_blocks_ungated(env, capsys):
    """No grant at all → blocked [ungated_external_merge] + audit event."""
    assert _run() == EXIT_MERGE_FAILED
    assert env.store.get_subtask("st-1", TASK).state == "blocked"
    err = capsys.readouterr().err
    assert "[ungated_external_merge]" in err
    assert SHA in err
    # The audit hook fired with the merged sha + absent grant.
    assert len(env.audit_calls) == 1
    event_type, _t, sid, payload = env.audit_calls[0]
    assert event_type == "ungated_external_merge"
    assert sid == "st-1"
    assert payload["merged_sha"] == SHA
    assert payload["grant_sha"] is None


def test_reconcile_grant_mismatch_blocks_ungated(env, capsys):
    """A grant for a DIFFERENT sha than the merged head → blocked + audit."""
    env.store.record_merge_approval(
        "st-1", TASK, approved_by="owner", approved_sha="stale-sha",
    )

    assert _run() == EXIT_MERGE_FAILED
    assert env.store.get_subtask("st-1", TASK).state == "blocked"
    err = capsys.readouterr().err
    assert "[ungated_external_merge]" in err
    assert len(env.audit_calls) == 1
    _e, _t, _s, payload = env.audit_calls[0]
    assert payload["grant_sha"] == "stale-sha"
    assert payload["merged_sha"] == SHA


def test_ungated_blocked_records_reason_for_watchdog(env):
    """The blocked transition carries the ungated reason (watchdog reads it)."""
    import sqlite3

    _run()
    conn = sqlite3.connect(env.store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT reason FROM transition_log WHERE subtask_id = 'st-1' "
            "AND to_state = 'blocked' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert "ungated_external_merge" in row["reason"]


# ── 3b: scope rule in EVERY role prompt ──────────────────────────────────────

_ROLE_PROMPTS = [
    "code_reviewer", "coder", "conductor", "mergemaster",
    "plan_reviewer", "planner",
]

_SCOPE_SENTENCE = (
    "Operate only on the PR, branch, and worktree assigned by your current task."
)


@pytest.mark.parametrize("role", _ROLE_PROMPTS)
def test_scope_rule_present_in_every_role_prompt(role):
    text = Path(f"src/codeband/prompts/{role}.md").read_text(encoding="utf-8")
    assert _SCOPE_SENTENCE in text
    assert "REPORT it in the room instead of acting" in text


# ── 3c: conductor verify-claims duty ─────────────────────────────────────────

def test_conductor_prompt_pins_verify_claims_duty():
    text = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")
    assert "Verify claims before acting" in text
    assert "cb status" in text
    assert "treated as **not having happened**" in text
    # Names the protocol effects it must verify.
    for effect in ("merged", "abandoned", "approved", "blocked", "resumed"):
        assert effect in text


# ── 3d: conductor acceptance-verification dispatch carries acceptance criteria ─

def test_conductor_acceptance_dispatch_includes_acceptance_criteria():
    """The Acceptance Verification Protocol dispatch must include the task's
    acceptance criteria so the Verifier has the contract to check against."""
    text = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")
    # The dispatch line must name all five fields — PR URL, subtask, task key,
    # branch, and acceptance criteria.
    assert "acceptance criteria" in text
    # Specifically within the Acceptance Verification Protocol section.
    avp_start = text.index("### Acceptance Verification Protocol")
    avp_end = text.index("\n###", avp_start + 1)
    avp_text = text[avp_start:avp_end]
    assert "acceptance criteria" in avp_text
    assert "PR URL" in avp_text or "PR url" in avp_text.lower()
    assert "subtask" in avp_text
    assert "task key" in avp_text
    assert "branch" in avp_text
