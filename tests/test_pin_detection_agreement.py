"""Drift-guard: pin-detection criteria in _detect_room_pin (#86 discriminator)
and _check_one_agent_room_pins (#85 transport heal) must agree.

Both carry parallel implementations of the same two criteria:
  1. any ``processing`` record with inserted_at older than threshold
  2. the ``pending`` HEAD (data[0]) with inserted_at older than threshold

Without a guard, a future edit to one path can silently diverge from the other,
reintroducing the false-block class of bug #86 was built to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from codeband.config import WatchdogConfig

_THRESHOLD = timedelta(seconds=600)
_ROOM_ID = "room-agree"
_AGENT_ID = "agent-agree"

# Age constants relative to threshold (T = 600s).
_PINNED_AGE = 900   # > T  → should be detected as a pin
_FRESH_AGE = 60     # < T  → should NOT be detected as a pin


# ── helpers ──────────────────────────────────────────────────────────────────

def _msg(msg_id: str, age_seconds: int) -> MagicMock:
    m = MagicMock()
    m.id = msg_id
    m.inserted_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return m


def _client(
    *,
    processing: list | None = None,
    pending: list | None = None,
) -> MagicMock:
    """REST client returning the given processing/pending message lists."""
    client = MagicMock()
    client.agent_api_messages = MagicMock()

    async def _list(*_args: object, **kwargs: object) -> MagicMock:
        status = kwargs.get("status", "")
        resp = MagicMock()
        resp.data = (processing or []) if status == "processing" else (pending or [])
        return resp

    client.agent_api_messages.list_agent_messages = AsyncMock(side_effect=_list)
    client.agent_api_messages.mark_agent_message_processed = AsyncMock()
    client.agent_api_messages.mark_agent_message_processing = AsyncMock()
    return client


def _daemon() -> object:
    from codeband.agents.watchdog import WatchdogDaemon

    rest = MagicMock()
    rest.agent_api_messages = MagicMock()
    rest.agent_api_messages.create_agent_chat_message = AsyncMock()
    return WatchdogDaemon(
        config=WatchdogConfig(transport_pin_threshold_seconds=600),
        rest_client=rest,
        agent_id="agent-wd",
        conductor_id="agent-wd",
    )


# ── shared fixture cases ──────────────────────────────────────────────────────
# Each tuple: (label, processing_msgs, pending_msgs, expected_pin)
# «expected_pin» is the ground-truth each path must agree on.

_CASES: list[tuple[str, list, list, bool]] = [
    (
        "empty_mailbox",
        [], [],
        False,
    ),
    (
        "processing_older_than_threshold",
        [_msg("proc-old", _PINNED_AGE)], [],
        True,
    ),
    (
        "processing_younger_than_threshold",
        [_msg("proc-fresh", _FRESH_AGE)], [],
        False,
    ),
    (
        "pending_head_older_than_threshold",
        [], [_msg("pend-old", _PINNED_AGE)],
        True,
    ),
    (
        "pending_head_younger_than_threshold",
        [], [_msg("pend-fresh", _FRESH_AGE)],
        False,
    ),
    (
        "pending_head_fresh_non_head_old",
        # data[0] is fresh; only the HEAD (data[0]) governs the cursor.
        [], [_msg("pend-head-fresh", _FRESH_AGE), _msg("pend-body-old", _PINNED_AGE)],
        False,
    ),
    (
        "both_buckets_old",
        [_msg("proc-old2", _PINNED_AGE)], [_msg("pend-old2", _PINNED_AGE)],
        True,
    ),
]

_PARAMS = [(c[0], c[1], c[2], c[3]) for c in _CASES]


# ── per-path baseline tests (document expected behaviour for each path) ───────

@pytest.mark.parametrize("label,processing,pending,expected_pin", _PARAMS)
@pytest.mark.asyncio
async def test_detect_room_pin_baseline(
    label: str,
    processing: list,
    pending: list,
    expected_pin: bool,
) -> None:
    """#86 discriminator probe (_detect_room_pin) classifies each fixture correctly."""
    daemon = _daemon()
    client = _client(processing=processing, pending=pending)

    result = await daemon._detect_room_pin(
        _AGENT_ID, _ROOM_ID, client, _THRESHOLD, datetime.now(UTC),
    )

    assert result is expected_pin, (
        f"[{label}] _detect_room_pin={result!r}, expected={expected_pin!r}"
    )


@pytest.mark.parametrize("label,processing,pending,expected_pin", _PARAMS)
@pytest.mark.asyncio
async def test_check_one_agent_room_pins_baseline(
    label: str,
    processing: list,
    pending: list,
    expected_pin: bool,
) -> None:
    """#85 heal rung (_check_one_agent_room_pins) attempts healing iff a pin is expected."""
    daemon = _daemon()
    client = _client(processing=processing, pending=pending)

    await daemon._check_one_agent_room_pins(
        _AGENT_ID, _ROOM_ID, client, _THRESHOLD, datetime.now(UTC),
    )

    heal_attempted = (
        client.agent_api_messages.mark_agent_message_processed.await_count > 0
        or client.agent_api_messages.mark_agent_message_processing.await_count > 0
    )
    assert heal_attempted is expected_pin, (
        f"[{label}] heal_attempted={heal_attempted!r}, expected={expected_pin!r}"
    )


# ── agreement (drift-guard) test ──────────────────────────────────────────────

@pytest.mark.parametrize("label,processing,pending,_expected", _PARAMS)
@pytest.mark.asyncio
async def test_both_paths_agree_on_same_mailbox(
    label: str,
    processing: list,
    pending: list,
    _expected: bool,
) -> None:
    """Core drift guard: _detect_room_pin and _check_one_agent_room_pins must agree.

    Both paths are driven from the same mailbox state. If this test fails, one
    path's pin criteria drifted from the other — the discriminator and heal rung
    no longer share the same definition of "pinned".
    """
    daemon = _daemon()
    now = datetime.now(UTC)

    detect_client = _client(processing=processing, pending=pending)
    heal_client = _client(processing=processing, pending=pending)

    detected = await daemon._detect_room_pin(
        _AGENT_ID, _ROOM_ID, detect_client, _THRESHOLD, now,
    )

    await daemon._check_one_agent_room_pins(
        _AGENT_ID, _ROOM_ID, heal_client, _THRESHOLD, now,
    )
    heal_attempted = (
        heal_client.agent_api_messages.mark_agent_message_processed.await_count > 0
        or heal_client.agent_api_messages.mark_agent_message_processing.await_count > 0
    )

    assert detected is heal_attempted, (
        f"[{label}] pin-criteria divergence: "
        f"_detect_room_pin={detected!r} but heal_attempted={heal_attempted!r}. "
        "A future edit caused the #86 discriminator and #85 heal rung to disagree."
    )
