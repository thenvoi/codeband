"""Worker supervisor — restart loop with recovery context."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from codeband.session.context import build_recovery_context
from codeband.session.identity import WorkerIdentity

if TYPE_CHECKING:
    from codeband.monitoring.activity_log import ActivityLogger

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    """Wraps a pooled worker agent in a reconnect-forever loop.

    Every cycle: increment session count, optionally rebuild recovery context
    (git log + uncommitted changes + TASK.md), create a fresh agent, run it.
    Both crashes and clean exits from ``agent.run()`` trigger another cycle
    after ``restart_delay_seconds``. The loop only ends when the enclosing
    task is cancelled (via the runner's shutdown path).
    """

    def __init__(
        self,
        *,
        worker_id: str,
        agent_id: str,
        create_agent_fn: Callable[..., Coroutine[Any, Any, Any]],
        state_dir: Path,
        worktree_path: Path,
        restart_delay_seconds: float = 5.0,
        activity: ActivityLogger | None = None,
    ):
        self._worker_id = worker_id
        self._agent_id = agent_id
        self._create_agent_fn = create_agent_fn
        self._state_dir = state_dir
        self._worktree_path = worktree_path
        self._restart_delay = restart_delay_seconds
        self._activity = activity

    def _load_assignment_state(self) -> dict:
        """Load assignment state from .codeband_state.json in worktree.

        Returns a dict with task_branch, task_id, pr_number (any may be None).
        This is the single source of truth for assignment state — not stored
        in WorkerIdentity to avoid dual-write divergence.
        """
        import json

        state_file = self._worktree_path / ".codeband_state.json"
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            logger.info(
                "Loaded assignment state for %s: branch=%s pr=%s",
                self._worker_id, data.get("task_branch"), data.get("pr_number"),
            )
            return data
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning(
                "Failed to read .codeband_state.json for %s",
                self._worker_id, exc_info=True,
            )
            return {}

    async def run(self) -> None:
        """Run the worker, reconnecting forever until the task is cancelled."""
        identity = WorkerIdentity.load(self._state_dir, self._worker_id)
        if identity is None:
            identity = WorkerIdentity(
                worker_id=self._worker_id,
                agent_id=self._agent_id,
                worktree_path=str(self._worktree_path),
            )

        while True:
            identity.session_count += 1

            recovery_context: str | None = None
            if identity.session_count > 1:
                assignment = self._load_assignment_state()
                try:
                    recovery_context = build_recovery_context(
                        self._worker_id, self._worktree_path, identity,
                        assignment=assignment,
                    )
                except Exception:
                    logger.warning(
                        "Failed to build recovery context for %s", self._worker_id,
                        exc_info=True,
                    )

            identity.save(self._state_dir)
            if self._activity:
                self._activity.log(
                    "SESSION_START", self._worker_id,
                    f"Session #{identity.session_count}",
                )
            agent = await self._create_agent_fn(recovery_context=recovery_context)

            exit_reason: str
            try:
                await agent.run()
                exit_reason = "clean_exit"
            except asyncio.CancelledError:
                await self._close_agent(agent)
                raise
            except Exception as exc:
                identity.last_session_error = str(exc)
                identity.save(self._state_dir)
                exit_reason = f"crash: {type(exc).__name__}: {exc}"
                if self._activity:
                    self._activity.log(
                        "SESSION_CRASH", self._worker_id,
                        f"Session #{identity.session_count} crashed: {exc}",
                    )

            # Normal exit path (clean or exception). Cancellation already
            # closed the agent above before re-raising; we only reach here
            # on non-cancellation paths.
            await self._close_agent(agent)

            if self._activity:
                self._activity.log(
                    "SESSION_RESTART", self._worker_id,
                    f"Session #{identity.session_count} ended ({exit_reason}) — "
                    f"restarting as #{identity.session_count + 1}",
                )
            logger.warning(
                "Worker %s session %d ended (%s). Restarting in %.1fs...",
                self._worker_id, identity.session_count,
                exit_reason, self._restart_delay,
            )
            await asyncio.sleep(self._restart_delay)

    async def _close_agent(self, agent: Any) -> None:
        """Best-effort SDK teardown between worker restart cycles."""
        stop = getattr(agent, "stop", None)
        if stop is not None:
            try:
                await stop(timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug(
                    "Error stopping agent %s", self._worker_id, exc_info=True,
                )
            return

        close = getattr(agent, "close", None)
        if close is None:
            return
        try:
            await close()
        except Exception:
            logger.debug(
                "Error closing agent %s", self._worker_id, exc_info=True,
            )
