"""Persistent worker identity — survives session crashes and restarts."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class WorkerIdentity(BaseModel):
    """Persistent identity for a pooled worker agent, stored as JSON in state_dir."""

    worker_id: str
    agent_id: str
    worktree_path: str
    session_count: int = 0
    last_session_started_at: datetime | None = None
    last_session_ended_at: datetime | None = None
    last_session_error: str | None = None

    def save(self, state_dir: Path) -> None:
        """Atomically write identity to state_dir/{worker_id}.json."""
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / f"{self.worker_id}.json"
        data = self.model_dump(mode="json")
        fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            Path(tmp).replace(target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, state_dir: Path, worker_id: str) -> WorkerIdentity | None:
        """Load identity from state_dir, returning None if not found."""
        path = state_dir / f"{worker_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return cls.model_validate(data)
