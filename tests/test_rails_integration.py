"""Integration gate for the deterministic rails on REAL git + REAL sqlite.

This is the **(A) hard gate** that must be green *before* P5 activation. The
per-module unit suites (``test_state_store``, ``test_fsm``, ``test_handoff``,
``test_watchdog_upgrade``, ``test_rehydration``) each test one rail in
isolation and mock the boundaries — notably ``subprocess.run`` for every git /
gh shell-out. That mock-theater is the same class of blind spot that hid the
``click`` stderr bug: a green unit suite over mocked subprocess proves the code
*calls* git, not that it *reads real git correctly*.

This module composes the rails together and drives them against a **real temp
git repository** and a **real temp SQLite database** — no subprocess mock on
the watchdog's git-HEAD leg, which is the signal the whole RFC turns on. It
drives ``cb-phase`` directly at the script level (not via LLM agents): this
validates the *machinery* (the (A) gate). Agent *behavior* — coders actually
calling ``cb-phase`` and the Conductor routing through the FSM — is P5 and out
of scope here.

Coverage map:

* ``TestHappyPath`` — one subtask through every state, asserting store rows and
  ``transition_log`` at each step.
* ``TestRejectionEdgesFsm`` — illegal edge + wrong caller-role, each asserting
  nothing is written.
* ``TestCbPhaseGate`` — the three ``cb-phase verify`` gate rejections (dirty
  tree, no open PR, verify command non-zero) plus the happy advance, on real
  git with a real verify subprocess; only the ``gh`` PR-state call is isolated
  behind ``handoff._pr_is_open`` (see the ``gh`` seam note on each test).
* ``TestKillAndRehydrate`` — non-terminal subtasks in the store; each role's
  recovery context.
* ``TestFanoutInvariants`` — N concurrent FSM instances: no double-merge, no
  merge before approval, and the global cycle cap across the live set.
* ``TestWatchdogRealGit`` — the mechanical progress signal reading *actual*
  ``git rev-parse`` output, HEAD-advanced vs not, with NO mocked subprocess.

A note on "round caps" (RFC §two-level model, fan-out invariants): the FSM
(``state/fsm.py``) has **no per-subtask review-round counter** — the
``review_failed → in_progress → … → review_pending → review_failed`` loop is
not bounded in code. The only cap the rails actually implement is the
watchdog's ``max_phase_visits`` *mechanical stall cap* (RFC line 178 equates the
"cycle/stall cap" with ``max_phase_visits``). So "round caps enforced globally"
is exercised here as that stall cap applied across the full live set of
concurrent subtasks (``TestFanoutInvariants.test_global_cycle_cap_across_set``
+ ``TestWatchdogRealGit``), not as an FSM review-round counter. See the report
for this distinction.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.agents.watchdog import WatchdogDaemon
from codeband.cli import handoff
from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    RepoConfig,
    WatchdogConfig,
    WorkspaceConfig,
)
from codeband.state import StateStore
from codeband.state.fsm import InvalidTransitionError, transition
from codeband.state.rehydration import build_agent_recovery_context


# ── real-git helpers ─────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    """Run a real git command in ``repo``; return stdout (raises on failure)."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    """Initialise a real git repo at ``path`` with one commit on ``main``."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True,
                   capture_output=True, text=True)
    _git(path, "config", "user.email", "rails-test@example.com")
    _git(path, "config", "user.name", "Rails Test")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial commit")
    return path


def _commit_on(repo: Path, branch: str, content: str) -> str:
    """Check out ``branch`` (creating it if needed), commit a file, return HEAD."""
    existing = _git(repo, "branch", "--list", branch)
    if existing:
        _git(repo, "checkout", branch)
    else:
        _git(repo, "checkout", "-b", branch)
    fname = f"{branch}.txt"
    (repo / fname).write_text(content, encoding="utf-8")
    _git(repo, "add", fname)
    _git(repo, "commit", "-m", f"work on {branch}: {content}")
    return _git(repo, "rev-parse", branch)


def _branch_head(repo: Path, branch: str) -> str:
    return _git(repo, "rev-parse", branch)


# ── sqlite / store helpers ───────────────────────────────────────────────────


def _new_store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state" / "orchestration.db")


def _log_rows(store: StateStore, subtask_id: str) -> list[sqlite3.Row]:
    """Read the real ``transition_log`` rows for a subtask, oldest first."""
    conn = sqlite3.connect(store.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT from_state, to_state, caller_role, reason "
            "FROM transition_log WHERE subtask_id = ? ORDER BY id",
            (subtask_id,),
        ).fetchall()
    finally:
        conn.close()


def _log_count(store: StateStore, subtask_id: str) -> int:
    return len(_log_rows(store, subtask_id))


def _fake_rest() -> MagicMock:
    """A REST stub whose only used method is the async chat-message writer."""
    rest = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    return rest


# ─────────────────────────────────────────────────────────────────────────────
# 1. Happy path — one subtask through every state (real sqlite, real FSM)
# ─────────────────────────────────────────────────────────────────────────────


class TestHappyPath:
    """planned → assigned → in_progress → verify_pending → review_pending →
    review_passed → merge_pending → merged, asserting store + log each step."""

    def test_full_lifecycle_records_every_transition(self, tmp_path):
        store = _new_store(tmp_path)
        store.create_task("room-1", "ship the feature", "room-1")

        steps = [
            ("assigned", "conductor"),
            ("in_progress", "coder"),
            ("verify_pending", "coder"),
            ("review_pending", "coder"),
            ("review_passed", "reviewer"),
            ("merge_pending", "mergemaster"),
            ("merged", "mergemaster"),
        ]
        prev_state = "planned"
        for i, (new_state, role) in enumerate(steps, start=1):
            transition("st-1", "room-1", new_state, caller_role=role,
                       reason=f"step-{i}", store=store)

            row = store.get_subtask("st-1")
            assert row is not None
            assert row.state == new_state
            assert row.task_id == "room-1"

            log = _log_rows(store, "st-1")
            assert len(log) == i  # exactly one new row per step
            last = log[-1]
            assert last["from_state"] == prev_state
            assert last["to_state"] == new_state
            assert last["caller_role"] == role
            assert last["reason"] == f"step-{i}"
            prev_state = new_state

        # Terminal — the full ordered trail is durable.
        assert store.get_subtask("st-1").state == "merged"
        trail = [(r["from_state"], r["to_state"]) for r in _log_rows(store, "st-1")]
        assert trail == [
            ("planned", "assigned"),
            ("assigned", "in_progress"),
            ("in_progress", "verify_pending"),
            ("verify_pending", "review_pending"),
            ("review_pending", "review_passed"),
            ("review_passed", "merge_pending"),
            ("merge_pending", "merged"),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# 2a. Rejection edges in the FSM — illegal edge + wrong caller-role
# ─────────────────────────────────────────────────────────────────────────────


class TestRejectionEdgesFsm:
    """Each rejection asserts the gate raises AND nothing is written."""

    def _seed(self, tmp_path, *, state: str, role_chain):
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        for new_state, role in role_chain:
            transition("st-1", "room-1", new_state, caller_role=role, store=store)
        assert store.get_subtask("st-1").state == state
        return store

    def test_illegal_edge_not_in_table_rejected(self, tmp_path):
        # planned --conductor--> merged is not a legal edge.
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        before = _log_count(store, "st-1")

        with pytest.raises(InvalidTransitionError):
            transition("st-1", "room-1", "merged", caller_role="conductor",
                       store=store)

        # ensure_subtask creates the row at 'planned', but no state change and
        # no transition_log row may be written.
        assert store.get_subtask("st-1").state == "planned"
        assert _log_count(store, "st-1") == before == 0

    def test_wrong_caller_role_rejected(self, tmp_path):
        # assigned --coder--> in_progress is legal; the SAME edge driven by a
        # 'reviewer' must be rejected.
        store = self._seed(
            tmp_path, state="assigned", role_chain=[("assigned", "conductor")],
        )
        before = _log_count(store, "st-1")

        with pytest.raises(InvalidTransitionError):
            transition("st-1", "room-1", "in_progress", caller_role="reviewer",
                       store=store)

        assert store.get_subtask("st-1").state == "assigned"
        assert _log_count(store, "st-1") == before  # nothing appended


# ─────────────────────────────────────────────────────────────────────────────
# 2b. Rejection edges at the cb-phase gate — dirty tree, no PR, verify != 0
#     Real git + real verify subprocess. Only the gh PR-state call is isolated.
# ─────────────────────────────────────────────────────────────────────────────


class TestCbPhaseGate:
    """``cb-phase verify`` gate, composed against a real git worktree.

    The clean-tree gate and (when configured) the verify command run as real
    subprocesses. The PR-state gate calls ``gh pr view`` which cannot run
    hermetically in CI, so it is isolated behind ``handoff._pr_is_open`` — the
    single documented ``gh`` seam. Everything else (git status, the verify
    command, the FSM transition, the SQLite write) is real.
    """

    def _project(self, tmp_path, *, verify_command=None):
        """Build a project dir with codeband.yaml + a store seeded at
        ``verify_pending`` for subtask ``st-1``. project_dir is kept separate
        from the git worktree so writing codeband.yaml never dirties the tree.

        Returns ``(project_dir, store)``; the store path matches what
        ``handoff._resolve_store`` resolves from the config.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        workspace = tmp_path / "workspace"
        cfg = CodebandConfig(
            repo=RepoConfig(url="https://github.com/example/repo.git",
                            branch="main"),
            agents=AgentsConfig(handoff_verify_command=verify_command),
            workspace=WorkspaceConfig(path=str(workspace)),
        )
        cfg.to_yaml(project_dir / "codeband.yaml")

        store = StateStore(workspace / "state" / "orchestration.db")
        store.create_task("room-1", "demo", "room-1")
        transition("st-1", "room-1", "assigned", caller_role="conductor", store=store)
        transition("st-1", "room-1", "in_progress", caller_role="coder", store=store)
        transition("st-1", "room-1", "verify_pending", caller_role="coder", store=store)
        return project_dir, store

    def _run(self, project_dir: Path, worktree: Path) -> int:
        return handoff.main([
            "verify", "st-1",
            "--task", "room-1",
            "--pr", "42",
            "--worktree", str(worktree),
            "--project-dir", str(project_dir),
        ])

    def test_dirty_tree_rejected(self, tmp_path):
        # Fully real: the clean-tree gate fires first, so gh is never reached
        # and needs no seam. The worktree is made dirty with an untracked file.
        project_dir, store = self._project(tmp_path)
        repo = _init_repo(tmp_path / "repo")
        (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) != 0
        assert store.get_subtask("st-1").state == "verify_pending"
        assert _log_count(store, "st-1") == before

    def test_no_open_pr_rejected(self, tmp_path, monkeypatch):
        # gh seam: PR reported not-OPEN. Tree is real and clean.
        project_dir, store = self._project(tmp_path)
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: False)
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) != 0
        assert store.get_subtask("st-1").state == "verify_pending"
        assert _log_count(store, "st-1") == before

    def test_verify_command_nonzero_rejected(self, tmp_path, monkeypatch):
        # gh seam: PR OPEN. The verify command runs as a REAL subprocess and
        # exits non-zero, so the gate must reject and write nothing.
        project_dir, store = self._project(tmp_path, verify_command="exit 7")
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) != 0
        assert store.get_subtask("st-1").state == "verify_pending"
        assert _log_count(store, "st-1") == before

    def test_happy_verify_advances_to_review_pending(self, tmp_path, monkeypatch):
        # gh seam: PR OPEN. Clean real tree + a REAL passing verify command.
        # The subtask advances and a real transition_log row is appended.
        project_dir, store = self._project(tmp_path, verify_command="exit 0")
        repo = _init_repo(tmp_path / "repo")
        monkeypatch.setattr(handoff, "_pr_is_open", lambda pr: True)
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) == 0
        assert store.get_subtask("st-1").state == "review_pending"
        assert _log_count(store, "st-1") == before + 1
        last = _log_rows(store, "st-1")[-1]
        assert (last["from_state"], last["to_state"]) == (
            "verify_pending", "review_pending",
        )
        assert last["caller_role"] == "coder"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Kill-and-rehydrate — per-role recovery context from durable state
# ─────────────────────────────────────────────────────────────────────────────


class TestKillAndRehydrate:
    """With non-terminal subtasks in the store, each role gets the right
    in-flight context; a terminal subtask is never surfaced."""

    def _seed(self, tmp_path):
        store = _new_store(tmp_path)
        store.create_task("room-1", "build the dark-mode toggle", "room-1")
        # A spread of states across the lifecycle, plus one terminal.
        store.ensure_subtask("st-inprog", "room-1", state="in_progress",
                             assigned_worker="coder-claude_sdk-0")
        store.ensure_subtask("st-review", "room-1", state="review_pending",
                             assigned_worker="coder-codex-0")
        store.ensure_subtask("st-passed", "room-1", state="review_passed",
                             assigned_worker="coder-claude_sdk-0")
        store.ensure_subtask("st-merge", "room-1", state="merge_pending",
                             assigned_worker="coder-codex-0")
        store.ensure_subtask("st-merged", "room-1", state="merged",
                             assigned_worker="coder-claude_sdk-0")
        return store

    async def test_conductor_sees_all_inflight(self, tmp_path):
        store = self._seed(tmp_path)
        ctx = await build_agent_recovery_context("conductor", store)
        assert ctx is not None
        # All four non-terminal subtasks appear; the merged one does not.
        for sid in ("st-inprog", "st-review", "st-passed", "st-merge"):
            assert sid in ctx
        assert "st-merged" not in ctx

    async def test_mergemaster_sees_merge_pending_and_review_passed(self, tmp_path):
        store = self._seed(tmp_path)
        ctx = await build_agent_recovery_context("mergemaster", store)
        assert ctx is not None
        assert "st-passed" in ctx
        assert "st-merge" in ctx
        # Not awaiting integration:
        assert "st-inprog" not in ctx
        assert "st-review" not in ctx
        assert "st-merged" not in ctx

    async def test_reviewer_sees_only_review_pending(self, tmp_path):
        store = self._seed(tmp_path)
        ctx = await build_agent_recovery_context("reviewer-codex-0", store)
        assert ctx is not None
        assert "st-review" in ctx
        for other in ("st-inprog", "st-passed", "st-merge", "st-merged"):
            assert other not in ctx

    async def test_planner_sees_active_task(self, tmp_path):
        store = self._seed(tmp_path)
        ctx = await build_agent_recovery_context("planner-claude_sdk-0", store)
        assert ctx is not None
        assert "room-1" in ctx
        assert "build the dark-mode toggle" in ctx

    async def test_empty_store_yields_no_context(self, tmp_path):
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        # No subtask rows → nothing relevant in durable state for any role.
        assert await build_agent_recovery_context("conductor", store) is None
        assert await build_agent_recovery_context("planner-claude_sdk-0", store) is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fan-out invariants — N concurrent FSM instances; assert across the set
# ─────────────────────────────────────────────────────────────────────────────


class TestFanoutInvariants:
    """The genuinely new risk surface vs. band-of-devs' single track."""

    def _drive_to_merged(self, store, sid):
        for new_state, role in [
            ("assigned", "conductor"),
            ("in_progress", "coder"),
            ("verify_pending", "coder"),
            ("review_pending", "coder"),
            ("review_passed", "reviewer"),
            ("merge_pending", "mergemaster"),
            ("merged", "mergemaster"),
        ]:
            transition(sid, "room-1", new_state, caller_role=role, store=store)

    def test_no_double_merge_across_set(self, tmp_path):
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        sids = [f"st-{i}" for i in range(4)]
        for sid in sids:
            self._drive_to_merged(store, sid)

        # Every subtask is merged; a SECOND merge of any of them is rejected
        # (terminal state) and appends no extra 'merged' row.
        for sid in sids:
            assert store.get_subtask(sid).state == "merged"
            merged_rows_before = sum(
                1 for r in _log_rows(store, sid) if r["to_state"] == "merged"
            )
            with pytest.raises(InvalidTransitionError):
                transition(sid, "room-1", "merged", caller_role="mergemaster",
                           store=store)
            merged_rows_after = sum(
                1 for r in _log_rows(store, sid) if r["to_state"] == "merged"
            )
            assert merged_rows_before == merged_rows_after == 1

    def test_no_merge_before_approval_across_set(self, tmp_path):
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        # N subtasks parked at review_pending — reviewed, not yet PASSED.
        sids = [f"st-{i}" for i in range(4)]
        for sid in sids:
            for new_state, role in [
                ("assigned", "conductor"),
                ("in_progress", "coder"),
                ("verify_pending", "coder"),
                ("review_pending", "coder"),
            ]:
                transition(sid, "room-1", new_state, caller_role=role, store=store)

        for sid in sids:
            # Mergemaster cannot jump an un-approved subtask into merge_pending…
            with pytest.raises(InvalidTransitionError):
                transition(sid, "room-1", "merge_pending",
                           caller_role="mergemaster", store=store)
            # …nor straight to merged.
            with pytest.raises(InvalidTransitionError):
                transition(sid, "room-1", "merged",
                           caller_role="mergemaster", store=store)
            assert store.get_subtask(sid).state == "review_pending"
            assert not any(
                r["to_state"] in ("merge_pending", "merged")
                for r in _log_rows(store, sid)
            )

    async def test_global_cycle_cap_across_set(self, tmp_path, monkeypatch):
        """The watchdog stall cap (``max_phase_visits``) applied across the
        full live set, driven by REAL git HEAD movement.

        Three concurrent in-flight subtasks share one repo. One makes real
        progress every round; two stall. After ``max_phase_visits`` patrols the
        two stalled subtasks — and only those — are marked ``blocked`` via the
        real FSM. This is the global enforcement of the cycle cap: one counter
        per subtask, evaluated across every active subtask each patrol.
        """
        repo = _init_repo(tmp_path / "repo")
        for b in ("feat-0", "feat-1", "feat-2"):
            _commit_on(repo, b, "init")
        _git(repo, "checkout", "main")
        monkeypatch.chdir(repo)

        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        # Seed directly at in_progress (no transition_log rows) so git HEAD is
        # the ONLY progress signal in play. pr_number stays None → gh is never
        # called.
        for i in range(3):
            store.ensure_subtask(f"st-{i}", "room-1", state="in_progress",
                                 metadata={"branch": f"feat-{i}"})

        rest = _fake_rest()
        daemon = WatchdogDaemon(
            config=WatchdogConfig(max_phase_visits=2, git_progress_check=True),
            rest_client=rest,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            state_store=store,
        )
        now = datetime.now(timezone.utc)

        await daemon._check_subtask_progress(now)          # patrol 1: baseline
        _commit_on(repo, "feat-1", "round-2")              # only feat-1 advances
        _git(repo, "checkout", "main")
        await daemon._check_subtask_progress(now)          # patrol 2: 0,2 stall→1
        _commit_on(repo, "feat-1", "round-3")
        _git(repo, "checkout", "main")
        await daemon._check_subtask_progress(now)          # patrol 3: 0,2 stall→2→blocked

        assert store.get_subtask("st-0").state == "blocked"
        assert store.get_subtask("st-2").state == "blocked"
        assert store.get_subtask("st-1").state == "in_progress"
        # Exactly one blocked-alert per stalled subtask (global, not per-run).
        assert rest.agent_api_messages.create_agent_chat_message.await_count == 2
        # The blocks were applied by the real FSM with the watchdog role.
        for sid in ("st-0", "st-2"):
            assert any(
                r["to_state"] == "blocked" and r["caller_role"] == "watchdog"
                for r in _log_rows(store, sid)
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Watchdog on REAL git — mechanical progress signal reads actual git HEAD
# ─────────────────────────────────────────────────────────────────────────────


class TestWatchdogRealGit:
    """No mocked subprocess on the git-HEAD leg. The watchdog runs a real
    ``git rev-parse`` in the process cwd; the test drives real commits."""

    def _daemon(self, store, *, max_phase_visits, rest=None):
        return WatchdogDaemon(
            config=WatchdogConfig(
                max_phase_visits=max_phase_visits, git_progress_check=True,
            ),
            rest_client=rest if rest is not None else _fake_rest(),
            agent_id="agent-wd",
            conductor_id="agent-cond",
            state_store=store,
        )

    async def test_real_head_advance_resets_stall_counter(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "repo")
        sha1 = _commit_on(repo, "feat-a", "v1")
        _git(repo, "checkout", "main")
        monkeypatch.chdir(repo)

        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        store.ensure_subtask("st-a", "room-1", state="in_progress",
                             metadata={"branch": "feat-a"})  # pr_number None → no gh

        daemon = self._daemon(store, max_phase_visits=10)
        now = datetime.now(timezone.utc)

        await daemon._check_subtask_progress(now)            # baseline
        health = daemon._subtask_state["st-a"]
        assert health.last_git_head == sha1                  # read REAL git HEAD
        assert health.patrol_visits_without_progress == 0

        await daemon._check_subtask_progress(now)            # no commit → stall
        await daemon._check_subtask_progress(now)            # stall again
        assert health.patrol_visits_without_progress == 2

        sha2 = _commit_on(repo, "feat-a", "v2")              # REAL HEAD movement
        _git(repo, "checkout", "main")
        assert sha2 != sha1
        await daemon._check_subtask_progress(now)
        assert health.last_git_head == sha2                  # observed the new SHA
        assert health.patrol_visits_without_progress == 0    # progress reset it

    async def test_real_git_stall_marks_blocked(self, tmp_path, monkeypatch):
        repo = _init_repo(tmp_path / "repo")
        _commit_on(repo, "feat-b", "v1")
        _git(repo, "checkout", "main")
        monkeypatch.chdir(repo)

        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        store.ensure_subtask("st-b", "room-1", state="in_progress",
                             metadata={"branch": "feat-b"})

        rest = _fake_rest()
        daemon = self._daemon(store, max_phase_visits=2, rest=rest)
        now = datetime.now(timezone.utc)

        await daemon._check_subtask_progress(now)   # baseline (counts as progress)
        await daemon._check_subtask_progress(now)   # stall → 1
        assert store.get_subtask("st-b").state == "in_progress"
        await daemon._check_subtask_progress(now)   # stall → 2 == cap → blocked

        assert store.get_subtask("st-b").state == "blocked"
        rest.agent_api_messages.create_agent_chat_message.assert_awaited_once()
        msg = rest.agent_api_messages.create_agent_chat_message.call_args.kwargs[
            "message"
        ]
        assert "st-b" in msg.content
        assert "could not be applied" not in msg.content  # FSM applied it
        assert any(
            r["to_state"] == "blocked" and r["caller_role"] == "watchdog"
            for r in _log_rows(store, "st-b")
        )
