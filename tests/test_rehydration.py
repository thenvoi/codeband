"""Tests for universal agent rehydration (RFC Workstream 5)."""

from __future__ import annotations

import asyncio

import pytest

from codeband.state.rehydration import (
    build_agent_recovery_context,
    recover_for_reconnect,
)
from codeband.state.store import StateStore


def _seed(tmp_path):
    """Seed a StateStore with one task and a spread of subtask states."""
    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task("task-1", "Build the widget pipeline", "room-1")
    store.ensure_subtask("st-plan", "task-1", state="planned", assigned_worker="coder-claude_sdk-0")
    store.ensure_subtask("st-rev", "task-1", state="review_pending", assigned_worker="coder-codex-0")
    store.ensure_subtask("st-pass", "task-1", state="review_passed", assigned_worker="coder-codex-1")
    store.ensure_subtask("st-accept", "task-1", state="acceptance_passed", assigned_worker="coder-claude_sdk-0")
    store.ensure_subtask("st-merge", "task-1", state="merge_pending", assigned_worker="coder-claude_sdk-1")
    store.ensure_subtask("st-rebase", "task-1", state="needs_rebase", assigned_worker="coder-codex-0")
    # Terminal subtasks — must never appear in any recovery context.
    store.ensure_subtask("st-done", "task-1", state="merged")
    store.ensure_subtask("st-drop", "task-1", state="abandoned")
    return store


def test_conductor_lists_all_non_terminal_as_table(tmp_path):
    store = _seed(tmp_path)
    ctx = asyncio.run(build_agent_recovery_context("conductor", store))
    assert ctx is not None
    assert "| Subtask | State | Worker | PR |" in ctx
    # All six non-terminal subtasks present...
    for sid in ("st-plan", "st-rev", "st-pass", "st-accept", "st-merge", "st-rebase"):
        assert sid in ctx
    # ...and the two terminal ones excluded.
    assert "st-done" not in ctx
    assert "st-drop" not in ctx


def test_mergemaster_shows_merge_states_only(tmp_path):
    store = _seed(tmp_path)
    ctx = asyncio.run(build_agent_recovery_context("mergemaster", store))
    assert ctx is not None
    assert "st-pass" in ctx  # review_passed
    assert "st-accept" in ctx  # acceptance_passed — ready to queue
    assert "st-merge" in ctx  # merge_pending
    assert "st-rebase" in ctx  # needs_rebase — the merge gate's send-back
    assert "st-rev" not in ctx  # review_pending — not Mergemaster's concern
    assert "st-plan" not in ctx


def test_reviewer_shows_only_review_pending(tmp_path):
    store = _seed(tmp_path)
    ctx = asyncio.run(build_agent_recovery_context("reviewer-codex-0", store))
    assert ctx is not None
    assert "st-rev" in ctx
    assert "st-pass" not in ctx
    assert "st-merge" not in ctx
    assert "st-plan" not in ctx


def test_planner_shows_active_task_description(tmp_path):
    store = _seed(tmp_path)
    ctx = asyncio.run(build_agent_recovery_context("planner-claude_sdk-0", store))
    assert ctx is not None
    assert "Build the widget pipeline" in ctx
    assert "task-1" in ctx


def test_plan_reviewer_shows_task_and_subtask_count(tmp_path):
    store = _seed(tmp_path)
    ctx = asyncio.run(build_agent_recovery_context("plan_reviewer-codex-0", store))
    assert ctx is not None
    assert "Build the widget pipeline" in ctx
    # Six non-terminal subtasks reference task-1.
    assert "subtasks in flight: 6" in ctx


def test_verifier_shows_only_review_passed(tmp_path):
    store = _seed(tmp_path)
    ctx = asyncio.run(build_agent_recovery_context("verifier-codex-0", store))
    assert ctx is not None
    assert "st-pass" in ctx  # review_passed — awaiting the acceptance verdict
    assert "st-rev" not in ctx  # review_pending — the reviewer's concern
    assert "st-accept" not in ctx  # already accepted
    assert "st-merge" not in ctx


def test_returns_none_when_nothing_relevant(tmp_path):
    """Empty store → no recovery context for any role."""
    store = StateStore(tmp_path / "state" / "orchestration.db")
    for key in ("conductor", "mergemaster", "reviewer-codex-0",
                "planner-claude_sdk-0", "plan_reviewer-codex-0"):
        assert asyncio.run(build_agent_recovery_context(key, store)) is None


def test_mergemaster_none_when_no_matching_states(tmp_path):
    """A store with only review_pending work yields nothing for Mergemaster."""
    store = StateStore(tmp_path / "state" / "orchestration.db")
    store.create_task("task-1", "desc", "room-1")
    store.ensure_subtask("st-rev", "task-1", state="review_pending")
    assert asyncio.run(build_agent_recovery_context("mergemaster", store)) is None
    # ...but the reviewer does see it.
    assert asyncio.run(build_agent_recovery_context("reviewer-codex-0", store)) is not None


def test_unknown_role_returns_none(tmp_path):
    store = _seed(tmp_path)
    assert asyncio.run(build_agent_recovery_context("watchdog", store)) is None


def test_recover_for_reconnect_opens_store_and_builds(tmp_path):
    _seed(tmp_path)
    ctx = asyncio.run(recover_for_reconnect("conductor", tmp_path))
    assert ctx is not None
    assert "st-plan" in ctx


def test_recover_for_reconnect_swallows_errors(tmp_path, monkeypatch):
    """A rehydration failure returns None rather than raising."""
    import codeband.state.rehydration as rehydration

    async def _boom(agent_key, store):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(rehydration, "build_agent_recovery_context", _boom)
    _seed(tmp_path)
    assert asyncio.run(recover_for_reconnect("conductor", tmp_path)) is None


# ── reconnect-loop wiring (single-process _run_agent_forever) ────────────────

def test_run_agent_forever_threads_recovery_context(tmp_path):
    """_run_agent_forever rebuilds recovery context and passes it to the factory."""
    from codeband.orchestration import runner

    _seed(tmp_path)
    captured: dict = {}

    class _FakeAgent:
        async def run(self):
            # End the otherwise-infinite loop after the first cycle.
            raise asyncio.CancelledError

    def make_agent(recovery_context=None):
        captured["rc"] = recovery_context
        return _FakeAgent()

    class _Activity:
        def log(self, *a, **k):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner._run_agent_forever(
                make_agent, "conductor", _Activity(),
                agent_key="conductor", workspace_path=tmp_path,
            )
        )

    assert captured["rc"] is not None
    assert "st-plan" in captured["rc"]


def test_run_agent_forever_falls_back_to_none_on_rehydration_error(tmp_path, monkeypatch):
    """A rehydration error must not break the reconnect loop; rc falls back to None."""
    from codeband.orchestration import runner
    import codeband.state.rehydration as rehydration

    async def _boom(agent_key, workspace_path):
        raise RuntimeError("kaboom")

    # Patch the symbol _run_agent_forever imports at call time.
    monkeypatch.setattr(rehydration, "recover_for_reconnect", _boom)

    captured: dict = {}

    class _FakeAgent:
        async def run(self):
            raise asyncio.CancelledError

    def make_agent(recovery_context=None):
        captured["rc"] = recovery_context
        return _FakeAgent()

    class _Activity:
        def log(self, *a, **k):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner._run_agent_forever(
                make_agent, "conductor", _Activity(),
                agent_key="conductor", workspace_path=tmp_path,
            )
        )

    assert captured["rc"] is None


def test_run_agent_forever_survives_activity_log_oserror(tmp_path, monkeypatch):
    """The crash handler's own AGENT_CRASH log line raising OSError must not
    kill the reconnect-forever loop it reports on (S6-F9)."""
    from codeband.orchestration import runner

    monkeypatch.setattr(runner, "_RECONNECT_BASE_DELAY_SECONDS", 0)

    cycles = {"n": 0}

    class _FakeAgent:
        async def run(self):
            cycles["n"] += 1
            if cycles["n"] >= 3:
                raise asyncio.CancelledError  # end the otherwise-infinite loop
            raise RuntimeError("agent crashed")

    class _DiskFullActivity:
        calls = 0

        def log(self, *a, **k):
            type(self).calls += 1
            raise OSError(28, "No space left on device")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            runner._run_agent_forever(
                lambda recovery_context=None: _FakeAgent(),
                "conductor", _DiskFullActivity(),
            )
        )

    # Two crashes (cycles 1 and 2) were each logged (and each log raised);
    # cycle 3 raises CancelledError before any log call; no AGENT_RECONNECTED
    # fires because no run() succeeded. The loop lived on regardless.
    assert cycles["n"] == 3
    assert _DiskFullActivity.calls == 2
