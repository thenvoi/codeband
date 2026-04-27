"""Tests for prompt instructions around environment error reporting."""

from __future__ import annotations

from pathlib import Path


def test_code_reviewer_prompt_reports_actual_gh_reason():
    prompt = Path("src/codeband/prompts/code_reviewer.md").read_text(encoding="utf-8")

    assert "Unable to review PR #N — gh failed:" in prompt
    assert "gh CLI access blocked" in prompt
    assert "Include the real stderr/tool error text" in prompt


def test_conductor_prompt_escalates_with_concrete_reason():
    prompt = Path("src/codeband/prompts/conductor.md").read_text(encoding="utf-8")

    assert "Code Reviewer cannot access PR #N — gh failed:" in prompt
    assert "Do not fabricate a review result from error messages." in prompt
