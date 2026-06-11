"""Deterministic watchdog daemon — polls Band.ai REST and escalates stale agents."""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from codeband.config import WatchdogConfig

logger = logging.getLogger(__name__)


def _parse_ts(value: Any) -> datetime | None:
    """Coerce an ISO-8601 string or datetime into a tz-aware UTC datetime.

    Returns ``None`` for missing or unparseable values. Naive datetimes are
    assumed to be UTC.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@functools.lru_cache(maxsize=64)
def _mention_patterns(
    participants: frozenset[tuple[str, str]],
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile @-mention patterns ONCE per participant set (S8-F1).

    Previously every message × participant recompiled the same regex — a
    patrol over a busy room cost hundreds of identical ``re.compile`` calls
    per cycle. A room's participant set is stable across patrols, so the
    LRU cache (keyed on the frozen ``(id, name)`` set) makes compilation a
    once-per-set cost shared by every later patrol.

    Both-sided word boundary: ``@`` must be a true mention prefix (not part
    of a longer token like ``email@Coder-Claude-0``), and trailing chars must
    terminate the name (so ``@Coder-Claude-0`` is not a substring match of
    ``@Coder-Claude-01``).
    """
    return tuple(
        (
            pid,
            re.compile(rf"(?<![A-Za-z0-9_\-])@{re.escape(pname)}(?![A-Za-z0-9_\-])"),
        )
        for pid, pname in sorted(participants)
        if pname
    )


def _mentioned_participant_ids(
    msg: Any, participant_names: dict[str, str],
) -> set[str]:
    """Return the set of participant ids mentioned in this chat message.

    Tries structured ``msg.mentions`` first (Band.ai chat messages carry
    mentions as a list of items with ``.id``); falls back to scanning the
    message content with the cached per-participant-set patterns from
    :func:`_mention_patterns`. Test mocks that don't set these fields are
    silently ignored — the isinstance checks reject MagicMock-typed
    sentinels.
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
        for pid, pattern in _mention_patterns(frozenset(participant_names.items())):
            if pattern.search(content):
                found.add(pid)

    return found


_TERMINAL_PROTOCOL_RE = re.compile(
    r"("
    r"\bReview\s+(?:PASSED|FAILED)\b|"
    r"\bReview requested for PR\s+#?\d+\b|"
    r"\bMerged\s+PR\s+#?\d+\b|"
    r"\bcomplete\s+and\s+ready\s+for\s+review\b|"
    r"\bStatus:\s*idle\b|"
    r"\bIdle\b.*\bNo pending work\b"
    r")",
    re.IGNORECASE,
)


def _is_terminal_protocol_message(content: Any) -> bool:
    """True when an agent's latest message says its current protocol work is done."""
    return isinstance(content, str) and bool(_TERMINAL_PROTOCOL_RE.search(content))


# Subtask states the mechanical-progress patrol watches (RFC WS4 + Stage-2 +
# S2-1/F12). ``in_progress`` / ``verify_pending`` are the coder's working
# states; ``merge_pending`` is the merge queue — a subtask resting there with
# no progress (e.g. an approval request nobody acted on) goes stale like any
# other and escalates through the standard stall path. The watchdog never
# queries GitHub to *reconcile* a merge — that is ``cb-phase merge``'s
# idempotent reconcile step; only the existing PR-activity progress signal
# applies here, as it does to every patrolled state.
#
# ``review_pending`` / ``review_failed`` / ``review_passed`` / ``needs_rebase``
# are the resting states where dispatched work can silently die: a reviewer
# that never renders a verdict, a coder that never picks up the rework, a
# Mergemaster that never queues the approved PR, a rebase nobody starts. The
# mechanical signals are state-agnostic — transition recency applies to every
# state (each of these is *entered* by a transition, so the row exists), and
# the PR-activity signal applies wherever ``pr_number`` is set (it is, by the
# verify leg, for everything at/past ``review_pending``).
_PATROLLED_SUBTASK_STATES: frozenset[str] = frozenset(
    {
        "in_progress",
        "verify_pending",
        "review_pending",
        "review_failed",
        "review_passed",
        "merge_pending",
        "needs_rebase",
    }
)


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
    # ── Mechanical progress tracking (RFC WS4) ──────────────────────────────
    # Used for the per-subtask progress signals, not the chat-recency path.
    # Consecutive patrols with no observed mechanical progress (no git-HEAD
    # change on the subtask's branch and no newer transition-log/PR timestamp).
    patrol_visits_without_progress: int = 0
    # Last observed git HEAD for the subtask's branch ("" until first seen).
    last_git_head: str = ""
    # Most recent progress timestamp observed from the transition log or the
    # PR's updatedAt — whichever is newer.
    last_transition_timestamp: datetime | None = None
    # Consecutive patrols where EVERY attempted mechanical signal read FAILED
    # (git error, gh error, store error — as opposed to returned-but-unchanged).
    # Such a patrol observed nothing and does not count toward the stall cap
    # (S6-F6: observation vs absence); this counter makes a permanently
    # degraded probe visible at debug level.
    no_data_patrols: int = 0


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
        state_store: Any | None = None,
        owner_id: str | None = None,
        owner_handle: str | None = None,
        bare_repo: Any | None = None,
        repo_slug: str | None = None,
    ):
        self._config = config
        self._rest = rest_client
        self._human_rest = human_rest_client
        self._agent_id = agent_id
        self._conductor_id = conductor_id
        # Repo context for the mechanical-progress probes (S9-1), injected at
        # construction by the runner: ``bare_repo`` is the workspace layout's
        # bare clone (``{workspace}/repo.git``) that every coder branch is
        # pushed through, ``repo_slug`` is ``owner/repo`` from config
        # ``repo.url``. Without them the probes ran from the watchdog
        # process's cwd — which is the project dir only in the dogfood
        # topology, so in any other layout ``_git_head`` / ``_pr_updated_at``
        # silently resolved nothing (or, worse, the WRONG repo's state).
        # ``None`` degrades to the historical cwd behavior.
        self._bare_repo = bare_repo
        self._repo_slug = repo_slug
        # Owner/CC participant to @mention when a subtask lands in ``blocked``
        # (from ANY source — the watchdog's own stall cap, the verify-attempt
        # cap, or the review-round cap). ``owner_id`` is the Band participant id
        # used for the structured mention; ``owner_handle`` is the display name
        # for the message text (falls back to the id). DORMANT by default: when
        # ``owner_id`` is None (the runner does not pass it pre-activation) the
        # blocked-escalation patrol is a no-op, so this ships safely ahead of the
        # verify-gate activation. The CC-side Monitors remain the fail-safe.
        self._owner_id = owner_id
        self._owner_handle = owner_handle
        # Escalate-once per subtask, so a blocked subtask is announced to the
        # owner a single time rather than every patrol. Keyed by
        # ``(task_id, subtask_id)`` — subtask ids repeat across tasks (planners
        # emit st-1, st-2, … fresh per plan), so a bare subtask_id key would let
        # one task's escalation swallow another's.
        self._owner_escalated: set[tuple[str, str]] = set()
        self._state: dict[str, AgentHealthState] = {}
        # Durable state store (RFC WS1). May be None — when absent the watchdog
        # degrades to chat-recency-only behavior and the mechanical-progress
        # path is skipped entirely.
        self._store = state_store
        # Per-subtask mechanical-progress health, keyed by
        # ``(task_id, subtask_id)`` — task-scoped like the store rows it
        # mirrors. Separate from `_state` (which is keyed by agent_id for the
        # chat-recency path).
        self._subtask_state: dict[tuple[str, str], AgentHealthState] = {}
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
            return list(resp.data or [])
        resp = await self._rest.agent_api_messages.list_agent_messages(
            chat_id=room_id, status="all",
        )
        # The agent API has no ``since`` parameter (server-side paging gap —
        # a platform ask, not something to fight client-side), so the whole
        # room history comes back every patrol. Apply the same window bound
        # the human-API path gets server-side here, after fetch, so the
        # per-message mention scan never chews through months of history.
        # Messages with a missing/unparseable timestamp are kept — the patrol
        # loop already skips them, and dropping them here would silently
        # change behavior for partial records.
        messages = []
        for msg in resp.data or []:
            ts = _parse_ts(getattr(msg, "inserted_at", None))
            if ts is None or ts >= since:
                messages.append(msg)
        return messages

    async def _patrol(self) -> None:
        """Single patrol cycle: check all rooms for stale agents."""
        import asyncio

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

        from thenvoi_rest.core.api_error import ApiError
        from thenvoi_rest.errors.not_found_error import NotFoundError

        # Active-only patrol: task rows drive which rooms matter. A room whose
        # task row is no longer 'active' (e.g. superseded by a later
        # registration — see state/registration.py) is dead to the watchdog
        # and skipped before any REST call, which also retires the stale-room
        # NotFoundError warning for superseded rows. Each active room's owner
        # is resolved here too, for the nudge-eligibility filter below. With
        # no store (or a failed read) both maps stay empty and the patrol
        # degrades to the unfiltered behavior.
        task_rows = await asyncio.to_thread(self._task_rows)
        active_room_owners: dict[str, str | None] = {}
        inactive_rooms: set[str] = set()
        if task_rows:
            active_room_owners = {
                room: owner for _, room, status, owner in task_rows
                if status == "active"
            }
            inactive_rooms = {
                room for _, room, status, _ in task_rows if status != "active"
            } - set(active_room_owners)

        try:
            rooms = await self._list_rooms()
        except ApiError as e:
            logger.warning(
                "Watchdog: skipping patrol — list-chats returned HTTP %s%s",
                e.status_code,
                " (rate-limited by Band.ai)" if e.status_code == 429 else "",
            )
            return
        except Exception:
            logger.exception("Failed to list chats during patrol")
            return

        for room in rooms:
            room_id = room.id
            if room_id in inactive_rooms:
                continue
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
            except ApiError as e:
                logger.warning(
                    "Watchdog: skipping room %s — HTTP %s%s",
                    room_id,
                    e.status_code,
                    " (rate-limited by Band.ai)" if e.status_code == 429 else "",
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
            # The task owner is excluded regardless of type: mission control
            # may be an Agent-typed peer (initiator-as-owner), and it receives
            # owner escalations but must never be treated as a stalled worker.
            room_owner = active_room_owners.get(room_id) or self._owner_id
            participant_names: dict[str, str] = {
                p.id: (p.name or p.id) for p in parts_response.data
                if p.type == "Agent" and p.id != room_owner
            }

            # Per-agent activity signals.
            # `last_message`: the agent itself spoke. `last_mentioned`: another
            # participant @-mentioned the agent (e.g. the Conductor dispatched
            # work to a Coder). The staleness clock starts from the most recent
            # of the two — without this, an agent dispatched a task but
            # crashing before its first reply is invisible to the patrol and
            # never gets nudged.
            last_message: dict[str, datetime] = {}
            last_message_content: dict[str, Any] = {}
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
                    last_message_content[sender] = getattr(msg, "content", None)
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
                if (
                    last_msg_ts is not None
                    and (last_mention_ts is None or last_msg_ts >= last_mention_ts)
                    and _is_terminal_protocol_message(
                        last_message_content.get(agent_id),
                    )
                ):
                    self._state.pop(agent_id, None)
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
                        # Escalate-once, marker-after-send: the flag flips only
                        # when the send lands (or is permanently undeliverable,
                        # HTTP 422), so a transient failure retries next patrol
                        # instead of being silently burned forever. See
                        # _attempt_escalation_send for the full policy.
                        if await self._attempt_escalation_send(
                            self._send_escalation(
                                room_id, agent_id, staleness, participant_names,
                            ),
                            target=f"agent {agent_id}",
                            room_id=room_id,
                        ):
                            state.escalated = True
                except Exception:
                    logger.exception(
                        "Failed to nudge/escalate %s in room %s", agent_id, room_id,
                    )

        # Third escalation rung (RFC WS4): mechanical per-subtask progress.
        # Independent of the chat-recency path above; guarded so a store/git
        # failure never breaks the patrol loop.
        await self._check_subtask_progress(now)

        # Fourth rung: owner escalation for subtasks already in ``blocked`` —
        # from ANY source (stall cap, verify cap, review cap). Dormant until an
        # owner_id is supplied (post-activation); guarded so a notify failure
        # never breaks the patrol loop.
        await self._check_blocked_subtasks(now)

    def _task_rows(self) -> list[tuple[str, str, str, str | None]] | None:
        """Return ``(task_id, room_id, status, owner_id)`` for every task row.

        Returns ``None`` when no store is wired or the read fails — callers
        degrade to the unfiltered (pre-store) behavior. Reads the store's
        SQLite file directly (read-only), mirroring :meth:`_latest_transition`,
        since the Workstream-1 store surface does not expose a task-listing
        query.
        """
        if self._store is None:
            return None
        db_path = getattr(self._store, "db_path", None)
        if db_path is None:
            return None
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT task_id, room_id, status, owner_id FROM tasks",
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.debug("Could not read task rows", exc_info=True)
            return None
        return [(r[0], r[1], r[2], r[3]) for r in rows]

    def _active_task_ids(self) -> set[str] | None:
        """Task ids with ``status='active'``, or ``None`` to disable filtering."""
        rows = self._task_rows()
        if rows is None:
            return None
        return {task_id for task_id, _, status, _ in rows if status == "active"}

    async def _attempt_escalation_send(
        self, send: Any, *, target: str, room_id: str,
    ) -> bool:
        """Await an escalation *send*; return True when its once-marker may burn.

        Marker-after-send: an escalate-once marker burns only on a successful
        send, so a transient failure (network, 429, 5xx) is retried on the
        next patrol instead of being silently burned forever. Accepted trade:
        a send that succeeds but whose response is lost burns nothing and can
        produce a duplicate escalation. The one permanent failure is the
        server's HTTP 422 mention rejection
        (``mentioned_participant_not_in_room`` — see the participant-filter
        comment in :meth:`_patrol`): retrying the same message can only be
        rejected again, so burn the marker anyway and log at CRITICAL.
        """
        from thenvoi_rest.core.api_error import ApiError

        try:
            await send
        except ApiError as e:
            if e.status_code == 422:
                logger.critical(
                    "%s not mentionable in room %s — escalation undeliverable",
                    target, room_id,
                )
                return True
            logger.warning(
                "Escalation send to %s in room %s failed (HTTP %s) — "
                "will retry next patrol",
                target, room_id, e.status_code,
            )
            return False
        except Exception:
            logger.warning(
                "Escalation send to %s in room %s failed — will retry next patrol",
                target, room_id, exc_info=True,
            )
            return False
        return True

    async def _check_subtask_progress(self, now: datetime) -> None:
        """Detect stalled subtasks via mechanical signals and escalate (RFC WS4).

        For each in-flight subtask in :data:`_PATROLLED_SUBTASK_STATES` we read
        three deterministic signals — the git HEAD of its branch, its PR's
        ``updatedAt`` and the most recent ``transition_log`` timestamp. A change
        in any of them since the last patrol counts as progress and resets the
        per-subtask stall counter; otherwise the counter increments. When it
        reaches ``max_phase_visits`` the subtask is marked blocked (via the FSM
        when available) and the Conductor + human are notified.

        Fully no-ops when the store is absent or ``git_progress_check`` is off,
        preserving the prior chat-recency-only behavior.
        """
        import asyncio

        if self._store is None or not self._config.git_progress_check:
            return

        try:
            subtasks = await asyncio.to_thread(self._store.list_active_subtasks)
        except Exception:
            logger.debug("Watchdog could not list subtasks from store", exc_info=True)
            return

        active_task_ids = await asyncio.to_thread(self._active_task_ids)

        for sub in subtasks:
            # Active-only: a subtask of a superseded/closed task is dead to
            # the watchdog. None (read failure) degrades to no filtering.
            if active_task_ids is not None and sub.task_id not in active_task_ids:
                continue
            if sub.state not in _PATROLLED_SUBTASK_STATES:
                continue
            try:
                await self._check_one_subtask(sub, now)
            except Exception:
                logger.exception(
                    "Watchdog subtask-progress check failed for %s", sub.subtask_id,
                )

    async def _check_one_subtask(self, sub: Any, now: datetime) -> None:
        """Evaluate mechanical progress for a single in-flight subtask."""
        import asyncio

        branch = (sub.metadata or {}).get("branch") if sub.metadata else None
        # Observation vs absence (S6-F6): count how many signal reads were
        # *attempted* and how many FAILED to yield any data (git error, gh
        # error, store error). "Returned but unchanged" is an observation —
        # only a no-data read counts as failed.
        reads_attempted = 0
        reads_failed = 0

        git_head = None
        if branch:
            reads_attempted += 1
            git_head = await asyncio.to_thread(self._git_head, branch)
            if git_head is None:
                reads_failed += 1
        pr_ts = None
        if sub.pr_number is not None:
            reads_attempted += 1
            pr_ts = await asyncio.to_thread(self._pr_updated_at, sub.pr_number)
            if pr_ts is None:
                reads_failed += 1
        reads_attempted += 1
        transition_ok, transition_ts = await asyncio.to_thread(
            self._latest_transition, sub.subtask_id, sub.task_id,
        )
        if not transition_ok:
            reads_failed += 1
        # Newest of the two timestamped signals (PR update vs. transition log).
        latest_ts = max(
            (t for t in (pr_ts, transition_ts) if t is not None),
            default=None,
        )

        key = (sub.task_id, sub.subtask_id)
        health = self._subtask_state.get(key)
        if health is None:
            health = AgentHealthState(last_seen=now)
            self._subtask_state[key] = health

        # A patrol where EVERY attempted read failed observed nothing — it is
        # not evidence of a stall and must not advance the stall counter.
        # Track it separately so a permanently degraded probe (broken git,
        # expired gh auth) stays visible instead of silently freezing the cap.
        if reads_failed == reads_attempted:
            health.no_data_patrols += 1
            logger.debug(
                "Subtask %s: all %d mechanical signal reads failed "
                "(%d consecutive no-data patrols) — not counted toward "
                "the stall cap",
                sub.subtask_id, reads_attempted, health.no_data_patrols,
            )
            return
        health.no_data_patrols = 0

        progressed = False
        if git_head is not None and git_head != health.last_git_head:
            progressed = True
        if latest_ts is not None and (
            health.last_transition_timestamp is None
            or latest_ts > health.last_transition_timestamp
        ):
            progressed = True

        # Record the new baselines before deciding, so the next patrol compares
        # against the latest observation.
        if git_head is not None:
            health.last_git_head = git_head
        if latest_ts is not None:
            health.last_transition_timestamp = latest_ts

        if progressed:
            health.patrol_visits_without_progress = 0
            # Recovery — allow a future stall to escalate again.
            health.escalated = False
            return

        health.patrol_visits_without_progress += 1
        if (
            health.patrol_visits_without_progress >= self._config.max_phase_visits
            and not health.escalated
        ):
            # Marker-after-send (S6-F7n), matching the two sibling rungs: the
            # escalate-once marker burns only when the escalation reports
            # success (FSM transition applied or alert landed), so a transient
            # failure retries next patrol. A double-send is acceptable; a
            # silent permanent stall is not.
            if await self._send_blocked_escalation(sub):
                health.escalated = True

    def _git_head(self, branch: str) -> str | None:
        """Return the commit SHA at ``branch``, or ``None`` if it can't be read.

        Runs against the injected bare repo (``git -C <bare_repo>``) when one
        was supplied at construction — cwd-independent. ``--verify`` makes a
        nonexistent branch a clean non-zero exit instead of echoed garbage,
        and ``--end-of-options`` stops a branch name from ever being parsed
        as an option (the sweep-4 F-6 argument-injection note). A ``None``
        result is counted exactly as before — stall semantics are Batch 3's.
        """
        cmd = ["git"]
        if self._bare_repo is not None:
            cmd += ["-C", str(self._bare_repo)]
        cmd += ["rev-parse", "--verify", "--end-of-options", branch]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("git rev-parse %s failed", branch, exc_info=True)
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _pr_updated_at(self, pr_number: int) -> datetime | None:
        """Return the PR's ``updatedAt`` timestamp via ``gh``, or ``None``.

        A change in ``updatedAt`` captures any PR activity — including state
        transitions — so it doubles as the PR-state progress signal. Pinned
        with ``--repo <slug>`` when the runner injected one — repo identity
        from config, not from whatever repo the cwd happens to be in.
        """
        cmd = ["gh", "pr", "view", str(pr_number), "--json", "state,updatedAt"]
        if self._repo_slug is not None:
            cmd += ["--repo", self._repo_slug]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug("gh pr view %s failed", pr_number, exc_info=True)
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        return _parse_ts(data.get("updatedAt"))

    def _latest_transition(
        self, subtask_id: str, task_id: str,
    ) -> tuple[bool, datetime | None]:
        """Return ``(ok, MAX(timestamp))`` from the transition log for a subtask.

        Reads the store's SQLite file directly (read-only) since the
        Workstream-1 store surface does not expose a transition-log query. The
        read is task-scoped — a same-id subtask from another task must not
        count as progress here.

        ``ok`` distinguishes observation from absence (S6-F6): ``(True, None)``
        means the query succeeded and found no rows (e.g. before the FSM is
        wired up) — an *observation*; ``(False, None)`` means the read itself
        FAILED (no db path, sqlite error) and nothing was observed.
        """
        db_path = getattr(self._store, "db_path", None)
        if db_path is None:
            return False, None
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT MAX(timestamp) FROM transition_log "
                    "WHERE task_id = ? AND subtask_id = ?",
                    (task_id, subtask_id),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.debug("Could not read transition_log", exc_info=True)
            return False, None
        if not row or row[0] is None:
            return True, None
        return True, _parse_ts(row[0])

    async def _send_blocked_escalation(self, sub: Any) -> bool:
        """Mark a stalled subtask blocked and notify the Conductor + human.

        The FSM owns the canonical ``blocked`` transition; we apply it via
        :meth:`_mark_blocked_via_fsm`. Either way (applied or not) the human and
        Conductor are alerted via a chat message in the task's room.

        Returns ``True`` when the escalation took effect — the FSM transition
        applied *or* the room alert landed — so the caller can burn its
        escalate-once marker (marker-after-send, S6-F7n). ``False`` means
        nothing happened (no transition, no alert): the caller retries on the
        next patrol instead of silently never escalating again.
        """
        import asyncio

        visits = self._config.max_phase_visits
        logger.warning(
            "Subtask %s stalled: no git-HEAD change and no new transition across "
            "%d patrols — marking blocked",
            sub.subtask_id, visits,
        )
        if self._activity:
            self._activity.log(
                "SUBTASK_BLOCKED", "watchdog",
                f"Subtask {sub.subtask_id} blocked after {visits} patrols "
                f"with no mechanical progress",
            )

        fsm_applied = await asyncio.to_thread(self._mark_blocked_via_fsm, sub)

        # Resolve the task's room so the Conductor + human (both participants)
        # see the alert. Best-effort — a notify failure must not break patrol.
        room_id: str | None = None
        try:
            task = await asyncio.to_thread(self._store.get_task, sub.task_id)
            room_id = getattr(task, "room_id", None) if task else None
        except Exception:
            logger.debug("Could not resolve room for blocked subtask", exc_info=True)

        if room_id is None:
            return fsm_applied

        from thenvoi_rest.types import ChatMessageRequest

        suffix = "" if fsm_applied else " (blocked-transition could not be applied)"
        try:
            await self._rest.agent_api_messages.create_agent_chat_message(
                chat_id=room_id,
                message=ChatMessageRequest(
                    content=(
                        f"[Watchdog] Subtask {sub.subtask_id} appears stalled — no "
                        f"git-HEAD change and no new transition across {visits} "
                        f"patrols. Marking it blocked; Conductor please reassign or "
                        f"investigate.{suffix}"
                    ),
                    mentions=[],
                ),
            )
        except Exception:
            logger.exception(
                "Failed to post blocked-subtask alert for %s", sub.subtask_id,
            )
            return fsm_applied
        return True

    def _mark_blocked_via_fsm(self, sub: Any) -> bool:
        """Transition the subtask to ``blocked`` via the FSM.

        Returns ``True`` if the FSM applied the transition, ``False`` if it
        could not — no store available, or the transition raised (e.g. the
        subtask was already terminal). The chat alert fires either way.
        """
        if self._store is None:
            return False
        from codeband.state import fsm  # noqa: PLC0415 — keep watchdog import light
        try:
            fsm.transition(
                sub.subtask_id, sub.task_id, "blocked",
                caller_role="watchdog", store=self._store,
            )
            return True
        except Exception:
            logger.exception(
                "FSM blocked-transition failed for %s", sub.subtask_id,
            )
            return False

    async def _check_blocked_subtasks(self, now: datetime) -> None:
        """Escalate any ``blocked`` subtask to the owner via a Band @mention.

        Independent of how the subtask reached ``blocked`` — the watchdog's own
        stall cap, the ``cb-phase verify`` attempt cap, or the FSM review-round
        cap all land here. Each blocked subtask is announced to the owner once,
        on the first patrol whose send succeeds (escalate-once via
        ``_owner_escalated``, marker-after-send — see
        :meth:`_attempt_escalation_send`).

        The owner is resolved per blocked subtask from its task row
        (``task.owner_id``, persisted at kickoff), falling back to the optional
        ``self._owner_id`` constructor override. Fully no-ops when no store is
        wired, and skips subtasks of non-``active`` tasks entirely. When no
        owner (or room) can be resolved for a subtask it is skipped WITHOUT
        burning its escalate-once marker, so it can still escalate later if an
        owner appears. Guarded so a store read or a notify failure never breaks
        the patrol loop.
        """
        import asyncio

        if self._store is None:
            return

        try:
            subtasks = await asyncio.to_thread(self._store.list_active_subtasks)
        except Exception:
            logger.debug(
                "Watchdog could not list subtasks for owner escalation",
                exc_info=True,
            )
            return

        active_task_ids = await asyncio.to_thread(self._active_task_ids)

        for sub in subtasks:
            key = (sub.task_id, sub.subtask_id)
            if sub.state != "blocked" or key in self._owner_escalated:
                continue
            # Active-only: a blocked subtask of a superseded task is never
            # escalated — no owner or room resolution is even attempted.
            if active_task_ids is not None and sub.task_id not in active_task_ids:
                continue
            # Resolve the owner from the subtask's task row (set at kickoff),
            # falling back to the constructor override. Do this BEFORE any
            # marker burn: with no resolvable owner there is nobody to mention
            # and with no room there is nowhere to post — both skip without
            # consuming the marker, so the subtask can still escalate later.
            owner_id = await self._resolve_owner_id(sub.task_id)
            if owner_id is None:
                continue
            room_id = await self._resolve_room_id(sub.task_id)
            if room_id is None:
                continue
            # Marker-after-send: burn escalate-once only when the send lands
            # (or is permanently undeliverable — HTTP 422 mention rejection).
            if await self._attempt_escalation_send(
                self._send_owner_blocked_escalation(sub, owner_id, room_id),
                target=f"owner {owner_id}",
                room_id=room_id,
            ):
                self._owner_escalated.add(key)

    async def _resolve_owner_id(self, task_id: str) -> str | None:
        """Return the initiator id for *task_id*, or the constructor override.

        Reads ``owner_id`` off the task row (persisted at kickoff). Falls back to
        ``self._owner_id`` when the row carries no owner. Returns ``None`` when
        neither is available. Guarded so a store read failure degrades to the
        override rather than breaking the patrol.
        """
        import asyncio

        owner_id: str | None = None
        try:
            task = await asyncio.to_thread(self._store.get_task, task_id)
            owner_id = getattr(task, "owner_id", None) if task else None
        except Exception:
            logger.debug(
                "Could not resolve owner id for task %s", task_id, exc_info=True,
            )
        return owner_id or self._owner_id

    async def _resolve_room_id(self, task_id: str) -> str | None:
        """Return the room id from the task row, or ``None`` if unresolvable.

        Guarded so a store read failure degrades to ``None`` (the caller skips
        without burning any escalate-once marker) rather than breaking patrol.
        """
        import asyncio

        try:
            task = await asyncio.to_thread(self._store.get_task, task_id)
            return getattr(task, "room_id", None) if task else None
        except Exception:
            logger.debug(
                "Could not resolve room for task %s", task_id, exc_info=True,
            )
            return None

    async def _send_owner_blocked_escalation(
        self, sub: Any, owner_id: str, room_id: str,
    ) -> None:
        """@mention the owner about a blocked subtask, with its blocked reason.

        *owner_id* and *room_id* are resolved by the caller from the task row
        (owner falling back to the constructor override). The owner is a
        distinct room participant (not the Conductor whose credentials the
        watchdog borrows), so the mention is valid. The message carries the
        subtask id and the durable reason recorded on the blocked transition so
        the owner has actionable context. Send failures propagate to the
        caller, which owns the escalate-once marker (marker-after-send).
        """
        import asyncio

        reason = (
            await asyncio.to_thread(self._blocked_reason, sub.subtask_id, sub.task_id)
            or "no mechanical progress / cap reached"
        )

        from thenvoi_rest.types import (
            ChatMessageRequest,
            ChatMessageRequestMentionsItem,
        )

        handle = self._owner_handle or owner_id
        await self._rest.agent_api_messages.create_agent_chat_message(
            chat_id=room_id,
            message=ChatMessageRequest(
                content=(
                    f"@{handle} subtask {sub.subtask_id} is BLOCKED "
                    f"({reason}). It needs a human decision — reassign, "
                    f"intervene, or abandon."
                ),
                mentions=[ChatMessageRequestMentionsItem(id=owner_id)],
            ),
        )
        # Log only after the send lands — a retried transient failure would
        # otherwise record one phantom escalation per attempt.
        if self._activity:
            self._activity.log(
                "SUBTASK_BLOCKED_OWNER_ESCALATION", "watchdog",
                f"Escalated blocked subtask {sub.subtask_id} to owner {handle}",
            )

    def _blocked_reason(self, subtask_id: str, task_id: str) -> str | None:
        """Return the ``reason`` of the latest ``→ blocked`` transition, if any.

        Reads the store's SQLite file directly (read-only), mirroring
        :meth:`_latest_transition` — task-scoped, since subtask ids repeat
        across tasks. Returns ``None`` when no blocked transition is recorded
        or the reason is empty.
        """
        db_path = getattr(self._store, "db_path", None)
        if db_path is None:
            return None
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT reason FROM transition_log "
                    "WHERE task_id = ? AND subtask_id = ? AND to_state = 'blocked' "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id, subtask_id),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            logger.debug("Could not read blocked reason", exc_info=True)
            return None
        if not row or not row[0]:
            return None
        return row[0]

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
