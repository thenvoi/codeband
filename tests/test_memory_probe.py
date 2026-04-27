"""Tests for `codeband.memory.probe.probe_memory_backend`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from codeband.memory import probe
from codeband.memory.probe import probe_memory_backend, reset_memory_mode


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Each test starts with a clean probe cache and no env override."""
    monkeypatch.delenv("BAND_MEMORY_MODE", raising=False)
    reset_memory_mode()
    yield
    reset_memory_mode()


def _client_returning(data) -> object:
    client = type("FakeClient", (), {})()
    client.agent_api_memories = type("FakeMemAPI", (), {})()
    client.agent_api_memories.list_agent_memories = AsyncMock(return_value=data)
    return client


def _client_raising(exc: BaseException) -> object:
    client = type("FakeClient", (), {})()
    client.agent_api_memories = type("FakeMemAPI", (), {})()
    client.agent_api_memories.list_agent_memories = AsyncMock(side_effect=exc)
    return client


@dataclass
class _HttpError(Exception):
    status_code: int


class TestProbe:
    async def test_success_returns_band_mode(self):
        client = _client_returning(object())
        mode = await probe_memory_backend(client)
        assert mode == "band"

    async def test_http_403_falls_back_to_local(self):
        client = _client_raising(_HttpError(status_code=403))
        mode = await probe_memory_backend(client)
        assert mode == "local"

    async def test_http_402_falls_back_to_local(self):
        client = _client_raising(_HttpError(status_code=402))
        assert await probe_memory_backend(client) == "local"

    async def test_timeout_falls_back_to_local(self, monkeypatch):
        monkeypatch.setattr(probe, "_PROBE_TIMEOUT_SEC", 0.05)

        async def slow(*_args, **_kwargs):
            await asyncio.sleep(1.0)
            return object()

        client = type("FakeClient", (), {})()
        client.agent_api_memories = type("FakeMemAPI", (), {})()
        client.agent_api_memories.list_agent_memories = slow

        assert await probe_memory_backend(client) == "local"

    async def test_unexpected_5xx_falls_back_to_local(self):
        client = _client_raising(_HttpError(status_code=503))
        assert await probe_memory_backend(client) == "local"

    async def test_result_is_cached(self):
        client = _client_returning(object())
        await probe_memory_backend(client)
        await probe_memory_backend(client)
        assert client.agent_api_memories.list_agent_memories.await_count == 1

    async def test_force_reprobes(self):
        client = _client_returning(object())
        await probe_memory_backend(client)
        await probe_memory_backend(client, force=True)
        assert client.agent_api_memories.list_agent_memories.await_count == 2


class TestOverrides:
    async def test_env_var_band_skips_probe(self, monkeypatch):
        monkeypatch.setenv("BAND_MEMORY_MODE", "band")
        # Client that would fail — proving the probe is skipped.
        client = _client_raising(_HttpError(status_code=403))
        assert await probe_memory_backend(client) == "band"
        client.agent_api_memories.list_agent_memories.assert_not_awaited()

    async def test_env_var_local_skips_probe(self, monkeypatch):
        monkeypatch.setenv("BAND_MEMORY_MODE", "local")
        client = _client_returning(object())
        assert await probe_memory_backend(client) == "local"
        client.agent_api_memories.list_agent_memories.assert_not_awaited()

    async def test_config_override_band_skips_probe(self):
        client = _client_raising(_HttpError(status_code=403))
        assert await probe_memory_backend(client, config_override="band") == "band"
        client.agent_api_memories.list_agent_memories.assert_not_awaited()

    async def test_env_var_beats_config_override(self, monkeypatch):
        monkeypatch.setenv("BAND_MEMORY_MODE", "local")
        client = _client_returning(object())
        assert await probe_memory_backend(client, config_override="band") == "local"

    async def test_invalid_env_value_falls_through_to_probe(self, monkeypatch):
        monkeypatch.setenv("BAND_MEMORY_MODE", "bogus")
        client = _client_returning(object())
        assert await probe_memory_backend(client) == "band"
