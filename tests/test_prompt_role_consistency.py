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
    assert (
        "@mentioning **both an opposite-framework Code Reviewer and @Conductor**"
        in coder
    )
    assert "Pick the reviewer from the Worker Pool Roster" in coder

    # Code Reviewer side: expects direct dispatch from the Coder and direct
    # failure reporting back to the PR owner.
    assert "A Coder @mentions you directly at PR completion" in code_reviewer
    assert "@mention **both the PR-owning Coder and @Conductor**" in code_reviewer
    assert (
        "Codeband task branches have the form `codeband/<coder-worker-id>/<branch_slug>`"
        in code_reviewer
    )

    # Coder side: after fixes, go back to the same reviewer, not via a generic relay.
    assert "both the same Reviewer and @Conductor" in coder
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
