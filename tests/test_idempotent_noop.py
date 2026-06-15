"""Tests for the record-based no-op transition classification.

A refused FSM transition is a NO-OP (exit 0) iff a passing record for the
requested to_state at the caller's exact head_sha already exists in
transition_log. Anything else (moved head, anomalous split, no head_sha) is
a loud Illegal (InvalidTransitionError / non-zero).
"""

from __future__ import annotations

import pytest

from codeband.cli import handoff, merge
from codeband.state.fsm import (
    InvalidTransitionError,
    NoOpTransitionError,
    transition,
)
from codeband.state.store import StateStore

TASK = "room-1"
SHA1 = "sha-aaa111"
SHA2 = "sha-bbb222"


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_store(tmp_path) -> StateStore:
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(task_id=TASK, description="demo", room_id=TASK)
    return s


def _drive_to_review_passed(store, sha=SHA1):
    for new_state, role, head in [
        ("assigned", "conductor", None),
        ("in_progress", "coder", None),
        ("verify_pending", "coder", None),
        ("review_pending", "coder", sha),
        ("review_passed", "reviewer", sha),
    ]:
        transition("st-1", TASK, new_state, caller_role=role, store=store, head_sha=head)


def _drive_to_acceptance_passed(store, sha=SHA1):
    _drive_to_review_passed(store, sha=sha)
    transition("st-1", TASK, "acceptance_passed", caller_role="verifier",
               store=store, head_sha=sha)


# ── FSM-level unit tests ──────────────────────────────────────────────────────

class TestNoOpTransitionFSM:
    """Direct FSM tests for NoOpTransitionError classification."""

    def test_reviewer_approve_exact_dup_raises_noop(self, tmp_path):
        """review --approve when state already review_passed, same head → NO-OP."""
        store = _make_store(tmp_path)
        _drive_to_review_passed(store, sha=SHA1)

        with pytest.raises(NoOpTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA1)

        msg = str(exc_info.value)
        assert "NO-OP" in msg
        assert "already_review_passed" in msg
        assert SHA1 in msg
        assert "review_passed" in msg  # state echoed

    def test_reviewer_approve_forward_past_raises_noop(self, tmp_path):
        """review --approve when state already acceptance_passed → NO-OP.

        The review_passed record at SHA1 is in transition_log, so even though
        the current state is acceptance_passed (past review_passed), the record
        check finds the row and raises NoOpTransitionError.
        """
        store = _make_store(tmp_path)
        _drive_to_acceptance_passed(store, sha=SHA1)

        with pytest.raises(NoOpTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA1)

        msg = str(exc_info.value)
        assert "NO-OP" in msg
        assert "already_review_passed" in msg

    def test_verify_acceptance_exact_dup_raises_noop(self, tmp_path):
        """verify-acceptance --pass when already acceptance_passed → NO-OP."""
        store = _make_store(tmp_path)
        _drive_to_acceptance_passed(store, sha=SHA1)

        with pytest.raises(NoOpTransitionError) as exc_info:
            transition("st-1", TASK, "acceptance_passed", caller_role="verifier",
                       store=store, head_sha=SHA1)

        msg = str(exc_info.value)
        assert "NO-OP" in msg
        assert "already_acceptance_passed" in msg

    def test_no_state_change_on_noop(self, tmp_path):
        """NO-OP writes nothing — durable state is unchanged."""
        store = _make_store(tmp_path)
        _drive_to_review_passed(store, sha=SHA1)

        with pytest.raises(NoOpTransitionError):
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA1)

        assert store.get_subtask("st-1", TASK).state == "review_passed"

    def test_moved_head_is_illegal_not_noop(self, tmp_path):
        """review_passed@SHA1 recorded, attempt review_passed@SHA2 → Illegal."""
        store = _make_store(tmp_path)
        _drive_to_review_passed(store, sha=SHA1)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA2)

        assert not isinstance(exc_info.value, NoOpTransitionError)
        assert "Illegal transition" in str(exc_info.value)

    def test_anomalous_split_is_illegal(self, tmp_path):
        """review_passed@SHA1 + acceptance_passed@SHA2; re-attempt review_passed@SHA2 → Illegal.

        No review_passed record exists at SHA2, so there is no matching row and
        the refusal escalates to loud Illegal.
        """
        store = _make_store(tmp_path)
        # Drive to review_passed@SHA1
        _drive_to_review_passed(store, sha=SHA1)
        # Manually record acceptance_passed@SHA2 by driving there with SHA2 on the
        # verifier leg (verifier can enter acceptance_passed from review_passed).
        transition("st-1", TASK, "acceptance_passed", caller_role="verifier",
                   store=store, head_sha=SHA2)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA2)

        assert not isinstance(exc_info.value, NoOpTransitionError)
        assert "Illegal transition" in str(exc_info.value)

    def test_refused_transition_head_sha_none_is_illegal(self, tmp_path):
        """Refused transition with head_sha=None → Illegal (no record check)."""
        store = _make_store(tmp_path)
        _drive_to_review_passed(store, sha=SHA1)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=None)

        assert not isinstance(exc_info.value, NoOpTransitionError)
        assert "Illegal transition" in str(exc_info.value)


class TestIllegalRemainsIllegal:
    """Branch states stay loud Illegal — not silently NO-OP."""

    def test_review_from_blocked_is_illegal(self, tmp_path):
        """forward verb when state is blocked → unchanged Illegal transition."""
        store = _make_store(tmp_path)
        _drive_to_review_passed(store, sha=SHA1)
        transition("st-1", TASK, "blocked", caller_role="watchdog", store=store)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA1)

        assert "Illegal transition" in str(exc_info.value)
        assert not isinstance(exc_info.value, NoOpTransitionError)

    def test_review_from_needs_rebase_is_illegal(self, tmp_path):
        """forward verb when state is needs_rebase → unchanged Illegal transition."""
        store = _make_store(tmp_path)
        _drive_to_review_passed(store, sha=SHA1)
        transition("st-1", TASK, "needs_rebase", caller_role="mergemaster", store=store)

        with pytest.raises(InvalidTransitionError) as exc_info:
            transition("st-1", TASK, "review_passed", caller_role="reviewer",
                       store=store, head_sha=SHA1)

        assert "Illegal transition" in str(exc_info.value)
        assert not isinstance(exc_info.value, NoOpTransitionError)


# ── CLI-level integration tests ───────────────────────────────────────────────

def _make_review_store(tmp_path, sha=SHA1):
    """Store with st-1 at review_passed, wired for review commands."""
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(task_id=TASK, description="demo", room_id=TASK)
    _drive_to_review_passed(s, sha=sha)
    return s


def _patch_review_env(monkeypatch, store, pr_sha=SHA1):
    monkeypatch.setattr(handoff, "_resolve_store", lambda p: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda p, s, t: (TASK, None),
    )
    monkeypatch.setattr(handoff, "_pr_head_sha", lambda p, n: pr_sha)


class TestReviewCommandNoop:
    """cb-phase review --approve exits 0 with NO-OP when already satisfied."""

    def test_exact_dup_exits_zero(self, tmp_path, monkeypatch, capsys):
        store = _make_review_store(tmp_path, sha=SHA1)
        _patch_review_env(monkeypatch, store, pr_sha=SHA1)

        rc = handoff.main(["review", "st-1", "--pr", "42", "--approve"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NO-OP" in out
        assert "already_review_passed" in out

    def test_exact_dup_no_state_change(self, tmp_path, monkeypatch):
        store = _make_review_store(tmp_path, sha=SHA1)
        _patch_review_env(monkeypatch, store, pr_sha=SHA1)

        handoff.main(["review", "st-1", "--pr", "42", "--approve"])
        assert store.get_subtask("st-1", TASK).state == "review_passed"

    def test_forward_past_acceptance_exits_zero(self, tmp_path, monkeypatch, capsys):
        store = StateStore(tmp_path / "state" / "orchestration.db")
        store.create_task(task_id=TASK, description="demo", room_id=TASK)
        _drive_to_acceptance_passed(store, sha=SHA1)
        _patch_review_env(monkeypatch, store, pr_sha=SHA1)

        rc = handoff.main(["review", "st-1", "--pr", "42", "--approve"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NO-OP" in out

    def test_moved_head_illegal_non_zero(self, tmp_path, monkeypatch, capsys):
        """review --approve when head SHA has moved → non-zero Illegal (not STALE)."""
        store = _make_review_store(tmp_path, sha=SHA1)
        _patch_review_env(monkeypatch, store, pr_sha=SHA2)

        rc = handoff.main(["review", "st-1", "--pr", "42", "--approve"])
        assert rc != 0
        err = capsys.readouterr().err
        assert "Illegal transition" in err

    def test_blocked_state_illegal_non_zero(self, tmp_path, monkeypatch, capsys):
        """review --approve when state is blocked → non-zero Illegal."""
        store = _make_review_store(tmp_path, sha=SHA1)
        transition("st-1", TASK, "blocked", caller_role="watchdog", store=store)
        _patch_review_env(monkeypatch, store, pr_sha=SHA1)

        rc = handoff.main(["review", "st-1", "--pr", "42", "--approve"])
        assert rc != 0
        err = capsys.readouterr().err
        assert "Illegal transition" in err

    def test_needs_rebase_state_illegal_non_zero(self, tmp_path, monkeypatch, capsys):
        """review --approve when state is needs_rebase → non-zero Illegal."""
        store = _make_review_store(tmp_path, sha=SHA1)
        transition("st-1", TASK, "needs_rebase", caller_role="mergemaster", store=store)
        _patch_review_env(monkeypatch, store, pr_sha=SHA1)

        rc = handoff.main(["review", "st-1", "--pr", "42", "--approve"])
        assert rc != 0


def _make_acceptance_store(tmp_path, sha=SHA1):
    s = StateStore(tmp_path / "state" / "orchestration.db")
    s.create_task(task_id=TASK, description="demo", room_id=TASK)
    _drive_to_acceptance_passed(s, sha=sha)
    return s


def _patch_verify_acceptance_env(monkeypatch, store, pr_sha=SHA1):
    monkeypatch.setattr(handoff, "_resolve_store", lambda p: store)
    monkeypatch.setattr(
        handoff, "_resolve_task_id",
        lambda p, s, t: (TASK, None),
    )
    monkeypatch.setattr(handoff, "_pr_head_sha", lambda p, n: pr_sha)
    # Stub chain + claim checks so they always pass
    from unittest.mock import MagicMock
    chain_ok = MagicMock()
    chain_ok.ok = True
    monkeypatch.setattr(handoff, "_transition_chain_intact", lambda s: chain_ok)


class TestVerifyAcceptanceCommandNoop:
    """cb-phase verify-acceptance --accept exits 0 with NO-OP when already passed."""

    def test_exact_dup_exits_zero(self, tmp_path, monkeypatch, capsys):
        store = _make_acceptance_store(tmp_path, sha=SHA1)
        _patch_verify_acceptance_env(monkeypatch, store, pr_sha=SHA1)

        rc = handoff.main(["verify-acceptance", "st-1", "--pr", "42", "--accept"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "NO-OP" in out
        assert "already_acceptance_passed" in out

    def test_exact_dup_no_state_change(self, tmp_path, monkeypatch):
        store = _make_acceptance_store(tmp_path, sha=SHA1)
        _patch_verify_acceptance_env(monkeypatch, store, pr_sha=SHA1)

        handoff.main(["verify-acceptance", "st-1", "--pr", "42", "--accept"])
        assert store.get_subtask("st-1", TASK).state == "acceptance_passed"

    def test_moved_head_illegal_non_zero(self, tmp_path, monkeypatch, capsys):
        """verify-acceptance --accept when head SHA has moved → non-zero Illegal."""
        store = _make_acceptance_store(tmp_path, sha=SHA1)
        _patch_verify_acceptance_env(monkeypatch, store, pr_sha=SHA2)

        rc = handoff.main(["verify-acceptance", "st-1", "--pr", "42", "--accept"])
        assert rc != 0
        err = capsys.readouterr().err
        assert "Illegal transition" in err


# ── cb approve idempotency ────────────────────────────────────────────────────

class TestApproveIdempotency:
    """cb approve run twice at same head → second is exit 0, NO-OP [already_granted]."""

    def _setup_approval_store(self, tmp_path):
        """Store with st-1 at merge_pending with a pending approval request."""
        store = StateStore(tmp_path / "state" / "orchestration.db")
        store.create_task(task_id=TASK, description="demo", room_id=TASK)
        _drive_to_review_passed(store, sha=SHA1)
        transition("st-1", TASK, "merge_pending", caller_role="mergemaster",
                   store=store, head_sha=SHA1)
        store.set_pr_number("st-1", TASK, 42)
        store.mark_merge_approval_requested("st-1", TASK, requested_sha=SHA1)
        return store

    def test_second_approve_same_head_is_noop(self, tmp_path, monkeypatch, capsys):
        from types import SimpleNamespace

        store = self._setup_approval_store(tmp_path)
        # Record the first approval so merge_approved_sha == SHA1
        store.record_merge_approval("st-1", TASK, approved_by="owner", approved_sha=SHA1)

        monkeypatch.setattr(merge, "_resolve_store", lambda p: store)
        monkeypatch.setattr(
            merge, "_resolve_task_id",
            lambda p, s, t: (TASK, None),
        )
        monkeypatch.setattr(
            merge, "load_config",
            lambda p: SimpleNamespace(
                repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
                agents=SimpleNamespace(max_rebase_rounds=3),
            ),
        )
        monkeypatch.setattr(merge, "_pr_snapshot", lambda pr_number, cwd, repo=None: {
            "state": "OPEN", "headRefOid": SHA1,
        })

        result = merge.record_approval_grant(tmp_path, 42)
        # NO-OP: nothing should be recorded (empty list), message printed to stdout
        assert result == []
        out = capsys.readouterr().out
        assert "NO-OP" in out
        assert "already_granted" in out
        assert SHA1 in out

    def test_first_approve_is_recorded(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        store = self._setup_approval_store(tmp_path)
        # No previous approval recorded
        assert store.get_subtask("st-1", TASK).merge_approved_sha is None

        monkeypatch.setattr(merge, "_resolve_store", lambda p: store)
        monkeypatch.setattr(
            merge, "_resolve_task_id",
            lambda p, s, t: (TASK, None),
        )
        monkeypatch.setattr(
            merge, "load_config",
            lambda p: SimpleNamespace(
                repo=SimpleNamespace(url="https://github.com/acme/widgets.git"),
                agents=SimpleNamespace(max_rebase_rounds=3),
            ),
        )
        monkeypatch.setattr(merge, "_pr_snapshot", lambda pr_number, cwd, repo=None: {
            "state": "OPEN", "headRefOid": SHA1,
        })

        result = merge.record_approval_grant(tmp_path, 42)
        assert len(result) == 1
        assert SHA1 in result[0]
        assert store.get_subtask("st-1", TASK).merge_approved_sha == SHA1
