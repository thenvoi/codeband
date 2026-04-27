"""Tests for `codeband.workers.pool.WorkerPool`."""

from __future__ import annotations

import pytest

from codeband.config import Framework
from codeband.workers import (
    WorkerId,
    WorkerPool,
    WorkerRole,
    opposite_framework,
)


# ─── helpers ────────────────────────────────────────────────────────────────

def _pool_two_each() -> WorkerPool:
    """Pool with 2 Claude + 2 Codex coders, 1 of each reviewer."""
    p = WorkerPool()
    p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 2)
    p.register(WorkerRole.CODER, Framework.CODEX, 2)
    p.register(WorkerRole.REVIEWER, Framework.CLAUDE_SDK, 1)
    p.register(WorkerRole.REVIEWER, Framework.CODEX, 1)
    return p


# ─── opposite_framework ─────────────────────────────────────────────────────

class TestOppositeFramework:
    def test_claude_to_codex(self):
        assert opposite_framework(Framework.CLAUDE_SDK) == Framework.CODEX

    def test_codex_to_claude(self):
        assert opposite_framework(Framework.CODEX) == Framework.CLAUDE_SDK


# ─── WorkerId stringification ───────────────────────────────────────────────

class TestWorkerId:
    def test_str_format(self):
        wid = WorkerId(WorkerRole.CODER, Framework.CLAUDE_SDK, 0)
        assert str(wid) == "coder-claude_sdk-0"

    def test_reviewer_codex(self):
        wid = WorkerId(WorkerRole.REVIEWER, Framework.CODEX, 3)
        assert str(wid) == "reviewer-codex-3"


# ─── registration ───────────────────────────────────────────────────────────

class TestRegister:
    def test_register_creates_idle_slots(self):
        p = WorkerPool()
        ids = p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 3)
        assert len(ids) == 3
        assert [i.index for i in ids] == [0, 1, 2]
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 3
        assert p.total_count() == 3

    def test_register_count_zero_is_valid_noop(self):
        p = WorkerPool()
        ids = p.register(WorkerRole.CODER, Framework.CODEX, 0)
        assert ids == []
        assert p.total_count() == 0

    def test_register_extending_adds_new_slots(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)
        ids = p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 3)
        assert len(ids) == 3  # includes the original
        assert p.total_count() == 3

    def test_register_shrinking_is_rejected(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 3)
        with pytest.raises(ValueError, match="Cannot shrink"):
            p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)

    def test_register_negative_count_rejected(self):
        p = WorkerPool()
        with pytest.raises(ValueError, match=">= 0"):
            p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, -1)


# ─── acquire / release ──────────────────────────────────────────────────────

class TestAcquireRelease:
    def test_acquire_specific_framework(self):
        p = _pool_two_each()
        wid = p.acquire(WorkerRole.CODER, Framework.CLAUDE_SDK, task_id="t1")
        assert wid is not None
        assert wid.framework == Framework.CLAUDE_SDK
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 1

    def test_acquire_exhausted_returns_none(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CODEX, 1)
        first = p.acquire(WorkerRole.CODER, Framework.CODEX)
        second = p.acquire(WorkerRole.CODER, Framework.CODEX)
        assert first is not None
        assert second is None

    def test_acquire_any_framework(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CODEX, 1)
        wid = p.acquire(WorkerRole.CODER, framework=None)
        assert wid is not None
        assert wid.framework == Framework.CODEX

    def test_release_frees_slot(self):
        p = _pool_two_each()
        wid = p.acquire(WorkerRole.CODER, Framework.CLAUDE_SDK)
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 1
        p.release(wid)
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 2

    def test_release_unknown_worker_is_noop(self):
        p = _pool_two_each()
        ghost = WorkerId(WorkerRole.CODER, Framework.CLAUDE_SDK, 99)
        p.release(ghost)  # must not raise
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 2

    def test_release_idle_worker_is_noop(self):
        p = _pool_two_each()
        wid = p.acquire(WorkerRole.CODER, Framework.CLAUDE_SDK)
        p.release(wid)
        p.release(wid)  # double-release must not corrupt state
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 2


# ─── pair_for_task: cross-model pairing ─────────────────────────────────────

class TestPairForTask:
    def test_cross_model_pair(self):
        p = _pool_two_each()
        result = p.pair_for_task(
            WorkerRole.CODER, Framework.CLAUDE_SDK, task_id="t1",
        )
        assert result is not None
        coder, reviewer = result
        assert coder.framework == Framework.CLAUDE_SDK
        assert reviewer.framework == Framework.CODEX
        assert coder.role == WorkerRole.CODER
        assert reviewer.role == WorkerRole.REVIEWER

    def test_pair_flips_on_codex_coder(self):
        p = _pool_two_each()
        result = p.pair_for_task(WorkerRole.CODER, Framework.CODEX)
        assert result is not None
        coder, reviewer = result
        assert coder.framework == Framework.CODEX
        assert reviewer.framework == Framework.CLAUDE_SDK

    def test_fallback_same_framework_when_opposite_exhausted(self):
        """When no opposite-framework reviewer is idle, same-model is allowed."""
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)
        p.register(WorkerRole.REVIEWER, Framework.CLAUDE_SDK, 1)
        # No Codex reviewer registered — pair must degrade.
        result = p.pair_for_task(WorkerRole.CODER, Framework.CLAUDE_SDK)
        assert result is not None
        coder, reviewer = result
        assert reviewer.framework == Framework.CLAUDE_SDK  # same-model fallback

    def test_no_coder_available_returns_none(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)
        p.register(WorkerRole.REVIEWER, Framework.CODEX, 1)
        p.acquire(WorkerRole.CODER, Framework.CLAUDE_SDK)  # use it up
        assert p.pair_for_task(WorkerRole.CODER, Framework.CLAUDE_SDK) is None

    def test_no_reviewer_available_returns_none_without_reserving_coder(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)
        # No reviewers at all.
        assert p.pair_for_task(WorkerRole.CODER, Framework.CLAUDE_SDK) is None
        # Coder must still be idle — pairing is atomic.
        assert p.idle_count(WorkerRole.CODER, Framework.CLAUDE_SDK) == 1

    def test_concurrent_pair_attempts_do_not_double_assign(self):
        """Two pair_for_task calls for the same slot must not both succeed."""
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)
        p.register(WorkerRole.REVIEWER, Framework.CODEX, 1)

        first = p.pair_for_task(WorkerRole.CODER, Framework.CLAUDE_SDK, task_id="t1")
        second = p.pair_for_task(WorkerRole.CODER, Framework.CLAUDE_SDK, task_id="t2")
        assert first is not None
        assert second is None


# ─── introspection ──────────────────────────────────────────────────────────

class TestIntrospection:
    def test_active_frameworks_reflects_registration(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 2)
        assert p.active_frameworks(WorkerRole.CODER) == [Framework.CLAUDE_SDK]
        p.register(WorkerRole.CODER, Framework.CODEX, 1)
        assert p.active_frameworks(WorkerRole.CODER) == [
            Framework.CLAUDE_SDK, Framework.CODEX,
        ]

    def test_snapshot_lists_all_slots_with_status(self):
        p = WorkerPool()
        p.register(WorkerRole.CODER, Framework.CLAUDE_SDK, 1)
        p.register(WorkerRole.REVIEWER, Framework.CODEX, 1)
        p.acquire(WorkerRole.CODER, Framework.CLAUDE_SDK, task_id="t1")

        snap = p.snapshot()
        assert len(snap) == 2
        coder = next(s for s in snap if s["role"] == "coder")
        reviewer = next(s for s in snap if s["role"] == "reviewer")
        assert coder["busy"] is True
        assert coder["current_task"] == "t1"
        assert reviewer["busy"] is False
        assert reviewer["current_task"] is None


# ─── config primitives: PoolEntry + FrameworkPool ───────────────────────────

class TestConfigPoolPrimitives:
    def test_pool_entry_defaults(self):
        from codeband.config import PoolEntry

        e = PoolEntry()
        assert e.count == 0
        assert e.model is None
        assert e.description is None
        assert e.max_restarts == 5

    def test_framework_pool_active_frameworks(self):
        from codeband.config import FrameworkPool, PoolEntry

        fp = FrameworkPool(
            claude_sdk=PoolEntry(count=2),
            codex=PoolEntry(count=0),
        )
        assert fp.active_frameworks() == [Framework.CLAUDE_SDK]
        assert fp.total_count() == 2

    def test_framework_pool_entry_for(self):
        from codeband.config import FrameworkPool, PoolEntry

        fp = FrameworkPool(
            claude_sdk=PoolEntry(count=1, model="claude-x"),
            codex=PoolEntry(count=2, model="gpt-5"),
        )
        assert fp.entry_for(Framework.CLAUDE_SDK).model == "claude-x"
        assert fp.entry_for(Framework.CODEX).model == "gpt-5"

    def test_framework_pool_yaml_roundtrip(self):
        import yaml

        from codeband.config import FrameworkPool, PoolEntry

        fp = FrameworkPool(
            claude_sdk=PoolEntry(count=2, description="refactoring"),
            codex=PoolEntry(count=1),
        )
        dumped = yaml.safe_dump(fp.model_dump(mode="json"))
        loaded = FrameworkPool.model_validate(yaml.safe_load(dumped))
        assert loaded.claude_sdk.count == 2
        assert loaded.claude_sdk.description == "refactoring"
        assert loaded.codex.count == 1
