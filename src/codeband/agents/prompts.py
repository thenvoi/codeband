"""Shared prompt loading utility for agent adapters."""

from __future__ import annotations

from pathlib import Path


def load_prompt(path: Path, fallback: str = "See Codeband documentation.") -> str:
    """Load a prompt from file, returning fallback if not found."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


def build_review_prompt(
    custom_prompt: str | None,
    review_guidelines: str | None,
    default_prompt: Path,
) -> str:
    """Build a reviewer prompt with optional guidelines.

    Shared by both Code Reviewer and Plan Reviewer agents.
    """
    prompt = custom_prompt or load_prompt(default_prompt)
    if review_guidelines:
        prompt += f"\n\n## Additional Review Guidelines\n{review_guidelines}\n"
    return prompt
