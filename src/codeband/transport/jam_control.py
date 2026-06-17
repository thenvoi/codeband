"""Async client for jamd's Unix-socket Control contract (jam-contract, tjam 0.2.7).

jamd serves HTTP/1.1 + JSON over a Unix socket at ``$JAM_CONFIG_DIR/jam.sock``
(default ``$HOME/.jam/jam.sock``), ``0600``, UID-filtered. This module speaks the
small subset codeband's delivery path needs:

* ``POST /v1/adopt``  — ``AdoptReq{opts: EnsureOpts, agent_key}`` → bring an
  EXISTING Band agent online as a ``generic`` (Pull) peer (onboarding).
* ``POST /v1/inbox``  — ``{target}`` → ``{messages: [Message,…]}`` (the durable
  per-peer queue; un-acked messages, FIFO within each room).
* ``POST /v1/ack``    — ``{target, msg_id}`` → ``200`` on success; on the
  underlying mark-processed failure (the 422) a 5xx with the message left
  **queued and retriable**. This is the non-wedge property — :meth:`ack` never
  raises on a rejection; it returns an :class:`AckOutcome`.
* ``POST /v1/send`` / ``POST /v1/reply`` — outbound (not used by the default
  inbound path, which keeps outbound on the SDK REST tools, but provided for
  completeness/tests).
* ``GET  /v1/ping``   — readiness probe.

The wire ``Message`` carries ``sender_id`` and ``sender_type`` (verified against
tjam ``jam-domain/src/message.rs``), which is why the socket — not the lossy
``jam inbox`` CLI — is the inbound source: faithful self-message filtering needs
both fields.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Stay under jamd's 45s default unary timeout.
_DEFAULT_TIMEOUT_SECONDS = 30.0

# Base URL is irrelevant over a UDS transport (httpx still needs a valid host).
_BASE_URL = "http://jamd.local"


class JamControlError(RuntimeError):
    """A Control call failed in a way the caller should treat as fatal-ish.

    Used for non-ack calls (adopt/inbox/send/reply). Ack rejections are NOT
    raised — they return an :class:`AckOutcome` (non-fatal by contract).
    """


def socket_path() -> str:
    """Resolve jamd's control socket: ``$JAM_CONFIG_DIR/jam.sock`` else ``~/.jam``."""
    cfg = os.environ.get("JAM_CONFIG_DIR")
    base = Path(cfg) if cfg else Path.home() / ".jam"
    return str(base / "jam.sock")


def agent_scope(agent_id: str) -> str:
    """Deterministic per-agent jam peer scope for an adopted codeband agent."""
    return f"codeband-{agent_id}"


@dataclass(frozen=True)
class Target:
    """Addresses which adopted peer a Control call acts as (``Target`` DTO)."""

    profile: str = "default"
    scope: str = ""
    handle: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"profile": self.profile, "scope": self.scope, "handle": self.handle}


@dataclass(frozen=True)
class AckOutcome:
    """Result of an ack. ``ok=False`` is non-fatal — the message stays queued."""

    ok: bool
    error: str | None = None


class JamControlClient:
    """Thin async HTTP/JSON-over-UDS client for jamd's Control contract."""

    def __init__(self, *, socket: str | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS):
        self._socket = socket or socket_path()
        self._timeout = timeout
        self._client: Any = None  # lazily-built httpx.AsyncClient

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=self._socket),
                base_url=_BASE_URL,
                timeout=self._timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def ping(self) -> bool:
        """Best-effort readiness probe; never raises."""
        try:
            resp = await self._ensure_client().get("/v1/ping")
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001 - probe is advisory
            logger.debug("jam ping failed: %s", exc)
            return False

    async def adopt(self, opts: dict[str, Any], agent_key: str) -> None:
        """Adopt an existing Band agent as a generic (Pull) peer. Idempotent."""
        resp = await self._ensure_client().post(
            "/v1/adopt", json={"opts": opts, "agent_key": agent_key}
        )
        if resp.status_code >= 400:
            raise JamControlError(f"adopt failed: HTTP {resp.status_code}: {resp.text[:300]}")

    async def inbox(self, target: Target) -> list[dict[str, Any]]:
        """Return the peer's un-acked messages (full wire ``Message`` dicts)."""
        resp = await self._ensure_client().post("/v1/inbox", json={"target": target.as_dict()})
        if resp.status_code >= 400:
            raise JamControlError(f"inbox failed: HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return list(data.get("messages") or [])

    async def ack(self, target: Target, msg_id: str) -> AckOutcome:
        """Mark a message processed. NEVER raises on a rejection (non-wedge).

        A 5xx (the swallowed-422 case) or a transport error leaves the message
        queued and retriable; the caller logs and moves on. Other messages are
        unaffected — there is no head-of-line cursor.
        """
        try:
            resp = await self._ensure_client().post(
                "/v1/ack", json={"target": target.as_dict(), "msg_id": msg_id}
            )
        except Exception as exc:  # noqa: BLE001 - ack must be non-fatal
            return AckOutcome(ok=False, error=f"transport: {exc}")
        if resp.status_code == 200:
            return AckOutcome(ok=True)
        return AckOutcome(ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

    async def send(
        self, target: Target, chat_id: str, content: str, recipient_ids: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        resp = await self._ensure_client().post(
            "/v1/send",
            json={
                "target": target.as_dict(),
                "chat_id": chat_id,
                "content": content,
                "recipient_ids": list(recipient_ids),
            },
        )
        if resp.status_code >= 400:
            raise JamControlError(f"send failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def reply(self, target: Target, msg_id: str, text: str) -> dict[str, Any]:
        resp = await self._ensure_client().post(
            "/v1/reply", json={"target": target.as_dict(), "msg_id": msg_id, "text": text}
        )
        if resp.status_code >= 400:
            raise JamControlError(f"reply failed: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()
