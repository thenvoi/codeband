"""Atomic task registration — the single writer of "a task exists".

A task is *registered* when two things agree: a ``tasks`` row in the durable
state store and the ``{workspace}/state/.codeband_room`` pointer file —
living next to the very DB it pairs with — naming that row's room.
(``<project_dir>/.codeband_room`` is the legacy location, still read as a
fallback with a deprecation warning and migrated away on the next
registration.) Historically those were written by separate code paths at
separate times (``send_task`` wrote the row best-effort mid-kickoff and the
pointer only after the task message; the ``/codeband`` peer-seeding path wrote
the pointer and never the row), which produced four observable broken states:

* **H1 — row-without-pointer:** a crash after the row write but before the
  pointer write leaves ``cb-phase`` unable to resolve the task.
* **H2 — pointer-without-row:** a swallowed store failure (or a path that
  never writes the row at all) leaves a pointer that resolves to nothing.
* **H3 — message-before-pointer:** the task message activates agents before
  the pointer exists, so an early ``cb-phase`` call races the write.
* **H4 — ownerless row:** best-effort owner resolution leaves ``owner_id``
  NULL, and the watchdog can never escalate to a human.

:func:`register_task` is the one primitive that closes all four: it validates
the owner up front, applies every DB mutation (supersede + insert/update) in
one transaction, and writes the pointer only after the commit — **row-first**,
because a row without a pointer is the recoverable state (re-running the
registration repairs it), while a pointer without a row is a dead end for
``cb-phase``. Both ``send_task`` and ``cb register-task`` call it; nothing
else may write ``{workspace}/state/.codeband_room`` (or the legacy
``<project_dir>/.codeband_room``) or a ``tasks`` row.

This module is deliberately import-clean of any Band/network client — it owns
only the DB (via :class:`~codeband.state.store.StateStore`) and the pointer
file, so peer seeders can call it without Band credentials.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from codeband.config import AgentsConfig, CodebandConfig
from codeband.state.store import StateStore

# Name of the active-room pointer file. The single source of truth for
# "which task is active" as read by cb-phase, cb approve/reject, cleanup and
# doctor. Canonical location: ``{workspace}/state/`` — next to the
# ``orchestration.db`` it pairs with (see :func:`state_pointer_path`).
# Legacy location: the project dir (see :func:`legacy_pointer_path`).
ROOM_POINTER_NAME = ".codeband_room"

# The verdict legs registration understands. Anything else in
# ``agents.required_verdicts`` is a typo and fails registration loudly —
# a misspelled verdict must never silently become an ungated merge.
KNOWN_VERDICTS: frozenset[str] = frozenset({"verify", "review"})

# What an absent / default ``agents.merge_approval`` resolves to: the task
# owner approves every merge. Snapshotted onto the tasks row like
# ``required_verdicts``.
DEFAULT_MERGE_APPROVAL = "owner"

# What an absent ``agents.required_verdicts`` key resolves to: both legs.
DEFAULT_REQUIRED_VERDICTS: tuple[str, ...] = ("verify", "review")


@dataclass
class RegistrationResult:
    """Outcome of one :func:`register_task` call."""

    room_id: str
    # "registered"    — fresh row inserted (no prior valid registration).
    # "re-registered" — a row for this room already existed; owner updated.
    # "superseded"    — a *different* active task was superseded first.
    outcome: str
    superseded_task_id: str | None = None


def resolve_required_verdicts(agents: AgentsConfig) -> list[str]:
    """Resolve and validate ``agents.required_verdicts`` for registration.

    Resolution happens at *registration* time — the result is snapshotted onto
    the tasks row so later config edits cannot change an in-flight task:

    * key absent (``None``) → the default ``["verify", "review"]``
    * present and non-empty → taken verbatim
    * explicitly ``[]`` → :class:`ValueError`, unless
      ``agents.allow_ungated_merge`` is also set (the deliberately ugly
      escape hatch for "merge with zero verdicts")

    Every verdict in the resolved list is then validated as *executable*:

    * an unknown name (not in :data:`KNOWN_VERDICTS`) fails, naming the entry
      — typo protection, since a missing verdict would silently weaken gating
    * ``verify`` requires ``agents.handoff_verify_command`` to be set; this
      intentionally turns a fresh install's silent verify-skip into a loud
      fail-at-seed
    * ``review`` has no precondition

    Raises :class:`ValueError` with an actionable message; returns the
    resolved list on success.
    """
    configured = agents.required_verdicts
    if configured is None:
        resolved = list(DEFAULT_REQUIRED_VERDICTS)
    elif not configured:
        if not agents.allow_ungated_merge:
            raise ValueError(
                "register_task: agents.required_verdicts is [] — every PR "
                "would merge with zero verdicts. Set agents.allow_ungated_merge: "
                "true to explicitly allow ungated merges, or list the verdicts "
                "this task requires (e.g. [verify, review])."
            )
        resolved = []
    else:
        resolved = list(configured)

    unknown = [v for v in resolved if v not in KNOWN_VERDICTS]
    if unknown:
        known = ", ".join(sorted(KNOWN_VERDICTS))
        raise ValueError(
            f"register_task: unknown verdict {unknown[0]!r} in "
            f"agents.required_verdicts — known verdicts: {known}. "
            "Fix the typo or remove the entry."
        )

    if "verify" in resolved and not agents.handoff_verify_command:
        raise ValueError(
            "register_task: agents.required_verdicts includes 'verify' but "
            "agents.handoff_verify_command is not set — the verify leg would "
            "be unexecutable. Set your test command (agents."
            "handoff_verify_command in codeband.yaml) or remove 'verify' "
            "from required_verdicts."
        )

    return resolved


def resolve_merge_approval(agents: AgentsConfig) -> str:
    """Resolve and validate ``agents.merge_approval`` for registration.

    Like :func:`resolve_required_verdicts`, resolution happens at
    *registration* time and the result is snapshotted onto the tasks row, so a
    mid-task config edit cannot change an in-flight task's approver:

    * ``"owner"`` (the default) — the task owner approves every merge
    * ``"human:<handle>"`` — the named human approves (the handle must be
      non-empty)
    * ``"none"`` — reserved: rejected with a message saying unapproved merges
      are not supported in V1
    * anything else fails registration loudly (typo protection — a mistyped
      approver must never silently become a different routing)

    Raises :class:`ValueError` with an actionable message; returns the
    validated value on success.
    """
    value = agents.merge_approval
    if value == "owner":
        return value
    if value == "none":
        raise ValueError(
            "register_task: agents.merge_approval is 'none' — unapproved "
            "merges are not supported in V1. Use 'owner' (default) or "
            "'human:<handle>'."
        )
    if value.startswith("human:"):
        handle = value[len("human:"):]
        if not handle:
            raise ValueError(
                "register_task: agents.merge_approval 'human:' names no "
                "handle — use 'human:<handle>' (e.g. human:yoni)."
            )
        return value
    raise ValueError(
        f"register_task: unknown merge_approval {value!r} — expected 'owner' "
        "(default) or 'human:<handle>'. Fix the typo."
    )


def resolve_state_dir(config: CodebandConfig, project_dir: Path) -> Path:
    """Resolve the workspace ``state/`` dir — same resolution the store uses.

    Delegates to :func:`codeband.config.resolve_workspace_path` (the one
    shared workspace rule, ``$WORKSPACE``-aware — same semantics as the
    runner), plus ``state/`` — the directory holding ``orchestration.db``,
    ``memories.jsonl`` and the active-room pointer.
    """
    from codeband.config import resolve_workspace_path

    return resolve_workspace_path(config, project_dir) / "state"


def state_pointer_path(state_dir: Path) -> Path:
    """Canonical pointer location: ``{workspace}/state/.codeband_room``.

    Next to the ``orchestration.db`` it pairs with — the pointer names a
    ``tasks`` row in that exact DB, so the two must travel (and be wiped)
    together. In Docker, ``state/`` is the shared volume every container
    mounts, so the pointer is visible swarm-wide for free.
    """
    return state_dir / ROOM_POINTER_NAME


def legacy_pointer_path(project_dir: Path) -> Path:
    """Legacy pointer location: ``<project_dir>/.codeband_room``.

    Read as a fallback (with a deprecation warning) and removed on the next
    registration; nothing writes it anymore.
    """
    return project_dir / ROOM_POINTER_NAME


def read_room_pointer(
    project_dir: Path, state_dir: Path, *, warn_legacy: bool = True,
) -> str | None:
    """Return the active room id, or ``None`` if no pointer resolves.

    Reads the canonical ``{state_dir}/.codeband_room`` first; falls back to
    the legacy ``<project_dir>/.codeband_room`` (one-line stderr deprecation
    warning naming both paths, suppressible via ``warn_legacy=False`` for
    callers that are about to migrate/remove it anyway).
    """
    new_pointer = state_pointer_path(state_dir)
    try:
        room_id = new_pointer.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        room_id = ""
    if room_id:
        return room_id

    legacy = legacy_pointer_path(project_dir)
    try:
        room_id = legacy.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if room_id and warn_legacy:
        print(
            f"warning: read legacy active-room pointer {legacy}; the pointer "
            f"now lives at {new_pointer} (the next registration migrates it).",
            file=sys.stderr,
        )
    return room_id or None


def register_task(
    *,
    room_id: str,
    description: str,
    owner_id: str,
    agents: AgentsConfig,
    owner_handle: str | None = None,
    project_dir: Path,
    store: StateStore,
) -> RegistrationResult:
    """Register *room_id* as the active task: tasks row + pointer, row-first.

    ``owner_id`` is required and must be non-empty — a missing owner raises
    :class:`ValueError` before anything is written. ``agents`` (the project's
    ``AgentsConfig``) is required because the task's verdict legs are resolved
    and validated here, at registration time — see
    :func:`resolve_required_verdicts` — and the resolved list is snapshotted
    onto the tasks row. Validation lives in this primitive, not the CLI
    wrappers, so both seeding paths (``cb task`` and ``cb register-task``)
    fail loudly on an unexecutable or mistyped verdict list before anything
    is written.

    One active task at a time is enforced here: if the pointer currently
    names a *different* room with a live row, that task is marked
    ``'superseded'`` in the same transaction that registers the new one.
    Re-registering the same room updates only the owner fields and the
    verdict snapshot (description/status untouched — the snapshot is
    re-resolved from *current* config, consistent with re-register-updates-
    owner) and rewrites the pointer, so the call is safe to retry — including
    over the half-states the old writers could leave behind
    (row-without-pointer, pointer-without-row).

    The pointer write happens strictly after the DB commit and any failure
    propagates loudly: the resulting row-without-pointer state is exactly what
    a re-run repairs.
    """
    if not owner_id:
        raise ValueError(
            "register_task: owner_id is required and must be non-empty — "
            "every task needs an owner the watchdog can escalate to."
        )
    if not room_id:
        raise ValueError("register_task: room_id is required and must be non-empty.")

    # Resolve + validate the verdict legs and the merge approver before
    # anything is written — a bad list (typo, unexecutable verify, accidental
    # []) or a bad approver must fail at seed time.
    required_verdicts = resolve_required_verdicts(agents)
    merge_approval = resolve_merge_approval(agents)

    # The pointer lives next to the DB the store owns — derive its directory
    # from the store itself so the two can never disagree on resolution.
    state_dir = Path(store.db_path).parent
    pointer_room = read_room_pointer(project_dir, state_dir, warn_legacy=False)

    # A pointer to a different room only matters if that room has a live row;
    # a dangling pointer (no row) is the invalid H2 state and is simply
    # overwritten by the fresh registration.
    supersede_task_id: str | None = None
    if pointer_room is not None and pointer_room != room_id:
        if store.get_task(pointer_room) is not None:
            supersede_task_id = pointer_room

    # All DB mutations — supersede + insert/update — land in one transaction.
    db_outcome = store.register_task_atomic(
        task_id=room_id,
        description=description,
        room_id=room_id,
        owner_id=owner_id,
        owner_handle=owner_handle,
        required_verdicts=required_verdicts,
        merge_approval=merge_approval,
        supersede_task_id=supersede_task_id,
    )

    # Row-first: the pointer is written only after the commit. A failure here
    # is raised loudly — the row already exists, so re-running register_task
    # for the same room repairs the pointer. Written to the canonical
    # location next to the DB; a stale legacy pointer is then removed
    # (best-effort) so the two locations can never disagree — this is the
    # migration path for pre-relocation installs.
    pointer = state_pointer_path(state_dir)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(room_id, encoding="utf-8")
    legacy = legacy_pointer_path(project_dir)
    if legacy != pointer:
        try:
            legacy.unlink(missing_ok=True)
        except OSError:
            pass  # reads prefer the canonical location; a leftover is inert

    if supersede_task_id is not None:
        outcome = "superseded"
    elif db_outcome == "updated":
        outcome = "re-registered"
    else:
        outcome = "registered"
    return RegistrationResult(
        room_id=room_id,
        outcome=outcome,
        superseded_task_id=supersede_task_id,
    )
