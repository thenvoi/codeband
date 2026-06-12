# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -e ".[dev]"              # Install in dev mode (Claude + Codex bundled)
pytest                               # Run all tests
pytest tests/test_config.py -v       # Run a single test file
pytest -k "test_yaml_roundtrip"      # Run a specific test
ruff check src/ tests/               # Lint
ruff check --fix src/ tests/         # Lint with auto-fix
```

## Architecture

Codeband orchestrates multiple AI coding agents in parallel via the [Band.ai](https://band.ai) platform.

### Two agent patterns

1. **ClaudeSDK/Codex adapters** — Every agent that uses an LLM (Planner, Conductor, Coder, Plan Reviewer, Code Reviewer, Mergemaster) wraps `ClaudeSDKAdapter` or `CodexAdapter` from `band-sdk` (Python imports still use the `thenvoi` module namespace — the PyPI package was renamed but module paths stayed). They expose an `.adapter` property for `Agent.create()`. Every role supports both Claude and Codex frameworks. Permissions are role-specific: Coders, Mergemaster, and Code Reviewer run with full access (`bypassPermissions` for Claude, `danger-full-access` for Codex) — Coders and Mergemaster in git worktrees, the Code Reviewer in an isolated scratch directory. Coordination-only agents (Plan Reviewer, Claude Conductor) use `permission_mode="dontAsk"` with `approval_mode=None` — the Claude CLI honors the worktree's `.claude/settings.json` allow list (see profiles in `workspace/init.py`) and deterministically denies everything else. The Codex Conductor gets the closest available equivalent via `sandbox="read-only"` + `approval_policy="never"` + an isolated temporary `cwd` outside the repo. That keeps native Codex file tools away from project files, but it is still weaker than Claude's true "MCP tools only" shape because the current Thenvoi Codex adapter does not expose a native-tool allowlist. Prompts are loaded via the shared `agents/prompts.py:load_prompt()` utility and are framework-portable — the `thenvoi_*` MCP tool names are injected by both adapters. See `player_claude.py` / `player_codex.py` as the canonical coding pair and `conductor.py` as the coordination example.

2. **Deterministic daemon** — The Watchdog runs as a plain `asyncio` task (not a Band.ai Agent). It polls the Band.ai REST API on an interval, applies deterministic threshold rules to detect stale agents, nudges them, and escalates to the Conductor on a second threshold crossing. No LLM. See `watchdog.py:WatchdogDaemon`.

All LLM calls — including the `cb prs --smart` / `cb issues --smart` CLI helpers (`utility_llm.py:one_shot_text`) — go through Claude Code SDK or Codex CLI, so a user can authenticate once via `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, or `OPENAI_API_KEY` and the whole system works.

### Auth policy (asymmetric by design)

- **Claude is subscription-first.** `cli.py:_resolve_claude_auth` strips `ANTHROPIC_API_KEY` at startup whenever any OAuth source exists: `CLAUDE_CODE_OAUTH_TOKEN`, macOS Keychain (`security -s "Claude Code-credentials"`), or `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude/.credentials.json`). This is because the Claude CLI's own precedence puts API key *above* OAuth — without intervention, a user with `claude` logged in locally would silently pay per token.
- **Codex is API-key-first.** When both `OPENAI_API_KEY` and a `~/.codex/auth.json` ChatGPT session exist, the API key wins (per OpenAI's own automation guidance and `docker/entrypoint.sh`). Parallel Codex workers exhaust subscription quotas quickly.
- **Preflight in `cb run`** (`preflight.py`) makes one tiny call through each CLI before spawning agents. It pattern-matches known error text ("Credit balance is too low", "usage limit reached", "rate_limit_error", "please log in", etc.) and fails fast with a remediation hint. Without this, auth/billing errors surface as plain assistant text posted into chat rooms — the watchdog stays happy and the whole swarm idles silently. `--skip-preflight` bypasses for CI/offline. The Codex preflight only runs when the config has at least one Codex agent.
- **Doctor** (`doctor.check_claude_auth`) warns when `ANTHROPIC_API_KEY` is set alongside host subscription OAuth — Codeband auto-prefers at run-time, but the WARN flags the suboptimal `.env` for cleanup.

### Worker pool architecture

Coders, Code Reviewers, Planners, and Plan Reviewers are **pool roles** — each is declared in `codeband.yaml` as `{framework: {count, model}}` under `agents.{coders, reviewers, planners, plan_reviewers}`. Conductor and Mergemaster are **singletons**. Pool member identities follow `{role}-{framework}-{index}` (e.g., `coder-claude_sdk-0`); Band.ai display names are title-cased (`Coder-Claude-0`). The default `cb init` config is 8 agents total — fits Band.ai's free-tier 10-agent cap.

The `codeband/workers/pool.py:WorkerPool` is a thread-safe in-memory allocator with `acquire(role, framework)`, `release(worker_id)`, and `pair_for_task(coder_role, coder_framework)` which atomically reserves a coder and an opposite-framework reviewer (adversarial cross-model review is the primary value prop — a Claude coder's PR routes to a Codex reviewer, and vice versa). The allocator is defined but not yet wired into the Conductor's LLM prompt — allocation is currently prompt-enforced via `runner._build_worker_roster()` which surfaces the pool to the Conductor. Code-backed allocation is on the roadmap.

**Coder→Reviewer dispatch is direct, not relayed.** At PR completion, the Coder picks an opposite-framework Reviewer from the same Worker Pool Roster (now also injected into the Coder prompt via `_build_worker_roster()` and `_create_coder(worker_roster=...)`) and @mentions them alongside the Conductor. The Conductor stays silent at first dispatch and only relays for re-review rounds after a `[Critical]` finding. The roster includes concrete display names, and Coders use deterministic worker-index pairing (`Coder-Claude-1` → `Reviewer-Codex-1` when available, otherwise modulo reviewer count). For collision-free parallel review, opposite-framework reviewer capacity should be at least as large as coder capacity; `cb doctor` warns when capacity is lower. Code-backed arbitration via `WorkerPool.pair_for_task()` is still the roadmap.

### Session recovery

Every agent runs under a reconnect-forever loop: both crashes and clean exits from `agent.run()` trigger another cycle. Unsupervised roles (Conductor, Mergemaster, Planner, Plan Reviewer, Code Reviewer) use the module-level `_run_agent_forever` in `orchestration/runner.py` with exponential backoff capped at 60s. Coders additionally run under `WorkerSupervisor` (`session/supervisor.py`), which rebuilds recovery context from git log + uncommitted changes + `TASK.md` on each restart; identity is persisted as JSON in `workspace/state/<worker-id>.json` (`session/identity.py:WorkerIdentity`). Only SIGINT/SIGTERM ends the process. `PoolEntry.restart_delay_seconds` in `config.py` controls the base supervisor delay; `PoolEntry.max_restarts` is deprecated and no longer honored.

**Reconnect ownership.** Local mode: the outer reconnect loop is the *sole* lifecycle owner — `_patch_band_local_runtime()` disables the SDK's PHX auto-reconnect, so socket death is detected by the websockets keepalive (~40s), which exits the routing task and triggers a fresh cycle; the SDK's idle resync (`agents.idle_resync_seconds`, default 30) is the delivery backstop for missed pushes — with a known upstream defect: the backstop self-disables per room once any one message exhausts its retry budget (client-side permafail pins the head of the `/next` queue); `agents.max_message_retries` (default 3, vs the SDK's 1) reduces how often that fires but does not eliminate it; on each (re)start agents subscribe to existing rooms scoped to active tasks in the StateStore (`cb run --fresh` opts out; a store failure subscribes everything — fail toward connectivity). Teardown between cycles (`_safe_stop_agent`, supervisor `_close_agent`) fails loud at ERROR and verifies the websocket actually closed. Distributed mode (`run_agent`) is intentionally unpatched: SDK-native reconnect and subscribe-existing.

### Communication: three-channel model

The system uses three channels: **chat** (Band.ai @mentions) for content delivery and coordination, **memory** (Band.ai memory API) for protocol state tracking, and **GitHub PR comments** for code review artifacts. Memory has a 1000-char content limit — full plans, reviews, and logs flow through chat and GitHub, while memory stores lightweight state envelopes.

Agents interact through **protocols** (Code Review, Clarification, Merge Conflict, Test Failure, Plan Revision) — structured collaboration patterns with no hard round limits; the Conductor intervenes when interactions stall. Each protocol stores a state envelope in memory using search-safe alphanumeric tokens (e.g., `protocol code_review cid cr_42_r1 pr 42 round 1 state findings_posted`). Protocol state uses `system="working", type="episodic", segment="agent", scope="organization"`. Repo knowledge uses `system="long_term", type="procedural", segment="tool"`.

The `orchestration/kickoff.py` creates the room topology and sends the initial task. The `orchestration/runner.py` starts agents as concurrent `asyncio` tasks: every role (Conductor, Mergemaster, Planner/Plan-Reviewer/Code-Reviewer pool members, Coders) runs under a reconnect-forever loop, plus the Watchdog daemon. The runner waits only on a shutdown signal (`shutdown_event`); on SIGINT/SIGTERM it cancels every task and exits cleanly.

### Memory backend (paid tier vs free tier)

Band.ai's memory API is a paid-tier feature. The `codeband/memory/` package wraps this: `probe.probe_memory_backend()` runs one `list_agent_memories()` call at startup and caches the result (`band` / `local`). On free tier (HTTP 402/403/404/501) or unreachable Band.ai, `runner._install_memory_backend()` monkey-patches `AgentTools.{store,list,archive}_memory` on the SDK runtime class to delegate to a local JSONL store at `workspace/state/memories.jsonl` (`memory/local_store.py`, `fcntl.flock` for concurrent writes). Tool names stay identical, so prompts don't change. `BAND_MEMORY_MODE=band|local` env var or `band.memory_mode: band|local` in `codeband.yaml` skips the probe. Free-tier + distributed mode doesn't share state across hosts — single-machine only.

### Environment doctor

`codeband/doctor.py` implements `cb doctor` — a read-only diagnostic that checks env vars (Claude/Codex/Band auth), external tools (git, gh + auth), config files (codeband.yaml, agent_config.yaml), workspace writability, and Band.ai REST connectivity including the memory probe. Each check is a small function returning a `CheckResult` (OK/WARN/FAIL/INFO/SKIP) with a `remediation` hint; registry lives at the bottom of `doctor.py` in `_CHECKS`. Exit code is 1 if any check FAILs, else 0. No side effects — never modifies state.

### Merge strategy

The Mergemaster uses a Bors-style batch-then-bisect algorithm (see `prompts/mergemaster.md`). The Code Reviewer performs code review before merge, posting findings as GitHub PR comments. The Mergemaster handles integration testing and uses chat + PR comments for the Test Failure and Merge Conflict protocols. Dual-player / ensemble-at-implementation was removed in the worker-pool refactor — cross-model code review is the adversarial signal now; if ensemble is ever needed it's a future Conductor directive, not a first-class concept.

### GitHub integration

`github/prs.py` provides PR discovery for task selection. `github/issues.py` provides issue discovery with the same pattern. Both shell out to the `gh` CLI, support deterministic sort modes and AI-powered `--smart` ranking. Exposed via `cb prs`, `cb issues`, and `cb issue <number>` CLI commands.

### Monitoring

`monitoring/activity_log.py` provides append-only JSONL logging of system events (session starts/crashes, nudges, escalations). `monitoring/feed.py` provides a live terminal stream by polling the Band.ai human API. Both are exposed as one-shot CLI commands (`cb log` / `cb feed`) and as embedded views inside the interactive shell — bare `cb` opens a single-terminal session that runs the orchestrator, scrolls the feed above the prompt, and accepts slash commands (`/log`, `/diff`, `/task`, …). The shell's slash commands are dispatched from `shell/commands.py`; the prompt loop and feed wiring live in `shell/repl.py`. Compose subprocess plumbing for `cb up` / `cb down` / `/down` / `SharedComposeBackend` goes through `orchestration/compose.py` so cwd + `CODEBAND_PROJECT_DIR` are owned in one place.

### Config layer

Two YAML files drive the system: `codeband.yaml` (project config — Pydantic models in `config.py`) and `agent_config.yaml` (Band.ai credentials — generated by `orchestration/setup.py`). All config models use `model_dump(mode="json")` for YAML serialization to avoid enum serialization issues with `yaml.safe_load`.

### Prompt files

Agent system prompts live in `src/codeband/prompts/*.md` and are loaded directly from the installed package at runtime — there is no project-level override. Each agent module defines `_DEFAULT_PROMPT = Path(__file__).parent.parent / "prompts" / "<role>.md"` and `agents/prompts.py:load_prompt()` reads it. The runner composes extra sections (Conductor worker roster, Mergemaster config, reviewer guidelines) via the orthogonal `custom_prompt` / `review_guidelines` constructor args. Prompts include critical anti-loop discipline rules (from proven Band.ai patterns): @mentioning = function call, go silent after handoff, chat for notifications only, content exchange via memory protocols. Each prompt documents which protocols the agent participates in and the exact memory API parameters to use.

## Conventions

- Python 3.11+, ruff for linting, line length 100
- Pydantic v2 models for all configuration
- `from __future__ import annotations` in every file
- Band.ai SDK imports are deferred (inside functions) to keep import time fast and avoid hard dependency when only config/workspace code is used
- Tests use `tmp_path` fixtures for filesystem isolation; git integration tests create real repos with `subprocess`

---

## Determinism fleet (fork-local coordination)

> This section coordinates the multi-session build of the determinism RFC in this **fork**. It is not part of upstream codeband — exclude it when upstreaming phase branches.

We are implementing [`docs/rfc-deterministic-orchestration.md`](docs/rfc-deterministic-orchestration.md) as **one independent Claude Code session per phase**, each in its own git worktree, each landing **one PR**. Before writing code, read your phase's RFC workstream in full.

**Guiding principle:** the LLM decides, code enforces and remembers — the FSM gates EFFECTS (transitions/merges), not the Conductor's creative routing.

| Phase / worktree | RFC workstream | Owns (new files) | May edit | Depends on |
|---|---|---|---|---|
| `phase-1-state-store` | WS1 | `state/__init__.py`, `state/store.py`, `tests/test_state_store.py` | `orchestration/runner.py` (`_install_memory_backend`), `orchestration/kickoff.py` (`send_task`) | — (**blocks all**) |
| `phase-2-fsm-gates` | WS2+WS3 | `state/fsm.py`, `cli/__init__.py` (move from `cli.py`), `cli/handoff.py`, `tests/test_fsm.py`, `tests/test_handoff.py` | `pyproject.toml`, `config.py` (`AgentsConfig`) | Phase 1 |
| `phase-3-watchdog` | WS4 | `tests/test_watchdog_upgrade.py` | `agents/watchdog.py`, `config.py` (`WatchdogConfig`), maybe `orchestration/runner.py` | Phase 1 |
| `phase-4-rehydration` | WS5 | `state/rehydration.py`, `tests/test_rehydration.py` | `orchestration/runner.py` (5 factories + `_run_agent_forever`), `agents/{conductor,planner,plan_reviewer,code_reviewer,mergemaster}.py`, `orchestration/agent_main.py` | Phase 1 |

**Merge order:** Phase 1 first (everything imports `state.store`). Then Phase 2 ∥ Phase 3. Phase 4 last (heaviest `runner.py` edits). Overlap to rebase around: `config.py` (P2 vs P3, different classes) and `runner.py` (P1/P3/P4).

**Rules for every session:**
- Stay in your lane — implement exactly your phase; do not touch other phases' files. If you need to, STOP and report.
- Branch + PR always; never commit to `main`; Conventional Commits.
- Run the suite before push; add **no new failures**. Self-review (`git diff main...HEAD`) — scope must match the phase only.

**Dev env (per worktree — `.venv` is not shared):**
```
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```
**Baseline on `main`: the full suite passes** (the 3 `CliRunner` failures previously listed here have been fixed). Acceptance for any phase = the suite stays fully green and your new tests pass.
