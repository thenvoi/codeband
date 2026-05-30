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

Codeband defaults to API-key auth and treats subscription OAuth as an explicit opt-in. This is set by `claude.auth_mode` in `codeband.yaml`:

```yaml
claude:
  auth_mode: api_key      # default; or: subscription
```

### `api_key` (default)

Authenticate with `ANTHROPIC_API_KEY`. The Anthropic API runs under Anthropic's Commercial Terms, which permit automated, parallel, programmatic use — the pattern Codeband's agent swarm uses. Subscription OAuth is never used automatically: if `ANTHROPIC_API_KEY` is missing, `cb run` preflight fails fast with a clear message rather than silently falling back to a subscription.

### `subscription` (explicit opt-in)

Bill a Claude Pro/Max subscription. Set `claude.auth_mode: subscription`, then provide a credential, resolved in this order:

1. `CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token`. Required for Docker and CI.
2. Host subscription OAuth:
   - macOS: Keychain entry written by `claude` login.
   - Linux/Windows: `$CLAUDE_CONFIG_DIR/.credentials.json`, defaulting to `~/.claude/.credentials.json`.

In this mode, when `ANTHROPIC_API_KEY` is also set, Codeband strips it from the spawned Claude process (the CLI would otherwise prefer it) and keeps it only as a fallback used if the subscription path reports a usage-limit error.

**Terms note.** Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) (which govern Claude Pro/Max) restrict accessing the service "through automated or non-human means" except via an API key or where Anthropic explicitly permits it. Driving a multi-agent orchestration tool off a subscription is at best a grey area. `subscription` mode is provided for users who knowingly accept that; the supported path for orchestration is `api_key`. `cb doctor` warns whenever `subscription` mode is active.

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
