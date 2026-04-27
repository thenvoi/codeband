# Deployment Guide

Codeband supports three deployment modes. All modes require the same initial setup, then diverge in how agents are started.

## Initial Setup (all modes)

### 1. Initialize the project

```bash
cd my-project
cb init --repo https://github.com/myorg/myrepo.git
```

This creates:
- `codeband.yaml` — project configuration
- `prompts/` — customizable agent prompts
- `.env.example` — environment variable template

### 2. Configure authentication

```bash
cp .env.example .env
```

Edit `.env`. Claude Code supports two authentication methods — choose one:

**Option A: Anthropic API key** (pay-per-token, no subscription rate limits)

```
ANTHROPIC_API_KEY=sk-ant-...
```

**Option B: Claude subscription OAuth token** (uses your Claude Pro/Max plan)

```bash
# Generate a long-lived token (run once, locally):
claude setup-token
```

Then add it to `.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=...
```

> **Note:** Subscription plans have rate limits designed for individual use. Running multiple agents in parallel may hit these limits. API keys offer more predictable scaling for multi-agent workloads.
>
> If both `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are set, Codeband prefers the OAuth token (fixed-cost subscription) and ignores the API key.

**Codex authentication:**

Any Codex agent (planner, plan reviewer, coder, code reviewer, plus optionally conductor and mergemaster) accepts either an API key or a ChatGPT Pro/Plus subscription. Unlike Claude Code, Codex does not have a subscription-token environment variable — credentials live in `~/.codex/auth.json` instead.

- **API key** — set `OPENAI_API_KEY` in `.env`. Works everywhere.
- **Subscription** — run `codex login --device-auth` on the host once. In local mode (`cb run`), Codex reads `~/.codex/auth.json` directly. In Docker mode (`cb up`), Codeband bind-mounts the host `~/.codex` **read-only** into every codex-capable service (planner, plan reviewer, coder, code reviewer). The entrypoint copies the file into a container-local `CODEX_HOME` (`/tmp/codex-auth`) at startup; the host file is never modified. OAuth refresh-token rotations stay inside the container, so you may occasionally need to re-run `codex login --device-auth` on the host when the refresh token expires.

If both are present, `OPENAI_API_KEY` wins — the entrypoint writes API-key auth into the container-local `CODEX_HOME`, leaving the host `auth.json` untouched.

**Remaining variables:**

```
BAND_API_KEY=band_u_...
OPENAI_API_KEY=sk-...          # Optional — only set if using API-key auth for Codex
GH_TOKEN=ghp_...               # Recommended for Docker gh CLI auth
```

### 3. Register agents on Band.ai

**Option A — paid/enterprise Band.ai:**

```bash
cb setup-agents
```

This registers all agents (Planner, Conductor, Reviewers, Coders, Mergemaster) on the Band.ai platform and writes credentials to `agent_config.yaml`. The Watchdog is an in-process daemon that reuses the Conductor's credentials — it is not registered as a platform agent. Safe to run multiple times — existing agents are reused.

**Option B — free-tier Band.ai:** `cb setup-agents` requires the enterprise agent-registration API and will fail with a 403. Create the agents manually in the Band.ai web UI and write `agent_config.yaml` by hand — see the step-by-step walkthrough in [Configuration](docs/CONFIGURATION.md#manual-agent-registration-free-tier).

---

## Mode 1: Local (no Docker)

All agents run in a single Python process on your machine. Simplest way to get started.

**Requirements:** Python 3.11+, git

```bash
cb              # interactive shell — orchestrator + live feed + slash prompt
# or, headless:
cb run          # agents only, no UI (CI / scripts)
```

`cb` (the interactive shell) starts the agents in-process and drops you at a `>` prompt where you can issue slash commands (`/task`, `/diff`, `/prs`, …) and watch the live feed scroll by — all in one terminal. `cb run` is the headless equivalent for CI and scripts.

Both forms start the same fleet: Coders get isolated git worktrees; the Conductor, Mergemaster, and Watchdog coordinate via Band.ai.

Send a task — either inside the shell (`> /task ...`) or from a separate terminal:

```bash
cb task "Implement JWT authentication"
```

### How the workspace looks

```
workspace/
    repo.git/                        # bare clone (shared object store)
    worktrees/
        planner-claude_sdk-0/        # detached HEAD (read-only)
        plan_reviewer-codex-0/       # detached HEAD (read-only)
        coder-claude_sdk-0/          # branch: codeband/coder-claude_sdk-0/<task>
        coder-codex-0/               # branch: codeband/coder-codex-0/<task>
        mergemaster/                 # branch: main
    scratch/
        reviewer-claude_sdk-0/       # scratch dir for gh calls (no repo)
        reviewer-codex-0/
    notes/                           # Conductor writes plan.md here
    state/                           # identity files, activity log, memory JSONL
```

### When to use this mode

- Development and testing
- Small tasks where latency matters more than reliability
- You want to see everything in one terminal

---

## Mode 2: Local Docker

Each agent runs in its own Docker container on a single host. Containers share workspace via Docker volumes.

**Requirements:** Docker, Docker Compose

### Start

```bash
# Build and start
cb up -d

# Or manually:
docker compose -f docker/docker-compose.yml up --build -d
```

### Stop

```bash
cb down

# Remove volumes too:
cb down -v
```

### View logs

```bash
docker compose -f docker/docker-compose.yml logs -f
docker compose -f docker/docker-compose.yml logs -f coder-claude-0
```

### Architecture

```
┌─────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  conductor  │  │ coder-claude-0  │  │  coder-codex-0  │
└──────┬──────┘  └────────┬────────┘  └────────┬────────┘
       │                  │                    │
       └──────────────────┼────────────────────┘
                          │
                ┌─────────┴──────────┐
                │   Docker Volumes   │
                │  bare_repo         │
                │  worktrees         │
                │  shared_notes      │
                │  shared_state      │
                └────────────────────┘
```

All containers share four Docker volumes. The first container to start clones the repo (with file locking to prevent races). Each agent gets its own worktree (coders, planners, plan_reviewers, mergemaster) or scratch dir (reviewers) for isolation.

### Adding more coders

Scale a pool entry in `codeband.yaml` via `cb scale`:

```bash
cb scale coders.claude_sdk=2   # add a second Claude coder
cb setup-agents                # register the new `Coder-Claude-1` on Band.ai
```

Then add a matching service block to `docker/docker-compose.yml`:

```yaml
  coder-claude-1:
    <<: *agent-base
    build:
      context: ..
      dockerfile: docker/Dockerfile.claude
    environment:
      <<: *env-base
      AGENT_KEY: coder-claude_sdk-1
      AGENT_ROLE: coder
    volumes: *volumes-base
```

### When to use this mode

- Running on a single server or VM
- You want container isolation but don't need multi-host
- CI/CD pipelines

---

## Mode 3: Distributed (multi-host / multi-cloud)

Each agent runs independently on its own host. No shared filesystem. Agents coordinate via Band.ai WebSocket and sync code through the git remote origin.

**Requirements per host:** Docker (or Python 3.11+ with git), network access to Band.ai and the git remote

> **Requires paid Band.ai.** Distributed mode depends on Band.ai's memory API to share protocol state across hosts. On free tier, Codeband falls back to a per-host JSONL file (`workspace/state/memories.jsonl`) that **does not sync between hosts** — each agent sees only its own state and the system degrades silently. Verify with `cb doctor` on one of the hosts: the "Memory backend" line should read `Band.ai remote API (paid tier)`. If it says "local JSONL store", either upgrade Band.ai or use Mode 1 / Mode 2.

### How it works

```
┌───── AWS ──────┐   ┌──── Render ────┐   ┌───── GCP ──────┐
│   conductor    │   │coder-claude_sdk│   │  coder-codex   │
│  (own clone)   │   │ -0 (own clone) │   │ -0 (own clone) │
└───────┬────────┘   └───────┬────────┘   └───────┬────────┘
        │                    │                    │
        │         ┌──────────┴──────────┐         │
        └─────────┤   Band.ai Platform  ├─────────┘
                  │  (WebSocket + REST) │
                  └──────────┬──────────┘
                             │
                  ┌──────────┴──────────┐
                  │    Git Remote       │
                  │  (GitHub/GitLab)    │
                  └─────────────────────┘
```

Each agent:
1. Clones the repo independently at startup
2. Creates its own local worktree
3. Connects to Band.ai via WebSocket
4. Coders push branches to origin; Mergemaster fetches and merges them

### Step-by-step walkthrough

#### 1. Set distributed mode in config

Edit `codeband.yaml`:

```yaml
workspace:
  path: "/workspace"
  worktree_prefix: "codeband"
  mode: distributed
```

#### 2. Decide your topology

Example split across AWS and GCP:

| Host | Cloud | Agents |
|------|-------|--------|
| Host A | AWS EC2 | Conductor, Watchdog, Planner-Claude-0, Plan-Reviewer-Codex-0 |
| Host B | GCP Cloud Run | Coder-Claude-0, Coder-Codex-0 |
| Host C | AWS EC2 | Reviewer-Claude-0, Reviewer-Codex-0, Mergemaster |

#### 3. Distribute config files to each host

Every host needs these three artifacts (identical copies):

```
codeband.yaml          # from cb init
agent_config.yaml      # from cb setup-agents
prompts/               # from cb init
```

Plus environment variables: `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` for Claude agents (and `OPENAI_API_KEY` for Codex agents). For agents that use `gh` inside Docker, also set `GH_TOKEN` or `GITHUB_TOKEN`.

#### 4. Start agents on each host

**With Docker:**

Use `-e ANTHROPIC_API_KEY=sk-ant-...` or `-e CLAUDE_CODE_OAUTH_TOKEN=...` depending on your auth method.

```bash
# Host A — Conductor
docker run -d --name conductor \
  -e AGENT_KEY=conductor \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e DEPLOYMENT_MODE=distributed \
  -v ./codeband.yaml:/app/config/codeband.yaml:ro \
  -v ./agent_config.yaml:/app/config/agent_config.yaml:ro \
  -v ./prompts:/app/config/prompts:ro \
  codeband:latest \
  python -m codeband.orchestration.agent_main

# Host A — Watchdog
docker run -d --name watchdog \
  -e AGENT_KEY=watchdog \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e DEPLOYMENT_MODE=distributed \
  -v ./codeband.yaml:/app/config/codeband.yaml:ro \
  -v ./agent_config.yaml:/app/config/agent_config.yaml:ro \
  -v ./prompts:/app/config/prompts:ro \
  codeband:latest \
  python -m codeband.orchestration.agent_main

# Host B — Coder-Claude-0
docker run -d --name coder-claude-0 \
  -e AGENT_KEY=coder-claude_sdk-0 \
  -e AGENT_ROLE=coder \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e DEPLOYMENT_MODE=distributed \
  -v ./codeband.yaml:/app/config/codeband.yaml:ro \
  -v ./agent_config.yaml:/app/config/agent_config.yaml:ro \
  -v ./prompts:/app/config/prompts:ro \
  codeband:latest \
  python -m codeband.orchestration.agent_main

# Host B — Coder-Codex-0
docker run -d --name coder-codex-0 \
  -e AGENT_KEY=coder-codex-0 \
  -e AGENT_ROLE=coder \
  -e OPENAI_API_KEY=sk-... \
  -e DEPLOYMENT_MODE=distributed \
  -v ./codeband.yaml:/app/config/codeband.yaml:ro \
  -v ./agent_config.yaml:/app/config/agent_config.yaml:ro \
  -v ./prompts:/app/config/prompts:ro \
  codeband:latest \
  python -m codeband.orchestration.agent_main

# Host C — Mergemaster
docker run -d --name mergemaster \
  -e AGENT_KEY=mergemaster \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e DEPLOYMENT_MODE=distributed \
  -v ./codeband.yaml:/app/config/codeband.yaml:ro \
  -v ./agent_config.yaml:/app/config/agent_config.yaml:ro \
  -v ./prompts:/app/config/prompts:ro \
  codeband:latest \
  python -m codeband.orchestration.agent_main
```

**Without Docker (Python directly):**

```bash
pip install codeband
cb run --agent coder-claude_sdk-0
```

**Using the distributed compose template (single host, for testing):**

```bash
docker compose -f docker/docker-compose.distributed.yml up \
  conductor mergemaster \
  planner-claude-0 plan-reviewer-codex-0 \
  coder-claude-0 coder-codex-0 \
  reviewer-claude-0 reviewer-codex-0 \
  watchdog
```

#### 5. Send a task

From any machine with the project config:

```bash
cb task "Implement JWT authentication"
```

The Conductor picks it up via Band.ai regardless of where it's running.

### What happens at runtime

```
1.  Conductor receives task via Band.ai
2.  Conductor routes to Planner via @mention
3.  Planner analyzes codebase, decomposes task, writes plan
4.  Planner @mentions Plan Reviewer (cross-framework) for review
5.  Plan Reviewer posts findings → Planner revises if needed
6.  Planner requests user approval via @mention
7.  User approves → Planner notifies Conductor
8.  Conductor reads plan, @mentions each Coder with assignments
9.  Coders receive assignments via Band.ai WebSocket
10. Each coder works in its local worktree:
    - Writes code and tests
    - git commit && git push origin codeband/coder-claude_sdk-0/impl-auth
    - Opens PR via gh pr create
11. Coders @mention Conductor: "done, PR #N"
12. Conductor @mentions Code Reviewer (opposite framework) for review
13. Code Reviewer posts findings as GitHub PR comments
14. Coder addresses feedback and pushes fixes (re-review if required)
15. Conductor @mentions Mergemaster: "merge PRs X and Y"
16. Mergemaster runs batch-then-bisect integration tests, then merges:
    - git fetch origin && git merge --no-ff origin/<branch>
    - git push origin main
17. Mergemaster @mentions Conductor: "merged, tests pass"
18. Conductor @mentions user: "task complete"
```

### Git authentication on remote hosts

Each host needs git credentials to push/pull from your repo. Options:

- **SSH key**: Mount `~/.ssh` into the container or set `GIT_SSH_COMMAND`
- **HTTPS token**: Set `REPO_URL=https://<token>@github.com/myorg/myrepo.git` in the environment
- **GitHub App**: Use a GitHub App installation token
- **Credential helper**: Configure `git credential.helper` in the container

The `REPO_URL` environment variable overrides the URL in `codeband.yaml`, which is useful for injecting tokens without modifying the config file.

### Monitoring in distributed mode

The interactive shell embeds the feed above the prompt — open one with:

```bash
cb --attach     # thin client: feed + slash commands, no in-process agents
```

Outside the shell, the same data is exposed as one-shot commands:

```bash
# Live feed — streams from Band.ai, works from anywhere
cb feed
cb feed --agent coder-claude_sdk-0 --no-thoughts

# cb log only reads LOCAL activity.jsonl — per-host only
# Use cb feed (or the shell) for cross-host monitoring
```

### Startup order

Doesn't matter. Agents connect to the same Band.ai chat room. Late joiners pick up message history. The only requirement is that `cb setup-agents` has been run first (locally) to register agents and generate credentials.

### Failure handling

| Scenario | What happens |
|----------|-------------|
| Coder container crashes | `WorkerSupervisor` reconnects forever (only SIGINT/SIGTERM ends a session). Recovery context rebuilt from git state. |
| Conductor/Mergemaster crashes | That agent stops. Other agents continue but can't receive new work. Restart the container. |
| Watchdog crashes | Health monitoring stops. Other agents unaffected. Restart the container. |
| Network partition | Agent disconnects from Band.ai. Reconnects automatically when network recovers. |
| Git push fails | Coder reports error to Conductor via chat. Conductor can reassign. |

### When to use this mode

- Production workloads
- Teams with infrastructure across multiple clouds
- When you need to scale agents independently (e.g., GPU instances for heavy coders, lightweight instances for Watchdog)
- When you want agent-level fault isolation

---

## Comparison

| | Local | Local Docker | Distributed |
|---|---|---|---|
| **Setup complexity** | Minimal | Low | Medium |
| **Shared filesystem** | Yes | Yes (volumes) | No |
| **Multi-host** | No | No | Yes |
| **Fault isolation** | None (one process) | Container-level | Host-level |
| **Git sync** | Shared bare repo | Shared bare repo | Push/fetch to remote |
| **Monitoring** | `cb` shell embeds feed; `cb log` / `cb feed` outside | Same | `cb --attach` shell or `cb feed` |
| **Config** | `mode: local` (default) | `mode: local` (default) | `mode: distributed` |
| **Interactive start** | `cb` | `cb up` (auto-attaches shell) | `cb --attach` (after agents are running) |
| **Headless start** | `cb run` | `cb up --detach` | `cb run --agent <key>` per host |
