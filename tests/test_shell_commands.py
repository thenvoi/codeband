"""Tests for the shell slash-command parser and dispatcher."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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


@pytest.mark.asyncio
async def test_pending_uses_slash_command_hints_in_shell(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.config.to_yaml(tmp_path / "codeband.yaml")

    fake_result = type("Result", (), {
        "returncode": 0,
        "stdout": json.dumps([{
            "number": 7,
            "title": "Add review gate",
            "labels": [],
            "comments": [{"body": "Review PASSED (risk: high)"}],
        }]),
        "stderr": "",
    })()

    buf = io.StringIO()
    with patch("subprocess.run", return_value=fake_result), redirect_stdout(buf):
        result = await dispatch("/pending", ctx)

    assert result is None
    out = buf.getvalue()
    assert "Approve:  /approve <number>" in out
    assert "Reject:   /reject <number> --reason" in out
    assert "cb approve" not in out


@pytest.mark.asyncio
async def test_approve_missing_room_uses_slash_task_hint(tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.config.to_yaml(tmp_path / "codeband.yaml")

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/approve 7", ctx)

    assert result is None
    out = buf.getvalue()
    assert "Start a task first with '/task' or '/issue'" in out
    assert "cb task" not in out


@pytest.mark.asyncio
async def test_scale_omits_cli_next_steps_in_shell(tmp_path: Path):
    """`/scale` prints the shell-tailored next steps, not the cli-worded block."""
    ctx = _make_ctx(tmp_path)  # compose_file=None → local/shell restart hint
    ctx.config.to_yaml(tmp_path / "codeband.yaml")

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/scale coders.claude_sdk=2", ctx)

    assert result is None
    out = buf.getvalue()
    assert "Scaled coders.claude_sdk to 2" in out          # printed in both modes
    assert "Restart the interactive shell to pick up changes" not in out  # cli block gated
    assert "/quit and re-run" in out                        # shell-tailored hint


@pytest.mark.asyncio
async def test_log_bad_since_prints_clean_error(tmp_path: Path):
    """A malformed /log --since must not crash the shell or dump a traceback."""
    ctx = _make_ctx(tmp_path)

    buf = io.StringIO()
    with redirect_stdout(buf):
        result = await dispatch("/log --since garbage", ctx)

    assert result is None
    out = buf.getvalue()
    assert "Error:" in out
    assert "ISO date" in out
    assert "Command crashed" not in out


def test_shell_parse_since_accepts_relative_and_iso():
    from codeband.shell.commands import _parse_since

    assert _parse_since("2h") is not None
    assert _parse_since("2026-05-01") is not None


def test_shell_parse_since_rejects_garbage():
    from codeband.shell.commands import _parse_since

    with pytest.raises(ValueError):
        _parse_since("garbage")
    with pytest.raises(ValueError):
        _parse_since("1x")


@pytest.mark.asyncio
async def test_log_type_filter_is_comma_separated(tmp_path: Path, monkeypatch):
    """`/log --type A,B` filters to the union of types (aligned with feed)."""
    import codeband.shell.commands as cmds
    from codeband.monitoring.activity_log import ActivityEvent

    ctx = _make_ctx(tmp_path)
    ctx.backend.events = [
        ActivityEvent(timestamp="2026-05-30T00:00:00+00:00", event_type="NUDGE",
                      agent="a", summary="n"),
        ActivityEvent(timestamp="2026-05-30T00:00:01+00:00", event_type="ERROR",
                      agent="a", summary="e"),
        ActivityEvent(timestamp="2026-05-30T00:00:02+00:00", event_type="LLM_USAGE",
                      agent="a", summary="u"),
    ]

    captured: list = []
    monkeypatch.setattr(cmds, "render_activity_events", lambda evs: captured.append(list(evs)))

    result = await dispatch("/log --type NUDGE,ERROR", ctx)

    assert result is None
    assert {e.event_type for e in captured[0]} == {"NUDGE", "ERROR"}


# ── helpers ──────────────────────────────────────────────────────────────

def _make_ctx(tmp_path: Path, *, compose_file: Path | None = None) -> SlashContext:
    """Build a minimal SlashContext with a stub backend."""
    from codeband.config import (
        AgentsConfig, CodebandConfig, RepoConfig,
    )

    config = CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/r.git", branch="main"),
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
    events: list = []

    def list_worktrees(self):
        return {}

    def worktree_diff(self, *args, **kwargs):
        raise NotImplementedError

    def read_activity_events(self, **kwargs):
        # The /log handler now reads all events and filters client-side, so
        # the stub ignores any event_type kwarg and returns everything.
        return list(self.events)

    def make_activity_reader(self):
        raise NotImplementedError
