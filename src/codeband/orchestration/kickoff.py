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

    # Resolve agent identities (need agent API keys for this)
    identities = {}  # key -> (agent_id, agent_name)
    for key, creds in agent_config.agents.items():
        client = AsyncRestClient(api_key=creds.api_key, base_url=config.band.rest_url)
        identity = await client.agent_api_identity.get_agent_me()
        identities[key] = (identity.data.id, identity.data.name)
        logger.info("Resolved %s: %s (%s)", key, identity.data.name, identity.data.id)

    conductor_id, conductor_name = identities["conductor"]

    # Clean up the previous task room (if any)
    await _cleanup_rooms(human_client, identities, agent_config, config, project_dir)

    # Human creates the task room
    room = await human_client.human_api_chats.create_my_chat_room(
        chat=CreateMyChatRoomRequestChat()
    )
    room_id = room.data.id
    logger.info("Created task room: %s", room_id)

    # Human adds all agents to the room
    for key, (aid, aname) in identities.items():
        await human_client.human_api_participants.add_my_chat_participant(
            room_id,
            participant=ParticipantRequest(participant_id=aid),
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

    # Persist room ID so approve/reject/cleanup can target this specific room
    room_file = project_dir / ".codeband_room"
    room_file.write_text(room_id, encoding="utf-8")

    # Print summary
    print(f"\nTask room: {room_id}")
    print(f"Agents: {', '.join(identities.keys())}\n")


async def send_room_message(
    config: CodebandConfig,
    project_dir: Path,
    message: str,
    *,
    command_style: str = "cli",
) -> None:
    """Send a message to the existing Codeband task room (for approve/reject).

    Reads the room ID from .codeband_room (written by send_task) and sends
    the message @mentioning the Conductor. Does NOT create a new room.
    """
    from thenvoi_rest import AsyncRestClient, ChatMessageRequest
    from thenvoi_rest.types import ChatMessageRequestMentionsItem as Mention

    room_file = project_dir / ".codeband_room"
    try:
        task_room_id = room_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        task_cmd = "/task" if command_style == "slash" else "cb task"
        issue_cmd = "/issue" if command_style == "slash" else "cb issue"
        raise RuntimeError(
            "No active Codeband task room found (.codeband_room missing). "
            f"Start a task first with '{task_cmd}' or '{issue_cmd}'."
        )

    api_key = _require_api_key()
    human_client = AsyncRestClient(api_key=api_key, base_url=config.band.rest_url)
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
    human_client,
    identities: dict[str, tuple],
    agent_config,
    config,
    project_dir: Path,
) -> None:
    """Remove agents from the previous task room only.

    Reads .codeband_room to find the specific room to clean up. Other rooms
    (including concurrent Codeband tasks) are left untouched.
    """
    room_file = project_dir / ".codeband_room"
    try:
        prev_room_id = room_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        prev_room_id = None

    if not prev_room_id:
        logger.debug("No previous task room found, skipping cleanup")
        return

    logger.info("Cleaning up previous task room: %s", prev_room_id)
    await _remove_agents_from_room(prev_room_id, agent_config, config)


async def reset_active_room(config: CodebandConfig, project_dir: Path) -> str | None:
    """Remove all agents from the active task room and delete the pointer file.

    Returns the room id that was cleaned up, or None if there was nothing to
    reset. Safe to call repeatedly — missing file or dead-on-Band room both
    reduce to a no-op.
    """
    room_file = project_dir / ".codeband_room"
    try:
        room_id = room_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not room_id:
        room_file.unlink(missing_ok=True)
        return None

    agent_config = load_agent_config(project_dir)
    await _remove_agents_from_room(room_id, agent_config, config)
    room_file.unlink(missing_ok=True)
    return room_id
