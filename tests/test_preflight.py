"""Tests for the Claude auth preflight check."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestCheckClaudeAuth:
    """Detect known auth/billing failures before spawning agents."""

    @pytest.mark.asyncio
    async def test_ok_on_normal_reply(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="ok"),
        ):
            assert await check_claude_auth() is None

    @pytest.mark.asyncio
    async def test_detects_credit_balance_too_low(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="Credit balance is too low to access the API."),
        ):
            err = await check_claude_auth()
        assert err is not None
        assert "credit balance" in err.summary.lower()
        assert "billing" in err.remediation.lower()

    @pytest.mark.asyncio
    async def test_detects_invalid_api_key(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="authentication_error: invalid x-api-key"),
        ):
            err = await check_claude_auth()
        assert err is not None
        assert "ANTHROPIC_API_KEY" in err.remediation

    @pytest.mark.asyncio
    async def test_detects_rate_limit(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="rate_limit_error: too many requests"),
        ):
            err = await check_claude_auth()
        assert err is not None
        assert "rate limit" in err.remediation.lower()

    @pytest.mark.asyncio
    async def test_detects_usage_limit_reached(self):
        """Pro/Max subscription exhausted — surface clearly, don't let the
        phrase land silently in a chat room.
        """
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="Claude usage limit reached"),
        ):
            err = await check_claude_auth()
        assert err is not None
        assert "usage limit" in err.summary.lower()

    @pytest.mark.asyncio
    async def test_detects_please_run_login(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="Please run /login"),
        ):
            err = await check_claude_auth()
        assert err is not None
        assert "login" in err.remediation.lower() or "setup-token" in err.remediation.lower()

    @pytest.mark.asyncio
    async def test_wraps_raised_exceptions(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(side_effect=RuntimeError("network down")),
        ):
            err = await check_claude_auth()
        assert err is not None
        assert "RuntimeError" in err.summary
        assert "network down" in err.summary

    @pytest.mark.asyncio
    async def test_match_is_case_insensitive(self):
        from codeband.preflight import check_claude_auth

        with patch(
            "codeband.utility_llm.one_shot_text",
            AsyncMock(return_value="CREDIT BALANCE IS TOO LOW"),
        ):
            err = await check_claude_auth()
        assert err is not None


class TestCheckCodexAuth:
    """Codex preflight shells out to `codex exec` and matches known error text."""

    @pytest.mark.asyncio
    async def test_ok_on_normal_reply(self):
        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(return_value=(0, "session id: abc\nok\ntokens used 42\n")),
        ):
            assert await check_codex_auth() is None

    @pytest.mark.asyncio
    async def test_detects_not_logged_in(self):
        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(return_value=(1, "Error: not logged in. Run `codex login`.")),
        ):
            err = await check_codex_auth()
        assert err is not None
        assert "codex login" in err.remediation.lower()

    @pytest.mark.asyncio
    async def test_detects_rate_limit(self):
        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(return_value=(1, "You've hit your rate limit")),
        ):
            err = await check_codex_auth()
        assert err is not None
        assert "rate" in err.summary.lower()

    @pytest.mark.asyncio
    async def test_detects_usage_limit(self):
        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(return_value=(1, "usage limit exceeded")),
        ):
            err = await check_codex_auth()
        assert err is not None

    @pytest.mark.asyncio
    async def test_detects_invalid_api_key(self):
        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(return_value=(1, "401 Unauthorized: invalid api key")),
        ):
            err = await check_codex_auth()
        assert err is not None
        assert "openai_api_key" in err.remediation.lower()

    @pytest.mark.asyncio
    async def test_codex_cli_missing(self):
        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(side_effect=FileNotFoundError("codex")),
        ):
            err = await check_codex_auth()
        assert err is not None
        assert "codex" in err.remediation.lower()

    @pytest.mark.asyncio
    async def test_timeout_is_reported(self):
        import asyncio

        from codeband.preflight import check_codex_auth

        with patch(
            "codeband.preflight._run_codex_probe",
            AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            err = await check_codex_auth()
        assert err is not None
        assert "timed out" in err.summary.lower()


class TestRunPreflight:
    """`run_preflight` parallelizes Claude + Codex checks via asyncio.gather."""

    @pytest.fixture
    def claude_only_config(self):
        from codeband.config import CodebandConfig

        return CodebandConfig.model_validate({
            "repo": {"url": "https://github.com/x/y", "branch": "main"},
            "agents": {
                "conductor": {"framework": "claude_sdk"},
                "mergemaster": {"framework": "claude_sdk"},
                "planners": {"claude_sdk": {"count": 1}, "codex": {"count": 0}},
                "plan_reviewers": {"claude_sdk": {"count": 1}, "codex": {"count": 0}},
                "coders": {"claude_sdk": {"count": 1}, "codex": {"count": 0}},
                "reviewers": {"claude_sdk": {"count": 1}, "codex": {"count": 0}},
            },
        })

    @pytest.fixture
    def mixed_config(self):
        from codeband.config import CodebandConfig

        return CodebandConfig.model_validate({
            "repo": {"url": "https://github.com/x/y", "branch": "main"},
            "agents": {
                "conductor": {"framework": "claude_sdk"},
                "mergemaster": {"framework": "claude_sdk"},
                "planners": {"claude_sdk": {"count": 1}, "codex": {"count": 0}},
                "plan_reviewers": {"claude_sdk": {"count": 0}, "codex": {"count": 1}},
                "coders": {"claude_sdk": {"count": 1}, "codex": {"count": 1}},
                "reviewers": {"claude_sdk": {"count": 1}, "codex": {"count": 1}},
            },
        })

    @pytest.mark.asyncio
    async def test_skips_codex_when_not_configured(self, claude_only_config):
        from codeband.preflight import run_preflight

        codex_called = AsyncMock(return_value=None)
        with patch("codeband.preflight.check_claude_auth", AsyncMock(return_value=None)), \
             patch("codeband.preflight.check_codex_auth", codex_called):
            result = await run_preflight(claude_only_config)

        assert result is None
        codex_called.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_both_in_parallel(self, mixed_config):
        """Both checks run concurrently — total wall time ≈ max(claude, codex), not sum."""
        import asyncio
        import time

        from codeband.preflight import run_preflight

        async def slow_ok():
            await asyncio.sleep(0.5)
            return None

        with patch("codeband.preflight.check_claude_auth", side_effect=slow_ok), \
             patch("codeband.preflight.check_codex_auth", side_effect=slow_ok):
            start = time.monotonic()
            result = await run_preflight(mixed_config)
            elapsed = time.monotonic() - start

        assert result is None
        # Two 0.5s tasks run sequentially would take ≥1.0s. In parallel, ~0.5s
        # plus a small scheduling margin. Allow 0.8s as a generous upper bound.
        assert elapsed < 0.8, f"preflight took {elapsed:.2f}s — looks sequential"

    @pytest.mark.asyncio
    async def test_claude_error_wins_when_both_fail(self, mixed_config):
        """Claude's error is preferred when both fail simultaneously."""
        from codeband.preflight import PreflightError, run_preflight

        claude_err = PreflightError(summary="claude failed", remediation="fix claude")
        codex_err = PreflightError(summary="codex failed", remediation="fix codex")

        with patch(
            "codeband.preflight.check_claude_auth", AsyncMock(return_value=claude_err),
        ), patch(
            "codeband.preflight.check_codex_auth", AsyncMock(return_value=codex_err),
        ):
            result = await run_preflight(mixed_config)

        assert result is claude_err

    @pytest.mark.asyncio
    async def test_codex_error_surfaces_when_claude_passes(self, mixed_config):
        from codeband.preflight import PreflightError, run_preflight

        codex_err = PreflightError(summary="codex failed", remediation="fix codex")
        with patch(
            "codeband.preflight.check_claude_auth", AsyncMock(return_value=None),
        ), patch(
            "codeband.preflight.check_codex_auth", AsyncMock(return_value=codex_err),
        ):
            result = await run_preflight(mixed_config)

        assert result is codex_err
