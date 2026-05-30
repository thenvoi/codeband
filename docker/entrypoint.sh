#!/usr/bin/env bash
# Codeband agent entrypoint
#
# Initializes the git workspace (bare clone + worktree) before starting the agent.
# Environment variables:
#   AGENT_KEY       - Agent key (e.g., "conductor", "coder-claude_sdk-0", "mergemaster")
#   AGENT_ROLE      - Agent role (conductor, coder, reviewer, planner, plan_reviewer, mergemaster, watchdog)
#   WORKSPACE       - Workspace root (default: /workspace)
#   REPO_URL        - Git repository URL (falls back to codeband.yaml if unset)
#   REPO_BRANCH     - Branch to base work on (falls back to codeband.yaml, default: main)
#   WORKTREE_PREFIX - Prefix for worktree branches (default: codeband)
#   CODEBAND_CONFIG - Path to codeband.yaml (default: /app/config/codeband.yaml)

set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
BARE_REPO="${WORKSPACE}/repo.git"
WORKTREES="${WORKSPACE}/worktrees"
WORKTREE_PREFIX="${WORKTREE_PREFIX:-codeband}"
CODEBAND_CONFIG="${CODEBAND_CONFIG:-/app/config/codeband.yaml}"

# Read REPO_URL and REPO_BRANCH from codeband.yaml if not set in environment
# Use the venv Python (has PyYAML installed) rather than system python3.
VENV_PYTHON="${VENV_PYTHON:-/app/.venv/bin/python}"
if [ -z "${REPO_URL:-}" ] && [ -f "${CODEBAND_CONFIG}" ]; then
    REPO_URL=$("${VENV_PYTHON}" -c "import yaml; c=yaml.safe_load(open('${CODEBAND_CONFIG}')); print(c['repo']['url'])" 2>/dev/null || true)
fi
if [ -z "${REPO_BRANCH:-}" ] && [ -f "${CODEBAND_CONFIG}" ]; then
    REPO_BRANCH=$("${VENV_PYTHON}" -c "import yaml; c=yaml.safe_load(open('${CODEBAND_CONFIG}')); print(c['repo'].get('branch','main'))" 2>/dev/null || true)
fi
REPO_BRANCH="${REPO_BRANCH:-main}"

echo "=== Codeband Agent Entrypoint ==="
echo "Agent: ${AGENT_KEY:-unknown}"
echo "Role:  ${AGENT_ROLE:-unknown}"
echo "Workspace: ${WORKSPACE}"

# ── Validate Claude Code authentication ────────────────────────────────
# Codeband defaults to API-key auth (claude.auth_mode: api_key). The Claude
# CLI checks ANTHROPIC_API_KEY before CLAUDE_CODE_OAUTH_TOKEN, so in api_key
# mode we keep the key and the CLI uses it natively. Only in the explicit
# `subscription` opt-in do we unset the key so the CLI falls through to OAuth.
CLAUDE_AUTH_MODE="${CLAUDE_AUTH_MODE:-}"
if [ -z "${CLAUDE_AUTH_MODE}" ] && [ -f "${CODEBAND_CONFIG}" ]; then
    CLAUDE_AUTH_MODE=$("${VENV_PYTHON}" -c "import yaml; c=yaml.safe_load(open('${CODEBAND_CONFIG}')) or {}; print((c.get('claude') or {}).get('auth_mode','api_key'))" 2>/dev/null || echo "api_key")
fi
CLAUDE_AUTH_MODE="${CLAUDE_AUTH_MODE:-api_key}"

if [ "${CLAUDE_AUTH_MODE}" = "subscription" ] && [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Claude auth (subscription mode): both keys set; unsetting ANTHROPIC_API_KEY to use CLAUDE_CODE_OAUTH_TOKEN"
    unset ANTHROPIC_API_KEY
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Claude auth: ANTHROPIC_API_KEY configured (API key, auth_mode=${CLAUDE_AUTH_MODE})"
elif [ "${CLAUDE_AUTH_MODE}" = "subscription" ] && [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "Claude auth: CLAUDE_CODE_OAUTH_TOKEN configured (subscription mode)"
elif [ "${CLAUDE_AUTH_MODE}" = "api_key" ]; then
    echo "WARNING: auth_mode=api_key but ANTHROPIC_API_KEY is not set."
    echo "  Set ANTHROPIC_API_KEY in .env, or set claude.auth_mode: subscription in codeband.yaml"
    echo "  and provide CLAUDE_CODE_OAUTH_TOKEN (from: claude setup-token)."
else
    echo "WARNING: No Claude Code authentication found."
    echo "  auth_mode=subscription requires CLAUDE_CODE_OAUTH_TOKEN (run: claude setup-token)."
fi

# ── Configure GitHub auth for gh CLI ────────────────────────────────────
if [ -z "${GH_TOKEN:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
    export GH_TOKEN="${GITHUB_TOKEN}"
fi

if [ -n "${GH_TOKEN:-}" ]; then
    echo "GitHub auth: GH_TOKEN configured for gh CLI"
elif [ "${AGENT_ROLE:-}" = "coder" ] || [ "${AGENT_ROLE:-}" = "reviewer" ] || [ "${AGENT_ROLE:-}" = "mergemaster" ]; then
    echo "GitHub auth: no GH_TOKEN/GITHUB_TOKEN configured; gh CLI commands may fail"
fi

# ── Configure git credentials (mounted from host) ───────────────────────
if [ -f "${HOME}/.git-credentials" ] && [ -s "${HOME}/.git-credentials" ]; then
    git config --global credential.helper store
fi

# ── Configure SSH auth (forwarded from host) ────────────────────────────
if [ -S "/run/ssh-agent" ]; then
    export SSH_AUTH_SOCK=/run/ssh-agent
fi

# Clone into a bare repo, handling pre-existing empty directories (Docker volumes).
_clone_bare_repo() {
    git init --bare "${BARE_REPO}"
    git -C "${BARE_REPO}" remote add origin "${REPO_URL}"
    git -C "${BARE_REPO}" fetch origin
    # Point HEAD at the configured branch
    git -C "${BARE_REPO}" symbolic-ref HEAD "refs/heads/${REPO_BRANCH}"
}

# ── Clone bare repo (if not already cloned) ──────────────────────────────
DEPLOYMENT_MODE="${DEPLOYMENT_MODE:-local}"

if [ -n "${REPO_URL:-}" ] && [ ! -f "${BARE_REPO}/HEAD" ]; then
    echo "Cloning bare repo: ${REPO_URL}"
    if [ "${DEPLOYMENT_MODE}" = "distributed" ]; then
        # Distributed mode: each agent clones independently, no shared volume
        _clone_bare_repo
    else
        # Local mode: shared volume, use lock to prevent concurrent clones
        LOCK="${WORKSPACE}/.clone.lock"
        exec 200>"${LOCK}"
        if flock -n 200; then
            # Re-check after acquiring lock — another container may have cloned
            if [ ! -f "${BARE_REPO}/HEAD" ]; then
                _clone_bare_repo
                echo "Clone complete."
            else
                echo "Repo already cloned by another agent."
            fi
        else
            echo "Another agent is cloning. Waiting for lock..."
            flock 200
            echo "Lock acquired — repo should be ready."
        fi
        exec 200>&-
    fi
fi

# ── Create worktree for this agent ───────────────────────────────────────
if [ "${AGENT_ROLE:-}" = "coder" ] || [ "${AGENT_ROLE:-}" = "mergemaster" ]; then
    AGENT_KEY="${AGENT_KEY:-agent}"
    WT_PATH="${WORKTREES}/${AGENT_KEY}"

    if [ ! -d "${WT_PATH}" ]; then
        if [ "${AGENT_ROLE}" = "mergemaster" ]; then
            BRANCH="${REPO_BRANCH}"
        else
            BRANCH="${WORKTREE_PREFIX}/${AGENT_KEY}/workspace"
        fi

        echo "Creating worktree: ${WT_PATH} (branch: ${BRANCH})"

        # Check if branch exists
        if git -C "${BARE_REPO}" rev-parse --verify "${BRANCH}" >/dev/null 2>&1; then
            git -C "${BARE_REPO}" worktree add "${WT_PATH}" "${BRANCH}"
        else
            git -C "${BARE_REPO}" worktree add -b "${BRANCH}" "${WT_PATH}"
        fi

        echo "Worktree created."
    fi

    # Set working directory for the agent
    export AGENT_CWD="${WT_PATH}"
fi

# ── Ensure shared directories exist ──────────────────────────────────────
mkdir -p "${WORKSPACE}/notes" "${WORKSPACE}/state"

# ── Codex authentication ────────────────────────────────────────────────
# Host ~/.codex is only bind-mounted (read-only) into codex-capable services
# — coders, code_reviewer, plan_reviewer, planner — via the compose files.
# Conductor, mergemaster, and watchdog never see the host credential file
# (their compose entries inherit &volumes-base, not &volumes-codex).
#
# Within a codex-capable container, we further gate on the configured
# framework: only roles whose codeband.yaml framework is "codex" touch the
# mount. Everyone else skips the block below entirely.
#
# When active, the entrypoint copies host ~/.codex/auth.json once into a
# container-local CODEX_HOME (/tmp/codex-auth) and points Codex at that
# copy. All subsequent writes (API-key login, OAuth refresh rotations) stay
# container-local — the host auth.json is never mutated. Trade-off: OAuth
# refresh-token rotations do not persist back to the host, so subscription
# users will eventually need to re-login on the host when the refresh
# token expires (weeks-to-months timescale).
_detect_uses_codex() {
    # Echo "1" if this worker's framework is codex, else "0".
    # Falls back to "0" on any parse error.
    #
    # Worker identity encodes framework directly: pool AGENT_KEYs look like
    # `coder-codex-0`, `reviewer-claude_sdk-1`, `plan_reviewer-codex-0`,
    # `planner-claude_sdk-0`. Singletons (`conductor`, `mergemaster`) read
    # their framework from the config directly.
    "${VENV_PYTHON}" - "${CODEBAND_CONFIG}" "${AGENT_ROLE:-}" "${AGENT_KEY:-}" <<'PYEOF' 2>/dev/null || echo "0"
import sys, yaml
try:
    _, cfg_path, role, key = sys.argv
    framework = None
    # Pool workers: framework is in the key (second-to-last segment).
    if role in ("coder", "reviewer", "plan_reviewer", "planner"):
        parts = key.rsplit("-", 2)
        if len(parts) == 3:
            framework = parts[1]
    else:
        # Singletons read framework from codeband.yaml.
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        agents = cfg.get("agents", {}) or {}
        if role in ("conductor", "mergemaster"):
            framework = (agents.get(role, {}) or {}).get("framework", "claude_sdk")
    print("1" if framework == "codex" else "0")
except Exception:
    print("0")
PYEOF
}

if command -v codex >/dev/null 2>&1 && [ -f "${CODEBAND_CONFIG}" ]; then
    USES_CODEX="$(_detect_uses_codex)"
else
    USES_CODEX="0"
fi

if [ "${USES_CODEX}" = "1" ]; then
    export CODEX_HOME="/tmp/codex-auth"
    mkdir -p "${CODEX_HOME}"
    chmod 700 "${CODEX_HOME}"
    HOST_CODEX_AUTH="${HOME}/.codex/auth.json"
    if [ -f "${HOST_CODEX_AUTH}" ]; then
        cp "${HOST_CODEX_AUTH}" "${CODEX_HOME}/auth.json"
    fi
    CODEX_AUTH_FILE="${CODEX_HOME}/auth.json"
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        echo "Codex auth: OPENAI_API_KEY (API key, container-local)"
        echo "${OPENAI_API_KEY}" | codex login --with-api-key 2>/dev/null || true
    elif [ -f "${CODEX_AUTH_FILE}" ] && grep -q '"auth_mode": *"ChatGPT"' "${CODEX_AUTH_FILE}" 2>/dev/null; then
        echo "Codex auth: ChatGPT subscription (copied from host ~/.codex)"
    elif [ -f "${CODEX_AUTH_FILE}" ] && grep -q '"OPENAI_API_KEY"' "${CODEX_AUTH_FILE}" 2>/dev/null; then
        echo "Codex auth: API key (copied from host ~/.codex)"
    else
        echo "WARNING: No Codex authentication found."
        echo "  Set OPENAI_API_KEY in .env, or run 'codex login --device-auth'"
        echo "  on the host to use a ChatGPT Pro/Plus subscription."
    fi
fi

# ── Run the agent ────────────────────────────────────────────────────────
echo "Starting agent process..."
exec "$@"
