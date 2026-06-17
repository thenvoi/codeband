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


# Band chat inline-markup mentions: ``@[[<participant-uuid>]]``. Some send
# paths deliver mentions ONLY as this markup — no structured ``mentions``
# list and no ``@DisplayName`` text — which made a re-dispatch invisible to
# the fallback scan: the mentioned agent's staleness clock never started,
# and a terminal-shaped *older* own-message then untracked it entirely
# (finding 17, both halves).
_UUID_MENTION_RE = re.compile(r"@\[\[([0-9a-f-]{36})\]\]", re.IGNORECASE)


def _mentioned_participant_ids(
    msg: Any, participant_names: dict[str, str],
) -> set[str]:
    """Return the set of participant ids mentioned in this chat message.

    Tries structured ``msg.mentions`` first (Band.ai chat messages carry
    mentions as a list of items with ``.id``); falls back to scanning the
    message content — BOTH the ``@[[uuid]]`` inline markup (matched against
    the participant id set; finding 17) and the ``@DisplayName`` form via the
    cached per-participant-set patterns from :func:`_mention_patterns`. Test
    mocks that don't set these fields are silently ignored — the isinstance
    checks reject MagicMock-typed sentinels.
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
        for uid in _UUID_MENTION_RE.findall(content):
            if uid in participant_names:
                found.add(uid)
            elif uid.lower() in participant_names:
                found.add(uid.lower())
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
# ``review_pending`` / ``review_failed`` / ``review_passed`` /
# ``acceptance_passed`` / ``needs_rebase`` are the resting states where
# dispatched work can silently die: a reviewer that never renders a verdict, a
# Verifier that never renders an acceptance verdict, a coder that never picks
# up the rework, a Mergemaster that never queues the approved PR, a rebase
# nobody starts. The mechanical signals are state-agnostic — transition recency
# applies to every state (each of these is *entered* by a transition, so the
# row exists), and the PR-activity signal applies wherever ``pr_number`` is set
# (it is, by the verify leg, for everything at/past ``review_pending``).
_PATROLLED_SUBTASK_STATES: frozenset[str] = frozenset(
    {
        "in_progress",
        "verify_pending",
        "review_pending",
        "review_failed",
        "review_passed",
        "acceptance_passed",
        "merge_pending",
        "needs_rebase",
    }
)

# Sentinel: distinguishes "transition timestamp pre-fetched as None (no rows)"
# from "not pre-fetched at all (fall back to per-subtask query)".
_TRANSITION_UNSET = object()


# Free-tier recency-probe paging. The agent message API pages OLDEST-first
# (default page_size 20, max 100) with no ``since`` parameter, so the probe
# requests the largest page and walks backward from the LAST page.
_PROBE_PAGE_SIZE = 100
# How many newest pages the probe will walk per room per patrol. A room with
# ``_MAX_PROBE_PAGES × _PROBE_PAGE_SIZE`` (500) messages inside one staleness
# window is active by definition — older history cannot change any recency
# verdict, so walking further is pure cost.
_MAX_PROBE_PAGES = 5


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
        agent_rest_clients: dict[str, Any] | None = None,
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
        # Integrity rung (Stage-3): remembered tip of each hash chain
        # (``{chain_name: (last_verified_id, last_hash)}``). Each patrol
        # verifies only rows past the remembered id (incremental) and detects
        # head REGRESSION (the remembered tip gone/rewritten — tail truncation a
        # forward walk cannot see). Escalate-once per (room, chain, kind), same
        # marker-after-send discipline as the blocked rung.
        self._chain_tips: dict[str, tuple[int, str | None]] = {}
        self._integrity_alerted: set[tuple[str, str, str]] = set()
        # Deep full-history integrity sweep (Stage-3 PR3): the longer-cadence
        # counterpart to the incremental rung above. Runs every
        # ``full_integrity_interval_patrols`` patrols, walking BOTH chains from
        # row 1 to catch the incremental rung's structural blind spot — an
        # in-place edit of an INTERIOR, already-verified row (id below the
        # remembered tip), which a forward-from-tip walk never re-reads. Its own
        # escalate-once marker set so the two rungs never suppress each other;
        # findings are attributed to the verifier role. ``db_path`` reads only —
        # never touches ``_chain_tips`` (that state belongs to the incremental
        # rung; the two are fully decoupled).
        self._full_integrity_alerted: set[tuple[str, str, str]] = set()
        self._full_integrity_patrol_count: int = 0
        # Transport-health (turn-boundary 422 pin) heal rung. Per-agent REST
        # clients keyed by agent_id — each is authenticated as THAT agent so
        # the `list_agent_messages(status="processing")` read and the
        # `mark_agent_message_processed` heal act on its own delivery row (a
        # call with the Conductor's credentials only sees/heals the Conductor's
        # deliveries). ``None`` opts out and the rung is a no-op — same shape
        # as ``state_store``. ``_pin_heal_attempts`` counts consecutive failed
        # heals per ``(agent_id, message_id)``; ``_pin_escalated`` is the
        # escalate-once marker so a server-side-rejected heal can not become
        # its own storm.
        self._agent_rest_clients: dict[str, Any] = dict(agent_rest_clients or {})
        self._pin_heal_attempts: dict[tuple[str, str], int] = {}
        self._pin_escalated: set[tuple[str, str]] = set()

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
        """List messages in a room within the ``since`` window.

        On enterprise tier uses the human API (captures text + thought +
        tool_call + tool_result + error), which bounds the read server-side
        via its ``since`` parameter. On free tier uses the agent-API inbox
        (text only, chat-only), which has no ``since`` parameter and pages
        OLDEST-first — so the recency probe must page from the END:
        requesting the default page of a long room returns only its oldest
        messages, the client-side window filters them all out, and the probe
        reads an active room as silence. :meth:`_fetch_recent_agent_messages`
        fetches the newest page(s) instead; the window filter is applied here
        on top, as before. Messages with a missing/unparseable timestamp are
        kept — the patrol loop already skips them, and dropping them here
        would silently change behavior for partial records.
        """
        if self._human_rest is not None:
            resp = await self._human_rest.human_api_messages.list_my_chat_messages(
                chat_id=room_id, since=since,
            )
            return list(resp.data or [])
        raw = await self._fetch_recent_agent_messages(room_id, since)
        messages = []
        for msg in raw:
            ts = _parse_ts(getattr(msg, "inserted_at", None))
            if ts is None or ts >= since:
                messages.append(msg)
        return messages

    async def _fetch_recent_agent_messages(
        self, room_id: str, since: datetime,
    ) -> list[Any]:
        """Fetch the NEWEST agent-API message page(s) covering ``since``.

        The agent API returns messages oldest-first with no ``since``
        parameter (server-side recency paging is a platform ask), so one
        probe request first learns ``metadata.total_pages``, then walks pages
        from the LAST one backward — continuing only while every message on
        the page is still inside the window (i.e. the window may extend into
        the previous page) and at most :data:`_MAX_PROBE_PAGES` pages deep.
        Beyond the cap the room has had ``_MAX_PROBE_PAGES × page_size``
        messages inside one staleness window — active by definition; older
        history cannot change any recency verdict.

        A response without usable paging metadata (older server, test
        doubles — the ``isinstance`` check rejects MagicMock sentinels)
        degrades to the first response's data, the pre-paging behavior.
        Returned oldest→newest across pages, matching single-page order.
        """
        fetch = self._rest.agent_api_messages.list_agent_messages
        first = await fetch(
            chat_id=room_id, status="all", page=1, page_size=_PROBE_PAGE_SIZE,
        )
        meta = getattr(first, "metadata", None)
        total_pages = getattr(meta, "total_pages", None)
        if not isinstance(total_pages, int) or total_pages <= 1:
            return list(first.data or [])

        pages: list[list[Any]] = []
        page_no = total_pages
        while page_no >= 1 and len(pages) < _MAX_PROBE_PAGES:
            if page_no == 1:
                data = list(first.data or [])  # already fetched above
            else:
                resp = await fetch(
                    chat_id=room_id, status="all",
                    page=page_no, page_size=_PROBE_PAGE_SIZE,
                )
                data = list(resp.data or [])
            pages.append(data)
            # Pages are oldest-first internally: the first parseable timestamp
            # is the page's oldest. Only when even that is inside the window
            # can the window extend into the previous page. No parseable
            # timestamp at all → stop; nothing decidable lies further back.
            oldest_ts = next(
                (
                    ts for m in data
                    if (ts := _parse_ts(getattr(m, "inserted_at", None))) is not None
                ),
                None,
            )
            if oldest_ts is None or oldest_ts < since:
                break
            page_no -= 1
        pages.reverse()
        return [msg for page in pages for msg in page]

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
                # Terminal-shaped untrack ONLY when the agent's own message is
                # the newest signal: an UNANSWERED inbound mention newer than
                # the agent's last own-message means dispatched work is
                # pending, and untracking would blind the patrol to the very
                # agent most likely to be dormant (finding 17 — this guard is
                # only as good as the mention scan above, which is why the
                # @[[uuid]] markup form must be parsed too).
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

        # Fifth rung (Stage-3): ledger integrity. Incrementally verify the
        # hash chains and escalate a chain break or a head regression. Guarded
        # so a store/read failure never breaks the patrol loop.
        await self._check_chain_integrity(now)

        # Sixth rung (Stage-3 PR3): deep full-history integrity sweep on a
        # longer cadence. Walks both chains from row 1 every N patrols to catch
        # the incremental rung's interior-old-row blind spot. Code-driven and
        # independent of any verifier LLM seat; guarded like the rung above.
        await self._check_chain_integrity_full(now)

        # Seventh rung: transport-health (turn-boundary 422 pin) heal. Reuses
        # the patrol's already-listed rooms so the rung is one extra call per
        # (agent, active room) and orthogonal to the chat-recency nudge above —
        # a pinned agent cannot read a nudge, so a separate transport-level
        # heal is required. ``rooms`` is the patrol's room list and
        # ``inactive_rooms`` the same active-only filter.
        await self._check_transport_pins(rooms, inactive_rooms, now)

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

        patrolled = [
            sub for sub in subtasks
            if (active_task_ids is None or sub.task_id in active_task_ids)
            and sub.state in _PATROLLED_SUBTASK_STATES
        ]

        # Batch-fetch latest transition timestamps for all patrolled subtasks in
        # one query (N+1 elimination). On success, fill None for subtasks with
        # no rows so every key is present → _check_one_subtask uses the batch
        # result. On failure the dict stays empty and _check_one_subtask falls
        # back to its own per-subtask _latest_transition call.
        transition_batch: dict[tuple[str, str], Any] = {}
        try:
            transition_batch = await asyncio.to_thread(
                self._store.batch_latest_transitions,
                [(sub.task_id, sub.subtask_id) for sub in patrolled],
            )
            # Pre-populate None for subtasks absent from the result (no rows yet).
            for _sub in patrolled:
                _key = (_sub.task_id, _sub.subtask_id)
                if _key not in transition_batch:
                    transition_batch[_key] = None
        except Exception:
            logger.debug("Watchdog batch transition fetch failed", exc_info=True)

        _unset = _TRANSITION_UNSET
        for sub in patrolled:
            try:
                prefetched = transition_batch.get((sub.task_id, sub.subtask_id), _unset)
                await self._check_one_subtask(sub, now, prefetched_transition_ts=prefetched)
            except Exception:
                logger.exception(
                    "Watchdog subtask-progress check failed for %s", sub.subtask_id,
                )

    async def _check_one_subtask(
        self, sub: Any, now: datetime, *, prefetched_transition_ts: Any = _TRANSITION_UNSET,
    ) -> None:
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

        # SHA-drift rung (finding 28): for merge_pending subtasks, detect when
        # the branch HEAD has moved past the approved SHA.  The approved SHA is
        # the commit the approver signed off on; a drift means the grant is stale
        # and a fresh rebase → re-review → re-approval cycle is needed.  Route
        # via the same mergemaster FSM edge used by the merge leg so the rebase-
        # round cap and all FSM invariants are preserved.  Fires only when both
        # merge_approved_sha and git_head are known; skipped on unresolvable HEAD
        # to avoid false trips on transient probe failures.
        approved_sha: str | None = getattr(sub, "merge_approved_sha", None)
        if (
            sub.state == "merge_pending"
            and approved_sha
            and git_head is not None
            and git_head != approved_sha
        ):
            await self._on_merge_pending_sha_drift(sub, git_head)
            return

        # Backstop: approved + merge_pending + HEAD matches the approved SHA,
        # but dispatch stalled. Re-nudge Mergemaster; do NOT let this PR get
        # escalated to blocked while a valid grant is on record.
        approved_sha = getattr(sub, "merge_approved_sha", None)
        if (
            sub.state == "merge_pending"
            and approved_sha is not None
            and git_head is not None
            and git_head == approved_sha
            and self._config.merge_approval_backstop_max_renudges > 0
        ):
            if await self._maybe_backstop_renudge(sub, approved_sha, now):
                health = self._subtask_state.get((sub.task_id, sub.subtask_id))
                if health is not None:
                    health.patrol_visits_without_progress = 0
                return
            # cap hit → fall through to the stall counter so a genuinely hung
            # Mergemaster still surfaces to the owner as blocked.

        # Acceptance-advance rung: acceptance_passed + dispatch stalled.
        # Re-nudge Mergemaster so a verified-and-accepted PR is not escalated
        # to blocked while waiting for the merge step to be triggered.
        if (
            sub.state == "acceptance_passed"
            and self._config.acceptance_advance_max_renudges > 0
        ):
            if await self._maybe_acceptance_advance_renudge(sub, now):
                health = self._subtask_state.get((sub.task_id, sub.subtask_id))
                if health is not None:
                    health.patrol_visits_without_progress = 0
                return
            # cap hit → fall through to the stall counter so a genuinely hung
            # Mergemaster still surfaces to the owner as blocked.

        pr_ts = None
        if sub.pr_number is not None:
            reads_attempted += 1
            pr_ts = await asyncio.to_thread(self._pr_updated_at, sub.pr_number)
            if pr_ts is None:
                reads_failed += 1
        reads_attempted += 1
        if prefetched_transition_ts is not _TRANSITION_UNSET:
            transition_ok = True
            transition_ts = prefetched_transition_ts
        else:
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

    async def _on_merge_pending_sha_drift(self, sub: Any, live_sha: str) -> None:
        """Route a merge_pending subtask to needs_rebase after its head drifted.

        Uses the Mergemaster's FSM edge (``merge_pending → needs_rebase``) so
        the rebase-round cap is enforced and all FSM invariants are preserved.
        Logs a room alert so the Conductor and Coder are informed immediately.
        Best-effort: a failed FSM transition or post is logged but does not
        break patrol.
        """
        import asyncio
        from codeband.state import fsm

        approved_sha: str | None = getattr(sub, "merge_approved_sha", None)
        reason = (
            f"[watchdog] head SHA moved since grant "
            f"({approved_sha[:8] if approved_sha else '?'} → {live_sha[:8]})"
        )
        logger.warning(
            "Subtask %s: merge_pending head drifted from approved SHA %s to %s "
            "— routing to needs_rebase",
            sub.subtask_id, approved_sha, live_sha,
        )
        try:
            await asyncio.to_thread(
                fsm.transition,
                sub.subtask_id, sub.task_id, "needs_rebase",
                caller_role="mergemaster", reason=reason, store=self._store,
            )
        except Exception:
            logger.exception(
                "Watchdog: needs_rebase transition failed for %s after SHA drift",
                sub.subtask_id,
            )
            return

        if self._activity:
            self._activity.log(
                "SUBTASK_SHA_DRIFT", "watchdog",
                f"Subtask {sub.subtask_id} routed to needs_rebase: "
                f"head moved from {approved_sha} to {live_sha}",
            )

        room_id = await self._resolve_room_id(sub.task_id)
        if room_id is None:
            return
        from thenvoi_rest.types import ChatMessageRequest

        try:
            await self._rest.agent_api_messages.create_agent_chat_message(
                chat_id=room_id,
                message=ChatMessageRequest(
                    content=(
                        f"[Watchdog] Subtask {sub.subtask_id}: branch HEAD moved "
                        f"({approved_sha[:8] if approved_sha else '?'} → {live_sha[:8]}) "
                        f"while merge_pending — grant is stale, subtask sent to "
                        f"needs_rebase. Coder: rebase, re-verify, re-earn verdicts, "
                        f"then request re-approval."
                    ),
                    mentions=[],
                ),
            )
        except Exception:
            logger.exception(
                "Watchdog: failed to post SHA-drift alert for %s", sub.subtask_id,
            )

    async def _maybe_backstop_renudge(
        self, sub: Any, approved_sha: str, now: datetime,
    ) -> bool:
        """Return True when the backstop owns the patrol; False on cap-hit.

        True = caller early-returns (rung is active, do not advance the stall
        counter).  False = cap exhausted, caller falls through to the normal
        stall path so a genuinely hung Mergemaster still surfaces as blocked.

        Reads approval_grant + merge_backstop_nudge rows from the audit log,
        filters to the current ``approved_sha``, and decides whether to fire.
        Applies the staleness window against the anchor timestamp (last nudge,
        or the original grant if no nudge yet). On fire: sends, flips
        swarm-status active, and appends a durable marker (marker-after-send
        discipline: only appended on successful send).
        """
        import asyncio

        if self._store is None:
            return False

        rows = await asyncio.to_thread(
            self._store.latest_audit_events,
            task_id=sub.task_id, subtask_id=sub.subtask_id,
            event_types=("approval_grant", "merge_backstop_nudge"),
        )
        grant_ts: datetime | None = None
        last_nudge_ts: datetime | None = None
        renudges_for_sha = 0
        for event_type, ts_str, payload in rows:
            if (payload or {}).get("approved_sha") != approved_sha:
                continue
            ts = _parse_ts(ts_str)
            if event_type == "approval_grant" and grant_ts is None:
                grant_ts = ts
            elif event_type == "merge_backstop_nudge":
                renudges_for_sha += 1
                if last_nudge_ts is None:
                    last_nudge_ts = ts
        if grant_ts is None:
            return False  # no grant in audit log — stall path owns this
        if renudges_for_sha >= self._config.merge_approval_backstop_max_renudges:
            return False  # cap hit — release patrol to stall path
        anchor_ts = last_nudge_ts or grant_ts
        window = timedelta(seconds=self._config.merge_approval_backstop_seconds)
        if (now - anchor_ts) < window:
            return True  # within window — own patrol, don't fire yet
        fired = await self._send_merge_backstop_renudge(sub, approved_sha)
        if not fired:
            return True  # transient send failure — retry next patrol
        await self._flip_swarm_status_active(sub.task_id)
        await asyncio.to_thread(
            self._store.append_audit_event,
            "merge_backstop_nudge",
            task_id=sub.task_id,
            subtask_id=sub.subtask_id,
            payload={"pr_number": sub.pr_number, "approved_sha": approved_sha},
        )
        return True

    async def _send_merge_backstop_renudge(
        self, sub: Any, approved_sha: str,
    ) -> bool:
        """@mention the Mergemaster asking it to run ``cb-phase merge``.

        Resolves the Mergemaster agent id from ``_role_map``, then the room
        from the store's task row.  Returns True on successful send, False on
        any resolution failure or send exception (logged at WARNING; does not
        raise so patrol continues).
        """
        from thenvoi_rest.types import ChatMessageRequest, ChatMessageRequestMentionsItem

        mm_id: str | None = next(
            (aid for aid, role in self._role_map.items() if role == "mergemaster"),
            None,
        )
        if mm_id is None:
            logger.warning(
                "Backstop: no Mergemaster in role_map for subtask %s — cannot renudge",
                sub.subtask_id,
            )
            return False

        room_id = await self._resolve_room_id(sub.task_id)
        if room_id is None:
            logger.warning(
                "Backstop: could not resolve room for task %s — cannot renudge %s",
                sub.task_id, sub.subtask_id,
            )
            return False

        short_sha = approved_sha[:8] if approved_sha else "?"
        pr_ref = f"PR #{sub.pr_number}" if sub.pr_number is not None else "the PR"
        content = (
            f"[Watchdog backstop] Subtask {sub.subtask_id}: {pr_ref} has a "
            f"recorded human approval at {short_sha} but merge dispatch appears "
            f"stalled. Please run `cb-phase merge` now."
        )
        try:
            await self._rest.agent_api_messages.create_agent_chat_message(
                chat_id=room_id,
                message=ChatMessageRequest(
                    content=content,
                    mentions=[ChatMessageRequestMentionsItem(id=mm_id)],
                ),
            )
        except Exception:
            logger.warning(
                "Backstop: failed to send merge renudge for subtask %s",
                sub.subtask_id, exc_info=True,
            )
            return False
        return True

    async def _maybe_acceptance_advance_renudge(
        self, sub: Any, now: datetime,
    ) -> bool:
        """Return True when the acceptance-advance rung owns the patrol; False on cap-hit.

        True = caller early-returns (rung is active, do not advance the stall
        counter).  False = cap exhausted, caller falls through to the normal
        stall path so a genuinely hung Mergemaster still surfaces as blocked.

        Reads ``acceptance_advance_nudge`` markers from the audit log to track
        how many renudges have fired since the subtask entered
        ``acceptance_passed`` (markers older than the entry timestamp are from a
        prior acceptance_passed visit and do not count). Anchor is
        ``sub.updated_at`` — the FSM and the subtask row are written in the
        same transaction, so this equals the ``acceptance_passed`` transition
        timestamp. On fire: sends, flips swarm-status active, and appends a
        durable marker (marker-after-send discipline: only appended on
        successful send).
        """
        import asyncio

        if self._store is None:
            return False

        entry_ts = _parse_ts(sub.updated_at)
        if entry_ts is None:
            return False

        rows = await asyncio.to_thread(
            self._store.latest_audit_events,
            task_id=sub.task_id, subtask_id=sub.subtask_id,
            event_types=("acceptance_advance_nudge",),
        )
        # Only count markers written after the subtask entered acceptance_passed;
        # older markers are from a prior visit and must not consume the current cap.
        last_nudge_ts: datetime | None = None
        nudges_since_entry = 0
        for _, ts_str, _ in rows:
            ts = _parse_ts(ts_str)
            if ts is None or ts < entry_ts:
                continue
            nudges_since_entry += 1
            if last_nudge_ts is None:
                last_nudge_ts = ts  # rows are newest-first

        if nudges_since_entry >= self._config.acceptance_advance_max_renudges:
            return False  # cap hit — release patrol to stall path

        anchor_ts = last_nudge_ts or entry_ts
        window = timedelta(seconds=self._config.acceptance_advance_backstop_seconds)
        if (now - anchor_ts) < window:
            return True  # within window — own patrol, don't fire yet

        fired = await self._send_acceptance_advance_renudge(sub)
        if not fired:
            return True  # transient send failure — retry next patrol
        await self._flip_swarm_status_active(sub.task_id)
        await asyncio.to_thread(
            self._store.append_audit_event,
            "acceptance_advance_nudge",
            task_id=sub.task_id,
            subtask_id=sub.subtask_id,
            payload={"pr_number": sub.pr_number},
        )
        return True

    async def _send_acceptance_advance_renudge(self, sub: Any) -> bool:
        """@mention the Mergemaster asking it to run ``cb-phase merge``.

        Resolves the Mergemaster agent id from ``_role_map``, then the room
        from the store's task row.  Returns True on successful send, False on
        any resolution failure or send exception (logged at WARNING; does not
        raise so patrol continues).
        """
        from thenvoi_rest.types import ChatMessageRequest, ChatMessageRequestMentionsItem

        mm_id: str | None = next(
            (aid for aid, role in self._role_map.items() if role == "mergemaster"),
            None,
        )
        if mm_id is None:
            logger.warning(
                "AcceptanceAdvance: no Mergemaster in role_map for subtask %s — cannot renudge",
                sub.subtask_id,
            )
            return False

        room_id = await self._resolve_room_id(sub.task_id)
        if room_id is None:
            logger.warning(
                "AcceptanceAdvance: could not resolve room for task %s — cannot renudge %s",
                sub.task_id, sub.subtask_id,
            )
            return False

        pr_ref = f"PR #{sub.pr_number}" if sub.pr_number is not None else "the PR"
        content = (
            f"[Watchdog] Subtask {sub.subtask_id}: {pr_ref} has passed acceptance "
            f"but merge dispatch appears stalled. Please run `cb-phase merge` now."
        )
        try:
            await self._rest.agent_api_messages.create_agent_chat_message(
                chat_id=room_id,
                message=ChatMessageRequest(
                    content=content,
                    mentions=[ChatMessageRequestMentionsItem(id=mm_id)],
                ),
            )
        except Exception:
            logger.warning(
                "AcceptanceAdvance: failed to send renudge for subtask %s",
                sub.subtask_id, exc_info=True,
            )
            return False
        return True

    async def _flip_swarm_status_active(self, task_id: str) -> None:
        """Write a ``swarm status active task <task_id>`` memory envelope.

        **GLOBAL EFFECT — not per-subtask.** This write makes the recorded
        envelope the *latest* swarm-status entry, which un-suppresses ALL
        patrol rungs for ALL subtasks in ALL active rooms. The Conductor only
        writes ``waiting_human_approval`` when ALL work is blocked on approval;
        once any one grant lands that precondition is false, so ``active`` is
        the semantically-correct latest envelope. The Conductor re-writes
        ``waiting_human_approval`` on its next active turn if the condition
        still holds.

        Swallows all exceptions — this is a hint to the patrol-gate, not
        gated state; a write failure means the gate reads the old envelope for
        one more cycle, which is safe.
        """
        content = f"swarm status active task {task_id}"
        try:
            if self._memory_store is not None:
                await self._memory_store.store(
                    content=content,
                    system="working",
                    type="episodic",
                    segment="agent",
                    scope="organization",
                )
            else:
                from thenvoi_rest.types import MemoryCreateRequest

                await self._rest.agent_api_memories.create_agent_memory(
                    memory=MemoryCreateRequest(
                        content=content,
                        system="working",
                        type="episodic",
                        segment="agent",
                        scope="organization",
                    ),
                )
        except Exception:
            logger.debug(
                "Backstop: could not flip swarm-status to active for task %s",
                task_id, exc_info=True,
            )

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

        A discriminator gate runs first: when the FSM-expected actor for the
        subtask's current state has a transport pin in the task's room, the
        block is DEFERRED (returns ``False`` without touching FSM state or
        posting). A transport condition must never mutate gated state — the
        heal rung clears the pin and the next patrol re-evaluates.
        """
        import asyncio

        if await self._stall_block_deferred_for_pin(sub):
            return False

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

        from thenvoi_rest.types import ChatMessageRequest, ChatMessageRequestMentionsItem

        # Determine who to mention in the stall alert: the assigned worker
        # when known, otherwise the task owner.  Never pass None to
        # ChatMessageRequestMentionsItem — that produces an HTTP 422
        # (mentioned_participant_not_in_room) from the server.
        mention_id: str | None = getattr(sub, "assigned_worker", None)
        if mention_id is None:
            mention_id = await self._resolve_owner_id(sub.task_id)
        if mention_id is None:
            logger.debug(
                "Stall-blocked alert for subtask %s: no mention target "
                "(assigned_worker=None, owner unresolvable) — posting without mention",
                sub.subtask_id,
            )
        mentions = (
            [ChatMessageRequestMentionsItem(id=mention_id)] if mention_id else []
        )

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
                    mentions=mentions,
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

    async def _stall_block_deferred_for_pin(self, sub: Any) -> bool:
        """Return ``True`` if the stall→blocked transition should be DEFERRED.

        The stall rung counts an absent transition + an absent git-HEAD change
        across ``max_phase_visits`` patrols as a substantive stall. A transport
        pin on the FSM-expected actor presents the same symptoms (the agent's
        delivery queue is wedged at the head, so no FSM advance is possible
        until the pin clears). Without this gate, a pinned actor was getting
        marked ``blocked`` — and ``blocked`` poisons recovery because the only
        legal exit needs a Conductor resume (not auto-triggered).

        Algorithm:

        1. Resolve the set of FSM-expected roles for ``sub.state`` via
           :func:`state_to_roles`. Terminal states → no expected actor →
           proceed.
        2. Take every agent of those roles that has a REST client (i.e. is
           reachable from the watchdog).
        3. For each, probe the agent's own delivery queue in the task's room
           with the SAME pin criteria as the heal rung (processing record OR
           pending head with ``inserted_at`` older than
           ``transport_pin_threshold_seconds``).
        4. Any candidate pinned — or any probe that raises — DEFERS the
           block. Probe uncertainty fails toward defer because a false block
           poisons recovery; a transient transport error just delays a real
           block one patrol.

        ``review_passed`` returns ``{verifier, mergemaster}`` and both are
        probed: the v1 approximation can over-defer when a subtask is actually
        waiting on the verifier while the mergemaster is independently pinned
        on another room, but the cost is a bounded delay of the block, never a
        wrong block. ``required_verdicts`` is intentionally not consulted
        here.

        No-ops to ``False`` (proceed) when:

        * the transport-heal rung is disabled — the operator opted out of the
          composition, so the discriminator opts out too;
        * no per-agent REST clients are wired — nothing to probe with;
        * the task's room cannot be resolved — the pin lookup is per-room and
          we cannot tell, fall through to today's behavior;
        * no candidate agent of the expected role has a REST client — also
          nothing to probe with.

        Only ``self._role_map`` membership + room scoping are used (not
        ``assigned_agent_id``); probing a non-participant returns an empty
        mailbox for the room, so non-participants cannot cause a false defer.
        """
        if not self._config.transport_heal_enabled:
            return False
        if not self._agent_rest_clients:
            return False

        from codeband.state.fsm import state_to_roles  # noqa: PLC0415

        state = getattr(sub, "state", None)
        if not isinstance(state, str):
            return False
        expected_roles = state_to_roles(state)
        if not expected_roles:
            return False
        candidate_agents = [
            aid for aid, role in self._role_map.items()
            if role in expected_roles and aid in self._agent_rest_clients
        ]
        if not candidate_agents:
            return False

        room_id = await self._resolve_room_id(sub.task_id)
        if room_id is None:
            return False

        threshold = timedelta(seconds=self._config.transport_pin_threshold_seconds)
        now = datetime.now(UTC)
        for aid in candidate_agents:
            client = self._agent_rest_clients[aid]
            try:
                pinned = await self._detect_room_pin(
                    aid, room_id, client, threshold, now,
                )
            except Exception:
                logger.info(
                    "Stall->blocked deferred for subtask %s: probe of agent "
                    "%s (role %s) in room %s raised — treating as transport-"
                    "pinned (fail toward defer).",
                    sub.subtask_id, aid, self._role_map.get(aid), room_id,
                    exc_info=True,
                )
                if self._activity:
                    self._activity.log(
                        "AGENT_PIN_DEFER", "watchdog",
                        f"Stall→blocked deferred for subtask {sub.subtask_id}: "
                        f"probe of agent {aid} raised (fail toward defer)",
                        subtask_id=sub.subtask_id,
                        expected_role=self._role_map.get(aid),
                        pinned_agent=aid,
                    )
                return True
            if pinned:
                logger.info(
                    "Stall->blocked deferred for subtask %s: expected actor "
                    "%s (role %s) transport-pinned in room %s; leaving to "
                    "transport-heal rung.",
                    sub.subtask_id, aid, self._role_map.get(aid), room_id,
                )
                if self._activity:
                    self._activity.log(
                        "AGENT_PIN_DEFER", "watchdog",
                        f"Stall→blocked deferred for subtask {sub.subtask_id}: "
                        f"agent {aid} transport-pinned",
                        subtask_id=sub.subtask_id,
                        expected_role=self._role_map.get(aid),
                        pinned_agent=aid,
                    )
                return True
        return False

    async def _detect_room_pin(
        self,
        agent_id: str,
        room_id: str,
        client: Any,
        threshold: timedelta,
        now: datetime,
    ) -> bool:
        """Return ``True`` if ``agent_id`` has a transport pin in ``room_id``.

        Pin criteria mirror :meth:`_check_one_agent_room_pins` exactly so the
        heal rung and the stall discriminator share one definition:

        * any ``processing`` record whose ``inserted_at`` is older than
          ``threshold`` (crash-during-turn class), OR
        * the ``pending`` HEAD (``data[0]``) whose ``inserted_at`` is older
          than ``threshold`` (post-turn 422 class) — only the head pins the
          cursor.

        Probe errors propagate so the caller can decide policy (the
        discriminator treats them as pinned to fail toward defer); the heal
        rung's own probes silence errors locally because its policy is
        different (skip-and-retry).
        """
        resp = await client.agent_api_messages.list_agent_messages(
            chat_id=room_id, status="processing", page_size=100,
        )
        for msg in list(getattr(resp, "data", None) or []):
            if not isinstance(getattr(msg, "id", None), str):
                continue
            inserted = _parse_ts(getattr(msg, "inserted_at", None))
            if inserted is not None and (now - inserted) > threshold:
                return True

        pending_resp = await client.agent_api_messages.list_agent_messages(
            chat_id=room_id, status="pending", page=1, page_size=100,
        )
        pending_data = list(getattr(pending_resp, "data", None) or [])
        if not pending_data:
            return False
        head = pending_data[0]
        if not isinstance(getattr(head, "id", None), str):
            return False
        inserted = _parse_ts(getattr(head, "inserted_at", None))
        return inserted is not None and (now - inserted) > threshold

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

        ``_owner_escalated`` is pruned on each call: markers for subtasks that
        are no longer in ``blocked`` state (e.g. resumed to ``in_progress`` via
        ``cb-phase resume``) are removed so the subtask can escalate again if
        it re-blocks.  Sibling marker ``_integrity_alerted`` is keyed by
        ``(room, chain, kind)`` — not by subtask — so it is intentionally NOT
        cleared here.
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

        # Prune stale escalation markers: remove keys for subtasks that are no
        # longer blocked (resumed → in_progress, or otherwise left blocked) so
        # they can re-escalate if they re-block.  Intentionally accepts a rare
        # duplicate escalation over a silent-forever blocked subtask.
        currently_blocked_keys = {
            (s.task_id, s.subtask_id) for s in subtasks if s.state == "blocked"
        }
        self._owner_escalated &= currently_blocked_keys

        # Fetch task rows once to derive both active_task_ids and the
        # room/owner lookup, avoiding per-subtask get_task calls (N+1
        # elimination). _task_rows returns None on failure → degrade gracefully.
        task_rows = await asyncio.to_thread(self._task_rows)
        if task_rows is not None:
            active_task_ids: set[str] | None = {
                tid for tid, _, status, _ in task_rows if status == "active"
            }
            # task_id → (room_id, owner_id)
            task_info: dict[str, tuple[str | None, str | None]] = {
                tid: (room_id_r, owner_id_r)
                for tid, room_id_r, _, owner_id_r in task_rows
            }
        else:
            active_task_ids = None
            task_info = {}

        for sub in subtasks:
            key = (sub.task_id, sub.subtask_id)
            if sub.state != "blocked" or key in self._owner_escalated:
                continue
            # Active-only: a blocked subtask of a superseded task is never
            # escalated — no owner or room resolution is even attempted.
            if active_task_ids is not None and sub.task_id not in active_task_ids:
                continue
            # Resolve owner and room from pre-fetched task info (avoids per-subtask
            # get_task calls). Fall back to constructor override for owner.
            info = task_info.get(sub.task_id)
            owner_id = (info[1] if info else None) or self._owner_id
            if owner_id is None:
                continue
            room_id = info[0] if info else None
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

    # ── ledger integrity rung (Stage-3) ────────────────────────────────────

    async def _check_chain_integrity(self, now: datetime) -> None:
        """Verify the hash chains incrementally and escalate any tamper signal.

        Two alert conditions, both rung-style owner escalations with the
        marker discipline of the other rungs:

        * **chain break** — a row whose recomputed hash disagrees with its
          stored ``row_hash`` (an in-place edit of a business column);
        * **head regression** — the previously-verified tip is gone or
          rewritten (tail truncation), which a forward chain walk by
          construction cannot see, so it is checked explicitly against the
          remembered ``(id, hash)``.

        Incremental by design: each patrol verifies only rows past the
        remembered tip (``self._chain_tips``). Full-history verification stays
        the manual ``cb verify-log``'s job. No-ops without a store; guarded so
        a read failure never breaks the patrol loop.
        """
        import asyncio

        if self._store is None or getattr(self._store, "db_path", None) is None:
            return

        try:
            problems = await asyncio.to_thread(self._verify_chains_incremental)
        except Exception:
            logger.debug("Watchdog chain-integrity verify failed", exc_info=True)
            return
        if not problems:
            return

        await self._escalate_integrity_problems(
            problems, marker_set=self._integrity_alerted, source="watchdog",
        )

    async def _escalate_integrity_problems(
        self,
        problems: list[tuple[str, str, str]],
        *,
        marker_set: set[tuple[str, str, str]],
        source: str,
    ) -> None:
        """Owner-escalate each ledger-integrity problem once per (room, chain, kind).

        Shared by the incremental and full-history rungs. An integrity break is
        a global event — it escalates into every ACTIVE task's room to its
        owner, a single time per (room, chain, kind), with the same
        marker-after-send discipline as the blocked rung. ``marker_set`` is the
        calling rung's own escalate-once set (the two rungs keep SEPARATE sets so
        a finding from one never suppresses the other); ``source`` attributes the
        alert (``"watchdog"`` for the incremental rung, ``"verifier"`` for the
        full-history sweep).
        """
        import asyncio

        task_rows = await asyncio.to_thread(self._task_rows)
        active = [
            (room, owner) for _, room, status, owner in (task_rows or [])
            if status == "active"
        ]
        for chain_name, kind, detail in problems:
            for room_id, owner in active:
                owner_id = owner or self._owner_id
                if owner_id is None or room_id is None:
                    continue
                mkey = (room_id, chain_name, kind)
                if mkey in marker_set:
                    continue
                if await self._attempt_escalation_send(
                    self._send_integrity_alert(
                        room_id, owner_id, chain_name, kind, detail,
                        source=source,
                    ),
                    target=f"owner {owner_id}",
                    room_id=room_id,
                ):
                    marker_set.add(mkey)

    def _verify_chains_incremental(self) -> list[tuple[str, str, str]]:
        """Incrementally verify both chains; return ``(chain, kind, detail)`` problems.

        Reads the store's SQLite file directly (read-only), like the watchdog's
        other DB readers. Advances ``self._chain_tips`` past every verified row
        so the next patrol only re-checks new rows. On a head regression the
        tip is re-baselined to the current physical tip so forward verification
        resumes (the alert has already fired; the escalate-once marker stops a
        repeat).
        """
        from codeband.state.store import (
            AUDIT_HASH_COLS,
            TRANSITION_HASH_COLS,
            verify_chain,
        )

        problems: list[tuple[str, str, str]] = []
        conn = sqlite3.connect(self._store.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            for table, cols in (
                ("transition_log", TRANSITION_HASH_COLS),
                ("audit_log", AUDIT_HASH_COLS),
            ):
                last_id, last_hash = self._chain_tips.get(table, (0, None))

                # Head regression: the remembered tip row is gone or its hash
                # was rewritten, or the physical max id fell below it
                # (truncation). A pure forward walk from last_id cannot see
                # this, so check it before walking.
                if last_id > 0:
                    tip = conn.execute(
                        f"SELECT row_hash FROM {table} WHERE id = ?",  # noqa: S608 — fixed literal
                        (last_id,),
                    ).fetchone()
                    max_row = conn.execute(
                        f"SELECT MAX(id) AS m FROM {table}"  # noqa: S608 — fixed literal
                    ).fetchone()
                    cur_max = max_row["m"] if max_row is not None else None
                    if (
                        tip is None
                        or tip["row_hash"] != last_hash
                        or (cur_max is not None and cur_max < last_id)
                    ):
                        problems.append((
                            table,
                            "head_regression",
                            f"remembered head id={last_id} is gone or rewritten "
                            f"(current max id={cur_max}) — possible tail truncation",
                        ))
                        # Re-baseline to the current physical tip so forward
                        # verification resumes next cycle.
                        cur = conn.execute(
                            f"SELECT id, row_hash FROM {table} "  # noqa: S608 — fixed literal
                            "ORDER BY id DESC LIMIT 1"
                        ).fetchone()
                        self._chain_tips[table] = (
                            (cur["id"], cur["row_hash"]) if cur is not None
                            else (0, None)
                        )
                        continue

                result = verify_chain(
                    conn, table, cols, after_id=last_id, prev_hash=last_hash,
                )
                if not result.ok:
                    problems.append((
                        table,
                        "chain_break",
                        f"first broken row id={result.broken_id}: expected "
                        f"row_hash {result.expected_hash}, stored "
                        f"{result.actual_hash}",
                    ))
                # Advance the tip to the last good row (the break stops the
                # walk, so head_id/head_hash is the last verified row before it).
                self._chain_tips[table] = (result.head_id, result.head_hash)
        finally:
            conn.close()
        return problems

    async def _check_chain_integrity_full(self, now: datetime) -> None:
        """Deep full-history integrity sweep on a longer cadence (verifier role).

        Co-located with the incremental rung (:meth:`_check_chain_integrity`)
        but distinct in three ways:

        * **Cadence** — runs every ``full_integrity_interval_patrols`` patrols,
          not every patrol, because re-hashing the whole ledger is more work.
        * **Coverage** — walks both chains from row 1, so it catches the
          incremental rung's structural blind spot: an in-place edit of an
          INTERIOR, already-verified row (id below the remembered tip), which a
          forward-from-tip walk never re-reads and the head-regression check
          (which inspects only the remembered tip) cannot see either.
        * **Attribution** — findings are attributed to the verifier role; the
          deep evidence-integrity sweep is conceptually the verifier's job. It
          is code-driven and runs whether or not a verifier LLM seat is
          allocated — integrity is a safety sweep, not an LLM behavior.

        Same owner-escalation + escalate-once (per room, chain, kind) discipline
        as the incremental rung, with its own marker set so the two rungs never
        suppress each other. No-ops without a store; guarded so a read failure
        never breaks the patrol loop.
        """
        import asyncio

        if self._store is None or getattr(self._store, "db_path", None) is None:
            return

        # Longer cadence: only sweep on every Nth patrol. Counting here (rather
        # than off the main patrol counter) keeps the rung self-contained and
        # the cadence independent of the other rungs.
        self._full_integrity_patrol_count += 1
        interval = self._config.full_integrity_interval_patrols
        if self._full_integrity_patrol_count % interval != 0:
            return

        try:
            problems = await asyncio.to_thread(self._verify_chains_full)
        except Exception:
            logger.debug(
                "Watchdog full-history integrity verify failed", exc_info=True,
            )
            return
        if not problems:
            return

        await self._escalate_integrity_problems(
            problems, marker_set=self._full_integrity_alerted, source="verifier",
        )

    def _verify_chains_full(self) -> list[tuple[str, str, str]]:
        """Verify both hash chains from row 1 (genesis); return ``(chain, kind, detail)``.

        The deep counterpart to :meth:`_verify_chains_incremental`: it re-reads
        and re-hashes EVERY row rather than only rows past the remembered tip,
        reusing ``cb verify-log``'s whole-chain logic (``verify_chain`` with the
        default ``after_id=0``). This is the only rung that catches an in-place
        edit of an interior, already-verified row. Read-only and stateless —
        deliberately never touches ``self._chain_tips`` (that belongs to the
        incremental rung; the two rungs stay fully decoupled).
        """
        from codeband.state.store import (
            AUDIT_HASH_COLS,
            TRANSITION_HASH_COLS,
            verify_chain,
        )

        problems: list[tuple[str, str, str]] = []
        conn = sqlite3.connect(self._store.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            for table, cols in (
                ("transition_log", TRANSITION_HASH_COLS),
                ("audit_log", AUDIT_HASH_COLS),
            ):
                result = verify_chain(conn, table, cols)
                if not result.ok:
                    problems.append((
                        table,
                        "chain_break",
                        f"first broken row id={result.broken_id}: expected "
                        f"row_hash {result.expected_hash}, stored "
                        f"{result.actual_hash}",
                    ))
        finally:
            conn.close()
        return problems

    async def _send_integrity_alert(
        self, room_id: str, owner_id: str, chain_name: str, kind: str,
        detail: str, *, source: str = "watchdog",
    ) -> None:
        """@mention the owner about a ledger integrity break.

        Sent with the watchdog's (Conductor's) credentials, like the other
        rungs. The owner is a distinct room participant, so the mention is
        valid. ``source`` names which rung found the break — ``"watchdog"`` for
        the per-patrol incremental check, ``"verifier"`` for the deep
        full-history sweep — and is carried into both the chat text and the
        activity-log actor so a full-history finding is attributed to the
        verifier role. Send failures propagate to the caller, which owns the
        escalate-once marker (marker-after-send).
        """
        from thenvoi_rest.types import (
            ChatMessageRequest,
            ChatMessageRequestMentionsItem,
        )

        label = (
            "chain break (a row was edited in place)"
            if kind == "chain_break"
            else "head regression (rows may have been truncated)"
        )
        sweep = (
            "verifier full-history integrity sweep"
            if source == "verifier"
            else "watchdog integrity check"
        )
        handle = self._owner_handle or owner_id
        await self._rest.agent_api_messages.create_agent_chat_message(
            chat_id=room_id,
            message=ChatMessageRequest(
                content=(
                    f"@{handle} LEDGER INTEGRITY ALERT ({sweep}) — "
                    f"{chain_name}: {label}. "
                    f"{detail}. Run `cb verify-log` and investigate: the state "
                    "ledger may have been modified out of band."
                ),
                mentions=[ChatMessageRequestMentionsItem(id=owner_id)],
            ),
        )
        if self._activity:
            self._activity.log(
                "LEDGER_INTEGRITY_ALERT", source,
                f"{chain_name} {kind}: {detail}",
            )

    # ── transport-health (turn-boundary 422 pin) heal rung ─────────────────

    async def _check_transport_pins(
        self,
        rooms: list[Any],
        inactive_rooms: set[str],
        now: datetime,
    ) -> None:
        """Detect and HEAL turn-boundary 422 cursor pins.

        When the agent finishes its turn, the SDK marks the inbound delivery
        ``processed``. If that POST returns 422 (no active processing attempt),
        the delivery falls back into the ``pending`` bucket head-of-queue and
        the agent's cursor is pinned — the agent will not pull the next message
        because the transport layer sees the old delivery as still unprocessed.
        A chat nudge cannot wake a pinned agent (its cursor is wedged below
        the new message).

        This rung probes BOTH buckets per (agent, room):
        - ``pending`` (post-turn 422 class): 2-step re-open (``/processing``)
          then complete (``/processed``), verified by re-listing the head.
          Cursor advance confirmed by re-list = success; same head = failed
          attempt; verify error = unknown (retry next patrol).
        - ``processing`` (crash-during-turn class): 1-step ``/processed``
          re-assert. A 422 here means the delivery is already processed;
          treated as a benign idempotent no-op.

        Only deliveries whose ``inserted_at`` is older than
        ``transport_pin_threshold_seconds`` are considered pins — this T is
        LONGER than any plausible real turn (default 1800s), so mid-turn
        deliveries are never touched.

        Per-agent REST clients are required because both the probes and the
        heals act on the calling agent's own delivery row — the Conductor's
        credentials only see/heal the Conductor's deliveries. When
        ``agent_rest_clients`` is empty (or the kill switch is off), the rung
        is a no-op and the existing nudge / escalation paths are unaffected.
        """
        if not self._config.transport_heal_enabled:
            return
        if not self._agent_rest_clients:
            return

        threshold = timedelta(seconds=self._config.transport_pin_threshold_seconds)
        room_ids = [
            r.id for r in rooms if getattr(r, "id", None) not in inactive_rooms
        ]
        for agent_id, client in self._agent_rest_clients.items():
            if agent_id == self._agent_id:
                # The watchdog already runs with this client's credentials via
                # ``self._rest``; skipping prevents a redundant double-check
                # on the same delivery rows.
                continue
            for room_id in room_ids:
                await self._check_one_agent_room_pins(
                    agent_id, room_id, client, threshold, now,
                )

    async def _check_one_agent_room_pins(
        self,
        agent_id: str,
        room_id: str,
        client: Any,
        threshold: timedelta,
        now: datetime,
    ) -> None:
        """Probe one (agent, room) for pinned deliveries and heal each."""
        from thenvoi_rest.core.api_error import ApiError

        try:
            resp = await client.agent_api_messages.list_agent_messages(
                chat_id=room_id, status="processing", page_size=100,
            )
        except ApiError:
            logger.debug(
                "Transport-heal: list-processing failed for %s in %s",
                agent_id, room_id, exc_info=True,
            )
            return
        except Exception:
            logger.debug(
                "Transport-heal: list-processing crashed for %s in %s",
                agent_id, room_id, exc_info=True,
            )
            return

        for msg in list(getattr(resp, "data", None) or []):
            msg_id = getattr(msg, "id", None)
            if not isinstance(msg_id, str):
                continue
            # Use ``inserted_at`` as a conservative proxy for the delivery's
            # ``started_at``: a delivery's /processing call is always after the
            # message was inserted, so ``started_at >= inserted_at`` — using
            # ``inserted_at`` UNDER-counts pin age and only fires later than a
            # true ``started_at`` read would. The SDK does not surface the
            # delivery's ``started_at`` on the ChatMessage record.
            inserted = _parse_ts(getattr(msg, "inserted_at", None))
            if inserted is None or (now - inserted) <= threshold:
                continue
            await self._attempt_pin_heal(
                agent_id, room_id, msg_id, client, bucket="processing",
            )

        # --- pending probe (post-turn 422 class) ---
        # A post-turn 422 leaves the poison in the pending bucket (no active
        # processing attempt), not processing. Only the head (data[0]) pins
        # the cursor; re-asserting /processing on non-head entries creates
        # junk attempts and must be avoided.
        try:
            pending_resp = await client.agent_api_messages.list_agent_messages(
                chat_id=room_id, status="pending", page=1, page_size=100,
            )
        except ApiError:
            logger.debug(
                "Transport-heal: list-pending failed for %s in %s",
                agent_id, room_id, exc_info=True,
            )
            return
        except Exception:
            logger.debug(
                "Transport-heal: list-pending crashed for %s in %s",
                agent_id, room_id, exc_info=True,
            )
            return

        pending_data = list(getattr(pending_resp, "data", None) or [])
        if not pending_data:
            return
        head = pending_data[0]
        head_id = getattr(head, "id", None)
        if not isinstance(head_id, str):
            return
        inserted = _parse_ts(getattr(head, "inserted_at", None))
        if inserted is None or (now - inserted) <= threshold:
            return
        await self._attempt_pin_heal(
            agent_id, room_id, head_id, client, bucket="pending",
        )

    async def _attempt_pin_heal(
        self, agent_id: str, room_id: str, message_id: str, client: Any,
        *, bucket: str = "processing",
    ) -> None:
        """Heal one stuck delivery; behavior differs by ``bucket``.

        ``bucket="processing"`` (crash-during-turn class): 1-step
        ``mark_agent_message_processed``. A 422 means the delivery is already
        processed — cursor already advanced; idempotent no-op.

        ``bucket="pending"`` (post-turn 422 class): a post-turn 422 leaves
        the delivery in the ``pending`` bucket (no active processing attempt),
        so ``/processed`` alone 422s and must not be mis-read as success.
        The 2-step heal re-opens a processing attempt
        (``mark_agent_message_processing``) then completes it
        (``mark_agent_message_processed``), then VERIFIES via a pending-head
        re-list that the cursor actually advanced. Success is decided by the
        verify (head advanced or absent), not by swallowing a 422.

        After ``transport_heal_max_attempts`` verify-failures on the same
        pending pin (or non-422 failures on a processing pin), escalate to the
        owner once and stop healing this delivery.
        """
        from thenvoi_rest.core.api_error import ApiError

        pin_key = (agent_id, message_id)
        if pin_key in self._pin_escalated:
            return

        if bucket == "pending":
            # Step a: re-open a processing attempt so /processed won't 422.
            try:
                await client.agent_api_messages.mark_agent_message_processing(
                    chat_id=room_id, id=message_id,
                )
            except Exception:
                logger.debug(
                    "Transport-heal: mark-processing step failed for "
                    "agent=%s msg=%s in room=%s",
                    agent_id, message_id, room_id, exc_info=True,
                )
            # Step b: terminate the attempt.
            try:
                await client.agent_api_messages.mark_agent_message_processed(
                    chat_id=room_id, id=message_id,
                )
            except Exception:
                logger.debug(
                    "Transport-heal: mark-processed step failed for "
                    "agent=%s msg=%s in room=%s",
                    agent_id, message_id, room_id, exc_info=True,
                )
            # Step c: verify by re-listing the pending head. A 422 on step b
            # is expected when the delivery already advanced; the re-list is
            # what decides success — do NOT treat a swallowed 422 as success.
            try:
                verify_resp = await client.agent_api_messages.list_agent_messages(
                    chat_id=room_id, status="pending", page=1, page_size=100,
                )
                verify_data = list(getattr(verify_resp, "data", None) or [])
                head_still_pinned = (
                    bool(verify_data)
                    and getattr(verify_data[0], "id", None) == message_id
                )
            except Exception:
                logger.debug(
                    "Transport-heal: pending verify re-list failed for "
                    "agent=%s msg=%s in room=%s — unknown outcome, retry next patrol",
                    agent_id, message_id, room_id, exc_info=True,
                )
                return  # UNKNOWN — do not update tracking
            if not head_still_pinned:
                # Cursor advanced (head gone or replaced) — success.
                self._pin_heal_attempts.pop(pin_key, None)
                logger.info(
                    "Transport-heal: pending pin healed for agent=%s msg=%s "
                    "in room=%s (cursor advanced)",
                    agent_id, message_id, room_id,
                )
                if self._activity:
                    self._activity.log(
                        "AGENT_PIN_HEALED", "watchdog",
                        f"Healed transport pin for {agent_id} msg={message_id}",
                        branch="pending_2step",
                        pin_class="pending",
                    )
            else:
                # Same head — heal did not advance the cursor.
                attempts = self._pin_heal_attempts.get(pin_key, 0) + 1
                self._pin_heal_attempts[pin_key] = attempts
                logger.warning(
                    "Transport-heal: pending heal attempt %d/%d did not "
                    "advance cursor for agent=%s msg=%s in room=%s",
                    attempts, self._config.transport_heal_max_attempts,
                    agent_id, message_id, room_id,
                )
                if attempts >= self._config.transport_heal_max_attempts:
                    await self._escalate_unhealable_pin(
                        agent_id, room_id, message_id, attempts,
                    )
                    self._pin_escalated.add(pin_key)
            return

        # --- processing bucket: 1-step (unchanged) ---
        try:
            await client.agent_api_messages.mark_agent_message_processed(
                chat_id=room_id, id=message_id,
            )
        except ApiError as e:
            if e.status_code == 422:
                # Already processed → cursor already advanced. Idempotent
                # no-op; drop tracking so the slot frees up.
                self._pin_heal_attempts.pop(pin_key, None)
                logger.debug(
                    "Transport-heal: delivery %s for %s already processed "
                    "(422) — treating as no-op",
                    message_id, agent_id,
                )
                return
            attempts = self._pin_heal_attempts.get(pin_key, 0) + 1
            self._pin_heal_attempts[pin_key] = attempts
            logger.warning(
                "Transport-heal attempt %d/%d failed for agent=%s msg=%s "
                "in room=%s: HTTP %s",
                attempts, self._config.transport_heal_max_attempts,
                agent_id, message_id, room_id, e.status_code,
            )
            if attempts >= self._config.transport_heal_max_attempts:
                await self._escalate_unhealable_pin(
                    agent_id, room_id, message_id, attempts,
                )
                self._pin_escalated.add(pin_key)
            return
        except Exception:
            attempts = self._pin_heal_attempts.get(pin_key, 0) + 1
            self._pin_heal_attempts[pin_key] = attempts
            logger.warning(
                "Transport-heal attempt %d/%d crashed for agent=%s msg=%s "
                "in room=%s",
                attempts, self._config.transport_heal_max_attempts,
                agent_id, message_id, room_id, exc_info=True,
            )
            if attempts >= self._config.transport_heal_max_attempts:
                await self._escalate_unhealable_pin(
                    agent_id, room_id, message_id, attempts,
                )
                self._pin_escalated.add(pin_key)
            return

        # 2xx: cursor advanced. Clear tracking so a future fresh pin on the
        # same agent/message starts at attempt 0 (current message_id is one-
        # shot, so this is mostly defensive).
        self._pin_heal_attempts.pop(pin_key, None)
        logger.info(
            "Transport-heal: re-asserted processed for agent=%s msg=%s "
            "in room=%s (cursor advanced)",
            agent_id, message_id, room_id,
        )
        if self._activity:
            self._activity.log(
                "AGENT_PIN_HEALED", "watchdog",
                f"Healed transport pin for {agent_id} msg={message_id}",
                branch="processing_1step",
                pin_class="processing",
            )

    async def _escalate_unhealable_pin(
        self, agent_id: str, room_id: str, message_id: str, attempts: int,
    ) -> None:
        """Owner-escalate a pin that survived ``max_attempts`` heal tries.

        Actively @-mentions the owner (``self._owner_id`` / ``self._owner_handle``,
        set by the runner at construction) so the human is woken instead of
        relying on them seeing a passive room post. The pinned agent is
        intentionally NOT @-mentioned: a wedged cursor cannot read inbound
        chat anyway, so the mention would be wasted and risks another 422
        storm. When no owner id is configured (e.g. the runner has not wired
        it for this swarm) the post degrades to mention-less, preserving the
        pre-upgrade behavior. Best-effort: a send failure is logged but does
        not break patrol.
        """
        from thenvoi_rest.types import (
            ChatMessageRequest,
            ChatMessageRequestMentionsItem,
        )

        threshold = self._config.transport_pin_threshold_seconds
        logger.critical(
            "Transport-heal: unable to clear pin for agent=%s msg=%s in "
            "room=%s after %d attempts — escalating",
            agent_id, message_id, room_id, attempts,
        )
        if self._activity:
            self._activity.log(
                "AGENT_PIN_UNHEALABLE", "watchdog",
                f"Pin survived {attempts} heals: {agent_id} msg={message_id}",
            )
        mentions: list[Any] = []
        owner_prefix = ""
        if self._owner_id:
            handle = self._owner_handle or self._owner_id
            mentions = [ChatMessageRequestMentionsItem(id=self._owner_id)]
            owner_prefix = f"@{handle} "
        try:
            await self._rest.agent_api_messages.create_agent_chat_message(
                chat_id=room_id,
                message=ChatMessageRequest(
                    content=(
                        f"{owner_prefix}[Watchdog] Transport pin on agent "
                        f"{agent_id} (message {message_id}) survived "
                        f"{attempts} heal attempts after >{threshold}s in "
                        f"processing. Owner: investigate or restart the agent."
                    ),
                    mentions=mentions,
                ),
            )
        except Exception:
            logger.exception(
                "Transport-heal: escalation post failed for agent=%s in room=%s",
                agent_id, room_id,
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
