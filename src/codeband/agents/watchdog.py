"""Deterministic watchdog daemon — polls Band.ai REST and escalates stale agents."""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from codeband.config import WatchdogConfig

logger = logging.getLogger(__name__)


def _mentioned_participant_ids(
    msg: Any, participant_names: dict[str, str],
) -> set[str]:
    """Return the set of participant ids mentioned in this chat message.

    Tries structured ``msg.mentions`` first (Band.ai chat messages carry
    mentions as a list of items with ``.id``); falls back to scanning the
    message content for ``@<display_name>`` with right-side word-boundary
    semantics so ``@Coder-Claude-0`` does not match ``@Coder-Claude-01``.
    Test mocks that don't set these fields are silently ignored — the
    isinstance checks reject MagicMock-typed sentinels.
    """
    found: set[str] = set()

    raw_mentions = getattr(msg, "mentions", None)
    if isinstance(raw_mentions, (list, tuple)):
        for item in raw_mentions:
            mid = getattr(item, "id", None)
            if isinstance(mid, str) and mid in participant_names:
                found.add(mid)

    content = getattr(msg, "content", None)
    if isinstance(content, str):
        for pid, pname in participant_names.items():
            if not pname:
                continue
            # Both-sided word boundary: `@` must be a true mention prefix
            # (not part of a longer token like `email@Coder-Claude-0`), and
            # trailing chars must terminate the name (so `@Coder-Claude-0`
            # is not a substring match of `@Coder-Claude-01`).
            pattern = re.compile(
                rf"(?<![A-Za-z0-9_\-])@{re.escape(pname)}(?![A-Za-z0-9_\-])",
            )
            if pattern.search(content):
                found.add(pid)

    return found


@dataclasses.dataclass
class AgentHealthState:
    """In-memory health tracking for a single agent."""

    last_seen: datetime
    nudged_at: datetime | None = None
    nudge_count: int = 0
    escalated: bool = False  # Escalate-once: stays True until agent becomes healthy
    # Set when an agent was healthy on a patrol cycle AFTER being nudged —
    # i.e. the nudge confirmed it's alive. Gates `nudge_suppression_seconds`
    # so legitimately-idle agents don't get re-nudged every staleness cycle.
    confirmed_alive_at: datetime | None = None


class WatchdogDaemon:
    """Deterministic health monitor — polls via REST and escalates on threshold crossings.

    Runs as a plain asyncio task (not a Band.ai Agent). Reuses the Conductor's
    REST credentials for writes so it doesn't consume a platform agent slot or
    room participant seat. On enterprise tier, an additional human-API REST
    client is supplied for reads so the liveness signal includes thoughts and
    tool calls in addition to chat text; on free tier that client is omitted
    and reads fall back to the agent-API inbox (chat-only). Escalation policy
    is described by ``stale_threshold_seconds`` (default, per-role overrides
    via ``role_stale_thresholds``) plus the nudge/escalate-once state machine
    in ``_patrol``.
    """

    def __init__(
        self,
        *,
        config: WatchdogConfig,
        rest_client: Any,
        agent_id: str,
        conductor_id: str,
        activity: Any | None = None,
        agent_id_to_role: dict[str, str] | None = None,
        human_rest_client: Any | None = None,
        local_memory_store: Any | None = None,
    ):
        self._config = config
        self._rest = rest_client
        self._human_rest = human_rest_client
        self._agent_id = agent_id
        self._conductor_id = conductor_id
        self._state: dict[str, AgentHealthState] = {}
        self._activity = activity
        self._role_map = agent_id_to_role or {}
        # Rooms we've already warned about for stale state — log once per
        # room instead of every patrol cycle.
        self._warned_stale_rooms: set[str] = set()
        # Memory backend for the swarm-status gate. On paid tier this stays
        # None and we read via `rest_client.agent_api_memories`; on free tier
        # the runner injects a `LocalMemoryStore` so the watchdog reads from
        # the same JSONL file the agent tools write to.
        self._memory_store = local_memory_store
        # Track whether we logged an "idle — skipping patrols" line this
        # idle window so we don't repeat it every cycle.
        self._idle_skip_logged = False

    async def run(self) -> None:
        """Main patrol loop — runs until cancelled."""
        import asyncio

        liveness = "human-api" if self._human_rest else "agent-api"
        logger.info(
            "Watchdog daemon started (interval=%ds, default_threshold=%ds, "
            "role_overrides=%s, liveness=%s)",
            self._config.check_interval_seconds,
            self._config.stale_threshold_seconds,
            dict(self._config.role_stale_thresholds),
            liveness,
        )
        while True:
            try:
                await self._patrol()
            except Exception:
                logger.exception("Watchdog patrol failed")
            await asyncio.sleep(self._config.check_interval_seconds)

    def _threshold_for(self, agent_id: str) -> timedelta:
        """Resolve the stale threshold for an agent by its role."""
        role = self._role_map.get(agent_id)
        seconds = self._config.role_stale_thresholds.get(
            role or "", self._config.stale_threshold_seconds,
        )
        return timedelta(seconds=seconds)

    def _max_window(self) -> timedelta:
        """Largest threshold across default + role overrides — bounds the read."""
        candidates = [self._config.stale_threshold_seconds,
                      *self._config.role_stale_thresholds.values()]
        return timedelta(seconds=max(candidates))

    async def _list_rooms(self) -> list[Any]:
        """List chat rooms, preferring the human API when available."""
        if self._human_rest is not None:
            resp = await self._human_rest.human_api_chats.list_my_chats()
        else:
            resp = await self._rest.agent_api_chats.list_agent_chats()
        return list(resp.data or [])

    async def _read_latest_swarm_status(self) -> tuple[str, datetime] | None:
        """Read the most recent ``swarm status …`` envelope from memory.

        Returns ``(state, written_at)`` for the newest matching envelope, or
        ``None`` if memory is unavailable, returns no matches, or the latest
        record's content does not parse.

        The Conductor writes these envelopes (see ``prompts/conductor.md``):
        ``swarm status active task <slug>`` when accepting a new user task,
        ``swarm status waiting_human_approval task <slug> pr <N>`` while a PR
        is blocked on a human merge decision, and
        ``swarm status complete task <slug>`` when reporting completion. We
        gate patrols on this so a fully-idle or correctly-waiting swarm is not
        poked between actionable steps.
        """
        try:
            if self._memory_store is not None:
                resp = await self._memory_store.list(
                    system="working", type="episodic", segment="agent",
                    scope="organization", content_query="swarm status",
                )
            else:
                resp = await self._rest.agent_api_memories.list_agent_memories(
                    system="working", type="episodic", segment="agent",
                    scope="organization", content_query="swarm status",
                )
            records = list(getattr(resp, "data", None) or [])
        except Exception:
            logger.debug(
                "Watchdog could not read swarm-status envelope", exc_info=True,
            )
            return None

        if not records:
            return None

        def _ts(rec: Any) -> datetime:
            value = getattr(rec, "updated_at", None) or getattr(rec, "inserted_at", None)
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=UTC)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value)
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                except ValueError:
                    pass
            return datetime.min.replace(tzinfo=UTC)

        latest = max(records, key=_ts)
        content = (getattr(latest, "content", "") or "").strip()
        first_line = content.split("\n", 1)[0].lower()
        parts = first_line.split()
        # Expect "swarm status <state> ..." — anything else means the record
        # was written outside our protocol; treat as no-signal.
        if len(parts) < 3 or parts[0] != "swarm" or parts[1] != "status":
            return None
        return parts[2], _ts(latest)

    async def _list_messages(self, room_id: str, since: datetime) -> list[Any]:
        """List messages in a room.

        On enterprise tier uses the human API (captures text + thought +
        tool_call + tool_result + error); on free tier uses the agent-API
        inbox (text only, chat-only).
        """
        if self._human_rest is not None:
            resp = await self._human_rest.human_api_messages.list_my_chat_messages(
                chat_id=room_id, since=since,
            )
        else:
            resp = await self._rest.agent_api_messages.list_agent_messages(
                chat_id=room_id, status="all",
            )
        return list(resp.data or [])

    async def _patrol(self) -> None:
        """Single patrol cycle: check all rooms for stale agents."""
        now = datetime.now(UTC)

        # Gate: if the Conductor has reported task completion or is waiting on
        # a human merge approval within the idle-grace window, the agents have
        # nothing actionable to do — skip nudging entirely. Falls through
        # (today's time-based behavior) when no envelope exists, so a fresh
        # swarm or one whose Conductor has not yet adopted the protocol is
        # unaffected.
        status = await self._read_latest_swarm_status()
        if status is not None:
            state, written_at = status
            grace = timedelta(seconds=self._config.swarm_idle_grace_seconds)
            if state in {"complete", "waiting_human_approval"} and now - written_at < grace:
                if not self._idle_skip_logged:
                    logger.info(
                        "Watchdog: swarm status is '%s' (written %ds ago) — "
                        "suppressing nudges until grace window of %ds elapses",
                        state,
                        int((now - written_at).total_seconds()),
                        self._config.swarm_idle_grace_seconds,
                    )
                    self._idle_skip_logged = True
                return
        # Active again (or no envelope) — reset the once-per-window log gate.
        self._idle_skip_logged = False

        # Bound the read window to 2x the largest threshold — any agent whose
        # last activity falls outside this window is definitely stale, and we
        # don't need older records to make a decision.
        since = now - self._max_window() * 2

        try:
            rooms = await self._list_rooms()
        except Exception:
            logger.exception("Failed to list chats during patrol")
            return

        from thenvoi_rest.errors.not_found_error import NotFoundError

        for room in rooms:
            room_id = room.id
            try:
                messages = await self._list_messages(room_id, since)
                # Participant list is always via the agent API — the write
                # path uses agent credentials and needs the Conductor's view
                # of who's currently mentionable in the room.
                parts_response = (
                    await self._rest.agent_api_participants.list_agent_chat_participants(
                        chat_id=room_id,
                    )
                )
            except NotFoundError:
                # Room was deleted server-side but still appears in the agent's
                # chat listing (stale Band.ai state from a prior session).
                # Skip quietly — running `cb reset` before the next session
                # removes the agent from the stale room. Warn once per room
                # so the diagnostic is visible without spamming every patrol.
                if room_id not in self._warned_stale_rooms:
                    self._warned_stale_rooms.add(room_id)
                    logger.warning(
                        "Room %s no longer exists — skipping. "
                        "Run 'cb reset' to clean up stale session state.",
                        room_id,
                    )
                continue
            except Exception:
                logger.exception("Failed to inspect room %s during patrol", room_id)
                continue

            # Only mention agents that are currently in the room — historic
            # senders who have since been removed would trigger HTTP 422
            # `mentioned_participant_not_in_room` from the server. Human
            # participants (type="User") are excluded so the watchdog never
            # nudges or escalates at the human user who opened the session.
            participant_names: dict[str, str] = {
                p.id: (p.name or p.id) for p in parts_response.data
                if p.type == "Agent"
            }

            # Per-agent activity signals.
            # `last_message`: the agent itself spoke. `last_mentioned`: another
            # participant @-mentioned the agent (e.g. the Conductor dispatched
            # work to a Coder). The staleness clock starts from the most recent
            # of the two — without this, an agent dispatched a task but
            # crashing before its first reply is invisible to the patrol and
            # never gets nudged.
            last_message: dict[str, datetime] = {}
            last_mentioned: dict[str, datetime] = {}
            for msg in messages:
                sender = msg.sender_id
                ts = msg.inserted_at
                if ts is None:
                    continue
                if sender in participant_names and (
                    sender not in last_message or ts > last_message[sender]
                ):
                    last_message[sender] = ts
                # Mentions: a sender does not start their own staleness clock
                # by mentioning themselves, so skip self-mentions.
                for mid in _mentioned_participant_ids(msg, participant_names):
                    if mid == sender:
                        continue
                    if mid not in last_mentioned or ts > last_mentioned[mid]:
                        last_mentioned[mid] = ts

            # Iterate every agent participant (skip self). An agent that has
            # neither spoken nor been mentioned is "untracked" — preserve the
            # historical behavior of not nudging dormant pool members who
            # haven't been given any work yet.
            for agent_id in participant_names:
                if agent_id == self._agent_id:
                    continue
                last_msg_ts = last_message.get(agent_id)
                last_mention_ts = last_mentioned.get(agent_id)
                if last_msg_ts is None and last_mention_ts is None:
                    continue
                last_seen = max(
                    t for t in (last_msg_ts, last_mention_ts) if t is not None
                )

                threshold = self._threshold_for(agent_id)
                staleness = now - last_seen
                state = self._state.get(agent_id)

                if staleness <= threshold:
                    # Healthy. If we had previously nudged this agent, record
                    # `confirmed_alive_at` so the post-response cooldown kicks
                    # in — prevents nagging legitimately-idle agents (e.g. a
                    # Planner waiting on human approval). The cooldown must
                    # survive subsequent healthy patrols where `nudged_at` is
                    # already None; only drop state when there is nothing
                    # worth preserving.
                    if state is not None and state.nudged_at is not None:
                        state.confirmed_alive_at = last_seen
                        state.nudged_at = None
                        state.nudge_count = 0
                        state.escalated = False
                    elif state is None or state.confirmed_alive_at is None:
                        self._state.pop(agent_id, None)
                    # else: cooldown active — preserve state until it elapses
                    continue

                # Stale. If the agent responded to a recent nudge, honour the
                # cooldown before nudging again.
                if state is not None and state.confirmed_alive_at is not None:
                    cooldown = timedelta(
                        seconds=self._config.nudge_suppression_seconds,
                    )
                    if now - state.confirmed_alive_at < cooldown:
                        continue
                    # Cooldown elapsed — treat as a fresh detection.
                    state.confirmed_alive_at = None

                try:
                    if state is None or state.nudged_at is None:
                        # First detection — send nudge
                        await self._send_nudge(room_id, agent_id, participant_names)
                    elif (
                        state.nudge_count >= 1
                        and not state.escalated
                        and (now - state.nudged_at)
                        >= timedelta(seconds=self._config.nudge_grace_seconds)
                    ):
                        # Escalate-once: flip state before attempting the send so
                        # a server rejection (e.g. mention validation) doesn't
                        # produce an unbounded retry loop on every patrol.
                        state.escalated = True
                        await self._send_escalation(
                            room_id, agent_id, staleness, participant_names,
                        )
                except Exception:
                    logger.exception(
                        "Failed to nudge/escalate %s in room %s", agent_id, room_id,
                    )

    async def _send_nudge(
        self, room_id: str, agent_id: str, names: dict[str, str],
    ) -> None:
        """Send a nudge message to a stale agent."""
        from thenvoi_rest.types import ChatMessageRequest, ChatMessageRequestMentionsItem

        logger.info("Nudging stale agent %s in room %s", agent_id, room_id)
        if self._activity:
            self._activity.log("AGENT_NUDGED", "watchdog", f"Nudged {agent_id}")
        display = names.get(agent_id, agent_id)
        await self._rest.agent_api_messages.create_agent_chat_message(
            chat_id=room_id,
            message=ChatMessageRequest(
                content=(
                    f"[Watchdog] Status check for @{display} — "
                    f"please report your current state."
                ),
                mentions=[ChatMessageRequestMentionsItem(id=agent_id)],
            ),
        )
        now = datetime.now(UTC)
        state = self._state.get(agent_id)
        if state is None:
            state = AgentHealthState(last_seen=now)
            self._state[agent_id] = state
        state.nudged_at = now
        state.nudge_count += 1

    async def _send_escalation(
        self,
        room_id: str,
        agent_id: str,
        staleness: timedelta,
        names: dict[str, str],
    ) -> None:
        """Send escalation alert — a louder second ping at the stale agent.

        Mentioning the Conductor here is impossible: the Watchdog borrows the
        Conductor's REST credentials (see ``orchestration/runner.py``), so a
        self-mention is rejected by Band.ai with ``cannot_mention_self``. The
        Conductor still receives this message through its own inbound chat
        event stream because it's a room participant.
        """
        minutes = staleness.total_seconds() / 60
        logger.warning(
            "Escalating: agent %s unresponsive for %.0f minutes", agent_id, minutes,
        )
        if self._activity:
            self._activity.log(
                "AGENT_ESCALATED", "watchdog",
                f"Escalated {agent_id} (unresponsive {minutes:.0f}m)",
            )
        from thenvoi_rest.types import ChatMessageRequest, ChatMessageRequestMentionsItem

        display = names.get(agent_id, agent_id)
        await self._rest.agent_api_messages.create_agent_chat_message(
            chat_id=room_id,
            message=ChatMessageRequest(
                content=(
                    f"Watchdog escalation: agent @{display} appears stuck. "
                    f"Last activity: {minutes:.0f} minutes ago. "
                    f"Nudge sent with no response."
                ),
                mentions=[ChatMessageRequestMentionsItem(id=agent_id)],
            ),
        )

    async def close(self) -> None:
        """Cleanup (no-op, follows agent pattern)."""
