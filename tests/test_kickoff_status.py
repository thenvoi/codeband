"""Tests for `kickoff.query_status` branching on memory mode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from codeband.config import (
    AgentConfigFile,
    AgentCredentials,
    BandConfig,
    CodebandConfig,
    RepoConfig,
    WorkspaceConfig,
)
from codeband.memory import LocalMemoryStore, reset_memory_mode
from codeband.orchestration.kickoff import query_status


@pytest.fixture(autouse=True)
def _reset_probe(monkeypatch):
    monkeypatch.delenv("BAND_MEMORY_MODE", raising=False)
    reset_memory_mode()
    yield
    reset_memory_mode()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    # Write a minimal agent_config.yaml so load_agent_config works.
    agent_config = AgentConfigFile(agents={
        "conductor": AgentCredentials(agent_id="c1", api_key="key"),
    })
    agent_config.to_yaml(tmp_path / "agent_config.yaml")
    return tmp_path


def _make_config(tmp_path: Path, *, memory_mode: str = "auto") -> CodebandConfig:
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git"),
        workspace=WorkspaceConfig(path=str(tmp_path / "workspace")),
        band=BandConfig(memory_mode=memory_mode),
    )


class TestQueryStatus:
    async def test_local_mode_reads_from_jsonl(
        self, project_dir: Path, capsys: pytest.CaptureFixture,
    ):
        config = _make_config(project_dir, memory_mode="local")

        workspace_path = Path(config.workspace.path)
        store = LocalMemoryStore(workspace_path / "state" / "memories.jsonl")
        await store.store(
            content="protocol plan cid plan_r1 state ready "
                    "from planner to conductor auth feature",
            system="working", type="episodic", segment="agent",
            scope="organization", thought="plan ready",
        )
        await store.store(
            content="Test command: pytest -v",
            system="long_term", type="procedural", segment="tool",
            scope="organization", thought="test command",
        )

        with patch("thenvoi_rest.AsyncRestClient"):
            await query_status(config, project_dir)

        out = capsys.readouterr().out
        assert "CODEBAND STATUS" in out
        assert "auth feature" in out
        assert "test command" in out

    async def test_local_mode_prints_empty_state(
        self, project_dir: Path, capsys: pytest.CaptureFixture,
    ):
        config = _make_config(project_dir, memory_mode="local")

        with patch("thenvoi_rest.AsyncRestClient"):
            await query_status(config, project_dir)

        assert "No active tasks or knowledge found." in capsys.readouterr().out

    async def test_band_mode_uses_rest_client(
        self, project_dir: Path, capsys: pytest.CaptureFixture,
    ):
        config = _make_config(project_dir, memory_mode="band")

        class _FakeMem:
            content = "protocol plan cid plan_r1 state ready from planner to conductor"
            thought = "plan"
            updated_at = "2026-01-01T00:00:00Z"

        class _FakeResp:
            def __init__(self, data):
                self.data = data

        fake_client = type("FakeClient", (), {})()
        fake_client.agent_api_memories = type("FakeMemAPI", (), {})()
        fake_client.agent_api_memories.list_agent_memories = AsyncMock(
            side_effect=[_FakeResp([_FakeMem()]), _FakeResp([_FakeMem()])],
        )

        with patch("thenvoi_rest.AsyncRestClient", return_value=fake_client):
            await query_status(config, project_dir)

        out = capsys.readouterr().out
        assert "CODEBAND STATUS" in out
        # Local JSONL store should NOT exist in band mode.
        jsonl = Path(config.workspace.path) / "state" / "memories.jsonl"
        assert not jsonl.exists()
