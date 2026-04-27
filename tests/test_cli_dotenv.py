"""Tests for ``--dir``-driven .env loading at the cli group level.

Regression for: ``cb --dir /some/project`` from a different CWD must
load that project's ``.env``, not the CWD's. The cli group's body now
calls ``_load_project_dotenv`` which prefers ``<project>/.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

from codeband.cli import _load_project_dotenv


def test_loads_env_from_project_dir(tmp_path: Path, monkeypatch):
    project = tmp_path / "p1"
    project.mkdir()
    (project / ".env").write_text("CODEBAND_TEST_KEY=from_project\n")

    # CWD is a different directory with no .env.
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    monkeypatch.delenv("CODEBAND_TEST_KEY", raising=False)
    _load_project_dotenv(str(project))
    assert os.environ.get("CODEBAND_TEST_KEY") == "from_project"


def test_falls_back_to_cwd_search_when_no_project_env(tmp_path: Path, monkeypatch):
    project = tmp_path / "p2"
    project.mkdir()
    # No .env in project; CWD has one.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("CODEBAND_TEST_KEY2=from_cwd\n")
    monkeypatch.chdir(cwd)

    monkeypatch.delenv("CODEBAND_TEST_KEY2", raising=False)
    _load_project_dotenv(str(project))
    assert os.environ.get("CODEBAND_TEST_KEY2") == "from_cwd"


def test_does_not_override_existing_env_var(tmp_path: Path, monkeypatch):
    """load_dotenv default is non-overriding — preserve that."""
    project = tmp_path / "p3"
    project.mkdir()
    (project / ".env").write_text("CODEBAND_TEST_KEY3=from_project\n")
    monkeypatch.setenv("CODEBAND_TEST_KEY3", "from_shell")
    monkeypatch.chdir(tmp_path)

    _load_project_dotenv(str(project))
    assert os.environ.get("CODEBAND_TEST_KEY3") == "from_shell"


def test_project_aware_decorator_runs_init_before_callback(tmp_path: Path, monkeypatch):
    """Subcommand --dir must drive .env loading even when CWD is elsewhere.

    Regression: the cli group only sees its own --dir, so a subcommand
    invocation like ``cb task ... --dir /project`` from a different CWD
    must still load /project/.env. The @_project_aware decorator on
    each subcommand is responsible for that.
    """
    from codeband.cli import _project_aware

    project = tmp_path / "p4"
    project.mkdir()
    (project / ".env").write_text("CODEBAND_TEST_KEY4=from_subcmd_dir\n")
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.delenv("CODEBAND_TEST_KEY4", raising=False)

    captured = {}

    @_project_aware
    def fake_command(*, project_dir: str) -> None:
        captured["env_at_call"] = os.environ.get("CODEBAND_TEST_KEY4")
        captured["project_dir"] = project_dir

    fake_command(project_dir=str(project))

    assert captured["env_at_call"] == "from_subcmd_dir"
    assert captured["project_dir"] == str(project)


def test_clirunner_subcommand_dir_loads_env(tmp_path: Path, monkeypatch):
    """End-to-end CliRunner regression: ``cb <subcmd> --dir /project``
    must load /project/.env even when CWD is elsewhere. Catches the
    bug class where the @_project_aware decorator silently fails to
    apply (e.g., wrong order, missing on a new subcommand).
    """
    from click.testing import CliRunner

    from codeband.cli import cli

    project = tmp_path / "p5"
    project.mkdir()
    (project / ".env").write_text("CODEBAND_E2E_KEY=from_clirunner_test\n")
    # Minimal codeband.yaml so subcommands that load_config don't bail.
    (project / "codeband.yaml").write_text(
        "repo:\n"
        "  url: https://example.com/r.git\n"
        "  branch: main\n"
    )

    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("CODEBAND_E2E_KEY", raising=False)

    # `cb log` is cheap (read-only) and project-aware. It will load
    # `--dir`'s .env via @_project_aware before any other side effect.
    runner = CliRunner(mix_stderr=False)
    runner.invoke(cli, ["log", "--dir", str(project)])

    assert os.environ.get("CODEBAND_E2E_KEY") == "from_clirunner_test"
