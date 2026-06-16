"""Lock-in regression tests for agent permission modes.

Prevents a future revert to ``approval_mode="auto_decline"`` on the
coordination agents (Planner, Plan Reviewer, Conductor), which silently
overrides ``.claude/settings.json`` allow rules via a ``can_use_tool``
hook — see ``plans/jiggly-spinning-mountain.md`` for the full history.

The coordination agents should use ``permission_mode="dontAsk"`` so the
bundled Claude CLI honors the worktree's ``.claude/settings.json``
allow list and deterministically denies everything else.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _adapter_kwargs(constructor, **init_kwargs) -> dict:
    """Run an agent constructor with the Claude SDK adapter mocked, and
    return the kwargs that were passed into ``ClaudeSDKAdapter(...)``."""
    with patch("band.adapters.ClaudeSDKAdapter") as mock_adapter:
        mock_adapter.return_value = MagicMock()
        constructor(**init_kwargs)
    assert mock_adapter.call_count == 1, mock_adapter.call_args_list
    return mock_adapter.call_args.kwargs


class TestCoordinationPermissionModes:
    """Planner, Plan Reviewer, Conductor must run with dontAsk + no callback."""

    def test_planner_uses_dontask_and_no_approval_mode(self):
        from codeband.agents.planner import ClaudePlannerRunner

        kwargs = _adapter_kwargs(ClaudePlannerRunner, workspace="/tmp/fake-worktree")

        assert kwargs["permission_mode"] == "dontAsk"
        assert kwargs["approval_mode"] is None

    def test_plan_reviewer_uses_dontask_and_no_approval_mode(self):
        from codeband.agents.plan_reviewer import ClaudePlanReviewerRunner

        kwargs = _adapter_kwargs(
            ClaudePlanReviewerRunner, workspace="/tmp/fake-worktree",
        )

        assert kwargs["permission_mode"] == "dontAsk"
        assert kwargs["approval_mode"] is None

    def test_conductor_uses_dontask_and_no_approval_mode(self):
        from codeband.agents.conductor import ClaudeConductorRunner

        kwargs = _adapter_kwargs(ClaudeConductorRunner)

        assert kwargs["permission_mode"] == "dontAsk"
        assert kwargs["approval_mode"] is None


class TestCodingPermissionModesUnchanged:
    """Coders still bypass permissions — this test protects that invariant
    (so a future cleanup doesn't accidentally apply ``dontAsk`` everywhere).
    """

    def test_claude_coder_still_bypasses(self):
        from codeband.agents.player_claude import ClaudePlayerRunner

        kwargs = _adapter_kwargs(
            ClaudePlayerRunner, workspace="/tmp/fake-worktree",
        )

        assert kwargs["permission_mode"] == "bypassPermissions"
        assert kwargs["approval_mode"] is None
