"""Tests for `codeband.agents.watchdog_probe.probe_liveness_backend`.

Mirrors `tests/test_memory_probe.py` — the liveness probe follows the same
shape as the memory probe so that free-tier Band.ai accounts fall back
cleanly and the env/config override precedent is consistent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from codeband.agents import watchdog_probe
from codeband.agents.watchdog_probe import (
    probe_liveness_backend,
    reset_liveness_mode,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Each test starts with a clean probe cache and no env override."""
    monkeypatch.delenv("WATCHDOG_LIVENESS_MODE", raising=False)
    reset_liveness_mode()
    yield
    reset_liveness_mode()


def _client_returning(data) -> object:
    client = type("FakeClient", (), {})()
    client.human_api_chats = type("FakeHumanChatsAPI", (), {})()
    client.human_api_chats.list_my_chats = AsyncMock(return_value=data)
    return client


def _client_raising(exc: BaseException) -> object:
    client = type("FakeClient", (), {})()
    client.human_api_chats = type("FakeHumanChatsAPI", (), {})()
    client.human_api_chats.list_my_chats = AsyncMock(side_effect=exc)
    return client


@dataclass
class _HttpError(Exception):
    status_code: int


class TestProbe:
    async def test_success_returns_human_mode(self):
        client = _client_returning(object())
        assert await probe_liveness_backend(client) == "human"

    async def test_http_402_falls_back_to_agent(self):
        client = _client_raising(_HttpError(status_code=402))
        assert await probe_liveness_backend(client) == "agent"

    async def test_http_403_falls_back_to_agent(self):
        client = _client_raising(_HttpError(status_code=403))
        assert await probe_liveness_backend(client) == "agent"

    async def test_http_404_falls_back_to_agent(self):
        client = _client_raising(_HttpError(status_code=404))
        assert await probe_liveness_backend(client) == "agent"

    async def test_http_501_falls_back_to_agent(self):
        client = _client_raising(_HttpError(status_code=501))
        assert await probe_liveness_backend(client) == "agent"

    async def test_timeout_falls_back_to_agent(self, monkeypatch):
        monkeypatch.setattr(watchdog_probe, "_PROBE_TIMEOUT_SEC", 0.05)

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(1.0)
            return object()

        client = type("FakeClient", (), {})()
        client.human_api_chats = type("FakeHumanChatsAPI", (), {})()
        client.human_api_chats.list_my_chats = slow

        assert await probe_liveness_backend(client) == "agent"

    async def test_unexpected_5xx_falls_back_to_agent(self):
        client = _client_raising(_HttpError(status_code=503))
        assert await probe_liveness_backend(client) == "agent"

    async def test_result_is_cached(self):
        client = _client_returning(object())
        await probe_liveness_backend(client)
        await probe_liveness_backend(client)
        assert client.human_api_chats.list_my_chats.await_count == 1

    async def test_force_reprobes(self):
        client = _client_returning(object())
        await probe_liveness_backend(client)
        await probe_liveness_backend(client, force=True)
        assert client.human_api_chats.list_my_chats.await_count == 2


class TestOverrides:
    async def test_env_var_human_skips_probe(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_LIVENESS_MODE", "human")
        client = _client_raising(_HttpError(status_code=403))
        assert await probe_liveness_backend(client) == "human"
        client.human_api_chats.list_my_chats.assert_not_awaited()

    async def test_env_var_agent_skips_probe(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_LIVENESS_MODE", "agent")
        client = _client_returning(object())
        assert await probe_liveness_backend(client) == "agent"
        client.human_api_chats.list_my_chats.assert_not_awaited()

    async def test_config_override_human_skips_probe(self):
        client = _client_raising(_HttpError(status_code=403))
        assert await probe_liveness_backend(client, config_override="human") == "human"
        client.human_api_chats.list_my_chats.assert_not_awaited()

    async def test_config_override_agent_skips_probe(self):
        client = _client_returning(object())
        assert await probe_liveness_backend(client, config_override="agent") == "agent"
        client.human_api_chats.list_my_chats.assert_not_awaited()

    async def test_env_var_takes_precedence_over_config_override(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_LIVENESS_MODE", "agent")
        client = _client_returning(object())
        # config says human but env says agent → env wins
        assert await probe_liveness_backend(client, config_override="human") == "agent"
        client.human_api_chats.list_my_chats.assert_not_awaited()
