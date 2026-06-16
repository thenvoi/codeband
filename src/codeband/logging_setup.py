"""Friendly rewrite of the benign session-resume warning.

When a Codeband container is recreated (e.g. ``cb up`` after a rebuild), the
local Claude CLI session store at ``~/.claude/projects/…`` is wiped, but the
Band.ai room still holds the previous ``claude_sdk_session_id`` task event.
On the next bootstrap the adapter tries to ``--resume`` that session, the
CLI exits 1, and the adapter catches the exception and silently recovers by
creating a fresh session — but first logs a WARNING that alarms operators.

We rewrite *only* that adapter WARNING to a calm INFO line.

We deliberately do **not** drop the SDK's preceding ERROR records
(``Fatal error in message reader: Command failed …`` and ``Error in session
loop: Command failed …``). Those same messages are emitted for *any*
non-zero CLI exit (bad auth, wrong model, broken bundled CLI, network
death), and the SDK's ``ProcessError`` doesn't carry the real stderr, so the
log record alone can't distinguish the benign resume miss from a real
failure. Swallowing them unconditionally would silently hide production
breakage; letting them through costs a bit of noise on rebuild but keeps
real failures visible.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

_ADAPTER_LOGGER = "band.adapters.claude_sdk"
_SDK_QUERY_LOGGER = "claude_agent_sdk._internal.query"

_FRIENDLY_MSG = (
    "Claude session reset after container recreation (expected); starting fresh."
)


class _SessionResumeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == _ADAPTER_LOGGER
            and record.levelno == logging.WARNING
            and "Session resume failed" in record.getMessage()
        ):
            record.msg = _FRIENDLY_MSG
            record.args = ()
            record.levelno = logging.INFO
            record.levelname = "INFO"
            record.exc_info = None
            record.exc_text = None

        return True


def install_session_resume_filter() -> None:
    """Attach the filter to the adapter logger. Idempotent."""
    logger = logging.getLogger(_ADAPTER_LOGGER)
    if not any(isinstance(f, _SessionResumeFilter) for f in logger.filters):
        logger.addFilter(_SessionResumeFilter())


class _DropMessageReaderErrorFilter(logging.Filter):
    """Drop the SDK's ``Fatal error in message reader`` ERROR records.

    Scoped via :func:`suppress_preflight_sdk_noise` only — see the
    module docstring for why we do NOT drop these unconditionally.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == _SDK_QUERY_LOGGER and record.levelno >= logging.ERROR
        )


@contextmanager
def suppress_preflight_sdk_noise() -> Iterator[None]:
    """Silence the SDK's noisy ``Fatal error in message reader`` ERROR
    log for the duration of the ``with`` block.

    Use only around the preflight call: preflight already classifies the
    failure and prints a clean remediation, so the SDK's raw log line is
    pure noise. Outside this scope the log stays load-bearing — see the
    rationale in the module docstring at the top of this file.
    """
    logger = logging.getLogger(_SDK_QUERY_LOGGER)
    flt = _DropMessageReaderErrorFilter()
    logger.addFilter(flt)
    try:
        yield
    finally:
        logger.removeFilter(flt)
