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
    check_bundled_claude_cli,
    check_claude_auth,
    check_claude_cli,
    check_codeband_yaml,
    check_codex_auth,
    check_codex_cli,
    check_cross_model_pairing,
    check_gh,
    check_gh_auth,
    check_git,
    check_active_room_membership,
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

class TestBundledClaudeCli:
    """Claude agents use the CLI bundled in claude_agent_sdk, not system `claude`.
    A stale bundled CLI sends request shapes current models reject (400, swallowed
    by the adapter → silent zombie agent)."""

    @staticmethod
    def _ctx() -> Context:
        return Context(project_dir=Path.cwd(), config=None)

    def test_warns_when_bundled_older_than_system(self, monkeypatch, tmp_path):
        fake = tmp_path / "claude"
        fake.write_text("")
        monkeypatch.setattr("codeband.doctor._bundled_claude_cli_path", lambda: fake)
        monkeypatch.setattr("codeband.doctor.shutil.which", lambda _name: "/usr/bin/claude")

        def ver(command):
            return (
                "2.1.81 (Claude Code)"
                if str(fake) in command[0]
                else "2.1.179 (Claude Code)"
            )

        monkeypatch.setattr("codeband.doctor._claude_cli_version", ver)
        result = check_bundled_claude_cli(self._ctx())
        assert result.status == Status.WARN
        assert "claude-agent-sdk" in result.remediation

    def test_ok_when_bundled_current(self, monkeypatch, tmp_path):
        fake = tmp_path / "claude"
        fake.write_text("")
        monkeypatch.setattr("codeband.doctor._bundled_claude_cli_path", lambda: fake)
        monkeypatch.setattr("codeband.doctor.shutil.which", lambda _name: "/usr/bin/claude")
        monkeypatch.setattr(
            "codeband.doctor._claude_cli_version",
            lambda _command: "2.1.179 (Claude Code)",
        )
        result = check_bundled_claude_cli(self._ctx())
        assert result.status == Status.OK

    def test_info_when_no_bundled_cli(self, monkeypatch):
        monkeypatch.setattr("codeband.doctor._bundled_claude_cli_path", lambda: None)
        result = check_bundled_claude_cli(self._ctx())
        assert result.status == Status.INFO

    def test_version_tuple_parsing(self):
        from codeband.doctor import _cli_version_tuple

        assert _cli_version_tuple("2.1.81 (Claude Code)") == (2, 1, 81)
        assert _cli_version_tuple("2.1.179 (Claude Code)") == (2, 1, 179)
        assert _cli_version_tuple("") == ()


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

    @staticmethod
    def _ctx(auth_mode: str | None = None) -> Context:
        """Context with a config at the given auth_mode (None → no config → api_key)."""
        cfg = None
        if auth_mode is not None:
            cfg = CodebandConfig.model_validate({
                "repo": {"url": "https://github.com/x/y"},
                "claude": {"auth_mode": auth_mode},
            })
        return Context(project_dir=Path.cwd(), config=cfg)

    def test_api_key_mode_with_key_ok(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        result = check_claude_auth(self._ctx("api_key"))
        assert result.status == Status.OK
        assert "ANTHROPIC_API_KEY" in result.message

    def test_no_config_defaults_to_api_key(self, monkeypatch):
        """ctx.config is None (no codeband.yaml) → api_key default."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        result = check_claude_auth(self._ctx(None))
        assert result.status == Status.OK

    def test_api_key_mode_no_key_fails(self):
        result = check_claude_auth(self._ctx("api_key"))
        assert result.status == Status.FAIL
        assert "ANTHROPIC_API_KEY" in result.remediation
        assert "subscription" in result.remediation.lower()

    def test_api_key_mode_ignores_host_subscription(self, monkeypatch):
        """In api_key mode a host subscription does not satisfy the requirement —
        the subscription path is never taken implicitly, so this still FAILs."""
        monkeypatch.setattr(
            "codeband.doctor._has_claude_subscription_oauth", lambda: True,
        )
        result = check_claude_auth(self._ctx("api_key"))
        assert result.status == Status.FAIL

    def test_subscription_mode_with_oauth_warns(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        result = check_claude_auth(self._ctx("subscription"))
        assert result.status == Status.WARN
        assert "consumer terms" in result.remediation.lower()
        assert "subscription" in result.message.lower()

    def test_subscription_mode_with_host_oauth_warns(self, monkeypatch):
        monkeypatch.setattr(
            "codeband.doctor._has_claude_subscription_oauth", lambda: True,
        )
        result = check_claude_auth(self._ctx("subscription"))
        assert result.status == Status.WARN

    def test_subscription_mode_no_credential_fails(self):
        """subscription mode but no OAuth token and no host creds → FAIL."""
        result = check_claude_auth(self._ctx("subscription"))
        assert result.status == Status.FAIL
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


class TestCrossModelPairing:
    def test_warns_when_reviewer_capacity_below_coder_capacity(self, tmp_path):
        cfg = CodebandConfig(
            repo=RepoConfig(url="https://github.com/example/repo.git"),
            workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
            band=BandConfig(),
            agents=AgentsConfig(
                coders=FrameworkPool(
                    claude_sdk=PoolEntry(count=2),
                    codex=PoolEntry(count=0),
                ),
                reviewers=ReviewersConfig(
                    claude_sdk=PoolEntry(count=0),
                    codex=PoolEntry(count=1),
                ),
                planners=FrameworkPool(claude_sdk=PoolEntry(count=1)),
                plan_reviewers=PlanReviewersConfig(codex=PoolEntry(count=1)),
            ),
        )

        result = check_cross_model_pairing(Context(project_dir=tmp_path, config=cfg))

        assert result.status == Status.WARN
        assert "2 claude_sdk authors share 1 codex reviewers" in result.message
        assert "reviewer capacity" in result.remediation


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


class TestActiveRoomMembership:
    """Lazy-invite visibility check: which agents are in the active task room."""

    async def test_skips_without_config(self, tmp_path):
        result = await check_active_room_membership(Context(project_dir=tmp_path))
        assert result.status == Status.SKIP

    async def test_skips_without_room_pointer(self, tmp_path):
        cfg = _make_config(tmp_path)
        acfg = AgentConfigFile(
            agents={"conductor": AgentCredentials(agent_id="cond", api_key="k")}
        )
        ctx = Context(project_dir=tmp_path, config=cfg, agent_config=acfg)
        result = await check_active_room_membership(ctx)
        assert result.status == Status.SKIP
        assert ".codeband_room not found" in result.message

    async def test_reports_present_and_pending_agents(self, tmp_path):
        """Conductor present, others pending — the expected fresh-room state."""
        cfg = _make_config(tmp_path)
        acfg = AgentConfigFile(
            agents={
                "conductor": AgentCredentials(agent_id="cond-id", api_key="k1"),
                "coder-claude_sdk-0": AgentCredentials(agent_id="cc0-id", api_key="k2"),
                "reviewer-codex-0": AgentCredentials(agent_id="rx0-id", api_key="k3"),
            }
        )
        (tmp_path / ".codeband_room").write_text(
            "deadbeef-1234-5678-9abc-def012345678", encoding="utf-8",
        )
        ctx = Context(project_dir=tmp_path, config=cfg, agent_config=acfg)

        # Mock the participants response: only the Conductor is in the room.
        fake_participant = type("P", (), {"id": "cond-id"})()
        fake_resp = type("R", (), {"data": [fake_participant]})()

        async def fake_list(chat_id):
            return fake_resp

        def fake_client(**_):
            c = type("C", (), {})()
            c.agent_api_participants = type("A", (), {})()
            c.agent_api_participants.list_agent_chat_participants = fake_list
            return c

        with patch("thenvoi_rest.AsyncRestClient", side_effect=fake_client):
            result = await check_active_room_membership(ctx)

        assert result.status == Status.INFO
        assert "conductor" in result.message
        assert "not yet invited" in result.message
        assert "coder-claude_sdk-0" in result.message
        assert "reviewer-codex-0" in result.message

    async def test_warns_on_room_lookup_failure(self, tmp_path):
        """A deleted-on-Band room or transient REST failure becomes WARN, not FAIL."""
        cfg = _make_config(tmp_path)
        acfg = AgentConfigFile(
            agents={"conductor": AgentCredentials(agent_id="cond", api_key="k")}
        )
        (tmp_path / ".codeband_room").write_text("ghost-room", encoding="utf-8")
        ctx = Context(project_dir=tmp_path, config=cfg, agent_config=acfg)

        async def fake_list(chat_id):
            raise RuntimeError("404 Not Found")

        def fake_client(**_):
            c = type("C", (), {})()
            c.agent_api_participants = type("A", (), {})()
            c.agent_api_participants.list_agent_chat_participants = fake_list
            return c

        with patch("thenvoi_rest.AsyncRestClient", side_effect=fake_client):
            result = await check_active_room_membership(ctx)

        assert result.status == Status.WARN
        assert "ghost-ro" in result.message  # truncated room id appears


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
