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

    assert "@mentioning both @Conductor and @Plan Reviewer" in planner
    assert "This is the primary delivery mechanism and is what starts plan review." in planner
    assert "When the Conductor sends you a plan for review" not in reviewer
    assert "When the Planner sends a plan message that @mentions both you and the Conductor" in reviewer
