"""Tests for cb run session-agent provisioning wiring.

Covers: mint-on-startup, skip-if-already-set, crash-recovery sweep,
clean-exit delete, and late room enrollment — all via CliRunner so the
full run() CLI path is exercised.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from codeband.cli import cli


def _make_mock_config(total_agents: int = 8) -> MagicMock:
    config = MagicMock()
    config.agents.total_agent_count.return_value = total_agents
    config.band.rest_url = "https://band.example.com"
    config.workspace.mode = "local"
    return config


def _stale_ts() -> str:
    """Timestamp older than the 900-second stale threshold."""
    old = datetime.now(timezone.utc) - timedelta(seconds=1000)
    return old.isoformat()


# ─── Provisioning fires when BAND_API_KEY is set ──────────────────────────────


@patch("codeband.cli.load_config")
@patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
@patch("codeband.cli._provision_coordinator_identity", new_callable=AsyncMock)
def test_run_provisions_session_agent_when_band_key_set(
    mock_provision,
    mock_run_local,
    mock_load_config,
    tmp_path,
):
    """cb run calls _provision_coordinator_identity when BAND_API_KEY is present."""
    mock_load_config.return_value = _make_mock_config()
    mock_run_local.return_value = None
    mock_provision.return_value = ("agent-id-abc", "band-key-xyz")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--skip-preflight", "--dir", str(tmp_path)],
        env={"BAND_API_KEY": "band-key-xyz"},
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    mock_provision.assert_awaited_once()
    # First arg is config, second is project Path, third is the band key
    assert mock_provision.call_args.args[2] == "band-key-xyz"


@patch("codeband.cli.load_config")
@patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
@patch("codeband.cli._provision_coordinator_identity", new_callable=AsyncMock)
def test_run_skips_provisioning_when_key_already_set(
    mock_provision,
    mock_run_local,
    mock_load_config,
    tmp_path,
):
    """cb run does NOT reprovision when CODEBAND_SESSION_AGENT_KEY is already set."""
    mock_load_config.return_value = _make_mock_config()
    mock_run_local.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--skip-preflight", "--dir", str(tmp_path)],
        env={
            "BAND_API_KEY": "band-key-xyz",
            "CODEBAND_SESSION_AGENT_KEY": "already-set-key",
        },
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    mock_provision.assert_not_awaited()


@patch("codeband.cli.load_config")
@patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
@patch("codeband.cli._provision_coordinator_identity", new_callable=AsyncMock)
def test_run_skips_provisioning_when_no_band_key(
    mock_provision,
    mock_run_local,
    mock_load_config,
    tmp_path,
):
    """cb run silently skips provisioning when BAND_API_KEY is absent."""
    mock_load_config.return_value = _make_mock_config()
    mock_run_local.return_value = None

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--skip-preflight", "--dir", str(tmp_path)],
        env={},  # no BAND_API_KEY
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    mock_provision.assert_not_awaited()


# ─── Clean-exit cleanup ───────────────────────────────────────────────────────


@patch("codeband.cli.load_config")
@patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
@patch("codeband.cli._provision_coordinator_identity", new_callable=AsyncMock)
@patch(
    "codeband.orchestration.session_agent.delete_session_agent",
    new_callable=AsyncMock,
)
def test_run_deletes_session_agent_on_clean_exit(
    mock_delete,
    mock_provision,
    mock_run_local,
    mock_load_config,
    tmp_path,
):
    """Session agent is deleted after run_local returns (clean exit)."""
    mock_load_config.return_value = _make_mock_config()
    mock_run_local.return_value = None
    mock_provision.return_value = ("agent-id-clean", "band-key-clean")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--skip-preflight", "--dir", str(tmp_path)],
        env={"BAND_API_KEY": "band-key-clean"},
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    mock_delete.assert_awaited_once()
    assert mock_delete.call_args.args[0] == "agent-id-clean"
    assert mock_delete.call_args.kwargs["band_api_key"] == "band-key-clean"


@patch("codeband.cli.load_config")
@patch("codeband.orchestration.runner.run_local", new_callable=AsyncMock)
@patch("codeband.cli._provision_coordinator_identity", new_callable=AsyncMock)
@patch(
    "codeband.orchestration.session_agent.delete_session_agent",
    new_callable=AsyncMock,
)
def test_run_deletes_session_agent_even_when_run_local_errors(
    mock_delete,
    mock_provision,
    mock_run_local,
    mock_load_config,
    tmp_path,
):
    """Session agent cleanup runs in finally — fires even when run_local raises."""
    mock_load_config.return_value = _make_mock_config()
    mock_run_local.side_effect = RuntimeError("orchestrator crashed")
    mock_provision.return_value = ("agent-id-error", "band-key-err")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "--skip-preflight", "--dir", str(tmp_path)],
        env={"BAND_API_KEY": "band-key-err"},
    )

    assert result.exit_code != 0
    # Cleanup must have fired despite the crash
    mock_delete.assert_awaited_once()
    assert mock_delete.call_args.args[0] == "agent-id-error"


# ─── Crash-recovery sweep at startup ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_provision_sweeps_dead_pid_agent_before_registering(tmp_path):
    """_provision_coordinator_identity sweeps a dead-pid stale agent, then mints fresh.

    This exercises the real crash-recovery path: a previous run crashed, leaving
    a session-agent marker with pid=999999999 (provably dead). The next run's
    provisioning step must sweep it, then register a new agent.
    """
    import json

    from codeband.cli import _provision_coordinator_identity

    # Write a real stale marker: dead pid (999999999) + fresh timestamp.
    # Dead pid is the crash-recovery signal — the process died between heartbeats.
    stale_agent_id = "crash-leftover-agent"
    stale_marker = tmp_path / f"{stale_agent_id}.json"
    stale_marker.write_text(
        json.dumps(
            {
                "agent_id": stale_agent_id,
                "agent_name": f"codeband-session-repo-{stale_agent_id}",
                "pid": 999999999,  # dead — provably not running
                "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                "repo": "testrepo",
            }
        ),
        encoding="utf-8",
    )
    assert stale_marker.is_file()

    config = MagicMock()
    config.band.rest_url = "https://x.com"

    sweep_list_resp = MagicMock()
    stale_agent_obj = MagicMock()
    stale_agent_obj.id = stale_agent_id
    stale_agent_obj.name = f"codeband-session-repo-{stale_agent_id}"
    sweep_list_resp.data = [stale_agent_obj]

    new_agent_obj = MagicMock()
    new_agent_obj.id = "new-session-agent-id"
    new_creds_obj = MagicMock()
    new_creds_obj.api_key = "new-session-key"
    register_resp = MagicMock()
    register_resp.data.agent = new_agent_obj
    register_resp.data.credentials = new_creds_obj

    profile_resp = MagicMock()
    profile_resp.data.id = "operator-id"

    sweep_client = MagicMock()
    sweep_client.human_api_agents.list_my_agents = AsyncMock(return_value=sweep_list_resp)
    sweep_client.human_api_agents.delete_my_agent = AsyncMock()
    sweep_client.human_api_agents.register_my_agent = AsyncMock(return_value=register_resp)
    sweep_client.human_api_profile.get_my_profile = AsyncMock(return_value=profile_resp)

    # Override sessions_dir so markers go to tmp_path, not ~/.codeband/sessions
    with (
        patch("thenvoi_rest.AsyncRestClient", return_value=sweep_client),
        patch(
            "codeband.orchestration.session_agent._sessions_dir",
            return_value=tmp_path,
        ),
        patch("codeband.state.registration.read_room_pointer", return_value=None),
        patch("codeband.state.registration.resolve_state_dir", return_value=tmp_path),
    ):
        agent_id, returned_key = await _provision_coordinator_identity(
            config, tmp_path, "test-band-key"
        )

    # Stale agent was genuinely removed: REST delete called + marker gone
    sweep_client.human_api_agents.delete_my_agent.assert_any_call(
        stale_agent_id,
        force=True,
    )
    assert not stale_marker.is_file(), "Stale marker must be deleted by sweep"

    # Fresh agent was registered
    assert agent_id == "new-session-agent-id"
    assert os.environ.get("CODEBAND_SESSION_AGENT_KEY") == "new-session-key"

    # Cleanup the env side-effect so we don't pollute other tests
    os.environ.pop("CODEBAND_SESSION_AGENT_KEY", None)


# ─── Late room enrollment ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provision_enrolls_session_agent_in_active_room(tmp_path):
    """_provision_coordinator_identity enrolls the minted agent in the active room."""
    from codeband.cli import _provision_coordinator_identity

    config = MagicMock()
    config.band.rest_url = "https://x.com"

    sweep_list_resp = MagicMock()
    sweep_list_resp.data = []

    new_agent_obj = MagicMock()
    new_agent_obj.id = "enrolled-session-agent-id"
    new_creds_obj = MagicMock()
    new_creds_obj.api_key = "enrolled-session-key"
    register_resp = MagicMock()
    register_resp.data.agent = new_agent_obj
    register_resp.data.credentials = new_creds_obj

    profile_resp = MagicMock()
    profile_resp.data.id = "operator-id"

    session_identity_resp = MagicMock()
    session_identity_resp.data.id = "enrolled-session-agent-id"

    shared_client = MagicMock()
    shared_client.human_api_agents.list_my_agents = AsyncMock(return_value=sweep_list_resp)
    shared_client.human_api_agents.register_my_agent = AsyncMock(return_value=register_resp)
    shared_client.human_api_profile.get_my_profile = AsyncMock(return_value=profile_resp)
    shared_client.agent_api_identity.get_agent_me = AsyncMock(return_value=session_identity_resp)
    shared_client.human_api_participants.add_my_chat_participant = AsyncMock()

    with (
        patch("thenvoi_rest.AsyncRestClient", return_value=shared_client),
        patch(
            "codeband.orchestration.session_agent._sessions_dir",
            return_value=tmp_path,
        ),
        patch(
            "codeband.state.registration.read_room_pointer",
            return_value="active-room-uuid",
        ),
        patch("codeband.state.registration.resolve_state_dir", return_value=tmp_path),
    ):
        agent_id, _ = await _provision_coordinator_identity(config, tmp_path, "test-band-key")

    # Session agent was enrolled as a room participant
    shared_client.human_api_participants.add_my_chat_participant.assert_awaited_once()
    enroll_call = shared_client.human_api_participants.add_my_chat_participant.call_args
    assert enroll_call.args[0] == "active-room-uuid"
    assert enroll_call.kwargs["participant"].participant_id == "enrolled-session-agent-id"

    os.environ.pop("CODEBAND_SESSION_AGENT_KEY", None)


@pytest.mark.asyncio
async def test_provision_skips_enrollment_when_no_active_room(tmp_path):
    """No active room → enrollment is skipped silently (non-fatal)."""
    from codeband.cli import _provision_coordinator_identity

    config = MagicMock()
    config.band.rest_url = "https://x.com"

    sweep_list_resp = MagicMock()
    sweep_list_resp.data = []

    new_agent_obj = MagicMock()
    new_agent_obj.id = "no-room-agent-id"
    new_creds_obj = MagicMock()
    new_creds_obj.api_key = "no-room-key"
    register_resp = MagicMock()
    register_resp.data.agent = new_agent_obj
    register_resp.data.credentials = new_creds_obj

    profile_resp = MagicMock()
    profile_resp.data.id = "operator-id"

    shared_client = MagicMock()
    shared_client.human_api_agents.list_my_agents = AsyncMock(return_value=sweep_list_resp)
    shared_client.human_api_agents.register_my_agent = AsyncMock(return_value=register_resp)
    shared_client.human_api_profile.get_my_profile = AsyncMock(return_value=profile_resp)
    shared_client.human_api_participants.add_my_chat_participant = AsyncMock()

    with (
        patch("thenvoi_rest.AsyncRestClient", return_value=shared_client),
        patch(
            "codeband.orchestration.session_agent._sessions_dir",
            return_value=tmp_path,
        ),
        patch(
            "codeband.state.registration.read_room_pointer",
            return_value=None,  # no active room
        ),
        patch("codeband.state.registration.resolve_state_dir", return_value=tmp_path),
    ):
        agent_id, _ = await _provision_coordinator_identity(config, tmp_path, "test-band-key")

    # No enrollment attempted
    shared_client.human_api_participants.add_my_chat_participant.assert_not_awaited()
    assert agent_id == "no-room-agent-id"

    os.environ.pop("CODEBAND_SESSION_AGENT_KEY", None)
