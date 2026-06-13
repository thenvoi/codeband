"""Persistent JSONL activity log — append-only structured event history.

**Attribution scope (Stage-3).** :func:`record_cli_invocation` logs a
``cli_invocation`` event at the start of every ``cb`` / ``cb-phase`` command
and a ``cli_completion`` event with the exit code when it returns. The event
carries the full ``argv``, ``cwd``, ``pid``, timestamp and the env markers
(``CODEBAND_AGENT_SESSION`` / ``CODEBAND_ROLE`` / worker id). This is the
attribution record: the row-5 forensics question ("which process ran this?")
becomes answerable for the entire **sanctioned CLI surface**.

The residual is by design: actions taken OUTSIDE the CLI — a raw ``sqlite3``
write to the state DB, a direct shell ``git``/``gh`` — leave no
``cli_invocation`` row. That is the adversary line we do not defend (detection
over prevention, honesty over theater). Logging is best-effort: a failure to
resolve the activity-log path (no ``codeband.yaml`` in scope, e.g. ``cb init``)
or to write it never breaks the command.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


class EventType:
    """Well-known activity ``event_type`` values."""

    LLM_USAGE: Final[str] = "LLM_USAGE"
    CLI_INVOCATION: Final[str] = "cli_invocation"
    CLI_COMPLETION: Final[str] = "cli_completion"


@dataclasses.dataclass
class ActivityEvent:
    """A single activity event."""

    timestamp: str
    event_type: str
    agent: str
    summary: str
    details: dict | None = None


class ActivityLogger:
    """Append-only JSONL logger for codeband activity events."""

    def __init__(self, log_path: Path):
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, agent: str, summary: str, **details) -> None:
        """Append an event to the activity log.

        The append holds an exclusive ``fcntl.flock`` for the write — the
        same discipline ``LocalMemoryStore`` uses (S6-F8). Every agent task
        in the process (plus the watchdog) shares this one file; without the
        lock, concurrent appends can interleave into a torn line that then
        poisons every future read.
        """
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "agent": agent,
            "summary": summary,
            "details": details if details else None,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(json.dumps(event) + "\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _project_dir_from_argv(argv: list[str]) -> str:
    """Best-effort extract of the ``--dir`` value from a raw argv.

    The CLI logger runs before click parses, so it scans argv directly for the
    common ``--dir <p>`` / ``--dir=<p>`` option every project-aware command
    shares. Falls back to ``"."`` (which :func:`resolve_project_dir` then
    resolves via ``$CODEBAND_PROJECT_DIR`` then cwd).
    """
    for i, tok in enumerate(argv):
        if tok == "--dir" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--dir="):
            return tok.split("=", 1)[1]
    return "."


def _resolve_activity_logger(argv: list[str]) -> "ActivityLogger | None":
    """Resolve the ``ActivityLogger`` for the project this invocation targets.

    Routes through the same project-dir + workspace resolution as every
    ``cb-phase`` leg (explicit ``--dir`` > ``$CODEBAND_PROJECT_DIR`` > cwd, then
    ``resolve_workspace_path``). Returns ``None`` — best-effort — when no
    ``codeband.yaml`` is in scope or anything else fails, so attribution
    logging never breaks a command (notably ``cb init``, which runs before a
    config exists).
    """
    try:
        from codeband.cli.handoff import resolve_project_dir
        from codeband.config import load_config, resolve_workspace_path

        project = resolve_project_dir(_project_dir_from_argv(argv))
        config = load_config(project)
        workspace = resolve_workspace_path(config, project)
        return ActivityLogger(workspace / "state" / "activity.jsonl")
    except Exception:
        return None


def _actor_markers() -> dict:
    """Capture the env attribution markers + process identity for an audit event."""
    return {
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "agent_session": os.environ.get("CODEBAND_AGENT_SESSION"),
        "role": os.environ.get("CODEBAND_ROLE"),
        "worker_id": os.environ.get("CODEBAND_WORKER_ID"),
    }


def _actor_label(markers: dict) -> str:
    """A short ``agent`` label for the event row from the captured markers."""
    if markers.get("role"):
        return markers["role"]
    return "agent" if markers.get("agent_session") else "human"


def record_cli_invocation(prog: str, argv: list[str]) -> Callable[[int], None]:
    """Log a ``cli_invocation`` event; return a completion callback.

    Call once at the top of the ``cb`` / ``cb-phase`` entrypoint with the
    program name and the raw argv (sans prog). Appends the invocation event
    (full argv, cwd, pid, timestamp, env markers) and returns a callback that,
    given the process exit code, appends the matching ``cli_completion`` event.
    Both writes use :meth:`ActivityLogger.log`'s ``fcntl.flock`` discipline and
    are wrapped so a logging failure never propagates into the command.
    """
    markers = _actor_markers()
    full_argv = [prog, *argv]
    logger_obj = _resolve_activity_logger(argv)
    label = _actor_label(markers)

    if logger_obj is not None:
        try:
            logger_obj.log(
                EventType.CLI_INVOCATION,
                label,
                " ".join(full_argv),
                argv=full_argv,
                **markers,
            )
        except Exception:
            logger.debug("cli_invocation logging failed", exc_info=True)

    def _complete(exit_code: int) -> None:
        if logger_obj is None:
            return
        try:
            logger_obj.log(
                EventType.CLI_COMPLETION,
                label,
                f"{prog} exited {exit_code}",
                argv=full_argv,
                exit_code=exit_code,
                pid=markers["pid"],
                role=markers["role"],
            )
        except Exception:
            logger.debug("cli_completion logging failed", exc_info=True)

    return _complete


class ActivityReader:
    """Read and filter the activity log.

    Accepts either a path on the host filesystem (default) or a callable
    that returns the raw JSONL text — the latter lets distributed-mode
    callers fetch the log via ``docker compose exec`` without changing
    the parsing/filtering logic.
    """

    def __init__(
        self,
        log_path: Path | None = None,
        *,
        text_loader: Callable[[], str] | None = None,
    ):
        if (log_path is None) == (text_loader is None):
            raise ValueError("Provide exactly one of log_path or text_loader")
        self._path = log_path
        self._text_loader = text_loader

    def _load_text(self) -> str:
        if self._text_loader is not None:
            return self._text_loader()
        if self._path is None:
            raise RuntimeError("ActivityReader has neither log_path nor text_loader")
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")

    def read(
        self,
        *,
        agent: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[ActivityEvent]:
        """Read events, optionally filtered by agent, type, or time."""
        text = self._load_text().strip()
        if not text:
            return []

        events: list[ActivityEvent] = []
        for line in text.splitlines():
            if not line:
                continue
            # One torn/malformed line (a crash mid-append, a corrupted byte)
            # must not kill `cb log` forever — skip it with a warning, the
            # same policy as LocalMemoryStore._iter_records (S6-F8).
            try:
                data = json.loads(line)
                if agent and data["agent"] != agent:
                    continue
                if event_type and data["event_type"] != event_type:
                    continue
                if since:
                    ts = datetime.fromisoformat(data["timestamp"])
                    if ts < since:
                        continue
                events.append(ActivityEvent(**data))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Skipping malformed activity log line: %s", exc)
        return events
