"""Universal agent rehydration (RFC Workstream 5).

:func:`build_agent_recovery_context` reads the durable
:class:`~codeband.state.store.StateStore` and produces a per-role markdown
recovery prompt that is prepended to a reconnecting agent's system prompt —
the same convention the coder path already uses
(``session/context.py:build_recovery_context``). Only the non-coder
coordination roles use this module; coders rehydrate from git state, which
this module never touches.

Per-role content:

* **conductor** → a table of *all* non-terminal subtasks (id, state, worker, PR).
* **mergemaster** → subtasks in ``merge_pending`` / ``review_passed`` /
  ``acceptance_passed`` / ``needs_rebase``.
* **reviewer** (code reviewer) → subtasks in ``review_pending``.
* **verifier** → subtasks in ``review_passed`` (awaiting the acceptance verdict).
* **planner** → the active task description(s).
* **plan_reviewer** → active task description(s) + in-flight subtask count.

Task rows are only reachable through active subtasks (the StateStore exposes
``get_task`` by id and ``list_active_subtasks``, but no list-all-tasks), so a
planner / plan-reviewer that reconnects *before* any subtask row exists yields
``None`` — the agent simply re-derives the task from the room as it does today.
``None`` always means "nothing relevant in durable state", and the agent's
behavior is then identical to today.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from codeband.state.store import StateStore, SubtaskRow, TaskRow

logger = logging.getLogger(__name__)

_SINGLETON_ROLES = frozenset({"conductor", "mergemaster"})
_POOL_ROLES = frozenset({"planner", "plan_reviewer", "coder", "reviewer", "verifier"})

_HEADER = "## Recovery context (from durable state)"
_TABLE_HEAD = "| Subtask | State | Worker | PR |\n|---------|-------|--------|----|"


def _role_from_agent_key(agent_key: str) -> str:
    """Derive a role name from an agent_config key.

    Singletons map to themselves (``conductor`` / ``mergemaster``); pool keys
    are ``{role}-{framework}-{index}`` (e.g. ``reviewer-codex-0`` → ``reviewer``).
    A bare role name is accepted as-is so callers can pass either form.
    """
    if agent_key in _SINGLETON_ROLES:
        return agent_key
    parts = agent_key.rsplit("-", 2)
    if len(parts) == 3 and parts[0] in _POOL_ROLES:
        return parts[0]
    return agent_key


def _subtask_rows(subtasks: Iterable["SubtaskRow"]) -> list[str]:
    rows: list[str] = []
    for st in subtasks:
        worker = st.assigned_worker or "—"
        pr = f"#{st.pr_number}" if st.pr_number is not None else "—"
        rows.append(f"| {st.subtask_id} | {st.state} | {worker} | {pr} |")
    return rows


def _table_context(intro: str, subtasks: list["SubtaskRow"], footer: str | None) -> str | None:
    if not subtasks:
        return None
    lines = [_HEADER, "", intro, "", _TABLE_HEAD, *_subtask_rows(subtasks)]
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def _tasks_from_subtasks(
    subtasks: list["SubtaskRow"], store: "StateStore"
) -> list["TaskRow"]:
    """Return distinct task rows referenced by the given subtasks (order-stable)."""
    seen: list[str] = []
    for st in subtasks:
        if st.task_id not in seen:
            seen.append(st.task_id)
    tasks: list["TaskRow"] = []
    for task_id in seen:
        task = store.get_task(task_id)
        if task is not None:
            tasks.append(task)
    return tasks


def _planning_context(
    role: str, active: list["SubtaskRow"], store: "StateStore"
) -> str | None:
    tasks = _tasks_from_subtasks(active, store)
    if not tasks:
        return None
    label = "Planner" if role == "planner" else "Plan Reviewer"
    lines = [_HEADER, "", f"You reconnected as {label}. Active task(s):", ""]
    for task in tasks:
        lines.append(f"- **{task.task_id}**: {task.description}")
        if role == "plan_reviewer":
            count = sum(1 for st in active if st.task_id == task.task_id)
            lines.append(f"  - subtasks in flight: {count}")
    return "\n".join(lines)


async def build_agent_recovery_context(
    agent_key: str, store: "StateStore"
) -> str | None:
    """Build a per-role markdown recovery prompt from durable state.

    ``agent_key`` is the agent_config key (e.g. ``conductor`` or
    ``reviewer-codex-0``). Returns ``None`` when nothing in the store is
    relevant to this role — the caller then reconnects with no extra context,
    which is identical to today's behavior.
    """
    role = _role_from_agent_key(agent_key)
    active = store.list_active_subtasks()

    if role == "conductor":
        return _table_context(
            "You reconnected. The orchestration state store has these "
            "in-flight subtasks:",
            active,
            "Resume coordination from this state rather than re-deriving from chat.",
        )
    if role == "mergemaster":
        # ``needs_rebase`` rests with the coder, but the Mergemaster queued the
        # merge that was sent back — it must see the state to avoid re-queueing
        # or treating the subtask as lost. ``acceptance_passed`` is the
        # ready-to-queue state once a Verifier is configured (the
        # verify_acceptance gate sits between review and merge).
        pending = [
            st for st in active
            if st.state in (
                "merge_pending", "review_passed", "acceptance_passed",
                "needs_rebase",
            )
        ]
        return _table_context(
            "You reconnected as Mergemaster. Subtasks awaiting integration:",
            pending,
            None,
        )
    if role == "reviewer":
        pending = [st for st in active if st.state == "review_pending"]
        return _table_context(
            "You reconnected as Code Reviewer. Subtasks awaiting review:",
            pending,
            None,
        )
    if role == "verifier":
        # The Verifier checks evidence integrity once review passes: subtasks
        # resting at ``review_passed`` await its ``cb-phase verify-acceptance``
        # verdict (the last gate before merge).
        pending = [st for st in active if st.state == "review_passed"]
        return _table_context(
            "You reconnected as Verifier. Subtasks awaiting acceptance "
            "verification:",
            pending,
            None,
        )
    if role in ("planner", "plan_reviewer"):
        return _planning_context(role, active, store)

    return None


async def recover_for_reconnect(
    agent_key: str, workspace_path: Path | str
) -> str | None:
    """Open the workspace StateStore and build recovery context, never raising.

    Used at the reconnect call sites (single-process ``_run_agent_forever`` and
    the distributed ``run_agent`` dispatch). Any failure — missing DB, schema
    drift, a rehydration bug — is swallowed and returns ``None`` so the
    reconnect loop can never be broken by rehydration.
    """
    try:
        from codeband.state.store import StateStore

        db_path = Path(workspace_path) / "state" / "orchestration.db"
        store = StateStore(db_path)
        return await build_agent_recovery_context(agent_key, store)
    except Exception:
        logger.warning(
            "Rehydration failed for %s — reconnecting without recovery context",
            agent_key,
            exc_info=True,
        )
        return None
