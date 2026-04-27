"""Invariants that protect the package-only prompt flow.

Agent prompts live inside the installed package and are never copied to the
user's project. These tests pin the behaviour so a future change that
reintroduces project-level overrides (or silently drops the packaged
prompt file) fails loudly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from codeband.cli import cli


class TestCbInitWritesNoPromptsDir:
    """`cb init` must not materialise a project-level `./prompts/` directory.

    The package is the single source of truth; writing copies would shadow
    future upstream improvements on `pip install -U codeband`.
    """

    def test_init_does_not_create_prompts_directory(self, tmp_path: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--dir", str(tmp_path), "--repo", "https://github.com/a/b.git"],
        )

        assert result.exit_code == 0, result.output
        assert (tmp_path / "codeband.yaml").exists()
        assert not (tmp_path / "prompts").exists()


class TestDoctorHasNoUpdatePromptsFlag:
    """The removed `--update-prompts` flag must stay gone."""

    @patch("codeband.doctor.report")
    @patch("codeband.doctor.run_all", new_callable=AsyncMock)
    def test_flag_is_unrecognised(self, mock_run_all, _mock_report, tmp_path: Path):
        from codeband.doctor import Context

        mock_run_all.return_value = (Context(project_dir=tmp_path), 0)
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--dir", str(tmp_path), "--update-prompts"])
        assert result.exit_code != 0
        assert "no such option" in result.output.lower()


class TestConductorPromptLoading:
    """`ClaudeConductorRunner` must load its system prompt from the installed package
    and compose runtime sections via typed kwargs — no cross-module private
    access from the runner.
    """

    def test_loads_packaged_prompt_without_kwargs(self):
        """With no kwargs, the Conductor reads `conductor.md` from the package."""
        from codeband.agents.conductor import _DEFAULT_PROMPT, ClaudeConductorRunner

        # The packaged file must exist — a renamed or deleted prompt would
        # fall back to `load_prompt`'s sentinel fallback string and the
        # Conductor would start with a useless system prompt.
        assert _DEFAULT_PROMPT.is_file(), (
            f"Packaged Conductor prompt missing at {_DEFAULT_PROMPT}"
        )
        expected = _DEFAULT_PROMPT.read_text(encoding="utf-8")

        runner = ClaudeConductorRunner()
        assert runner.adapter.custom_section == expected

    def test_worker_roster_and_auto_merge_append_to_prompt(self):
        from codeband.agents.conductor import ClaudeConductorRunner

        runner = ClaudeConductorRunner(
            worker_roster="# Worker Pool Roster\nROSTER-MARKER",
            auto_merge="green_only",
        )
        section = runner.adapter.custom_section
        assert "ROSTER-MARKER" in section
        assert "auto_merge: green_only" in section
        assert "## Current Configuration" in section

    def test_custom_prompt_overrides_composition(self):
        """When `custom_prompt` is provided the packaged prompt and
        compositional kwargs are all ignored — the caller owns the prompt."""
        from codeband.agents.conductor import ClaudeConductorRunner

        runner = ClaudeConductorRunner(
            custom_prompt="only this text",
            worker_roster="ignored-roster",
            auto_merge="ignored",
        )
        section = runner.adapter.custom_section
        assert section == "only this text"
        assert "ignored-roster" not in section
        assert "auto_merge" not in section
