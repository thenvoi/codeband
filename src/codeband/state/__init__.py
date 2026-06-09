"""Durable orchestration state for Codeband.

A local SQLite database holds the typed, queryable record of a task and its
subtasks — replacing free-text Band-memory envelopes on the state path. The
store is the foundation of the deterministic-orchestration RFC (Workstream 1):
the LLM decides, code enforces and remembers, and the store is where it
remembers. See ``docs/rfc-deterministic-orchestration.md``.

In Phase 1 the store ran in *shadow mode* — written but never read. Since the
verify-gate activation it is read on the orchestration path: ``cb-phase``
resolves the task and gates handoffs from it, and the watchdog patrols from
its task and subtask rows.
"""

from __future__ import annotations

from codeband.state.registration import (
    RegistrationResult,
    register_task,
)
from codeband.state.store import (
    StateStore,
    SubtaskRow,
    TaskRow,
)

__all__ = [
    "RegistrationResult",
    "StateStore",
    "SubtaskRow",
    "TaskRow",
    "register_task",
]
