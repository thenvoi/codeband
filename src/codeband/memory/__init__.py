"""Memory backend for Codeband.

On paid Band.ai tiers, memory operations go through the Band.ai REST API.
On free tier (no memory API), a local JSONL-backed store is used instead.
The backend is resolved once per process via `probe_memory_backend()`.
"""

from __future__ import annotations

from codeband.memory.local_store import (
    LocalMemoryStore,
    MemoryListResponse,
    MemoryRecord,
)
from codeband.memory.probe import (
    MemoryMode,
    get_memory_mode,
    probe_memory_backend,
    reset_memory_mode,
    set_memory_mode,
)

__all__ = [
    "LocalMemoryStore",
    "MemoryListResponse",
    "MemoryRecord",
    "MemoryMode",
    "get_memory_mode",
    "probe_memory_backend",
    "reset_memory_mode",
    "set_memory_mode",
]
