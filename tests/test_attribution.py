"""Tests for Stage-3 attribution (PR2).

CLI invocation/completion logging, the per-subcommand cb-phase role gate, and
the unchanged ``cb approve`` agent-session guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from codeband.cli import handoff
from codeband.cli.handoff import EXIT_ROLE_MISMATCH, _check_role
from codeband.monitoring.activity_log import (
    ActivityReader,
    EventType,
    record_cli_invocation,
)


# ── role gate matrix (2c) ────────────────────────────────────────────────────

# (command, allowed_role, a_disallowed_role)
_MATRIX = [
    ("start", "coder", "reviewer"),
    ("start", "conductor", "mergemaster"),
    ("verify", "coder", "reviewer"),
    ("review", "reviewer", "coder"),
    ("merge", "mergemaster", "coder"),
    ("abandon", "conductor", "coder"),
    ("resume", "conductor", "reviewer"),
]


@pytest.mark.parametrize("command,allowed,disallowed", _MATRIX)
def test_role_gate_pass_and_refuse(command, allowed, disallowed, monkeypatch):
    monkeypatch.setenv("CODEBAND_ROLE", allowed)
    assert _check_role(command) is None  # allowed role passes

    monkeypatch.setenv("CODEBAND_ROLE", disallowed)
    assert _check_role(command) == EXIT_ROLE_MISMATCH  # mismatch refused


def test_role_gate_unset_is_operator_path(monkeypatch):
    """No CODEBAND_ROLE → every command is allowed (the human operator path)."""
    monkeypatch.delenv("CODEBAND_ROLE", raising=False)
    for command, _allowed, _ in _MATRIX:
        assert _check_role(command) is None


def test_role_gate_unknown_command_is_ungated(monkeypatch):
    monkeypatch.setenv("CODEBAND_ROLE", "coder")
    assert _check_role("not-a-command") is None


def test_role_mismatch_short_circuits_main(monkeypatch, capsys):
    """A role mismatch returns EXIT_ROLE_MISMATCH from main without running the leg
    (no store/config needed — the gate fires before dispatch)."""
    monkeypatch.setenv("CODEBAND_ROLE", "reviewer")
    code = handoff.main(["merge", "st-1", "--pr", "1"])
    assert code == EXIT_ROLE_MISMATCH
    err = capsys.readouterr().err
    assert "[role_mismatch]" in err
    assert "mergemaster" in err


# ── CLI invocation logging (2a) ──────────────────────────────────────────────

def _project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    project.mkdir()
    ws = tmp_path / "ws"
    (project / "codeband.yaml").write_text(
        "repo:\n"
        "  url: https://github.com/o/r.git\n"
        "  branch: main\n"
        "workspace:\n"
        f"  path: {ws}\n"
    )
    return project, ws


def test_invocation_and_completion_events_logged(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEBAND_PROJECT_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.delenv("CODEBAND_AGENT_SESSION", raising=False)
    monkeypatch.setenv("CODEBAND_ROLE", "coder")
    project, ws = _project(tmp_path)

    complete = record_cli_invocation("cb-phase", ["verify", "st-1", "--dir", str(project)])
    complete(4)

    reader = ActivityReader(ws / "state" / "activity.jsonl")
    events = reader.read()
    by_type = {e.event_type: e for e in events}

    assert EventType.CLI_INVOCATION in by_type
    assert EventType.CLI_COMPLETION in by_type

    inv = by_type[EventType.CLI_INVOCATION]
    assert inv.details["argv"] == ["cb-phase", "verify", "st-1", "--dir", str(project)]
    assert inv.details["pid"] > 0
    assert inv.details["role"] == "coder"
    assert "cwd" in inv.details
    assert inv.agent == "coder"  # actor label from the role marker

    comp = by_type[EventType.CLI_COMPLETION]
    assert comp.details["exit_code"] == 4


def test_invocation_logging_is_best_effort_without_config(tmp_path, monkeypatch):
    """No codeband.yaml in scope → logging silently no-ops, never raises."""
    monkeypatch.delenv("CODEBAND_PROJECT_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)  # empty dir, no codeband.yaml

    # Must not raise; returns a callable that also must not raise.
    complete = record_cli_invocation("cb", ["status"])
    complete(0)


def test_actor_label_is_human_without_markers(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEBAND_PROJECT_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE", raising=False)
    monkeypatch.delenv("CODEBAND_ROLE", raising=False)
    monkeypatch.delenv("CODEBAND_AGENT_SESSION", raising=False)
    project, ws = _project(tmp_path)

    record_cli_invocation("cb", ["status", "--dir", str(project)])(0)

    events = ActivityReader(ws / "state" / "activity.jsonl").read()
    assert events[0].agent == "human"


# ── runner spawn seam exports CODEBAND_ROLE (2b) ─────────────────────────────

def test_spawn_seam_exports_role_when_given(tmp_path, monkeypatch):
    import os

    from codeband.orchestration.runner import _export_project_dir_env

    # Track the keys with monkeypatch so its teardown restores them even though
    # the function under test mutates os.environ directly (the spawn seam).
    monkeypatch.setenv("CODEBAND_ROLE", "")
    monkeypatch.setenv("CODEBAND_AGENT_SESSION", "")
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", "")
    _export_project_dir_env(tmp_path, role="coder")

    assert os.environ["CODEBAND_ROLE"] == "coder"
    assert os.environ["CODEBAND_AGENT_SESSION"] == "1"


def test_spawn_seam_leaves_role_unset_in_local_mode(tmp_path, monkeypatch):
    """run_local passes no role (one process, many roles) → CODEBAND_ROLE unset."""
    import os

    from codeband.orchestration.runner import _export_project_dir_env

    monkeypatch.delenv("CODEBAND_ROLE", raising=False)
    monkeypatch.setenv("CODEBAND_AGENT_SESSION", "")
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", "")
    _export_project_dir_env(tmp_path)

    assert "CODEBAND_ROLE" not in os.environ


# ── cb approve agent-session guard unchanged (#46) ───────────────────────────

def test_cb_approve_still_refuses_in_agent_session(tmp_path, monkeypatch):
    from codeband.cli import cli

    project, _ = _project(tmp_path)
    monkeypatch.setenv("CODEBAND_AGENT_SESSION", "1")
    result = CliRunner().invoke(cli, ["approve", "1", "--dir", str(project)])
    assert result.exit_code != 0
    assert "human-approval primitive" in result.output
