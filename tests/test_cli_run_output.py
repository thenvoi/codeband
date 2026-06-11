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

    @patch("codeband.cli.load_config")
    @patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
    def test_run_fresh_flag_reaches_run_local(
        self, mock_run_local, mock_load_config, tmp_path,
    ):
        """--fresh threads through to run_local(fresh=True)."""
        mock_load_config.return_value = _make_mock_config()
        mock_run_local.return_value = None

        runner = CliRunner()
        result = runner.invoke(
            cli, ["run", "--skip-preflight", "--fresh", "--dir", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert mock_run_local.call_args.kwargs.get("fresh") is True

    @patch("codeband.cli.load_config")
    @patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
    def test_run_default_is_not_fresh(
        self, mock_run_local, mock_load_config, tmp_path,
    ):
        mock_load_config.return_value = _make_mock_config()
        mock_run_local.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--skip-preflight", "--dir", str(tmp_path)])

        assert result.exit_code == 0
        assert mock_run_local.call_args.kwargs.get("fresh") is False


class TestPreflightErrorOutput:
    """Verify 'cb run' prints a clean preflight error: just the actionable
    remediation when classified, summary + remediation when unclassified.

    The diagnostic context (SDK exception text, structured error fields) is
    kept inside the exception itself for --debug, but never user-facing on
    a classified failure — that's pure noise.
    """

    @patch("codeband.cli.load_config")
    @patch("codeband.preflight.run_preflight", new_callable=AsyncMock)
    def test_classified_error_shows_only_remediation(
        self, mock_run_preflight, mock_load_config, tmp_path
    ):
        from codeband.preflight import PreflightError

        mock_load_config.return_value = _make_mock_config()
        mock_run_preflight.return_value = PreflightError(
            summary=(
                "Claude auth check failed: Command failed with exit code 1\n"
                "rate_limit_event status=rejected resets_at=1777363800 "
                "type=five_hour\nassistant_message_error=rate_limit"
            ),
            remediation=(
                "Claude Pro/Max usage limit reached. Wait for reset, upgrade "
                "the subscription, or fall back to ANTHROPIC_API_KEY."
            ),
            classified=True,
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--dir", str(tmp_path)])

        assert result.exit_code != 0
        assert "Claude Pro/Max usage limit reached" in result.output
        assert "rate_limit_event" not in result.output
        assert "Command failed with exit code" not in result.output
        assert "assistant_message_error" not in result.output

    @patch("codeband.cli.load_config")
    @patch("codeband.preflight.run_preflight", new_callable=AsyncMock)
    def test_unclassified_error_shows_summary_and_remediation(
        self, mock_run_preflight, mock_load_config, tmp_path
    ):
        from codeband.preflight import PreflightError

        mock_load_config.return_value = _make_mock_config()
        mock_run_preflight.return_value = PreflightError(
            summary="Claude SDK call raised RuntimeError: totally novel failure",
            remediation=(
                "Check Claude CLI auth (ANTHROPIC_API_KEY, "
                "CLAUDE_CODE_OAUTH_TOKEN, or macOS keychain via `claude` "
                "login) and network connectivity."
            ),
            classified=False,
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--dir", str(tmp_path)])

        assert result.exit_code != 0
        assert "totally novel failure" in result.output
        assert "Check Claude CLI auth" in result.output


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
