# RFC: Deterministic Orchestration for Codeband

**Status:** Draft
**Author:** Yoni Bagelman (with Claude Code)
**Scope:** Add a deterministic, code-enforced control plane (state store, FSM, gated handoffs, mechanical watchdog, universal rehydration) to codeband — *without* losing the LLM-Conductor flexibility, parallel agent pools, or low ops friction that make it useful.

> **Guiding principle:** *The LLM decides, code enforces and remembers — the FSM gates EFFECTS (transitions/merges), not the Conductor's creative routing.*

---

## 1. Motivation

Codeband today is **overwhelmingly LLM-driven orchestration with a thin deterministic scaffold**. There is no finite-state machine in code that governs the pipeline; "state" is free-text protocol envelopes that the Conductor LLM writes to memory and re-reads, and routing is the Conductor reacting to `@mentions`. This buys real strengths — flexibility on novel tasks, dynamic re-planning, and parallel pools of coders/reviewers — but it has no guarantees and weak crash semantics.

Concrete weaknesses in the current code:

- **Brittle, schemaless state.** Protocol envelopes are parsed by `src/codeband/orchestration/kickoff.py:_parse_envelope()` / `_format_task_status()` purely for `cb status` display. Nothing reads them to *drive* or *gate* a transition. There is no durable, queryable record of where each unit of work is.
- **In-memory, chat-recency watchdog.** `src/codeband/agents/watchdog.py` holds `AgentHealthState` in memory (lost on restart) and judges liveness by *chat-message recency only* — so it false-positives an agent doing long silent work, and cannot tell "genuinely progressing" from "stuck in a loop." It can nudge/escalate but has no mechanical progress signal and no cycle cap.
- **Coder-only rehydration.** Only coders rebuild context after a crash (`src/codeband/session/supervisor.py:WorkerSupervisor` + `src/codeband/session/context.py:build_recovery_context()`). Conductor, Mergemaster, Planner, Plan-Reviewer, and Code-Reviewer reconnect *blank* and re-derive everything from the room.
- **No transition enforcement.** Phase transitions are purely LLM-prompted. Nothing prevents a skipped review, a double-merge, an out-of-order move, or an infinite review loop — only prose admonitions in `prompts/conductor.md`.

This RFC was itself motivated by a live failure that is exactly on-point: a planning run stalled when a Codex review turn timed out (180s, no retry/escalation boundary) and a subsequent `422` left the agent's Band cursor stalled — a silent death the chat-recency watchdog never flagged. **The instabilities this RFC fixes are the ones that killed the run to write it.**

The reference implementation for the deterministic patterns is the author's `band-of-devs` fork (Docker-per-agent, shared volumes, CLI-gated transitions). The goal here is to **adapt**, not copy: band-of-devs is a single linear pipeline; codeband is a parallel swarm with a central LLM Conductor.

## 2. Design overview

### 2.1 The split: decide vs. enforce-and-remember

Codeband currently *fuses* deciding (creative, dynamic) with effecting (state changes). band-of-devs fuses them on the code side. The win is **separating them**:

- The **Conductor LLM keeps deciding** — decomposition, assignment to pool workers, re-planning, conflict handling. This is where flexibility lives; a rigid FSM here would make codeband worse.
- A **code control plane enforces and records every state-changing effect** — "this subtask advanced to review", "this PR merged" — validating against an FSM, persisting durably, and rejecting illegal moves.

The LLM can decide anything; it just cannot *make an illegal transition happen* or *lose track of state*.

### 2.2 Two-level model (the key adaptation for codeband's fan-out)

band-of-devs has one global `pipeline.yaml`, one phase at a time. Codeband fans out: planner → N subtasks → N coders → reviews → merges, concurrently. So state is modeled at **two levels**:

| Level | Owner | Governs |
|---|---|---|
| **Task** | LLM Conductor (loose) | decomposition, the assignment map, overall progress |
| **Subtask / PR** | code FSM (rigid) | the lifecycle of one unit of work, instantiated N times, with global invariants (no merge before approval, no double-merge, round caps) |

Your phase graph becomes the *per-subtask* graph; global invariants become code checks across the live instance set.

### 2.3 The enforcement seam: a validated CLI

In an `@mention`-driven, two-framework (Claude + Codex) world you cannot enforce a gate by hoping the LLM behaves. The portable mechanism — proven in band-of-devs (`band-phase` / `request-review`) — is a **validated CLI** the agent prompts must call, with all validation in the CLI. It works identically for Claude and Codex (both run shell commands), and the effect only happens if the CLI allows it, regardless of what the Conductor *intended*. The Conductor keeps routing via `@mentions`; the CLI gates the consequences.

---

## 3. Workstreams

All new state machinery lives under a new `src/codeband/state/` package — separable, independently testable, no circular imports — so the changes stay rebase-friendly and PR-able upstream.

### Workstream 1 — Typed durable state store

**New modules:** `src/codeband/state/__init__.py`, `src/codeband/state/store.py`.

A local **SQLite** DB at `{workspace_path}/state/orchestration.db`. Three tables:

```sql
CREATE TABLE tasks (
    task_id     TEXT PRIMARY KEY,   -- room_id as natural key
    description TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE subtask_states (
    subtask_id      TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id),
    state           TEXT NOT NULL DEFAULT 'planned',
    assigned_worker TEXT,
    pr_number       INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    metadata        TEXT              -- JSON blob
);

CREATE TABLE transition_log (        -- append-only audit
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subtask_id  TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    caller_role TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    reason      TEXT
);
```

`StateStore` API: `create_task()`, `ensure_subtask(subtask_id, task_id)` (`INSERT OR IGNORE`), `get_subtask()`, `list_active_subtasks()`.

**Row-creation timing:**
- **Task row** is created in `src/codeband/orchestration/kickoff.py:send_task()` using `room_id` as `task_id` — that is all that exists at kickoff; no subtask info is available yet.
- **Subtask rows** are created lazily on the first `fsm.transition()` for that subtask (`store.ensure_subtask(...)` before writing) — no Conductor action required.

**Why SQLite over Band-memory.** Band-memory has a hard ~1000-char content limit and is unavailable on the free tier (HTTP 402/403/404/501, probed once via `src/codeband/memory/probe.py:probe_memory_backend()`) and in offline/Docker runs. SQLite is local, persistent, queryable, and behaves identically in local (`runner.py:run_local()`) and distributed (`orchestration/agent_main.py`) modes. Band-memory is retained only as an *optional async secondary write for observability* — never on the read path.

**Integration points:** `orchestration/runner.py:_install_memory_backend()` also inits the `StateStore`; `kickoff.py:send_task()` writes the task row after room creation.

### Workstream 2 — Per-subtask FSM

**New module:** `src/codeband/state/fsm.py`.

States:

```
planned → assigned → in_progress → verify_pending → review_pending → review_passed → merge_pending → merged
                                                          ↘ review_failed → in_progress
                            ↘ blocked
                            ↘ abandoned
```

`VALID_TRANSITIONS` is a static dict keyed by `(current_state, caller_role)`:

| From (state, role) | Allowed next |
|---|---|
| `(planned, conductor)` | `assigned` |
| `(assigned, coder)` | `in_progress` |
| `(in_progress, coder)` | `verify_pending`, `blocked` |
| `(verify_pending, coder)` | `review_pending` *(only after the `cb-phase` gate passes)* |
| `(review_pending, reviewer)` | `review_passed`, `review_failed` |
| `(review_failed, coder)` | `in_progress` |
| `(review_passed, mergemaster)` | `merge_pending` |
| `(merge_pending, mergemaster)` | `merged` |
| `(any non-terminal, conductor)` | `abandoned` |
| `(any non-terminal, watchdog)` | `blocked` |

The last two are cross-cutting wildcards (enforced in `_is_allowed`, not enumerated per state): the Conductor may abandon, and the Watchdog (WS4) may block, any non-terminal subtask.

```python
def transition(subtask_id: str, task_id: str, new_state: str,
               caller_role: str, reason: str = "") -> None:
    """Auto-creates the subtask (ensure_subtask). Under BEGIN EXCLUSIVE:
    validates (current_state, caller_role) ∈ VALID_TRANSITIONS, writes the
    new state, appends transition_log. Raises InvalidTransitionError otherwise."""
```

Task-level routing is **not** code-enforced (Conductor owns it); only subtask-level *effects* are.

### Workstream 3 — Verify-gated handoffs (`cb-phase`)

The enforcement seam. Two steps because `src/codeband/cli.py` is a single ~1400-line flat module:

- **3a — refactor** `cli.py` → a `cli/` package: `src/codeband/cli/__init__.py` (mechanical move, no logic change; the `cb = "codeband.cli:cli"` entry point stays valid), then add `src/codeband/cli/handoff.py`.
- **3b — implement** the `cb-phase` entry point (register `cb-phase = "codeband.cli.handoff:main"` in `pyproject.toml`).

Gate sequence for `cb-phase verify <subtask_id> --task <task_id> --pr <n> [--worktree <path>]`:

1. `git -C <worktree> status --porcelain` must be empty (clean tree).
2. `gh pr view <n> --json state` must be `OPEN`.
3. If `agents.handoff_verify_command` is configured, run it; exit 0 required.
4. On success, `fsm.transition(subtask_id, task_id, "review_pending", caller_role="coder")`.

Config: add `handoff_verify_command: str | None = None` to `src/codeband/config.py:AgentsConfig`. The CLI imports no Band SDK and no asyncio — it is a pure subprocess callable by both Claude and Codex agents.

### Workstream 4 — Watchdog upgrade

Extend `src/codeband/agents/watchdog.py` with **mechanical progress signals** alongside the existing chat-recency:

- **Git HEAD per subtask:** `git rev-parse <task_branch>` for subtasks in `in_progress`/`verify_pending` (branches queried from SQLite).
- **PR state:** `gh pr view <pr_number> --json state,updatedAt`.
- **Transition-log recency:** `SELECT MAX(timestamp) FROM transition_log WHERE subtask_id=?`.

New `AgentHealthState` fields: `patrol_visits_without_progress: int = 0`, `last_git_head: str = ""`, `last_transition_timestamp: datetime | None = None`.

**Escalation ladder:**
1. Chat staleness → nudge the agent (existing `_send_nudge()`).
2. Nudge grace elapsed → escalate to Conductor (existing `_send_escalation()`).
3. `patrol_visits_without_progress >= max_phase_visits` (no git-HEAD change *and* no new transition-log entry across N patrols) → `fsm.transition(subtask_id, task_id, "blocked", "watchdog")`, notify Conductor + human (new deterministic path).

Config: add `max_phase_visits: int = 10` and `git_progress_check: bool = True` to `WatchdogConfig`.

> This step is what would have caught the stall that motivated this RFC: a timed-out turn produces no git-HEAD change and no transition-log entry, so the cycle/stall cap fires instead of the run dying silently.

### Workstream 5 — Universal rehydration

**New module:** `src/codeband/state/rehydration.py`.

```python
async def build_agent_recovery_context(agent_key: str, store: StateStore) -> str | None:
    """agent_key e.g. 'conductor', 'mergemaster', 'reviewer-codex-0'.
    Reads SQLite for non-terminal subtasks relevant to this role and returns
    a markdown recovery prompt (or None)."""
```

Per-role content: **conductor** → table of all non-terminal subtasks (id, state, worker, pr); **mergemaster** → subtasks in `merge_pending`/`review_passed`; **reviewer-*** → subtasks in `review_pending`; **planner-*** → active task description; **plan-reviewer-*** → task description + subtask count.

**Plumbing (explicit, so the change is mechanical and reviewable):** add `recovery_context: str | None = None` to the five non-coder factories in `orchestration/runner.py` (`_create_conductor`, `_create_planner`, `_create_plan_reviewer`, `_create_code_reviewer`, `_create_mergemaster`) and to the corresponding runner `__init__`s in `agents/{conductor,planner,plan_reviewer,code_reviewer,mergemaster}.py`. Each prepends the recovery context into the system prompt — the same convention `_create_coder()` already uses. `runner.py:_run_agent_forever()` calls `build_agent_recovery_context()` on each reconnect; `orchestration/agent_main.py:main()` calls it before `agent.run()` in distributed mode. The existing coder path (`session/supervisor.py` + `session/context.py`) is unchanged.

Distributed/Docker mode is favored for crash isolation (one agent per process), so a single crash no longer takes down all agents as it can in the default single-process `cb run`.

---

## 4. Phasing

Each phase is independently valuable and a standalone PR.

| Phase | Content | Depends on | PR story |
|---|---|---|---|
| **0 — Baseline** | `pip install -e ".[dev]" && pytest` green on `main`. No new code. | — | trivial (this RFC is docs-only) |
| **1 — State store (shadow)** | `state/__init__.py`, `state/store.py`, `tests/test_state_store.py`. Hooks: `runner.py:_install_memory_backend()` inits the store; `kickoff.py:send_task()` writes the task row. **Record-only — zero behavior change.** | — | merge first |
| **2 — FSM + gated handoffs** | `state/fsm.py`, `cli/__init__.py` (move), `cli/handoff.py`, tests. `pyproject.toml` adds `cb-phase`; `config.py` adds `handoff_verify_command`. | Phase 1 | rebase onto Phase 1 |
| **3 — Watchdog upgrade** | extend `agents/watchdog.py` (mechanical signals + `max_phase_visits` path), `config.py` knobs, `tests/test_watchdog_upgrade.py`. | Phase 1 | parallel to Phase 2 |
| **4 — Universal rehydration** | `state/rehydration.py`, factory/constructor `recovery_context` plumbing, `agent_main.py` hook, tests. | Phase 1 | parallel to Phase 2/3 |

**Acceptance highlights.** Phase 1: all existing tests pass + store tests pass + no observable behavior change in `cb run`. Phase 2: `cb-phase verify` exits 0 only on clean tree + open PR + passing verify; FSM rejects invalid transitions and wrong caller-role; `cb` import path unchanged. Phase 3: watchdog marks a subtask `blocked` after `max_phase_visits` patrols with no git-HEAD change; existing nudge/escalate behavior intact. Phase 4: a simulated Conductor crash+restart receives the in-progress subtask table in its prompt; coder supervisor path unchanged.

---

## 5. Risks

**Risk 1 — Code FSM vs. LLM Conductor conflict.** The FSM gates *effects*, not *routing*. If the Conductor tells a coder to jump to `review_pending` without the `cb-phase` gate, the CLI raises `InvalidTransitionError` and exits non-zero — the agent sees an actionable error and retries the correct sequence or escalates to a human. The FSM never silently ignores a bad transition. The prompts must include a "transition rejected → here's what to do" path so the Conductor recovers gracefully rather than looping.

**Risk 2 — Band-memory is too weak to be the store.** ~1000-char content limit, free-tier unavailability, no offline/Docker support, opaque substring-only query. SQLite is local, unbounded, queryable, and identical across all run modes. Band-memory stays as an optional async observability mirror, never on the read path.

**Risk 3 — Fork maintenance cost.** All new machinery lives under `src/codeband/state/` as separable, independently-testable submodules with no circular imports; the `cli.py → cli/` refactor is mechanical. Each phase is a standalone, cherry-pickable, revertable PR — which is also the upstream-contribution path: these land on `thenvoi/codeband` one phase at a time.

---

## 6. Reference patterns (band-of-devs)

Adapted, not copied verbatim, to codeband's in-process / Band-coordinated / pool-parallel architecture:

- `docker/shared/pipeline_phase.py` — static `VALID_TRANSITIONS`, atomic validated transitions, caller-role enforcement, transition log → **Workstreams 1–2**.
- `docker/shared/pipeline_watchdog.py` — per-phase thresholds, `MAX_PHASE_VISITS` cycle detection, git-HEAD/handoff progress signals, HITL pause, escalation ladder → **Workstream 4**.
- `docker/shared/repo_init.py` — idempotent, file-locked rehydration → **Workstream 5**.
- `docker/claude/bin/request-review` — clean-tree + verify-exit-0 gated handoff → **Workstream 3**.
