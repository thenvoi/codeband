"""Tests for codeband.orchestration.kickoff module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from codeband.orchestration.kickoff import (
    _format_task_status,
    _parse_envelope,
    _truncate_task_name,
)


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


@dataclass
class FakeListResponse:
    data: list


@dataclass
class FakeMemory:
    """Mimics Band.ai memory objects returned by list_agent_memories."""

    content: str
    thought: str = ""
    updated_at: str = ""
    inserted_at: str = ""


# ---------------------------------------------------------------------------
# _parse_envelope
# ---------------------------------------------------------------------------

class TestParseEnvelope:
    """Tests for parsing protocol state envelope content strings."""

    def test_code_review_with_all_fields(self):
        content = (
            "protocol code_review cid cr_5_r1 pr 5 round 1 "
            "state findings_posted risk low from reviewer to player-0 All checks pass"
        )
        env = _parse_envelope(content)
        assert env["protocol"] == "code_review"
        assert env["cid"] == "cr_5_r1"
        assert env["pr"] == 5
        assert env["round"] == 1
        assert env["state"] == "findings_posted"
        assert env["risk"] == "low"
        assert env["from"] == "reviewer"
        assert env["to"] == "player-0"
        assert env["summary"] == "All checks pass"

    def test_plan_without_pr_or_round(self):
        content = "protocol plan cid plan_r1 state ready from planner to conductor README badges"
        env = _parse_envelope(content)
        assert env["protocol"] == "plan"
        assert env["cid"] == "plan_r1"
        assert env["state"] == "ready"
        assert env["summary"] == "README badges"
        assert "pr" not in env
        assert "round" not in env
        assert "risk" not in env

    def test_plan_review_approved(self):
        content = (
            "protocol plan_review cid plr_r1 state approved "
            "from plan_reviewer to conductor Looks good"
        )
        env = _parse_envelope(content)
        assert env["protocol"] == "plan_review"
        assert env["state"] == "approved"
        assert env["summary"] == "Looks good"

    def test_merge_conflict_envelope(self):
        content = (
            "protocol merge_conflict cid mc_8_r1 pr 8 round 1 "
            "state initiated from mergemaster to player-0 conflicting files"
        )
        env = _parse_envelope(content)
        assert env["protocol"] == "merge_conflict"
        assert env["pr"] == 8
        assert env["state"] == "initiated"

    def test_unparseable_returns_empty(self):
        assert _parse_envelope("random text") == {}
        assert _parse_envelope("") == {}


# ---------------------------------------------------------------------------
# _format_task_status
# ---------------------------------------------------------------------------

class TestTruncateTaskName:
    """Tests for _truncate_task_name."""

    def test_short_name_unchanged(self):
        assert _truncate_task_name("Add README badges") == "Add README badges"

    def test_splits_at_sentence_boundary(self):
        name = "Add badges to README.md. Single phase, player-0, branch foo"
        assert _truncate_task_name(name) == "Add badges to README.md"

    def test_preserves_filenames_with_dots(self):
        name = "Add shields.io badges to README.md in repo"
        assert _truncate_task_name(name) == "Add shields.io badges to README.md in repo"

    def test_caps_at_max_length(self):
        long_name = "A" * 100
        result = _truncate_task_name(long_name, max_len=60)
        assert len(result) == 60
        assert result.endswith("\u2026")

    def test_strips_leading_em_dash(self):
        assert _truncate_task_name("\u2014 Add badges") == "Add badges"

    def test_strips_leading_hyphen(self):
        assert _truncate_task_name("- Add badges") == "Add badges"


class TestFormatTaskStatus:
    """Tests for the task-level pipeline status view."""

    def test_full_pipeline_single_pr(self):
        """Plan + plan review + code review → task name header, full pipeline on PR line."""
        mems = [
            FakeMemory(
                content=(
                    "protocol plan cid plan_r1 state ready "
                    "from planner to conductor README badges"
                ),
                updated_at="2026-04-11T14:55:15",
            ),
            FakeMemory(
                content=(
                    "protocol plan_review cid plr_r1 state approved "
                    "from plan_reviewer to conductor OK"
                ),
                updated_at="2026-04-11T14:55:45",
            ),
            FakeMemory(
                content=(
                    "protocol code_review cid cr_5_r1 pr 5 round 1 "
                    "state findings_posted risk low "
                    "from reviewer to player-0 clean"
                ),
                updated_at="2026-04-11T14:57:23",
            ),
        ]
        output = _format_task_status(mems)
        lines = output.split("\n")
        # Header: quoted task name
        assert '"README badges"' in lines[0]
        # PR pipeline includes all stages left to right
        pr_line = lines[1]
        assert "PR #5" in pr_line
        assert "plan \u2713" in pr_line
        assert "plan review \u2713" in pr_line
        assert "coded \u2713" in pr_line
        assert "review \u2713" in pr_line
        assert "low risk" in pr_line
        # Full pipeline order: plan before coded before review
        assert pr_line.index("plan") < pr_line.index("coded") < pr_line.index("low risk")

    def test_plan_only_early_stage(self):
        """Only a plan envelope — task-level stages shown, no PR lines."""
        mems = [
            FakeMemory(
                content=(
                    "protocol plan cid plan_r1 state ready "
                    "from planner to conductor Add caching"
                ),
                updated_at="2026-04-11T14:55:15",
            ),
        ]
        output = _format_task_status(mems)
        assert '"Add caching"' in output
        assert "plan \u2713" in output
        assert "PR #" not in output

    def test_plan_review_needs_revision(self):
        """Plan review rejected — shows failure on pipeline."""
        mems = [
            FakeMemory(
                content=(
                    "protocol plan cid plan_r1 state ready "
                    "from planner to conductor Refactor API"
                ),
            ),
            FakeMemory(
                content=(
                    "protocol plan_review cid plr_r1 state needs_revision "
                    "from plan_reviewer to conductor scope too broad"
                ),
            ),
        ]
        output = _format_task_status(mems)
        assert "plan review \u2717 needs_revision" in output

    def test_multiple_prs_grouped_separately(self):
        """Two PRs — each gets its own pipeline line with all stages."""
        mems = [
            FakeMemory(
                content=(
                    "protocol plan cid plan_r1 state ready "
                    "from planner to conductor Fix auth"
                ),
            ),
            FakeMemory(
                content=(
                    "protocol code_review cid cr_8_r1 pr 8 round 1 "
                    "state findings_posted risk low "
                    "from reviewer to player-0 clean"
                ),
            ),
            FakeMemory(
                content=(
                    "protocol code_review cid cr_9_r1 pr 9 round 2 "
                    "state needs_revision risk medium "
                    "from reviewer to player-1 issues found"
                ),
            ),
        ]
        output = _format_task_status(mems)
        assert "PR #8" in output
        assert "PR #9" in output
        assert output.index("PR #8") < output.index("PR #9")
        # Both PRs include plan stage + coded + their review
        pr8_line = [line for line in output.split("\n") if "PR #8" in line][0]
        assert "plan \u2713" in pr8_line
        assert "coded \u2713" in pr8_line
        # PR #9 shows round 2
        pr9_line = [line for line in output.split("\n") if "PR #9" in line][0]
        assert "round 2" in pr9_line
        assert "medium risk" in pr9_line

    def test_empty_protocols(self):
        """No envelopes → empty string."""
        assert _format_task_status([]) == ""

    def test_unparseable_envelopes_skipped(self):
        """Envelopes that don't match the format are silently skipped."""
        mems = [
            FakeMemory(content="some random memory content"),
            FakeMemory(
                content=(
                    "protocol plan cid plan_r1 state ready "
                    "from planner to conductor Real task"
                ),
            ),
        ]
        output = _format_task_status(mems)
        assert "Real task" in output


class TestKickoffConfig:
    """Tests for kickoff configuration validation."""

    def test_agent_config_required_keys(self, sample_agent_config):
        """Agent config has all required pool + singleton keys."""
        required = [
            "conductor", "mergemaster",
            "coder-claude_sdk-0", "coder-codex-0",
            "reviewer-claude_sdk-0", "reviewer-codex-0",
            "planner-claude_sdk-0", "plan_reviewer-codex-0",
        ]
        for key in required:
            creds = sample_agent_config.get(key)
            assert creds.agent_id
            assert creds.api_key

    def test_missing_agent_raises(self, sample_agent_config):
        """Missing agent key raises KeyError."""
        with pytest.raises(KeyError, match="coder-claude_sdk-99"):
            sample_agent_config.get("coder-claude_sdk-99")


def _make_clients(human_client, agent_map):
    """Build a mock AsyncRestClient factory.

    agent_map: {api_key: (agent_id, agent_name)}
    """
    agent_clients = {}
    for api_key, (agent_id, agent_name) in agent_map.items():
        c = AsyncMock()
        c.agent_api_identity.get_agent_me.return_value = FakeIdentityResponse(
            data=FakeIdentity(id=agent_id, name=agent_name)
        )
        c.agent_api_chats.list_agent_chats.return_value = FakeListResponse(data=[])
        agent_clients[api_key] = c

    def factory(api_key, base_url=None):
        if api_key == "human-key":
            return human_client
        return agent_clients[api_key]

    return factory


class TestSendTask:
    """Tests for send_task using human API."""

    @pytest.mark.asyncio
    async def test_requires_band_api_key(self, sample_config, sample_agent_config, tmp_path):
        """send_task raises ValueError when BAND_API_KEY is missing."""
        from codeband.orchestration.kickoff import send_task

        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")

        env = {k: v for k, v in os.environ.items() if k != "BAND_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="BAND_API_KEY"):
                await send_task(sample_config, tmp_path, "test task")

    @pytest.mark.asyncio
    async def test_human_creates_room_and_sends_message(
        self, sample_config, sample_agent_config, tmp_path
    ):
        """send_task uses human API to create room and send task message."""
        from codeband.orchestration import kickoff

        # Registration resolves the default verdict list (includes 'verify'),
        # so the config must carry an executable verify command.
        sample_config.agents.handoff_verify_command = "true"
        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")

        human_client = AsyncMock()
        human_client.human_api_chats.create_my_chat_room.return_value = FakeRoomResponse(
            data=FakeRoom(id="room-123")
        )
        # Owner resolution is required: send_task aborts without a profile id.
        human_client.human_api_profile.get_my_profile.return_value = FakeIdentityResponse(
            data=FakeIdentity(id="owner-1", name="Initiator")
        )
        human_client.human_api_participants.add_my_chat_participant = AsyncMock()
        human_client.human_api_messages.send_my_chat_message = AsyncMock()
        human_client.human_api_chats.list_my_chats.return_value = FakeListResponse(data=[])

        factory = _make_clients(human_client, {
            "key-cond":  ("cond-0", "Conductor"),
            "key-mm":    ("mm-0",   "Mergemaster"),
            "key-pl-c0": ("pl-c-0", "Planner-Claude-0"),
            "key-pr-x0": ("pr-x-0", "Plan-Reviewer-Codex-0"),
            "key-co-c0": ("co-c-0", "Coder-Claude-0"),
            "key-co-x0": ("co-x-0", "Coder-Codex-0"),
            "key-re-c0": ("re-c-0", "Reviewer-Claude-0"),
            "key-re-x0": ("re-x-0", "Reviewer-Codex-0"),
        })

        with patch.dict(os.environ, {"BAND_API_KEY": "human-key"}):
            # Patch at the source module level so the deferred import picks it up
            import thenvoi_rest
            original = thenvoi_rest.AsyncRestClient
            thenvoi_rest.AsyncRestClient = factory
            try:
                await kickoff.send_task(sample_config, tmp_path, "implement feature X")
            finally:
                thenvoi_rest.AsyncRestClient = original

        # Human created the room
        human_client.human_api_chats.create_my_chat_room.assert_called_once()

        # Lazy invites: only the Conductor is added at room creation. Every
        # other agent is invited later by the inviting agent (Conductor →
        # Planner; Coder → Reviewer; …) via thenvoi_add_participant once the
        # workflow needs them.
        human_client.human_api_participants.add_my_chat_participant.assert_called_once()
        add_call = human_client.human_api_participants.add_my_chat_participant.call_args
        assert add_call[0][0] == "room-123"
        assert add_call[1]["participant"].participant_id == "cond-0"

        # Human sent the task message
        human_client.human_api_messages.send_my_chat_message.assert_called_once()
        call_args = human_client.human_api_messages.send_my_chat_message.call_args
        assert call_args[0][0] == "room-123"
        msg = call_args[1]["message"]
        assert "implement feature X" in msg.content
        assert "Conductor" in msg.content

    @pytest.mark.asyncio
    async def test_task_message_includes_repo_info(
        self, sample_config, sample_agent_config, tmp_path
    ):
        """Task message includes repo URL and branch for specificity."""
        from codeband.orchestration import kickoff

        # Registration resolves the default verdict list (includes 'verify'),
        # so the config must carry an executable verify command.
        sample_config.agents.handoff_verify_command = "true"
        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")

        human_client = AsyncMock()
        human_client.human_api_chats.create_my_chat_room.return_value = FakeRoomResponse(
            data=FakeRoom(id="room-456")
        )
        # Owner resolution is required: send_task aborts without a profile id.
        human_client.human_api_profile.get_my_profile.return_value = FakeIdentityResponse(
            data=FakeIdentity(id="owner-1", name="Initiator")
        )
        human_client.human_api_participants.add_my_chat_participant = AsyncMock()
        human_client.human_api_messages.send_my_chat_message = AsyncMock()
        human_client.human_api_chats.list_my_chats.return_value = FakeListResponse(data=[])

        factory = _make_clients(human_client, {
            "key-cond":  ("cond-0", "Conductor"),
            "key-mm":    ("mm-0",   "Mergemaster"),
            "key-pl-c0": ("pl-c-0", "Planner-Claude-0"),
            "key-pr-x0": ("pr-x-0", "Plan-Reviewer-Codex-0"),
            "key-co-c0": ("co-c-0", "Coder-Claude-0"),
            "key-co-x0": ("co-x-0", "Coder-Codex-0"),
            "key-re-c0": ("re-c-0", "Reviewer-Claude-0"),
            "key-re-x0": ("re-x-0", "Reviewer-Codex-0"),
        })

        with patch.dict(os.environ, {"BAND_API_KEY": "human-key"}):
            import thenvoi_rest
            original = thenvoi_rest.AsyncRestClient
            thenvoi_rest.AsyncRestClient = factory
            try:
                await kickoff.send_task(sample_config, tmp_path, "add logging")
            finally:
                thenvoi_rest.AsyncRestClient = original

        msg = human_client.human_api_messages.send_my_chat_message.call_args[1]["message"]
        assert "https://github.com/example/repo.git" in msg.content
        assert "branch: main" in msg.content


class TestResetActiveRoom:
    """Tests for reset_active_room — Band.ai cleanup helper behind `cb reset`."""

    @pytest.mark.asyncio
    async def test_no_room_file_is_noop(self, sample_config, tmp_path):
        """Returns None when .codeband_room is missing — no API calls made."""
        from codeband.orchestration.kickoff import reset_active_room
        result = await reset_active_room(sample_config, tmp_path)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_room_file_deletes_and_noops(self, sample_config, tmp_path):
        """Empty pointer file is cleaned up; returns None; no API calls."""
        from codeband.orchestration.kickoff import reset_active_room
        (tmp_path / ".codeband_room").write_text("", encoding="utf-8")
        result = await reset_active_room(sample_config, tmp_path)
        assert result is None
        assert not (tmp_path / ".codeband_room").exists()

    @pytest.mark.asyncio
    async def test_removes_all_agents_and_deletes_pointer(
        self, sample_config, sample_agent_config, tmp_path,
    ):
        """Each agent's client calls remove_agent_chat_participant; pointer is deleted."""
        from codeband.orchestration import kickoff

        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")
        (tmp_path / ".codeband_room").write_text("stale-room-id", encoding="utf-8")

        factory = _make_clients(AsyncMock(), {
            creds.api_key: (creds.agent_id, key)
            for key, creds in sample_agent_config.agents.items()
        })

        import thenvoi_rest
        original = thenvoi_rest.AsyncRestClient
        thenvoi_rest.AsyncRestClient = factory
        try:
            result = await kickoff.reset_active_room(sample_config, tmp_path)
        finally:
            thenvoi_rest.AsyncRestClient = original

        assert result == "stale-room-id"
        assert not (tmp_path / ".codeband_room").exists()

        # Every agent's client should have called remove_agent_chat_participant
        # exactly once with (room_id, agent_id).
        for key, creds in sample_agent_config.agents.items():
            client = factory(creds.api_key)
            client.agent_api_participants.remove_agent_chat_participant.assert_called_once_with(
                "stale-room-id", creds.agent_id,
            )

    @pytest.mark.asyncio
    async def test_reset_clears_both_pointer_locations(
        self, sample_config, sample_agent_config, tmp_path,
    ):
        """Transition-era state: pointer present at BOTH the canonical
        (workspace/state/) and legacy (project-dir) locations — reset must
        clear both so neither can resurrect a dead room."""
        from codeband.orchestration import kickoff

        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")
        canonical = tmp_path / "workspace" / "state" / ".codeband_room"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("stale-room-id", encoding="utf-8")
        (tmp_path / ".codeband_room").write_text("stale-room-id", encoding="utf-8")

        factory = _make_clients(AsyncMock(), {
            creds.api_key: (creds.agent_id, key)
            for key, creds in sample_agent_config.agents.items()
        })

        import thenvoi_rest
        original = thenvoi_rest.AsyncRestClient
        thenvoi_rest.AsyncRestClient = factory
        try:
            result = await kickoff.reset_active_room(sample_config, tmp_path)
        finally:
            thenvoi_rest.AsyncRestClient = original

        assert result == "stale-room-id"
        assert not canonical.exists()
        assert not (tmp_path / ".codeband_room").exists()

    @pytest.mark.asyncio
    async def test_agent_removal_errors_are_swallowed(
        self, sample_config, sample_agent_config, tmp_path,
    ):
        """Agents that can't leave the room (already removed, 404, etc.) don't abort reset."""
        from codeband.orchestration import kickoff

        sample_agent_config.to_yaml(tmp_path / "agent_config.yaml")
        (tmp_path / ".codeband_room").write_text("ghost-room", encoding="utf-8")

        factory = _make_clients(AsyncMock(), {
            creds.api_key: (creds.agent_id, key)
            for key, creds in sample_agent_config.agents.items()
        })
        # Make the conductor's removal raise; the rest should still be called
        # and the pointer still deleted.
        cond_client = factory("key-cond")
        cond_client.agent_api_participants.remove_agent_chat_participant.side_effect = (
            RuntimeError("404 Not Found")
        )

        import thenvoi_rest
        original = thenvoi_rest.AsyncRestClient
        thenvoi_rest.AsyncRestClient = factory
        try:
            result = await kickoff.reset_active_room(sample_config, tmp_path)
        finally:
            thenvoi_rest.AsyncRestClient = original

        assert result == "ghost-room"
        assert not (tmp_path / ".codeband_room").exists()
        # Mergemaster removal still happened despite conductor failure
        mm_client = factory("key-mm")
        mm_client.agent_api_participants.remove_agent_chat_participant.assert_called_once()
