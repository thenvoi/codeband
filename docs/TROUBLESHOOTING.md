# Troubleshooting

Start with:

```bash
cb doctor
```

`cb doctor` is read-only. It checks Python, Claude/Codex/Band credentials, `git`, `gh`, config files, expected agent roles, workspace writability, Band.ai connectivity, and the active memory backend.

## Common Errors

### `FileNotFoundError: codeband.yaml not found`

Run:

```bash
cb init --repo <url>
```

### `FileNotFoundError: agent_config.yaml not found`

Register agents:

```bash
cb setup-agents
```

On free-tier Band.ai, manually create the agents and write `agent_config.yaml`. See [Configuration](CONFIGURATION.md#manual-agent-registration-free-tier).

### `ValueError: BAND_API_KEY environment variable is required`

Add this to `.env`:

```ini
BAND_API_KEY=band_u_...
```

### `cb setup-agents` fails with `403` or `forbidden`

Your Band.ai account likely does not have access to the agent-registration API. Use manual registration.

### Claude or Codex login failures

Run:

```bash
cb doctor
```

Then check [Authentication](AUTHENTICATION.md) for credential precedence. Most failures are caused by missing framework CLIs, expired local login, missing API keys in Docker, or quota/rate-limit issues.

### Coder keeps crashing and restarting

Coders reconnect via `WorkerSupervisor`. Check:

```bash
cb log --agent coder-claude_sdk-0
```

Also inspect the worker state file under the configured workspace, for example:

```bash
.codeband/state/coder-claude_sdk-0.json
```

Tune restart pacing in `codeband.yaml`:

```yaml
agents:
  coders:
    claude_sdk:
      restart_delay_seconds: 5.0
```

### Agents appear stuck

Watch the live feed:

```bash
cb feed --no-thoughts
```

The Watchdog nudges idle agents after `stale_threshold_seconds` and escalates to the Conductor if progress does not resume.

### Git worktree errors after a crash

Inspect worktrees:

```bash
git -C .codeband/repo.git worktree list
```

Remove a broken worktree:

```bash
git -C .codeband/repo.git worktree remove --force <path>
```

If local state is disposable, delete the workspace and let Codeband rebuild it on the next run.

### `cb up` cannot find `docker-compose.yml`

`cb up` needs the Docker assets from the source repository. If you installed Codeband from PyPI (via `uv tool`, `pipx`, or `pip`), clone the repository and run from that checkout, or copy the `docker/` directory into your project.

### Git clone or fetch timeouts

Git operations time out after 120 seconds. For large repositories, test the remote directly:

```bash
git clone --bare <url> /tmp/codeband-test.git
```

### `ValueError: Unknown framework`

Only these framework keys are supported:

- `claude_sdk`
- `codex`

Check pool entries in `codeband.yaml` and keys in `agent_config.yaml`.

## Debugging Checklist

1. Run `cb doctor`.
2. Confirm `.env` is loaded in the same shell or container where Codeband runs.
3. Confirm `agent_config.yaml` keys match `codeband.yaml`.
4. Confirm `gh auth status` works for the target repository.
5. Confirm the memory backend matches your deployment mode.
6. Check `cb feed --no-thoughts` and `cb log`.
