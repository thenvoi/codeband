"""Worker-pool abstraction: per-task allocation of coders + reviewers.

Phase A lands the pure in-memory allocator. Phase B wires it into the
runtime (`orchestration/runner.py`) and makes the Conductor use it for
task dispatch.
"""

from __future__ import annotations

from codeband.workers.pool import (
    WorkerId,
    WorkerPool,
    WorkerRole,
    WorkerSlot,
    opposite_framework,
)

__all__ = [
    "WorkerId",
    "WorkerPool",
    "WorkerRole",
    "WorkerSlot",
    "opposite_framework",
]
