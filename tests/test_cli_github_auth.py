"""Tests for GitHub auth and Claude auth bootstrap in the CLI."""

from __future__ import annotations

import os
from unittest.mock import patch

from click.testing import CliRunner

from codeband.cli import (
    _detect_codex_auth,
    _detect_github_auth,
    _has_codex_subscription_auth,
    _resolve_claude_auth,
    _resolve_codex_auth,
    cli,
)


class TestInitEnvExample:
    """Project init writes the expected environment template."""

    def test_init_writes_gh_token_to_env_example(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(
            cli,
            [
                "init",
                "--repo",
                "https://github.com/example/repo.git",
                "--dir",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0
        env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")
        assert "GH_TOKEN=ghp_..." in env_example


class TestResolveClaudeAuth:
    """OAuth sources take precedence over API key at startup.

    Keychain is a possible OAuth source on macOS developer machines; we mock
    the probe to False by default so tests run deterministically regardless
    of host state.
    """

    def test_oauth_env_preferred_over_api_key(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            _resolve_claude_auth()
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-test"
            assert os.environ["CODEBAND_FALLBACK_ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_api_key_kept_when_no_oauth_anywhere(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            _resolve_claude_auth()
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_oauth_kept_when_no_api_key(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            _resolve_claude_auth()
            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-test"

    def test_noop_when_neither_set(self):
        with patch.dict(os.environ, {}, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            _resolve_claude_auth()  # should not raise

    def test_subscription_oauth_strips_api_key_and_keeps_fallback(self):
        """If the host has stored subscription OAuth (macOS keychain OR Linux
        credentials file), ANTHROPIC_API_KEY must be stripped so the bundled
        Claude CLI falls through to subscription OAuth. Codeband keeps a
        process-local fallback for usage-limit exhaustion.
        """
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=True,
        ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            os.environ.pop("CODEBAND_FALLBACK_ANTHROPIC_API_KEY", None)
            _resolve_claude_auth()
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert os.environ["CODEBAND_FALLBACK_ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_subscription_probe_only_runs_when_api_key_set(self):
        """Don't waste a subprocess call when there's no API key to strip anyway."""
        with patch.dict(os.environ, {}, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth",
        ) as mock_probe:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            _resolve_claude_auth()
            mock_probe.assert_not_called()


class TestResolveCodexAuth:
    """Codex subscription auth wins at startup; API key is fallback only."""

    def test_api_key_kept_when_no_subscription_auth(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        monkeypatch.delenv("CODEBAND_FALLBACK_OPENAI_API_KEY", raising=False)

        _resolve_codex_auth()

        assert os.environ["OPENAI_API_KEY"] == "sk-test"
        assert "CODEBAND_FALLBACK_OPENAI_API_KEY" not in os.environ

    def test_subscription_auth_strips_api_key_and_keeps_fallback(
        self, monkeypatch, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            '{"auth_mode": "chatgpt", "tokens": {}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("CODEBAND_FALLBACK_OPENAI_API_KEY", raising=False)

        _resolve_codex_auth()

        assert "OPENAI_API_KEY" not in os.environ
        assert os.environ["CODEBAND_FALLBACK_OPENAI_API_KEY"] == "sk-test"

    def test_non_chatgpt_auth_file_does_not_strip_api_key(self, monkeypatch, tmp_path):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            '{"OPENAI_API_KEY": "sk-from-file"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("CODEBAND_FALLBACK_OPENAI_API_KEY", raising=False)

        _resolve_codex_auth()

        assert os.environ["OPENAI_API_KEY"] == "sk-test"
        assert "CODEBAND_FALLBACK_OPENAI_API_KEY" not in os.environ

    def test_detects_codex_subscription_auth_lowercase(self, monkeypatch, tmp_path):
        # The real Codex CLI writes lowercase "chatgpt" — this is the load-bearing case.
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            '{"auth_mode": "chatgpt"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert _has_codex_subscription_auth() is True

    def test_detects_codex_subscription_auth_mixed_case(self, monkeypatch, tmp_path):
        # Robustness: accept any capitalisation variation.
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        for variant in ("ChatGPT", "CHATGPT"):
            (codex_home / "auth.json").write_text(
                f'{{"auth_mode": "{variant}"}}',
                encoding="utf-8",
            )
            assert _has_codex_subscription_auth() is True

    def test_apikey_auth_mode_does_not_match(self, monkeypatch, tmp_path):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            '{"auth_mode": "apikey"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert _has_codex_subscription_auth() is False

    def test_missing_auth_json_returns_false(self, monkeypatch, tmp_path):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert _has_codex_subscription_auth() is False

    def test_malformed_json_returns_false(self, monkeypatch, tmp_path):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("not json {{", encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert _has_codex_subscription_auth() is False

    def test_json_array_root_returns_false(self, monkeypatch, tmp_path):
        # JSON is valid but not a dict — should not match.
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text('["chatgpt"]', encoding="utf-8")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert _has_codex_subscription_auth() is False

    def test_integration_resolve_codex_auth_lowercase(self, monkeypatch, tmp_path):
        # Integration: OPENAI_API_KEY must be stripped when auth.json has lowercase "chatgpt".
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            '{"auth_mode": "chatgpt", "tokens": {}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("CODEBAND_FALLBACK_OPENAI_API_KEY", raising=False)

        _resolve_codex_auth()

        assert "OPENAI_API_KEY" not in os.environ
        assert os.environ["CODEBAND_FALLBACK_OPENAI_API_KEY"] == "sk-test"


class TestHasClaudeSubscriptionOAuth:
    """Subscription credential probe covers keychain (macOS) and the
    ``.credentials.json`` file location (Linux/Windows, per Claude Code docs).
    """

    def test_credentials_file_in_default_location(self, tmp_path, monkeypatch):
        from codeband.cli import _has_claude_subscription_oauth

        fake_home = tmp_path
        (fake_home / ".claude").mkdir()
        (fake_home / ".claude" / ".credentials.json").write_text("{}")
        monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with patch("codeband.cli.sys.platform", "linux"):
            assert _has_claude_subscription_oauth() is True

    def test_credentials_file_in_claude_config_dir(self, tmp_path, monkeypatch):
        """CLAUDE_CONFIG_DIR overrides the default ``~/.claude`` location."""
        from codeband.cli import _has_claude_subscription_oauth

        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        (custom_dir / ".credentials.json").write_text("{}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nonexistent_home")
        with patch("codeband.cli.sys.platform", "linux"):
            assert _has_claude_subscription_oauth() is True

    def test_returns_false_when_nothing_present(self, tmp_path, monkeypatch):
        from codeband.cli import _has_claude_subscription_oauth

        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with patch("codeband.cli.sys.platform", "linux"):
            assert _has_claude_subscription_oauth() is False

    def test_macos_keychain_hit_short_circuits(self, monkeypatch):
        """On macOS, a keychain match returns True without checking the file."""
        from codeband.cli import _has_claude_subscription_oauth

        fake_result = type("R", (), {"returncode": 0})()
        with patch("codeband.cli.sys.platform", "darwin"), patch(
            "codeband.cli.subprocess.run", return_value=fake_result,
        ) as mock_run:
            assert _has_claude_subscription_oauth() is True
            mock_run.assert_called_once()


class TestDetectGithubAuth:
    """Docker bootstrap should forward GitHub auth to containers."""

    def test_existing_env_token_wins(self):
        env = {"GH_TOKEN": "already-set"}

        with patch("codeband.cli.subprocess.run") as mock_run:
            _detect_github_auth(env)

        mock_run.assert_not_called()
        assert env["GH_TOKEN"] == "already-set"

    def test_uses_gh_auth_token_when_env_missing(self):
        env: dict[str, str] = {}

        with patch("codeband.cli.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "ghu_test_token\n"

            _detect_github_auth(env)

        assert env["GH_TOKEN"] == "ghu_test_token"

    def test_ignores_missing_gh(self):
        env: dict[str, str] = {}

        with patch("codeband.cli.subprocess.run", side_effect=FileNotFoundError):
            _detect_github_auth(env)

        assert "GH_TOKEN" not in env


class TestDetectCodexAuth:
    """Docker bootstrap should mount host ~/.codex into containers."""

    def test_existing_codex_home_wins(self):
        env = {"CODEX_HOME": "/custom/path"}

        with patch("codeband.cli.Path.home") as mock_home:
            _detect_codex_auth(env)

        mock_home.assert_not_called()
        assert env["CODEX_HOME"] == "/custom/path"

    def test_creates_default_codex_home(self, tmp_path):
        env: dict[str, str] = {}

        with patch("codeband.cli.Path.home", return_value=tmp_path):
            _detect_codex_auth(env)

        assert env["CODEX_HOME"] == str(tmp_path / ".codex")
        assert (tmp_path / ".codex").is_dir()

    def test_idempotent_when_dir_already_exists(self, tmp_path):
        (tmp_path / ".codex").mkdir()
        env: dict[str, str] = {}

        with patch("codeband.cli.Path.home", return_value=tmp_path):
            _detect_codex_auth(env)

        assert env["CODEX_HOME"] == str(tmp_path / ".codex")
