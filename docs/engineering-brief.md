# Codeband, jam, and the `/codeband` command — engineering brief

> **Snapshot as of 2026-05-31.** Predates initiator-as-owner (PRs #23–25) and the Stage-2 merge gate. Accurate as a historical mechanism brief; refresh pending post-Stage-2.

**Audience:** CTO / engineering leadership (and the Claude Code agent reading on their behalf).
**Purpose:** A complete, mechanism-level account of (1) the `/codeband` Claude Code command and its `jam` integration, (2) the deterministic-orchestration hardening of codeband itself, (3) the onboarding skill built from it, and (4) the broader pattern library this is a first instance of.
**Date:** 2026-05-31.

> **Note for the reader's agent:** the deterministic-orchestration code (Part 2) lives in this repo under `src/codeband/state/`, `src/codeband/cli/handoff.py`, and `src/codeband/agents/watchdog.py`, with the design in `docs/rfc-deterministic-orchestration.md`. The `/codeband` command and `jam` integration (Part 1) live outside this repo, in the operator's `~/.claude/` (`commands/codeband.md`, `codeband/setup.sh`); the descriptions here are authoritative for those.

---

## Contents

- [TL;DR](#tldr)
- [Glossary](#glossary)
- [Part 1 — The `/codeband` command & the `jam` integration](#part-1)
- [Part 2 — Deterministic orchestration in codeband](#part-2)
- [Part 3 — The onboarding skill & distribution](#part-3)
- [Part 4 — The pattern library: protocoled patterns as skills](#part-4)

---

## TL;DR

We built and validated a working system in which **a single Claude Code session acts as the human-facing coordinator of an 8-agent autonomous coding swarm**, end to end, without the operator ever touching a chat UI. You type `/codeband <task>` inside any repo; Claude provisions itself an identity on the Band coordination platform, owns the task room, drives the swarm, watches GitHub and the process log out-of-band, and hands you back a reviewed PR.

Getting there required diverging from how the underlying tools (`jam`, `codeband`) are normally used, in several non-obvious ways that each cost real debugging. The two headline divergences:

1. **We replaced a missing native capability with a synthesized one.** The intended design pushes inbound messages into Claude's turn automatically (via a harness feature called `TeamCreate`). That feature isn't in our Claude Code build. Rather than abandon the approach, we **synthesized "push" using a polling `Monitor`** on the message bridge's inbox file — Claude gets woken on every new message as if it were native push.

2. **We are making codeband itself production-grade by separating two things it currently fuses:** *deciding* what to do (creative, dynamic — stays in the LLM) from *enforcing and recording* what is allowed to happen (mechanical — moves into code). This is a deterministic control plane — a state machine, durable storage, mechanical liveness signals, and universal crash-recovery — added **without** sacrificing the agent autonomy and parallelism that make codeband worth using. It is built, unit-tested, and currently **dormant by design**; the final "activation" flip is the next and riskiest step.

A third strand is **distribution and reuse**: `/codeband` is one concrete instance of a more general idea — *protocoled multi-agent patterns shipped as skills* — that we want to generalize well beyond Claude Code and well beyond coding.

---

## Glossary

- **Band** — a messaging/coordination platform where autonomous agents (and humans) live, addressed by handle, talking in rooms.
- **codeband** — a multi-agent coding orchestrator: a Conductor LLM decomposes a task, spins up a pool of coder/reviewer/merge agents (Claude *and* Codex), and they coordinate on Band to plan → code → review → merge a task into a PR.
- **jam** — a CLI that wires a Claude Code session onto Band *as its own agent*, so it can send/receive Band messages.
- **`/codeband`** — the slash command that glues these together so *your* Claude session becomes the swarm's coordinator and your single point of contact.

---

<a id="part-1"></a>

# Part 1 — The `/codeband` command & the `jam` integration

> **One-line:** `/codeband <task>` makes *this* Claude Code session the sole coordinator of an 8-agent codeband swarm running against the repo you're sitting in — and it does so by inserting Claude onto the Band platform as its own agent, owning the task room, and synthesizing the message-push and liveness signals that the stock tooling doesn't reliably provide.

The command lives at `~/.claude/commands/codeband.md` (a user-level slash command, surfaced as the `codeband` skill) with an idempotent bootstrap at `~/.claude/codeband/setup.sh`.

## 1.1 The big picture

There is a **shared "codeband home"** at `~/projects/codeband` that holds the API keys (`.env`) and the 8 Band-registered agents (`agent_config.yaml`). Each run re-points that home at the current repo's `origin` and runs from there. Because the home is shared, **only one swarm runs at a time** — switching repos wipes the prior workspace.

The interesting engineering is *how a Claude Code session inserts itself as the coordinator* of a swarm it didn't create. The flow, end to end:

1. **Claude comes online as its own Band peer.** `jam onboard --team codeband-<slug>` provisions an ephemeral Band agent (`<owner>/claude-<repo>-<hex>`) with its own agent API key and starts a background "sockpuppet" bridge daemon that receives Band messages in real time and writes them to a local inbox file.
2. **Claude creates the task room with its own agent key** and adds the 8 codeband agents — so Claude is `task.owner` and the Conductor reports back to *it*. (Our first assumption was that the *user* key had to own the room; that was wrong — see §1.4.)
3. **Claude seeds the task** to the Conductor, then **arms three persistent `Monitor`s** that auto-wake it (inbox / PR / liveness — §1.3).
4. **Claude coordinates**: reads inbound via the inbox file, replies with `jam reply`, approves PRs by posting approval text into the room, and relays concise summaries to you in natural language. You never touch the Band UI.

## 1.2 How Claude inserts itself as coordinator (the mechanics)

### Bootstrap gate (Step 0)

Before anything, the command checks whether the stack is already provisioned:

```bash
CB_HOME="$HOME/projects/codeband"
if command -v codeband >/dev/null && command -v jam >/dev/null \
   && [ -f "$CB_HOME/.env" ] && [ -f "$CB_HOME/agent_config.yaml" ] \
   && jam whoami >/dev/null 2>&1; then echo "SETUP-OK"; else echo "SETUP-NEEDED"; fi
```

`SETUP-NEEDED` triggers `setup.sh` (see Part 3 for the full bootstrap). `SETUP-OK` goes straight to the swarm.

### Re-point the home at the current repo (Steps 1–2)

Claude reads the current repo's `origin` (`git remote get-url origin`) and branch, compares it to the `url:` already in `codeband.yaml`, and if different **wipes and re-points**: kills any running pid, `codeband reset`, removes `.codeband/repo.git`, worktrees, state files and scratch, then patches `url:`/`branch:` in `codeband.yaml`. It derives a stable slug from the repo URL and builds:

- `TEAM="codeband-<slug>"`
- `INBOX="$HOME/.claude/teams/$TEAM/inboxes/team-lead.json"` ← **the bridge inbox file the inbox Monitor watches.**

> **Note:** the swarm clones the repo's **`origin`** — it works on what's *pushed*, not your local uncommitted edits. If there's no `origin`, it falls back to the local committed state.

### Come online as an ephemeral Band peer (Step 3)

```bash
cd "$TARGET_DIR"
if jam daemon status 2>/dev/null | grep -q '^Running'; then echo "bridge already running"
else jam onboard --team "$TEAM" >/dev/null 2>&1; fi
```

`jam onboard` provisions the identity `<owner>/claude-<repo>-<hex>`, gives it its own agent API key, and starts the sockpuppet bridge. (The `grep -q '^Running'` is deliberate — see the gotcha in §1.4.)

### Start the swarm in the background (Steps 4–5)

```bash
cd "$CB_HOME" && mkdir -p .ensemble && nohup codeband run > .ensemble/run.log 2>&1 & echo $! > .ensemble/run.pid
```

Claude then polls `.ensemble/run.log` for ~40s; on repeated `429`/preflight/auth/clone errors it kills the run and does **not** seed the task.

### Create the room as itself, add the 8 agents, seed the task (Step 6)

This is the core trick, and it bypasses jam (see §1.4). An inline Python heredoc, run with codeband's bundled interpreter, uses the `thenvoi_rest` SDK directly:

1. **Recover Claude's own agent key** by scanning `~/.config/jam/sessions/*/*.json` for the record whose `cwd` matches the target dir and pulling its `agent_api_key`.
2. Build an `AsyncRestClient(api_key=cc_key, ...)` — **Claude now acts as itself over the Band REST API.**
3. `create_agent_chat(...)` — **Claude owns the room.**
4. For each of the 8 codeband agents: `add_agent_chat_participant(...)`.
5. Post the seed message `@`-mentioning the Conductor: *"here's a new task for the team. Please send it to the Planner … Report progress, questions, and PR-approval requests back to me in this room. Task: … Repository: …"*
6. Persist the room id to `.codeband_room`.

Because Claude created the room with its own key, it is `task.owner`, and the Conductor `@`-mentions *Claude* for reports — which is exactly why the inbox Monitor (next section) is the delivery channel.

## 1.3 The three Monitors (in exact detail)

The most important architectural decision in the command is that **Claude is woken by three independent, persistent `Monitor`s** rather than by a single chat stream. This is defense in depth: the in-band chat channel (Conductor reports) is unreliable, so two of the three monitors get their truth from *out of band* (GitHub and the process log).

### Monitor 1 — Inbox (synthesized message push)

Watches the bridge inbox file `~/.claude/teams/<team>/inboxes/team-lead.json`, polling every **2 seconds**. It pre-seeds the set of already-seen `message_id`s so it only fires on genuinely new messages, then:

```python
for m in json.load(open(INBOX)):
    mid = m['band']['message_id']
    if mid not in seen:
        seen.add(mid)
        print('NEW BAND MSG ' + mid + ': ' + (m.get('summary') or '')[:240], flush=True)
```

Each `NEW BAND MSG …` line auto-wakes Claude, which reads the message and replies via `jam reply`. **This is the synthesized replacement for native push** (see §1.4, #2).

### Monitor 2 — PR watcher (GitHub-backed, authoritative)

Watches `codeband pending` (which is backed by GitHub), polling every **25 seconds**, and fires on any change to the filtered PR lines:

```bash
cur="$(codeband pending --dir . 2>/dev/null | grep -E '#[0-9]+|http' | tr -s ' ')"
if [ -n "$cur" ] && [ "$cur" != "$prev" ]; then echo "PR STATUS:"; echo "$cur"; prev="$cur"; fi
```

This exists because **the Conductor is an unreliable reporter** (§1.4, #4): it routes the coder's "PR ready" to a reviewer/mergemaster and often never loops the owner in — and with `auto_merge` it may merge without asking. We do not trust chat for PR awareness; we poll GitHub.

### Monitor 3 — Liveness (silent-stall detector)

Tails `.ensemble/run.log` every **30 seconds**, watching two things:

**(a) Error signatures** — a count of lines matching:

```
timed out | Failed to mark message | crashed | Traceback | 429 | too many requests | preflight fail | unauthorized
```

When the count rises, it prints `SWARM ERROR SIGNAL:` plus the new matching lines.

**(b) Flat-line detection** — it counts "real" log lines (inverse-grepping out watchdog noise: `no longer exists | Watchdog | [WATCHDOG]`). If that count is unchanged for **12 consecutive 30-second patrols (~6 minutes)**, it prints `SWARM STALL: no real log progress for ~6m …`.

This is the guard for the *silent* death we actually watched happen: a Codex turn timed out and a `422 Failed to mark message` stalled that agent's Band cursor — no message, no PR, no error chat. The chat-recency machinery never noticed. The flat-line detector would.

> **Why three, and why two are out-of-band:** chat is necessary (the swarm talks on it) but not sufficient (it lies about PR status and goes silent on stalls). GitHub is the source of truth for the deliverable; the process log is the source of truth for liveness. Triangulating across all three is what makes the coordinator trustworthy.

## 1.4 Hard-won fixes & workarounds (the non-obvious engineering)

Each of these cost real debugging and is non-obvious enough to call out explicitly.

### 1. Claude *can* own the room — the "agent not in peer network" wall was a `jam` bug

We initially hit a wall building the room: `jam chat new --with @handle` and `jam agent list` reported codeband's agents as "not in peer network." Root cause: **jam's peer resolver only reads the first page (~20) of peers** and silently drops the rest, so the extra codeband agents fell off the page. The Band agent API itself pages fine. **Fix:** bypass jam's room-building CLI entirely and create the room + add participants via the `thenvoi_rest` agent API directly (Step 6). *Rule baked into the command: never use `jam chat new --with` / `jam agent list` to build the room.*

### 2. No `TeamCreate` in this Claude build → synthesized push via Monitor

The intended band-peer design relies on a harness feature, `TeamCreate`, to register the session as a team member so the harness injects new inbox messages as native `<teammate-message>` blocks. **`TeamCreate` is absent from our Claude Code build**, so that injection never fires — the bridge writes the inbox file, but nothing delivers it into Claude's turn. **Fix:** the inbox Monitor (§1.3, Monitor 1) polls that same inbox file every 2s and prints a wake line per new message. From Claude's perspective it's push; we built the last hop ourselves. (The stock band-peer skill explicitly says: if `TeamCreate` isn't available, the skill won't work — *we made it work anyway.*)

### 3. `cb approve` is broken in this topology → approve via `jam reply`

`codeband approve` reads `.codeband_room` and posts as the **user** key — who is *not* a participant in Claude's room (Claude created it with its own *agent* key). So approvals fail. **Fix:** approve by posting the approval text into the room yourself, mirroring codeband's expected wording:

```bash
jam reply <recent Conductor msg_id> "APPROVED: Please merge PR #<N> — <link>. Reviewed and approved."
```

(Request changes with `"CHANGES REQUESTED on PR #<N>: <reasons>."`)

### 4. The Conductor is an unreliable reporter → GitHub-backed PR watcher

Covered above (§1.3, Monitor 2). The Conductor routes "PR ready" elsewhere and often never loops the owner in. We get PR truth from `cb pending`/GitHub, not chat.

### 5. Smaller gotchas that cost time

- **`jam daemon status` exits 0 even when not running** → we must `grep -q '^Running'` rather than trust the exit code.
- **`claude --worktree` can't nest** inside a running session → fleet/dev sessions must launch from a fresh terminal or via manual `git worktree add`.
- The swarm works on `origin` (pushed state), not local edits.

## 1.5 Where we deviate from stock `jam` (and why)

Stock `jam`, as used by the `band-peer` skill, wires a Claude session as a **passive, push-driven peer**: inbound messages arrive automatically as `<teammate-message>` blocks (via `TeamCreate`), rooms are built with jam's CLI conveniences (`jam chat new --with`), and Claude is a *participant*, not an owner. Our usage diverges on five axes — every divergence is a deliberate response to a concrete failure of the intended path:

| Axis | Stock `jam` / band-peer | Our `/codeband` usage | Why |
|------|------------------------|----------------------|-----|
| **Inbound delivery** | Native `<teammate-message>` push via `TeamCreate` | Synthesized push: a `Monitor` polls the inbox file every 2s | `TeamCreate` absent from our CC build |
| **Room construction** | `jam chat new --with` / `jam agent list` | Direct `thenvoi_rest` agent API (`create_agent_chat`, `add_agent_chat_participant`) | jam's resolver only pages ~20 peers (a bug) |
| **Identity** | jam abstracts the agent key away | We scrape Claude's own `agent_api_key` from `~/.config/jam/sessions/*.json` and act as that agent over REST | needed to own the room as an agent |
| **Role** | Claude is a participant/peer | Claude is the room **owner** (`task.owner`) | so the Conductor reports back to Claude |
| **Source of truth** | the Band message stream | message stream **+ GitHub (`cb pending`) + process log (`run.log`)** | chat is unreliable for PR status and silent on stalls |

**Net:** jam supplies the parts it's reliable at — identity provisioning (`jam onboard`), the bridge daemon, and the message read/reply primitives (`jam inbox`, `jam reply`, `jam ack`). Everything jam is *unreliable* at in this topology — push delivery, room construction, and authoritative state — we route around it (the agent API, our own Monitors, GitHub, the process log).

`jam` subcommands we rely on: `init`, `whoami`, `onboard`, `daemon status/stop`, `inbox`, `reply`, `ack`. We avoid: `chat new --with` / `agent list` (paging bug), `cb feed` (blocks/streams), `cb approve` (wrong key).

## 1.6 Using it & tearing it down

- **Run:** from inside any git repo, `/codeband Add a dark-mode toggle and open a PR`. First run bootstraps the stack; later runs go straight to the swarm.
- **Stop:** `kill $(cat ~/projects/codeband/.ensemble/run.pid)` + `jam daemon stop`.

**File locations:** command `~/.claude/commands/codeband.md`; bootstrap `~/.claude/codeband/setup.sh`; runtime home `~/projects/codeband`; bridge inbox `~/.claude/teams/codeband-<slug>/inboxes/team-lead.json`; jam session store `~/.config/jam/sessions/*/*.json`.

---

<a id="part-2"></a>

# Part 2 — Deterministic orchestration in codeband

> **One-line:** We are making codeband production-grade by adding a deterministic, code-enforced control plane — a state machine, durable storage, mechanical liveness signals, and universal crash-recovery — *without* sacrificing the agent autonomy and parallelism that make it worth using. The guiding principle: **the LLM decides; code enforces and remembers.**

Full design: `docs/rfc-deterministic-orchestration.md` (this repo). The implementation lives under `src/codeband/state/`, `src/codeband/cli/handoff.py`, and `src/codeband/agents/watchdog.py`.

## 2.1 The problem: codeband today is overwhelmingly LLM-driven, with a thin deterministic scaffold

Three concrete fragilities, each observed in practice:

- **There is no state machine in code.** "State" is free-text *protocol envelopes* (e.g. `protocol code_review pr 42 round 1 state findings_posted`) that the **Conductor LLM** writes into memory and re-reads. Python parses those strings *only to render `cb status`* — nothing reads them to *drive* or *gate* a transition. So nothing in code prevents a skipped review, a double-merge, an out-of-order move, or an infinite review loop. The "pipeline" is prose in `prompts/conductor.md`, enforced only by the LLM following instructions.
- **The watchdog was in-memory and judged liveness by chat recency.** Health state lived in memory (lost on restart) and "stuck" was inferred purely from how recently an agent posted a chat message. That **false-positives an agent doing long silent work**, can't tell "progressing" from "looping," and has no cycle cap.
- **Only coders rehydrated after a crash.** Coders rebuilt context from git + `TASK.md`; every other agent (Conductor, Mergemaster, Planner, reviewers) reconnected **blank** and re-derived everything from the room. Pipeline position was never checkpointed anywhere durable.

**We watched all of this bite:** a planning run silently died when a Codex turn timed out (no retry boundary) and a `422` stalled that agent's Band cursor — the chat-recency watchdog never noticed, and nothing surfaced it. The deterministic layer is engineered to remove exactly these failure modes.

## 2.2 The principle and the trap

The trap is "make codeband deterministic" — that would kill the flexibility and parallel pools that make it good. The win is **separating two things codeband currently fuses:**

- *Deciding* what to do — creative, dynamic → **stays in the LLM.**
- *Enforcing and recording* what is allowed to happen — mechanical → **moves into code.**

> **The LLM decides, code enforces and remembers — the FSM gates EFFECTS (transitions/merges), not the Conductor's creative routing.**

## 2.3 The key adaptation: a two-level state model

`band-of-devs` (the Docker-based fork that inspired this) has one global pipeline, one phase at a time. codeband **fans out**: a planner decomposes a task into N subtasks, N coders work concurrently, reviews and merges interleave. So state is modeled at two levels:

| Level | Owner | Governs | Enforcement |
|-------|-------|---------|-------------|
| **Task** | LLM Conductor (loose) | decomposition, the assignment map, overall progress | **none** — the Conductor routes freely via `@mentions` |
| **Subtask / PR** | code FSM (rigid) | one unit-of-work lifecycle, instantiated N times; global invariants (no merge before approval, no double-merge, round caps) | `VALID_TRANSITIONS` + `BEGIN EXCLUSIVE` + the `cb-phase` gate |

Each unit of work is its own FSM *instance*, keyed by `(task_id, subtask_id, pr_number)`, with a round counter and an owner. Many run at once. Global invariants are code checks across the live instance set.

## 2.4 The five workstreams (mechanism level)

All new machinery lives under `src/codeband/state/`, plus `src/codeband/cli/handoff.py` and an extension to `agents/watchdog.py`. It landed in five additive phases, each a standalone, revertable PR.

### WS1 — Typed durable state store (`state/store.py`)

A single local **SQLite** DB at `{workspace}/state/orchestration.db` (stdlib `sqlite3` only). Three tables:

- `tasks(task_id PK, description, room_id, created_at, status)` — `task_id == room_id`.
- `subtask_states(subtask_id PK, task_id FK, state, assigned_worker, pr_number, created_at, updated_at, metadata, review_round)`.
- `transition_log(id PK, subtask_id, from_state, to_state, caller_role, timestamp, reason)` — append-only audit.

Each method opens a fresh short-lived connection inside a context-managed transaction with `PRAGMA journal_mode=WAL`, `busy_timeout=30000`, `foreign_keys=ON`. **Short-lived connections + WAL are what make it safe across processes** — both the single-process `run_local` path and the distributed `agent_main` path point at the same file. `TERMINAL_STATES = {merged, abandoned}`; note `blocked` is deliberately *non-terminal* so blocked subtasks still surface to the watchdog and rehydration.

The task row is written at kickoff (`orchestration/kickoff.py:send_task`, using `room_id` as `task_id`); subtask rows are created lazily by the FSM. **Both writes are fully guarded** with try/except + a "shadow mode" warning — a store failure can never break `cb run`.

> **Why SQLite, not Band memory** (a deliberate, load-bearing choice): Band memory has a hard ~1000-char content limit, is unavailable on the free tier (returns HTTP 402/403/404/501), is unavailable offline/in Docker, and supports only opaque substring queries. It is unfit to be the authoritative store. SQLite is local, unbounded, queryable, and behaves identically across run modes. Band memory is kept only as an optional async **observability mirror — never on the read path.**

### WS2 — Per-subtask FSM (`state/fsm.py`)

The state graph per subtask:

```
planned → assigned → in_progress → verify_pending → review_pending
        → review_passed → merge_pending → merged
                        ↘ review_failed → in_progress
                        ↘ blocked
                        ↘ abandoned
```

`VALID_TRANSITIONS` is a static `dict[(current_state, caller_role) → frozenset(next_states)]`. A move is legal only if both the edge **and the caller's role** match. Examples:

```python
("planned", "conductor"):         {"assigned"},
("assigned", "coder"):            {"in_progress"},
("in_progress", "coder"):         {"verify_pending", "blocked"},
("verify_pending", "coder"):      {"review_pending"},
("review_pending", "reviewer"):   {"review_passed", "review_failed"},
("review_failed", "coder"):       {"in_progress", "blocked"},
("review_passed", "mergemaster"): {"merge_pending"},
("merge_pending", "mergemaster"): {"merged"},
```

Two cross-cutting wildcards: `(any non-terminal, conductor) → abandoned` and `(any non-terminal, watchdog) → blocked`.

`transition(...)` is the only mutation path. It opens its own connection, runs `BEGIN EXCLUSIVE`, **re-reads state inside the exclusive transaction**, validates the edge + role, writes the new state, appends a `transition_log` row, and commits — or rolls back and raises `InvalidTransitionError` (writing nothing) on an illegal move. The exclusive transaction is what makes the global invariants (no double-merge, no out-of-order move) hold under concurrency.

**Review-round cap** (`MAX_REVIEW_ROUNDS = 3`): entering `review_failed` increments the durable `review_round` in the same transaction. Once `review_round >= 3`, a `review_failed → in_progress` rework is rejected; the only legal escape is `→ blocked`. This is a **distinct** mechanism from the watchdog's stall cap: it bounds a *productive-but-circular* loop (real commits each round, which the watchdog would never flag as stalled).

### WS3 — Verify-gated handoffs (`cli/handoff.py`, the `cb-phase` CLI)

This is the **enforcement seam**, and the cleverest piece of the design. In an `@mention`-driven world with two agent frameworks (Claude + Codex), you cannot enforce a gate by hoping the LLM behaves. So you make the *effect* go through a **validated CLI that both frameworks can shell out to**:

```
cb-phase verify <subtask_id> --task <task_id> --pr <n> [--worktree <path>]
```

Gate sequence (each failure → stderr + exit 1):

1. **clean tree** — `git status --porcelain` empty;
2. **PR open** — `gh pr view <n> --json state` == `OPEN`;
3. **optional verify command** — if configured (`agents.handoff_verify_command`), run it; **exit 0 required**;
4. **only then** → `transition(..., "review_pending", caller_role="coder")`.

The Conductor still routes via `@mentions`; the CLI gates the *consequences*. It imports **no Band SDK and no asyncio** — a fast, pure subprocess identical for Claude and Codex.

### WS4 — Mechanical watchdog (`agents/watchdog.py`)

The upgrade is **+284 lines, purely additive** — the entire old chat-recency machinery is retained as the first rungs of the escalation ladder; the mechanical stall→blocked path is the new third rung.

New `AgentHealthState` fields keyed by `subtask_id`: `patrol_visits_without_progress`, `last_git_head`, `last_transition_timestamp`. Each patrol, for each `in_progress`/`verify_pending` subtask, reads **three deterministic progress signals**:

- **git HEAD advanced?** — `git rev-parse <branch>` (branch from `subtask.metadata["branch"]`);
- **PR changed?** — `gh pr view <pr> --json state,updatedAt` (a change in `updatedAt` captures state changes too);
- **newer transition row?** — `SELECT MAX(timestamp) FROM transition_log WHERE subtask_id=?`.

If any advanced → progress: reset the counter. Otherwise increment it. When `patrol_visits_without_progress >= max_phase_visits` (default **10**) → escalate: transition the subtask to `blocked` via the FSM (`caller_role="watchdog"`) and post a chat alert to the room ("Conductor please reassign or investigate"). The whole path is wrapped so a store/git failure never breaks the patrol loop.

> **This is the path that would have caught the silent stall that motivated the whole RFC.** A timed-out turn produces no git-HEAD change and no new transition row, so the stall cap fires deterministically instead of the run dying quietly. Belongs out-of-process for crash isolation — which favors codeband's distributed/Docker mode.

### WS5 — Universal rehydration (`state/rehydration.py`)

`build_agent_recovery_context(agent_key, store) → str | None` reads the durable store and returns a **per-role markdown recovery prompt** that's prepended to a reconnecting agent's system prompt:

- **conductor** → a table of *all* non-terminal subtasks (`| Subtask | State | Worker | PR |`);
- **mergemaster** → subtasks in `merge_pending` / `review_passed`;
- **reviewer** → subtasks in `review_pending`;
- **planner** → the active task description(s);
- **plan_reviewer** → task descriptions + in-flight subtask counts.

Returns `None` when nothing relevant is in durable state (so behavior is then identical to today). It is called on **every reconnect for all non-coder agents** — the reconnect loop was refactored to build a fresh `make_agent(recovery_context)` each cycle. `recover_for_reconnect(...)` swallows *all* exceptions → `None`, so rehydration can never break the reconnect loop. The existing coder path (rebuild from git + `TASK.md`) is left untouched.

## 2.5 How we preserve flexibility & autonomy while adding gates

This is the part to emphasize: **the deterministic layer is surgical, not totalizing.**

**Gated (mechanical, in code):** advancing to `review_pending` (only via `cb-phase` after clean-tree + open-PR + verify-exit-0); passing/failing review *by the right role*; merge transitions; abandonment (conductor-only); blocking (watchdog-only); the review-round cap.

**NOT gated (creative, in the LLM):** the Conductor's routing, the decomposition into subtasks, `@mention` dispatch, re-planning, parallel pool sizing. The Conductor keeps full creative latitude over *what* to do and *who* does it.

The FSM never *silently* ignores a bad move — it raises `InvalidTransitionError` and exits non-zero, so the agent receives an **actionable error** it can recover from. Illegal *consequences* (a merge before approval, a double-merge, an out-of-order or skipped review, an unbounded loop) become impossible; everything else stays as flexible as it is today. Parallel pools, dynamic decomposition, and multi-framework agents all continue to work unchanged.

## 2.6 Where we deviate from stock codeband and from `band-of-devs`

### vs. stock codeband

| Dimension | Stock codeband | This work |
|-----------|----------------|-----------|
| **Pipeline state** | free-text protocol envelopes parsed only for display | typed SQLite FSM that *gates* effects |
| **Authoritative store** | Band memory (1000-char cap, free-tier 402/403, no offline) | local SQLite; Band memory demoted to optional observability mirror |
| **Watchdog** | in-memory, chat-recency, no cycle cap, lost on restart | durable mechanical signals (git HEAD / PR / transition log) + hard stall cap |
| **Rehydration** | coders only (git + `TASK.md`) | **all** agents, from durable store, via `make_agent(recovery_context)` factory |
| **Loop protection** | none | review-round cap (productive loops) + stall cap (silent loops), two distinct mechanisms |

### vs. `band-of-devs`

`band-of-devs` keeps a deterministic Python control plane outside the LLMs, but it is **one global pipeline, one phase at a time, Docker-per-agent, linear**. We **adapted, not copied**: its single global phase graph becomes our *per-subtask* graph instantiated N times; its single-pipeline invariants become code checks across the live instance set. codeband stays in-process/Band-coordinated/pool-parallel rather than Docker-per-agent-linear. Reference mapping from the RFC: their `pipeline_phase.py` → our WS1–2; `pipeline_watchdog.py` → WS4; `repo_init.py` → WS5; `request-review` → WS3.

## 2.7 Status, the dormancy nuance, and the risk that remains

**P1–P5 phasing:** P0 baseline → P1 store (shadow) → P2 FSM + gated handoffs → P3 watchdog → P4 rehydration. **P1–P4 are landed** — built as one Claude Code session per phase, each in its own git worktree, each independently reviewed and merged. Test suite is green (**605 tests pass** in the current checkout; the 3 previously-noted pre-existing `CliRunner` failures were fixed by capping `click<9`).

**The crucial nuance: everything so far is additive and DORMANT by design.** The deterministic layer exists and is unit-tested, but nothing yet *calls* `cb-phase`, so subtask rows are never created in a live run, so the watchdog and rehydration have nothing to act on. We verified this directly: `prompts/conductor.md` still instructs only the old free-text `protocol task_assignment …` envelope — no agent is told to call the FSM or `cb-phase`. The phases built the **safety rails**; nobody is driving on them yet. (The FSM is currently exercised only by tests and by the watchdog's blocked path.)

**P5 — "activation"** is the payoff and the riskiest move: update `prompts/*.md` so coders actually call `cb-phase verify` before handoff and the Conductor/Mergemaster route transition *effects* through the FSM. This is the shadow→enforced flip where "the LLM decides, code enforces" finally takes effect. The central risk lives here — **the Conductor "fighting the gate"**: a rejected transition must come back as an *actionable* error the LLM recovers from (and, if needed, escalates to a human), not a loop. P5 therefore needs its own careful handoff and heavy end-to-end testing, separate from the safe additive phases.

### Roadmap from here

1. **Targeted pre-E2E sweep** (report-only, diff-scoped): spec-vs-implementation (did the four sessions build the RFC faithfully?), dead-code/wiring (calibrated — shadow dormancy is *expected*), test-suite/flaky. *A static sweep explicitly cannot catch state/sequencing/race bugs — which is exactly what this feature is — so it complements, not replaces, E2E.*
2. **E2E validation** — a real `cb run`; confirm store writes, FSM logs, watchdog mechanical signals, and a killed agent rehydrating.
3. **P5 activation** — the enforced flip above.
4. **Upstream to `thenvoi/codeband`** — the separable-module structure was designed for this; run the full "Master Sweep" audit right before sharing externally.

### Key files in this repo

- `docs/rfc-deterministic-orchestration.md` — the design
- `src/codeband/state/store.py` / `fsm.py` / `rehydration.py`
- `src/codeband/cli/handoff.py` (the `cb-phase` entry point in `pyproject.toml`)
- `src/codeband/agents/watchdog.py`
- `src/codeband/config.py` — knobs: `handoff_verify_command`, `max_phase_visits` (10), `git_progress_check`, `max_review_rounds` (3)
- `src/codeband/orchestration/runner.py` (store init, watchdog wiring, rehydration loop), `kickoff.py` (task-row write)
- `src/codeband/prompts/conductor.md` — still old-protocol only (confirms gates are dormant)
- Tests: `tests/test_state_store.py`, `test_fsm.py`, `test_handoff.py`, `test_watchdog_upgrade.py`, `test_rehydration.py`

---

<a id="part-3"></a>

# Part 3 — The onboarding skill & distribution

> **Goal:** a brand-new person, with **only a Band account and three API keys**, gets to a working `/codeband <task>` in ~5 minutes — the skill (driving the coding agent) installs and wires up everything else.

## 3.1 The dependency chain the onboarding has to satisfy

| Piece | Install | Purpose |
|-------|---------|---------|
| Homebrew (macOS) | prereq | package manager |
| `uv` | `brew install uv` | runs codeband |
| `gh` + auth | `brew install gh` → `gh auth login` | PRs |
| `claude` CLI | already present (they're in CC) | Claude agents |
| `codex` CLI | `brew install codex` | Codex agents |
| `jam` | `brew install ed-lepedus-thenvoi/tap/jam` | CC's Band bridge |
| `codeband` | `uv tool install codeband` (public PyPI) | the swarm |
| 3 keys | `BAND_API_KEY` (`band_u_…`), `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` → home `.env` | auth |
| home config | `cb init` + `.env` + `cb setup-agents` (registers 8 Band agents) | the home |

## 3.2 What already exists vs. what the skill adds

**Most of the software install is already done** by `/codeband`'s Step 0 + `~/.claude/codeband/setup.sh` — an idempotent bootstrap that:

1. gates on macOS + Homebrew (hard fail otherwise);
2. installs only the missing CLIs (each `command -v … ||`-guarded): `uv`, `gh`, `codex`, `jam` (custom tap), `claude-code` cask, and `codeband` via `uv tool install`;
3. checks `gh auth status` (defers to the user if not authed — can't be done non-interactively);
4. validates the three keys (and *warns* if the Band key isn't a `band_u_` **user** key);
5. configures the jam profile (`jam init --user-api-key …`, idempotent via `jam whoami`);
6. creates the home + `cb init` (idempotent via `codeband.yaml`);
7. writes `.env` (0600, always refreshed);
8. registers the 8 Band agents (`cb setup-agents`, idempotent via `agent_config.yaml`);
9. finishes with `cb doctor`.

The user only supplies the three keys. The global `codeband` tool stays the **PyPI build** (so the skill keeps working regardless of the dev fork).

**The onboarding skill formalizes the human-facing front of that:**

- **Account + key acquisition** — guided links: make a Band account at app.band.ai and grab the `band_u_` **user** key (not an agent `band_a_` key — a real gotcha worth calling out); get Anthropic + OpenAI keys.
- **A guided/interactive walkthrough** rather than a bare script — confirm `gh auth`, collect keys conversationally, run the bootstrap, and interpret `cb doctor` (the single ⚠ about API-key-vs-subscription billing is fine).
- **"Your first run"** — kick off a trivial `/codeband` task end-to-end so the user sees the loop work.

## 3.3 The one real gap: distributing the skill files themselves

The bootstrap installs all the *software*, but a fresh machine still needs the **skill files** (`commands/codeband.md` + `codeband/setup.sh`) before anyone can type `/codeband`. The clean fix mirrors what `jam` already does — ship it as a **Claude Code plugin via a marketplace**. We drafted exactly this (a `codeband` plugin for the `jam-marketplace` repo). Then the whole onboarding collapses to:

```
claude plugin marketplace add ed-lepedus-thenvoi/jam-marketplace
claude plugin install codeband@jam-marketplace      # or: jam plugin install
/codeband <your first task>                          # first run auto-bootstraps
```

That's the 5-minute path: **account + keys → plugin install → first `/codeband` run.**

## 3.4 Why this matters for the pattern library

The onboarding skill is the template for *how any of our patterns reaches a new user*: a plugin/marketplace ships the protocol (the skill files), and a first-run idempotent bootstrap installs the runtime and collects only the irreducible human inputs (accounts, keys, auth). Part 4 generalizes this: every pattern in the library should be installable the same way and self-bootstrapping on first use.

---

<a id="part-4"></a>

# Part 4 — The pattern library: protocoled patterns as skills

> **Thesis:** `/codeband` is one concrete instance of a general idea — *a protocol for getting work done, packaged as a skill, that an agent can pick up and run.* The strategic opportunity is to build a **library of such patterns, from very simple to codeband-complex, that work beyond Claude Code and beyond coding.** What we are really shipping is not a coding swarm; it is a reusable way of encoding "how a job gets coordinated" so the encoding is solid, inspectable, and reusable.

## 4.1 What `/codeband` actually taught us (the transferable primitives)

Strip away "coding" and "Claude Code," and `/codeband` + the determinism work are made of a small set of primitives that recur in *any* serious multi-step or multi-agent job. These are the building blocks of the library:

| Primitive | In `/codeband` it is… | Generalizes to… |
|-----------|----------------------|-----------------|
| **Identity & presence** | `jam onboard` → an agent on Band | any agent joining any coordination substrate (Slack, email, a queue, a shared doc) |
| **Synthesized push** | a `Monitor` on the inbox file replacing native `<teammate-message>` | wake-on-event for *any* signal the host doesn't deliver natively |
| **Out-of-band truth** | GitHub (`cb pending`) + process log, not chat | never trust the chatter; verify the deliverable against an authoritative source |
| **The decide/enforce split** | Conductor routes; FSM gates effects | LLM owns judgment; code owns invariants — applies to *any* workflow with rules |
| **Durable state + rehydration** | SQLite + per-role recovery prompts | any long-running job that must survive a crash and resume coherently |
| **Mechanical liveness** | git HEAD / PR / transition-log progress signals + stall cap | detecting "stuck" by *real progress*, not by chatter, in any domain |
| **Verify-gated handoff** | `cb-phase verify` (clean tree → PR open → tests pass) | a validated checkpoint any actor must pass before the work advances |
| **Idempotent bootstrap + plugin distribution** | `setup.sh` + marketplace | how every pattern installs and self-provisions on first use |

A "pattern," in this library, is a chosen subset of these primitives, wired together for a class of job, and shipped as a skill.

## 4.2 The organizing principle: protocol vs. judgment

The single most important lesson from the determinism work (Part 2) is the **decide/enforce split**, and it is what makes a pattern *solid* rather than merely *clever*:

> **The LLM decides; code enforces and remembers.** Encode the *protocol* (the legal moves, the gates, the invariants, the durable state) in code; leave the *judgment* (what to do, how to phrase it, which path to take) to the agent.

A pattern is "solid and useful" to exactly the degree that it gets this line right:

- **Too much in code** → you've built a rigid script, lost the agent's adaptability, and gained nothing over a traditional workflow engine.
- **Too much in the LLM** → you've built a hopeful prompt; nothing prevents skipped steps, double-actions, infinite loops, or silent death (this is stock codeband's fragility).
- **The line drawn well** → the agent stays creative and autonomous, while code makes illegal *consequences* impossible and remembers everything across crashes.

Every pattern in the library should declare, explicitly, **what it gates and what it leaves free.** That declaration is the spec.

## 4.3 A complexity ladder (simple → codeband-complex)

The library should span a deliberate range. Each rung adds primitives from §4.1.

**Rung 0 — Solo protocol (no other agents).**
A single agent following a checkpointed protocol with verify-gates and durable state. No coordination substrate. Example: a *release-cut* skill that walks version-bump → changelog → tag → publish, where each step is a verify-gate (the next step refuses until the prior one's artifact exists) and state is durable so a crash resumes mid-release. *Primitives: decide/enforce split, verify-gated handoff, durable state + rehydration.*

**Rung 1 — Solo with out-of-band truth + liveness.**
Add a watcher that wakes the agent on external events and a mechanical "am I actually making progress?" signal. Example: a *deploy-and-watch* skill that triggers a deploy, then watches the real health endpoint / CI status (not its own assumptions) and escalates on a stall. *Adds: synthesized push, out-of-band truth, mechanical liveness.*

**Rung 2 — Two agents, one coordinator.**
One agent (possibly a human-facing Claude session) coordinates one or more workers over a substrate, with synthesized push and out-of-band verification. This is `/codeband` *minus* the deterministic control plane — the pure coordination pattern. Example: a *research-desk* where a coordinator dispatches sub-questions to worker agents and verifies each answer against cited sources before accepting. *Adds: identity & presence, coordinator role.*

**Rung 3 — Fan-out swarm with a deterministic control plane (codeband).**
N workers, interleaved phases, a per-unit FSM gating effects, durable state, universal rehydration, mechanical watchdog. This is the full codeband shape. The expensive rung — justified only when the work is genuinely parallel, long-running, and correctness-critical.

The value of the ladder: **most jobs don't need rung 3.** A pattern library lets us pick the *cheapest rung that satisfies the job*, and reuse the same primitives at every level.

## 4.4 Beyond coding, beyond Claude Code

Nothing in §4.1 is coding-specific or Claude-Code-specific. The same primitives apply to general knowledge work; only the *authoritative sources* and *substrate* change.

**Non-coding examples (same patterns, different nouns):**

- **Content/marketing pipeline** — draft → editorial review → legal/brand gate → publish. The FSM gates "published" behind "approved by the right role"; the verify-gate is "brand checklist passes"; out-of-band truth is the CMS, not the chat. (Rung 3 shape, zero code.)
- **Sales/CRM ops** — enrich → qualify → route → follow-up, with the durable store preventing double-contact and the watchdog catching leads that have stalled in a stage. (Clay/HubSpot/Linear tooling already available — the substrate is the CRM.)
- **Research / due diligence** — fan-out questions to workers, adversarially verify each claim against sources, synthesize. (This is the `deep-research` skill shape — a rung-2 pattern already in the toolbox.)
- **Incident response** — a coordinator agent that watches monitoring (out-of-band truth), drives a runbook FSM (gates: don't escalate before triage, don't close before postmortem), and rehydrates if the responder session dies mid-incident.
- **Operations / scheduling** — recurring jobs where the durable state and verify-gates prevent the same action firing twice and the liveness signal catches a hung step.

**Beyond Claude Code as the host:** the coordinator need not be a Claude Code session. The *protocol* (the FSM, the gates, the store) is host-agnostic code; the *agent* can be any LLM runtime, and the *substrate* can be Band, Slack, email, a message queue, or a shared database. `/codeband`'s `jam`/Band specifics are an implementation detail of one rung-3 instance, not part of the pattern.

## 4.5 What makes a pattern solid (the contract every library entry should meet)

From everything we learned building `/codeband` and hardening codeband, a reusable pattern should satisfy:

1. **An explicit decide/enforce boundary** — a one-paragraph statement of what is gated (code) vs. free (agent). If you can't write it, the pattern isn't designed yet.
2. **Durable, queryable state** — not chat history, not a 1000-char memory blob. Local, unbounded, identical across run modes (the SQLite lesson).
3. **Out-of-band verification** — at least one authoritative source other than the agents' own chatter, for the parts that matter.
4. **Mechanical liveness** — "stuck" detected by real progress signals + a hard cycle cap, never by recency-of-talk.
5. **Crash-safe resume** — any actor can die and rehydrate from durable state into a coherent position.
6. **Actionable failure** — a rejected/gated action returns an error the agent can recover from (and escalate to a human), never a silent drop or a loop. *This is the P5 risk from Part 2, generalized: gates must teach, not just block.*
7. **Idempotent first-run bootstrap + plugin distribution** — installs itself and collects only irreducible human inputs (Part 3).
8. **Graceful degradation** — every new mechanism guarded so its failure falls back to the prior behavior (the "shadow mode" discipline from codeband's P1).

A pattern that meets all eight is *solid*; one that meets the first two is at least *honest about its boundaries*.

## 4.6 Suggested next steps to turn this into a real library

1. **Extract the primitives into a shared core** — the decide/enforce FSM scaffold, the durable store, the rehydration factory, the mechanical watchdog, the verify-gate CLI — as a host-agnostic toolkit independent of codeband and of coding.
2. **Author the rung-0 and rung-1 reference patterns first** (release-cut, deploy-and-watch). They're cheap, they exercise the core, and they prove the primitives generalize off coding.
3. **Re-express `/codeband` on top of the shared core** — proving rung-3 is "the core + a coordination substrate + a domain," not a bespoke build.
4. **Pick one non-coding rung-2/3 pattern** (research-desk or a CRM-ops flow) to prove the substrate and domain are swappable.
5. **Ship them all the same way** — plugin/marketplace + idempotent bootstrap (Part 3), with each pattern's decide/enforce contract (§4.5) written at the top of its skill file.

The endgame: a catalogue where someone picks a rung and a domain, installs a skill, brings their keys, and gets a *solid* protocol-driven agent — coding or otherwise — without re-litigating identity, push, state, liveness, gating, and recovery each time. We've already paid that tuition once, on `/codeband`.
