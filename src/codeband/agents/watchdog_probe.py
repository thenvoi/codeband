"""Probe whether the Band.ai human API is available on this tier.

An enterprise-plan Band.ai account responds to `list_my_chats()` with data.
A free-tier account fails with an HTTP error (commonly 402/403/404/501).
We probe exactly once per process and cache the result as a module-level
`_MODE`, so subsequent `get_liveness_mode()` calls are free.

An explicit override via `WATCHDOG_LIVENESS_MODE` env var (`human` | `agent`)
skips the probe — useful for CI pinning and for forcing surfacing of real
Band.ai errors.

This mirrors `codeband.memory.probe` exactly; the two resources share the
same tiering shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal

logger = logging.getLogger(__name__)

LivenessMode = Literal["human", "agent"]

_MODE: LivenessMode | None = None
_PROBE_TIMEOUT_SEC = 5.0


def get_liveness_mode() -> LivenessMode | None:
    """Return the cached mode, or None if the probe hasn't run yet."""
    return _MODE


def set_liveness_mode(mode: LivenessMode) -> None:
    """Force the mode — intended for tests and explicit CLI overrides."""
    global _MODE
    _MODE = mode


def reset_liveness_mode() -> None:
    """Clear the cache so the next `probe_liveness_backend()` re-runs the probe."""
    global _MODE
    _MODE = None


def _env_override() -> LivenessMode | None:
    raw = os.environ.get("WATCHDOG_LIVENESS_MODE", "").strip().lower()
    if raw in ("human", "agent"):
        return raw  # type: ignore[return-value]
    if raw and raw != "auto":
        logger.warning(
            "Ignoring invalid WATCHDOG_LIVENESS_MODE=%r "
            "(expected 'human' | 'agent' | 'auto')",
            raw,
        )
    return None


async def probe_liveness_backend(
    rest_client: Any,
    *,
    config_override: str | None = None,
    force: bool = False,
) -> LivenessMode:
    """Resolve whether to use the human API or fall back to the agent API.

    Precedence: env var override > config override > live probe. The result
    is cached; pass `force=True` to re-probe.
    """
    global _MODE

    if not force and _MODE is not None:
        return _MODE

    env = _env_override()
    if env is not None:
        logger.info("Watchdog liveness: %s (via WATCHDOG_LIVENESS_MODE)", env)
        _MODE = env
        return _MODE

    if config_override in ("human", "agent"):
        logger.info("Watchdog liveness: %s (via codeband.yaml)", config_override)
        _MODE = config_override  # type: ignore[assignment]
        return _MODE

    try:
        await asyncio.wait_for(
            rest_client.human_api_chats.list_my_chats(),
            timeout=_PROBE_TIMEOUT_SEC,
        )
        logger.info(
            "Watchdog liveness: human-api (chat + thoughts + tool_calls, enterprise)",
        )
        _MODE = "human"
    except asyncio.TimeoutError:
        logger.warning(
            "Human-API probe timed out after %.1fs — using agent-API "
            "liveness (chat-only). Set WATCHDOG_LIVENESS_MODE=human to override.",
            _PROBE_TIMEOUT_SEC,
        )
        _MODE = "agent"
    except Exception as exc:
        status = _extract_status(exc)
        if status in (402, 403, 404, 501):
            logger.info(
                "Watchdog liveness: agent-api (chat-only, free tier — HTTP %s)",
                status,
            )
        else:
            logger.warning(
                "Human-API probe failed (%s: %s) — using agent-API liveness "
                "(chat-only). Set WATCHDOG_LIVENESS_MODE=human once resolved.",
                type(exc).__name__,
                exc,
            )
        _MODE = "agent"

    return _MODE


def _extract_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across SDK/httpx error shapes."""
    for attr in ("status_code", "status"):
        status = getattr(exc, attr, None)
        if isinstance(status, int):
            return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None
