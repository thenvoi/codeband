"""Mode-aware filesystem access for the interactive shell.

API-bound slash commands talk to Band.ai and work the same in both modes.
Filesystem-bound slash commands (``/diff``, ``/log``, ``/usage``) need a
backend that reads either host paths (local mode) or files inside an agent
container (distributed mode, via ``docker compose exec``).

Both backends implement the same ``FSBackend`` Protocol so callers don't
care which is in play. Pick one with :func:`make_backend`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from codeband.config import CodebandConfig, DeploymentMode
from codeband.monitoring.activity_log import ActivityEvent, ActivityReader
from codeband.orchestration.compose import compose_run, compose_stack_running
from codeband.workspace.diff import (
    DiffError,
    GitRunner,
    WorkerDiff,
    compute_worker_diff,
    compute_worker_diff_with_runner,
)


class FSBackend(Protocol):
    """Mode-agnostic filesystem operations used by slash commands."""

    def list_worktrees(self) -> dict[str, Path]:
        """Return ``{worker_id: worktree_path}`` for every worker that has one.

        In distributed mode the path is the in-container location
        (``/workspace/worktrees/<id>``) — used only for display.
        """
        ...

    def worktree_diff(
        self,
        worker_id: str,
        base_branch: str,
        *,
        include_patch: bool = False,
    ) -> WorkerDiff:
        """Compute the worker's diff against the base-branch fork point."""
        ...

    def read_activity_events(
        self,
        *,
        agent: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[ActivityEvent]:
        """Read filtered events from the activity log."""
        ...

    def make_activity_reader(self) -> ActivityReader:
        """Return an :class:`ActivityReader` bound to this backend's log location."""
        ...


# ─── Local backend ────────────────────────────────────────────────────────


@dataclass
class LocalBackend:
    """Reads host paths under ``<project>/<workspace>/...``."""

    config: CodebandConfig
    project_dir: Path

    def list_worktrees(self) -> dict[str, Path]:
        from codeband.workspace.init import resolve_layout

        layout = resolve_layout(self._resolved_config())
        candidates: dict[str, Path] = dict(layout.coder_worktrees)
        if layout.mergemaster_worktree is not None:
            candidates["mergemaster"] = layout.mergemaster_worktree
        return candidates

    def worktree_diff(
        self,
        worker_id: str,
        base_branch: str,
        *,
        include_patch: bool = False,
    ) -> WorkerDiff:
        worktrees = self.list_worktrees()
        if worker_id not in worktrees:
            raise DiffError(f"Unknown worker: {worker_id}")
        return compute_worker_diff(
            worktrees[worker_id], worker_id, base_branch, include_patch=include_patch,
        )

    def read_activity_events(
        self,
        *,
        agent: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[ActivityEvent]:
        return self.make_activity_reader().read(
            agent=agent, event_type=event_type, since=since,
        )

    def make_activity_reader(self) -> ActivityReader:
        return ActivityReader(self._state_dir() / "activity.jsonl")

    def _resolved_config(self) -> CodebandConfig:
        from codeband.orchestration.runner import _resolve_workspace_config
        return _resolve_workspace_config(self.config, self.project_dir)

    def _state_dir(self) -> Path:
        return Path(self._resolved_config().workspace.path) / "state"


# ─── Shared-compose backend ────────────────────────────────────────────────

# The container path layout below is fixed by ``docker/docker-compose.yml``'s
# named volumes:
#   /workspace/repo.git           bare_repo volume
#   /workspace/worktrees/<id>     worktrees volume
#   /workspace/state/             shared_state volume
# Every agent service inherits these mounts via &volumes-base, so any
# running service can serve as the exec target. ``conductor`` is a
# guaranteed singleton.
#
# This backend ASSUMES that single-host shape (one compose file, all
# services sharing the same named volumes). It does NOT support a true
# multi-host distributed deployment where each agent has its own
# workspace — that's a v2 ``MultiHostBackend``, not built yet.
_CONTAINER_WORKTREES = "/workspace/worktrees"
_CONTAINER_STATE = "/workspace/state"
_DEFAULT_EXEC_SERVICE = "conductor"


@dataclass
class SharedComposeBackend:
    """Reads files inside an agent container via ``docker compose exec``.

    Assumes a single-host docker-compose stack with shared named volumes
    (the layout in ``docker/docker-compose.yml``). Any agent service can
    serve as the exec target since they all mount the same volumes;
    ``conductor`` is the default because it's always a singleton.
    """

    config: CodebandConfig
    project_dir: Path
    compose_file: Path
    service: str = _DEFAULT_EXEC_SERVICE

    def list_worktrees(self) -> dict[str, Path]:
        from codeband.workspace.init import resolve_layout

        layout = resolve_layout(self.config)
        # Worker IDs are deterministic from config; we don't probe the
        # container. Paths returned are in-container locations — printed
        # for display, never opened on the host.
        candidates: dict[str, Path] = {
            wid: Path(_CONTAINER_WORKTREES) / wid
            for wid in layout.coder_worktrees
        }
        if layout.mergemaster_worktree is not None:
            candidates["mergemaster"] = Path(_CONTAINER_WORKTREES) / "mergemaster"
        return candidates

    def worktree_diff(
        self,
        worker_id: str,
        base_branch: str,
        *,
        include_patch: bool = False,
    ) -> WorkerDiff:
        worktrees = self.list_worktrees()
        if worker_id not in worktrees:
            raise DiffError(f"Unknown worker: {worker_id}")

        container_path = worktrees[worker_id]
        runner: GitRunner = self._make_git_runner(container_path)
        return compute_worker_diff_with_runner(
            runner,
            worker_id=worker_id,
            worktree=container_path,
            base_branch=base_branch,
            include_patch=include_patch,
        )

    def read_activity_events(
        self,
        *,
        agent: str | None = None,
        event_type: str | None = None,
        since: datetime | None = None,
    ) -> list[ActivityEvent]:
        return self.make_activity_reader().read(
            agent=agent, event_type=event_type, since=since,
        )

    def make_activity_reader(self) -> ActivityReader:
        def loader() -> str:
            try:
                return self._exec(["cat", f"{_CONTAINER_STATE}/activity.jsonl"])
            except _DockerExecError as e:
                # Missing log = empty (agents may not have written anything yet).
                if "No such file" in str(e):
                    return ""
                raise

        return ActivityReader(text_loader=loader)

    def _make_git_runner(self, container_path: Path) -> GitRunner:
        def runner(args: list[str]) -> str:
            try:
                return self._exec(["git", "-C", str(container_path), *args])
            except _DockerExecError as e:
                raise DiffError(str(e)) from e
        return runner

    def _exec(self, command: list[str]) -> str:
        """Run ``command`` inside ``self.service`` and return stdout.

        Delegates to :func:`compose_run`, which owns the cwd + env that
        keep compose interpolation pointing at the same project files
        the original ``cb up`` used (even when the shell was launched
        from a different working directory).
        """
        try:
            result = compose_run(
                self.project_dir, self.compose_file,
                ["exec", "-T", self.service, *command],
            )
        except subprocess.CalledProcessError as e:
            raise _DockerExecError(
                f"`{' '.join(command)}` failed in container "
                f"'{self.service}': {e.stderr.strip()}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise _DockerExecError(
                f"`{' '.join(command)}` timed out in container '{self.service}'"
            ) from e
        return result.stdout


class _DockerExecError(Exception):
    """Raised when docker compose exec fails."""


# ─── Factory ──────────────────────────────────────────────────────────────


def make_backend(
    config: CodebandConfig,
    project_dir: Path,
    *,
    attach: bool = False,
    compose_file: Path | None = None,
) -> FSBackend:
    """Pick the right FS backend for the current shell / CLI context.

    Selection rule (first-match):

    1. ``attach=True`` (shell launched as a thin client, e.g. by
       ``cb up``) AND a compose stack with running containers exists →
       :class:`SharedComposeBackend`. This is the path that prevents
       ``cb up``'s post-exec shell from reading stale host worktrees
       when ``workspace.mode`` is the default ``local``.
    2. ``workspace.mode == DISTRIBUTED`` → :class:`SharedComposeBackend`.
       One-shot CLI commands (``cb diff``, ``cb log``, ``cb usage``) hit
       this path: the user explicitly configured distributed, so honor
       it without probing.
    3. Otherwise → :class:`LocalBackend` (host filesystem reads).

    A standalone shell with ``mode: local`` never picks the compose
    backend even if a stack happens to be running, because local agents
    own the host worktrees and a stale compose stack shouldn't shadow
    them.

    Pass ``compose_file`` to skip the lookup if you already have it.
    """
    if attach:
        try:
            if compose_file is None:
                from codeband.orchestration.compose import find_compose_file
                compose_file = find_compose_file(project_dir)
        except FileNotFoundError:
            # No compose file on attach: fall through to LocalBackend so
            # the shell still opens. /diff & /log will fail at call time
            # if the host paths aren't populated either, but that's a
            # honest "no data available" signal.
            return LocalBackend(config=config, project_dir=project_dir)

        if compose_stack_running(compose_file, project_dir):
            return SharedComposeBackend(
                config=config, project_dir=project_dir, compose_file=compose_file,
            )
        # Attach was requested but nothing is running — fall through.

    if config.workspace.mode == DeploymentMode.DISTRIBUTED:
        if compose_file is None:
            from codeband.orchestration.compose import find_compose_file
            compose_file = find_compose_file(project_dir)
        return SharedComposeBackend(
            config=config, project_dir=project_dir, compose_file=compose_file,
        )

    return LocalBackend(config=config, project_dir=project_dir)
