# Codeband Architecture

How Codeband works under the hood — for operators debugging a stuck system, contributors adding new agents or protocols, and anyone curious about the design decisions.

For installation and usage, see the [README](README.md). For deployment topologies, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Why Multiple Models?

The primary reason to reach for Codeband over running one coding agent with subagents is **adversarial cross-model review**. Same-model LLM judging has a well-documented self-preference bias — a model reviewing another instance of itself tends to rate its own style, phrasing, and approach more favorably. Cross-model judging (Claude reviews Codex, Codex reviews Claude) breaks that bias and catches a different class of mistakes: each model family has different blind spots inherited from its training, and pairing them adversarially surfaces problems that same-model review silently accepts.

Codeband enforces this at runtime:
- **Coder ↔ Code Reviewer**: a Claude coder's PR is routed to a Codex reviewer, and vice versa.
- **Planner ↔ Plan Reviewer**: a Claude-produced plan is validated by a Codex plan reviewer (and vice versa).

Parallelism is a secondary, weaker benefit — Claude Code and Codex both already do their own internal subagent / concurrent-tool-call parallelism, so "run two coders in parallel" doesn't buy much on its own. The coupling that matters is the adversarial pair.

## Overview

```
                    ┌──────────────┐
                    │     User     │
                    └──────┬───────┘
                           │ "Implement JWT auth"
                           ▼
                    ┌──────────────┐
                    │  Conductor   │  Coordinates workflow,
                    │  (singleton) │  allocates workers from pools
                    └──────┬───────┘
                           │ @mention
                           ▼
                ┌──────────────────┐       ┌──────────────────┐
                │ Planner-Claude-0 │──────▶│ Plan-Reviewer-   │  Cross-model
                │     (pool)       │       │   Codex-0 (pool) │  plan validation
                └──────────┬───────┘       └──────────────────┘
                           │ plan approved
                           ▼
                  ┌────────────────┐
                  │   Conductor    │  allocates coders; reviewers are
                  │                │  direct-dispatched from the roster
                  └──┬──────────┬──┘
                     │          │
        ┌────────────┘          └────────────┐
        ▼                                    ▼
  ┌──────────────┐                     ┌──────────────┐
  │Coder-Claude-0│                     │Coder-Codex-0 │
  │  (pool)      │                     │  (pool)      │
  └──────┬───────┘                     └──────┬───────┘
         │ PR                                 │ PR
         ▼ (cross-model pairing at dispatch)  ▼
  ┌──────────────┐                     ┌──────────────┐
  │Reviewer-     │  adversarial review │Reviewer-     │
  │Codex-0 (pool)│                     │Claude-0(pool)│
  └──────┬───────┘                     └──────┬───────┘
         │ review_passed                      │
         └────────────┬───────────────────────┘
                      │
          ┌───────────▼──────────┐  (when verifier configured)
          │  Verifier-Codex-0    │  evidence-integrity acceptance gate
          │      (pool)          │  cb-phase verify-acceptance
          └───────────┬──────────┘
                      │ acceptance_passed
                      ▼
               ┌─────────────┐              ┌──────────┐
               │ Mergemaster │              │ Watchdog │  Monitors health,
               │ (singleton) │              │          │  nudges stuck agents
               └──────┬──────┘              └──────────┘
                      │ cb-phase merge (gated)
                      ▼
     ┌────────────────────────────────────────────────────┐
     │  State Store  (workspace/state/orchestration.db)    │
     │  SQLite WAL — tasks · subtask_states · transition_  │
     │  log · audit_log — both chains hash-linked          │
     └────────────────────────────────────────────────────┘

     ┌─────────────────────────────────────────┐
     │  Protocol State (blackboard)             │
     │  Band.ai memory on paid tier;            │
     │  local JSONL on free tier                │
     └─────────────────────────────────────────┘
```

## Agent Roles

**Singletons** (one instance per project):

| Role | Description | Framework |
|------|-------------|-----------|
| **Conductor** | Coordinator. Allocates workers from pools, routes tasks, sends lightweight notifications, monitors protocol state, and intervenes when protocols fail. Does NOT relay content — agents send content directly. | `claude_sdk` (default), `codex` |
| **Mergemaster** | Integration agent. Batch-merges coder branches with bisect-on-failure. Handles merge conflicts and test failures via protocols. | `claude_sdk` (default), `codex` |
| **Watchdog** | Health monitor. Detects stuck agents, sends nudges, escalates to Conductor. Not a Band.ai agent — in-process daemon reusing Conductor credentials. | Deterministic daemon (no LLM) |

**Pool roles** (N instances per framework, each instance identified as `<role>-<framework>-<index>`):

| Role | Description | Frameworks |
|------|-------------|------------|
| **Planner** | Task analyst. Reads the codebase, decomposes tasks into parallelizable subtasks with optional `framework_hint`, sends plans via chat. Emits abstract subtask specs — the Conductor binds specific coders at dispatch. | `claude_sdk` today; Codex planned |
| **Plan Reviewer** | Plan validation gate. Reviews plans before coders begin — decomposition quality, file conflict risk, acceptance criteria. Paired with Planner on the opposite framework. Read-only codebase access. | `claude_sdk`, `codex` |
| **Coder** | Coding worker. Executes subtasks in an isolated git worktree (`workspace/worktrees/<worker-id>/`). Auto-restarted by `WorkerSupervisor` on crash. | `claude_sdk`, `codex` |
| **Code Reviewer** | Code quality gate. Reviews PRs, posts findings as PR comments, assigns a risk level. Directly @mentioned by the Coder on the framework **opposite** the coder. | `claude_sdk`, `codex` |
| **Verifier** | Evidence-integrity acceptance gate. Runs after code review passes (`review_passed`) and before merge — the last SHA-pinned gate. Renders the `verify_acceptance` verdict via `cb-phase verify-acceptance --accept/--reject`. `--accept` → `acceptance_passed` (eligible for merge); `--reject` → `review_failed` (back to the review-round loop, including its cap). Runs in an isolated scratch directory with `gh` CLI access. Opposite-vendor pairing is the ideal (same benefit as cross-model code review) but a single-vendor Verifier degrades gracefully (`cb doctor` warns). Setting `agents.verifiers.count: 0` opts out — tasks then merge straight from `review_passed`. Implementation: `agents/verifier.py`. | `claude_sdk`, `codex` |

**Capacity is declared in yaml** under `agents.{planners, plan_reviewers, coders, reviewers, verifiers}`, with a `count` per framework. The Conductor allocates task owners and fallback routes. First-dispatch reviewer selection is direct: Coders and Planners use the Worker Pool Roster to @mention deterministic opposite-framework reviewers by display name.

## Communication: Three Channels

| Channel | Purpose | Examples |
|---------|---------|---------|
| **Chat** (Band.ai @mentions) | Content delivery + coordination | Full plans, review verdicts, conflict details, task assignments |
| **Memory** (Band.ai API *or* local JSONL) | Protocol state tracking | `review round 1 findings posted`, `conflict resolved` |
| **GitHub** (PR comments) | Code review artifacts | Detailed review findings, test failure logs |

**Principle: Chat carries content. Memory tracks protocol state. GitHub stores code review artifacts.** The Conductor routes notifications but does not relay content — agents send content directly via chat and PR comments.

```
task room (all agents)
  Planner    → Conductor + Plan Reviewer    full plan via chat (direct, no forwarding)
  Conductor  → Coder                        task assignment (with allocated branch name)
  Coder      → Conductor + Code Reviewer    PR URL + framework tag (direct review dispatch)
  Code Rev.  → GitHub PR                    detailed findings via gh pr comment
  Code Rev.  → Conductor                    verdict (PASSED/FAILED + risk level)
  Mergemaster → chat                        conflict/failure details
  Watchdog monitors all                     via REST API polling

protocol state (memory — Band.ai or local JSONL)
  Each protocol step stores: type, correlation ID, PR, round, state, from, to
  Memory content limit: 1000 chars — never store full plans, reviews, or logs
```

## Protocols

Agents don't just receive tasks and report results — they **interact with each other**. The Code Reviewer discusses code quality with the Coder. The Coder asks the Planner for clarification. The Mergemaster coordinates conflict resolution with the Coder who wrote the code. These interactions happen through **protocols** — structured patterns that define how agents collaborate.

A protocol specifies which agents participate, what content they exchange, and through which channel. The Conductor monitors each protocol's progress via memory state envelopes, stepping in if an interaction stalls — but it doesn't impose hard limits on how many exchanges agents can have. Agents iterate until the work is done.

| Protocol | Agents | Typical Flow | Content Channel |
|----------|--------|-------------|-----------------|
| **Code Review** | Code Reviewer ↔ Coder | Findings + risk classification → fix → re-review | GitHub PR comments |
| **Clarification** | Any agent → Planner | Question → answer | Chat messages |
| **Merge Conflict** | Mergemaster → Coder | Conflict details → rebase → retry | Chat + PR comments |
| **Test Failure** | Mergemaster → Coder | Failure details → fix → retry | Chat + PR comments |
| **Plan Revision** | Coder → Planner | Issue report → revised plan | Chat messages |

Each protocol follows the same pattern:
1. Producing agent sends **full content** via chat or `gh pr comment`, @mentioning the next agent
2. Producing agent stores a **state envelope** in memory (protocol type, correlation ID, PR, round, state, from, to)
3. Next agent reads content from the chat message or PR comments, acts, stores updated state
4. If the interaction stalls, the Conductor intervenes

### Example — Code Review Protocol (with cross-model allocation)

```
Coder-Claude-0 → Conductor + Reviewer-Codex-0 (chat): "PR #42 ready: <url>. Framework: claude_sdk."
Reviewer-Codex-0 reads PR via gh pr diff --repo → posts findings via gh pr comment
Reviewer-Codex-0 stores state: "protocol code_review cid cr_42_r1 pr 42 round 1 state findings_posted risk medium from reviewer-codex-0 to coder-claude_sdk-0"
Reviewer-Codex-0 → Conductor (chat): "Review FAILED for PR #42 (risk: medium): 3 critical findings"
Conductor → Coder-Claude-0 (chat): "Review failed for PR #42 — check PR comments and fix"
Coder-Claude-0 reads findings → fixes → pushes
Coder-Claude-0 → Conductor (chat): "Addressed review for PR #42"
Conductor → Reviewer-Codex-0 (chat): "Coder pushed fixes for PR #42 — please re-review"
```

## Worker Pool + Allocation

The **worker pool** tracks capacity declared in `codeband.yaml` and provides the intended deterministic allocation model. Implementation: `src/codeband/workers/pool.py` — a `WorkerPool` with `acquire(role, framework)`, `release(worker_id)`, and `pair_for_task(coder_role, coder_framework)` which atomically reserves a coder and an opposite-framework reviewer.

Worker identities are `{role}-{framework}-{index}` strings. Band.ai display names are the title-cased version (`Coder-Claude-0`).

Allocation is prompt-enforced in the current MVP. The Worker Pool Roster is appended to the Planner, Conductor, and Coder prompts with concrete display names. Coders and Planners use deterministic worker-index pairing for first dispatch, while the Conductor handles task assignment, re-review routing, and malformed-message fallback. Code-backed arbitration via `WorkerPool` is on the roadmap.

For collision-free parallel review, provision at least one opposite-framework reviewer for each coder in the paired pool:

| Coder capacity | Reviewer capacity needed |
|----------------|--------------------------|
| `coders.claude_sdk=N` | `reviewers.codex>=N` |
| `coders.codex=N` | `reviewers.claude_sdk>=N` |

Use the same pattern for planners and plan reviewers when scaling planning throughput.

## Memory Model

Memory stores **protocol state** and **repo knowledge** — not full content (1000-char limit).

| Use Case | System | Type | Segment | Scope |
|----------|--------|------|---------|-------|
| Protocol state envelopes | `working` | `episodic` | `agent` | `organization` |
| Repo knowledge (test commands, build quirks) | `long_term` | `procedural` | `tool` | `organization` |
| Merge decisions | `long_term` | `episodic` | `agent` | `organization` |

State envelopes use search-safe alphanumeric tokens (e.g., `protocol code_review cid cr_42_r1 pr 42 round 1 state findings_posted`) for server-side filtering via `content_query`.

On **paid Band.ai**, these calls hit the Band.ai memory REST API. On **free tier**, Codeband monkey-patches the SDK's `AgentTools.{store,list,archive}_memory` at startup to delegate to a local JSONL file at `workspace/state/memories.jsonl` (with `fcntl.flock` for concurrent writes). Agents see the same tool names — no prompt changes. Trade-off: free-tier is single-machine only (the local file isn't shared across hosts).

The selection happens once per process in `codeband/orchestration/runner.py:_install_memory_backend()`, which calls `codeband/memory/probe.probe_memory_backend()`. The result is cached for the life of the process. `BAND_MEMORY_MODE=band|local` env var and `band.memory_mode` in `codeband.yaml` both skip the probe.

## Workspace Isolation

Each worker has a directory under `workspace/worktrees/` keyed by its worker ID:

```
/workspace/
    repo.git/                                  # bare clone (shared in local, independent in distributed)
    worktrees/
        planner-claude_sdk-0/                  # detached HEAD (read-only)
        plan_reviewer-codex-0/                 # detached HEAD (read-only)
        coder-claude_sdk-0/                    # workspace branch: codeband/coder-claude_sdk-0/workspace
        coder-codex-0/                         # workspace branch: codeband/coder-codex-0/workspace
        mergemaster/                           # branch: main
    scratch/
        reviewer-claude_sdk-0/                 # scratch dir for gh calls (no repo)
        reviewer-codex-0/
    state/                                     # persistent state files
    state/memories.jsonl                       # local memory store (free-tier Band.ai)
```

Each coder has a persistent **workspace branch** (`codeband/coder-<framework>-<N>/workspace`). For each task, the coder creates a **task branch** from it (`codeband/coder-claude_sdk-0/add-auth`), implements, pushes, and opens a PR. Between tasks, the workspace branch is reset to `origin/main`.

**Local mode** — all workers share a bare clone. **Distributed mode** — each worker clones independently and syncs via `git push`/`git fetch`. Either way, coders work in parallel without interference.

## Task Flow (end to end)

1. **User sends task** → Conductor receives it in the task room
2. **Conductor picks an idle Planner** from the pool and @mentions it
3. **Planner** → analyzes codebase, emits abstract subtasks (with optional `framework_hint`), sends plan via chat
4. **Planner** → @mentions Conductor + an opposite-framework Plan Reviewer
5. **Plan Reviewer** → validates decomposition, file conflicts, acceptance criteria
6. **Plan Reviewer approves** → Conductor allocates coders from the pool per subtask (matching `framework_hint` if set, else any idle coder)
7. **Coders work in parallel** — each in its own worktree, branch `codeband/<coder-id>/<slug>`; Coder runs `cb-phase start <subtask_id>` at pickup (seeds `in_progress` in the state store)
8. **Coder reports completion** → runs `cb-phase verify <subtask_id> --pr <n>` (gates: clean tree, PR open, optional test command, HEAD matches PR head); on success the subtask advances to `review_pending`; @mentions Conductor and a cross-model Reviewer with the PR URL
9. **Code Reviewer starts from the Coder's direct @mention**; Conductor only performs fallback dispatch if the Coder omitted a Reviewer
10. **Code Reviewer** → reads PR, posts findings as PR comments, runs `cb-phase review --approve/--reject <subtask_id>`, reporting `review_passed` or `review_failed`
11. **If review fails** → Code Review Protocol iterates (same reviewer stays) until resolved or the review-round cap is hit (→ `blocked`, owner escalation)
12. **Verifier** (when configured) → on `review_passed` runs `cb-phase verify-acceptance --accept/--reject <subtask_id>`. `--accept` records `acceptance_passed`; `--reject` sends the subtask back to `review_failed`
13. **Mergemaster** → runs `cb-phase merge <subtask_id>` which: checks SHA-pinned merge eligibility (all required verdicts pinned to the same HEAD), requests approval from the task owner via chat, and — once granted via `cb approve <pr>` — executes `gh pr merge --match-head-commit <sha>`; batch merge with bisect-on-failure
14. **State store** → task promoted to `completed` once every subtask reaches `merged`
15. **Conductor** → final status to user

## State Layer

Durable orchestration state lives in a single SQLite database at `workspace/state/orchestration.db` (`state/store.py`). All processes that share a workspace — the local in-process runner and the distributed `agent_main` path — read and write this same file. It uses WAL mode with short atomic transactions and a busy timeout so concurrent access never corrupts.

**Four tables:**

| Table | Purpose |
|-------|---------|
| `tasks` | One row per task (keyed by `room_id`). Carries `status` (`active`/`superseded`/`completed`), the task `owner_id` (for watchdog escalation), a snapshotted `required_verdicts` list, and a snapshotted `merge_approval` policy. All fields are resolved at registration time and frozen — a mid-task config edit cannot change an in-flight task. |
| `subtask_states` | One row per `(task_id, subtask_id)`. Carries the current FSM state, assigned worker, PR number, and durable counters: `review_round`, `verify_attempts`, `rebase_rounds`, and the SHA-pinned merge-approval grant columns. |
| `transition_log` | Append-only audit of every FSM state transition. Each row is hash-chained: `row_hash = SHA-256(business_columns + prev_hash)`, so an after-the-fact edit of any business column breaks the chain at exactly that row. `cb verify-log` and the Watchdog's integrity rung detect breaks. |
| `audit_log` | Append-only record of non-transition effects: approval grants, approval-request markers, `pr_number` bindings, `ungated_external_merge` events. Separate from `transition_log` so the Watchdog's transition patrols keep "FSM transitions only" semantics. Own hash chain (same scheme). |

**Hash chains are tamper-evident, not tamper-proof** — a process with write access to the DB file can recompute the chain. The adversary line is raw DB writes by a motivated actor; the chains catch accidental corruption and after-the-fact edits by ordinary callers.

### FSM (`state/fsm.py`)

Every subtask advances through a fixed lifecycle:

```
planned → assigned → in_progress → verify_pending → review_pending
        → review_passed → [acceptance_passed →] merge_pending → merged
        ↘ review_failed → in_progress (or → blocked at cap)
        ↘ needs_rebase → in_progress (or → blocked at rebase cap)
        ↘ blocked → in_progress (conductor resume)
        ↘ abandoned (conductor, any state)
```

`acceptance_passed` is only reached when a Verifier is configured — the merge-eligibility gate decides which path a task's snapshotted `required_verdicts` requires.

`fsm.transition()` is the **only mutation path**. It opens a `BEGIN EXCLUSIVE` transaction, validates `(current_state, caller_role)` against a static table (`VALID_TRANSITIONS`), enforces three runtime caps in the same transaction, then writes the new state and appends a hash-chained `transition_log` row. Illegal edges or wrong caller roles raise `InvalidTransitionError` and write nothing.

**Runtime caps enforced inside the transition:**

| Cap | Default | What it bounds |
|-----|---------|----------------|
| `max_review_rounds` | 3 | `review_failed → in_progress` rework cycles — bounds a productive loop the Watchdog's stall cap never catches (each round commits real code) |
| `max_rebase_rounds` | 3 | `needs_rebase` entries — bounds the merge-gate send-back loop (each round writes fresh transition rows, so the Watchdog stall cap doesn't fire) |
| `max_verify_attempts` | (config) | `cb-phase verify` rejections — bounds the verify-gate loop (coder commits real code each attempt, HEAD advances) |

The **merge-eligibility gate** fires inside every `→ merge_pending` transition: every verdict leg in the task's snapshotted `required_verdicts` must have a `transition_log` row pinned to exactly the `head_sha` being merged (`verify` → `review_pending` transition, `review` → `review_passed`, `verify_acceptance` → `acceptance_passed`). An ineligible attempt raises `MergeNotEligibleError` with machine-readable reason tags and writes nothing. Because `transition()` is the only path into `merge_pending`, no caller can bypass this check.

### Task registration (`state/registration.py`)

`register_task()` is the single writer of "a task exists". It:
1. Resolves and validates `required_verdicts` and `merge_approval` from the current `AgentsConfig`
2. Applies supersede + insert/update in one atomic transaction
3. Writes the active-room pointer file (`workspace/state/.codeband_room`) **strictly after** the DB commit — row-first, because a row-without-pointer is recoverable by re-running registration; a pointer-without-row is a dead end for `cb-phase`

Both `cb task` (kickoff) and `cb register-task` (manual re-seed) call `register_task()`; nothing else may write the pointer file or a `tasks` row.

### Rehydration (`state/rehydration.py`)

On every agent reconnect, `recover_for_reconnect()` opens the state store and builds a per-role markdown recovery prompt prepended to the agent's system prompt. Per-role content: Conductor gets all non-terminal subtasks; Mergemaster gets `merge_pending/review_passed/acceptance_passed/needs_rebase`; Code Reviewer gets `review_pending`; Verifier gets `review_passed`; Planner/Plan Reviewer get active task descriptions. `None` means nothing relevant in durable state — agent reconnects normally.

## CLI Layer (`cb` and `cb-phase`)

### `cli/__init__.py` — Main CLI

The `cb` command group (Click). Handles auth resolution at startup (`_resolve_claude_auth` / `_resolve_codex_auth`), `--dir` project routing, and `.env` loading. Subcommands include `cb run`, `cb task`, `cb register-task`, `cb approve`, `cb reject`, `cb prs`, `cb issues`, `cb log`, `cb feed`, `cb doctor`, `cb init`, and the bare `cb` path that opens the interactive shell.

### `cli/handoff.py` — `cb-phase`

The **enforcement seam** for coding agents. All phase advances happen by shelling out to `cb-phase`; the effect occurs only if every gate passes regardless of what the Conductor intended.

**Commands:**

| Command | Role | What it does |
|---------|------|--------------|
| `cb-phase start <subtask_id>` | `coder` | Seeds `in_progress` in the state store at pickup |
| `cb-phase verify <subtask_id> --pr <n>` | `coder` | Gate sequence: verify-attempt cap check → clean tree → PR open + branch matches → optional test command → HEAD = PR head → advances to `review_pending`; failure increments durable `verify_attempts` |
| `cb-phase review --approve/--reject <subtask_id>` | `reviewer` | Advances to `review_passed` or `review_failed`; SHA-pinned to worktree HEAD |
| `cb-phase verify-acceptance --accept/--reject <subtask_id>` | `verifier` | Advances to `acceptance_passed` or `review_failed`; runs hash-chain integrity check before accepting (broken chain → rejected) |
| `cb-phase abandon <subtask_id>` | `conductor` | Drives the `(any, conductor) → abandoned` FSM wildcard |
| `cb-phase resume <subtask_id>` | `conductor` | Drives `blocked → in_progress`; preserves all durable counters (not a cap reset) |

The `task_id` is always resolved from the active-room pointer (`workspace/state/.codeband_room`), never from the command line — agents pass a semantic label on `--task` for readability only.

### `cli/merge.py` — `cb-phase merge`

The **only sanctioned merge path**. Agents request a merge by running `cb-phase merge <subtask_id>`; this code executes it behind the FSM's SHA-pinned eligibility gate and a durable, SHA-pinned approval grant.

Invocation flow (fail-closed):

1. Resolve task, subtask, PR number (persisted on first call; a `--pr` that disagrees with the stored binding is rejected)
2. **Reconcile first**: if subtask is already `merge_pending` and PR is already `MERGED`, record `merged` only if the grant's SHA matches the merged head (crash recovery). Without a matching grant → `blocked` + `ungated_external_merge` audit event
3. From `review_passed` / `acceptance_passed`: attempt `→ merge_pending` at PR head SHA (eligibility gate runs inside the FSM transition)
4. **Execution-time SHA re-check**: PR head must still equal the `merge_pending` SHA; a push → `needs_rebase`
5. **Approval**: `cb approve <pr>` writes a SHA-pinned grant; `cb-phase merge` proceeds only when grant SHA matches `merge_pending` SHA. If not yet granted, sends approval request to the task owner (once per `merge_pending` SHA) and exits 0 to wait
6. **Mergeability pre-check**: conflicting PR → `needs_rebase`
7. **Execute** `gh pr merge --merge --match-head-commit <pending_sha>` (SHA-pinned to prevent merging unverified code if branch moves between snapshot and execution)
8. On success: record `merged` transition (task auto-promoted to `completed` if last subtask); delete remote branch best-effort

## Shell (Interactive REPL)

Bare `cb` (no subcommand, TTY) opens a single-terminal session: `shell/repl.py` launches three concurrent asyncio tasks — the in-process orchestrator (local mode), the live feed poller, and a `prompt_toolkit` REPL. `patch_stdout` renders feed output and agent debug logs above the prompt without disturbing the input line.

Slash commands are dispatched from `shell/commands.py` via a `REGISTRY` of handlers. Available commands include `/log`, `/diff`, `/task`, `/usage`, `/down`, `/quit`. Handlers delegate to Click command callbacks or to a `FSBackend` (`shell/fs.py`) abstraction that works identically in local and distributed mode. `shell/render.py` provides shared terminal rendering helpers.

## Monitoring

`monitoring/activity_log.py` — append-only JSONL log at `workspace/state/activity.log`. Three event types: `LLM_USAGE` (cost/latency per LLM call), `cli_invocation` (argv + cwd + pid + env markers at start of every `cb`/`cb-phase` command), `cli_completion` (exit code). The `cli_invocation`/`cli_completion` pair is the **attribution record**: the row-5 forensics question ("which process ran `cb-phase verify`?") is answerable for the entire sanctioned CLI surface. Actions outside the CLI (raw `sqlite3`, shell `git`/`gh`) leave no row — by design (detection over prevention). Failures to write the log never break the command.

`monitoring/feed.py` — live terminal stream. Polls Band.ai's human API, resolves `@[[uuid]]` mention tokens to display names, and colorizes output by role. Used by the interactive shell's feed task and by `cb feed` (one-shot).

`monitoring/usage.py` — token usage aggregation. `SDKUsageHandler` parses SDK log lines into `LLM_USAGE` activity events; `UsageSummary` aggregates them. Surfaced via `/usage` in the shell and `cb log --type LLM_USAGE`.

## Anti-Loop Discipline

- **@mentioning = function call** — only @mention when you need action
- **After handoff, go silent** — no "standing by" messages
- **Structured protocols** — agent interactions follow defined patterns so exchanges are productive, not open-ended
- **Two-level intervention** — the Conductor uses judgment to intervene early; a hard safety limit of 5 rounds guarantees termination regardless
- **Humans are the default audience** for status updates

## Merge Strategy (Bors-style)

The Mergemaster follows a batch-then-bisect algorithm:

1. Collect pending merge requests into a batch (already passed code review)
2. Merge all into a temporary integration branch, run tests
3. Tests pass → fast-forward main
4. Tests fail → binary bisect to find the breaking branch, report via Test Failure Protocol

The Planner minimizes conflicts by spreading subtasks across non-overlapping files. When conflicts occur, the conflicting branch is removed from the batch and reported to the Conductor for rebasing.

> **Note:** Pool allocation and protocol tracking remain **prompt-enforced** — agents follow roster and protocol instructions from their system prompts. Merge gating (eligibility, approval, SHA-pinning) and FSM state transitions are **code-enforced** via the state layer and `cb-phase` CLI. See [Roadmap](README.md#roadmap) for planned code-backed pool allocation.

## Session Recovery

Coders run under a `WorkerSupervisor` (`session/supervisor.py`) that auto-restarts on crash. Worker identity is persisted as JSON in `workspace/state/<worker-id>.json` (`session/identity.py`). On restart, `session/context.py` rebuilds context from git log + uncommitted changes + `TASK.md`. `restart_delay_seconds` on each coder pool entry controls the base delay; repeated identical failures back off up to 60 seconds.

Unsupervised agents (Conductor, Mergemaster, Planners, Plan Reviewers, Code Reviewers) also run under a reconnect-forever loop with exponential backoff. SIGINT/SIGTERM is the normal shutdown path.

## Design Influences

Codeband's core design principle — **chat carries content, memory tracks protocol state, the Conductor routes notifications but does not relay content** — avoids the "telephone game" problem that plagues hub-and-spoke coordinators, where content quality degrades as it passes through a central relay. Inspired by [Gastown](https://github.com/gastownhall/gastown), which pioneered this agent-interaction model using a custom communication stack (Dolt databases, Beads, mail/nudge systems, seance). Codeband takes the same ideas and reimplements them on top of Band.ai's chat + memory primitives, trading custom infrastructure for simplicity.

The adversarial cross-model review principle comes from the same playbook as human code review: reviewer ≠ author catches more issues than self-review, and the effect is amplified when reviewer and author draw on different training distributions (different LLM model families).
