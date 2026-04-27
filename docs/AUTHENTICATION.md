# Authentication

Codeband coordinates Band.ai agents and shells out to Claude Code, Codex, `git`, and `gh`. This page explains which credentials are used and in what order.

## Required Credentials

At minimum, a full cross-model run needs:

```ini
BAND_API_KEY=band_u_...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

`GH_TOKEN` is recommended for Docker and CI because Codeband uses GitHub PR and issue workflows through `gh`.

## Claude

Claude agents can authenticate in three ways. Codeband resolves them in this order at `cb run` startup:

1. `CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`. Recommended for Docker and CI.
2. Host subscription OAuth:
   - macOS: Keychain entry written by `claude` login.
   - Linux/Windows: `$CLAUDE_CONFIG_DIR/.credentials.json`, defaulting to `~/.claude/.credentials.json`.
3. `ANTHROPIC_API_KEY`, for pay-per-token usage.

When subscription auth is available, Codeband strips `ANTHROPIC_API_KEY` from the spawned Claude process so the Claude CLI does not silently prefer API-key billing over subscription auth. `cb doctor` warns when both are present.

## Codex

Codex agents can use either:

- `OPENAI_API_KEY`, recommended for automation and parallel agents.
- Host login from `codex login --device-auth`, useful for low-volume local runs.

Codex is intentionally API-key-first. If `OPENAI_API_KEY` and subscription login are both present, the API key wins.

For Docker, mount or provide credentials explicitly:

- Set `OPENAI_API_KEY` in `.env`, or
- Bind-mount `~/.codex/auth.json` into the container environment.

## Band.ai

`BAND_API_KEY` is required for task submission, agent setup, WebSocket communication, and memory backend detection.

Paid/enterprise Band.ai accounts can use:

```bash
cb setup-agents
```

Free-tier accounts may need manual agent creation. See [Configuration](CONFIGURATION.md#manual-agent-registration-free-tier).

## GitHub

Codeband uses `gh` for PR and issue workflows.

Local development can use an interactive `gh auth login`. For Docker and CI, set:

```ini
GH_TOKEN=ghp_...
```

Use a token with the minimum repository permissions needed for the target workflow.

## Preflight

`cb run` makes a tiny call through each configured framework CLI before spawning agents. This catches billing, login, quota, and rate-limit failures at startup instead of letting the swarm stall later.

Skip preflight only when you intentionally need to run offline or in a constrained CI job:

```bash
cb run --skip-preflight
```

## Docker Caveat

Containers cannot read the host macOS Keychain. For Docker mode, put one of these in `.env`:

```ini
CLAUDE_CODE_OAUTH_TOKEN=...
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GH_TOKEN=...
```

`cb up` forwards `.env` into the containers.
