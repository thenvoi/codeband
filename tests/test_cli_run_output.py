"""Tests that 'codeband run' produces visible console output."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from codeband.cli import cli


def _make_mock_config(total_agents: int = 8) -> MagicMock:
    """Create a mock CodebandConfig with the right shape."""
    config = MagicMock()
    config.agents.total_agent_count.return_value = total_agents
    return config


class TestRunOutput:
    """Verify 'codeband run' prints status messages to the user."""

    @patch("codeband.cli.load_config")
    @patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
    def test_run_prints_startup_banner(self, mock_run_local, mock_load_config, tmp_path):
        """The run command should print a startup message before launching agents."""
        mock_load_config.return_value = _make_mock_config(8)
        mock_run_local.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--skip-preflight", "--dir", str(tmp_path)])

        assert result.exit_code == 0
        assert "starting" in result.output.lower()
        assert "8 agents" in result.output.lower()

    @patch("codeband.cli.load_config")
    @patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
    def test_run_prints_shutdown_message(self, mock_run_local, mock_load_config, tmp_path):
        """The run command should print a message when agents stop."""
        mock_load_config.return_value = _make_mock_config()
        mock_run_local.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--skip-preflight", "--dir", str(tmp_path)])

        assert result.exit_code == 0
        assert "stopped" in result.output.lower()


class TestRunDebugFlag:
    """Verify --debug flag controls logging verbosity."""

    @patch("codeband.cli.load_config")
    @patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
    def test_default_log_level_is_warning(self, mock_run_local, mock_load_config, tmp_path):
        """Without --debug, root logger level should be WARNING (quiet)."""
        mock_load_config.return_value = _make_mock_config()
        mock_run_local.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--skip-preflight", "--dir", str(tmp_path)])

        assert result.exit_code == 0
        assert logging.getLogger().level == logging.WARNING

    @patch("codeband.cli.load_config")
    @patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
    def test_debug_flag_sets_debug_level(self, mock_run_local, mock_load_config, tmp_path):
        """With --debug, root logger level should be DEBUG."""
        mock_load_config.return_value = _make_mock_config()
        mock_run_local.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--debug", "--skip-preflight", "--dir", str(tmp_path)])

        assert result.exit_code == 0
        assert logging.getLogger().level == logging.DEBUG
