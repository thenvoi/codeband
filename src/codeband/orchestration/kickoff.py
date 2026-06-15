"""Kickoff — create a task room, add participants, and send initial task."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from codeband.config import CodebandConfig, load_agent_config

logger = logging.getLogger(__name__)


def _require_api_key() -> str:
    """Return BAND_API_KEY from the environment, or raise ValueError."""
    api_key = os.environ.get("BAND_API_KEY")
    if not api_key:
        raise ValueError(
            "BAND_API_KEY environment variable is required. "
            "Get one from https://platform.band.ai"
        )
    return api_key


async def send_task(config: CodebandConfig, project_dir: Path, description: str) -> None:
    """Create a single task room with all agents and send the task.

    The human running this command is the room creator and task sender.
    Requires BAND_API_KEY environment variable (the human's API key).
    """
    from thenvoi_rest import AsyncRestClient, ChatMessageRequest, ParticipantRequest
    from thenvoi_rest.human_api_chats import CreateMyChatRoomRequestChat
    from thenvoi_rest.types import ChatMessageRequestMentionsItem as Mention

    api_key = _require_api_key()
    human_client = AsyncRestClient(api_key=api_key, base_url=config.band.rest_url)

    agent_config = load_agent_config(project_dir)

    # Only the Conductor's identity is needed at kickoff: they are the sole
    # initial participant and the @mention target of the task message. Every
    # other agent is invited lazily by an inviting agent later, by display
    # name, so the human-side code does not need their IDs.
    conductor_creds = agent_config.get("conductor")
    conductor_client = AsyncRestClient(
        api_key=conductor_creds.api_key, base_url=config.band.rest_url,
    )
    conductor_identity = await conductor_client.agent_api_identity.get_agent_me()
    conductor_id = conductor_identity.data.id
    conductor_name = conductor_identity.data.name
    logger.info("Resolved conductor: %s (%s)", conductor_name, conductor_id)

    # Clean up the previous task room (if any)
    await _cleanup_rooms(agent_config, config, project_dir)

    # Human creates the task room
    room = await human_client.human_api_chats.create_my_chat_room(
        chat=CreateMyChatRoomRequestChat()
    )
    room_id = room.data.id
    logger.info("Created task room: %s", room_id)

    # The initiator is whoever holds BAND_API_KEY (the human_client above).
    # Resolve their Band participant id so the watchdog can @mention them when
    # a subtask of this task lands blocked. Owner resolution is REQUIRED: a
    # failure aborts kickoff loudly *before any message is posted* — an
    # ownerless task can never be escalated to a human, so it must not start.
    try:
        profile = await human_client.human_api_profile.get_my_profile()
        owner_id = getattr(profile.data, "id", None)
    except Exception as exc:
        raise RuntimeError(
            "Could not resolve the task initiator's Band profile (needed as "
            "the task owner). Check BAND_API_KEY and Band.ai connectivity, "
            "then retry."
        ) from exc
    if not owner_id or not isinstance(owner_id, str):
        raise RuntimeError(
            "Band profile lookup returned no participant id — cannot register "
            "an ownerless task. Check BAND_API_KEY, then retry."
        )
    raw_handle = getattr(profile.data, "name", None)
    owner_handle = raw_handle if isinstance(raw_handle, str) else None

    # Register the task — tasks row + .codeband_room pointer, atomically and
    # row-first — BEFORE the task message below. The pointer must exist before
    # any agent is activated, so an early cb-phase call cannot race it. Any
    # registration failure aborts kickoff loudly; nothing is swallowed.
    from codeband.state import StateStore
    from codeband.state.registration import register_task

    workspace_path = Path(config.workspace.path)
    if not workspace_path.is_absolute():
        workspace_path = project_dir / workspace_path
    store = StateStore(workspace_path / "state" / "orchestration.db")
    registration = register_task(
        room_id=room_id,
        description=description,
        owner_id=owner_id,
        agents=config.agents,
        owner_handle=owner_handle,
        project_dir=project_dir,
        store=store,
    )
    if registration.superseded_task_id:
        logger.info(
            "Superseded previous task %s", registration.superseded_task_id,
        )

    # Human adds only the Conductor — the human's first message @mentions the
    # Conductor, so the Conductor must be a participant for that message to
    # land. Every other agent is invited lazily by the inviting agent
    # (Conductor invites Planner; Planner invites Plan Reviewer; Coder invites
    # Reviewer; …) via thenvoi_add_participant once the workflow needs them.
    await human_client.human_api_participants.add_my_chat_participant(
        room_id,
        participant=ParticipantRequest(participant_id=conductor_id),
    )

    context_msg = (
        f"{description}\n\n"
        f"@{conductor_name} — here's a new task for the team. "
        f"Please send it to the Planner for analysis.\n\n"
        f"Repository: {config.repo.url} (branch: {config.repo.branch})"
    )

    # Human sends the task message @mentioning the conductor
    mentions = [Mention(id=conductor_id, name=conductor_name)]
    await human_client.human_api_messages.send_my_chat_message(
        room_id,
        message=ChatMessageRequest(content=context_msg, mentions=mentions),
    )
    logger.info("Task sent to room %s", room_id)

    # No pointer write here: register_task above already persisted
    # .codeband_room (row-first, before the task message).

    # Print summary
    print(f"\nTask room: {room_id}")
    print(f"Initial participant: {conductor_name} (other agents invited on demand)\n")


async def send_room_message(
    config: CodebandConfig,
    project_dir: Path,
    message: str,
    *,
    command_style: str = "cli",
) -> None:
    """Send a message to the existing Codeband task room (for approve/reject).

    Reads the room ID from the active-room pointer (canonical
    ``{workspace}/state/.codeband_room``, legacy project-dir fallback — see
    ``state/registration.py``) and sends the message @mentioning the
    Conductor. Does NOT create a new room.
    """
    from thenvoi_rest import AsyncRestClient, ChatMessageRequest
    from thenvoi_rest.types import ChatMessageRequestMentionsItem as Mention

    from codeband.state.registration import read_room_pointer, resolve_state_dir

    task_room_id = read_room_pointer(
        project_dir, resolve_state_dir(config, project_dir),
    )
    if not task_room_id:
        task_cmd = "/task" if command_style == "slash" else "cb task"
        issue_cmd = "/issue" if command_style == "slash" else "cb issue"
        raise RuntimeError(
            "No active Codeband task room found (state/.codeband_room missing). "
            f"Start a task first with '{task_cmd}' or '{issue_cmd}'."
        )

    agent_config = load_agent_config(project_dir)

    # Resolve conductor name (need one API call for the display name)
    conductor_creds = agent_config.get("conductor")
    conductor_id = conductor_creds.agent_id
    conductor_client = AsyncRestClient(
        api_key=conductor_creds.api_key, base_url=config.band.rest_url,
    )
    conductor_identity = await conductor_client.agent_api_identity.get_agent_me()
    conductor_name = conductor_identity.data.name

    content = f"@{conductor_name} — {message}"
    mentions = [Mention(id=conductor_id, name=conductor_name)]

    session_key = os.environ.get("CODEBAND_SESSION_AGENT_KEY") or None
    if session_key:
        session_client = AsyncRestClient(api_key=session_key, base_url=config.band.rest_url)
        await session_client.agent_api_messages.create_agent_chat_message(
            task_room_id,
            message=ChatMessageRequest(content=content, mentions=mentions),
        )
    else:
        api_key = _require_api_key()
        human_client = AsyncRestClient(api_key=api_key, base_url=config.band.rest_url)
        await human_client.human_api_messages.send_my_chat_message(
            task_room_id,
            message=ChatMessageRequest(content=content, mentions=mentions),
        )
    logger.info("Message sent to room %s", task_room_id)


# Regex for the structured envelope first line.
# protocol <type> cid <id> [pr <N>] [round <N>] state <state> [risk <level>]
#   from <agent> to <agent> [<summary>]
_ENVELOPE_RE = re.compile(
    r"protocol\s+(\w+)"           # 1: protocol type
    r"\s+cid\s+(\S+)"             # 2: correlation id
    r"(?:\s+pr\s+(\d+))?"         # 3: optional PR number
    r"(?:\s+round\s+(\d+))?"      # 4: optional round
    r"\s+state\s+(\w+)"           # 5: state
    r"(?:\s+risk\s+(\w+))?"       # 6: optional risk level
    r"\s+from\s+(\S+)"            # 7: from agent
    r"\s+to\s+(\S+)"              # 8: to agent
    r"(?:\s+(.*))?"               # 9: optional summary text
)

# States that indicate the step completed successfully.
_DONE_STATES = frozenset({
    "ready", "approved", "resolved", "merged", "findings_posted", "completed",
})


def _parse_envelope(content: str) -> dict:
    """Parse a protocol state envelope content string into a dict.

    Returns an empty dict if the content doesn't match the expected format.
    """
    m = _ENVELOPE_RE.match(content)
    if not m:
        return {}
    result: dict = {
        "protocol": m.group(1),
        "cid": m.group(2),
        "state": m.group(5),
        "from": m.group(7),
        "to": m.group(8),
    }
    if m.group(3):
        result["pr"] = int(m.group(3))
    if m.group(4):
        result["round"] = int(m.group(4))
    if m.group(6):
        result["risk"] = m.group(6)
    if m.group(9):
        result["summary"] = m.group(9)
    return result


def _truncate_task_name(summary: str, max_len: int = 60) -> str:
    """Extract a short task name from a plan summary.

    Strips leading dashes/bullets, takes up to the first ". " sentence boundary,
    and caps at *max_len* chars.
    """
    clean = re.sub(r"^[\u2014\-\u2013\u2022\s]+", "", summary)  # strip leading —/-/bullet
    # Split on ". " (period + space) to preserve filenames like "shields.io"
    first_sentence = clean.split(". ")[0].strip()
    if len(first_sentence) <= max_len:
        return first_sentence
    return first_sentence[: max_len - 1].rstrip() + "\u2026"


def _format_task_status(protocols) -> str:
    """Format protocol memory objects into a task-level pipeline view.

    Returns a multi-line string ready to print, or "" if there's nothing to show.
    """
    if not protocols:
        return ""

    # Parse all envelopes, skip unparseable ones.
    envelopes = []
    for mem in protocols:
        content = getattr(mem, "content", "") or ""
        env = _parse_envelope(content)
        if env:
            envelopes.append(env)

    if not envelopes:
        return ""

    # --- Task name from the plan envelope's summary ---
    task_name = "Current task"
    for env in envelopes:
        if env["protocol"] == "plan" and env.get("summary"):
            task_name = _truncate_task_name(env["summary"])
            break

    lines: list[str] = [f'  "{task_name}"']

    # --- Task-level pipeline: plan → plan_review ---
    task_stages: list[str] = []
    for proto in ("plan", "plan_review"):
        label = proto.replace("_", " ")
        matches = [e for e in envelopes if e["protocol"] == proto]
        if not matches:
            continue
        state = matches[-1]["state"]
        if state in _DONE_STATES:
            task_stages.append(f"{label} \u2713")
        else:
            task_stages.append(f"{label} \u2717 {state}")

    # --- PR-level lines, grouped by PR number ---
    pr_envelopes: dict[int, list[dict]] = {}
    for env in envelopes:
        pr_num = env.get("pr")
        if pr_num is not None:
            pr_envelopes.setdefault(pr_num, []).append(env)

    for pr_num in sorted(pr_envelopes):
        pr_envs = pr_envelopes[pr_num]
        # Start with task-level stages, then add PR-specific stages.
        stages = list(task_stages)
        # Infer "coded" — if a code_review envelope exists, coding happened.
        stages.append("coded \u2713")
        for env in pr_envs:
            label = env["protocol"].replace("code_", "").replace("_", " ")
            state = env["state"]
            if state in _DONE_STATES:
                seg = f"{label} \u2713"
            else:
                seg = f"{label} \u2717 {state}"
            rnd = env.get("round")
            if rnd and rnd > 1:
                seg += f" round {rnd}"
            risk = env.get("risk")
            if risk:
                seg += f" ({risk} risk)"
            stages.append(seg)
        lines.append(f"    PR #{pr_num}  " + " \u2192 ".join(stages))

    # If no PRs yet, show task-level stages on their own line.
    if not pr_envelopes and task_stages:
        lines.append("    " + " \u2192 ".join(task_stages))

    return "\n".join(lines)


async def query_status(
    config: CodebandConfig,
    project_dir: Path,
    *,
    command_style: str = "cli",
) -> None:
    """Query task status from whichever memory backend is active.

    Runs the same Band.ai probe as `run_local` (cached module-wide) and reads
    from the resolved backend. On free tier this means the local JSONL store;
    on paid tier it hits the Band.ai REST API.
    """
    from thenvoi_rest import AsyncRestClient

    from codeband.memory import probe_memory_backend

    agent_config = load_agent_config(project_dir)
    conductor_creds = agent_config.get("conductor")

    rest_client = AsyncRestClient(
        api_key=conductor_creds.api_key,
        base_url=config.band.rest_url,
    )

    override = config.band.memory_mode if config.band.memory_mode != "auto" else None
    mode = await probe_memory_backend(rest_client, config_override=override)

    if mode == "local":
        knowledge_data, protocols_data = await _query_local_memories(config, project_dir)
    else:
        knowledge_resp = await rest_client.agent_api_memories.list_agent_memories(
            system="long_term", type="procedural", segment="tool", scope="organization",
        )
        protocols_resp = await rest_client.agent_api_memories.list_agent_memories(
            system="working", type="episodic", segment="agent", scope="organization",
        )
        knowledge_data = knowledge_resp.data or []
        protocols_data = protocols_resp.data or []

    if not (knowledge_data or protocols_data):
        print("No active tasks or knowledge found.")
        return

    print("\n" + "=" * 56)
    print("  CODEBAND STATUS")
    if mode == "local":
        print("  (local JSONL — Band.ai memory unavailable)")
    print("=" * 56)
    if protocols_data:
        print()
        print(_format_task_status(protocols_data))
    if knowledge_data:
        print("\n  Repo knowledge:")
        for m in knowledge_data:
            thought = getattr(m, "thought", "") or ""
            print(f"    {thought[:70] or m.content[:70]}")
    print()
    log_cmd = "/log" if command_style == "slash" else "cb log"
    print(f"  Tip: Use '{log_cmd}' for full activity history.")
    print("=" * 56 + "\n")


async def _query_local_memories(config: CodebandConfig, project_dir: Path):
    """Read repo-knowledge and protocol-state lists from the local JSONL store."""
    from codeband.memory import LocalMemoryStore

    workspace_path = Path(config.workspace.path)
    if not workspace_path.is_absolute():
        workspace_path = project_dir / workspace_path
    store = LocalMemoryStore(workspace_path / "state" / "memories.jsonl")

    knowledge = await store.list(
        system="long_term", type="procedural", segment="tool", scope="organization",
    )
    protocols = await store.list(
        system="working", type="episodic", segment="agent", scope="organization",
    )
    return knowledge.data, protocols.data


async def _remove_agents_from_room(
    room_id: str, agent_config, config,
) -> None:
    """Remove every agent in *agent_config* from *room_id* (best-effort).

    Uses each agent's own credentials to leave the room. A 404 means the room
    already does not exist on Band.ai — treated as success. Other errors are
    logged at debug level and otherwise swallowed so a single agent's failure
    doesn't block the rest.
    """
    from thenvoi_rest import AsyncRestClient

    for key, creds in agent_config.agents.items():
        try:
            client = AsyncRestClient(api_key=creds.api_key, base_url=config.band.rest_url)
            await client.agent_api_participants.remove_agent_chat_participant(
                room_id, creds.agent_id,
            )
        except Exception as e:
            logger.debug("Could not leave room %s for %s: %s", room_id, key, e)


async def _cleanup_rooms(
    agent_config,
    config,
    project_dir: Path,
) -> None:
    """Remove agents from the previous task room only.

    Reads the active-room pointer (both locations — the canonical
    ``{workspace}/state/.codeband_room`` and the legacy project-dir file) to
    find the specific room to clean up. Other rooms (including concurrent
    Codeband tasks) are left untouched. Each agent leaves on its own
    credentials, so a 404 (already-not-a-member, common under lazy invites
    where most agents were never added) is harmless.
    """
    from codeband.state.registration import read_room_pointer, resolve_state_dir

    # warn_legacy=False: send_task re-registers immediately after this,
    # which migrates a legacy pointer anyway — no need to nag here.
    prev_room_id = read_room_pointer(
        project_dir, resolve_state_dir(config, project_dir), warn_legacy=False,
    )

    if not prev_room_id:
        logger.debug("No previous task room found, skipping cleanup")
        return

    logger.info("Cleaning up previous task room: %s", prev_room_id)
    await _remove_agents_from_room(prev_room_id, agent_config, config)


async def reset_active_room(config: CodebandConfig, project_dir: Path) -> str | None:
    """Remove all agents from the active task room and delete the pointer file.

    Returns the room id that was cleaned up, or None if there was nothing to
    reset. Safe to call repeatedly — missing file or dead-on-Band room both
    reduce to a no-op. Clears BOTH pointer locations (canonical
    ``{workspace}/state/.codeband_room`` and the legacy project-dir file) so
    a reset can never leave a stale legacy pointer behind to resurrect a
    dead room.
    """
    from codeband.state.registration import (
        legacy_pointer_path,
        read_room_pointer,
        resolve_state_dir,
        state_pointer_path,
    )

    state_dir = resolve_state_dir(config, project_dir)
    pointer_files = (state_pointer_path(state_dir), legacy_pointer_path(project_dir))

    room_id = read_room_pointer(project_dir, state_dir, warn_legacy=False)
    if not room_id:
        for f in pointer_files:
            f.unlink(missing_ok=True)
        return None

    agent_config = load_agent_config(project_dir)
    await _remove_agents_from_room(room_id, agent_config, config)
    for f in pointer_files:
        f.unlink(missing_ok=True)
    return room_id
