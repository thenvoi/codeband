"""Tests for the shell FSBackend protocol — LocalBackend + SharedComposeBackend."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch


from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    DeploymentMode,
    FrameworkPool,
    PoolEntry,
    RepoConfig,
    WorkspaceConfig,
)
from codeband.shell.fs import SharedComposeBackend, LocalBackend, make_backend


# ─── LocalBackend ──────────────────────────────────────────────────────────


def test_local_backend_reads_activity_jsonl(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    state = project / ".codeband" / "state"
    state.mkdir(parents=True)
    (state / "activity.jsonl").write_text(
        json.dumps({
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": "TASK_RECEIVED",
            "agent": "conductor",
            "summary": "do the thing",
            "details": None,
        }) + "\n"
    )

    config = _make_config(workspace_path=str(project / ".codeband"))
    backend = LocalBackend(config=config, project_dir=project)
    events = backend.read_activity_events()
    assert len(events) == 1
    assert events[0].agent == "conductor"
    assert events[0].event_type == "TASK_RECEIVED"


def test_local_backend_lists_worktrees_from_layout(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    config = _make_config(workspace_path=str(project / ".codeband"))
    backend = LocalBackend(config=config, project_dir=project)
    candidates = backend.list_worktrees()
    # Default config in _make_config has 1 claude_sdk coder + mergemaster.
    assert "coder-claude_sdk-0" in candidates
    assert "mergemaster" in candidates


def test_local_backend_diff_uses_real_git(tmp_path: Path):
    """End-to-end: diff a worktree on a real git repo."""
    project = tmp_path / "proj"
    project.mkdir()
    workspace = project / ".codeband"
    worktree = workspace / "worktrees" / "coder-claude_sdk-0"
    worktree.mkdir(parents=True)
    _git("init", cwd=worktree)
    _git_config(worktree)
    (worktree / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=worktree)
    _git("commit", "-m", "init", cwd=worktree)
    _git("branch", "-M", "main", cwd=worktree)

    config = _make_config(workspace_path=str(workspace))
    backend = LocalBackend(config=config, project_dir=project)
    wd = backend.worktree_diff("coder-claude_sdk-0", "main")
    assert wd.has_changes is False  # nothing diverged from main


# ─── SharedComposeBackend ─────────────────────────────────────────────────────────


def test_docker_backend_exec_builds_correct_command(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")

    config = _make_config(workspace_path="/workspace", mode=DeploymentMode.DISTRIBUTED)
    backend = SharedComposeBackend(
        config=config,
        project_dir=project,
        compose_file=compose_file,
        service="conductor",
    )

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="hello\n", stderr="")

    with patch("codeband.shell.fs.subprocess.run", side_effect=fake_run):
        out = backend._exec(["cat", "/workspace/state/activity.jsonl"])

    assert out == "hello\n"
    assert captured["cmd"] == [
        "docker", "compose", "-f", str(compose_file),
        "exec", "-T", "conductor",
        "cat", "/workspace/state/activity.jsonl",
    ]
    # Compose context: cwd is project, CODEBAND_PROJECT_DIR matches.
    assert captured["cwd"] == str(project)
    assert captured["env"]["CODEBAND_PROJECT_DIR"] == str(project)


def test_docker_backend_list_worktrees_uses_container_paths(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    config = _make_config(workspace_path="/workspace", mode=DeploymentMode.DISTRIBUTED)
    backend = SharedComposeBackend(
        config=config, project_dir=project, compose_file=compose_file,
    )

    candidates = backend.list_worktrees()
    assert candidates["coder-claude_sdk-0"] == Path(
        "/workspace/worktrees/coder-claude_sdk-0"
    )
    assert candidates["mergemaster"] == Path("/workspace/worktrees/mergemaster")


def test_docker_backend_reads_activity_via_exec(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    config = _make_config(workspace_path="/workspace", mode=DeploymentMode.DISTRIBUTED)
    backend = SharedComposeBackend(
        config=config, project_dir=project, compose_file=compose_file,
    )

    fake_text = json.dumps({
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": "AGENT_START",
        "agent": "conductor",
        "summary": "started",
        "details": None,
    }) + "\n"

    def fake_run(cmd, **kwargs):
        # Verify it's hitting `cat /workspace/state/activity.jsonl`.
        assert "cat" in cmd
        assert "/workspace/state/activity.jsonl" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=fake_text, stderr="")

    with patch("codeband.shell.fs.subprocess.run", side_effect=fake_run):
        events = backend.read_activity_events()

    assert len(events) == 1
    assert events[0].event_type == "AGENT_START"


# ─── Factory ───────────────────────────────────────────────────────────────


def test_make_backend_picks_local_for_local_mode(tmp_path: Path):
    config = _make_config(workspace_path=str(tmp_path / ".codeband"))
    backend = make_backend(config, tmp_path)
    assert isinstance(backend, LocalBackend)


def test_make_backend_picks_compose_for_distributed_mode(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    docker_dir = project / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n")

    config = _make_config(
        workspace_path=str(project / ".codeband"),
        mode=DeploymentMode.DISTRIBUTED,
    )
    backend = make_backend(config, project)
    assert isinstance(backend, SharedComposeBackend)
    assert backend.service == "conductor"


def test_make_backend_attach_with_running_stack_picks_compose_backend(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    docker_dir = project / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n")

    # workspace.mode is the *default* (local) — what cb up looks like
    # to the post-exec shell when the user hasn't edited their yaml.
    config = _make_config(workspace_path=str(project / ".codeband"))

    captured = {}

    def fake_run(cmd, **kwargs):
        # Simulate `docker compose ps --status running --quiet` returning
        # one container ID — i.e., a stack is up.
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

    with patch("codeband.shell.fs.subprocess.run", side_effect=fake_run):
        backend = make_backend(config, project, attach=True)

    assert isinstance(backend, SharedComposeBackend)
    # The probe must run with project context too.
    assert captured["cwd"] == str(project)
    assert captured["env"]["CODEBAND_PROJECT_DIR"] == str(project)


def test_make_backend_attach_no_stack_running_falls_back_to_local(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    docker_dir = project / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n")
    config = _make_config(workspace_path=str(project / ".codeband"))

    def fake_run(cmd, **kwargs):
        # Empty stdout → no containers running.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("codeband.shell.fs.subprocess.run", side_effect=fake_run):
        backend = make_backend(config, project, attach=True)

    assert isinstance(backend, LocalBackend)


def test_make_backend_attach_no_compose_file_falls_back_to_local(tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    # No docker/ directory, no docker-compose.yml anywhere.
    config = _make_config(workspace_path=str(project / ".codeband"))
    backend = make_backend(config, project, attach=True)
    assert isinstance(backend, LocalBackend)


def test_make_backend_standalone_local_mode_ignores_running_stack(tmp_path: Path):
    """A local-mode shell should NOT pick the compose backend even if a
    stack happens to be running — that would shadow host worktrees."""
    project = tmp_path / "proj"
    project.mkdir()
    docker_dir = project / "docker"
    docker_dir.mkdir()
    (docker_dir / "docker-compose.yml").write_text("services: {}\n")
    config = _make_config(workspace_path=str(project / ".codeband"))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

    with patch("codeband.shell.fs.subprocess.run", side_effect=fake_run):
        backend = make_backend(config, project, attach=False)

    assert isinstance(backend, LocalBackend)


# ─── helpers ───────────────────────────────────────────────────────────────


def _make_config(*, workspace_path: str, mode: DeploymentMode = DeploymentMode.LOCAL):
    return CodebandConfig(
        repo=RepoConfig(url="https://example.com/r.git", branch="main"),
        workspace=WorkspaceConfig(path=workspace_path, mode=mode),
        agents=AgentsConfig(
            coders=FrameworkPool(
                claude_sdk=PoolEntry(count=1, model="claude-sonnet-4-6"),
                codex=PoolEntry(count=0),
            ),
        ),
    )


def _git(*args, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _git_config(repo: Path) -> None:
    _git("config", "user.email", "t@t.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
