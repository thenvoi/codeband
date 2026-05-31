"""Tests for `cb up` profile-gated pool resolution.

Two pools have profile-gated services in the bundled compose files —
planners and plan-reviewers. Each variant lives behind a docker compose
profile (``claude-planner`` / ``codex-planner`` and ``claude-plan-reviewer``
/ ``codex-plan-reviewer``). ``cb up`` derives the active profiles from
``codeband.yaml``, so flipping pool counts in config alone is enough to
switch frameworks — no manual compose edit, no leakage of stale
``COMPOSE_PROFILES`` values from the user's shell.
"""

from __future__ import annotations

from pathlib import Path

from codeband.cli import _apply_pool_profile, _pool_compose_profiles
from codeband.config import (
    AgentsConfig,
    CodebandConfig,
    FrameworkPool,
    PlanReviewersConfig,
    PoolEntry,
    RepoConfig,
)
from codeband.orchestration.compose import compose_env, compose_project_name


def _write_config(
    project: Path,
    planners: FrameworkPool | None = None,
    plan_reviewers: PlanReviewersConfig | None = None,
) -> None:
    agents_kwargs: dict = {}
    if planners is not None:
        agents_kwargs["planners"] = planners
    if plan_reviewers is not None:
        agents_kwargs["plan_reviewers"] = plan_reviewers
    cfg = CodebandConfig(
        repo=RepoConfig(url="https://x/y.git", branch="main"),
        agents=AgentsConfig(**agents_kwargs),
    )
    cfg.to_yaml(project / "codeband.yaml")


class TestPlannerComposeProfiles:
    def test_default_config_selects_claude_planner(self, tmp_path: Path):
        """The shipped default has Claude planner, count: 1."""
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
        )
        assert _pool_compose_profiles(tmp_path, "planners", "planner") == ["claude-planner"]

    def test_compose_project_name_is_codeband_scoped(self, tmp_path: Path):
        project = tmp_path / "My Codeband Project!"
        project.mkdir()

        assert compose_project_name(project) == "codeband-my-codeband-project"
        env = compose_env(project)
        assert env["CODEBAND_PROJECT_DIR"] == str(project)
        assert env["COMPOSE_PROJECT_NAME"] == "codeband-my-codeband-project"

    def test_codex_only_config_selects_codex_planner(self, tmp_path: Path):
        """Flipping to Codex planner in config alone activates the right profile."""
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=1, model="gpt-5.5"),
            ),
        )
        assert _pool_compose_profiles(tmp_path, "planners", "planner") == ["codex-planner"]

    def test_no_planner_returns_empty(self, tmp_path: Path):
        """count: 0 on both is allowed (no planner runs)."""
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=0),
            ),
        )
        assert _pool_compose_profiles(tmp_path, "planners", "planner") == []

    def test_missing_config_returns_empty(self, tmp_path: Path):
        """No codeband.yaml → no profile activated, with a warning printed.

        Safer to start no planner than the wrong one — the user will see
        the missing-config warning on stderr and can fix it.
        """
        # tmp_path is empty; no codeband.yaml exists.
        assert _pool_compose_profiles(tmp_path, "planners", "planner") == []


class TestApplyPlannerProfile:
    """Verifies the env-mutation semantics: config wins, user state preserved."""

    def test_existing_planner_profile_is_replaced_by_config(self, tmp_path: Path):
        """Pre-set COMPOSE_PROFILES=claude-planner with Codex-only config.

        Regression: previous implementation appended derived profiles, which
        meant a stale ``COMPOSE_PROFILES=claude-planner`` in the user's
        shell would force both planners to run. Now the planner-related
        values are filtered out before the config-derived profile is added.
        """
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=1, model="gpt-5.5"),
            ),
        )

        env = {"COMPOSE_PROFILES": "claude-planner,debug"}
        _apply_pool_profile(env, tmp_path, "planners", "planner")

        # Order: unrelated profiles first (in original order), then derived.
        result = env["COMPOSE_PROFILES"].split(",")
        assert "claude-planner" not in result, (
            f"stale claude-planner leaked through: {env['COMPOSE_PROFILES']}"
        )
        assert "codex-planner" in result
        assert "debug" in result, "unrelated user profile must be preserved"

    def test_no_planner_in_config_strips_stale_planner(self, tmp_path: Path):
        """When the config asks for no planner, stale planner values are stripped.

        Regression: the old implementation short-circuited on empty derived
        profiles and let a stale ``COMPOSE_PROFILES=claude-planner`` survive,
        so Docker would still start a planner the config didn't ask for.
        Config now wins absolutely — only unrelated user profiles survive.
        """
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=0),
            ),
        )

        env = {"COMPOSE_PROFILES": "claude-planner,debug"}
        _apply_pool_profile(env, tmp_path, "planners", "planner")

        # Stale claude-planner is stripped; debug (unrelated) survives.
        assert env["COMPOSE_PROFILES"] == "debug"

    def test_no_planner_in_config_with_only_stale_planner_drops_key(
        self, tmp_path: Path,
    ):
        """If stripping stale leaves nothing, COMPOSE_PROFILES is removed.

        Avoids leaving an empty-string env value, which docker compose
        would interpret as a malformed profile list.
        """
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=0),
            ),
        )

        env = {"COMPOSE_PROFILES": "claude-planner"}
        _apply_pool_profile(env, tmp_path, "planners", "planner")

        assert "COMPOSE_PROFILES" not in env

    def test_no_existing_compose_profiles_writes_just_derived(self, tmp_path: Path):
        """Empty starting env: COMPOSE_PROFILES becomes exactly the derived set."""
        _write_config(
            tmp_path,
            FrameworkPool(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
        )

        env: dict[str, str] = {}
        _apply_pool_profile(env, tmp_path, "planners", "planner")

        assert env["COMPOSE_PROFILES"] == "claude-planner"


class TestApplyPlanReviewerProfile:
    """Plan reviewer pool gets the same treatment as the planner pool.

    Symmetric coverage so docs that say "flip the opposite for
    plan_reviewers" work end-to-end in Docker.
    """

    def test_inverted_pair_activates_claude_plan_reviewer(self, tmp_path: Path):
        """Codex planner + Claude plan-reviewer (the inversion).

        Default config has Claude planner + Codex plan-reviewer. To
        invert, the user flips both pools. With profile-gated services,
        `cb up` must derive both ``codex-planner`` and
        ``claude-plan-reviewer`` from this config.
        """
        _write_config(
            tmp_path,
            planners=FrameworkPool(
                claude_sdk=PoolEntry(count=0),
                codex=PoolEntry(count=1, model="gpt-5.5"),
            ),
            plan_reviewers=PlanReviewersConfig(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
        )

        env: dict[str, str] = {}
        _apply_pool_profile(env, tmp_path, "planners", "planner")
        _apply_pool_profile(env, tmp_path, "plan_reviewers", "plan-reviewer")

        result = env["COMPOSE_PROFILES"].split(",")
        assert "codex-planner" in result
        assert "claude-plan-reviewer" in result
        assert "claude-planner" not in result
        assert "codex-plan-reviewer" not in result

    def test_stale_plan_reviewer_is_stripped(self, tmp_path: Path):
        """Same replacement guarantee as planner — pre-set profile is stripped."""
        _write_config(
            tmp_path,
            plan_reviewers=PlanReviewersConfig(
                claude_sdk=PoolEntry(count=1),
                codex=PoolEntry(count=0),
            ),
        )

        env = {"COMPOSE_PROFILES": "codex-plan-reviewer,monitoring"}
        _apply_pool_profile(env, tmp_path, "plan_reviewers", "plan-reviewer")

        result = env["COMPOSE_PROFILES"].split(",")
        assert "codex-plan-reviewer" not in result
        assert "claude-plan-reviewer" in result
        assert "monitoring" in result
