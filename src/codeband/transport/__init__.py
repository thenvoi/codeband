"""Opt-in jam delivery transport (``CODEBAND_DELIVERY=jam``).

This package implements the wedge-immune message-delivery path: instead of the
SDK ExecutionContext's WebSocket + ``/next`` server cursor (which can pin on a
swallowed mark-processed 422), inbound messages are pulled from the local jam
daemon (``jamd``) over its wire-stable Unix-socket Control contract — a durable
per-peer queue with non-fatal acks and no head-of-line cursor.

Everything here is **dormant unless** ``runner._resolve_delivery_mode`` resolves
to ``jam`` — the modules are imported lazily inside that branch only, so the
default ``sdk`` path never touches this code. The brain (FSM, gates, cb-phase,
StateStore, watchdog, pool, auth, preflight, doctor) and the wedge-recovery
machinery (#102/#103/watchdog heal rung) are unchanged and still cover the
``sdk`` path.
"""

from __future__ import annotations
