"""Tests for the shell slash-command parser and dispatcher."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from codeband.shell.commands import (
    REGISTRY,
    SlashContext,
    _parse_kv_args,
    dispatch,
    parse_line,
)


def test_parse_line_strips_slash_and_returns_args():
    assert parse_line("/task add a feature") == ("task", "add a feature")
    assert parse_line("  /diff coder-claude_sdk-0 -p  ") == ("diff", "coder-claude_sdk-0 -p")


def test_parse_line_no_slash_returns_empty():
    assert parse_line("not a command") == ("", "")
    assert parse_line("") == ("", "")
    assert parse_line("/") == ("", "")


def test_parse_line_command_only():
    assert parse_line("/help") == ("help", "")
    assert parse_line("/quit  ") == ("quit", "")


def test_parse_kv_args_handles_flags():
    pos, flags = _parse_kv_args("--smart --limit 10 foo", known_flags={"smart"})
    assert pos == ["foo"]
    assert flags["smart"] is True
    assert flags["limit"] == "10"


def test_parse_kv_args_supports_equals_form():
    _, flags = _parse_kv_args("--reason=\"too risky\" 42", known_flags=set())
    assert flags["reason"] == "too risky"


def test_parse_kv_args_recognizes_short_p():
    pos, flags = _parse_kv_args("worker -p", known_flags={"patch"})
    assert pos == ["worker"]
    assert flags.get("patch") is True


def test_registry_contains_expected_commands():
    expected = {
        "task", "issue", "issues", "prs", "diff", "status", "pending",
        "approve", "reject", "log", "usage", "scale", "doctor", "down",
        "help", "quit",
    }
    assert expected <= set(REGISTRY)


@pytest.mark.asyncio
async def test_dispatch_unknown_prints_hint(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/nope", ctx)
    assert result is None
    assert "Unknown command" in buf.getvalue()


@pytest.mark.asyncio
async def test_dispatch_quit_returns_quit(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    result = await dispatch("/quit", ctx)
    assert result == "quit"


@pytest.mark.asyncio
async def test_down_returns_down_when_compose_file_present(tmp_path: Path):
    """Even when workspace.mode is local (the default), /down works as
    long as the shell is attached to a docker stack — tracked by
    ctx.compose_file. Regression for: cb up forces attach mode while
    leaving workspace.mode at the default."""
    ctx = _make_ctx(tmp_path, compose_file=tmp_path / "docker-compose.yml")
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/down", ctx)
    assert result == "down"
    # No "Use /quit instead" diagnostic should have been printed.
    assert "Use /quit instead" not in buf.getvalue()


@pytest.mark.asyncio
async def test_down_refuses_when_not_attached(tmp_path: Path):
    ctx = _make_ctx(tmp_path, compose_file=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/down", ctx)
    assert result is None
    assert "no docker stack is attached" in buf.getvalue()


@pytest.mark.asyncio
async def test_dispatch_help_lists_registered_commands(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/help", ctx)
    assert result is None
    out = buf.getvalue()
    assert "/diff" in out
    assert "/task" in out
    assert "/quit" in out


@pytest.mark.asyncio
async def test_dispatch_diff_no_args_lists_workers_or_advises(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        await dispatch("/diff", ctx)
    out = buf.getvalue()
    # Either Usage prompt (if there are workers) or the no-workers message.
    assert "/diff <worker>" in out or "No coder or mergemaster" in out


# ── helpers ──────────────────────────────────────────────────────────────

def _make_ctx(tmp_path: Path, *, compose_file: Path | None = None) -> SlashContext:
    """Build a minimal SlashContext with a stub backend."""
    from codeband.config import (
        AgentsConfig, CodebandConfig, RepoConfig,
    )

    config = CodebandConfig(
        repo=RepoConfig(url="https://example.com/r.git", branch="main"),
        agents=AgentsConfig(),
    )
    backend = _StubBackend()
    return SlashContext(
        config=config,
        project_dir=tmp_path,
        backend=backend,
        shutdown_event=None,
        compose_file=compose_file,
    )


class _StubBackend:
    def list_worktrees(self):
        return {}

    def worktree_diff(self, *args, **kwargs):
        raise NotImplementedError

    def read_activity_events(self, **kwargs):
        return []

    def make_activity_reader(self):
        raise NotImplementedError
