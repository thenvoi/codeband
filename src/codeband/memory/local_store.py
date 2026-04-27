"""Local JSONL-backed memory store — free-tier fallback for Band.ai memory.

The store is a single append-only JSONL file. Reads do a full scan and filter
in memory; volume is tiny (typically <100 live envelopes per task), so this
is fine. Writes use an advisory `fcntl.flock` to stay safe when multiple
agents in one process (or sibling processes sharing the workspace) append
simultaneously.

Returned records duck-type the Band.ai SDK's Pydantic Memory objects well
enough for existing readers at `kickoff.py:_format_task_status` and the
agent tool runtime (which only touches `.content` / `.thought` /
`.inserted_at` / `.updated_at` and, for list calls, `response.data`).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryRecord:
    """A single memory entry — mirrors the fields Codeband actually reads."""

    id: str
    content: str
    system: str
    type: str
    segment: str
    scope: str
    thought: str = ""
    subject_id: str | None = None
    metadata: dict[str, Any] | None = None
    inserted_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    archived_at: str | None = None
    status: str = "active"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> MemoryRecord:
        return cls(**json.loads(line))


@dataclass
class MemoryListResponse:
    """Shape returned by `LocalMemoryStore.list()` — duck-types SDK list response."""

    data: list[MemoryRecord]
    meta: dict[str, Any] | None = None


class LocalMemoryStore:
    """Append-only JSONL memory store at <workspace>/state/memories.jsonl.

    Thread/process-safe for writes via `fcntl.flock`. Reads are full-scan
    with in-memory filtering.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    # --- public API ---------------------------------------------------------

    async def store(
        self,
        *,
        content: str,
        system: str,
        type: str,
        segment: str,
        thought: str = "",
        scope: str = "subject",
        subject_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        record = MemoryRecord(
            id=f"mem_{uuid.uuid4().hex}",
            content=content,
            system=system,
            type=type,
            segment=segment,
            scope=scope,
            thought=thought,
            subject_id=subject_id,
            metadata=metadata,
        )
        self._append(record)
        return record

    async def list(
        self,
        *,
        subject_id: str | None = None,
        scope: str | None = None,
        system: str | None = None,
        type: str | None = None,
        segment: str | None = None,
        content_query: str | None = None,
        page_size: int = 50,
        status: str | None = "active",
    ) -> MemoryListResponse:
        # JSONL is append-only, so iteration yields oldest → newest. When
        # there are more matches than page_size, callers care about the
        # *newest* slice (the Conductor uses the latest protocol round).
        # Collect all matches and return the trailing page in chronological
        # order. Volume is small (<100 active envelopes typical); the
        # all-in-memory scan is fine.
        matches: list[MemoryRecord] = []
        for record in self._iter_records():
            if status != "all" and record.status != (status or "active"):
                continue
            if subject_id is not None and record.subject_id != subject_id:
                continue
            if scope is not None and record.scope != scope:
                continue
            if system is not None and record.system != system:
                continue
            if type is not None and record.type != type:
                continue
            if segment is not None and record.segment != segment:
                continue
            if content_query and not self._matches_query(record.content, content_query):
                continue
            matches.append(record)
        if page_size and len(matches) > page_size:
            matches = matches[-page_size:]
        return MemoryListResponse(data=matches)

    async def archive(self, memory_id: str) -> MemoryRecord | None:
        """Mark `memory_id` archived. Returns the updated record, or None if unknown."""
        updated: MemoryRecord | None = None
        records = list(self._iter_records())
        for rec in records:
            if rec.id == memory_id and rec.status != "archived":
                rec.status = "archived"
                rec.archived_at = _now_iso()
                rec.updated_at = rec.archived_at
                updated = rec
                break
        if updated is not None:
            self._rewrite(records)
        return updated

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _matches_query(content: str, query: str) -> bool:
        """Substring match on the first line, matching Band.ai semantics.

        Conductor prompt documents: `content_query` must appear on the first
        line of the memory content. We replicate that so switching backends
        doesn't change query behavior.
        """
        first_line = content.split("\n", 1)[0].lower()
        return query.lower() in first_line

    def _iter_records(self) -> Iterator[MemoryRecord]:
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield MemoryRecord.from_json(line)
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning("Skipping malformed memory record: %s", exc)

    def _append(self, record: MemoryRecord) -> None:
        with self._locked("a") as fh:
            fh.write(record.to_json() + "\n")

    def _rewrite(self, records: list[MemoryRecord]) -> None:
        # Rewrite atomically via temp file to avoid partial writes during archive.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with self._locked("a"):  # hold the lock on the real file while writing tmp
            with tmp.open("w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(rec.to_json() + "\n")
            tmp.replace(self.path)

    @contextlib.contextmanager
    def _locked(self, mode: str) -> Iterator[Any]:
        with self.path.open(mode, encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield fh
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
