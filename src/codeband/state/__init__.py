"""Durable orchestration state for Codeband.

A local SQLite database holds the typed, queryable record of a task and its
subtasks — replacing free-text Band-memory envelopes on the state path. The
store is the foundation of the deterministic-orchestration RFC (Workstream 1):
the LLM decides, code enforces and remembers, and the store is where it
remembers. See ``docs/rfc-deterministic-orchestration.md``.

In Phase 1 the store runs in *shadow mode* — it is written to but never read
to drive orchestration, so the swarm behaves identically whether or not it
exists.
"""

from __future__ import annotations

from codeband.state.store import (
    StateStore,
    SubtaskRow,
    TaskRow,
)

__all__ = [
    "StateStore",
    "SubtaskRow",
    "TaskRow",
]
