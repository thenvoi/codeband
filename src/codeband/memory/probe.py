"""Probe whether the Band.ai memory API is available on this tier.

A paid Band.ai account responds to `list_agent_memories()` with data (even
if empty). A free-tier account fails with an HTTP error (commonly 402/403).
We probe exactly once per process and cache the result as a module-level
`_MODE`, so subsequent `get_memory_mode()` calls are free.

An explicit override via the `BAND_MEMORY_MODE` env var (`band` | `local`)
skips the probe — useful for CI pinning and for forcing surfacing of real
Band.ai errors.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal

logger = logging.getLogger(__name__)

MemoryMode = Literal["band", "local"]

_MODE: MemoryMode | None = None
_REASON: str | None = None
_PROBE_TIMEOUT_SEC = 5.0


def get_memory_mode() -> MemoryMode | None:
    """Return the cached mode, or None if the probe hasn't run yet."""
    return _MODE


def get_memory_mode_reason() -> str | None:
    """Return a short human-readable reason for the cached mode (or None).

    Set alongside ``_MODE`` whenever a probe runs. Callers (e.g., the
    runner's startup banner) use it to produce a precise user-facing
    message instead of an ambiguous "free tier or unreachable".
    """
    return _REASON


def set_memory_mode(mode: MemoryMode, reason: str | None = None) -> None:
    """Force the mode — intended for tests and explicit CLI overrides."""
    global _MODE, _REASON
    _MODE = mode
    _REASON = reason


def reset_memory_mode() -> None:
    """Clear the cache so the next `probe_memory_backend()` re-runs the probe."""
    global _MODE, _REASON
    _MODE = None
    _REASON = None


def _env_override() -> MemoryMode | None:
    raw = os.environ.get("BAND_MEMORY_MODE", "").strip().lower()
    if raw in ("band", "local"):
        return raw  # type: ignore[return-value]
    if raw and raw != "auto":
        logger.warning(
            "Ignoring invalid BAND_MEMORY_MODE=%r (expected 'band' | 'local' | 'auto')",
            raw,
        )
    return None


async def probe_memory_backend(
    rest_client: Any,
    *,
    config_override: str | None = None,
    force: bool = False,
) -> MemoryMode:
    """Resolve whether to use Band.ai memory or the local JSONL store.

    Precedence: env var override > config override > live probe. The result
    is cached; pass `force=True` to re-probe.
    """
    global _MODE, _REASON

    if not force and _MODE is not None:
        return _MODE

    # 1. Env var override.
    env = _env_override()
    if env is not None:
        logger.info("Memory backend: %s (via BAND_MEMORY_MODE)", env)
        _MODE = env
        _REASON = f"forced via BAND_MEMORY_MODE={env}"
        return _MODE

    # 2. Explicit config override (`band.memory_mode` in codeband.yaml).
    if config_override in ("band", "local"):
        logger.info("Memory backend: %s (via codeband.yaml)", config_override)
        _MODE = config_override  # type: ignore[assignment]
        _REASON = f"forced via codeband.yaml ({config_override})"
        return _MODE

    # 3. Live probe.
    try:
        await asyncio.wait_for(
            rest_client.agent_api_memories.list_agent_memories(
                system="working",
                type="episodic",
                segment="agent",
                scope="organization",
                page_size=1,
            ),
            timeout=_PROBE_TIMEOUT_SEC,
        )
        logger.info("Band.ai memory: available (paid tier) — using remote API")
        _MODE = "band"
        _REASON = "paid tier"
    except asyncio.TimeoutError:
        logger.warning(
            "Band.ai memory probe timed out after %.1fs — falling back to local "
            "JSONL store. Multi-host deployments will not share state.",
            _PROBE_TIMEOUT_SEC,
        )
        _MODE = "local"
        _REASON = "Band.ai unreachable"
    except Exception as exc:  # HTTP 4xx/5xx or SDK-level errors
        status = _extract_status(exc)
        if status in (402, 403, 404, 501):
            logger.info(
                "Band.ai memory: unavailable (HTTP %s — free tier). "
                "Using local JSONL store. Multi-host deployments require paid Band.ai.",
                status,
            )
            _REASON = "free tier"
        else:
            logger.warning(
                "Band.ai memory probe failed (%s: %s) — falling back to local "
                "JSONL store. If this is a transient outage, set BAND_MEMORY_MODE=band "
                "once resolved.",
                type(exc).__name__,
                exc,
            )
            _REASON = "Band.ai unreachable"
        _MODE = "local"

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
