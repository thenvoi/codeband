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
import os
from typing import TYPE_CHECKING

from codeband.models import CLAUDE_OPUS, CLAUDE_SONNET

if TYPE_CHECKING:
    from codeband.config import CodebandConfig

# A worker pool whose entry omits `model` falls back to a default at spawn time.
# That default is Sonnet for every role except coders (Opus) — mirror the
# spawner here (runner.py: reviewers/plan_reviewers `or CLAUDE_SONNET`, coders
# via ClaudePlayerRunner's Opus default). Only the non-Sonnet exception needs an
# entry; everything else — including any pool added later — defaults to Sonnet.
_POOL_MODEL_DEFAULT: dict[str, str] = {"coders": CLAUDE_OPUS}

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PreflightError:
    """A preflight failure — summary describes what happened, remediation
    tells the user how to fix it.

    ``classified`` is True when a known pattern matched the failure text;
    in that case the remediation is self-explanatory and the cli prints
    only that line. False means we couldn't classify it, so the summary
    is the only diagnostic available and must be shown to the user.
    """

    summary: str
    remediation: str
    classified: bool = False


# Ordered list of (lower-case substring, remediation). First match wins.
# Keep phrases short and specific so one real provider error string matches
# exactly one pattern.
_CLAUDE_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        # A 400 the API returns when the request carries an outdated extended-
        # thinking shape (legacy `thinking.type.enabled`). The usual cause is a
        # stale Claude Code CLI bundled inside an old `claude-agent-sdk`. band's
        # adapter swallows this error and emits nothing, so the agent connects
        # but silently does no work — keep this pattern ahead of the generic
        # `invalid_request_error` so the remediation is specific.
        "is not supported for this model",
        (
            "A configured Claude model rejected the request shape. This is almost "
            "always a stale Claude Code CLI bundled inside `claude-agent-sdk` sending "
            "the legacy `thinking.type.enabled` shape that current models reject. "
            "Run `pip install -U claude-agent-sdk`, or pin a model that accepts it in "
            "codeband.yaml. (`cb doctor` reports the bundled CLI version.)"
        ),
    ),
    (
        "invalid_request_error",
        (
            "Anthropic rejected the request as invalid — often a stale Claude Code "
            "CLI bundled inside `claude-agent-sdk` sending an outdated request shape. "
            "Run `pip install -U claude-agent-sdk`, or check the configured model in "
            "codeband.yaml."
        ),
    ),
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
        # Newer Claude CLI wording, e.g.
        # "You've hit your limit · resets 1:10am (America/Los_Angeles)".
        "hit your limit",
        (
            "Claude Pro/Max usage limit reached. Wait for reset, upgrade the "
            "subscription, or fall back to ANTHROPIC_API_KEY."
        ),
    ),
    (
        # Stream-json event the CLI emits on stdout when a Pro/Max usage
        # limit is rejected. Captured by ``utility_llm.one_shot_text`` and
        # appended to the exception message.
        "status=rejected",
        (
            "Claude Pro/Max usage limit reached. Wait for reset, upgrade the "
            "subscription, or fall back to ANTHROPIC_API_KEY."
        ),
    ),
    (
        # ``AssistantMessage.error`` literal from the API — billing path.
        "assistant_message_error=billing_error",
        (
            "Top up at https://console.anthropic.com/settings/billing, or "
            "switch to a Claude Pro/Max OAuth token (run `claude setup-token` "
            "and set CLAUDE_CODE_OAUTH_TOKEN in .env), or — on macOS — "
            "`claude` login to seed the keychain and unset ANTHROPIC_API_KEY."
        ),
    ),
    (
        "assistant_message_error=authentication_failed",
        "Claude authentication failed. Verify ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.",
    ),
    (
        "assistant_message_error=rate_limit",
        "Claude rate limit hit. Wait a moment, or switch auth method.",
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

_CLAUDE_USAGE_LIMIT_PATTERNS = (
    "usage limit reached",
    "hit your limit",
    "status=rejected",
)


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

_CODEX_USAGE_LIMIT_PATTERNS = ("usage limit",)


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
        # Detach stdin: a `codex` build without a real `exec` subcommand falls
        # back to an interactive session and would block on TTY input until the
        # timeout. With no stdin it fails fast instead of hanging the preflight.
        stdin=asyncio.subprocess.DEVNULL,
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
    if _is_codex_usage_limit(haystack) and _restore_openai_api_key_fallback():
        return await check_codex_auth()
    for pattern, remediation in _CODEX_ERROR_PATTERNS:
        if pattern in haystack:
            # Extract a short preview for the summary — truncate long output.
            preview = output.strip().splitlines()[-3:]
            return PreflightError(
                summary=f"Codex auth check failed: {' / '.join(preview)[:300]}",
                remediation=remediation,
                classified=True,
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


def _is_codex_usage_limit(haystack: str) -> bool:
    return any(pattern in haystack for pattern in _CODEX_USAGE_LIMIT_PATTERNS)


def _restore_openai_api_key_fallback() -> bool:
    """Restore stripped OpenAI API-key auth after Codex subscription exhaustion."""
    fallback_key = os.environ.pop("CODEBAND_FALLBACK_OPENAI_API_KEY", "")
    if not fallback_key or os.environ.get("OPENAI_API_KEY"):
        return False
    os.environ["OPENAI_API_KEY"] = fallback_key
    logger.info(
        "Codex subscription usage limit reached; retrying preflight with OPENAI_API_KEY"
    )
    return True


async def check_claude_auth(
    auth_mode: str = "api_key", model: str = CLAUDE_SONNET
) -> PreflightError | None:
    """Send one tiny Claude SDK call to verify auth works end-to-end.

    Returns ``None`` on success; a ``PreflightError`` describing the
    failure otherwise. The probe uses ``utility_llm.one_shot_text`` so it
    exercises the exact same auth path as every coding agent — including the
    ``model``, so a model that rejects the request shape (e.g. a stale bundled
    CLI sending legacy ``thinking.type.enabled``) is caught before agents spawn
    rather than silently swallowed at runtime.

    In the default ``api_key`` mode, an absent ``ANTHROPIC_API_KEY`` is a
    fast, classified failure (no API call): subscription OAuth is never used
    implicitly, so we refuse rather than silently take the Consumer-Terms-
    restricted path. To use a subscription, set ``claude.auth_mode:
    subscription`` in ``codeband.yaml``.
    """
    if auth_mode == "api_key" and not os.environ.get("ANTHROPIC_API_KEY"):
        return PreflightError(
            summary="claude.auth_mode is 'api_key' but ANTHROPIC_API_KEY is not set",
            remediation=(
                "Set ANTHROPIC_API_KEY in .env (Anthropic API — the supported "
                "path for automated/parallel agents).\n"
                "To deliberately use a Claude Pro/Max subscription instead, set "
                "claude.auth_mode: subscription in codeband.yaml — note that "
                "Anthropic's Consumer Terms restrict automated subscription use."
            ),
            classified=True,
        )

    from codeband.utility_llm import one_shot_text

    try:
        result = await one_shot_text("Reply with just: ok", model=model)
    except Exception as exc:
        # Usage-limit, auth, and rate-limit failures surface here too: the
        # CLI exits non-zero and ``one_shot_text`` re-raises with stderr
        # and structured stream-json context appended. Run the same pattern
        # matcher so the user sees the specific remediation, not a generic
        # "check auth" hint.
        message = f"{type(exc).__name__}: {exc}"
        haystack = message.lower()
        if _is_claude_usage_limit(haystack) and _restore_anthropic_api_key_fallback():
            return await check_claude_auth(auth_mode, model)
        for pattern, remediation in _CLAUDE_ERROR_PATTERNS:
            if pattern in haystack:
                return PreflightError(
                    summary=f"Claude auth check failed (model={model}): {exc}",
                    remediation=remediation,
                    classified=True,
                )
        return PreflightError(
            summary=f"Claude SDK call raised (model={model}) {message}",
            remediation=(
                "Check Claude CLI auth (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, "
                "or macOS keychain via `claude` login) and network connectivity."
            ),
            classified=False,
        )

    haystack = result.lower()
    if _is_claude_usage_limit(haystack) and _restore_anthropic_api_key_fallback():
        return await check_claude_auth(auth_mode, model)
    for pattern, remediation in _CLAUDE_ERROR_PATTERNS:
        if pattern in haystack:
            return PreflightError(
                summary=f"Claude auth check failed (model={model}): {result.strip()}",
                remediation=remediation,
                classified=True,
            )

    return None


def _is_claude_usage_limit(haystack: str) -> bool:
    return any(pattern in haystack for pattern in _CLAUDE_USAGE_LIMIT_PATTERNS)


def _restore_anthropic_api_key_fallback() -> bool:
    """Restore stripped API-key auth after subscription usage-limit exhaustion.

    In ``subscription`` mode, ``codeband.cli._resolve_claude_auth`` strips
    ``ANTHROPIC_API_KEY`` so the subscription path wins, but stores a
    process-local backup. This fallback is intentionally narrow: we restore the
    key only after the Claude subscription path reports a usage limit. (In the
    default ``api_key`` mode nothing is stripped, so there's no backup to
    restore and this is a no-op.)
    """
    fallback_key = os.environ.pop("CODEBAND_FALLBACK_ANTHROPIC_API_KEY", "")
    if not fallback_key or os.environ.get("ANTHROPIC_API_KEY"):
        return False
    os.environ["ANTHROPIC_API_KEY"] = fallback_key
    logger.info(
        "Claude subscription usage limit reached; retrying preflight with ANTHROPIC_API_KEY"
    )
    return True


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


def _claude_models(config: CodebandConfig) -> list[str]:
    """Distinct Claude models actually configured across *all* roles, order-preserving.

    Discovered generically from the ``AgentsConfig`` fields so new singleton agents
    or worker pools are probed automatically — there is no role list to keep in sync.
    Two shapes are recognized (the same ones the rest of the code duck-types):

    * singleton agents expose ``.framework`` + ``.model`` (Conductor, Mergemaster);
    * worker pools expose ``.entry_for(framework)`` (planners/coders/reviewers/...).

    A pool entry with ``model=None`` falls back to the same default the spawner uses
    (Opus for coders, Sonnet elsewhere — see ``_POOL_MODEL_DEFAULT``). Each model is
    probed by ``run_preflight`` so one that rejects the request shape (e.g. a stale
    bundled CLI sending legacy ``thinking.type.enabled``) fails fast instead of
    producing a silent, do-nothing agent at runtime.
    """
    from codeband.config import Framework

    agents = config.agents
    models: list[str] = []
    for field_name in type(agents).model_fields:
        value = getattr(agents, field_name)
        if hasattr(value, "framework") and hasattr(value, "model"):
            # Singleton agent.
            if value.framework == Framework.CLAUDE_SDK and value.model:
                models.append(value.model)
        elif hasattr(value, "entry_for"):
            # Worker pool.
            entry = value.entry_for(Framework.CLAUDE_SDK)
            if entry.count > 0:
                models.append(
                    entry.model or _POOL_MODEL_DEFAULT.get(field_name, CLAUDE_SONNET)
                )

    seen: set[str] = set()
    distinct: list[str] = []
    for model in models:
        if model and model not in seen:
            seen.add(model)
            distinct.append(model)
    return distinct


async def run_preflight(config: CodebandConfig) -> PreflightError | None:
    """Run all applicable auth preflight checks concurrently.

    Every distinct Claude model in the config is probed (not just a default),
    so a model that rejects the request shape is caught before agents spawn.
    Codex is checked only when at least one Codex-framework agent is configured.
    The checks are independent CLI cold-starts (~2–5s each) so we run them via
    :func:`asyncio.gather` and return the first error encountered. Claude wins
    ties — its checks are fed to ``gather`` first, so a Claude error is preferred
    over a coincident Codex error.
    """
    import asyncio

    tasks = [
        check_claude_auth(config.claude.auth_mode, model)
        for model in _claude_models(config)
    ]
    if _config_uses_codex(config):
        tasks.append(check_codex_auth())

    results = await asyncio.gather(*tasks)
    for err in results:
        if err is not None:
            return err
    return None
