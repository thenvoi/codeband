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
            "thenvoi.adapters.claude_sdk",
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
            "thenvoi.integrations.claude_sdk.session_manager",
            logging.ERROR,
            "Error in session loop: Command failed with exit code 1",
        )
        assert flt.filter(r) is True
        assert r.levelno == logging.ERROR


class TestLeavesOthersAlone:
    def test_passes_unrelated_warning_from_adapter(self, flt: _SessionResumeFilter):
        r = _make_record(
            "thenvoi.adapters.claude_sdk",
            logging.WARNING,
            "Some other warning about a room",
        )
        assert flt.filter(r) is True
        assert r.levelno == logging.WARNING
        assert r.getMessage() == "Some other warning about a room"

    def test_passes_info_from_any_logger(self, flt: _SessionResumeFilter):
        r = _make_record(
            "thenvoi.adapters.claude_sdk",
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
