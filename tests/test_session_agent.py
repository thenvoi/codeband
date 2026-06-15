"""Tests for orchestration/session_agent.py and the kickoff enrollment path."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codeband.orchestration.session_agent import (
    _HEARTBEAT_INTERVAL_SECONDS,
    _STALE_THRESHOLD_SECONDS,
    is_stale,
    read_marker,
    register_session_agent,
    start_heartbeat_loop,
    sweep_stale_session_agents,
    write_heartbeat,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _fresh_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_ts() -> str:
    """Timestamp older than _STALE_THRESHOLD_SECONDS."""
    old = datetime.now(timezone.utc) - timedelta(seconds=_STALE_THRESHOLD_SECONDS + 60)
    return old.isoformat()


def _make_marker(
    tmp_path: Path,
    agent_id: str,
    *,
    ts: str | None = None,
    pid: int | None = None,
) -> Path:
    marker = {
        "agent_id": agent_id,
        "agent_name": f"codeband-session-repo-{agent_id[:4]}",
        "pid": pid if pid is not None else os.getpid(),
        "last_heartbeat": ts if ts is not None else _fresh_ts(),
        "repo": "testrepo",
    }
    path = tmp_path / f"{agent_id}.json"
    path.write_text(json.dumps(marker), encoding="utf-8")
    return path


# ─── Threshold constants ───────────────────────────────────────────────────────


def test_stale_threshold_is_900s():
    assert _STALE_THRESHOLD_SECONDS == 900


def test_heartbeat_interval_is_300s():
    assert _HEARTBEAT_INTERVAL_SECONDS == 300


# ─── is_stale ─────────────────────────────────────────────────────────────────


def test_is_stale_no_marker():
    assert is_stale(None) is True


def test_is_stale_old_timestamp(tmp_path):
    marker = {
        "agent_id": "abc",
        "pid": os.getpid(),
        "last_heartbeat": _stale_ts(),
    }
    assert is_stale(marker) is True


def test_is_stale_fresh_marker_live_pid(tmp_path):
    marker = {
        "agent_id": "abc",
        "pid": os.getpid(),
        "last_heartbeat": _fresh_ts(),
    }
    assert is_stale(marker) is False


def test_is_stale_dead_pid():
    # Use a pid that is very unlikely to be alive: 0 is invalid, use a probe
    # on a safely non-existent pid (os.kill raises ProcessLookupError).
    marker = {
        "agent_id": "abc",
        "pid": 999999999,
        "last_heartbeat": _fresh_ts(),
    }
    # 999999999 is above Linux's pid_max (4194304) and definitely not alive.
    assert is_stale(marker) is True


def test_is_stale_missing_ts():
    assert is_stale({"agent_id": "x", "pid": os.getpid()}) is True


def test_is_stale_malformed_ts():
    marker = {"agent_id": "x", "pid": os.getpid(), "last_heartbeat": "not-a-date"}
    assert is_stale(marker) is True


# ─── write_heartbeat / read_marker ────────────────────────────────────────────


def test_write_and_read_heartbeat(tmp_path):
    agent_id = "agent-aabbccdd"
    path = write_heartbeat(
        agent_id, "codeband-session-repo-aabb", pid=os.getpid(), repo="repo",
        sessions_dir=tmp_path,
    )
    assert path.is_file()
    data = read_marker(agent_id, sessions_dir=tmp_path)
    assert data is not None
    assert data["agent_id"] == agent_id
    assert data["repo"] == "repo"
    assert data["pid"] == os.getpid()
    ts = datetime.fromisoformat(data["last_heartbeat"])
    assert (datetime.now(timezone.utc) - ts).total_seconds() < 5


def test_write_heartbeat_updates_timestamp(tmp_path):
    agent_id = "agent-update"
    write_heartbeat(agent_id, "n", pid=1, repo="r", sessions_dir=tmp_path)
    # Force an old timestamp into the file
    path = tmp_path / f"{agent_id}.json"
    old = json.loads(path.read_text())
    old["last_heartbeat"] = _stale_ts()
    path.write_text(json.dumps(old))

    write_heartbeat(agent_id, "n", pid=os.getpid(), repo="r", sessions_dir=tmp_path)
    data = read_marker(agent_id, sessions_dir=tmp_path)
    ts = datetime.fromisoformat(data["last_heartbeat"])
    assert (datetime.now(timezone.utc) - ts).total_seconds() < 5


def test_read_marker_missing(tmp_path):
    assert read_marker("nonexistent", sessions_dir=tmp_path) is None


def test_read_marker_corrupt(tmp_path):
    (tmp_path / "badagent.json").write_text("not json")
    assert read_marker("badagent", sessions_dir=tmp_path) is None


# ─── register_session_agent ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_writes_marker_and_returns_creds(tmp_path):
    mock_client = MagicMock()
    agent_obj = MagicMock()
    agent_obj.id = "agent-id-123"
    creds_obj = MagicMock()
    creds_obj.api_key = "sk-test-key"
    response = MagicMock()
    response.data.agent = agent_obj
    response.data.credentials = creds_obj
    mock_client.human_api_agents.register_my_agent = AsyncMock(return_value=response)

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        agent_id, api_key = await register_session_agent(
            "owner-42",
            "testrepo",
            rest_url="https://example.com",
            band_api_key="band-key",
            sessions_dir=tmp_path,
        )

    assert agent_id == "agent-id-123"
    assert api_key == "sk-test-key"
    marker = read_marker("agent-id-123", sessions_dir=tmp_path)
    assert marker is not None
    assert marker["agent_id"] == "agent-id-123"
    assert marker["repo"] == "testrepo"


@pytest.mark.asyncio
async def test_register_rollback_on_marker_failure(tmp_path):
    """If marker write fails, the just-created agent is deleted before raising."""
    mock_client = MagicMock()
    agent_obj = MagicMock()
    agent_obj.id = "agent-id-rollback"
    creds_obj = MagicMock()
    creds_obj.api_key = "sk-rollback"
    response = MagicMock()
    response.data.agent = agent_obj
    response.data.credentials = creds_obj
    mock_client.human_api_agents.register_my_agent = AsyncMock(return_value=response)
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    # Make sessions_dir a FILE so mkdir fails → write_heartbeat raises
    fake_sessions = tmp_path / "sessions"
    fake_sessions.write_text("not a dir")

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        with pytest.raises(RuntimeError, match="agent rolled back"):
            await register_session_agent(
                "owner-42",
                "testrepo",
                rest_url="https://example.com",
                band_api_key="band-key",
                sessions_dir=fake_sessions,
            )

    mock_client.human_api_agents.delete_my_agent.assert_awaited_once_with(
        "agent-id-rollback", force=True,
    )


# ─── sweep_stale_session_agents ───────────────────────────────────────────────


def _make_agent(agent_id: str, name: str) -> MagicMock:
    a = MagicMock()
    a.id = agent_id
    a.name = name
    return a


@pytest.mark.asyncio
async def test_sweep_deletes_stale_old_timestamp(tmp_path):
    agent_id = "stale-old"
    _make_marker(tmp_path, agent_id, ts=_stale_ts())

    mock_client = MagicMock()
    agents = [_make_agent(agent_id, f"codeband-session-repo-{agent_id}")]
    mock_client.human_api_agents.list_my_agents = AsyncMock(
        return_value=MagicMock(data=agents)
    )
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        deleted = await sweep_stale_session_agents(
            band_api_key="k",
            rest_url="https://x.com",
            sessions_dir=tmp_path,
        )

    assert agent_id in deleted
    mock_client.human_api_agents.delete_my_agent.assert_awaited_once_with(
        agent_id, force=True,
    )


@pytest.mark.asyncio
async def test_sweep_deletes_agent_with_no_marker(tmp_path):
    agent_id = "no-marker-agent"

    mock_client = MagicMock()
    agents = [_make_agent(agent_id, f"codeband-session-repo-{agent_id}")]
    mock_client.human_api_agents.list_my_agents = AsyncMock(
        return_value=MagicMock(data=agents)
    )
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        deleted = await sweep_stale_session_agents(
            band_api_key="k",
            rest_url="https://x.com",
            sessions_dir=tmp_path,
        )

    assert agent_id in deleted


@pytest.mark.asyncio
async def test_sweep_deletes_dead_pid(tmp_path):
    agent_id = "dead-pid-agent"
    _make_marker(tmp_path, agent_id, pid=999999999)  # definitely not alive

    mock_client = MagicMock()
    agents = [_make_agent(agent_id, f"codeband-session-repo-{agent_id}")]
    mock_client.human_api_agents.list_my_agents = AsyncMock(
        return_value=MagicMock(data=agents)
    )
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        deleted = await sweep_stale_session_agents(
            band_api_key="k",
            rest_url="https://x.com",
            sessions_dir=tmp_path,
        )

    assert agent_id in deleted


@pytest.mark.asyncio
async def test_sweep_spares_fresh_marker(tmp_path):
    agent_id = "fresh-agent"
    _make_marker(tmp_path, agent_id, ts=_fresh_ts(), pid=os.getpid())

    mock_client = MagicMock()
    agents = [_make_agent(agent_id, f"codeband-session-repo-{agent_id}")]
    mock_client.human_api_agents.list_my_agents = AsyncMock(
        return_value=MagicMock(data=agents)
    )
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        deleted = await sweep_stale_session_agents(
            band_api_key="k",
            rest_url="https://x.com",
            sessions_dir=tmp_path,
        )

    assert agent_id not in deleted
    mock_client.human_api_agents.delete_my_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_skips_current_session(tmp_path):
    agent_id = "current-session"
    _make_marker(tmp_path, agent_id, ts=_stale_ts())  # stale BUT is current

    mock_client = MagicMock()
    agents = [_make_agent(agent_id, f"codeband-session-repo-{agent_id}")]
    mock_client.human_api_agents.list_my_agents = AsyncMock(
        return_value=MagicMock(data=agents)
    )
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        deleted = await sweep_stale_session_agents(
            band_api_key="k",
            rest_url="https://x.com",
            current_agent_id=agent_id,
            sessions_dir=tmp_path,
        )

    assert agent_id not in deleted
    mock_client.human_api_agents.delete_my_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_ignores_non_session_agents(tmp_path):
    """Non-codeband-session-* agents are never touched."""
    mock_client = MagicMock()
    agents = [
        _make_agent("fleet-agent", "conductor"),
        _make_agent("other-agent", "codeband-coder-claude-0"),
    ]
    mock_client.human_api_agents.list_my_agents = AsyncMock(
        return_value=MagicMock(data=agents)
    )
    mock_client.human_api_agents.delete_my_agent = AsyncMock()

    with patch("thenvoi_rest.AsyncRestClient", return_value=mock_client):
        deleted = await sweep_stale_session_agents(
            band_api_key="k",
            rest_url="https://x.com",
            sessions_dir=tmp_path,
        )

    assert deleted == []
    mock_client.human_api_agents.delete_my_agent.assert_not_awaited()


# ─── start_heartbeat_loop ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_loop_updates_marker(tmp_path):
    """The loop writes the marker immediately on the first tick."""
    # Patch _HEARTBEAT_INTERVAL_SECONDS to 0 so the loop fires without sleeping
    with patch("codeband.orchestration.session_agent._HEARTBEAT_INTERVAL_SECONDS", 0):
        task = asyncio.create_task(
            start_heartbeat_loop(
                "loop-agent", "codeband-session-repo-test", "repo",
                sessions_dir=tmp_path,
            )
        )
        # Let the loop run one iteration
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    data = read_marker("loop-agent", sessions_dir=tmp_path)
    assert data is not None
    assert data["agent_id"] == "loop-agent"


# ─── kickoff enrollment path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_task_enrolls_session_agent(tmp_path, monkeypatch):
    """Room creation WITH CODEBAND_SESSION_AGENT_KEY → session agent added as participant."""
    from codeband.orchestration import kickoff

    monkeypatch.setenv("BAND_API_KEY", "human-key")
    monkeypatch.setenv("CODEBAND_SESSION_AGENT_KEY", "session-key")

    # Build a minimal config mock
    config = MagicMock()
    config.band.rest_url = "https://band.example.com"
    config.repo.url = "https://github.com/org/repo"
    config.repo.branch = "main"
    config.workspace.path = str(tmp_path / "workspace")

    # Mock all REST client interactions
    conductor_creds = MagicMock()
    conductor_creds.api_key = "conductor-key"
    conductor_creds.agent_id = "conductor-id"

    agent_config_mock = MagicMock()
    agent_config_mock.get.return_value = conductor_creds

    conductor_identity_resp = MagicMock()
    conductor_identity_resp.data.id = "conductor-id"
    conductor_identity_resp.data.name = "Conductor"

    session_identity_resp = MagicMock()
    session_identity_resp.data.id = "session-agent-id"

    room_resp = MagicMock()
    room_resp.data.id = "room-uuid"

    profile_resp = MagicMock()
    profile_resp.data.id = "human-owner-id"
    profile_resp.data.name = "human"

    human_client = MagicMock()
    human_client.human_api_chats.create_my_chat_room = AsyncMock(return_value=room_resp)
    human_client.human_api_profile.get_my_profile = AsyncMock(return_value=profile_resp)
    human_client.human_api_participants.add_my_chat_participant = AsyncMock()
    human_client.human_api_messages.send_my_chat_message = AsyncMock()

    conductor_client = MagicMock()
    conductor_client.agent_api_identity.get_agent_me = AsyncMock(
        return_value=conductor_identity_resp
    )

    session_client = MagicMock()
    session_client.agent_api_identity.get_agent_me = AsyncMock(
        return_value=session_identity_resp
    )

    def _make_client(api_key, base_url):
        if api_key == "human-key":
            return human_client
        if api_key == "conductor-key":
            return conductor_client
        if api_key == "session-key":
            return session_client
        return MagicMock()

    store_mock = MagicMock()
    registration_mock = MagicMock()
    registration_mock.superseded_task_id = None
    store_mock.__enter__ = MagicMock(return_value=store_mock)
    store_mock.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(kickoff, "load_agent_config", return_value=agent_config_mock),
        patch("thenvoi_rest.AsyncRestClient", side_effect=_make_client),
        patch("codeband.state.StateStore", return_value=store_mock),
        patch("codeband.state.registration.register_task", return_value=registration_mock),
        patch("codeband.orchestration.kickoff._cleanup_rooms", new=AsyncMock()),
    ):
        await kickoff.send_task(config, tmp_path, "Do the thing")

    # Should have been called twice: once for conductor, once for session agent
    assert human_client.human_api_participants.add_my_chat_participant.await_count == 2
    calls = human_client.human_api_participants.add_my_chat_participant.call_args_list
    participant_ids = [call.kwargs["participant"].participant_id for call in calls]
    assert "conductor-id" in participant_ids
    assert "session-agent-id" in participant_ids


@pytest.mark.asyncio
async def test_send_task_no_session_key_no_extra_participant(tmp_path, monkeypatch):
    """Room creation WITHOUT CODEBAND_SESSION_AGENT_KEY → only conductor is added."""
    from codeband.orchestration import kickoff

    monkeypatch.setenv("BAND_API_KEY", "human-key")
    monkeypatch.delenv("CODEBAND_SESSION_AGENT_KEY", raising=False)

    config = MagicMock()
    config.band.rest_url = "https://band.example.com"
    config.repo.url = "https://github.com/org/repo"
    config.repo.branch = "main"
    config.workspace.path = str(tmp_path / "workspace")

    conductor_creds = MagicMock()
    conductor_creds.api_key = "conductor-key"
    conductor_identity_resp = MagicMock()
    conductor_identity_resp.data.id = "conductor-id"
    conductor_identity_resp.data.name = "Conductor"

    room_resp = MagicMock()
    room_resp.data.id = "room-uuid"

    profile_resp = MagicMock()
    profile_resp.data.id = "human-id"
    profile_resp.data.name = "human"

    human_client = MagicMock()
    human_client.human_api_chats.create_my_chat_room = AsyncMock(return_value=room_resp)
    human_client.human_api_profile.get_my_profile = AsyncMock(return_value=profile_resp)
    human_client.human_api_participants.add_my_chat_participant = AsyncMock()
    human_client.human_api_messages.send_my_chat_message = AsyncMock()

    conductor_client = MagicMock()
    conductor_client.agent_api_identity.get_agent_me = AsyncMock(
        return_value=conductor_identity_resp
    )

    agent_config_mock = MagicMock()
    agent_config_mock.get.return_value = conductor_creds

    def _make_client(api_key, base_url):
        if api_key == "human-key":
            return human_client
        return conductor_client

    store_mock = MagicMock()
    registration_mock = MagicMock()
    registration_mock.superseded_task_id = None

    with (
        patch.object(kickoff, "load_agent_config", return_value=agent_config_mock),
        patch("thenvoi_rest.AsyncRestClient", side_effect=_make_client),
        patch("codeband.state.StateStore", return_value=store_mock),
        patch("codeband.state.registration.register_task", return_value=registration_mock),
        patch("codeband.orchestration.kickoff._cleanup_rooms", new=AsyncMock()),
    ):
        await kickoff.send_task(config, tmp_path, "Do the thing")

    # Only conductor, no session agent
    assert human_client.human_api_participants.add_my_chat_participant.await_count == 1
    call = human_client.human_api_participants.add_my_chat_participant.call_args
    assert call.kwargs["participant"].participant_id == "conductor-id"


@pytest.mark.asyncio
async def test_send_task_enrollment_failure_raises(tmp_path, monkeypatch):
    """Enrollment failure → raises loud, does not silently continue."""
    from codeband.orchestration import kickoff

    monkeypatch.setenv("BAND_API_KEY", "human-key")
    monkeypatch.setenv("CODEBAND_SESSION_AGENT_KEY", "session-key")

    config = MagicMock()
    config.band.rest_url = "https://band.example.com"
    config.repo.url = "https://github.com/org/repo"
    config.repo.branch = "main"
    config.workspace.path = str(tmp_path / "workspace")

    conductor_creds = MagicMock()
    conductor_creds.api_key = "conductor-key"
    conductor_identity_resp = MagicMock()
    conductor_identity_resp.data.id = "conductor-id"
    conductor_identity_resp.data.name = "Conductor"

    session_identity_resp = MagicMock()
    session_identity_resp.data.id = "session-agent-id"

    room_resp = MagicMock()
    room_resp.data.id = "room-uuid"

    profile_resp = MagicMock()
    profile_resp.data.id = "human-id"
    profile_resp.data.name = "human"

    human_client = MagicMock()
    human_client.human_api_chats.create_my_chat_room = AsyncMock(return_value=room_resp)
    human_client.human_api_profile.get_my_profile = AsyncMock(return_value=profile_resp)
    # First call (conductor) succeeds, second call (session agent) fails
    human_client.human_api_participants.add_my_chat_participant = AsyncMock(
        side_effect=[None, RuntimeError("participant add failed")]
    )
    human_client.human_api_messages.send_my_chat_message = AsyncMock()

    conductor_client = MagicMock()
    conductor_client.agent_api_identity.get_agent_me = AsyncMock(
        return_value=conductor_identity_resp
    )

    session_client = MagicMock()
    session_client.agent_api_identity.get_agent_me = AsyncMock(
        return_value=session_identity_resp
    )

    agent_config_mock = MagicMock()
    agent_config_mock.get.return_value = conductor_creds

    def _make_client(api_key, base_url):
        if api_key == "human-key":
            return human_client
        if api_key == "conductor-key":
            return conductor_client
        if api_key == "session-key":
            return session_client
        return MagicMock()

    store_mock = MagicMock()
    registration_mock = MagicMock()
    registration_mock.superseded_task_id = None

    with (
        patch.object(kickoff, "load_agent_config", return_value=agent_config_mock),
        patch("thenvoi_rest.AsyncRestClient", side_effect=_make_client),
        patch("codeband.state.StateStore", return_value=store_mock),
        patch("codeband.state.registration.register_task", return_value=registration_mock),
        patch("codeband.orchestration.kickoff._cleanup_rooms", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="enroll session agent"):
            await kickoff.send_task(config, tmp_path, "Do the thing")
