"""Tests for small CLI helpers and command-output behavior."""

from __future__ import annotations

from types import SimpleNamespace

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


class TestFeedBannerAndHistory:
    """`cb feed` prints a startup banner (stderr) and exposes --history."""

    @pytest.fixture
    def _patched_feed(self, monkeypatch):
        """Stub out everything `cb feed` touches except banner + LiveFeed wiring.

        Captures the kwargs `LiveFeed` is constructed with so we can assert on
        `show_history`, and no-ops the poll loop so the command returns at once.
        """
        captured: dict[str, object] = {}

        class _FakeLiveFeed:
            def __init__(self, rest, formatter, *, show_history=False):
                captured["show_history"] = show_history

            def run(self):  # called inside _run_async (also stubbed) — return nothing
                return None

        monkeypatch.setenv("BAND_API_KEY", "test-key")
        monkeypatch.setattr(
            "codeband.cli.load_config",
            lambda project: SimpleNamespace(band=SimpleNamespace(rest_url="http://x")),
        )
        monkeypatch.setattr(
            "codeband.config.load_agent_config",
            lambda project: SimpleNamespace(agents={}),
        )
        monkeypatch.setattr(
            "band.client.rest.AsyncRestClient",
            lambda **kwargs: object(),
        )
        monkeypatch.setattr("codeband.monitoring.feed.LiveFeed", _FakeLiveFeed)
        monkeypatch.setattr("codeband.cli._run_async", lambda coro: None)
        return captured

    def test_live_mode_banner_on_stderr_only(self, _patched_feed):
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["feed"])
        assert result.exit_code == 0
        assert "Live feed" in result.stderr
        assert "live only" in result.stderr
        # Banner must not pollute stdout (piped/redirected feed output).
        assert "Live feed" not in result.stdout
        assert _patched_feed["show_history"] is False

    def test_history_flag_enables_replay(self, _patched_feed):
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["feed", "--history"])
        assert result.exit_code == 0
        assert "replaying history" in result.stderr
        assert _patched_feed["show_history"] is True

    def test_history_short_flag(self, _patched_feed):
        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(cli, ["feed", "-H"])
        assert result.exit_code == 0
        assert _patched_feed["show_history"] is True
