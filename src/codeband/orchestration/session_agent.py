"""Per-session Band agent lifecycle: register, heartbeat, sweep."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 300   # 5 min — orchestrator refreshes this often
_STALE_THRESHOLD_SECONDS = 900      # 15 min = 3 missed beats → stale
_SESSION_AGENT_PREFIX = "codeband-session-"


def _sessions_dir() -> Path:
    return Path.home() / ".codeband" / "sessions"


def _marker_path(agent_id: str) -> Path:
    return _sessions_dir() / f"{agent_id}.json"


def repo_slug_from_project(project_dir: Path | None = None) -> str:
    """Extract a short repo slug from git origin, falling back to 'repo'."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
            cwd=str(project_dir) if project_dir else None,
        )
        url = result.stdout.strip()
        m = re.search(r"/([^/]+?)(?:\.git)?$", url)
        return m.group(1) if m else "repo"
    except Exception:
        return "repo"


def write_heartbeat(
    agent_id: str,
    agent_name: str,
    pid: int,
    repo: str,
    *,
    sessions_dir: Path | None = None,
) -> Path:
    """Write or refresh the local liveness marker for ``agent_id``.

    Returns the path written.
    """
    base = sessions_dir if sessions_dir is not None else _sessions_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{agent_id}.json"
    data = {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "pid": pid,
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def read_marker(agent_id: str, *, sessions_dir: Path | None = None) -> dict | None:
    """Return the parsed marker dict for ``agent_id``, or None if absent/corrupt."""
    base = sessions_dir if sessions_dir is not None else _sessions_dir()
    path = base / f"{agent_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if the process is alive (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def is_stale(marker: dict | None) -> bool:
    """True if the marker indicates the session is no longer live.

    Single-machine assumption: no marker means the agent was never started
    or the marker was lost, which we treat as stale. Revisit when cross-machine
    concurrency is added (no-marker would need a network source of truth).
    """
    if marker is None:
        return True
    # Dead pid is an additional stale signal even when the timestamp is fresh
    pid = marker.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return True
    # Primary signal: heartbeat age
    ts = marker.get("last_heartbeat")
    if not ts:
        return True
    try:
        last = datetime.fromisoformat(ts)
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age > _STALE_THRESHOLD_SECONDS
    except ValueError:
        return True


async def register_session_agent(
    owner_id: str,
    repo: str,
    *,
    rest_url: str,
    band_api_key: str,
    sessions_dir: Path | None = None,
) -> tuple[str, str]:
    """Mint a ``codeband-session-*`` agent on Band.ai and write the initial marker.

    Returns ``(agent_id, api_key)``. Atomic: if the marker write fails the
    just-created agent is deleted before raising so no orphan is left on the
    platform.
    """
    import secrets

    from thenvoi_rest import AsyncRestClient
    from thenvoi_rest.types import AgentRegisterRequest

    hex_suffix = secrets.token_hex(4)
    name = f"{_SESSION_AGENT_PREFIX}{repo}-{hex_suffix}"
    description = f"Session agent for codeband operator (owner: {owner_id})"

    client = AsyncRestClient(api_key=band_api_key, base_url=rest_url)
    response = await client.human_api_agents.register_my_agent(
        agent=AgentRegisterRequest(name=name, description=description)
    )
    agent = response.data.agent
    credentials = response.data.credentials
    agent_id = agent.id
    api_key = credentials.api_key
    logger.warning("Registered session agent %s (%s)", name, agent_id)

    try:
        write_heartbeat(
            agent_id, name, pid=os.getpid(), repo=repo,
            sessions_dir=sessions_dir,
        )
    except Exception as exc:
        try:
            await client.human_api_agents.delete_my_agent(agent_id, force=True)
            logger.info("Rolled back session agent %s after marker write failure", agent_id)
        except Exception:
            logger.warning("Rollback delete failed for orphaned agent %s", agent_id)
        raise RuntimeError(
            f"Failed to write session marker (agent rolled back): {exc}"
        ) from exc

    return agent_id, api_key


async def sweep_stale_session_agents(
    *,
    band_api_key: str,
    rest_url: str,
    current_agent_id: str | None = None,
    sessions_dir: Path | None = None,
) -> list[str]:
    """Delete stale ``codeband-session-*`` agents owned by this operator.

    Stale = no local marker, heartbeat older than ``_STALE_THRESHOLD_SECONDS``,
    or marker's pid is not alive. Single-machine assumption: no marker = stale.
    Agents with a fresh marker are never deleted. The current session's own
    agent (``current_agent_id``) is always skipped.
    """
    from thenvoi_rest import AsyncRestClient

    client = AsyncRestClient(api_key=band_api_key, base_url=rest_url)
    response = await client.human_api_agents.list_my_agents()
    agents = response.data or []

    deleted: list[str] = []
    for a in agents:
        if not a.name.startswith(_SESSION_AGENT_PREFIX):
            continue
        if current_agent_id and a.id == current_agent_id:
            logger.debug("Skipping current session agent %s", a.id)
            continue
        marker = read_marker(a.id, sessions_dir=sessions_dir)
        if not is_stale(marker):
            logger.debug("Agent %s has fresh marker — skipping sweep", a.id)
            continue
        try:
            await client.human_api_agents.delete_my_agent(a.id, force=True)
            base = sessions_dir if sessions_dir is not None else _sessions_dir()
            mpath = base / f"{a.id}.json"
            if mpath.is_file():
                mpath.unlink()
            deleted.append(a.id)
            logger.info("Swept stale session agent %s (%s)", a.name, a.id)
        except Exception as exc:
            logger.warning("Failed to delete session agent %s: %s", a.id, exc)

    return deleted


async def delete_session_agent(
    agent_id: str,
    *,
    band_api_key: str,
    rest_url: str,
    sessions_dir: Path | None = None,
) -> None:
    """Delete a specific session agent and its local marker (clean-exit cleanup).

    Separate from sweep: sweep is a startup garbage-collection pass over ALL
    stale agents; this is the targeted delete for the agent that was minted by
    THIS run and is being cleaned up at its normal exit.
    """
    from thenvoi_rest import AsyncRestClient

    client = AsyncRestClient(api_key=band_api_key, base_url=rest_url)
    await client.human_api_agents.delete_my_agent(agent_id, force=True)
    base = sessions_dir if sessions_dir is not None else _sessions_dir()
    mpath = base / f"{agent_id}.json"
    if mpath.is_file():
        mpath.unlink()
    logger.info("Deleted session agent %s (clean exit)", agent_id)


async def enroll_session_agent_in_room(
    *,
    session_agent_key: str,
    band_api_key: str,
    room_id: str,
    rest_url: str,
) -> None:
    """Add the session agent as a participant in an existing room.

    Called at ``cb run`` startup for late enrollment — the room was created
    before the session agent was minted, so send_task's enrollment path did not
    run. Covers both the /codeband path (jam creates the room, cb register-task
    registers it, then cb run starts) and the cb-task / cb-run two-step.
    """
    from thenvoi_rest import AsyncRestClient, ParticipantRequest

    session_client = AsyncRestClient(api_key=session_agent_key, base_url=rest_url)
    identity = await session_client.agent_api_identity.get_agent_me()
    session_agent_id = identity.data.id

    human_client = AsyncRestClient(api_key=band_api_key, base_url=rest_url)
    await human_client.human_api_participants.add_my_chat_participant(
        room_id,
        participant=ParticipantRequest(participant_id=session_agent_id),
    )
    logger.info(
        "Enrolled session agent %s in room %s (late enrollment)",
        session_agent_id, room_id,
    )


async def start_heartbeat_loop(
    agent_id: str,
    agent_name: str,
    repo: str,
    *,
    sessions_dir: Path | None = None,
) -> None:
    """Refresh the session marker every ``_HEARTBEAT_INTERVAL_SECONDS``.

    Runs indefinitely; cancelled by the orchestrator lifecycle on shutdown.
    """
    pid = os.getpid()
    while True:
        try:
            write_heartbeat(
                agent_id, agent_name, pid=pid, repo=repo,
                sessions_dir=sessions_dir,
            )
        except Exception:
            logger.exception("Heartbeat write failed for session agent %s", agent_id)
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
