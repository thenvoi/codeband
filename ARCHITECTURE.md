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
         │ verdict                            │
         └────────────┬───────────────────────┘
                      ▼
               ┌─────────────┐              ┌──────────┐
               │ Mergemaster │              │ Watchdog │  Monitors health,
               │ (singleton) │              │          │  nudges stuck agents
               └─────────────┘              └──────────┘

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
| **Planner** | Task analyst. Reads the codebase, decomposes tasks into parallelizable subtasks with optional `framework_hint`, sends plans via chat. Emits abstract subtask specs — the Conductor binds specific coders at dispatch. | `claude_sdk`, `codex` |
| **Plan Reviewer** | Plan validation gate. Reviews plans before coders begin — decomposition quality, file conflict risk, acceptance criteria. Paired with Planner on the opposite framework. Read-only codebase access. | `claude_sdk`, `codex` |
| **Coder** | Coding worker. Executes subtasks in an isolated git worktree (`workspace/worktrees/<worker-id>/`). Auto-restarted by `WorkerSupervisor` on crash. | `claude_sdk`, `codex` |
| **Code Reviewer** | Code quality gate. Reviews PRs, posts findings as PR comments, assigns a risk level. Directly @mentioned by the Coder on the framework **opposite** the coder. | `claude_sdk`, `codex` |

**Capacity is declared in yaml** under `agents.{planners, plan_reviewers, coders, reviewers}`, with a `count` per framework. The Conductor allocates task owners and fallback routes. First-dispatch reviewer selection is direct: Coders and Planners use the Worker Pool Roster to @mention deterministic opposite-framework reviewers by display name.

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
7. **Coders work in parallel** — each in its own worktree, branch `codeband/<coder-id>/<slug>`
8. **Coder reports completion** → @mention Conductor and a cross-model Reviewer with PR URL + framework
9. **Code Reviewer starts from the Coder's direct @mention**; Conductor only performs fallback dispatch if the Coder omitted a Reviewer
10. **Code Reviewer** → reads PR, posts findings as PR comments, reports verdict
11. **If review fails** → Code Review Protocol iterates (same reviewer stays) until resolved
12. **Risk-based routing** → low-risk auto-merges; higher risk waits for `cb approve`
13. **Mergemaster** → batch merge with bisect-on-failure
14. **Conductor** → final status to user

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

> **Note:** Merge strategy, protocol tracking, risk-based routing, and pool allocation are currently **prompt-enforced** — the agent LLMs follow these instructions from their system prompts. They are not yet deterministic code. Appropriate for an MVP; see [Roadmap](README.md#roadmap).

## Session Recovery

Coders run under a `WorkerSupervisor` (`session/supervisor.py`) that auto-restarts on crash. Worker identity is persisted as JSON in `workspace/state/<worker-id>.json` (`session/identity.py`). On restart, `session/context.py` rebuilds context from git log + uncommitted changes + `TASK.md`. `restart_delay_seconds` on each coder pool entry controls the base delay; repeated identical failures back off up to 60 seconds.

Unsupervised agents (Conductor, Mergemaster, Planners, Plan Reviewers, Code Reviewers) also run under a reconnect-forever loop with exponential backoff. SIGINT/SIGTERM is the normal shutdown path.

## Design Influences

Codeband's core design principle — **chat carries content, memory tracks protocol state, the Conductor routes notifications but does not relay content** — avoids the "telephone game" problem that plagues hub-and-spoke coordinators, where content quality degrades as it passes through a central relay. Inspired by [Gastown](https://github.com/gastownhall/gastown), which pioneered this agent-interaction model using a custom communication stack (Dolt databases, Beads, mail/nudge systems, seance). Codeband takes the same ideas and reimplements them on top of Band.ai's chat + memory primitives, trading custom infrastructure for simplicity.

The adversarial cross-model review principle comes from the same playbook as human code review: reviewer ≠ author catches more issues than self-review, and the effect is amplified when reviewer and author draw on different training distributions (different LLM model families).
