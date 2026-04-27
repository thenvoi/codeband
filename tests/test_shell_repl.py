"""Tests for shell/repl.py lifecycle wiring.

These tests target the small pure-Python helpers that drive shutdown and
error surfacing, not the full prompt loop (which depends on a TTY).
"""

from __future__ import annotations

import asyncio
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeband.shell.repl import (
    _announce_ready,
    _log_task_failure,
    _orchestrator_done_callback,
    _print_attached_roster,
    _run_preflight,
    start,
)


@pytest.mark.asyncio
async def test_log_task_failure_silent_on_clean_exit():
    async def coro():
        return None
    task = asyncio.create_task(coro(), name="quiet")
    await task

    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_task_failure(task)
    assert buf.getvalue() == ""


@pytest.mark.asyncio
async def test_log_task_failure_silent_on_cancel():
    async def coro():
        await asyncio.sleep(60)
    task = asyncio.create_task(coro(), name="cancelled")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_task_failure(task)
    assert buf.getvalue() == ""


@pytest.mark.asyncio
async def test_log_task_failure_prints_on_crash():
    async def coro():
        raise RuntimeError("boom")
    task = asyncio.create_task(coro(), name="loud")
    with pytest.raises(RuntimeError):
        await task

    buf = io.StringIO()
    with redirect_stdout(buf):
        _log_task_failure(task)
    out = buf.getvalue()
    assert "[loud] crashed" in out
    assert "RuntimeError" in out
    assert "boom" in out


@pytest.mark.asyncio
async def test_orchestrator_callback_sets_shell_exit_on_crash():
    session = MagicMock()
    session.app.is_running = False
    shell_exit = asyncio.Event()
    callback = _orchestrator_done_callback(session, shell_exit)

    async def coro():
        raise ValueError("dead")
    task = asyncio.create_task(coro(), name="orchestrator")
    with pytest.raises(ValueError):
        await task

    buf = io.StringIO()
    with redirect_stdout(buf):
        callback(task)

    assert shell_exit.is_set()
    assert "orchestrator" in buf.getvalue()
    assert "ValueError" in buf.getvalue()


@pytest.mark.asyncio
async def test_orchestrator_callback_sets_shell_exit_on_clean_unexpected_exit():
    """Clean orchestrator exit before /quit is treated as abnormal."""
    session = MagicMock()
    session.app.is_running = False
    shell_exit = asyncio.Event()
    callback = _orchestrator_done_callback(session, shell_exit)

    async def coro():
        return None
    task = asyncio.create_task(coro(), name="orchestrator")
    await task

    buf = io.StringIO()
    with redirect_stdout(buf):
        callback(task)

    assert shell_exit.is_set()
    assert "exited unexpectedly" in buf.getvalue()


@pytest.mark.asyncio
async def test_orchestrator_callback_silent_during_user_shutdown():
    """If shell_exit was already set (user typed /quit), no spurious warning."""
    session = MagicMock()
    session.app.is_running = False
    shell_exit = asyncio.Event()
    shell_exit.set()  # user already asked to quit
    callback = _orchestrator_done_callback(session, shell_exit)

    async def coro():
        return None
    task = asyncio.create_task(coro(), name="orchestrator")
    await task

    buf = io.StringIO()
    with redirect_stdout(buf):
        callback(task)
    assert buf.getvalue() == ""


@pytest.mark.asyncio
async def test_orchestrator_callback_calls_app_exit_when_prompt_running():
    session = MagicMock()
    session.app.is_running = True
    shell_exit = asyncio.Event()
    callback = _orchestrator_done_callback(session, shell_exit)

    async def coro():
        raise RuntimeError("late crash")
    task = asyncio.create_task(coro(), name="orchestrator")
    with pytest.raises(RuntimeError):
        await task

    with redirect_stdout(io.StringIO()):
        callback(task)

    session.app.exit.assert_called_once()


# ─── Preflight ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_preflight_aborts_on_claude_failure():
    from codeband.config import (
        AgentsConfig,
        CodebandConfig,
        FrameworkPool,
        PoolEntry,
        RepoConfig,
    )
    from codeband.preflight import PreflightError

    config = CodebandConfig(
        repo=RepoConfig(url="https://x/y.git", branch="main"),
        agents=AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=0),
            ),
        ),
    )
    err = PreflightError(summary="auth missing", remediation="set ANTHROPIC_API_KEY")

    buf = io.StringIO()
    with patch(
        "codeband.preflight.check_claude_auth",
        new=AsyncMock(return_value=err),
    ), redirect_stdout(buf):
        ok = await _run_preflight(config)

    assert ok is False
    assert "auth missing" in buf.getvalue()
    assert "set ANTHROPIC_API_KEY" in buf.getvalue()


@pytest.mark.asyncio
async def test_run_preflight_passes_when_clean():
    from codeband.config import (
        AgentsConfig,
        CodebandConfig,
        FrameworkPool,
        PoolEntry,
        RepoConfig,
    )

    config = CodebandConfig(
        repo=RepoConfig(url="https://x/y.git", branch="main"),
        agents=AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=0),
            ),
        ),
    )

    with patch(
        "codeband.preflight.check_claude_auth",
        new=AsyncMock(return_value=None),
    ), patch(
        "codeband.preflight.check_codex_auth",
        new=AsyncMock(return_value=None),
    ), redirect_stdout(io.StringIO()):
        ok = await _run_preflight(config)

    assert ok is True


# ─── Attach mode ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_mode_skips_orchestrator_and_preflight(tmp_path: Path):
    """In attach mode, start() must not call run_local or preflight."""
    from codeband.config import (
        AgentsConfig,
        CodebandConfig,
        FrameworkPool,
        PoolEntry,
        RepoConfig,
    )

    config = CodebandConfig(
        repo=RepoConfig(url="https://x/y.git", branch="main"),
        agents=AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=0),
            ),
        ),
    )

    # No BAND_API_KEY → start() returns early before doing anything
    # interesting; that's the simplest deterministic exit. We just want
    # to confirm it does NOT touch run_local or preflight along the way.
    with patch.dict("os.environ", {}, clear=False), \
         patch("codeband.orchestration.runner.run_local") as mock_run_local, \
         patch("codeband.shell.repl._run_preflight") as mock_preflight:
        # Force missing API key path
        import os
        os.environ.pop("BAND_API_KEY", None)
        with redirect_stdout(io.StringIO()):
            await start(config, tmp_path, attach=True)

    mock_run_local.assert_not_called()
    mock_preflight.assert_not_called()


# ─── Banner helpers ────────────────────────────────────────────────────────


def test_print_attached_roster_lists_configured_agents(tmp_path: Path):
    """Attached shell must surface the agent roster from ``agent_config.yaml``.

    Container stdout (where the runner's "Agents (N): …" banner lands in
    docker mode) is invisible to the user, so the shell needs to print a
    parallel banner from config.
    """
    yaml_text = """\
agents:
  conductor:
    agent_id: agent-cond
    api_key: dummy-key-cond
  mergemaster:
    agent_id: agent-mm
    api_key: dummy-key-mm
  coder-claude_sdk-0:
    agent_id: agent-coder
    api_key: dummy-key-coder
"""
    (tmp_path / "agent_config.yaml").write_text(yaml_text)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_attached_roster(tmp_path)
    out = buf.getvalue()

    assert "conductor" in out
    assert "mergemaster" in out
    assert "coder-claude_sdk-0" in out
    assert "watchdog" in out  # synthesized for parity with standalone banner
    assert "Agents (4):" in out


def test_print_attached_roster_handles_missing_config(tmp_path: Path):
    """No agent_config.yaml must not crash the shell."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        _print_attached_roster(tmp_path)
    assert "no agent_config.yaml" in buf.getvalue().lower()


@pytest.mark.asyncio
async def test_announce_ready_with_event_waits():
    """When a ready event is given, the hint prints only after it's set."""
    event = asyncio.Event()
    buf = io.StringIO()

    async def announce():
        with redirect_stdout(buf):
            await _announce_ready(event)

    task = asyncio.create_task(announce())
    await asyncio.sleep(0.05)
    assert "Ready" not in buf.getvalue()  # still waiting
    event.set()
    await task
    assert "Ready; use /help" in buf.getvalue()


@pytest.mark.asyncio
async def test_announce_ready_without_event_prints_immediately():
    """Attach mode passes None — the hint must print right away."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        await _announce_ready(None)
    assert "Ready; use /help" in buf.getvalue()


# ─── /down subprocess context ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_docker_down_runs_with_project_cwd_and_env(tmp_path: Path):
    """`/down` must invoke ``docker compose down`` with ``cwd=project_dir``
    and ``CODEBAND_PROJECT_DIR`` so compose interpolation matches the
    original ``cb up`` context — even if the shell was started from a
    different working directory.
    """
    from codeband.config import (
        AgentsConfig,
        CodebandConfig,
        FrameworkPool,
        PoolEntry,
        RepoConfig,
    )
    from codeband.shell.commands import SlashContext
    from codeband.shell.repl import _docker_down

    project = tmp_path / "proj"
    project.mkdir()
    compose_file = project / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    config = CodebandConfig(
        repo=RepoConfig(url="https://x/y.git", branch="main"),
        agents=AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=0),
            ),
        ),
    )
    ctx = SlashContext(
        config=config,
        project_dir=project,
        backend=MagicMock(),
        shutdown_event=None,
        compose_file=compose_file,
    )

    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        proc = MagicMock()

        async def fake_wait():
            return 0
        proc.wait = fake_wait
        return proc

    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=fake_create_subprocess_exec,
    ), redirect_stdout(io.StringIO()):
        await _docker_down(ctx)

    assert captured["args"][:4] == ("docker", "compose", "-f", str(compose_file))
    assert captured["args"][-2:] == ("down", "--remove-orphans")
    assert captured["cwd"] == str(project)
    assert captured["env"]["CODEBAND_PROJECT_DIR"] == str(project)
    assert captured["env"]["COMPOSE_PROJECT_NAME"] == "codeband-proj"
