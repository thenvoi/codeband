"""Tests for ``cb room-log`` command."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from codeband.cli import _fetch_room_messages, _room_msg_to_dict, cli


# ---------------------------------------------------------------------------
# Unit: _room_msg_to_dict
# ---------------------------------------------------------------------------


def _make_msg(**kw) -> SimpleNamespace:
    defaults = {
        "sender_id": "agent-uuid",
        "message_type": "text",
        "content": "hello world",
        "inserted_at": "2026-06-13T10:00:00+00:00",
        "created_at": None,
        "createdAt": None,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestMsgToDict:
    def test_maps_known_sender(self):
        m = _make_msg(sender_id="abc")
        d = _room_msg_to_dict(m, {"abc": "conductor"})
        assert d["sender_id"] == "abc"
        assert d["sender_name"] == "conductor"

    def test_unknown_sender_falls_back_to_id(self):
        m = _make_msg(sender_id="xyz")
        d = _room_msg_to_dict(m, {})
        assert d["sender_name"] == "xyz"

    def test_prefers_created_at_over_inserted_at(self):
        m = _make_msg(created_at="2026-06-13T09:00:00+00:00", inserted_at="2026-06-13T10:00:00+00:00")
        d = _room_msg_to_dict(m, {})
        assert d["inserted_at"] == "2026-06-13T09:00:00+00:00"

    def test_falls_back_to_inserted_at(self):
        m = _make_msg(created_at=None, createdAt=None, inserted_at="2026-06-13T10:00:00+00:00")
        d = _room_msg_to_dict(m, {})
        assert d["inserted_at"] == "2026-06-13T10:00:00+00:00"

    def test_message_type_lowercased(self):
        m = _make_msg(message_type="TEXT")
        d = _room_msg_to_dict(m, {})
        assert d["message_type"] == "text"

    def test_none_content_becomes_empty_string(self):
        m = _make_msg(content=None)
        d = _room_msg_to_dict(m, {})
        assert d["content"] == ""


# ---------------------------------------------------------------------------
# Unit: _fetch_room_messages — ordering and pagination
# ---------------------------------------------------------------------------


def _make_page(*contents: tuple[str, str]) -> list:
    """Build a page of message objects from (sender_id, inserted_at) pairs."""
    return [
        _make_msg(sender_id=sid, inserted_at=ts, content=f"msg-{ts}")
        for sid, ts in contents
    ]


class TestFetchRoomMessages:
    @pytest.mark.asyncio
    async def test_returns_messages_sorted_by_timestamp(self):
        page = _make_page(
            ("a", "2026-06-13T10:02:00+00:00"),
            ("b", "2026-06-13T10:01:00+00:00"),
            ("c", "2026-06-13T10:03:00+00:00"),
        )
        rest = MagicMock()
        rest.human_api_messages.list_my_chat_messages = AsyncMock(
            return_value=SimpleNamespace(data=page)
        )

        result = await _fetch_room_messages(rest, "room-1", page_size=50)

        timestamps = [getattr(m, "inserted_at") for m in result]
        assert timestamps == sorted(timestamps)

    @pytest.mark.asyncio
    async def test_stops_pagination_when_empty_page(self):
        # Use page_size=1 so a full first page triggers a second fetch, which
        # returns empty — verifying that we stop on the empty-page signal.
        call_count = 0

        async def _paged(room_id, page=1, page_size=1):
            nonlocal call_count
            call_count += 1
            if page == 1:
                return SimpleNamespace(data=_make_page(("a", "2026-01-01T00:00:00+00:00")))
            return SimpleNamespace(data=[])

        rest = MagicMock()
        rest.human_api_messages.list_my_chat_messages = _paged

        result = await _fetch_room_messages(rest, "room-2", page_size=1)
        assert call_count == 2  # page 1 (full) + page 2 (empty → stop)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_stops_when_page_is_smaller_than_page_size(self):
        call_count = 0

        async def _paged(room_id, page=1, page_size=50):
            nonlocal call_count
            call_count += 1
            if page == 1:
                # Return 3 < page_size=5 — last page signal
                return SimpleNamespace(data=_make_page(
                    ("a", "2026-01-01T00:01:00+00:00"),
                    ("b", "2026-01-01T00:02:00+00:00"),
                    ("c", "2026-01-01T00:03:00+00:00"),
                ))
            return SimpleNamespace(data=[])  # should never be reached

        rest = MagicMock()
        rest.human_api_messages.list_my_chat_messages = _paged

        result = await _fetch_room_messages(rest, "room-3", page_size=5)
        assert call_count == 1
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_concatenates_across_pages(self):
        async def _paged(room_id, page=1, page_size=2):
            if page == 1:
                return SimpleNamespace(data=_make_page(
                    ("a", "2026-01-01T00:01:00+00:00"),
                    ("b", "2026-01-01T00:02:00+00:00"),
                ))
            if page == 2:
                return SimpleNamespace(data=_make_page(
                    ("c", "2026-01-01T00:03:00+00:00"),
                ))
            return SimpleNamespace(data=[])

        rest = MagicMock()
        rest.human_api_messages.list_my_chat_messages = _paged

        result = await _fetch_room_messages(rest, "room-4", page_size=2)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Integration: CLI command
# ---------------------------------------------------------------------------


def _mock_config():
    cfg = MagicMock()
    cfg.band.rest_url = "https://band.example.com"
    return cfg


def _mock_agent_config():
    ac = MagicMock()
    creds = MagicMock()
    creds.agent_id = "agent-uuid"
    ac.agents.items.return_value = [("conductor", creds)]
    return ac


def _two_msg_rest():
    """REST mock returning two messages in reverse order (to test sort)."""
    msgs = _make_page(
        ("agent-uuid", "2026-06-13T10:02:00+00:00"),
        ("agent-uuid", "2026-06-13T10:01:00+00:00"),
    )
    rest = MagicMock()
    rest.human_api_messages.list_my_chat_messages = AsyncMock(
        return_value=SimpleNamespace(data=msgs)
    )
    return rest


def _make_msg_dicts(count: int = 2) -> list[dict]:
    """Return a list of formatted message dicts (as produced by _room_msg_to_dict)."""
    return [
        {
            "sender_id": "agent-uuid",
            "sender_name": "conductor",
            "message_type": "text",
            "content": f"message {i}",
            "inserted_at": f"2026-06-13T10:0{i}:00+00:00",
        }
        for i in range(count)
    ]


class TestRoomLogCliCommand:
    """CLI-level tests for ``cb room-log``."""

    # AsyncRestClient is a deferred local import — patch at its source module.
    # load_agent_config is also a local import — patch at its source too.
    # _fetch_room_messages is patched to avoid actual network calls.

    @patch("codeband.cli.load_config")
    @patch("codeband.config.load_agent_config")
    @patch("thenvoi.client.rest.AsyncRestClient")
    @patch("codeband.cli._fetch_room_messages", new_callable=AsyncMock)
    def test_explicit_room_id_skips_pointer(
        self, mock_fetch, mock_rest_cls, mock_agent_cfg, mock_load_cfg, tmp_path
    ):
        mock_load_cfg.return_value = _mock_config()
        mock_agent_cfg.return_value = _mock_agent_config()
        mock_fetch.return_value = [
            SimpleNamespace(
                sender_id="agent-uuid", message_type="text",
                content="hello", inserted_at="2026-06-13T10:01:00+00:00",
                created_at=None, createdAt=None,
            )
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["room-log", "room-explicit-id", "--dir", str(tmp_path)],
            env={"BAND_API_KEY": "test-key"},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "room-explicit-id" in result.output
        assert "1 message" in result.output

    @patch("codeband.cli.load_config")
    @patch("codeband.config.load_agent_config")
    @patch("thenvoi.client.rest.AsyncRestClient")
    @patch("codeband.cli._fetch_room_messages", new_callable=AsyncMock)
    @patch("codeband.state.registration.read_room_pointer", return_value="room-from-pointer")
    @patch("codeband.state.registration.resolve_state_dir")
    def test_default_room_uses_pointer(
        self, mock_state_dir, mock_read_ptr, mock_fetch, mock_rest_cls,
        mock_agent_cfg, mock_load_cfg, tmp_path,
    ):
        mock_load_cfg.return_value = _mock_config()
        mock_agent_cfg.return_value = _mock_agent_config()
        mock_fetch.return_value = []
        mock_state_dir.return_value = tmp_path

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["room-log", "--dir", str(tmp_path)],
            env={"BAND_API_KEY": "test-key"},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "room-from-pointer" in result.output

    @patch("codeband.cli.load_config")
    @patch("codeband.config.load_agent_config")
    @patch("thenvoi.client.rest.AsyncRestClient")
    @patch("codeband.cli._fetch_room_messages", new_callable=AsyncMock)
    def test_json_flag_outputs_json_lines(
        self, mock_fetch, mock_rest_cls, mock_agent_cfg, mock_load_cfg, tmp_path
    ):
        mock_load_cfg.return_value = _mock_config()
        mock_agent_cfg.return_value = _mock_agent_config()
        mock_fetch.return_value = [
            SimpleNamespace(
                sender_id="agent-uuid", message_type="text",
                content=f"msg {i}", inserted_at=f"2026-06-13T10:0{i}:00+00:00",
                created_at=None, createdAt=None,
            )
            for i in range(2)
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["room-log", "room-json-test", "--json", "--dir", str(tmp_path)],
            env={"BAND_API_KEY": "test-key"},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        lines = [line for line in result.output.strip().splitlines() if line]
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "sender_id" in obj
            assert "content" in obj
            assert "message_type" in obj

    @patch("codeband.cli.load_config")
    @patch("codeband.config.load_agent_config")
    @patch("thenvoi.client.rest.AsyncRestClient")
    @patch("codeband.cli._fetch_room_messages", new_callable=AsyncMock)
    def test_json_output_is_sorted_by_timestamp(
        self, mock_fetch, mock_rest_cls, mock_agent_cfg, mock_load_cfg, tmp_path
    ):
        mock_load_cfg.return_value = _mock_config()
        mock_agent_cfg.return_value = _mock_agent_config()
        # Return msgs in descending order; fetch helper sorts ascending
        # _fetch_room_messages sorts ascending — reflect that in the mock return
        mock_fetch.return_value = [
            SimpleNamespace(
                sender_id="agent-uuid", message_type="text",
                content="earlier", inserted_at="2026-06-13T10:01:00+00:00",
                created_at=None, createdAt=None,
            ),
            SimpleNamespace(
                sender_id="agent-uuid", message_type="text",
                content="later", inserted_at="2026-06-13T10:02:00+00:00",
                created_at=None, createdAt=None,
            ),
        ]

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["room-log", "room-sort-test", "--json", "--dir", str(tmp_path)],
            env={"BAND_API_KEY": "test-key"},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        lines = [line for line in result.output.strip().splitlines() if line]
        timestamps = [json.loads(line)["inserted_at"] for line in lines]
        assert timestamps == sorted(timestamps)

    @patch("codeband.cli.load_config")
    @patch("codeband.state.registration.read_room_pointer", return_value=None)
    @patch("codeband.state.registration.resolve_state_dir")
    def test_no_room_pointer_exits_with_error(
        self, mock_state_dir, mock_read_ptr, mock_load_cfg, tmp_path
    ):
        mock_load_cfg.return_value = _mock_config()
        mock_state_dir.return_value = tmp_path

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["room-log", "--dir", str(tmp_path)],
            env={"BAND_API_KEY": "test-key"},
        )

        assert result.exit_code != 0
        assert "No active room" in result.output
