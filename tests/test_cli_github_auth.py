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


def _write_config(project_dir, auth_mode: str | None) -> str:
    """Write a minimal codeband.yaml with the given claude.auth_mode.

    ``auth_mode=None`` omits the ``claude:`` block entirely (legacy file).
    Returns the project dir as a string for passing to _resolve_claude_auth.
    """
    lines = ["repo:\n", "  url: https://github.com/example/repo.git\n"]
    if auth_mode is not None:
        lines += ["claude:\n", f"  auth_mode: {auth_mode}\n"]
    (project_dir / "codeband.yaml").write_text("".join(lines), encoding="utf-8")
    return str(project_dir)


class TestResolveClaudeAuth:
    """Default (api_key) keeps ANTHROPIC_API_KEY; subscription mode opts into OAuth.

    Keychain is a possible OAuth source on macOS developer machines; we mock
    the probe to False by default so tests run deterministically regardless
    of host state.
    """

    def test_api_key_mode_keeps_key_even_with_oauth(self, tmp_path):
        """Default mode never strips the key — even when OAuth is also present."""
        project = _write_config(tmp_path, "api_key")
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=True,
        ):
            os.environ.pop("CODEBAND_FALLBACK_ANTHROPIC_API_KEY", None)
            _resolve_claude_auth(project)
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"
            assert "CODEBAND_FALLBACK_ANTHROPIC_API_KEY" not in os.environ

    def test_missing_config_defaults_to_api_key(self, tmp_path):
        """No codeband.yaml → api_key default → key is preserved."""
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=True,
        ):
            _resolve_claude_auth(str(tmp_path))  # no config file present
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_legacy_config_without_claude_block_defaults_to_api_key(self, tmp_path):
        project = _write_config(tmp_path, None)
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            _resolve_claude_auth(project)
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_subscription_mode_strips_api_key_with_oauth_env(self, tmp_path):
        project = _write_config(tmp_path, "subscription")
        env = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            os.environ.pop("CODEBAND_FALLBACK_ANTHROPIC_API_KEY", None)
            _resolve_claude_auth(project)
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-test"
            assert os.environ["CODEBAND_FALLBACK_ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_subscription_mode_strips_api_key_with_host_oauth(self, tmp_path):
        """Subscription mode + host keychain/credentials → strip + keep fallback."""
        project = _write_config(tmp_path, "subscription")
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=True,
        ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            os.environ.pop("CODEBAND_FALLBACK_ANTHROPIC_API_KEY", None)
            _resolve_claude_auth(project)
            assert "ANTHROPIC_API_KEY" not in os.environ
            assert os.environ["CODEBAND_FALLBACK_ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_subscription_mode_keeps_key_when_no_oauth_anywhere(self, tmp_path):
        """Subscription mode but no OAuth source — nothing to fall through to,
        so leave the key in place (preflight will surface the misconfig)."""
        project = _write_config(tmp_path, "subscription")
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth", return_value=False,
        ):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            _resolve_claude_auth(project)
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_api_key_mode_skips_subscription_probe(self, tmp_path):
        """Default mode never strips, so it must not even probe for host OAuth."""
        project = _write_config(tmp_path, "api_key")
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False), patch(
            "codeband.cli._has_claude_subscription_oauth",
        ) as mock_probe:
            _resolve_claude_auth(project)
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
            '{"auth_mode": "ChatGPT", "tokens": {}}',
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

    def test_detects_codex_subscription_auth(self, monkeypatch, tmp_path):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            '{"auth_mode": "ChatGPT"}',
            encoding="utf-8",
        )
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        assert _has_codex_subscription_auth() is True


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
