"""Tests for the opt-in jam delivery transport (``CODEBAND_DELIVERY=jam``).

The guarantees defended here:
* the ``sdk`` default is unchanged and the jam code is dormant when off;
* the jam path hands the adapter the SAME ``AgentInput`` shape (via the real
  ``DefaultPreprocessor``) — the brain is unchanged;
* the jam path reproduces the SDK ExecutionContext semantics that matter
  (self-filter, retry budget, per-room serialization);
* the non-wedge property: a rejected ack stays cosmetic — other messages flow.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from codeband.transport.jam_control import AckOutcome, Target, agent_scope
from codeband.transport.jam_runtime import JamAgent, _RoomWorker

# --- doubles ---------------------------------------------------------------


class FakeAdapter:
    def __init__(self, on_event=None):
        self.events: list = []
        self.started = None
        self._thenvoi_agent_id = None
        self._on_event_hook = on_event

    async def on_started(self, name, description):
        self.started = (name, description)

    async def on_event(self, inp):
        self.events.append(inp)
        if self._on_event_hook is not None:
            await self._on_event_hook(inp)


class FakeControl:
    def __init__(self, batches=None, ack_ok=True):
        self._batches = list(batches or [])
        self.ack_ok = ack_ok
        self.ack_outcomes: dict[str, bool] = {}
        self.acked: list[str] = []
        self.adopts: list = []
        self.closed = False
        self.sent: list = []

    async def ping(self):
        return True

    async def adopt(self, opts, agent_key):
        self.adopts.append((opts, agent_key))

    async def inbox(self, target):
        return self._batches.pop(0) if self._batches else []

    async def ack(self, target, msg_id):
        ok = self.ack_outcomes.get(msg_id, self.ack_ok)
        self.acked.append(msg_id)
        return AckOutcome(ok=ok, error=None if ok else "simulated")

    async def send(self, *a, **k):
        self.sent.append((a, k))
        return {"result": {"message_id": "x", "warnings": []}}

    async def close(self):
        self.closed = True


def _config(*, delivery="jam", max_retries=3, idle=1):
    return SimpleNamespace(
        agents=SimpleNamespace(
            idle_resync_seconds=idle,
            max_message_retries=max_retries,
            delivery=delivery,
        ),
        band=SimpleNamespace(ws_url="ws://test", rest_url="http://test"),
    )


def _creds(agent_id="agent-A"):
    return SimpleNamespace(agent_id=agent_id, api_key="band_a_testkey")


def _wire_msg(
    message_id,
    chat_id="room-1",
    content="hi",
    sender_id="user-1",
    sender_type="User",
    sender_name="User One",
    enqueued_at=1_718_649_600_000,
):
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_handle": "owner/u1",
        "sender_type": sender_type,
        "content": content,
        "enqueued_at": enqueued_at,
        "addressed_to_user": False,
    }


def _make_agent(adapter=None, control=None, *, agent_id="agent-A", max_retries=3):
    """A JamAgent wired with fakes + a real DefaultPreprocessor, no network."""
    from thenvoi.preprocessing.default import DefaultPreprocessor
    from thenvoi.runtime.types import SessionConfig

    adapter = adapter or FakeAdapter()
    control = control or FakeControl()
    agent = JamAgent(
        adapter,
        _creds(agent_id),
        _config(max_retries=max_retries),
        control=control,
        link=SimpleNamespace(rest=SimpleNamespace()),
        preprocessor=DefaultPreprocessor(),
    )
    # Hydration off so the real preprocessor never reaches the network.
    agent._session_config = SessionConfig(
        enable_context_hydration=False,
        max_message_retries=max_retries,
        idle_resync_seconds=1,
    )
    return agent, adapter, control


def _make_worker(agent, room_id="room-1"):
    from thenvoi.runtime.execution import ExecutionContext
    from thenvoi.runtime.retry_tracker import MessageRetryTracker

    worker = _RoomWorker(room_id, agent)
    worker._ctx = ExecutionContext(
        room_id, agent._link, _noop, config=agent._session_config, agent_id=agent._agent_id
    )
    worker._retry = MessageRetryTracker(
        max_retries=agent._config.agents.max_message_retries, room_id=room_id
    )
    return worker


async def _noop(ctx, event):
    return None


@pytest.fixture(autouse=True)
def _no_network_hydration(monkeypatch):
    """ExecutionContext hydration is REST-backed; stub it out for unit tests."""
    from unittest.mock import AsyncMock

    from thenvoi.runtime.execution import ExecutionContext

    monkeypatch.setattr(ExecutionContext, "_ensure_fresh_context", AsyncMock())


# --- flag resolution & dormancy -------------------------------------------


def test_resolve_delivery_default_sdk(monkeypatch):
    import codeband.orchestration.runner as r

    monkeypatch.delenv("CODEBAND_DELIVERY", raising=False)
    assert r._resolve_delivery_mode(_config(delivery="sdk")) == "sdk"


def test_resolve_delivery_jam_via_config(monkeypatch):
    import codeband.orchestration.runner as r

    monkeypatch.delenv("CODEBAND_DELIVERY", raising=False)
    assert r._resolve_delivery_mode(_config(delivery="jam")) == "jam"


def test_resolve_delivery_env_overrides_and_unknown_is_sdk(monkeypatch):
    import codeband.orchestration.runner as r

    monkeypatch.setenv("CODEBAND_DELIVERY", "jam")
    assert r._resolve_delivery_mode(_config(delivery="sdk")) == "jam"
    monkeypatch.setenv("CODEBAND_DELIVERY", "nonsense")
    assert r._resolve_delivery_mode(_config(delivery="jam")) == "sdk"


def test_sdk_mode_does_not_touch_jam(monkeypatch):
    """sdk mode returns the SDK agent and never constructs a JamAgent."""
    import codeband.orchestration.runner as r
    import codeband.transport.jam_runtime as jr

    monkeypatch.delenv("CODEBAND_DELIVERY", raising=False)
    monkeypatch.setattr(r, "_create_band_agent", lambda a, c, cfg: "SDK_AGENT")

    def _boom(*a, **k):
        raise AssertionError("JamAgent must not be constructed on the sdk path")

    monkeypatch.setattr(jr, "JamAgent", _boom)
    out = r._create_delivery_agent(object(), _creds(), _config(delivery="sdk"))
    assert out == "SDK_AGENT"


def test_jam_mode_builds_jam_agent(monkeypatch):
    import codeband.orchestration.runner as r

    monkeypatch.setenv("CODEBAND_DELIVERY", "jam")
    out = r._create_delivery_agent(FakeAdapter(), _creds(), _config(delivery="jam"))
    assert isinstance(out, JamAgent)


async def test_jam_preflight_raises_when_daemon_unreachable(monkeypatch, tmp_path):
    # Async on purpose: run_local/run_agent await this from inside a RUNNING
    # event loop, so the preflight must not call asyncio.run() (round-1 high).
    import codeband.orchestration.runner as r

    # Point at a config dir with no jam.sock → ping fails → fail fast.
    monkeypatch.setenv("JAM_CONFIG_DIR", str(tmp_path))
    with pytest.raises(SystemExit):
        await r._jam_delivery_preflight(_config(delivery="jam"))


async def test_run_closes_control_on_transport_fatal(monkeypatch):
    """run() must close the UDS client on a clean (transport-fatal) return."""

    class BoomControl(FakeControl):
        async def inbox(self, target):
            raise RuntimeError("jamd down")

    control = BoomControl()
    agent, adapter, _ = _make_agent(control=control)
    agent._poll_interval = 0  # no backoff sleep in the test
    monkeypatch.setattr("codeband.transport.jam_runtime._MAX_INBOX_FAILURES", 2)

    await asyncio.wait_for(agent.run(), timeout=5)

    assert control.closed is True  # httpx UDS client not leaked
    assert adapter.started  # start()'s adapter handshake ran


# --- receive shape / brain parity (round-1 blocker #1) --------------------


async def test_jam_receive_delivers_expected_shape():
    agent, adapter, control = _make_agent()
    worker = _make_worker(agent)
    await worker._process(_wire_msg("m1", content="hello world"))

    assert len(adapter.events) == 1
    inp = adapter.events[0]
    assert inp.msg.id == "m1"
    assert inp.room_id == "room-1"
    assert inp.msg.content == "hello world"
    assert inp.msg.sender_id == "user-1"
    assert inp.msg.sender_type == "User"
    assert inp.msg.message_type == "text"
    assert control.acked == ["m1"]  # acked on success


async def test_payload_is_message_created_payload_not_platform_message():
    """Round-1 blocker #1: the preprocessor reads payload.inserted_at."""
    from thenvoi.preprocessing.default import DefaultPreprocessor
    from thenvoi.platform.event import MessageEvent
    from thenvoi.runtime.execution import ExecutionContext
    from thenvoi.runtime.types import PlatformMessage

    agent, _adapter, _control = _make_agent()

    # The jam-built event uses a MessageCreatedPayload → preprocessor accepts it.
    event = agent._build_event("room-1", _wire_msg("m1"))
    assert event.payload.inserted_at  # MessageCreatedPayload has inserted_at
    ctx = ExecutionContext(
        "room-1", agent._link, _noop, config=agent._session_config, agent_id="agent-A"
    )
    inp = await DefaultPreprocessor().process(ctx=ctx, event=event, agent_id="agent-A")
    assert inp is not None and inp.msg.id == "m1"

    # Regression guard: a PlatformMessage (created_at, no inserted_at) as payload
    # would crash — proving the test would catch the wrong-shape bug.
    import datetime

    bad = MessageEvent(
        room_id="room-1",
        payload=PlatformMessage(
            id="m1",
            room_id="room-1",
            content="x",
            sender_id="u",
            sender_type="User",
            sender_name=None,
            message_type="text",
            metadata={},
            created_at=datetime.datetime.now(datetime.timezone.utc),
        ),
    )
    with pytest.raises(AttributeError):
        await DefaultPreprocessor().process(ctx=ctx, event=bad, agent_id="agent-A")


# --- self-message filter ---------------------------------------------------


async def test_self_message_filtered_and_drained():
    agent, adapter, control = _make_agent(agent_id="agent-A")
    worker = _make_worker(agent)
    # A message from the agent itself.
    await worker._process(_wire_msg("self1", sender_id="agent-A", sender_type="Agent"))

    assert adapter.events == []  # handler NOT invoked
    assert control.acked == ["self1"]  # drained from the queue


# --- NON-WEDGE: a rejected ack stays cosmetic, other messages flow --------


async def test_failed_ack_is_cosmetic_other_messages_flow():
    control = FakeControl(ack_ok=True)
    control.ack_outcomes["X"] = False  # X's ack is rejected (simulated 422)
    agent, adapter, _ = _make_agent(control=control)
    worker = _make_worker(agent)

    await worker._process(_wire_msg("X", content="first"))
    await worker._process(_wire_msg("Y", content="second"))

    # Both delivered to the handler — X's failed ack did NOT block Y.
    assert [e.msg.id for e in adapter.events] == ["X", "Y"]
    assert control.acked == ["X", "Y"]  # both ack-attempted; X rejected, no raise


async def test_no_cross_room_head_of_line(monkeypatch):
    """Round-1 high #3: a slow handler in room A must not delay room B."""
    gate = asyncio.Event()
    delivered: list[str] = []

    async def on_event(inp):
        delivered.append(inp.room_id)
        if inp.room_id == "room-A":
            await gate.wait()  # block room A indefinitely

    adapter = FakeAdapter(on_event=on_event)
    agent, _adapter, control = _make_agent(adapter=adapter)
    control.ack_ok = True

    # Route a message to room A (will block) and one to room B (should flow).
    agent._route(_wire_msg("a1", chat_id="room-A"))
    agent._route(_wire_msg("b1", chat_id="room-B"))

    # B must be handled even while A is stuck.
    async def _wait_b():
        while "room-B" not in delivered:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_b(), timeout=2.0)
    assert "room-B" in delivered
    gate.set()
    await agent.stop()


# --- retry budget + at-least-once (round-1 high #2) -----------------------


async def test_handler_failure_retries_then_drains():
    calls = {"n": 0}

    async def on_event(inp):
        calls["n"] += 1
        raise RuntimeError("boom")

    adapter = FakeAdapter(on_event=on_event)
    agent, _adapter, control = _make_agent(adapter=adapter, max_retries=2)
    worker = _make_worker(agent)

    # max_retries=2 → attempts 1 and 2 run the handler (and raise, no ack);
    # attempt 3 exceeds the budget → drained (acked) without invoking handler.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await worker._process(_wire_msg("poison"))
    assert control.acked == []  # never acked while failing → jam redelivers

    await worker._process(_wire_msg("poison"))  # 3rd: exceeded → drain
    assert calls["n"] == 2  # handler ran exactly max_retries times
    assert control.acked == ["poison"]  # drained to stop infinite redelivery


# --- dedupe ----------------------------------------------------------------


async def test_route_dedupes_inflight_and_handled():
    agent, adapter, control = _make_agent()
    msg = _wire_msg("dup")

    agent._route(msg)  # enqueued; now in-flight
    agent._route(msg)  # duplicate while in-flight → skipped
    # let the worker process it
    await asyncio.sleep(0.05)
    agent._route(msg)  # now handled → skipped
    await asyncio.sleep(0.05)

    assert [e.msg.id for e in adapter.events] == ["dup"]  # delivered once
    await agent.stop()


# --- lifecycle -------------------------------------------------------------


async def test_stop_tears_down_workers_and_closes_control():
    agent, _adapter, control = _make_agent()
    agent._route(_wire_msg("m1"))
    await asyncio.sleep(0.02)
    assert agent._workers  # a worker exists
    await agent.stop()
    assert control.closed is True


def test_agent_scope_and_target():
    assert agent_scope("abc") == "codeband-abc"
    t = Target(scope="codeband-abc")
    assert t.as_dict() == {"profile": "default", "scope": "codeband-abc", "handle": ""}


# --- doctor tripwire for the SDK-internals coupling ------------------------


def _doctor_ctx(tmp_path):
    from codeband.doctor import Context

    return Context(project_dir=tmp_path)


def test_doctor_jam_coupling_ok_on_current_sdk(tmp_path, monkeypatch):
    from codeband.doctor import Status, check_jam_delivery_sdk_coupling

    monkeypatch.delenv("CODEBAND_DELIVERY", raising=False)
    res = check_jam_delivery_sdk_coupling(_doctor_ctx(tmp_path))
    assert res.status is Status.OK


def test_doctor_jam_coupling_warn_when_symbol_moved_sdk_mode(tmp_path, monkeypatch):
    """A moved SDK symbol → WARN on the default sdk path (exit code unaffected)."""
    import importlib

    from codeband.doctor import Status, check_jam_delivery_sdk_coupling

    monkeypatch.delenv("CODEBAND_DELIVERY", raising=False)
    real = importlib.import_module

    def _fake(name, *a, **k):
        if name == "thenvoi.runtime.retry_tracker":
            raise ImportError("simulated rename")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", _fake)
    res = check_jam_delivery_sdk_coupling(_doctor_ctx(tmp_path))
    assert res.status is Status.WARN
    assert "retry_tracker" in res.message


def test_doctor_jam_coupling_fails_when_jam_selected(tmp_path, monkeypatch):
    """Same moved symbol, but CODEBAND_DELIVERY=jam → FAIL (path in use is broken)."""
    import importlib

    from codeband.doctor import Status, check_jam_delivery_sdk_coupling

    monkeypatch.setenv("CODEBAND_DELIVERY", "jam")
    real = importlib.import_module

    def _fake(name, *a, **k):
        if name == "thenvoi.runtime.retry_tracker":
            raise ImportError("simulated rename")
        return real(name, *a, **k)

    monkeypatch.setattr(importlib, "import_module", _fake)
    res = check_jam_delivery_sdk_coupling(_doctor_ctx(tmp_path))
    assert res.status is Status.FAIL
    assert res.remediation and "jam_runtime.py" in res.remediation
