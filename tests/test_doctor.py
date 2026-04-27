"""Tests for `cb doctor` checks and runner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codeband.config import (
    AgentConfigFile,
    AgentCredentials,
    AgentsConfig,
    BandConfig,
    CodebandConfig,
    FrameworkPool,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
    ReviewersConfig,
    WorkspaceConfig,
)
from codeband.doctor import (
    Context,
    Status,
    check_agent_config_yaml,
    check_band_api_key,
    check_claude_auth,
    check_claude_cli,
    check_codeband_yaml,
    check_codex_auth,
    check_codex_cli,
    check_gh,
    check_gh_auth,
    check_git,
    check_memory_mode,
    check_python_version,
    check_workspace_writable,
    run_all,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "BAND_API_KEY",
        "BAND_MEMORY_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    # Clear memory probe cache between tests.
    from codeband.memory import reset_memory_mode

    reset_memory_mode()
    yield
    reset_memory_mode()


def _make_config(tmp_path: Path, *, use_codex: bool = False) -> CodebandConfig:
    """Build a test config. `use_codex=True` puts a Codex coder in the pool."""
    coders = FrameworkPool(
        claude_sdk=PoolEntry(count=0 if use_codex else 1),
        codex=PoolEntry(count=1 if use_codex else 0),
    )
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        band=BandConfig(),
        agents=AgentsConfig(
            coders=coders,
            reviewers=ReviewersConfig(claude_sdk=PoolEntry(count=1)),
            plan_reviewers=PlanReviewersConfig(claude_sdk=PoolEntry(count=1)),
            planners=FrameworkPool(claude_sdk=PoolEntry(count=1)),
        ),
    )


# ─── individual checks ───────────────────────────────────────────────────────

class TestClaudeAuth:
    @pytest.fixture(autouse=True)
    def _isolate_subscription_probe(self, monkeypatch):
        """Default to no host subscription creds so tests run deterministically
        on dev machines where the macOS Keychain may contain a credential.
        """
        monkeypatch.setattr(
            "codeband.doctor._has_claude_subscription_oauth",
            lambda: False,
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    def test_no_auth_fails(self):
        result = check_claude_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.FAIL
        assert "CLAUDE_CODE_OAUTH_TOKEN" in result.remediation

    def test_api_key_ok(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        result = check_claude_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.OK
        assert "ANTHROPIC_API_KEY" in result.message

    def test_oauth_ok(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        result = check_claude_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.OK
        assert "CLAUDE_CODE_OAUTH_TOKEN" in result.message

    def test_both_env_vars_set_is_info(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        result = check_claude_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.INFO

    def test_api_key_plus_host_subscription_warns(self, monkeypatch):
        """API key set + subscription creds available on host (keychain or
        .credentials.json) → WARN. Codeband will auto-prefer subscription at
        run-time, but the user should make it explicit in .env.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setattr(
            "codeband.doctor._has_claude_subscription_oauth", lambda: True,
        )
        result = check_claude_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.WARN
        assert "subscription" in result.message.lower()
        assert "ANTHROPIC_API_KEY" in result.remediation

    def test_host_subscription_only_is_ok(self, monkeypatch):
        """No env vars, but host has keychain/file creds — that's a valid setup."""
        monkeypatch.setattr(
            "codeband.doctor._has_claude_subscription_oauth", lambda: True,
        )
        result = check_claude_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.OK
        assert "subscription" in result.message.lower()


class TestBandApiKey:
    def test_missing_warns(self):
        result = check_band_api_key(Context(project_dir=Path.cwd()))
        assert result.status == Status.WARN
        assert "cb task" in result.message

    def test_set_ok(self, monkeypatch):
        monkeypatch.setenv("BAND_API_KEY", "band_u_x")
        result = check_band_api_key(Context(project_dir=Path.cwd()))
        assert result.status == Status.OK


class TestCodexAuth:
    def test_no_auth_fails(self, tmp_path, monkeypatch):
        # Force HOME to a dir without ~/.codex so the check actually hits the fail path.
        monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
        result = check_codex_auth(Context(project_dir=tmp_path))
        assert result.status == Status.FAIL

    def test_openai_key_ok(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
        result = check_codex_auth(Context(project_dir=tmp_path))
        assert result.status == Status.OK

    def test_codex_login_with_auth_json_ok(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "auth.json").write_text('{"tokens": {}}')
        monkeypatch.setenv("HOME", str(home))
        result = check_codex_auth(Context(project_dir=tmp_path))
        assert result.status == Status.OK

    def test_empty_codex_dir_does_not_pass(self, monkeypatch, tmp_path):
        """`cb up` creates ~/.codex as a bind-mount target, so an empty
        directory is not evidence of a completed `codex login`. The check
        must fail unless auth.json actually exists."""
        home = tmp_path / "home"
        (home / ".codex").mkdir(parents=True)  # directory only, no auth.json
        monkeypatch.setenv("HOME", str(home))
        result = check_codex_auth(Context(project_dir=tmp_path))
        assert result.status == Status.FAIL


class TestCodebandYaml:
    def test_missing_fails(self, tmp_path):
        result = check_codeband_yaml(Context(project_dir=tmp_path))
        assert result.status == Status.FAIL
        assert "cb init" in result.remediation

    def test_parse_error_fails(self, tmp_path):
        (tmp_path / "codeband.yaml").write_text("not: valid: yaml: [")
        ctx = Context(project_dir=tmp_path, config_error="bogus")
        result = check_codeband_yaml(ctx)
        assert result.status == Status.FAIL
        assert "parse" in result.message.lower()

    def test_ok(self, tmp_path):
        cfg = _make_config(tmp_path)
        (tmp_path / "codeband.yaml").write_text("stub")  # file must exist
        ctx = Context(project_dir=tmp_path, config=cfg)
        result = check_codeband_yaml(ctx)
        assert result.status == Status.OK


class TestAgentConfig:
    def test_missing_warns(self, tmp_path):
        cfg = _make_config(tmp_path)
        ctx = Context(project_dir=tmp_path, config=cfg)
        result = check_agent_config_yaml(ctx)
        assert result.status == Status.WARN
        assert "setup-agents" in result.remediation

    def test_missing_keys_fails(self, tmp_path):
        cfg = _make_config(tmp_path)
        # Missing several expected keys.
        acfg = AgentConfigFile(agents={
            "conductor": AgentCredentials(agent_id="c", api_key="k"),
        })
        acfg.to_yaml(tmp_path / "agent_config.yaml")
        ctx = Context(project_dir=tmp_path, config=cfg, agent_config=acfg)
        result = check_agent_config_yaml(ctx)
        assert result.status == Status.FAIL
        assert "planner-claude_sdk-0" in result.message

    def test_all_present_ok(self, tmp_path):
        cfg = _make_config(tmp_path)
        acfg = AgentConfigFile(agents={
            key: AgentCredentials(agent_id=key, api_key="k")
            for key in (
                "conductor", "mergemaster",
                "planner-claude_sdk-0", "plan_reviewer-claude_sdk-0",
                "coder-claude_sdk-0", "reviewer-claude_sdk-0",
            )
        })
        acfg.to_yaml(tmp_path / "agent_config.yaml")
        ctx = Context(project_dir=tmp_path, config=cfg, agent_config=acfg)
        result = check_agent_config_yaml(ctx)
        assert result.status == Status.OK


class TestWorkspace:
    def test_writable_ok(self, tmp_path):
        cfg = _make_config(tmp_path)
        ctx = Context(project_dir=tmp_path, config=cfg)
        result = check_workspace_writable(ctx)
        assert result.status == Status.OK

    def test_skip_without_config(self, tmp_path):
        ctx = Context(project_dir=tmp_path)
        result = check_workspace_writable(ctx)
        assert result.status == Status.SKIP


class TestToolChecks:
    def test_git_missing_fails(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        result = check_git(Context(project_dir=Path.cwd()))
        assert result.status == Status.FAIL

    def test_gh_missing_fails(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert check_gh(Context(project_dir=Path.cwd())).status == Status.FAIL

    def test_gh_auth_skips_if_no_gh(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert check_gh_auth(Context(project_dir=Path.cwd())).status == Status.SKIP

    def test_gh_auth_fails_on_non_zero_exit(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")

        class _R:
            returncode = 1
            stdout = ""
            stderr = "not logged in"

        monkeypatch.setattr(
            "subprocess.run", lambda *a, **kw: _R(),
        )
        result = check_gh_auth(Context(project_dir=Path.cwd()))
        assert result.status == Status.FAIL
        assert "gh auth login" in result.remediation

    def test_claude_cli_missing_fails(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        result = check_claude_cli(Context(project_dir=Path.cwd()))
        assert result.status == Status.FAIL
        assert "@anthropic-ai/claude-code" in result.remediation

    def test_claude_cli_ok(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude")

        class _V:
            returncode = 0
            stdout = "1.2.3\n"
            stderr = ""

        monkeypatch.setattr("subprocess.run", lambda *a, **kw: _V())
        result = check_claude_cli(Context(project_dir=Path.cwd()))
        assert result.status == Status.OK
        assert "1.2.3" in result.message

    def test_codex_cli_missing_fails(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        result = check_codex_cli(Context(project_dir=Path.cwd()))
        assert result.status == Status.FAIL
        assert "@openai/codex" in result.remediation


class TestPython:
    def test_current_version_ok(self):
        result = check_python_version(Context(project_dir=Path.cwd()))
        assert result.status == Status.OK


class TestMemoryMode:
    async def test_skips_without_config(self, tmp_path):
        result = await check_memory_mode(Context(project_dir=tmp_path))
        assert result.status == Status.SKIP

    async def test_skips_without_conductor_creds(self, tmp_path):
        cfg = _make_config(tmp_path)
        acfg = AgentConfigFile(agents={})
        ctx = Context(project_dir=tmp_path, config=cfg, agent_config=acfg)
        result = await check_memory_mode(ctx)
        assert result.status == Status.SKIP


# ─── run_all / exit code ────────────────────────────────────────────────────

class TestRunAll:
    async def test_no_config_produces_fails_and_exit_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
        # Neutralize host subscription creds so the test doesn't depend on
        # whether the dev machine has `claude` logged in.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.setattr(
            "codeband.doctor._has_claude_subscription_oauth", lambda: False,
        )
        ctx, exit_code = await run_all(tmp_path)
        assert exit_code == 1
        # codeband.yaml FAIL plus Claude auth FAIL.
        assert ctx.results["codeband.yaml"].status == Status.FAIL
        assert ctx.results["Claude auth"].status == Status.FAIL

    async def test_happy_path_exit_0(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setenv("BAND_API_KEY", "band_u_x")
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

        class _GH:
            returncode = 0
            stdout = "logged in"
            stderr = ""

        class _Git:
            returncode = 0
            stdout = "git version 2.50.0"
            stderr = ""

        def _run(cmd, *a, **kw):
            return _Git() if cmd[0] == "git" else _GH()

        monkeypatch.setattr("subprocess.run", _run)

        cfg = _make_config(tmp_path)
        cfg.to_yaml(tmp_path / "codeband.yaml")
        acfg = AgentConfigFile(agents={
            key: AgentCredentials(agent_id=key, api_key="k")
            for key in (
                "conductor", "mergemaster",
                "planner-claude_sdk-0", "plan_reviewer-claude_sdk-0",
                "coder-claude_sdk-0", "reviewer-claude_sdk-0",
            )
        })
        acfg.to_yaml(tmp_path / "agent_config.yaml")

        fake_identity = type("I", (), {"data": type("D", (), {"name": "Test"})()})()
        async def fake_get_me():
            return fake_identity

        async def fake_list(*a, **kw):
            return object()

        def fake_client(**_):
            c = type("C", (), {})()
            c.agent_api_identity = type("A", (), {})()
            c.agent_api_identity.get_agent_me = fake_get_me
            c.agent_api_memories = type("M", (), {})()
            c.agent_api_memories.list_agent_memories = fake_list
            return c

        with patch("thenvoi_rest.AsyncRestClient", side_effect=fake_client):
            ctx, exit_code = await run_all(tmp_path)
        assert exit_code == 0, {n: (r.status.value, r.message) for n, r in ctx.results.items()}
        assert ctx.results["Band.ai REST reachable"].status == Status.OK
        assert ctx.results["Memory backend"].status == Status.OK

    async def test_codex_check_skipped_when_no_codex_agents(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
        cfg = _make_config(tmp_path, use_codex=False)
        cfg.to_yaml(tmp_path / "codeband.yaml")
        ctx, _ = await run_all(tmp_path)
        assert ctx.results["Codex auth"].status == Status.SKIP

    async def test_codex_check_applies_when_player_is_codex(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
        monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
        cfg = _make_config(tmp_path, use_codex=True)
        cfg.to_yaml(tmp_path / "codeband.yaml")
        ctx, _ = await run_all(tmp_path)
        assert ctx.results["Codex auth"].status == Status.FAIL
