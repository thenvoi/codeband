"""Shared prompt loading utility for agent adapters."""

from __future__ import annotations

from pathlib import Path

_KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"


def load_prompt(path: Path, fallback: str = "See Codeband documentation.") -> str:
    """Load a prompt from file, returning fallback if not found."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


def load_knowledge(*names: str) -> str:
    """Load and concatenate knowledge guides by name, wrapped in a header."""
    bodies = []
    for name in names:
        path = _KNOWLEDGE_DIR / f"{name}.md"
        bodies.append(path.read_text(encoding="utf-8"))
    if not bodies:
        return ""
    body = "\n\n".join(bodies)
    return (
        "\n\n# Engineering Knowledge Base\n\n"
        "The following guides define the craft standards for your work. Treat them as "
        "part of your instructions. When a guide and the target repository disagree, the "
        "target repository's conventions win.\n\n"
        + body
    )


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


def build_verify_prompt(custom_prompt: str | None, default_prompt: Path) -> str:
    """Build a Verifier prompt. Mirrors :func:`build_review_prompt` without
    guidelines — ``VerifiersConfig`` has no ``review_guidelines`` knob, so the
    Verifier's instructions are ``prompts/verifier.md`` verbatim (or an
    explicit ``custom_prompt`` override).
    """
    return custom_prompt or load_prompt(default_prompt)
