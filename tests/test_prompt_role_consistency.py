"""Tests for prompt consistency around conductor/planning responsibilities."""

from pathlib import Path


def test_conductor_prompt_keeps_technical_work_out_of_role():
    prompt = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "You are a coordinator, not an implementer or debugger." in prompt
    assert "Do **not** analyze code, debug failing tests, design implementations, or propose patches yourself." in prompt
    assert "provide a fix" not in prompt


def test_plan_review_trigger_is_planner_message_not_conductor_relay():
    planner = Path("src/codeband/prompts/planner.md").read_text(encoding="utf-8")
    reviewer = Path("src/codeband/prompts/plan_reviewer.md").read_text(encoding="utf-8")

    assert "a concrete **Plan Reviewer** from the Worker Pool Roster" in planner
    assert "@Plan-Reviewer-Codex-0" in planner
    assert "This is the primary delivery mechanism and is what starts plan review." in planner
    assert "When the Conductor sends you a plan for review" not in reviewer
    assert "When the Planner sends a plan message that @mentions both you and the Conductor" in reviewer


def test_mergemaster_conflict_reports_require_verification_artifacts():
    """Mergemaster must demand `gh pr view --json mergeable,mergeStateStatus`,
    `git diff --name-only --diff-filter=U`, and verbatim git stderr in every
    conflict report; the Conductor must verify before forwarding to the Coder.

    This pins the guardrail introduced after a Mergemaster hallucinated a
    merge conflict with a non-existent PR. Removing any of these phrases from
    the prompts re-opens that bug.
    """
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(encoding="utf-8")
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "git diff --name-only --diff-filter=U" in mergemaster
    assert "gh pr view <pr-number> --json mergeable,mergeStateStatus" in mergemaster
    assert "Last lines of `git merge` stderr:" in mergemaster
    # Cross-check: when gh says MERGEABLE/CLEAN, do not declare a conflict.
    assert 'gh pr view' in mergemaster and '"mergeable": "MERGEABLE"' in mergemaster

    # Conductor must refuse to forward an evidence-less conflict to the Coder.
    assert "Verify before forwarding to the Coder" in conductor
    assert "gh pr view --json mergeable,mergeStateStatus" in conductor


def test_planner_forbids_implementation_code_in_plans():
    """The Planner must describe WHAT to build, not HOW to implement it."""
    planner = Path("src/codeband/prompts/planner.md").read_text(encoding="utf-8")
    plan_reviewer = Path(
        "src/codeband/prompts/plan_reviewer.md",
    ).read_text(encoding="utf-8")

    assert "Plans describe WHAT, not HOW" in planner
    assert "Do **not** include in the plan" in planner
    assert "Function or method bodies" in planner

    # Plan Reviewer flags implementation code as a blocking issue.
    assert "Plan vs. Implementation Boundary" in plan_reviewer
    assert "[Blocking]" in plan_reviewer
    assert "Function or method bodies the Coder is supposed to write" in plan_reviewer


def test_coder_dispatches_review_directly_to_opposite_framework_reviewer():
    """Coder/reviewer review traffic is direct; Conductor observes.

    Pins the direct-dispatch invariant: at PR completion, the Coder
    @-mentions an opposite-framework Reviewer alongside the Conductor.
    Review failures and re-review requests also move directly between the
    owning Coder and same Reviewer, without forcing a Conductor relay.
    """
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")
    code_reviewer = Path(
        "src/codeband/prompts/code_reviewer.md",
    ).read_text(encoding="utf-8")
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    # Coder side: mention BOTH the Reviewer and the Conductor; pick from the roster.
    assert "@mentioning **only @Conductor**" not in coder, (
        "Coder prompt still routes through the Conductor — the relay was "
        "supposed to be removed in Bug 4."
    )
    assert "@mention @Conductor for awareness" in coder
    # Coder picks the reviewer themselves via peer discovery — not via a
    # Conductor relay, and not from a static hard-coded roster (lazy invites).
    # Discovery filters on `description` (the semantic field), not on a name
    # pattern, since names are an internal convention and descriptions carry
    # the role + framework signal we actually want to match on.
    assert "Pick the reviewer through discovery on `description`" in coder
    assert "thenvoi_lookup_peers()" in coder
    assert "role=code_review_agent" in coder
    assert "framework=Codex" in coder

    # Code Reviewer side: expects direct dispatch from the Coder and direct
    # failure reporting back to the PR owner.
    assert "A Coder @mentions you directly once their PR has passed verification" in code_reviewer
    assert "@mention **both the PR-owning Coder and @Conductor**" in code_reviewer
    assert (
        "Codeband task branches have the form `codeband/<coder-worker-id>/<branch_slug>`"
        in code_reviewer
    )

    # Coder side: after fixes, go back to the same reviewer, not via a generic relay.
    assert "@mention **the same Reviewer and @Conductor**" in coder
    assert "Use the Reviewer who failed the PR" in coder

    # Conductor side: stays silent whenever the direct path already reached the
    # next actor, and only falls back when owner/reviewer identity is missing.
    assert "Coder's @mention to the Reviewer is the dispatch" in conductor
    assert "Stay silent if the Reviewer already @mentioned the PR owner" in conductor
    assert "The same Reviewer re-reviews directly" in conductor


def test_human_approval_wait_state_suppresses_watchdog():
    """Human approval is an idle-but-not-complete state, so the watchdog must not nudge."""
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")
    watchdog = Path("src/codeband/agents/watchdog.py").read_text(encoding="utf-8")

    assert "swarm status waiting_human_approval" in conductor
    assert "When the human approves merge" in conductor
    assert '"waiting_human_approval"' in watchdog


def test_protocols_use_task_scoped_correlation_ids():
    """Prompt-level state must remain unambiguous with multiple active workers."""
    planner = Path("src/codeband/prompts/planner.md").read_text(encoding="utf-8")
    plan_reviewer = Path("src/codeband/prompts/plan_reviewer.md").read_text(
        encoding="utf-8",
    )
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")
    code_reviewer = Path(
        "src/codeband/prompts/code_reviewer.md",
    ).read_text(encoding="utf-8")

    assert "Create a short `task_key`" in conductor
    assert "max 32 characters" in conductor
    assert "Never use the full task text in protocol IDs or branch names" in conductor
    assert "plan_<task_key>_r<round>" in planner
    assert "plr_<task_key>_r<round>" in plan_reviewer
    assert "ta_<task_key>_<subtask_id>" in conductor
    assert "cr_<pr>_r<round> task <task_key>" in code_reviewer
    assert "cr_<pr>_r2 task <task_key>" in coder


def test_plan_revision_returns_to_same_plan_reviewer():
    planner = Path("src/codeband/prompts/planner.md").read_text(encoding="utf-8")
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "same Plan Reviewer" in planner
    assert "send the revised plan to @Conductor and the **same Plan Reviewer**" in planner
    assert "Please revise and send the revised plan to the same Plan Reviewer" in conductor
    assert "only after the revised plan is approved" in conductor


def test_mergemaster_only_processes_explicit_prs():
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "process **only the PR URL(s) explicitly listed in that @mention**" in mergemaster
    assert "Do not scan chat for other recent merge requests" in mergemaster
    assert "please merge only these approved PRs" in conductor


def test_prs_must_target_repo_base_before_merge_routing():
    """Dependent feature-branch PRs must be retargeted, not manually merged."""
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")

    assert "gh pr view <N> --json baseRefName,headRefName,state" in conductor
    assert "do **not** route it to @Mergemaster" in conductor
    assert "Please retarget the PR to the repo base branch" in conductor

    assert (
        "gh pr view <pr-number> --json baseRefName,headRefName,state,mergeable,mergeStateStatus"
        in mergemaster
    )
    assert "retarget required" in mergemaster
    assert "Do **not** manually merge" in mergemaster

    assert "PR base branch invariant" in coder
    assert "must target the repository base branch" in coder
    assert "gh pr create --base <repo-base>" in coder


def test_coder_starts_tasks_from_clean_assigned_branch():
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")

    assert "git checkout --detach origin/<repo-base>" in coder
    assert "git clean -fd" in coder
    assert "git branch -D <assigned-branch>" in coder
    assert "If the current branch is not the assigned branch" in coder
    assert "current branch, expected branch, base ref, and dirty paths" in coder


def test_coder_stop_reports_include_concrete_reason():
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")

    assert "Never send a generic completion failure" in coder
    assert "I stopped before completing this request" in coder
    assert "include the concrete stop reason" in coder


def test_conductor_cleans_up_superseded_prs_on_reassignment():
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "Reassignment Cleanup" in conductor
    assert "superseded by reassignment" in conductor
    assert "before assigning a replacement" in conductor


def test_mergemaster_uses_separate_git_commands_for_stale_checkout_retry():
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )

    assert "Run git and gh commands one at a time" in mergemaster
    assert "Do not chain fetch/reset/merge commands with `&&`" in mergemaster
    assert "git fetch origin && git reset --hard" not in mergemaster


def test_mergemaster_hard_resets_to_remote_base_before_every_merge():
    """Stopped cb sessions must not leave local-only master commits in play."""
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )

    assert "Never trust local base-branch state" in mergemaster
    assert "At the start of every merge request" in mergemaster
    assert "git merge --abort" in mergemaster
    assert "git rebase --abort" in mergemaster
    assert "git reset --hard origin/<repo-base>" in mergemaster
    assert "git clean -fd" in mergemaster
    assert "git status --short" in mergemaster
    assert "local base branch that is ahead of `origin/<repo-base>`" in mergemaster


def test_coder_prompt_forbids_repo_and_head_flags_on_gh_pr_create():
    """Hard guarantee: PR #1469 was opened against Delgan/loguru because the
    Coder's recovery path used `--head ofermend:<branch>` without `--repo`,
    so `gh pr create` defaulted to the upstream parent. The fix makes the
    canonical command plain (no `--repo`, no `--head`) and the prompt now
    explicitly forbids those flags. Any regression here re-opens the bug.
    """
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")

    assert "PR destination invariant" in coder
    assert "Codeband pre-pins your worktree's `gh` default repo" in coder
    # The forbidden-flag rule must name both flags by name.
    assert "Do **not** add `--repo` or `--head <owner>:<branch>` flags" in coder
    # The canonical command is the plain form — no `--repo` after `gh pr create`.
    canonical_block_present = (
        "gh pr create --base <repo-base> --title \"<task summary>\" --body" in coder
    )
    assert canonical_block_present, "Plain `gh pr create` form must remain canonical"
    # Post-creation destination check — Coder must verify the PR landed in the
    # configured repo before reporting completion.
    assert "headRepositoryOwner" in coder and "headRepository" in coder
    assert "gh pr close --repo <wrong-owner/wrong-repo>" in coder


def test_conductor_repo_pin_section_renders():
    """Conductor's prompt must receive a Configured Repository section that
    forbids routing wrong-repo PRs. Built by `runner._build_repo_pin`."""
    from codeband.config import (
        AgentsConfig, BandConfig, CodebandConfig, FrameworkPool, PoolEntry,
        RepoConfig, ReviewersConfig, WorkspaceConfig,
    )
    from codeband.orchestration.runner import _build_repo_pin

    config = CodebandConfig(
        repo=RepoConfig(url="https://github.com/ofermend/loguru.git"),
        workspace=WorkspaceConfig(path="/tmp/ws"),
        band=BandConfig(),
        agents=AgentsConfig(
            coders=FrameworkPool(claude_sdk=PoolEntry(count=1)),
            reviewers=ReviewersConfig(claude_sdk=PoolEntry(count=1)),
            planners=FrameworkPool(claude_sdk=PoolEntry(count=1)),
        ),
    )
    pin = _build_repo_pin(config)
    assert pin is not None
    assert "## Configured Repository" in pin
    assert "ofermend/loguru" in pin
    assert "headRepositoryOwner.login" in pin
    # The Conductor MUST close wrong-repo PRs and refuse to route them.
    assert "gh pr close <num> --repo <wrong-owner>/<wrong-repo>" in pin
    assert "Do NOT route the wrong PR" in pin


def test_conductor_repo_pin_skips_non_github_url():
    """Non-GitHub URLs (GitLab, self-hosted) → no repo pin (gh's destination
    invariant doesn't apply to non-GitHub)."""
    from codeband.config import (
        AgentsConfig, BandConfig, CodebandConfig, FrameworkPool, PoolEntry,
        RepoConfig, ReviewersConfig, WorkspaceConfig,
    )
    from codeband.orchestration.runner import _build_repo_pin

    config = CodebandConfig(
        repo=RepoConfig(url="https://gitlab.example.com/group/proj.git"),
        workspace=WorkspaceConfig(path="/tmp/ws"),
        band=BandConfig(),
        agents=AgentsConfig(
            coders=FrameworkPool(claude_sdk=PoolEntry(count=1)),
            reviewers=ReviewersConfig(claude_sdk=PoolEntry(count=1)),
            planners=FrameworkPool(claude_sdk=PoolEntry(count=1)),
        ),
    )
    assert _build_repo_pin(config) is None


# ── Stage-2 chunk 3: the gated merge edge in prompts (code↔prompt contract) ──


def test_mergemaster_merges_only_through_the_gate():
    """Stage-2: the Mergemaster never executes a merge itself — every merge,
    in the main flow AND inside the bisect, is one gated ``cb-phase merge``
    call per PR. A direct ``gh`` merge reappearing anywhere in the prompt
    re-opens the ungated-merge hole the FSM gate exists to close.
    """
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )

    assert "cb-phase merge <subtask_id> --pr <pr-number>" in mergemaster
    # One assertion covers both flows: no direct gh merge anywhere.
    assert "gh pr merge" not in mergemaster
    assert "--admin" not in mergemaster
    # The bisect's intermediate merges are gated per-subtask too.
    assert "Every intermediate merge in the bisect is gated per-subtask" in mergemaster
    # Outcome discipline: rest on awaiting-approval, report needs_rebase,
    # stop dead on blocked.
    assert "done with this PR for now" in mergemaster
    assert "needs_rebase" in mergemaster
    assert "rebase the branch yourself" in mergemaster
    assert "never attempt to route around a blocked subtask" in mergemaster
    # Merge requests carry the subtask id the gate call needs.
    assert "ask the Conductor for it before processing that PR" in mergemaster


def test_conductor_halt_discipline_covers_merge_gate():
    """The merge edge is gated exactly like verify/review: a gate refusal is
    authoritative, and ``needs_rebase`` routes back to the owning Coder."""
    conductor = Path("src/codeband/prompts/conductor.md").read_text(
        encoding="utf-8",
    )

    assert "The merge edge is gated exactly like verify and review." in conductor
    assert "A merge refused by the gate is authoritative" in conductor
    # needs_rebase is rework routing, with verdict-void instructions.
    assert "Rebase routing (`needs_rebase`)" in conductor
    assert "All prior verdicts are void on the new SHA" in conductor
    # Merge requests to the Mergemaster include subtask ids for the gate call.
    assert "subtask st-1, risk:" in conductor
    # Awaiting-approval is a normal pause, not a failure to re-route.
    assert "awaiting approval" in conductor
    # Chat approval must not re-trigger Mergemaster routing — Conductor clarifies once, then silent.
    assert "chat reply does not advance the merge gate" in conductor


def test_coder_rebase_rework_reenters_verify_walk():
    """On a ``needs_rebase`` assignment the Coder rebases, pushes, and re-runs
    ``cb-phase verify`` — and knows prior verdicts are void on the new SHA."""
    coder = Path("src/codeband/prompts/coder.md").read_text(encoding="utf-8")

    assert "Rebase rework (`needs_rebase`)" in coder
    assert "git rebase origin/<repo-base>" in coder
    assert "All prior verdicts are void on the new SHA — by design." in coder
    # Post-rebase re-review goes back through the normal walk, same reviewer.
    assert "exactly as for a first submission" in coder
    assert "post-rebase re-review" in coder


def test_code_reviewer_verdict_commands_pin_the_pr():
    """Both ``cb-phase review`` verdict lines must carry ``--pr`` — the
    verdict head SHA is resolved from the PR head (cwd-independent), never
    from the reviewer's repo-less scratch directory, where a cwd-based HEAD
    could only ever record NULL and silently void every verdict at the merge
    gate (the 2026-06-10 Scenario A incident). This drift class shipped
    undetected precisely because no test pinned the reviewer's gate commands.
    """
    reviewer = Path("src/codeband/prompts/code_reviewer.md").read_text(
        encoding="utf-8",
    )

    assert (
        "cb-phase review <subtask_id> --task <task_id> --pr <pr-number> --reject"
        in reviewer
    )
    assert (
        "cb-phase review <subtask_id> --task <task_id> --pr <pr-number> --approve"
        in reviewer
    )
    # No verdict line may remain without the PR pin.
    for line in reviewer.splitlines():
        if "cb-phase review" in line and ("--approve" in line or "--reject" in line):
            assert "--pr" in line, f"unpinned verdict command: {line!r}"


def test_codeband_command_doc_grants_approval_instead_of_prohibiting():
    """As of the merge-execution leg, ``cb approve`` writes the SHA-pinned
    approval grant and the invoking agent is the task owner/approver — the old
    do-not-use prohibition is obsolete and must stay gone.

    A2 (F14): the approval flow now uses --no-notify so the coordinator can
    post the room notification as its own jam identity (not the human key).
    """
    doc = Path("docs/commands/codeband.md").read_text(encoding="utf-8")

    # A2: coordinator-specific grant path uses --no-notify.
    assert "cb approve --no-notify" in doc
    assert "SHA-pinned" in doc
    assert "Never approve blindly" in doc
    assert "Do not run `cb approve`" in doc  # the withhold path
    # A2: coordinator posts notification as its own identity.
    assert "Durable merge grant recorded for PR" in doc
    # Distinctive substring of the pre-merge-leg prohibition.
    assert "do NOT use `cb approve`" not in doc


def test_conductor_recovery_playbook_names_all_three_options_with_commands():
    """Batch 4 (findings 23–25): every BLOCKED escalation must offer the three
    concrete recovery options WITH their commands. Pinned because unpinned
    prompt contracts drift (the sweep-10 lesson)."""
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "## Recovery playbook (blocked subtasks)" in conductor
    assert "`cb-phase resume <subtask_id>`" in conductor
    assert "`cb-phase abandon <subtask_id>`" in conductor
    assert "Human intervention" in conductor
    assert "resume is NOT a cap reset" in conductor
    # Conductor-authority edges: never delegated to workers.
    assert "never delegate them to a Coder or the Mergemaster" in conductor


def test_not_eligible_flow_references_automatic_needs_rebase_routing():
    """SHA-stale verdicts route to needs_rebase automatically (the
    stale_verdicts tag); the bare not_eligible reject is the missing-leg
    case only. Both prompts must reflect the split."""
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )
    conductor = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "`REJECTED [stale_verdicts]`" in mergemaster
    assert "routed the subtask to `needs_rebase` automatically" in mergemaster
    # The bare-reject bullet survives, scoped to the missing-leg case.
    assert "`REJECTED [not_eligible]`" in mergemaster
    assert "a verdict leg is missing entirely" in mergemaster
    # Conductor's rebase routing names the stale-verdict cause too.
    assert "stale_verdicts" in conductor


def test_mergemaster_cb_approve_prohibition_is_restated():
    """Finding 18: the Mergemaster must never run the human-approval
    primitive itself — the old removal of this prohibition was wrong."""
    mergemaster = Path("src/codeband/prompts/mergemaster.md").read_text(
        encoding="utf-8",
    )

    assert "Never run `cb approve` yourself" in mergemaster
    assert "HUMAN's approval primitive" in mergemaster
    assert "exclusively through `cb-phase merge`" in mergemaster
