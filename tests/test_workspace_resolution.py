"""The ONE workspace-path resolution rule — runner / cb-phase / registration parity.

Two implementations of "where does the workspace live" is how the container
gap happened: the runner honored ``$WORKSPACE`` (compose sets it to
``/workspace``, the shared volume), while ``cb-phase`` resolved relative
``workspace.path`` against the project dir — so agents looked for the
DB/pointer at ``/app/config/.codeband/state/`` instead of
``/workspace/state/``. Everything now routes through
``config.resolve_workspace_path``; this matrix pins that every consumer
agrees for set/unset × relative/absolute.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeband.cli import handoff
from codeband.config import (
    CodebandConfig,
    RepoConfig,
    WorkspaceConfig,
    resolve_workspace_path,
)
from codeband.orchestration.runner import _resolve_workspace_config
from codeband.state.registration import resolve_state_dir


def _make_config(workspace_path: str) -> CodebandConfig:
    return CodebandConfig(
        repo=RepoConfig(url="https://github.com/example/repo.git"),
        workspace=WorkspaceConfig(path=workspace_path),
    )


def _write_yaml(project_dir: Path, config: CodebandConfig) -> None:
    (project_dir / "codeband.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8",
    )


@pytest.mark.parametrize("workspace_env_set", [False, True])
@pytest.mark.parametrize("relative", [True, False])
def test_all_consumers_agree_on_workspace_path(
    tmp_path, monkeypatch, workspace_env_set, relative,
):
    """set/unset $WORKSPACE × relative/absolute workspace.path — cb-phase's
    store, the runner's resolved config and registration's state dir must all
    name the same workspace."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    env_root = tmp_path / "shared-volume"

    if workspace_env_set:
        monkeypatch.setenv("WORKSPACE", str(env_root))
    else:
        monkeypatch.delenv("WORKSPACE", raising=False)

    if relative:
        ws_value = ".codeband"
        expected = (env_root if workspace_env_set else project_dir) / ".codeband"
    else:
        ws_value = str(tmp_path / "abs-workspace")
        expected = tmp_path / "abs-workspace"  # $WORKSPACE never rebases absolute

    config = _make_config(ws_value)
    _write_yaml(project_dir, config)

    # The shared rule itself.
    assert resolve_workspace_path(config, project_dir) == expected

    # Runner: the resolved config the whole fleet runs with.
    resolved = _resolve_workspace_config(config, project_dir)
    assert Path(resolved.workspace.path) == expected

    # cb-phase / cb approve: the StateStore both gate legs read and write.
    store = handoff._resolve_store(project_dir)
    assert Path(store.db_path) == expected / "state" / "orchestration.db"

    # Registration / kickoff / doctor: the state dir holding the room pointer.
    assert resolve_state_dir(config, project_dir) == expected / "state"
