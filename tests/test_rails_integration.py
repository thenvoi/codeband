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
  behind ``handoff._verify_pr_snapshot`` (the one-snapshot ``gh`` seam).
* ``TestKillAndRehydrate`` — non-terminal subtasks in the store; each role's
  recovery context.
* ``TestFanoutInvariants`` — N concurrent FSM instances: no double-merge, no
  merge before approval, and the global cycle cap across the live set.
* ``TestWatchdogRealGit`` — the mechanical progress signal reading *actual*
  ``git rev-parse`` output, HEAD-advanced vs not, with NO mocked subprocess.
* ``TestReviewRoundCap`` — the FSM's per-subtask review-round cap: a *productive*
  ``review_failed → in_progress → … → review_failed`` loop (a real commit each
  round, HEAD advancing) is bounded in code, the count is durable across a store
  reopen, the counters are per-subtask, and the cap is a mechanism *distinct*
  from the watchdog stall cap (which never fires on a progressing loop).
* ``TestVerifyAttemptCap`` — the handoff CLI's per-subtask verify-attempt cap:
  a *productive* ``cb-phase verify`` rejection loop (a real commit each attempt,
  HEAD advancing, the verify command failing every time) is bounded in code at
  ``MAX_VERIFY_ATTEMPTS`` → escalated ``verify_pending → blocked``; the count is
  durable across a store reopen, per-subtask, configurable, and *distinct* from
  BOTH the watchdog stall cap (never fires on the progressing loop) and the
  review-round cap (the subtask never reaches ``review_failed`` at all). Mirrors
  ``TestReviewRoundCap`` on the verify leg of the pipeline.

The two caps are disjoint by construction. The watchdog's ``max_phase_visits``
is a *mechanical stall cap* (RFC line 178): it fires on the *absence* of
progress — no git-HEAD change and no new transition across N patrols — so it
cannot bound a loop that commits real code every round. The FSM's
``MAX_REVIEW_ROUNDS`` is a *review-round cap*: it counts how many times a
subtask has bounced back from review and refuses a further rework cycle once the
count is reached, regardless of how much progress each round made.
``TestReviewRoundCap.test_round_cap_distinct_from_watchdog_stall_cap`` drives the
exact loop the watchdog passes and the round cap rejects.
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
from codeband.cli.handoff import MAX_VERIFY_ATTEMPTS
from codeband.state.fsm import MAX_REVIEW_ROUNDS, InvalidTransitionError, transition
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


def _stub_pr_snapshot(monkeypatch, repo: Path, *, state: str = "OPEN") -> None:
    """Stub verify's ONE PR snapshot (gh) to track the repo's real state.

    PR-pinned verify outcomes require worktree HEAD == PR head and the PR's
    head branch == the worktree branch; these tests exercise the real git
    side, so the gh side is made to agree (lazily — evaluated per call, so a
    commit or checkout made mid-test moves the stubbed PR side too).
    """
    monkeypatch.setattr(
        handoff, "_verify_pr_snapshot",
        lambda project_dir, pr: {
            "state": state,
            "headRefName": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "headRefOid": _git(repo, "rev-parse", "HEAD"),
        },
    )


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
    store = StateStore(tmp_path / "state" / "orchestration.db")
    (store.db_path.parent / ".codeband_room").write_text("room-1", encoding="utf-8")
    return store


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

        # The verify and review outcomes pin head_sha (as cb-phase does); the
        # merge_pending step then passes the eligibility gate at the same SHA.
        steps = [
            ("assigned", "conductor", None),
            ("in_progress", "coder", None),
            ("verify_pending", "coder", None),
            ("review_pending", "coder", "sha-1"),
            ("review_passed", "reviewer", "sha-1"),
            ("merge_pending", "mergemaster", "sha-1"),
            ("merged", "mergemaster", None),
        ]
        prev_state = "planned"
        for i, (new_state, role, sha) in enumerate(steps, start=1):
            transition("st-1", "room-1", new_state, caller_role=role,
                       reason=f"step-{i}", store=store, head_sha=sha)

            row = store.get_subtask("st-1", "room-1")
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
        assert store.get_subtask("st-1", "room-1").state == "merged"
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
        assert store.get_subtask("st-1", "room-1").state == state
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
        assert store.get_subtask("st-1", "room-1").state == "planned"
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

        assert store.get_subtask("st-1", "room-1").state == "assigned"
        assert _log_count(store, "st-1") == before  # nothing appended


# ─────────────────────────────────────────────────────────────────────────────
# 2b. Rejection edges at the cb-phase gate — dirty tree, no PR, verify != 0
#     Real git + real verify subprocess. Only the gh PR-state call is isolated.
# ─────────────────────────────────────────────────────────────────────────────


class TestCbPhaseGate:
    """``cb-phase verify`` gate, composed against a real git worktree.

    The clean-tree gate and (when configured) the verify command run as real
    subprocesses. The PR gates call ``gh pr view`` which cannot run
    hermetically in CI, so they are isolated behind
    ``handoff._verify_pr_snapshot`` — the single documented ``gh`` seam. Everything else (git status, the verify
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
        # Active-room pointer: kickoff writes this; cb-phase resolves the
        # authoritative task_id (room UUID) from it, not from --task.
        (project_dir / ".codeband_room").write_text("room-1", encoding="utf-8")

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
        assert store.get_subtask("st-1", "room-1").state == "verify_pending"
        assert _log_count(store, "st-1") == before

    def test_no_open_pr_rejected(self, tmp_path, monkeypatch):
        # gh seam: PR reported not-OPEN. Tree is real and clean.
        project_dir, store = self._project(tmp_path)
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo, state="CLOSED")
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) != 0
        assert store.get_subtask("st-1", "room-1").state == "verify_pending"
        assert _log_count(store, "st-1") == before

    def test_verify_command_nonzero_rejected(self, tmp_path, monkeypatch):
        # gh seam: PR OPEN. The verify command runs as a REAL subprocess and
        # exits non-zero, so the gate must reject and write nothing.
        project_dir, store = self._project(tmp_path, verify_command="exit 7")
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) != 0
        assert store.get_subtask("st-1", "room-1").state == "verify_pending"
        assert _log_count(store, "st-1") == before

    def test_happy_verify_advances_to_review_pending(self, tmp_path, monkeypatch):
        # gh seam: PR OPEN. Clean real tree + a REAL passing verify command.
        # The subtask advances and a real transition_log row is appended.
        project_dir, store = self._project(tmp_path, verify_command="exit 0")
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)
        before = _log_count(store, "st-1")

        assert self._run(project_dir, repo) == 0
        assert store.get_subtask("st-1", "room-1").state == "review_pending"
        assert _log_count(store, "st-1") == before + 1
        last = _log_rows(store, "st-1")[-1]
        assert (last["from_state"], last["to_state"]) == (
            "verify_pending", "review_pending",
        )
        assert last["caller_role"] == "coder"


# ─────────────────────────────────────────────────────────────────────────────
# 2c. Reviewer-verdict command — review_pending → review_passed/failed via FSM
#     Real sqlite, driven through the cb-phase CLI (not an LLM).
# ─────────────────────────────────────────────────────────────────────────────


class TestCbPhaseReviewVerdict:
    """``cb-phase review --approve|--reject`` routes the verdict through the FSM.

    This is the structural bind that makes the verify gate non-bypassable:
    ``review_passed`` is reachable ONLY from ``review_pending``, which is
    reachable ONLY via the verify gate (``verify_pending → review_pending``).
    The verdict edge is legal ONLY from ``review_pending`` — from any other state
    the FSM raises and writes nothing, so there is no path to an approved subtask
    that skips verification. Driven at the script level on a real SQLite DB.
    """

    def _project(self, tmp_path):
        """Project dir with codeband.yaml + a real store (no subtasks yet).

        ``handoff._resolve_store`` resolves the same DB path from the config, so
        the CLI and the test share one store. Returns ``(project_dir, store)``.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        workspace = tmp_path / "workspace"
        cfg = CodebandConfig(
            repo=RepoConfig(url="https://github.com/example/repo.git", branch="main"),
            agents=AgentsConfig(),
            workspace=WorkspaceConfig(path=str(workspace)),
        )
        cfg.to_yaml(project_dir / "codeband.yaml")
        # Active-room pointer: kickoff writes this; cb-phase resolves the
        # authoritative task_id (room UUID) from it, not from --task.
        (project_dir / ".codeband_room").write_text("room-1", encoding="utf-8")
        store = StateStore(workspace / "state" / "orchestration.db")
        store.create_task("room-1", "demo", "room-1")
        return project_dir, store

    def _seed(self, store, sid, chain):
        # Steps are (state, role) or (state, role, head_sha) — the sha form is
        # needed on the verify/review outcomes so the merge-eligibility gate
        # passes when a chain walks through merge_pending.
        for step in chain:
            new_state, role, *rest = step
            transition(sid, "room-1", new_state, caller_role=role, store=store,
                       head_sha=rest[0] if rest else None)

    def _run(self, project_dir, sid, verdict, monkeypatch=None):
        if monkeypatch is not None:
            # PR-pinned verdicts: the head SHA comes from the PR (gh seam),
            # never the invoker's cwd.
            monkeypatch.setattr(
                handoff, "_pr_head_sha", lambda project_dir, pr: "sha-pr-head",
            )
        return handoff.main([
            "review", sid, "--task", "room-1", "--pr", "42", verdict,
            "--project-dir", str(project_dir),
        ])

    _TO_REVIEW_PENDING = [
        ("assigned", "conductor"),
        ("in_progress", "coder"),
        ("verify_pending", "coder"),
        ("review_pending", "coder"),
    ]

    def test_approve_from_review_pending_passes(self, tmp_path, monkeypatch):
        project_dir, store = self._project(tmp_path)
        self._seed(store, "st-1", self._TO_REVIEW_PENDING)
        before = _log_count(store, "st-1")

        assert self._run(project_dir, "st-1", "--approve", monkeypatch) == 0
        assert store.get_subtask("st-1", "room-1").state == "review_passed"
        assert _log_count(store, "st-1") == before + 1
        last = _log_rows(store, "st-1")[-1]
        assert (last["from_state"], last["to_state"]) == (
            "review_pending", "review_passed",
        )
        assert last["caller_role"] == "reviewer"

    def test_reject_from_review_pending_fails_review(self, tmp_path, monkeypatch):
        project_dir, store = self._project(tmp_path)
        self._seed(store, "st-1", self._TO_REVIEW_PENDING)

        assert self._run(project_dir, "st-1", "--reject", monkeypatch) == 0
        sub = store.get_subtask("st-1", "room-1")
        assert sub.state == "review_failed"
        assert sub.review_round == 1  # a reject counts as one failed review round
        last = _log_rows(store, "st-1")[-1]
        assert (last["from_state"], last["to_state"]) == (
            "review_pending", "review_failed",
        )
        assert last["caller_role"] == "reviewer"

    @pytest.mark.parametrize(
        "label, chain",
        [
            ("in_progress", [("assigned", "conductor"), ("in_progress", "coder")]),
            ("verify_pending", [
                ("assigned", "conductor"),
                ("in_progress", "coder"),
                ("verify_pending", "coder"),
            ]),
            ("blocked", [
                ("assigned", "conductor"),
                ("in_progress", "coder"),
                ("blocked", "coder"),
            ]),
            ("merged", [
                ("assigned", "conductor"),
                ("in_progress", "coder"),
                ("verify_pending", "coder"),
                ("review_pending", "coder", "sha-1"),
                ("review_passed", "reviewer", "sha-1"),
                ("merge_pending", "mergemaster", "sha-1"),
                ("merged", "mergemaster"),
            ]),
        ],
    )
    def test_verdict_illegal_outside_review_pending_writes_nothing(
        self, tmp_path, monkeypatch, label, chain,
    ):
        project_dir, store = self._project(tmp_path)
        self._seed(store, "st-1", chain)
        state_before = store.get_subtask("st-1", "room-1").state
        before = _log_count(store, "st-1")

        # The CLI surfaces the FSM rejection as a non-zero exit…
        assert self._run(project_dir, "st-1", "--approve", monkeypatch) != 0
        assert self._run(project_dir, "st-1", "--reject", monkeypatch) != 0
        # …and nothing was written for either attempt.
        assert store.get_subtask("st-1", "room-1").state == state_before
        assert _log_count(store, "st-1") == before

        # The FSM is the actual guard: a direct transition raises, writing nothing.
        for verdict in ("review_passed", "review_failed"):
            with pytest.raises(InvalidTransitionError):
                transition("st-1", "room-1", verdict, caller_role="reviewer",
                           store=store)
        assert _log_count(store, "st-1") == before


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
        for new_state, role, sha in [
            ("assigned", "conductor", None),
            ("in_progress", "coder", None),
            ("verify_pending", "coder", None),
            ("review_pending", "coder", f"sha-{sid}"),
            ("review_passed", "reviewer", f"sha-{sid}"),
            ("merge_pending", "mergemaster", f"sha-{sid}"),
            ("merged", "mergemaster", None),
        ]:
            transition(sid, "room-1", new_state, caller_role=role, store=store,
                       head_sha=sha)

    def test_no_double_merge_across_set(self, tmp_path):
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        sids = [f"st-{i}" for i in range(4)]
        for sid in sids:
            self._drive_to_merged(store, sid)

        # Every subtask is merged; a SECOND merge of any of them is rejected
        # (terminal state) and appends no extra 'merged' row.
        for sid in sids:
            assert store.get_subtask(sid, "room-1").state == "merged"
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
            assert store.get_subtask(sid, "room-1").state == "review_pending"
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

        assert store.get_subtask("st-0", "room-1").state == "blocked"
        assert store.get_subtask("st-2", "room-1").state == "blocked"
        assert store.get_subtask("st-1", "room-1").state == "in_progress"
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
        health = daemon._subtask_state[("room-1", "st-a")]
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
        assert store.get_subtask("st-b", "room-1").state == "in_progress"
        await daemon._check_subtask_progress(now)   # stall → 2 == cap → blocked

        assert store.get_subtask("st-b", "room-1").state == "blocked"
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


# ─────────────────────────────────────────────────────────────────────────────
# 6. Per-subtask review-round cap — bounds a PROGRESSING loop in code
# ─────────────────────────────────────────────────────────────────────────────


class TestReviewRoundCap:
    """The FSM's durable per-subtask review-round cap (``MAX_REVIEW_ROUNDS``).

    This is the loop the watchdog cannot catch: ``review_failed → in_progress →
    verify_pending → review_pending → review_failed`` with a *real commit every
    round*, so git HEAD advances and the watchdog's stall cap never fires. The
    cap is enforced in ``fsm.transition`` against a durable count, so it survives
    a crash/reopen and is independent per subtask.
    """

    # ── helpers ────────────────────────────────────────────────────────────

    def _fsm_cycle_to_review_failed(self, store, sid, *, first):
        """Run one review cycle ending at ``review_failed`` (pure FSM, no git).

        ``first=True`` starts from ``planned`` (assign first); otherwise starts
        from ``review_failed`` (the rework edge that the cap guards).
        """
        if first:
            transition(sid, "room-1", "assigned", caller_role="conductor", store=store)
        transition(sid, "room-1", "in_progress", caller_role="coder", store=store)
        transition(sid, "room-1", "verify_pending", caller_role="coder", store=store)
        transition(sid, "room-1", "review_pending", caller_role="coder", store=store)
        transition(sid, "room-1", "review_failed", caller_role="reviewer", store=store)

    def _drive_to_cap(self, store, sid):
        """Cycle ``sid`` to ``review_failed`` exactly ``MAX_REVIEW_ROUNDS`` times."""
        self._fsm_cycle_to_review_failed(store, sid, first=True)          # round 1
        for _ in range(MAX_REVIEW_ROUNDS - 1):                            # rounds 2..MAX
            self._fsm_cycle_to_review_failed(store, sid, first=False)

    # ── the crux: a progressing loop, bounded by the cap ─────────────────────

    def test_progressing_loop_hits_cap_with_real_commits(self, tmp_path):
        """A real commit every round (HEAD advances) — NOT a stall — and the cap
        still rejects ``review_failed → in_progress`` at the cap, writing nothing.
        Only ``review_failed → blocked`` is then legal."""
        repo = _init_repo(tmp_path / "repo")
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")

        heads: list[str] = []

        # Round 1: assign → … → review_failed, with a real commit.
        self._fsm_cycle_to_review_failed(store, "st-cap", first=True)
        heads.append(_commit_on(repo, "feat-cap", "round-1"))
        assert store.get_subtask("st-cap", "room-1").review_round == 1

        # Rounds 2..MAX: rework is legal each time (count below cap), and every
        # round lands a real commit so HEAD keeps moving.
        for r in range(2, MAX_REVIEW_ROUNDS + 1):
            self._fsm_cycle_to_review_failed(store, "st-cap", first=False)
            heads.append(_commit_on(repo, "feat-cap", f"round-{r}"))
            assert store.get_subtask("st-cap", "room-1").review_round == r

        # Every round advanced HEAD — this is a progressing loop, not a stall.
        assert len(set(heads)) == len(heads) == MAX_REVIEW_ROUNDS

        # At the cap, the rework edge is rejected with an ACTIONABLE error and
        # NOTHING is written (no state change, no log row, count unchanged).
        assert store.get_subtask("st-cap", "room-1").state == "review_failed"
        assert store.get_subtask("st-cap", "room-1").review_round == MAX_REVIEW_ROUNDS
        before = _log_count(store, "st-cap")
        with pytest.raises(InvalidTransitionError) as exc:
            transition("st-cap", "room-1", "in_progress", caller_role="coder",
                       store=store)
        message = str(exc.value).lower()
        assert "cap" in message and "blocked" in message  # actionable: how to escape
        assert store.get_subtask("st-cap", "room-1").state == "review_failed"
        assert store.get_subtask("st-cap", "room-1").review_round == MAX_REVIEW_ROUNDS
        assert _log_count(store, "st-cap") == before  # nothing written on rejection

        # The legal escalation out of review_failed at the cap is → blocked.
        transition("st-cap", "room-1", "blocked", caller_role="coder", store=store)
        assert store.get_subtask("st-cap", "room-1").state == "blocked"
        assert _log_count(store, "st-cap") == before + 1

    def test_configurable_cap_rejects_at_explicit_max(self, tmp_path):
        """The cap is configurable: passing ``max_review_rounds=1`` bounds the
        loop after a single failed review."""
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        self._fsm_cycle_to_review_failed(store, "st-1", first=True)  # round 1
        assert store.get_subtask("st-1", "room-1").review_round == 1

        before = _log_count(store, "st-1")
        with pytest.raises(InvalidTransitionError):
            transition("st-1", "room-1", "in_progress", caller_role="coder",
                       store=store, max_review_rounds=1)
        assert _log_count(store, "st-1") == before  # nothing written

        # The default cap (3) would still allow this rework — proving the bound
        # came from the override, not the default.
        transition("st-1", "room-1", "in_progress", caller_role="coder", store=store)
        assert store.get_subtask("st-1", "room-1").state == "in_progress"

    # ── durability: the count survives a crash/reopen mid-loop ───────────────

    def test_cap_survives_store_reopen(self, tmp_path):
        """A crash mid-loop must not reset the cap: the durable count persists
        across a fresh ``StateStore`` on the same DB file, and the cap still
        fires after reopen."""
        db_path = tmp_path / "state" / "orchestration.db"
        store = StateStore(db_path)
        store.create_task("room-1", "demo", "room-1")
        self._drive_to_cap(store, "st-d")
        assert store.get_subtask("st-d", "room-1").review_round == MAX_REVIEW_ROUNDS

        # Simulate a crash/restart: drop the handle, reopen the same file fresh.
        del store
        reopened = StateStore(db_path)
        assert reopened.get_subtask("st-d", "room-1").review_round == MAX_REVIEW_ROUNDS
        assert reopened.get_subtask("st-d", "room-1").state == "review_failed"

        before = _log_count(reopened, "st-d")
        with pytest.raises(InvalidTransitionError):
            transition("st-d", "room-1", "in_progress", caller_role="coder",
                       store=reopened)
        assert _log_count(reopened, "st-d") == before          # nothing written
        assert reopened.get_subtask("st-d", "room-1").review_round == MAX_REVIEW_ROUNDS

    # ── isolation: one subtask's cap does not affect another's counter ───────

    def test_per_subtask_round_counters_are_independent(self, tmp_path):
        """N concurrent subtasks each carry their own ``review_round``: one
        hitting the cap leaves another's rework untouched."""
        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")

        self._drive_to_cap(store, "st-capped")                       # → MAX
        self._fsm_cycle_to_review_failed(store, "st-fresh", first=True)  # → 1
        # A third, mid-loop, to show counters are tracked independently.
        self._fsm_cycle_to_review_failed(store, "st-mid", first=True)
        self._fsm_cycle_to_review_failed(store, "st-mid", first=False)   # → 2

        assert store.get_subtask("st-capped", "room-1").review_round == MAX_REVIEW_ROUNDS
        assert store.get_subtask("st-fresh", "room-1").review_round == 1
        assert store.get_subtask("st-mid", "room-1").review_round == 2

        # The capped subtask rejects rework…
        with pytest.raises(InvalidTransitionError):
            transition("st-capped", "room-1", "in_progress", caller_role="coder",
                       store=store)
        # …while the others, below the cap, rework freely.
        transition("st-fresh", "room-1", "in_progress", caller_role="coder",
                   store=store)
        transition("st-mid", "room-1", "in_progress", caller_role="coder", store=store)
        assert store.get_subtask("st-fresh", "room-1").state == "in_progress"
        assert store.get_subtask("st-mid", "room-1").state == "in_progress"
        assert store.get_subtask("st-capped", "room-1").state == "review_failed"

    # ── independence: round cap and watchdog stall cap catch disjoint faults ──

    async def test_round_cap_distinct_from_watchdog_stall_cap(self, tmp_path, monkeypatch):
        """The watchdog (even with a tight stall cap) NEVER fires on a loop that
        commits real code every round; the FSM round cap bounds that same loop.
        Proves the two are separate mechanisms catching disjoint failures."""
        repo = _init_repo(tmp_path / "repo")
        _commit_on(repo, "feat-x", "seed")
        _git(repo, "checkout", "main")
        monkeypatch.chdir(repo)

        store = _new_store(tmp_path)
        store.create_task("room-1", "demo", "room-1")
        store.ensure_subtask("st-x", "room-1", state="in_progress",
                             metadata={"branch": "feat-x"})  # pr None → no gh

        rest = _fake_rest()
        # A deliberately TIGHT stall cap — it would fire on any 2-patrol stall.
        daemon = WatchdogDaemon(
            config=WatchdogConfig(max_phase_visits=2, git_progress_check=True),
            rest_client=rest,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            state_store=store,
        )
        now = datetime.now(timezone.utc)

        await daemon._check_subtask_progress(now)  # baseline

        # First failed review (round 1).
        transition("st-x", "room-1", "verify_pending", caller_role="coder", store=store)
        transition("st-x", "room-1", "review_pending", caller_role="coder", store=store)
        transition("st-x", "room-1", "review_failed", caller_role="reviewer", store=store)

        # Each subsequent round: rework, a REAL commit (HEAD moves), a watchdog
        # patrol (which therefore sees progress), then back to review_failed.
        for r in range(2, MAX_REVIEW_ROUNDS + 1):
            transition("st-x", "room-1", "in_progress", caller_role="coder", store=store)
            _commit_on(repo, "feat-x", f"round-{r}")
            _git(repo, "checkout", "main")
            await daemon._check_subtask_progress(now)
            transition("st-x", "room-1", "verify_pending", caller_role="coder",
                       store=store)
            transition("st-x", "room-1", "review_pending", caller_role="coder",
                       store=store)
            transition("st-x", "room-1", "review_failed", caller_role="reviewer",
                       store=store)

        # The watchdog never blocked it — HEAD advanced every patrol, so its
        # stall counter kept resetting. This is the loop it cannot catch.
        assert daemon._subtask_state[("room-1", "st-x")].patrol_visits_without_progress == 0
        assert rest.agent_api_messages.create_agent_chat_message.await_count == 0
        assert store.get_subtask("st-x", "room-1").state == "review_failed"
        assert store.get_subtask("st-x", "room-1").review_round == MAX_REVIEW_ROUNDS

        # …but the FSM round cap DOES bound the same progressing loop.
        before = _log_count(store, "st-x")
        with pytest.raises(InvalidTransitionError):
            transition("st-x", "room-1", "in_progress", caller_role="coder",
                       store=store)
        assert _log_count(store, "st-x") == before


# ─────────────────────────────────────────────────────────────────────────────
# 7. Per-subtask verify-attempt cap — bounds a PROGRESSING verify loop in code
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifyAttemptCap:
    """The handoff CLI's durable per-subtask verify-attempt cap.

    This is the verify-leg sibling of ``TestReviewRoundCap``. The loop it bounds:
    a coder parked at ``verify_pending`` calls ``cb-phase verify``, a gate fails
    (here: the verify command exits non-zero), the coder *edits and commits*
    (git HEAD advances — it looks like progress), and tries again — forever. The
    watchdog's stall cap reads git HEAD, so a HEAD-advancing loop resets its
    counter every patrol and it never fires; the review-round cap counts
    ``review_failed`` re-entries, which this subtask never reaches. The cap is
    enforced in ``cli/handoff.py`` against a durable ``verify_attempts`` count
    (cumulative, never reset), so it survives a crash/reopen and is per-subtask.

    Semantics (a faithful mirror of the review-round cap): each *rejected* verify
    attempt increments ``verify_attempts`` (a *success* never does); once the
    count has reached ``MAX_VERIFY_ATTEMPTS``, the *next* call escalates
    ``verify_pending → blocked`` and writes nothing but that transition — exactly
    as the review-round cap records ``MAX_REVIEW_ROUNDS`` failures before refusing
    the next rework.
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    def _project(self, tmp_path, *, verify_command="exit 1", max_verify_attempts=None):
        """Build a project dir with codeband.yaml + a store (no subtasks yet).

        ``verify_command`` defaults to ``exit 1`` so every verify attempt is
        *rejected* at the (real subprocess) verify gate while the tree stays
        clean and the PR is reported OPEN — isolating the cap from the other
        gates. project_dir is kept separate from the git worktree so writing
        codeband.yaml never dirties the tree. Returns ``(project_dir, store)``.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        workspace = tmp_path / "workspace"
        agents_kwargs = {"handoff_verify_command": verify_command}
        if max_verify_attempts is not None:
            agents_kwargs["max_verify_attempts"] = max_verify_attempts
        cfg = CodebandConfig(
            repo=RepoConfig(url="https://github.com/example/repo.git",
                            branch="main"),
            agents=AgentsConfig(**agents_kwargs),
            workspace=WorkspaceConfig(path=str(workspace)),
        )
        cfg.to_yaml(project_dir / "codeband.yaml")
        # Active-room pointer: kickoff writes this; cb-phase resolves the
        # authoritative task_id (room UUID) from it, not from --task.
        (project_dir / ".codeband_room").write_text("room-1", encoding="utf-8")
        store = StateStore(workspace / "state" / "orchestration.db")
        (store.db_path.parent / ".codeband_room").write_text("room-1", encoding="utf-8")
        store.create_task("room-1", "demo", "room-1")
        return project_dir, store

    def _seed_verify_pending(self, store, sid, branch):
        """Drive a fresh subtask to ``verify_pending`` with its branch recorded.

        The branch goes in metadata *before* the first transition (which only
        ``ensure_subtask``s without metadata), so the watchdog can read the
        subtask's git HEAD.
        """
        store.ensure_subtask(sid, "room-1", metadata={"branch": branch})
        transition(sid, "room-1", "assigned", caller_role="conductor", store=store)
        transition(sid, "room-1", "in_progress", caller_role="coder", store=store)
        transition(sid, "room-1", "verify_pending", caller_role="coder", store=store)

    def _run_verify(self, project_dir, worktree, subtask_id, *, pr=42):
        return handoff.main([
            "verify", subtask_id,
            "--task", "room-1",
            "--pr", str(pr),
            "--worktree", str(worktree),
            "--project-dir", str(project_dir),
        ])

    # ── the crux: a progressing verify loop, bounded by the cap ──────────────

    async def test_cap_fires_on_progressing_loop_distinct_from_stall(
        self, tmp_path, monkeypatch,
    ):
        """A real commit before every attempt (HEAD advances) — NOT a stall —
        and the verify cap still escalates to ``blocked`` at the cap, while a
        deliberately TIGHT-stall watchdog patrolling the same loop never fires.
        Proves the verify cap and the watchdog stall cap catch disjoint faults.
        """
        repo = _init_repo(tmp_path / "repo")
        _commit_on(repo, "feat-v", "seed")          # create the subtask's branch
        _git(repo, "checkout", "main")
        monkeypatch.chdir(repo)                      # watchdog runs git here
        _stub_pr_snapshot(monkeypatch, repo)  # gh seam: PR OPEN, tracking the repo

        # Default cap (20). verify_command 'exit 1' rejects every attempt.
        project_dir, store = self._project(tmp_path, verify_command="exit 1")
        self._seed_verify_pending(store, "st-5", "feat-v")
        assert MAX_VERIFY_ATTEMPTS == 20             # the documented default

        rest = _fake_rest()
        daemon = WatchdogDaemon(
            config=WatchdogConfig(max_phase_visits=2, git_progress_check=True),
            rest_client=rest,
            agent_id="agent-wd",
            conductor_id="agent-cond",
            state_store=store,
        )
        now = datetime.now(timezone.utc)
        await daemon._check_subtask_progress(now)    # baseline patrol

        base_log = _log_count(store, "st-5")         # 3 seeding rows
        for i in range(1, MAX_VERIFY_ATTEMPTS + 1):  # MAX rejecting attempts
            _commit_on(repo, "feat-v", f"attempt-{i}")   # REAL HEAD movement
            _git(repo, "checkout", "main")
            await daemon._check_subtask_progress(now)    # watchdog sees progress
            assert self._run_verify(project_dir, repo, "st-5") != 0
            sub = store.get_subtask("st-5", "room-1")
            assert sub.verify_attempts == i              # one count per rejection
            assert sub.state == "verify_pending"         # rejection ≠ transition
            assert _log_count(store, "st-5") == base_log  # no log row on rejection

        # The progressing loop never tripped the (tight) watchdog stall cap.
        assert daemon._subtask_state[("room-1", "st-5")].patrol_visits_without_progress == 0
        assert rest.agent_api_messages.create_agent_chat_message.await_count == 0
        assert store.get_subtask("st-5", "room-1").verify_attempts == MAX_VERIFY_ATTEMPTS

        # The next verify call hits the cap: escalate verify_pending → blocked,
        # writing nothing but the blocked transition (no further increment).
        assert self._run_verify(project_dir, repo, "st-5") != 0
        sub = store.get_subtask("st-5", "room-1")
        assert sub.state == "blocked"
        assert sub.verify_attempts == MAX_VERIFY_ATTEMPTS  # not bumped on escalate
        assert _log_count(store, "st-5") == base_log + 1
        last = _log_rows(store, "st-5")[-1]
        assert (last["from_state"], last["to_state"]) == ("verify_pending", "blocked")
        assert last["caller_role"] == "coder"

    def test_configurable_cap_rejects_at_explicit_max(self, tmp_path, monkeypatch):
        """The cap is configurable: ``max_verify_attempts=2`` bounds the loop
        after two rejected attempts; the third call escalates to ``blocked``."""
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)
        project_dir, store = self._project(
            tmp_path, verify_command="exit 1", max_verify_attempts=2,
        )
        self._seed_verify_pending(store, "st-3", "feat-c")
        base = _log_count(store, "st-3")

        assert self._run_verify(project_dir, repo, "st-3") != 0   # attempt 1
        assert self._run_verify(project_dir, repo, "st-3") != 0   # attempt 2
        sub = store.get_subtask("st-3", "room-1")
        assert sub.verify_attempts == 2
        assert sub.state == "verify_pending"
        assert _log_count(store, "st-3") == base                  # no log rows

        # Third call: count has reached the (overridden) cap → blocked.
        assert self._run_verify(project_dir, repo, "st-3") != 0
        assert store.get_subtask("st-3", "room-1").state == "blocked"
        assert store.get_subtask("st-3", "room-1").verify_attempts == 2     # not re-bumped
        assert _log_count(store, "st-3") == base + 1

    # ── durability: the count survives a crash/reopen mid-loop ───────────────

    def test_cap_survives_store_reopen(self, tmp_path, monkeypatch):
        """A crash mid-loop must not reset the cap: the durable count persists
        across a fresh ``StateStore`` on the same DB file, and the cap still
        fires after reopen."""
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)
        project_dir, store = self._project(
            tmp_path, verify_command="exit 1", max_verify_attempts=3,
        )
        self._seed_verify_pending(store, "st-4", "feat-d")

        assert self._run_verify(project_dir, repo, "st-4") != 0   # attempt 1
        assert self._run_verify(project_dir, repo, "st-4") != 0   # attempt 2
        assert store.get_subtask("st-4", "room-1").verify_attempts == 2

        # Simulate a crash/restart: drop the handle, reopen the same file fresh.
        db_path = store.db_path
        del store
        reopened = StateStore(db_path)
        assert reopened.get_subtask("st-4", "room-1").verify_attempts == 2  # survived reopen
        assert reopened.get_subtask("st-4", "room-1").state == "verify_pending"

        base = _log_count(reopened, "st-4")
        assert self._run_verify(project_dir, repo, "st-4") != 0   # attempt 3 → cap
        assert reopened.get_subtask("st-4", "room-1").verify_attempts == 3
        assert _log_count(reopened, "st-4") == base               # still no log row

        # Next call after reopen escalates — the cap fired across the "crash".
        assert self._run_verify(project_dir, repo, "st-4") != 0
        assert reopened.get_subtask("st-4", "room-1").state == "blocked"
        assert _log_count(reopened, "st-4") == base + 1

    # ── isolation: one subtask's cap does not affect another's counter ───────

    def test_per_subtask_verify_counters_are_independent(self, tmp_path, monkeypatch):
        """N concurrent subtasks each carry their own ``verify_attempts``: one
        hitting the cap leaves the others' attempts and state untouched."""
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)
        project_dir, store = self._project(
            tmp_path, verify_command="exit 1", max_verify_attempts=3,
        )
        for sid, branch in [("st-1", "feat-a"), ("st-2", "feat-b"),
                            ("st-3", "feat-c")]:
            self._seed_verify_pending(store, sid, branch)

        # st-1 → cap (3 rejections, then a 4th call escalates to blocked).
        for _ in range(3):
            assert self._run_verify(project_dir, repo, "st-1") != 0
        assert self._run_verify(project_dir, repo, "st-1") != 0
        # st-2 once, st-3 twice — both below the cap.
        assert self._run_verify(project_dir, repo, "st-2") != 0
        for _ in range(2):
            assert self._run_verify(project_dir, repo, "st-3") != 0

        assert store.get_subtask("st-1", "room-1").state == "blocked"
        assert store.get_subtask("st-1", "room-1").verify_attempts == 3
        assert store.get_subtask("st-2", "room-1").state == "verify_pending"
        assert store.get_subtask("st-2", "room-1").verify_attempts == 1
        assert store.get_subtask("st-3", "room-1").state == "verify_pending"
        assert store.get_subtask("st-3", "room-1").verify_attempts == 2

        # The capped subtask is blocked, but the others — below their own caps —
        # keep accepting attempts independently.
        assert self._run_verify(project_dir, repo, "st-2") != 0
        assert store.get_subtask("st-2", "room-1").state == "verify_pending"
        assert store.get_subtask("st-2", "room-1").verify_attempts == 2

    # ── interaction: verify cap and review-round cap are independent loops ────

    def test_verify_cap_and_review_cap_are_independent_counters(
        self, tmp_path, monkeypatch,
    ):
        """``verify_attempts`` and ``review_round`` count disjoint loops: verify
        rejections never touch the review counter, a verify *success* never bumps
        either, and a failed review never touches the verify counter. A subtask
        can approach one cap without affecting the other."""
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)
        # Cap high enough that nothing blocks during this test.
        project_dir, store = self._project(
            tmp_path, verify_command="exit 1", max_verify_attempts=20,
        )
        self._seed_verify_pending(store, "st-6", "feat-i")

        # Two verify rejections: verify_attempts climbs, review_round stays 0.
        assert self._run_verify(project_dir, repo, "st-6") != 0
        assert self._run_verify(project_dir, repo, "st-6") != 0
        sub = store.get_subtask("st-6", "room-1")
        assert sub.verify_attempts == 2
        assert sub.review_round == 0

        # Now verify *passes* (verify command exits 0) → advances to
        # review_pending; a success leaves verify_attempts untouched.
        monkeypatch.setattr(handoff, "_verify_command", lambda project_dir, worktree: "exit 0")
        assert self._run_verify(project_dir, repo, "st-6") == 0
        sub = store.get_subtask("st-6", "room-1")
        assert sub.state == "review_pending"
        assert sub.verify_attempts == 2          # success never increments
        assert sub.review_round == 0

        # A failed review increments review_round only — verify_attempts is the
        # other cap's counter and stays put.
        transition("st-6", "room-1", "review_failed", caller_role="reviewer",
                   store=store)
        sub = store.get_subtask("st-6", "room-1")
        assert sub.review_round == 1
        assert sub.verify_attempts == 2


# ─────────────────────────────────────────────────────────────────────────────
# Active-room resolution — cb-phase derives the authoritative task_id from
# <project_dir>/.codeband_room, never from the agent-supplied --task label.
#
# Regression guard for the room_id-vs-task_key bug: tasks.task_id is the room
# UUID (kickoff sets task_id == room_id), but agents are trained on the semantic
# task_key (e.g. "subtract-fn") and pass *that* to --task. Trusting it FK-failed
# ``ensure_subtask`` and the verify gate never seeded. Now the room UUID from
# .codeband_room wins and the bogus label is ignored.
# ─────────────────────────────────────────────────────────────────────────────


class TestActiveRoomResolution:
    """cb-phase resolves the room UUID from .codeband_room; --task is a label."""

    # The authoritative id (what kickoff writes to .codeband_room and uses as
    # tasks.task_id) vs the semantic key an agent wrongly passes to --task.
    ROOM = "058dafc0-7913-4c09-98e5-d1b6a96b1358"
    BOGUS = "subtract-fn"

    def _project(self, tmp_path, *, write_room=True, verify_command=None):
        """Real project_dir + codeband.yaml + store with an active task row.

        ``write_room`` controls whether the active-room pointer exists. The
        store path matches what ``handoff._resolve_store`` resolves from config.
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
        if write_room:
            (project_dir / ".codeband_room").write_text(
                self.ROOM, encoding="utf-8",
            )
        store = StateStore(workspace / "state" / "orchestration.db")
        store.create_task(self.ROOM, "subtract demo", self.ROOM)
        return project_dir, store

    def test_start_resolves_room_and_ignores_bogus_task(self, tmp_path):
        # cb-phase start with a bogus --task label: it must resolve the room
        # UUID from .codeband_room, seed the subtask FK'd to THAT, and walk to
        # in_progress — exactly the path that FK-crashed before this fix.
        project_dir, store = self._project(tmp_path)

        rc = handoff.main([
            "start", "st-1", "--task", self.BOGUS,
            "--project-dir", str(project_dir),
        ])
        assert rc == 0

        sub = store.get_subtask("st-1", self.ROOM)
        assert sub is not None
        assert sub.task_id == self.ROOM      # FK target is the room UUID …
        assert sub.task_id != self.BOGUS     # … never the semantic label
        assert sub.state == "in_progress"

        # The walk really happened, and no stray task row was minted for the key.
        trail = [(r["from_state"], r["to_state"]) for r in _log_rows(store, "st-1")]
        assert trail == [("planned", "assigned"), ("assigned", "in_progress")]
        assert store.get_task(self.BOGUS) is None

    def test_verify_resolves_room_and_advances(self, tmp_path, monkeypatch):
        # cb-phase verify with a bogus --task: self-seeds from missing using the
        # resolved room, then the gate advances it to review_pending. Real clean
        # git tree + real passing verify command; only the gh PR call is stubbed.
        project_dir, store = self._project(tmp_path, verify_command="exit 0")
        repo = _init_repo(tmp_path / "repo")
        _stub_pr_snapshot(monkeypatch, repo)

        rc = handoff.main([
            "verify", "st-1", "--task", self.BOGUS, "--pr", "42",
            "--worktree", str(repo), "--project-dir", str(project_dir),
        ])
        assert rc == 0

        sub = store.get_subtask("st-1", self.ROOM)
        assert sub.task_id == self.ROOM
        assert sub.state == "review_pending"
        last = _log_rows(store, "st-1")[-1]
        assert (last["from_state"], last["to_state"]) == (
            "verify_pending", "review_pending",
        )

    def test_missing_pointer_errors_clean_and_writes_nothing(self, tmp_path, capsys):
        # No .codeband_room: must NOT FK-crash and must NOT silently proceed —
        # a clear non-zero "no active task" error, and nothing written.
        project_dir, store = self._project(tmp_path, write_room=False)

        rc = handoff.main([
            "start", "st-1", "--task", self.BOGUS,
            "--project-dir", str(project_dir),
        ])
        assert rc == handoff.EXIT_NO_ACTIVE_TASK
        assert "no active task" in capsys.readouterr().err
        assert store.get_subtask("st-1", self.ROOM) is None      # no partial row
        assert _log_count(store, "st-1") == 0         # no transition written

    def test_pointer_without_task_row_errors_clean(self, tmp_path, capsys):
        # Pointer present but names a room with no tasks row (the other failure
        # branch): same clean error, same nothing-written guarantee.
        project_dir, store = self._project(tmp_path, write_room=True)
        (project_dir / ".codeband_room").write_text(
            "room-does-not-exist", encoding="utf-8",
        )

        rc = handoff.main([
            "start", "st-1", "--task", self.BOGUS,
            "--project-dir", str(project_dir),
        ])
        assert rc == handoff.EXIT_NO_ACTIVE_TASK
        err = capsys.readouterr().err
        assert "no active task" in err
        assert "room-does-not-exist" in err
        assert store.get_subtask("st-1", self.ROOM) is None
        assert _log_count(store, "st-1") == 0
