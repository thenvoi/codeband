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


def _exception_signature(exc: BaseException) -> str:
    """Stable, single-line key identifying an exception for dedup/backoff.

    Uses the exception type plus the first line of the message, truncated.
    Two crashes that differ only in trailing detail (e.g. a session id)
    still collapse to the same signature.
    """
    msg = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}:{msg[:160]}"


class WorkerSupervisor:
    """Wraps a pooled worker agent in a reconnect-forever loop.

    Every cycle: increment session count, optionally rebuild recovery context
    (git log + uncommitted changes + TASK.md), create a fresh agent, run it.
    Both crashes and clean exits from ``agent.run()`` trigger another cycle.
    The loop only ends when the enclosing task is cancelled (via the runner's
    shutdown path).

    Backoff: when consecutive cycles share the same exit signature (same
    crash type+message, or repeated clean-exit churn) the delay doubles up
    to ``_MAX_BACKOFF_SECONDS``. Different signatures reset the counter.
    Tracebacks for repeat failures are suppressed so a deterministic loop
    (missing worktree, missing CLI binary) doesn't flood stdout.
    """

    _MAX_BACKOFF_SECONDS = 60.0
    _MAX_BACKOFF_DOUBLINGS = 6  # 2**6 = 64 → clamped to 60s

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

    def _compute_backoff(self, consecutive_same: int) -> float:
        """Exponential backoff capped at ``_MAX_BACKOFF_SECONDS``.

        ``consecutive_same`` is 0 for the first occurrence of a signature
        and increments for each repeat. With ``restart_delay_seconds=0``
        (test mode) the backoff stays 0 so existing fast tests don't slow
        down.
        """
        if self._restart_delay <= 0:
            return 0.0
        doublings = min(consecutive_same, self._MAX_BACKOFF_DOUBLINGS)
        return min(self._MAX_BACKOFF_SECONDS, self._restart_delay * (2 ** doublings))

    def _save_identity_safe(self, identity: WorkerIdentity) -> None:
        """Best-effort identity persistence inside the supervision loop (S6-F9).

        The reconnect-forever loop must outlive its own bookkeeping: a full
        disk / unwritable state dir degrades recovery context on the next
        restart, which is strictly better than the supervisor dying and the
        worker never restarting at all.
        """
        try:
            identity.save(self._state_dir)
        except OSError:
            logger.warning(
                "Failed to persist worker identity for %s — continuing",
                self._worker_id, exc_info=True,
            )

    def _log_activity_safe(self, event_type: str, summary: str) -> None:
        """Best-effort activity append inside the supervision loop (S6-F9)."""
        if not self._activity:
            return
        try:
            self._activity.log(event_type, self._worker_id, summary)
        except OSError:
            logger.warning(
                "Activity-log write (%s for %s) failed — continuing",
                event_type, self._worker_id, exc_info=True,
            )

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

        last_exit_signature: str | None = None
        consecutive_same: int = 0
        last_recovery_signature: str | None = None

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
                except Exception as exc:
                    sig = _exception_signature(exc)
                    if sig == last_recovery_signature:
                        logger.warning(
                            "Failed to build recovery context for %s "
                            "(repeat: %s)", self._worker_id, sig,
                        )
                    else:
                        last_recovery_signature = sig
                        logger.warning(
                            "Failed to build recovery context for %s",
                            self._worker_id, exc_info=True,
                        )

            self._save_identity_safe(identity)
            self._log_activity_safe(
                "SESSION_START", f"Session #{identity.session_count}",
            )
            agent = await self._create_agent_fn(recovery_context=recovery_context)

            exit_reason: str
            exit_signature: str
            try:
                await agent.run()
                exit_reason = "clean_exit"
                exit_signature = "clean_exit"
            except asyncio.CancelledError:
                await self._close_agent(agent)
                raise
            except Exception as exc:
                identity.last_session_error = str(exc)
                self._save_identity_safe(identity)
                exit_reason = f"crash: {type(exc).__name__}: {exc}"
                exit_signature = f"crash:{_exception_signature(exc)}"
                self._log_activity_safe(
                    "SESSION_CRASH",
                    f"Session #{identity.session_count} crashed: {exc}",
                )

            # Normal exit path (clean or exception). Cancellation already
            # closed the agent above before re-raising; we only reach here
            # on non-cancellation paths.
            await self._close_agent(agent)

            if exit_signature == last_exit_signature:
                consecutive_same += 1
            else:
                last_exit_signature = exit_signature
                consecutive_same = 0
            delay = self._compute_backoff(consecutive_same)

            self._log_activity_safe(
                "SESSION_RESTART",
                f"Session #{identity.session_count} ended ({exit_reason}) — "
                f"restarting as #{identity.session_count + 1}",
            )
            logger.warning(
                "Worker %s session %d ended (%s). Restarting in %.1fs...",
                self._worker_id, identity.session_count,
                exit_reason, delay,
            )
            await asyncio.sleep(delay)

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
