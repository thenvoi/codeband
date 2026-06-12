"""Tests for project-dir context resolution (Batch 2a, PR A).

Covers the ONE shared resolution helper (``cli/handoff.py:
resolve_project_dir`` — explicit flag > ``$CODEBAND_PROJECT_DIR`` > cwd),
the runner seam that exports the env var into every spawned agent session,
and ``cb-phase``'s traceback-free top-level error handling for config/IO
failures (missing / empty / malformed ``codeband.yaml``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeband.cli import handoff
from codeband.state.store import StateStore

ROOM = "room-uuid-1"


# ── resolution-order matrix ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient CODEBAND_PROJECT_DIR may leak into these tests."""
    monkeypatch.delenv("CODEBAND_PROJECT_DIR", raising=False)


def test_explicit_flag_beats_env(monkeypatch, tmp_path):
    flag_dir = tmp_path / "from-flag"
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", str(env_dir))
    assert handoff.resolve_project_dir(str(flag_dir)) == flag_dir.resolve()


def test_env_beats_cwd(monkeypatch, tmp_path):
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", str(env_dir))
    monkeypatch.chdir(tmp_path)
    assert handoff.resolve_project_dir(".") == env_dir.resolve()


def test_cwd_is_the_last_resort(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert handoff.resolve_project_dir(".") == tmp_path.resolve()


def test_empty_env_var_falls_back_to_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", "")
    monkeypatch.chdir(tmp_path)
    assert handoff.resolve_project_dir(".") == tmp_path.resolve()


def test_default_flag_value_is_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert handoff.resolve_project_dir() == tmp_path.resolve()


# ── env-var path works from a non-project cwd (integration) ────────────────


def _make_project(tmp_path: Path) -> Path:
    """A real project dir: codeband.yaml + seeded store + active-room pointer."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "codeband.yaml").write_text(
        "repo:\n  url: https://github.com/acme/widgets\n", encoding="utf-8",
    )
    store = StateStore(project / ".codeband" / "state" / "orchestration.db")
    store.create_task(task_id=ROOM, description="demo", room_id=ROOM)
    (project / ".codeband_room").write_text(ROOM, encoding="utf-8")
    return project


def test_cb_phase_start_resolves_project_via_env_from_foreign_cwd(
    monkeypatch, tmp_path,
):
    """``cb-phase start`` from a random cwd (a worktree, a container, an
    agent scratch dir) must find config/store/pointer via the env var —
    the exact cwd-roulette this batch closes."""
    project = _make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", str(project))

    assert handoff.main(["start", "st-1"]) == 0

    store = StateStore(project / ".codeband" / "state" / "orchestration.db")
    assert store.get_subtask("st-1", ROOM).state == "in_progress"


def test_cb_phase_start_without_env_fails_from_foreign_cwd(
    monkeypatch, tmp_path, capsys,
):
    """Sanity inverse: with no env var the historical cwd behavior remains,
    and from a foreign cwd that is a clean, tagged failure (no traceback)."""
    _make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert handoff.main(["start", "st-1"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("cb-phase:")
    assert "codeband.yaml not found" in err
    assert "Traceback" not in err


def test_explicit_project_dir_flag_beats_env_in_cb_phase(monkeypatch, tmp_path):
    """The flag wins over a (stale/wrong) env var pointing elsewhere."""
    project = _make_project(tmp_path)
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.chdir(tmp_path)

    assert handoff.main(["start", "st-1", "--project-dir", str(project)]) == 0


# ── runner seam: spawned sessions inherit the var ───────────────────────────


def test_runner_exports_project_dir_into_process_env(monkeypatch, tmp_path):
    """The runner's export seam: agent CLI sessions are subprocesses of this
    process, so the exported variable is exactly 'the env of every spawned
    coder/reviewer/mergemaster session'."""
    from codeband.orchestration.runner import _export_project_dir_env

    # setenv registers the teardown that undoes the direct os.environ writes
    # _export_project_dir_env performs below.
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", "stale-value")
    monkeypatch.setenv("CODEBAND_AGENT_SESSION", "stale-value")
    _export_project_dir_env(tmp_path)
    assert os.environ["CODEBAND_PROJECT_DIR"] == str(tmp_path.resolve())
    # The export FORCES the resolved dir — a stale ambient value never wins.
    _export_project_dir_env(tmp_path / "other")
    assert os.environ["CODEBAND_PROJECT_DIR"] == str((tmp_path / "other").resolve())
    # The agent-session marker rides the same seam (cb approve's accident
    # guard, finding 18): every spawned agent session inherits it.
    assert os.environ["CODEBAND_AGENT_SESSION"] == "1"


def test_record_approval_grant_resolves_project_dir_via_env(monkeypatch, tmp_path):
    """``cb approve``'s grant half follows the same contract: a raw default
    '.' flag value resolves through the env var, not the cwd."""
    from codeband.cli import merge

    project = _make_project(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CODEBAND_PROJECT_DIR", str(project))

    # No subtask is bound to PR 99 — the legacy no-grant path returns [].
    # The point is that it RESOLVED the project (config + store + pointer)
    # from the env var; an unresolved project dir would raise instead.
    assert merge.record_approval_grant(".", 99) == []


# ── cb-phase top-level error handling [F7-4] ────────────────────────────────


def test_missing_config_is_tagged_and_traceback_free(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = handoff.main(["start", "st-1", "--project-dir", str(empty)])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("cb-phase: ")
    assert "codeband.yaml not found" in err
    assert "Traceback" not in err


def test_empty_config_reports_missing_repo_field(tmp_path, capsys):
    """[F7-10] A zero-byte codeband.yaml must yield the actionable
    'repo: Field required', tagged and traceback-free."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "codeband.yaml").write_text("", encoding="utf-8")
    code = handoff.main(["start", "st-1", "--project-dir", str(project)])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("cb-phase: fatal — ValidationError")
    assert "repo" in err
    assert "Field required" in err
    assert "Traceback" not in err


def test_malformed_config_is_tagged_fatal(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "codeband.yaml").write_text("repo: 5\n", encoding="utf-8")
    code = handoff.main(["start", "st-1", "--project-dir", str(project)])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("cb-phase: fatal — ValidationError")
    assert "Traceback" not in err


def test_unreadable_yaml_is_tagged_fatal(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "codeband.yaml").write_text("repo: [unclosed\n", encoding="utf-8")
    code = handoff.main(["start", "st-1", "--project-dir", str(project)])
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("cb-phase: fatal — ")
    assert "Traceback" not in err
