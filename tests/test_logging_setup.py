"""Tests for the session-resume log filter."""

from __future__ import annotations

import logging

import pytest

from codeband.logging_setup import (
    _ADAPTER_LOGGER,
    _FRIENDLY_MSG,
    _SessionResumeFilter,
)


def _make_record(
    name: str, level: int, msg: str, args: tuple = ()
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


@pytest.fixture
def flt() -> _SessionResumeFilter:
    return _SessionResumeFilter()


class TestRewritesAdapterWarning:
    def test_rewrites_session_resume_failed_warning(self, flt: _SessionResumeFilter):
        r = _make_record(
            "band.adapters.claude_sdk",
            logging.WARNING,
            "Room %s: Session resume failed (session_id=%s): %s. Creating new session",
            args=("room-1", "sid-1", "boom"),
        )
        assert flt.filter(r) is True
        assert r.getMessage() == _FRIENDLY_MSG
        assert r.levelno == logging.INFO
        assert r.levelname == "INFO"


class TestPreservesRealFailures:
    """Real CLI failures must NOT be silenced — the stderr detail never makes
    it into the log message, so we cannot distinguish benign resume misses
    from genuine auth/model/network errors at the log-record level.
    """

    def test_passes_claude_agent_sdk_fatal(self, flt: _SessionResumeFilter):
        r = _make_record(
            "claude_agent_sdk._internal.query",
            logging.ERROR,
            "Fatal error in message reader: Command failed with exit code 1",
        )
        assert flt.filter(r) is True
        assert r.levelno == logging.ERROR

    def test_passes_session_manager_error(self, flt: _SessionResumeFilter):
        r = _make_record(
            "band.integrations.claude_sdk.session_manager",
            logging.ERROR,
            "Error in session loop: Command failed with exit code 1",
        )
        assert flt.filter(r) is True
        assert r.levelno == logging.ERROR


class TestLeavesOthersAlone:
    def test_passes_unrelated_warning_from_adapter(self, flt: _SessionResumeFilter):
        r = _make_record(
            "band.adapters.claude_sdk",
            logging.WARNING,
            "Some other warning about a room",
        )
        assert flt.filter(r) is True
        assert r.levelno == logging.WARNING
        assert r.getMessage() == "Some other warning about a room"

    def test_passes_info_from_any_logger(self, flt: _SessionResumeFilter):
        r = _make_record(
            "band.adapters.claude_sdk",
            logging.INFO,
            "Session resume failed but this is just an info line",
        )
        assert flt.filter(r) is True
        assert r.levelno == logging.INFO


class TestInstallIdempotent:
    def test_install_attaches_once(self):
        from codeband.logging_setup import install_session_resume_filter

        install_session_resume_filter()
        install_session_resume_filter()

        logger = logging.getLogger(_ADAPTER_LOGGER)
        matches = [f for f in logger.filters if isinstance(f, _SessionResumeFilter)]
        assert len(matches) == 1


class TestSuppressPreflightSdkNoise:
    """During preflight we already classify failures cleanly — the SDK's
    own ``Fatal error in message reader`` ERROR log is pure noise. Outside
    preflight it stays a load-bearing failure signal (see the
    ``TestPreservesRealFailures`` tests above), so suppression must be
    strictly scoped to the context manager."""

    def test_suppresses_message_reader_error_inside_context(self):
        from codeband.logging_setup import suppress_preflight_sdk_noise

        sdk_logger = logging.getLogger("claude_agent_sdk._internal.query")
        outer_filter_count = len(sdk_logger.filters)

        with suppress_preflight_sdk_noise():
            # During the context, the SDK logger has a filter that drops
            # ERROR records.
            r = _make_record(
                "claude_agent_sdk._internal.query",
                logging.ERROR,
                "Fatal error in message reader: Command failed with exit code 1",
            )
            assert any(f.filter(r) is False for f in sdk_logger.filters), (
                "expected an installed filter to suppress the message-reader ERROR"
            )

        # After the context, no extra filters remain on the SDK logger.
        assert len(sdk_logger.filters) == outer_filter_count

    def test_does_not_suppress_outside_context(self):
        """Sanity-check the scoping — without the context manager, the SDK
        logger has no extra filter, so ERROR records pass through."""
        from codeband.logging_setup import suppress_preflight_sdk_noise

        sdk_logger = logging.getLogger("claude_agent_sdk._internal.query")
        # Establish baseline.
        with suppress_preflight_sdk_noise():
            pass

        # After: any newly created log record is not blocked by a residual
        # filter.
        r = _make_record(
            "claude_agent_sdk._internal.query",
            logging.ERROR,
            "Fatal error in message reader: Command failed with exit code 1",
        )
        # Filters may exist from other tests, but none should drop this record.
        for f in sdk_logger.filters:
            assert f.filter(r) is not False, (
                f"residual filter {f!r} is suppressing ERROR outside the context"
            )
