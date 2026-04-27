# Configuration

`cb init --repo <url>` writes a default `codeband.yaml` designed for the free-tier Band.ai 10-agent cap. The default uses eight Band.ai agents plus one in-process Watchdog.

## Default `codeband.yaml`

```yaml
repo:
  url: "https://github.com/myorg/myrepo.git"
  branch: "main"

agents:
  conductor:   { framework: claude_sdk, model: claude-sonnet-4-6 }
  mergemaster:
    framework: claude_sdk
    model: claude-sonnet-4-6
    test_command: "pytest"
    auto_merge: "low"

  planners:
    claude_sdk: { count: 1, model: claude-sonnet-4-6 }
    codex:      { count: 0 }
  plan_reviewers:
    claude_sdk: { count: 0 }
    codex:      { count: 1, model: gpt-5.4 }
    # review_guidelines: "Optional project-wide plan-review policy"
  coders:
    claude_sdk:
      count: 1
      model: claude-opus-4-7
      description: "Strong at refactoring, testing, complex logic"
      restart_delay_seconds: 5.0
    codex:
      count: 1
      model: gpt-5.4
      description: "Fast at bulk generation, boilerplate"
  reviewers:
    claude_sdk: { count: 1, model: claude-sonnet-4-6 }
    codex:      { count: 1, model: gpt-5.4 }
    # review_guidelines: "All public functions need docstrings. No raw SQL."

  watchdog:
    check_interval_seconds: 120
    stale_threshold_seconds: 300
    nudge_grace_seconds: 60

workspace:
  path: ".codeband"
  worktree_prefix: "codeband"
  mode: "local"

band:
  rest_url: "https://app.band.ai"
  ws_url: "wss://app.band.ai/api/v1/socket/websocket"
  memory_mode: "auto"
```

## Agent Count

The default pool is:

| Role | Count |
|------|------:|
| Conductor | 1 |
| Mergemaster | 1 |
| Planner | 1 |
| Plan Reviewer | 1 |
| Coders | 2 |
| Reviewers | 2 |

Total: 8 Band.ai agents. The Watchdog is an in-process daemon and does not use a Band.ai agent seat.

## Frameworks

| Framework | Backed by | Typical use |
|-----------|-----------|-------------|
| `claude_sdk` | Claude Code | Complex reasoning, refactoring, careful stepwise work |
| `codex` | Codex | Bulk generation, boilerplate, fast iteration |

Every role can use either framework. The default keeps Conductor and Mergemaster on Claude, pairs a Claude Planner with a Codex Plan Reviewer, and keeps one Coder and one Reviewer from each framework.

## Cross-Model Pairing

The Conductor enforces adversarial pairing at runtime:

- Claude Coder PRs route to Codex Reviewers.
- Codex Coder PRs route to Claude Reviewers.
- Claude plans route to Codex Plan Reviewers.
- Codex plans route to Claude Plan Reviewers.

If an opposite-framework reviewer is unavailable, Codeband falls back to same-framework review with a warning. `cb doctor` warns when configuration makes cross-model pairing impossible.

## Scaling

Use `cb scale` to adjust pool sizes:

```bash
cb scale coders.claude_sdk=4
cb scale coders.codex=0
cb scale reviewers.codex=2
```

After scaling:

```bash
cb setup-agents
cb
```

`cb scale` prints the new total agent count and warns if the config exceeds the free-tier 10-agent cap.

## Review Guidelines

Add project-specific guidance at the pool level:

```yaml
reviewers:
  claude_sdk: { count: 1 }
  codex:      { count: 1 }
  review_guidelines: "All public functions need docstrings. No raw SQL."

plan_reviewers:
  claude_sdk: { count: 0 }
  codex:      { count: 1 }
  review_guidelines: "Reject plans that assign the same file to multiple coders."
```

## Merge Policy

The Code Reviewer assigns a risk level to every PR:

| Risk | Examples | Default behavior |
|------|----------|------------------|
| Low | Docs, tests, config, cosmetic fixes | Auto-merge |
| Medium | New features with tests, moderate logic | Human approval |
| High | Security-sensitive code, public API changes | Human approval |
| Critical | Auth, payments, deletion, infrastructure | Human approval |

Control auto-merge with:

```yaml
agents:
  mergemaster:
    auto_merge: "low"  # all | low | medium | none
```

## Memory Backend

Codeband probes Band.ai memory at startup:

| Tier | Backend | Multi-host |
|------|---------|------------|
| Paid Band.ai | Band.ai memory REST API | Yes |
| Free Band.ai | Local JSONL at `workspace/state/memories.jsonl` | No |

Force a backend when debugging:

```bash
export BAND_MEMORY_MODE=local  # band | local | auto
```

or:

```yaml
band:
  memory_mode: local
```

## Manual Agent Registration (Free Tier)

If `cb setup-agents` is unavailable, create these eight agents in the Band.ai web UI:

| Role | Recommended Band.ai name |
|------|--------------------------|
| Conductor | `Conductor` |
| Mergemaster | `Mergemaster` |
| Claude planner | `Planner-Claude-0` |
| Codex plan reviewer | `Plan-Reviewer-Codex-0` |
| Claude coder | `Coder-Claude-0` |
| Codex coder | `Coder-Codex-0` |
| Claude code reviewer | `Reviewer-Claude-0` |
| Codex code reviewer | `Reviewer-Codex-0` |

Then create `agent_config.yaml` next to `codeband.yaml`:

```yaml
agents:
  conductor:
    agent_id: <paste from Band.ai>
    api_key:  <paste from Band.ai>
  mergemaster:
    agent_id: ...
    api_key:  ...
  planner-claude_sdk-0:
    agent_id: ...
    api_key:  ...
  plan_reviewer-codex-0:
    agent_id: ...
    api_key:  ...
  coder-claude_sdk-0:
    agent_id: ...
    api_key:  ...
  coder-codex-0:
    agent_id: ...
    api_key:  ...
  reviewer-claude_sdk-0:
    agent_id: ...
    api_key:  ...
  reviewer-codex-0:
    agent_id: ...
    api_key:  ...
```

The keys on the left are load-bearing. They must match the configured role, framework, and zero-based index exactly.
