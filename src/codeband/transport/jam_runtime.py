"""``JamAgent`` — a jam-backed agent runtime with the ``thenvoi.Agent`` lifecycle.

Exposes the same ``.run()`` / ``.stop()`` contract the reconnect-forever loop
already depends on (``orchestration/runner.py``), but replaces the SDK's
WebSocket + ``/next`` ingestion with the jam Pull path:

* **onboarding**: ``start()`` adopts this existing Band agent as a ``generic``
  (Pull) peer over jamd's socket (idempotent), then runs the same adapter
  handshake the SDK does (``on_started`` after fetching agent metadata).
* **inbound**: a **dispatcher** polls ``inbox`` (the durable per-peer queue) and
  routes each message to a **per-room worker**. Each worker owns one reused
  ``ExecutionContext`` and processes its room's queue **serially** — mirroring
  the SDK's one-ExecutionContext-and-loop-per-room — so workers run concurrently
  across rooms (no cross-room head-of-line) while preserving per-room order.
* **per message**: the worker reproduces the SDK ``ExecutionContext`` semantics
  that matter — self-message filter, ``MessageRetryTracker`` budget (attempt
  recorded BEFORE processing, as the SDK does), context hydration before the
  preprocessor — then runs the SAME ``DefaultPreprocessor`` + ``adapter.on_event``
  so the brain receives an identical ``AgentInput``. ``mark_processing`` is NOT
  reproduced: jamd already did it (non-fatally) on enqueue.
* **ack**: on success the worker acks; a rejected ack (the swallowed-422 case) is
  **cosmetic** — the message stays queued, other messages keep flowing, nothing
  wedges. On handler failure the message is left un-acked (jam redelivers,
  at-least-once) until the retry budget trips, then it is acked-to-drain so a
  poison message cannot redeliver forever (jam has no ``mark_failed`` verb).

**Outbound is unchanged**: the adapter's ``tools.send_message`` still posts over
the SDK REST client (``ThenvoiLink.rest``) — already transport-agnostic and
non-wedging — so ``AgentTools`` is byte-identical to the SDK path.

────────────────────────────────────────────────────────────────────────────
SDK-INTERNALS COUPLING — RE-VERIFY ON ANY band-sdk BUMP
────────────────────────────────────────────────────────────────────────────
This runtime deliberately bypasses the SDK's public ``Agent.create(...).run()``
facade (that facade IS the wedging ``/next`` path) and reassembles the pieces
underneath it. It therefore depends on band-sdk surfaces that carry NO stability
promise — a minor SDK upgrade can move/rename them and silently break this path
(the default ``sdk`` path depends on none of them). The surfaces, most-fragile
first:

* ``thenvoi.runtime.execution.ExecutionContext._ensure_fresh_context()`` — a
  PRIVATE method we call directly to hydrate participants/history. Highest risk.
* ``thenvoi.runtime.execution.ExecutionContext`` — constructed here with a no-op
  ``on_execute`` and its WS/``/next`` loop never started (used only as the
  ``AgentTools.from_context`` source + preprocessor input).
* adapter ``_thenvoi_agent_id`` — a private attribute the SDK's ``Agent.start``
  sets; we set it ourselves before ``adapter.on_started`` / ``adapter.on_event``.
* ``thenvoi.preprocessing.default.DefaultPreprocessor`` (+ its ``process`` shape).
* ``thenvoi.runtime.retry_tracker.MessageRetryTracker`` (record_attempt/
  is_permanently_failed/mark_success).
* ``thenvoi.client.streaming.MessageCreatedPayload`` / ``MessageMetadata`` — the
  inbound payload models (the preprocessor reads ``payload.inserted_at``).
* ``thenvoi.platform.{link.ThenvoiLink, event.MessageEvent}``.

Tripwires: (1) the tests exercise the REAL preprocessor/ExecutionContext, so a
``pip install`` against a new SDK + ``pytest`` fails loudly; (2)
``doctor.check_jam_delivery_sdk_coupling`` imports each symbol above and reports
a clear, actionable failure at ``cb doctor`` time. If you bump band-sdk and
either trips, re-verify this module against the new SDK shapes. band-sdk is
pinned ``>=0.2.8,<0.3``; the ``1.0`` ``thenvoi``→``band`` rename is a whole-repo
migration that will touch this module too.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from codeband.transport.jam_control import (
    AckOutcome,
    JamControlClient,
    JamControlError,
    Target,
    agent_scope,
)

logger = logging.getLogger(__name__)

# Consecutive inbox-poll failures tolerated before run() returns (→ reconnect).
_MAX_INBOX_FAILURES = 5
# Dedupe-set bound (mirrors the SDK's _processed_ids cap shape).
_HANDLED_MAX = 4096


def _iso_from_epoch_ms(ms: Any) -> str:
    """tjam ``enqueued_at`` is unix epoch milliseconds; render ISO-8601 UTC."""
    try:
        if ms:
            return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return datetime.now(timezone.utc).isoformat()


async def _noop_handler(ctx: Any, event: Any) -> None:
    """Required ``on_execute`` for the ExecutionContext — never invoked.

    JamAgent drives ``preprocessor.process`` + ``adapter.on_event`` directly and
    never starts the context's own processing loop, so this is unreachable.
    """
    return None


class _LruSet:
    """Bounded insertion-ordered set for message-id dedupe (LRU eviction)."""

    def __init__(self, maxsize: int):
        self._d: OrderedDict[str, bool] = OrderedDict()
        self._max = maxsize

    def __contains__(self, key: str) -> bool:
        if key in self._d:
            self._d.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        self._d[key] = True
        self._d.move_to_end(key)
        while len(self._d) > self._max:
            self._d.popitem(last=False)


class JamAgent:
    """A jam-delivery-backed agent exposing the ``thenvoi.Agent`` run/stop shape."""

    def __init__(
        self,
        adapter: Any,
        creds: Any,
        config: Any,
        *,
        control: JamControlClient | None = None,
        link: Any = None,
        preprocessor: Any = None,
    ):
        self._adapter = adapter
        self._agent_id = creds.agent_id
        self._api_key = creds.api_key
        self._config = config
        # Injectable seams (tests pass fakes; production builds them in start()).
        self._control = control
        self._link = link
        self._preprocessor = preprocessor
        self._session_config: Any = None
        self._target = Target(scope=agent_scope(self._agent_id))
        self._poll_interval = float(config.agents.idle_resync_seconds)

        # Cross-room dedupe / in-flight tracking (single event loop → no locks).
        self._handled = _LruSet(_HANDLED_MAX)
        self._inflight: set[str] = set()
        self._workers: dict[str, _RoomWorker] = {}
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Build deps, adopt the peer, and run the adapter handshake."""
        if self._link is None:
            from thenvoi.platform.link import ThenvoiLink

            self._link = ThenvoiLink(
                self._agent_id,
                self._api_key,
                self._config.band.ws_url,
                self._config.band.rest_url,
            )
        if self._preprocessor is None:
            from thenvoi.preprocessing.default import DefaultPreprocessor

            self._preprocessor = DefaultPreprocessor()
        if self._control is None:
            self._control = JamControlClient()
        if self._session_config is None:
            from thenvoi.runtime.types import SessionConfig

            self._session_config = SessionConfig(
                idle_resync_seconds=self._config.agents.idle_resync_seconds,
                max_message_retries=self._config.agents.max_message_retries,
            )

        await self._adopt()
        if not await self._control.ping():
            raise JamControlError(
                "jamd control socket not reachable after adopt — is the jam "
                "daemon running? (jam delivery requires jamd)"
            )

        name, description = await self._fetch_metadata()
        setattr(self._adapter, "_thenvoi_agent_id", self._agent_id)
        await self._adapter.on_started(name, description)
        logger.info("JamAgent online: agent=%s scope=%s", self._agent_id, self._target.scope)

    async def run(self) -> None:
        """Run the dispatcher until the transport dies or the task is cancelled."""
        await self.start()
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())
        try:
            await self._dispatcher_task
        finally:
            # Close on BOTH clean return (transport-fatal) and cancellation, so
            # the httpx UDS client never leaks — distributed run_agent's clean
            # exit doesn't route through stop(). Idempotent with _safe_stop_agent.
            await self.stop()

    async def stop(self, timeout: float | None = None) -> bool:
        """Cancel the dispatcher + all room workers and close transports."""
        self._stopped.set()
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
        await self._drain_workers()
        if self._control is not None:
            await self._control.close()
        return True

    async def close(self) -> None:
        """Alias for :meth:`stop` — distributed ``run_agent`` teardown calls
        ``agent.close()`` when present; route it through the same idempotent path."""
        await self.stop()

    async def _drain_workers(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            await worker.stop()

    # --- onboarding / metadata --------------------------------------------

    async def _adopt(self) -> None:
        """Idempotently adopt this existing Band agent as a generic Pull peer."""
        opts = {
            "profile": "default",
            "scope": self._target.scope,
            "cwd": os.getcwd(),
            "agent_name": "",
            "host": "generic",
            "team_name": "",
            "teammate_name": "",
            "host_pid": 0,
            "agent_session_provider": "",
            "agent_session_id": "",
            "agent_session_path": "",
            "auto_session": False,
            "room": "",
            "host_session": "",
        }
        await self._control.adopt(opts, self._api_key)

    async def _fetch_metadata(self) -> tuple[str, str]:
        """Fetch this agent's name/description (mirrors runtime.initialize).

        Best-effort: a fetch failure falls back to a generic identity so the
        adapter handshake still runs (some adapters require a description).
        """
        try:
            resp = await self._link.rest.agent_api_identity.get_agent_me()
            data = getattr(resp, "data", None)
            if data is not None and getattr(data, "name", None):
                return data.name, (getattr(data, "description", None) or "A codeband agent")
        except Exception as exc:  # noqa: BLE001 - metadata is advisory for on_started
            logger.warning("JamAgent metadata fetch failed for %s: %s", self._agent_id, exc)
        return self._agent_id, "A codeband agent"

    # --- inbound dispatch --------------------------------------------------

    async def _dispatch_loop(self) -> None:
        """Poll the union inbox and fan messages out to per-room workers."""
        consecutive_failures = 0
        while not self._stopped.is_set():
            try:
                messages = await self._control.inbox(self._target)
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport tolerance
                consecutive_failures += 1
                logger.warning(
                    "jam inbox poll failed (%d/%d): %s",
                    consecutive_failures,
                    _MAX_INBOX_FAILURES,
                    exc,
                )
                if consecutive_failures >= _MAX_INBOX_FAILURES:
                    logger.error("jam inbox unreachable — exiting run() to reconnect")
                    return
                await asyncio.sleep(self._poll_interval)
                continue

            for msg in messages:
                self._route(msg)
            await asyncio.sleep(self._poll_interval)

    def _route(self, msg: dict[str, Any]) -> None:
        """Dedupe and hand a message to its room worker (spawn on first sight)."""
        msg_id = msg.get("message_id")
        chat_id = msg.get("chat_id")
        if not msg_id or not chat_id:
            return
        if msg_id in self._handled or msg_id in self._inflight:
            return
        self._inflight.add(msg_id)
        worker = self._workers.get(chat_id)
        if worker is None:
            worker = _RoomWorker(chat_id, self)
            self._workers[chat_id] = worker
            worker.start()
        worker.enqueue(msg)

    # --- helpers shared with workers --------------------------------------

    def _build_event(self, room_id: str, msg: dict[str, Any]) -> Any:
        """Build the SAME ``MessageEvent`` shape the SDK backlog path builds.

        The preprocessor reads ``payload.inserted_at`` and the payload MUST be a
        ``MessageCreatedPayload`` (not a ``PlatformMessage``). ``message_type`` and
        structured mentions are not on the jam wire, so synthesize
        ``message_type="text"`` and ``metadata{mentions:[], status:"sent"}`` —
        mentions remain visible in the (already-rewritten) content text.
        """
        from thenvoi.client.streaming import MessageCreatedPayload, MessageMetadata
        from thenvoi.platform.event import MessageEvent

        iso = _iso_from_epoch_ms(msg.get("enqueued_at"))
        payload = MessageCreatedPayload(
            id=msg["message_id"],
            content=msg.get("content", ""),
            sender_id=msg.get("sender_id", ""),
            sender_type=msg.get("sender_type", "User"),
            message_type="text",
            metadata=MessageMetadata(mentions=[], status="sent"),
            sender_name=(msg.get("sender_name") or None),
            chat_room_id=room_id,
            inserted_at=iso,
            updated_at=iso,
        )
        return MessageEvent(room_id=room_id, payload=payload)

    async def _ack_drain(self, msg_id: str, *, reason: str) -> None:
        """Mark a message handled and ack-to-drain it (non-fatal)."""
        self._handled.add(msg_id)
        outcome: AckOutcome = await self._control.ack(self._target, msg_id)
        if not outcome.ok:
            logger.debug(
                "jam drain-ack rejected for %s (%s; stays queued, cosmetic): %s",
                msg_id,
                reason,
                outcome.error,
            )


class _RoomWorker:
    """Serial per-room processor: one reused ExecutionContext + retry tracker."""

    def __init__(self, room_id: str, agent: JamAgent):
        self._room_id = room_id
        self._agent = agent
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._ctx: Any = None
        self._retry: Any = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    def enqueue(self, msg: dict[str, Any]) -> None:
        self._queue.put_nowait(msg)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass

    async def _run(self) -> None:
        from thenvoi.runtime.execution import ExecutionContext
        from thenvoi.runtime.retry_tracker import MessageRetryTracker

        self._ctx = ExecutionContext(
            self._room_id,
            self._agent._link,
            _noop_handler,
            config=self._agent._session_config,
            agent_id=self._agent._agent_id,
        )
        self._retry = MessageRetryTracker(
            max_retries=self._agent._config.agents.max_message_retries,
            room_id=self._room_id,
        )
        while True:
            msg = await self._queue.get()
            msg_id = msg.get("message_id", "")
            try:
                await self._process(msg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - handler failure → at-least-once retry
                logger.exception(
                    "jam room-worker %s: handler failed for %s — will retry",
                    self._room_id,
                    msg_id,
                )
            finally:
                # Always clear in-flight: success/drain already recorded _handled;
                # a handler failure is intentionally NOT _handled so the next poll
                # re-delivers it (until the retry budget trips → drain).
                self._agent._inflight.discard(msg_id)
                self._queue.task_done()

    async def _process(self, msg: dict[str, Any]) -> None:
        agent = self._agent
        msg_id = msg["message_id"]
        sender_id = msg.get("sender_id", "")
        sender_type = msg.get("sender_type", "")

        # Self-message filter (before retry accounting, mirroring the SDK order).
        if sender_type == "Agent" and sender_id == agent._agent_id:
            await agent._ack_drain(msg_id, reason="self")
            return

        # Permanently-failed skip (the SDK checks this first too).
        if self._retry.is_permanently_failed(msg_id):
            await agent._ack_drain(msg_id, reason="permanently_failed")
            return

        # Record the attempt BEFORE processing — SDK order (execution.py:1187).
        _attempts, exceeded = self._retry.record_attempt(msg_id)
        if exceeded:
            logger.error(
                "jam room-worker %s: msg %s exceeded retry budget — draining",
                self._room_id,
                msg_id,
            )
            await agent._ack_drain(msg_id, reason="exceeded_retries")
            return

        event = agent._build_event(self._room_id, msg)
        # Hydrate participants/history via REST (same calls the SDK makes), so the
        # preprocessor + AgentTools see identical context.
        await self._ctx._ensure_fresh_context()
        inp = await agent._preprocessor.process(
            ctx=self._ctx, event=event, agent_id=agent._agent_id
        )
        if inp is None:
            # Preprocessor backstop (self/non-message) — drain it.
            await agent._ack_drain(msg_id, reason="preprocessor_skip")
            return

        await agent._adapter.on_event(inp)

        # Success → clear retry tracking, mark handled, ack (non-fatal).
        self._retry.mark_success(msg_id)
        agent._handled.add(msg_id)
        outcome = await agent._control.ack(agent._target, msg_id)
        if not outcome.ok:
            logger.warning(
                "jam ack rejected for %s (cosmetic — stays queued, others flow): %s",
                msg_id,
                outcome.error,
            )
