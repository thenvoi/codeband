"""Tests for CLI input hardening: clean error paths instead of raw tracebacks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codeband.cli import cli as cb_cli


def _write_config(path: Path) -> None:
    path.write_text(
        "repo:\n  url: https://github.com/example/repo.git\n",
        encoding="utf-8",
    )


def _make_backend_patch():
    """Return a context manager that stubs make_backend so tests reach --since parsing."""
    reader = MagicMock()
    reader.read.return_value = []
    backend = MagicMock()
    backend.make_activity_reader.return_value = reader
    return patch("codeband.shell.fs.make_backend", return_value=backend)


class TestSinceCleanErrors:
    """Bad --since values must produce a one-line Error: message, not a traceback."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        _write_config(tmp_path / "codeband.yaml")
        return tmp_path

    def _invoke_usage(self, project: Path, since: str):
        with _make_backend_patch():
            return CliRunner().invoke(
                cb_cli,
                ["usage", "--since", since, "--dir", str(project)],
                catch_exceptions=False,
            )

    def _invoke_log(self, project: Path, since: str):
        with _make_backend_patch():
            return CliRunner().invoke(
                cb_cli,
                ["log", "--since", since, "--dir", str(project)],
                catch_exceptions=False,
            )

    def test_usage_bad_since_exits_nonzero_no_traceback(self, project: Path) -> None:
        result = self._invoke_usage(project, "notadate")
        combined = result.output or ""
        assert result.exit_code != 0
        assert "Error:" in combined
        assert "Traceback" not in combined

    def test_usage_bad_since_mentions_value(self, project: Path) -> None:
        result = self._invoke_usage(project, "zz99")
        combined = result.output or ""
        assert "zz99" in combined

    def test_log_bad_since_exits_nonzero_no_traceback(self, project: Path) -> None:
        result = self._invoke_log(project, "not-a-duration")
        combined = result.output or ""
        assert result.exit_code != 0
        assert "Error:" in combined
        assert "Traceback" not in combined

    def test_log_bad_since_mentions_value(self, project: Path) -> None:
        result = self._invoke_log(project, "xyz")
        combined = result.output or ""
        assert "xyz" in combined
