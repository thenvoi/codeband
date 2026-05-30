"""Tests for small CLI helpers and command-output behavior."""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from codeband.cli import _parse_since, cli
from codeband.monitoring.activity_log import parse_type_filter


class TestParseTypeFilter:
    """Shared comma-separated --type parsing for cb log / cb feed."""

    def test_none_and_empty_yield_no_filter(self):
        assert parse_type_filter(None) is None
        assert parse_type_filter("") is None
        assert parse_type_filter("  ,  ") is None

    def test_single_type(self):
        assert parse_type_filter("NUDGE") == {"NUDGE"}

    def test_comma_separated_with_whitespace(self):
        assert parse_type_filter("NUDGE, ERROR ,LLM_USAGE") == {"NUDGE", "ERROR", "LLM_USAGE"}


class TestParseSince:
    """`--since` parsing for `cb log` / `cb usage`."""

    def test_accepts_relative_spans(self):
        assert _parse_since("1h") is not None
        assert _parse_since("30m") is not None
        assert _parse_since("2d") is not None

    def test_accepts_iso_date(self):
        assert _parse_since("2026-05-01") is not None

    def test_rejects_garbage_with_bad_parameter(self):
        with pytest.raises(click.BadParameter):
            _parse_since("garbage")

    def test_rejects_non_numeric_relative_span(self):
        with pytest.raises(click.BadParameter):
            _parse_since("1x")
        with pytest.raises(click.BadParameter):
            _parse_since("xh")


class TestScaleNextSteps:
    """`cb scale` (cli mode) prints the cli-worded next steps."""

    def test_cb_scale_prints_cli_next_steps(self, tmp_path):
        runner = CliRunner()
        init = runner.invoke(
            cli, ["init", "--repo", "https://github.com/x/y.git", "--dir", str(tmp_path)],
        )
        assert init.exit_code == 0

        result = runner.invoke(
            cli, ["scale", "coders.claude_sdk=2", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "Scaled coders.claude_sdk to 2" in result.output
        assert "Next steps:" in result.output
        assert "cb setup-agents" in result.output
        assert "Restart the interactive shell to pick up changes" in result.output
