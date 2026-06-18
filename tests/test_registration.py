"""Tests for the atomic task-registration primitive (initiator-as-owner, part 1).

Covers the ``register_task`` contract (row-first, required owner, supersede
semantics, repair of the historical half-states), the ``send_task`` reorder
(owner required; registration strictly before the task message), and the
``cb register-task`` CLI wrapper. LLM-free: real sqlite + tmp dirs, mocked
Band clients only where ``send_task`` needs them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from codeband.cli import cli as cb_cli
from codeband.config import AgentsConfig, VerifiersConfig
from codeband.state import StateStore
from codeband.state.registration import register_task


def _gated_agents(**overrides) -> AgentsConfig:
    """An AgentsConfig whose default verdict list is executable.

    When ``handoff_verify_command`` is configured, the default
    ``required_verdicts`` resolution includes ``verify``. Tests that exercise
    the fully gated registration mechanics use this config so they pass the
    verdict gate.

    Verifiers are pinned INERT by default so the resolved snapshot stays the
    ``verify``/``review`` pair these mechanics tests assert (the active product
    default would couple in ``verify_acceptance``); the acceptance coupling has
    its own tests in test_verifier_acceptance.py. Callers may override.
    """
    overrides.setdefault("verifiers", VerifiersConfig())
    return AgentsConfig(handoff_verify_command="true", **overrides)


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """A StateStore backed by an isolated DB under tmp_path."""
    return StateStore(tmp_path / "state" / "orchestration.db")


def _pointer(project_dir: Path) -> Path:
    """Canonical pointer location for the primitive tests' store fixture.

    The ``store`` fixture lives at ``tmp_path/state/orchestration.db``, and
    the pointer now sits next to that DB.
    """
    return project_dir / "state" / ".codeband_room"


def _ws_pointer(project_dir: Path) -> Path:
    """Canonical pointer for the send_task/CLI tests (sample_config uses
    ``workspace.path: workspace`` → DB at ``workspace/state/``)."""
    return project_dir / "workspace" / "state" / ".codeband_room"


def _legacy_pointer(project_dir: Path) -> Path:
    """The pre-relocation pointer location (read-fallback only)."""
    return project_dir / ".codeband_room"


def _task_row_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    finally:
        conn.close()
    return count


# ---------------------------------------------------------------------------
# register_task — the primitive's contract
# ---------------------------------------------------------------------------

class TestRegisterTask:
    def test_fresh_registration_writes_row_and_pointer(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        result = register_task(
            room_id="room-1",
            description="do the thing",
            owner_id="owner-7",
            owner_handle="yoni/claude-abc",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )

        assert result.outcome == "registered"
        assert result.superseded_task_id is None
        task = store.get_task("room-1")
        assert task is not None
        assert task.description == "do the thing"
        assert task.status == "active"
        assert task.owner_id == "owner-7"
        assert task.owner_handle == "yoni/claude-abc"
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-1"

    @pytest.mark.parametrize("bad_owner", ["", None])
    def test_missing_owner_raises_and_writes_nothing(
        self, tmp_path: Path, store: StateStore, bad_owner
    ) -> None:
        with pytest.raises(ValueError, match="owner_id"):
            register_task(
                room_id="room-1",
                description="do the thing",
                owner_id=bad_owner,
                agents=_gated_agents(),
                project_dir=tmp_path,
                store=store,
            )

        assert store.get_task("room-1") is None
        assert _task_row_count(store.db_path) == 0
        assert not _pointer(tmp_path).exists()

    def test_reregister_same_room_updates_owner_only(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        register_task(
            room_id="room-1",
            description="original description",
            owner_id="owner-a",
            owner_handle="handle-a",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        result = register_task(
            room_id="room-1",
            description="DIFFERENT description must be ignored",
            owner_id="owner-b",
            owner_handle="handle-b",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )

        assert result.outcome == "re-registered"
        assert _task_row_count(store.db_path) == 1
        task = store.get_task("room-1")
        assert task is not None
        assert task.owner_id == "owner-b"
        assert task.owner_handle == "handle-b"
        # Description and status are deliberately untouched on re-registration.
        assert task.description == "original description"
        assert task.status == "active"
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-1"

    def test_new_room_supersedes_active_task(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        register_task(
            room_id="room-old",
            description="old task",
            owner_id="owner-a",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        result = register_task(
            room_id="room-new",
            description="new task",
            owner_id="owner-b",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )

        assert result.outcome == "superseded"
        assert result.superseded_task_id == "room-old"
        old = store.get_task("room-old")
        new = store.get_task("room-new")
        assert old is not None and old.status == "superseded"
        assert new is not None and new.status == "active"
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-new"

    def test_pointer_without_row_is_overwritten_cleanly(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        # The /codeband broken state (H2): a pointer that resolves to no row.
        _pointer(tmp_path).write_text("ghost-room", encoding="utf-8")

        result = register_task(
            room_id="room-1",
            description="real task",
            owner_id="owner-7",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )

        # Nothing to supersede — the dangling pointer was invalid state.
        assert result.outcome == "registered"
        assert result.superseded_task_id is None
        assert store.get_task("ghost-room") is None
        assert store.get_task("room-1") is not None
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-1"

    def test_fresh_registration_writes_new_location_only(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        """The pointer lives next to the DB; nothing writes the legacy
        project-dir location anymore."""
        register_task(
            room_id="room-1",
            description="task",
            owner_id="owner-7",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-1"
        assert not _legacy_pointer(tmp_path).exists()

    def test_legacy_pointer_is_read_for_supersede_detection(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        """A pre-relocation install: the active task is known only via the
        legacy pointer. Registering a new room must still supersede it."""
        register_task(
            room_id="room-old",
            description="old",
            owner_id="owner-a",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        # Simulate the pre-relocation on-disk state: pointer at the legacy
        # location only.
        _legacy_pointer(tmp_path).write_text("room-old", encoding="utf-8")
        _pointer(tmp_path).unlink()

        result = register_task(
            room_id="room-new",
            description="new",
            owner_id="owner-b",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        assert result.outcome == "superseded"
        assert result.superseded_task_id == "room-old"

    def test_reregistration_migrates_legacy_pointer(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        """Re-registering writes the new location and removes the legacy
        file — the migration path for pre-relocation installs."""
        register_task(
            room_id="room-1",
            description="task",
            owner_id="owner-a",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        _legacy_pointer(tmp_path).write_text("room-1", encoding="utf-8")
        _pointer(tmp_path).unlink()

        register_task(
            room_id="room-1",
            description="task",
            owner_id="owner-b",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-1"
        assert not _legacy_pointer(tmp_path).exists()

    def test_row_without_pointer_restores_pointer(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        # H1: the row exists but the pointer write never happened.
        register_task(
            room_id="room-1",
            description="task",
            owner_id="owner-a",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )
        _pointer(tmp_path).unlink()

        result = register_task(
            room_id="room-1",
            description="task",
            owner_id="owner-b",
            agents=_gated_agents(),
            project_dir=tmp_path,
            store=store,
        )

        assert result.outcome == "re-registered"
        assert _pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-1"
        task = store.get_task("room-1")
        assert task is not None
        assert task.owner_id == "owner-b"
        assert _task_row_count(store.db_path) == 1


# ---------------------------------------------------------------------------
# read_room_pointer — dual-location read (canonical next-to-DB + legacy)
# ---------------------------------------------------------------------------

class TestReadRoomPointer:
    def test_reads_canonical_location_first(self, tmp_path: Path) -> None:
        from codeband.state.registration import read_room_pointer

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / ".codeband_room").write_text("room-new", encoding="utf-8")
        _legacy_pointer(tmp_path).write_text("room-stale", encoding="utf-8")
        assert read_room_pointer(tmp_path, state_dir) == "room-new"

    def test_legacy_fallback_resolves_with_deprecation_warning(
        self, tmp_path: Path, capsys
    ) -> None:
        from codeband.state.registration import read_room_pointer

        state_dir = tmp_path / "state"
        _legacy_pointer(tmp_path).write_text("room-legacy", encoding="utf-8")
        assert read_room_pointer(tmp_path, state_dir) == "room-legacy"
        err = capsys.readouterr().err
        assert "legacy" in err
        assert str(_legacy_pointer(tmp_path)) in err
        assert str(state_dir / ".codeband_room") in err

    def test_legacy_fallback_warning_is_suppressible(
        self, tmp_path: Path, capsys
    ) -> None:
        from codeband.state.registration import read_room_pointer

        _legacy_pointer(tmp_path).write_text("room-legacy", encoding="utf-8")
        assert (
            read_room_pointer(tmp_path, tmp_path / "state", warn_legacy=False)
            == "room-legacy"
        )
        assert capsys.readouterr().err == ""

    def test_no_pointer_anywhere_returns_none(self, tmp_path: Path) -> None:
        from codeband.state.registration import read_room_pointer

        assert read_room_pointer(tmp_path, tmp_path / "state") is None

    def test_cb_phase_resolves_task_via_legacy_pointer(
        self, tmp_path: Path, store: StateStore, capsys
    ) -> None:
        """End-to-end through cb-phase's resolver: a pre-relocation repo
        (legacy pointer only) still resolves, with the deprecation warning."""
        from codeband.cli.handoff import _resolve_task_id

        store.create_task(task_id="room-1", description="t", room_id="room-1")
        # The store fixture's pointer dir is tmp_path/state — leave it empty.
        _legacy_pointer(tmp_path).write_text("room-1", encoding="utf-8")

        task_id, error = _resolve_task_id(tmp_path, store, None)
        assert (task_id, error) == ("room-1", None)
        assert "legacy" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# required_verdicts — resolution, fail-loud validation, snapshot (Stage-2)
# ---------------------------------------------------------------------------

class TestRequiredVerdicts:
    def _register(self, tmp_path: Path, store: StateStore, agents, room: str = "room-1"):
        return register_task(
            room_id=room,
            description="task",
            owner_id="owner-1",
            agents=agents,
            project_dir=tmp_path,
            store=store,
        )

    def test_absent_key_snapshots_default_list(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        # required_verdicts not set (None) → resolves to the full default.
        self._register(tmp_path, store, _gated_agents())
        task = store.get_task("room-1")
        assert task is not None
        assert task.required_verdicts == ["verify", "review"]

    def test_explicit_list_snapshotted_verbatim(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        agents = _gated_agents(required_verdicts=["review", "verify"])
        self._register(tmp_path, store, agents)
        task = store.get_task("room-1")
        assert task is not None
        assert task.required_verdicts == ["review", "verify"]  # order preserved

    def test_empty_list_without_flag_fails_and_writes_nothing(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        agents = _gated_agents(required_verdicts=[])
        with pytest.raises(ValueError, match="allow_ungated_merge"):
            self._register(tmp_path, store, agents)
        assert store.get_task("room-1") is None
        assert _task_row_count(store.db_path) == 0
        assert not _pointer(tmp_path).exists()

    def test_empty_list_with_ugly_flag_snapshots_empty(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        agents = _gated_agents(required_verdicts=[], allow_ungated_merge=True)
        result = self._register(tmp_path, store, agents)
        assert result.outcome == "registered"
        task = store.get_task("room-1")
        assert task is not None
        assert task.required_verdicts == []

    def test_fresh_default_without_verify_command_registers_without_verify(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        agents = AgentsConfig()  # handoff_verify_command unset
        result = self._register(tmp_path, store, agents)

        assert result.outcome == "registered"
        task = store.get_task("room-1")
        assert task is not None
        assert "verify" not in task.required_verdicts
        assert task.required_verdicts == ["review", "verify_acceptance"]

    def test_explicit_verify_without_command_fails_at_seed(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        agents = AgentsConfig(required_verdicts=["verify", "review"])
        with pytest.raises(ValueError, match="handoff_verify_command"):
            self._register(tmp_path, store, agents)
        assert _task_row_count(store.db_path) == 0
        assert not _pointer(tmp_path).exists()

    def test_unknown_verdict_fails_naming_the_entry(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        agents = _gated_agents(required_verdicts=["verify", "vibes"])
        with pytest.raises(ValueError, match="'vibes'"):
            self._register(tmp_path, store, agents)
        assert _task_row_count(store.db_path) == 0
        assert not _pointer(tmp_path).exists()

    def test_reregister_refreshes_snapshot_from_current_config(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        self._register(tmp_path, store, _gated_agents())
        assert store.get_task("room-1").required_verdicts == ["verify", "review"]

        # Config changed between registrations — a re-register of the same
        # room re-resolves and overwrites the snapshot (consistent with
        # re-register-updates-owner).
        result = self._register(
            tmp_path, store, _gated_agents(required_verdicts=["review"])
        )
        assert result.outcome == "re-registered"
        task = store.get_task("room-1")
        assert task.required_verdicts == ["review"]
        # Description/status remain untouched by re-registration.
        assert task.status == "active"

    def test_superseding_room_gets_its_own_fresh_snapshot(
        self, tmp_path: Path, store: StateStore
    ) -> None:
        self._register(tmp_path, store, _gated_agents(), room="room-old")
        result = self._register(
            tmp_path,
            store,
            _gated_agents(required_verdicts=["review"]),
            room="room-new",
        )
        assert result.outcome == "superseded"
        old = store.get_task("room-old")
        new = store.get_task("room-new")
        # The superseded row keeps its original snapshot; the new row carries
        # the list resolved from current config.
        assert old.status == "superseded"
        assert old.required_verdicts == ["verify", "review"]
        assert new.required_verdicts == ["review"]


# ---------------------------------------------------------------------------
# send_task — owner required, registration strictly before the task message
# ---------------------------------------------------------------------------

@dataclass
class FakeIdentity:
    id: str
    name: str


@dataclass
class FakeIdentityResponse:
    data: FakeIdentity


@dataclass
class FakeRoom:
    id: str


@dataclass
class FakeRoomResponse:
    data: FakeRoom


def _make_human_client(room_id: str) -> AsyncMock:
    human_client = AsyncMock()
    human_client.human_api_chats.create_my_chat_room.return_value = FakeRoomResponse(
        data=FakeRoom(id=room_id)
    )
    human_client.human_api_profile.get_my_profile.return_value = FakeIdentityResponse(
        data=FakeIdentity(id="owner-1", name="Initiator")
    )
    return human_client


def _make_client_factory(human_client: AsyncMock):
    """AsyncRestClient replacement: human key → human client, else conductor."""
    conductor_client = AsyncMock()
    conductor_client.agent_api_identity.get_agent_me.return_value = FakeIdentityResponse(
        data=FakeIdentity(id="cond-0", name="Conductor")
    )

    def factory(api_key, base_url=None):
        if api_key == "human-key":
            return human_client
        return conductor_client

    return factory


async def _run_send_task(human_client, sample_config, tmp_path: Path) -> None:
    import os

    import thenvoi_rest

    from codeband.orchestration import kickoff

    factory = _make_client_factory(human_client)
    with patch.dict(os.environ, {"BAND_API_KEY": "human-key"}):
        original = thenvoi_rest.AsyncRestClient
        thenvoi_rest.AsyncRestClient = factory
        try:
            await kickoff.send_task(sample_config, tmp_path, "implement feature X")
        finally:
            thenvoi_rest.AsyncRestClient = original


class TestSendTaskRegistration:
    @pytest.mark.asyncio
    async def test_owner_resolution_failure_aborts_before_message(
        self, sample_config, sample_agent_config, tmp_path: Path
    ) -> None:
        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")
        human_client = _make_human_client("room-123")
        human_client.human_api_profile.get_my_profile.side_effect = RuntimeError(
            "profile endpoint down"
        )

        with pytest.raises(RuntimeError, match="initiator"):
            await _run_send_task(human_client, sample_config, tmp_path)

        # Aborted loudly before any participant add or message post …
        human_client.human_api_participants.add_my_chat_participant.assert_not_called()
        human_client.human_api_messages.send_my_chat_message.assert_not_called()
        # … and before anything was registered.
        assert not _ws_pointer(tmp_path).exists()
        db_path = tmp_path / "workspace" / "state" / "orchestration.db"
        assert not db_path.exists() or _task_row_count(db_path) == 0

    @pytest.mark.asyncio
    async def test_registration_ordered_before_message_post(
        self, sample_config, sample_agent_config, tmp_path: Path, monkeypatch
    ) -> None:
        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")
        human_client = _make_human_client("room-123")

        events: list[str] = []

        from codeband.state import registration as registration_module

        real_register_task = registration_module.register_task

        def recording_register_task(**kwargs):
            events.append("register")
            return real_register_task(**kwargs)

        monkeypatch.setattr(
            registration_module, "register_task", recording_register_task
        )

        async def recording_send_message(room_id, message):
            events.append("message")
            # The pointer and the tasks row must already exist when the task
            # message (the agent-activation edge) is posted.
            assert _ws_pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-123"
            db_path = tmp_path / "workspace" / "state" / "orchestration.db"
            task = StateStore(db_path).get_task("room-123")
            assert task is not None
            assert task.owner_id == "owner-1"

        human_client.human_api_messages.send_my_chat_message = AsyncMock(
            side_effect=recording_send_message
        )

        await _run_send_task(human_client, sample_config, tmp_path)

        assert events == ["register", "message"]


# ---------------------------------------------------------------------------
# cb register-task — thin CLI wrapper
# ---------------------------------------------------------------------------

class TestRegisterTaskCli:
    def test_success_exits_zero_and_registers(self, sample_config, tmp_path: Path) -> None:
        sample_config.to_yaml(tmp_path / "codeband.yaml")

        runner = CliRunner()
        result = runner.invoke(cb_cli, [
            "register-task",
            "--room", "room-cli",
            "--owner", "owner-9",
            "--owner-handle", "yoni/peer",
            "--description", "seeded by a peer",
            "--dir", str(tmp_path),
        ])

        assert result.exit_code == 0, result.output
        assert "Registered task room-cli" in result.output
        assert _ws_pointer(tmp_path).read_text(encoding="utf-8").strip() == "room-cli"
        task = StateStore(
            tmp_path / "workspace" / "state" / "orchestration.db"
        ).get_task("room-cli")
        assert task is not None
        assert task.owner_id == "owner-9"
        assert task.owner_handle == "yoni/peer"
        assert task.status == "active"
        assert task.required_verdicts == ["review"]

    def test_missing_owner_exits_nonzero_writes_nothing(
        self, sample_config, tmp_path: Path
    ) -> None:
        sample_config.to_yaml(tmp_path / "codeband.yaml")

        runner = CliRunner()
        result = runner.invoke(cb_cli, [
            "register-task",
            "--room", "room-cli",
            "--description", "seeded by a peer",
            "--dir", str(tmp_path),
        ])

        assert result.exit_code != 0
        assert not _ws_pointer(tmp_path).exists()
        assert not (tmp_path / "workspace" / "state" / "orchestration.db").exists()

    def test_workspace_env_var_wins_over_config_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """$WORKSPACE overrides relative config.workspace.path for the store location.

        register-task must use resolve_workspace_path (the ONE shared rule) so
        Docker containers with $WORKSPACE=/workspace don't open a shadow store
        in a project-relative directory.
        """
        from codeband.config import AgentsConfig, CodebandConfig, RepoConfig, WorkspaceConfig

        workspace_dir = tmp_path / "custom_workspace"
        workspace_dir.mkdir()
        monkeypatch.setenv("WORKSPACE", str(workspace_dir))

        # Use a relative workspace.path so $WORKSPACE kicks in.
        config = CodebandConfig(
            repo=RepoConfig(url="https://github.com/example/repo.git", branch="main"),
            agents=AgentsConfig(handoff_verify_command="true"),
            workspace=WorkspaceConfig(path=".codeband"),
        )
        config.to_yaml(tmp_path / "codeband.yaml")

        runner = CliRunner()
        result = runner.invoke(cb_cli, [
            "register-task",
            "--room", "room-ws",
            "--owner", "owner-1",
            "--description", "workspace env test",
            "--dir", str(tmp_path),
        ])

        assert result.exit_code == 0, result.output
        # DB must be under $WORKSPACE, not under project_dir/.codeband/
        ws_db = workspace_dir / ".codeband" / "state" / "orchestration.db"
        assert ws_db.exists(), f"Expected DB at {ws_db}"
        task = StateStore(ws_db).get_task("room-ws")
        assert task is not None
        assert task.owner_id == "owner-1"
        # The shadow path (project-relative) must NOT have a DB.
        shadow_db = tmp_path / ".codeband" / "state" / "orchestration.db"
        assert not shadow_db.exists(), f"Shadow DB found at {shadow_db} — $WORKSPACE not honored"
