"""In-memory allocator for worker pools (coders, reviewers, planners).

The pool tracks `(role, framework)` capacity registered at `cb run`
startup and hands out idle workers to the Conductor at dispatch time.
Released workers return to the idle set and can be reused for later
tasks.

Concurrency: methods run under a `threading.Lock`. Since Codeband's
Conductor is single-task asyncio code today, the lock is cheap
insurance against future multi-threaded use (e.g., a Conductor that
dispatches tasks from a background thread). Do not hold the lock
across awaits.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from codeband.config import Framework


class WorkerRole(str, Enum):
    """Roles that have pooled capacity (not coordination singletons)."""

    PLANNER = "planner"
    PLAN_REVIEWER = "plan_reviewer"
    CODER = "coder"
    REVIEWER = "reviewer"


@dataclass(frozen=True)
class WorkerId:
    """Stable identifier for a registered worker slot.

    Rendered as `{role}-{framework}-{index}` for Band.ai display names,
    agent_config keys, and log output.
    """

    role: WorkerRole
    framework: Framework
    index: int

    def __str__(self) -> str:
        return f"{self.role.value}-{self.framework.value}-{self.index}"


@dataclass
class WorkerSlot:
    """Runtime state for one worker: its identity and current assignment."""

    worker_id: WorkerId
    busy: bool = False
    current_task: str | None = None


def opposite_framework(framework: Framework) -> Framework:
    """Return the cross-model framework for adversarial review pairing."""
    return Framework.CODEX if framework == Framework.CLAUDE_SDK else Framework.CLAUDE_SDK


@dataclass
class WorkerPool:
    """Tracks all registered worker slots and handles acquire/release.

    Usage:
        pool = WorkerPool()
        pool.register(WorkerRole.CODER, Framework.CLAUDE_SDK, count=2)
        pool.register(WorkerRole.REVIEWER, Framework.CODEX, count=1)

        coder, reviewer = pool.pair_for_task(WorkerRole.CODER, Framework.CLAUDE_SDK)
        ...
        pool.release(coder)
        pool.release(reviewer)
    """

    _slots: dict[str, WorkerSlot] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── registration ────────────────────────────────────────────────────────

    def register(
        self, role: WorkerRole, framework: Framework, count: int,
    ) -> list[WorkerId]:
        """Register `count` idle slots for (role, framework). Idempotent:
        registering again with a larger count extends the pool; smaller
        counts are rejected (shrinking is not supported at runtime)."""
        if count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        with self._lock:
            existing = [
                slot.worker_id for slot in self._slots.values()
                if slot.worker_id.role == role and slot.worker_id.framework == framework
            ]
            existing_count = len(existing)
            if count < existing_count:
                raise ValueError(
                    f"Cannot shrink pool ({role.value}, {framework.value}) "
                    f"from {existing_count} to {count} at runtime",
                )
            new_ids: list[WorkerId] = list(existing)
            for i in range(existing_count, count):
                wid = WorkerId(role=role, framework=framework, index=i)
                self._slots[str(wid)] = WorkerSlot(worker_id=wid)
                new_ids.append(wid)
            return new_ids

    # ── allocation ──────────────────────────────────────────────────────────

    def acquire(
        self,
        role: WorkerRole,
        framework: Framework | None = None,
        *,
        task_id: str | None = None,
    ) -> WorkerId | None:
        """Reserve an idle worker of (role, framework). Returns None if
        no idle worker matches. `framework=None` = any framework."""
        with self._lock:
            for slot in self._slots.values():
                if slot.busy:
                    continue
                if slot.worker_id.role != role:
                    continue
                if framework is not None and slot.worker_id.framework != framework:
                    continue
                slot.busy = True
                slot.current_task = task_id
                return slot.worker_id
        return None

    def release(self, worker_id: WorkerId) -> None:
        """Mark worker idle. No-op if already idle or unknown."""
        with self._lock:
            slot = self._slots.get(str(worker_id))
            if slot is None or not slot.busy:
                return
            slot.busy = False
            slot.current_task = None

    def pair_for_task(
        self,
        coder_role: WorkerRole,
        coder_framework: Framework,
        *,
        reviewer_role: WorkerRole = WorkerRole.REVIEWER,
        task_id: str | None = None,
    ) -> tuple[WorkerId, WorkerId] | None:
        """Atomically acquire a coder + an opposite-framework reviewer.

        Returns `(coder_id, reviewer_id)` on success, or `None` if either
        side can't be satisfied (in which case nothing is reserved).
        """
        preferred_reviewer_fw = opposite_framework(coder_framework)
        with self._lock:
            # Try to find both idle workers before committing.
            coder_slot = self._find_idle(coder_role, coder_framework)
            reviewer_slot = self._find_idle(reviewer_role, preferred_reviewer_fw)

            # Fallback: if opposite-framework reviewer is busy but a
            # same-framework one is idle, take it (caller gets same-model
            # review — documented degradation when config lacks diversity).
            if reviewer_slot is None:
                reviewer_slot = self._find_idle(reviewer_role, coder_framework)

            if coder_slot is None or reviewer_slot is None:
                return None
            if coder_slot.worker_id == reviewer_slot.worker_id:
                # Defensive: different roles → different slots; sanity check.
                return None

            coder_slot.busy = True
            coder_slot.current_task = task_id
            reviewer_slot.busy = True
            reviewer_slot.current_task = task_id
            return coder_slot.worker_id, reviewer_slot.worker_id

    def _find_idle(
        self, role: WorkerRole, framework: Framework,
    ) -> WorkerSlot | None:
        """Caller must hold the lock."""
        for slot in self._slots.values():
            if (
                not slot.busy
                and slot.worker_id.role == role
                and slot.worker_id.framework == framework
            ):
                return slot
        return None

    # ── introspection ───────────────────────────────────────────────────────

    def idle_count(
        self, role: WorkerRole, framework: Framework | None = None,
    ) -> int:
        """Count of idle workers matching (role, framework). Useful for
        'can I pair this task right now?' queries."""
        with self._lock:
            return sum(
                1 for s in self._slots.values()
                if not s.busy
                and s.worker_id.role == role
                and (framework is None or s.worker_id.framework == framework)
            )

    def total_count(self, role: WorkerRole | None = None) -> int:
        with self._lock:
            return sum(
                1 for s in self._slots.values()
                if role is None or s.worker_id.role == role
            )

    def active_frameworks(self, role: WorkerRole) -> list[Framework]:
        """Frameworks that have at least one registered worker for `role`."""
        with self._lock:
            seen: set[Framework] = set()
            for slot in self._slots.values():
                if slot.worker_id.role == role:
                    seen.add(slot.worker_id.framework)
            # Return in deterministic order.
            return [f for f in (Framework.CLAUDE_SDK, Framework.CODEX) if f in seen]

    def snapshot(self) -> list[dict]:
        """Serializable view of all slots — for `cb status` and logs."""
        with self._lock:
            return [
                {
                    "worker_id": str(s.worker_id),
                    "role": s.worker_id.role.value,
                    "framework": s.worker_id.framework.value,
                    "busy": s.busy,
                    "current_task": s.current_task,
                }
                for s in self._slots.values()
            ]
