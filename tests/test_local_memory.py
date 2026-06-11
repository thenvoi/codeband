"""Tests for `codeband.memory.local_store.LocalMemoryStore`."""

from __future__ import annotations

import asyncio
import json
from multiprocessing import Process
from pathlib import Path

import pytest

from codeband.memory import LocalMemoryStore, MemoryRecord


@pytest.fixture
def store(tmp_path: Path) -> LocalMemoryStore:
    return LocalMemoryStore(tmp_path / "state" / "memories.jsonl")


class TestStore:
    async def test_store_creates_file_and_writes_record(self, store: LocalMemoryStore):
        rec = await store.store(
            content="protocol plan cid plan_r1 state ready from planner to conductor",
            system="working", type="episodic", segment="agent",
            thought="plan ready", scope="organization",
        )
        assert rec.id.startswith("mem_")
        assert store.path.exists()
        lines = store.path.read_text().strip().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["content"].startswith("protocol plan")
        assert parsed["status"] == "active"

    async def test_store_populates_timestamps(self, store: LocalMemoryStore):
        rec = await store.store(
            content="x", system="working", type="episodic",
            segment="agent", thought="", scope="organization",
        )
        assert rec.inserted_at
        assert rec.updated_at
        assert rec.archived_at is None


class TestList:
    async def _seed(self, store: LocalMemoryStore) -> None:
        await store.store(
            content="protocol plan cid plan_r1 state ready from planner to conductor",
            system="working", type="episodic", segment="agent", scope="organization",
            thought="plan ready",
        )
        await store.store(
            content="protocol code_review cid cr_42_r1 pr 42 state findings_posted "
                    "from reviewer to player-0",
            system="working", type="episodic", segment="agent", scope="organization",
            thought="review posted",
        )
        await store.store(
            content="Test command: pytest -v",
            system="long_term", type="procedural", segment="tool", scope="organization",
            thought="test command",
        )

    async def test_filters_by_system_type_segment_scope(self, store: LocalMemoryStore):
        await self._seed(store)

        resp = await store.list(
            system="working", type="episodic", segment="agent", scope="organization",
        )
        assert len(resp.data) == 2
        assert all(r.system == "working" for r in resp.data)

        resp = await store.list(
            system="long_term", type="procedural", segment="tool", scope="organization",
        )
        assert len(resp.data) == 1
        assert resp.data[0].content.startswith("Test command")

    async def test_content_query_matches_first_line_only(self, store: LocalMemoryStore):
        await store.store(
            content="header line\nprotocol code_review cid cr_42_r1",
            system="working", type="episodic", segment="agent", scope="organization",
            thought="",
        )
        resp = await store.list(content_query="header", system="working")
        assert len(resp.data) == 1
        # `code_review` appears only on line 2 — must not match (Band.ai semantics).
        resp = await store.list(content_query="code_review", system="working")
        assert resp.data == []

    async def test_content_query_is_case_insensitive(self, store: LocalMemoryStore):
        await store.store(
            content="Protocol Plan", system="working", type="episodic",
            segment="agent", scope="organization", thought="",
        )
        resp = await store.list(content_query="protocol plan")
        assert len(resp.data) == 1

    async def test_archived_records_are_excluded_by_default(
        self, store: LocalMemoryStore,
    ):
        rec = await store.store(
            content="x", system="working", type="episodic",
            segment="agent", scope="organization", thought="",
        )
        await store.archive(rec.id)

        default_resp = await store.list(system="working")
        assert default_resp.data == []

        all_resp = await store.list(system="working", status="all")
        assert len(all_resp.data) == 1
        assert all_resp.data[0].status == "archived"

    async def test_page_size_caps_to_newest_results(self, store: LocalMemoryStore):
        """When matches > page_size, return the trailing (newest) page."""
        for i in range(5):
            await store.store(
                content=f"entry {i}", system="working", type="episodic",
                segment="agent", scope="organization", thought="",
            )
        resp = await store.list(system="working", page_size=3)
        assert len(resp.data) == 3
        # Records are appended in order, so entries 2/3/4 are the newest three.
        assert [r.content for r in resp.data] == ["entry 2", "entry 3", "entry 4"]


class TestArchive:
    async def test_archive_marks_record_and_returns_it(self, store: LocalMemoryStore):
        rec = await store.store(
            content="x", system="working", type="episodic",
            segment="agent", scope="organization", thought="",
        )
        updated = await store.archive(rec.id)
        assert updated is not None
        assert updated.status == "archived"
        assert updated.archived_at is not None

    async def test_archive_unknown_id_returns_none(self, store: LocalMemoryStore):
        result = await store.archive("mem_does_not_exist")
        assert result is None


class TestMalformedLines:
    async def test_skips_malformed_json_and_continues(self, tmp_path: Path):
        path = tmp_path / "state" / "memories.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "not json\n" + MemoryRecord(
                id="mem_1", content="ok", system="working", type="episodic",
                segment="agent", scope="organization", thought="",
            ).to_json() + "\n",
        )

        store = LocalMemoryStore(path)
        resp = await store.list(system="working")
        assert len(resp.data) == 1
        assert resp.data[0].id == "mem_1"


class TestConcurrentWrites:
    def test_two_processes_do_not_corrupt_file(self, tmp_path: Path):
        """Two concurrent processes both appending should produce valid JSONL."""
        path = tmp_path / "state" / "memories.jsonl"
        path.parent.mkdir(parents=True)
        path.touch()

        p1 = Process(target=_child_writer, args=(str(path), "a", 20))
        p2 = Process(target=_child_writer, args=(str(path), "b", 20))
        p1.start()
        p2.start()
        p1.join(10)
        p2.join(10)
        assert p1.exitcode == 0 and p2.exitcode == 0

        lines = [line for line in path.read_text().splitlines() if line.strip()]
        assert len(lines) == 40
        for line in lines:
            json.loads(line)  # raises on corruption


def _child_writer(path_str: str, prefix: str, count: int) -> None:
    """Top-level helper — multiprocessing can't pickle closures."""
    store = LocalMemoryStore(Path(path_str))

    async def run():
        for i in range(count):
            await store.store(
                content=f"{prefix}-{i}", system="working",
                type="episodic", segment="agent",
                scope="organization", thought="",
            )
    asyncio.run(run())


class TestSidecarLockAndCompaction:
    """S6-F10 (stable sidecar lock) + S8-F4 (archived-history compaction)."""

    def _record(self, n: int, status: str = "active") -> MemoryRecord:
        return MemoryRecord(
            id=f"mem_{n:04d}",
            content=f"record {n}",
            system="working",
            type="episodic",
            segment="agent",
            scope="organization",
            status=status,
        )

    def test_append_during_archive_is_not_lost(self, tmp_path: Path, monkeypatch):
        """The archive-vs-append lost-write race is closed (S6-F10).

        archive() replaces the data file's inode; under the old data-file
        lock, an append racing the rewrite could acquire the OLD inode's lock
        and write into the orphaned file — silently dropping a protocol-state
        envelope. The sidecar lock serializes the whole read-modify-rewrite
        against the append; the appender, blocked until archive finishes,
        re-opens the path and lands in the NEW file.
        """
        import threading


        store = LocalMemoryStore(tmp_path / "memories.jsonl")
        rec = asyncio.run(
            store.store(
                content="seed", system="working", type="episodic", segment="agent",
            ),
        )

        in_rewrite = threading.Event()
        appender_started = threading.Event()
        orig_rewrite = LocalMemoryStore._rewrite_locked

        def slow_rewrite(self, records):
            in_rewrite.set()
            # Hold the lock long enough for the appender to be blocked on it.
            appender_started.wait(timeout=5)
            import time

            time.sleep(0.2)
            return orig_rewrite(self, records)

        monkeypatch.setattr(LocalMemoryStore, "_rewrite_locked", slow_rewrite)

        def do_append():
            in_rewrite.wait(timeout=5)
            appender_started.set()
            store._append(self._record(999))

        t = threading.Thread(target=do_append)
        t.start()
        asyncio.run(store.archive(rec.id))
        t.join(timeout=5)
        assert not t.is_alive()

        contents = (tmp_path / "memories.jsonl").read_text(encoding="utf-8")
        assert "mem_0999" in contents  # the racing append survived
        assert '"status": "archived"' in contents  # and so did the archive

    def test_lock_uses_stable_sidecar_file(self, tmp_path: Path):
        store = LocalMemoryStore(tmp_path / "memories.jsonl")
        with store._locked():
            pass
        assert (tmp_path / "memories.jsonl.lock").exists()

    def test_archive_compacts_archived_history_beyond_keep_last(
        self, tmp_path: Path,
    ):
        """archive()'s rewrite keeps only the newest 50 archived records;
        live records are untouched and list() semantics unchanged (S8-F4)."""
        from codeband.memory.local_store import _ARCHIVED_KEEP_LAST

        store = LocalMemoryStore(tmp_path / "memories.jsonl")
        # 60 already-archived records (oldest first), 3 live ones, then
        # archive one more live record to trigger the rewrite.
        for n in range(60):
            store._append(self._record(n, status="archived"))
        for n in range(100, 103):
            store._append(self._record(n))
        trigger = self._record(200)
        store._append(trigger)

        result = asyncio.run(store.archive(trigger.id))
        assert result is not None

        records = list(store._iter_records())
        archived = [r for r in records if r.status == "archived"]
        active = [r for r in records if r.status == "active"]
        assert len(archived) == _ARCHIVED_KEEP_LAST == 50
        # Newest archived kept: the trigger plus the tail of the original 60.
        assert archived[-1].id == "mem_0200"
        assert archived[0].id == "mem_0011"  # 60+1 archived → oldest 11 dropped
        # Live records untouched, in order.
        assert [r.id for r in active] == ["mem_0100", "mem_0101", "mem_0102"]
        # list() for live records is unchanged.
        listed = asyncio.run(store.list())
        assert [r.id for r in listed.data] == ["mem_0100", "mem_0101", "mem_0102"]

    def test_no_compaction_below_threshold(self, tmp_path: Path):
        """At or below keep-last-50 archived records, nothing is dropped."""
        store = LocalMemoryStore(tmp_path / "memories.jsonl")
        for n in range(20):
            store._append(self._record(n, status="archived"))
        trigger = self._record(50)
        store._append(trigger)

        asyncio.run(store.archive(trigger.id))

        archived = [r for r in store._iter_records() if r.status == "archived"]
        assert len(archived) == 21
