"""Startup auth preflight — fail fast on auth/billing errors instead of
letting Anthropic's error text land silently as the Conductor's reply in
a Band.ai chat room.

The Claude Code CLI surfaces provider errors as plain assistant text
("Credit balance is too low", "Please run /login", etc.) with no
distinguishing structured signal. Without this check, the symptom is a
silent idle swarm: the Conductor "responds" with the error string, the
watchdog stays happy, and nothing ever gets done.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeband.config import CodebandConfig

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PreflightError:
    """A preflight failure — summary describes what happened, remediation
    tells the user how to fix it."""

    summary: str
    remediation: str


# Ordered list of (lower-case substring, remediation). First match wins.
# Keep phrases short and specific so one real provider error string matches
# exactly one pattern.
_CLAUDE_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        "credit balance is too low",
        (
            "Top up at https://console.anthropic.com/settings/billing, or "
            "switch to a Claude Pro/Max OAuth token (run `claude setup-token` "
            "and set CLAUDE_CODE_OAUTH_TOKEN in .env), or — on macOS — "
            "`claude` login to seed the keychain and unset ANTHROPIC_API_KEY."
        ),
    ),
    (
        "invalid x-api-key",
        "Anthropic rejected ANTHROPIC_API_KEY as invalid. Check the value in .env.",
    ),
    (
        "invalid api key",
        "Anthropic rejected ANTHROPIC_API_KEY as invalid. Check the value in .env.",
    ),
    (
        "please run /login",
        (
            "Claude CLI requests re-login. Run `claude setup-token` and put the "
            "result in CLAUDE_CODE_OAUTH_TOKEN, or `claude` login on macOS to "
            "seed the keychain (then unset ANTHROPIC_API_KEY)."
        ),
    ),
    (
        "usage limit reached",
        (
            "Claude Pro/Max usage limit reached. Wait for reset, upgrade the "
            "subscription, or fall back to ANTHROPIC_API_KEY."
        ),
    ),
    (
        "rate_limit_error",
        "Claude rate limit hit. Wait a moment, or switch auth method.",
    ),
    (
        "authentication_error",
        "Claude authentication failed. Verify ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.",
    ),
]


# Codex failures bubble up through the CLI's stdout/stderr rather than as
# typed errors — same pattern-matching approach as Claude.
_CODEX_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        "not logged in",
        (
            "Run `codex login --device-auth` on this host (ChatGPT subscription) "
            "or set OPENAI_API_KEY in .env (pay-per-token, recommended for "
            "parallel-agent workloads)."
        ),
    ),
    (
        "rate limit",
        (
            "Codex rate limit hit. For parallel-agent workloads, OPENAI_API_KEY "
            "(pay-per-token) avoids the tighter subscription caps."
        ),
    ),
    (
        "usage limit",
        (
            "Codex usage limit reached on this ChatGPT subscription. Wait for "
            "reset, or set OPENAI_API_KEY in .env for pay-per-token access."
        ),
    ),
    (
        "invalid api key",
        "OpenAI rejected OPENAI_API_KEY. Check the value in .env.",
    ),
    (
        "401 unauthorized",
        "OpenAI rejected OPENAI_API_KEY. Check the value in .env.",
    ),
    (
        "session expired",
        (
            "Codex session expired. Re-run `codex login --device-auth`, or set "
            "OPENAI_API_KEY in .env."
        ),
    ),
]


async def _run_codex_probe() -> tuple[int, str]:
    """Run a minimal ``codex exec`` and return ``(returncode, combined_output)``.

    Isolated so tests can mock the subprocess cleanly.
    """
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        "codex",
        "exec",
        "--skip-git-repo-check",
        "Reply with just: ok",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
    return proc.returncode or 0, stdout.decode(errors="replace")


async def check_codex_auth() -> PreflightError | None:
    """Send one tiny ``codex exec`` to verify auth / quota are usable.

    Only called when the config has at least one Codex-framework agent — for
    a Claude-only pool we skip this to avoid a wasted CLI call.
    """
    import asyncio

    try:
        returncode, output = await _run_codex_probe()
    except FileNotFoundError:
        return PreflightError(
            summary="`codex` CLI not found on PATH",
            remediation=(
                "Install via `brew install codex` (or equivalent) and run "
                "`codex login --device-auth`, or remove Codex agents from "
                "codeband.yaml if you don't use them."
            ),
        )
    except asyncio.TimeoutError:
        return PreflightError(
            summary="Codex auth check timed out",
            remediation=(
                "Codex CLI did not respond within 45s. Check network / OpenAI "
                "status, or verify `codex exec` runs manually."
            ),
        )
    except OSError as exc:
        return PreflightError(
            summary=f"Codex CLI invocation failed: {exc}",
            remediation="Verify `codex` is on PATH and executable.",
        )

    haystack = output.lower()
    for pattern, remediation in _CODEX_ERROR_PATTERNS:
        if pattern in haystack:
            # Extract a short preview for the summary — truncate long output.
            preview = output.strip().splitlines()[-3:]
            return PreflightError(
                summary=f"Codex auth check failed: {' / '.join(preview)[:300]}",
                remediation=remediation,
            )
    # Non-zero exit without a recognized pattern is still a failure signal.
    if returncode != 0:
        preview = output.strip().splitlines()[-3:]
        return PreflightError(
            summary=f"Codex probe exited {returncode}: {' / '.join(preview)[:300]}",
            remediation=(
                "Unexpected Codex CLI failure. Run `codex exec --skip-git-repo-check "
                "ok` manually to diagnose."
            ),
        )
    return None


async def check_claude_auth() -> PreflightError | None:
    """Send one tiny Claude SDK call to verify auth works end-to-end.

    Returns ``None`` on success; a ``PreflightError`` describing the
    failure otherwise. The probe uses ``utility_llm.one_shot_text`` so it
    exercises the exact same auth path as every coding agent.
    """
    from codeband.utility_llm import one_shot_text

    try:
        result = await one_shot_text("Reply with just: ok")
    except Exception as exc:
        return PreflightError(
            summary=f"Claude SDK call raised {type(exc).__name__}: {exc}",
            remediation=(
                "Check Claude CLI auth (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, "
                "or macOS keychain via `claude` login) and network connectivity."
            ),
        )

    haystack = result.lower()
    for pattern, remediation in _CLAUDE_ERROR_PATTERNS:
        if pattern in haystack:
            return PreflightError(
                summary=f"Claude auth check failed: {result.strip()}",
                remediation=remediation,
            )

    return None


def _config_uses_codex(config: CodebandConfig) -> bool:
    """True if any role in the config runs on the Codex framework.

    Used to scope the Codex preflight — no point shelling out to ``codex exec``
    on a Claude-only pool.
    """
    from codeband.config import Framework

    agents = config.agents
    for pool_name in ("planners", "plan_reviewers", "coders", "reviewers"):
        pool = getattr(agents, pool_name)
        if pool.entry_for(Framework.CODEX).count > 0:
            return True
    return (
        agents.conductor.framework == Framework.CODEX
        or agents.mergemaster.framework == Framework.CODEX
    )


async def run_preflight(config: CodebandConfig) -> PreflightError | None:
    """Run all applicable auth preflight checks concurrently.

    Claude is always checked. Codex is checked only when at least one
    Codex-framework agent is configured. Both checks are independent CLI
    cold-starts (~2–5s each) so we run them via :func:`asyncio.gather`
    and return the first error encountered. Claude wins ties — its check
    is fed to ``gather`` first, so a Claude error appears at index 0 and
    is preferred over a coincident Codex error.
    """
    import asyncio

    tasks = [check_claude_auth()]
    if _config_uses_codex(config):
        tasks.append(check_codex_auth())

    results = await asyncio.gather(*tasks)
    for err in results:
        if err is not None:
            return err
    return None
