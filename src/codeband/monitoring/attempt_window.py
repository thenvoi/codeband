"""Attempt-window observability marker (``MSG_ATTEMPT_WINDOW``).

This is a *pure-observer* instrument over the Band SDK's message-ack path. For
every message that reaches the success-ack, it emits a durable structured event
recording how long the server-side **processing attempt** was held open — from
``mark_processing`` (attempt opened) to ``mark_processed`` (attempt acked) — plus
whether the ``mark_processed`` REST call returned 422.

It is the diagnostic gauge for the mark-processed-422 cursor-pin: the pin fires
when a long agentic turn outlives the server's attempt-validity window, the
``/processed`` call 422s, the SDK swallows it, and the ``/next`` cursor wedges.

Design (see ``PLAN.md`` for the reviewed contract):

* The ack calls belong to the SDK. We wrap them but NEVER alter their return
  values, exceptions, side effects, or the SDK's existing swallow-the-422
  behavior. Each wrapper calls the SDK original first, with identical args, and
  returns/raises its result untouched. Recording and emission run in their own
  ``try/except`` so they can never perturb the ack path.
* Timing is captured at the :class:`ThenvoiLink` layer; the 422 (and the
  processing-success confirmation) are observed one layer below, at the REST
  client, via **observe-and-re-raise** — the identical exception still
  propagates up to the SDK's ``except`` and is swallowed exactly as before.
* REST-layer observations are confined to the ``ThenvoiLink`` call path by two
  :class:`~contextvars.ContextVar` "slots" set only inside the wrapped
  ``ThenvoiLink`` methods. Codeband's watchdog calls the same REST methods
  directly (and *intentionally* provokes 422s while healing pins); those calls
  run with no slot set, so the REST wrappers are transparent pass-throughs for
  them and the watchdog's 422 can never contaminate a marker.
* The open registry is keyed by ``(agent_id, room_id, message_id)`` because the
  local runtime hosts many agents in one process, each with its own link.
* ``t_open`` is recorded only when the ``mark_processing`` REST call actually
  succeeds; a swallowed processing failure yields ``elapsed_seconds=None`` rather
  than a misleading window.

The instrument is best-effort: if the SDK cannot be imported it logs a warning
and does nothing — it must never block startup.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from codeband.monitoring.activity_log import ActivityLogger

logger = logging.getLogger(__name__)


def _monotonic() -> float:
    """Indirection over :func:`time.monotonic` so tests can drive the clock
    without monkeypatching the global ``time`` module (which asyncio also uses)."""
    return time.monotonic()


#: Event type for the durable marker (matches the #97 UPPER_SNAKE family).
EVENT_TYPE = "MSG_ATTEMPT_WINDOW"

#: Cap on the open-attempt registry. Keys that never get a matching
#: ``mark_processed`` (handler-failure path, reaper path) are reclaimed here —
#: this is the leak bound. msg_ids are effectively unique, so stale keys are
#: harmless until evicted.
_MAX_OPEN = 2048

# --- module state ---------------------------------------------------------

# (agent_id, room_id, message_id) -> t_open (monotonic seconds). Insert only on
# a confirmed open; pop on emit. LRU-capped at _MAX_OPEN.
_open_registry: "OrderedDict[tuple[Optional[str], str, str], float]" = OrderedDict()

# Per-call observation slots, set only inside the wrapped ThenvoiLink methods.
# A ContextVar value set in a coroutine is visible to the awaited callee in the
# same task and does not leak to sibling tasks.
_open_slot: ContextVar[Optional[dict]] = ContextVar("_cb_attempt_open_slot", default=None)
_ack_slot: ContextVar[Optional[dict]] = ContextVar("_cb_attempt_ack_slot", default=None)

_activity: Optional["ActivityLogger"] = None

# SDK originals, captured once at first install so the wrappers can delegate.
_orig_link_processing: Any = None
_orig_link_processed: Any = None
_orig_rest_processing: Any = None
_orig_rest_processed: Any = None


def _record_open(agent_id: Optional[str], room_id: str, message_id: str) -> None:
    """Record a confirmed attempt-open timestamp, LRU-capping the registry."""
    key = (agent_id, room_id, message_id)
    _open_registry[key] = _monotonic()
    _open_registry.move_to_end(key)
    while len(_open_registry) > _MAX_OPEN:
        _open_registry.popitem(last=False)


def _emit(link: Any, room_id: str, message_id: str, t_ack: float, http_422: bool) -> None:
    """Pop the matching open and append the durable ``MSG_ATTEMPT_WINDOW`` event.

    Absence of a matching open (reaper path, or a swallowed processing failure)
    yields ``elapsed_seconds=None`` — a faithful "no confirmed window" signal.
    """
    agent_id = getattr(link, "agent_id", None)
    t_open = _open_registry.pop((agent_id, room_id, message_id), None)
    elapsed = round(t_ack - t_open, 3) if t_open is not None else None
    if _activity is None:
        return
    _activity.log(
        EVENT_TYPE,
        agent_id or "agent",
        f"msg {message_id} attempt window {elapsed}s 422={http_422}",
        msg_id=message_id,
        room_id=room_id,
        agent_id=agent_id,
        elapsed_seconds=elapsed,
        ack_422=http_422,
    )


def install_attempt_window_instrument(activity: "ActivityLogger") -> None:
    """Install the four class-level ack-path wraps (idempotent, best-effort).

    Safe to call from both the local and distributed runner paths: the patches
    are class-level and guarded by a ``_codeband_attempt_window`` sentinel, so a
    second call only refreshes the activity sink.
    """
    global _activity
    global _orig_link_processing, _orig_link_processed
    global _orig_rest_processing, _orig_rest_processed

    _activity = activity

    try:
        from thenvoi.platform.link import ThenvoiLink
        from thenvoi_rest.agent_api_messages.client import AsyncAgentApiMessagesClient
    except ImportError as exc:  # pragma: no cover - exercised only on a broken SDK
        logger.warning(
            "attempt-window instrument disabled: cannot import the Band SDK ack "
            "path (%s). Marker emission is skipped; ack behavior is unchanged.",
            exc,
        )
        return

    if getattr(ThenvoiLink.mark_processed, "_codeband_attempt_window", False):
        # Already installed this process; activity sink refreshed above.
        return

    _orig_link_processing = ThenvoiLink.mark_processing
    _orig_link_processed = ThenvoiLink.mark_processed
    _orig_rest_processing = AsyncAgentApiMessagesClient.mark_agent_message_processing
    _orig_rest_processed = AsyncAgentApiMessagesClient.mark_agent_message_processed

    @wraps(_orig_link_processing)
    async def _wrapped_link_processing(self, room_id, message_id, *args, **kwargs):
        slot = {"opened": False}
        token = _open_slot.set(slot)
        try:
            result = await _orig_link_processing(self, room_id, message_id, *args, **kwargs)
        finally:
            _open_slot.reset(token)
        # Reached only on a NORMAL return of the SDK original. If the original
        # propagated (e.g. cancellation), we never record an open — there is no
        # completed attempt to time.
        if slot["opened"]:
            try:
                _record_open(getattr(self, "agent_id", None), room_id, message_id)
            except Exception:  # noqa: BLE001 - observation must never perturb the ack path
                logger.debug("attempt-window: open record failed", exc_info=True)
        return result

    @wraps(_orig_rest_processing)
    async def _wrapped_rest_processing(self, *args, **kwargs):
        # Raises propagate untouched (SDK swallows them upstream). On success,
        # mark the open confirmed — but only inside a wrapped ThenvoiLink call.
        result = await _orig_rest_processing(self, *args, **kwargs)
        slot = _open_slot.get()
        if slot is not None:
            try:
                slot["opened"] = True
            except Exception:  # noqa: BLE001
                logger.debug("attempt-window: open-confirm failed", exc_info=True)
        return result

    @wraps(_orig_rest_processed)
    async def _wrapped_rest_processed(self, *args, **kwargs):
        try:
            return await _orig_rest_processed(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised identically below
            slot = _ack_slot.get()
            if slot is not None and getattr(exc, "status_code", None) == 422:
                try:
                    slot["http_422"] = True
                except Exception:  # noqa: BLE001
                    logger.debug("attempt-window: 422 observe failed", exc_info=True)
            raise

    @wraps(_orig_link_processed)
    async def _wrapped_link_processed(self, room_id, message_id, *args, **kwargs):
        t_ack = _monotonic()
        slot = {"http_422": False}
        token = _ack_slot.set(slot)
        try:
            result = await _orig_link_processed(self, room_id, message_id, *args, **kwargs)
        finally:
            _ack_slot.reset(token)
        # Emit ONLY on a normal return of the SDK original — i.e. a completed
        # (and possibly 422-swallowed) ack. If the original propagated (e.g.
        # cancellation), we re-raise the identical exception WITHOUT emitting a
        # marker or popping the open entry, so a later retry still has its window.
        try:
            _emit(self, room_id, message_id, t_ack, slot["http_422"])
        except Exception:  # noqa: BLE001 - emission must never perturb the ack path
            logger.debug("attempt-window: emit failed", exc_info=True)
        return result

    for fn in (
        _wrapped_link_processing,
        _wrapped_rest_processing,
        _wrapped_rest_processed,
        _wrapped_link_processed,
    ):
        fn._codeband_attempt_window = True  # type: ignore[attr-defined]

    ThenvoiLink.mark_processing = _wrapped_link_processing
    ThenvoiLink.mark_processed = _wrapped_link_processed
    AsyncAgentApiMessagesClient.mark_agent_message_processing = _wrapped_rest_processing
    AsyncAgentApiMessagesClient.mark_agent_message_processed = _wrapped_rest_processed

    logger.debug("attempt-window instrument installed")
