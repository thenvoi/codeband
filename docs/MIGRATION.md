# Migration: v0 → v1 (worker pool)

Codeband's v1 release swaps the fixed-N `players:` list for a **worker pool** and makes adversarial cross-model review the default. This is a one-way config change with no compat shim. If you're running a v0 project (single `players:` list, `planner: {model: ...}`, `code_reviewer: {framework, ...}`), follow the steps below.

## The change at a glance

| v0 | v1 |
|----|----|
| `players: [{key: player-0, framework: claude_sdk, ...}, ...]` | `coders: {claude_sdk: {count: N, ...}, codex: {count: N, ...}}` |
| `planner: {model: ...}` (always Claude) | `planners: {claude_sdk: {count: N, model: ...}, codex: {count: N, ...}}` |
| `plan_reviewer: {framework: ..., model: ..., review_guidelines: ...}` | `plan_reviewers: {claude_sdk: {count: N}, codex: {count: N}, review_guidelines: ...}` |
| `code_reviewer: {framework: ..., model: ..., review_guidelines: ...}` | `reviewers: {claude_sdk: {count: N}, codex: {count: N}, review_guidelines: ...}` |
| `conductor: {model: ...}` | `conductor: {framework: claude_sdk, model: ...}` |
| `mergemaster: {model: ..., test_command: ..., auto_merge: ...}` | `mergemaster: {framework: claude_sdk, model: ..., test_command: ..., auto_merge: ...}` |
| Agent keys in `agent_config.yaml`: `planner`, `conductor`, `code_reviewer`, `plan_reviewer`, `mergemaster`, `player-0`, `player-1`, … | Agent keys: `conductor`, `mergemaster`, `planner-claude_sdk-0`, `plan_reviewer-codex-0`, `coder-claude_sdk-0`, `coder-codex-0`, `reviewer-claude_sdk-0`, `reviewer-codex-0`, … |
| Band.ai agent names: `Planner`, `Player-0`, `Player-1`, `Code Reviewer`, `Plan Reviewer` | Band.ai agent names: `Planner-Claude-0`, `Plan-Reviewer-Codex-0`, `Coder-Claude-0`, `Coder-Codex-0`, `Reviewer-Claude-0`, `Reviewer-Codex-0` (singletons `Conductor`, `Mergemaster` unchanged) |
| `cb scale N` | `cb scale <pool>.<framework>=<count>` |
| Dual-player mode (Planner assigns same subtask to two players) | Removed — cross-model code review is the adversarial signal now |

## Step-by-step migration

### 1. Back up your project config

```bash
cp codeband.yaml codeband.yaml.v0.bak
cp agent_config.yaml agent_config.yaml.v0.bak
```

### 2. Rewrite `codeband.yaml`

The cleanest path is to regenerate with `cb init` and re-apply your custom settings:

```bash
# In a throwaway dir:
cb init --repo <same-repo-url>
cat codeband.yaml           # see the v1 shape
```

Then port your v0 customizations into the v1 yaml. Common translations:

- **2 Claude players + 1 Codex player** → `coders: {claude_sdk: {count: 2}, codex: {count: 1}}`
- **Claude code reviewer with review guidelines** → `reviewers: {claude_sdk: {count: 1}, review_guidelines: "..."}`
- **`planner.model = "claude-opus-4-7"`** → `planners: {claude_sdk: {count: 1, model: "claude-opus-4-7"}}`
- **Player descriptions** → per-framework description on the `coders.{framework}` entry (shared by all coders of that framework)
- **`max_restarts` / `restart_delay_seconds`** → per-framework entry on `coders.{framework}` (applies to all coders of that framework)

### 3. Re-register Band.ai agents

The v1 naming convention is different (`Coder-Claude-0` instead of `Player-0`, etc.), so Band.ai needs new agents.

**Paid/enterprise Band.ai:**

```bash
cb setup-agents
```

This auto-detects legacy v0 agents (`Player-0`, `Planner`, `Code Reviewer`, etc.) as excess and deletes them, then registers the v1 pool identities. Your old `agent_config.yaml` should be deleted or renamed first so `setup-agents` creates fresh credentials:

```bash
mv agent_config.yaml agent_config.yaml.v0.bak
cb setup-agents
```

**Free-tier Band.ai:** Delete the old agents in the Band.ai UI, create new ones with the v1 names, and write `agent_config.yaml` by hand. See [Configuration: Manual Agent Registration](CONFIGURATION.md#manual-agent-registration-free-tier).

### 4. Clean up the workspace

v1 uses different worktree directory names. The simplest path is to delete the workspace and let Codeband rebuild it:

```bash
rm -rf .codeband/          # or wherever your workspace.path points
```

v0 worktree directories (`workspace/worktrees/player-0/`, `planner/`, etc.) are no longer recognized. They won't break anything, just waste disk.

### 5. Verify

```bash
cb doctor
```

Doctor will catch any remaining stragglers (missing agent keys, config mismatches, cross-model pairing gaps). Fix anything flagged and you're good.

### 6. (Optional) Re-customize prompts

If you had custom prompts in `./prompts/` (project overrides), review them against the v1 package prompts:

- **`planner.md`**: no more "Player Roster" — it's a "Worker Pool Roster" now; subtasks don't name specific players, they emit `framework_hint` and let the Conductor allocate.
- **`conductor.md`**: new "Worker Pool" + "Allocation" sections; dual-player coordination removed.
- **`player.md`**: renamed role to "Coder"; identity is `Coder-<Framework>-<N>`; report completion to Conductor only (no longer @mentions Code Reviewer directly).
- **`code_reviewer.md`**: reviewer is allocated by Conductor per-PR with cross-model pairing; reviewer is now `Reviewer-<Framework>-<N>`.
- **`plan_reviewer.md`**: minor — dual-player validation dropped.
- **`mergemaster.md`**: dual-player comparison section dropped; batch+bisect unchanged.

If your customizations were thin, you can delete `./prompts/` and let the v1 package prompts take over.

## What breaks if you skip this?

- `cb run` fails at startup with `AttributeError` on `config.agents.players` (doesn't exist).
- `cb doctor` will catch most issues first: unknown config fields, missing pool keys in agent_config, cross-model pairing warnings.
- If you do `cb setup-agents` against a v0 config, you'll get a Pydantic validation error before anything hits Band.ai.

## Why no compat shim?

Alpha stage, small user base, and the config shapes differ too much to translate cleanly at runtime (v0's `players: [...]` list loses framework-pool structure when mapped onto v1's framework-keyed capacity). A one-page migration is cheaper than maintaining dual parsing paths forever.
