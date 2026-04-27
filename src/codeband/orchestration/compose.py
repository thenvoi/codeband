"""Helpers for locating and invoking ``docker-compose.yml``.

Single owner of the *compose context* — the cwd + ``CODEBAND_PROJECT_DIR``
env var that the project's compose file uses for path interpolation
(``${CODEBAND_PROJECT_DIR:-.}/codeband.yaml`` etc.). Without that
context, compose substitutes ``.`` (the cwd) and resolves to the wrong
project files when the caller is in a different directory.

Every place that shells out to ``docker compose`` for this project
should go through :func:`compose_run` (sync) or :func:`compose_run_async`
so the cwd + env are guaranteed correct. Today's callers:
``cli.up`` / ``cli.down``, ``shell.fs.SharedComposeBackend._exec``,
``shell.fs._compose_stack_running``, ``shell.repl._docker_down``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path


class ComposeFileNotFound(FileNotFoundError):
    """Raised when no docker-compose.yml can be located."""


def find_compose_file(project: Path) -> Path:
    """Return the project's compose file, or raise :class:`ComposeFileNotFound`.

    Search order: ``<project>/docker/docker-compose.yml``,
    ``<project>/docker-compose.yml``, then ``docker/docker-compose.yml``
    in the codeband source tree (works for editable installs).
    """
    for candidate in (
        project / "docker" / "docker-compose.yml",
        project / "docker-compose.yml",
    ):
        if candidate.exists():
            return candidate

    pkg_root = Path(__file__).resolve().parent.parent.parent.parent
    pkg_compose = pkg_root / "docker" / "docker-compose.yml"
    if pkg_compose.exists():
        return pkg_compose

    raise ComposeFileNotFound(
        "No docker-compose.yml found. Run 'codeband init' first."
    )


def compose_project_name(project_dir: Path) -> str:
    """Return a stable, descriptive Docker Compose project name.

    The bundled compose file lives under this package's ``docker/`` directory.
    Without an explicit project name Docker Compose derives names such as
    ``docker_default`` from that directory, which is ambiguous and can collide
    across Codeband projects.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", project_dir.name.lower()).strip("-_")
    if not slug:
        slug = "project"
    if not slug[0].isalnum():
        slug = f"project-{slug}"
    return f"codeband-{slug}"


def compose_env(project_dir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return ``os.environ`` augmented with the compose-context env vars.

    ``CODEBAND_PROJECT_DIR`` is always forced to ``project_dir`` (absolute)
    so the compose file's ``${CODEBAND_PROJECT_DIR:-.}`` substitutions
    point at the right host paths regardless of the caller's cwd.

    Pass ``extra`` to merge in command-specific vars (e.g.
    ``CODEBAND_DEBUG=1``).
    """
    env = os.environ.copy()
    env["CODEBAND_PROJECT_DIR"] = str(project_dir)
    env["COMPOSE_PROJECT_NAME"] = compose_project_name(project_dir)
    if extra:
        env.update(extra)
    return env


def compose_run(
    project_dir: Path,
    compose_file: Path,
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    timeout: float | None = 120,
    extra_env: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``docker compose -f <file> <args>`` with the project compose context.

    Always runs from ``project_dir``. By default the env is built from
    :func:`compose_env`; pass a complete ``env`` dict to override (used
    by ``cb up`` which mutates a full env via auth-detection helpers).
    Either way, ``CODEBAND_PROJECT_DIR`` is forced to ``project_dir``.

    ``args`` is the verb + flags (e.g. ``["ps", "--status", "running", "--quiet"]``,
    ``["up", "-d", "--build"]``, ``["exec", "-T", "conductor", "cat", "/x"]``).

    ``capture=True`` (default) buffers stdout/stderr; pass ``False`` for
    streaming subprocesses (``cb up`` foreground mode).
    """
    if env is None:
        env = compose_env(project_dir, extra_env)
    else:
        # Caller supplied a full env — guarantee compose context anyway.
        env = {
            **env,
            "CODEBAND_PROJECT_DIR": str(project_dir),
            "COMPOSE_PROJECT_NAME": compose_project_name(project_dir),
        }

    full = ["docker", "compose", "-f", str(compose_file), *args]
    return subprocess.run(
        full,
        cwd=str(project_dir),
        env=env,
        capture_output=capture,
        text=capture,  # only meaningful when capturing
        check=check,
        timeout=timeout,
    )


async def compose_run_async(
    project_dir: Path,
    compose_file: Path,
    args: list[str],
    *,
    extra_env: dict[str, str] | None = None,
) -> int:
    """Async variant for streaming subprocesses (``/down``).

    Inherits the parent's stdout/stderr — output renders inline through
    prompt_toolkit's ``patch_stdout``. Returns the process exit code.
    """
    full = ["docker", "compose", "-f", str(compose_file), *args]
    proc = await asyncio.create_subprocess_exec(
        *full,
        cwd=str(project_dir),
        env=compose_env(project_dir, extra_env),
    )
    return await proc.wait()


def compose_stack_running(compose_file: Path, project_dir: Path) -> bool:
    """Probe ``docker compose ps`` to see whether any service is running.

    ``--status running`` filters to actually-up containers. Returns False
    on any docker-side error (docker not installed, daemon down) so
    callers can fall back without crashing.
    """
    try:
        result = compose_run(
            project_dir, compose_file,
            ["ps", "--status", "running", "--quiet"],
            check=False, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())
