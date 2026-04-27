"""Fetch, sort, and rank open GitHub issues for task selection."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


def issue_url(slug: str, number: int) -> str:
    """Build a GitHub issue URL."""
    return f"https://github.com/{slug}/issues/{number}"


# ---------------------------------------------------------------------------
# Issue dataclass
# ---------------------------------------------------------------------------

_GH_LIST_FIELDS = [
    "number", "title", "author", "labels", "createdAt", "updatedAt", "comments",
]

_GH_DETAIL_FIELDS = _GH_LIST_FIELDS + ["body"]


@dataclass
class IssueInfo:
    """Parsed GitHub issue metadata."""

    number: int
    title: str
    author: str
    labels: list[str]
    created_at: str
    updated_at: str
    comments: int
    body: str = ""

    @classmethod
    def from_gh(cls, data: dict) -> IssueInfo:
        return cls(
            number=data["number"],
            title=data["title"],
            author=data["author"]["login"],
            labels=[label["name"] for label in data.get("labels", [])],
            created_at=data["createdAt"],
            updated_at=data["updatedAt"],
            comments=len(data.get("comments", [])),
            body=data.get("body", ""),
        )

    def summary_line(self, slug: str | None = None) -> str:
        labels = f" [{', '.join(self.labels)}]" if self.labels else ""
        link = ""
        if slug:
            link = f"  {issue_url(slug, self.number)}"
        return (
            f"#{self.number:<6} {self.title[:60]:<60}  "
            f"by {self.author:<12} "
            f"{self.comments} comments{labels}{link}"
        )


# ---------------------------------------------------------------------------
# Fetch from gh CLI
# ---------------------------------------------------------------------------

def _run_gh(cmd: list[str]) -> str:
    """Run a gh CLI command and return stdout."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError(
            "The 'gh' CLI is not installed. "
            "Install it from https://cli.github.com and run 'gh auth login'."
        )
    if result.returncode != 0:
        raise RuntimeError(f"gh command failed: {result.stderr.strip()}")
    return result.stdout


def fetch_open_issues(
    slug: str, *, limit: int = 30, label: str | None = None,
) -> list[dict]:
    """Fetch open issues via the gh CLI. Returns raw JSON dicts."""
    cmd = [
        "gh", "issue", "list",
        "--repo", slug,
        "--state", "open",
        "--limit", str(limit),
        "--json", ",".join(_GH_LIST_FIELDS),
    ]
    if label:
        cmd.extend(["--label", label])
    return json.loads(_run_gh(cmd))


def fetch_issue_detail(slug: str, number: int) -> dict:
    """Fetch a single issue with full body text."""
    cmd = [
        "gh", "issue", "view", str(number),
        "--repo", slug,
        "--json", ",".join(_GH_DETAIL_FIELDS),
    ]
    return json.loads(_run_gh(cmd))


# ---------------------------------------------------------------------------
# Deterministic sorting
# ---------------------------------------------------------------------------

_SORT_KEYS: dict[str, object] = {
    "newest": lambda issue: issue.updated_at,
    "oldest": lambda issue: issue.created_at,
    "most-discussed": lambda issue: issue.comments,
}

_SORT_REVERSE = {
    "newest": True,
    "oldest": False,
    "most-discussed": True,
}


def sort_issues(issues: list[IssueInfo], mode: str) -> list[IssueInfo]:
    """Sort issues by the given mode."""
    if mode not in _SORT_KEYS:
        raise ValueError(f"Unknown sort mode: {mode!r}. Choose from: {list(_SORT_KEYS)}")
    return sorted(issues, key=_SORT_KEYS[mode], reverse=_SORT_REVERSE[mode])


# ---------------------------------------------------------------------------
# AI-powered ranking
# ---------------------------------------------------------------------------

@dataclass
class RankedIssue:
    """An issue with an AI-generated rationale."""

    number: int
    title: str
    reason: str


async def smart_rank(issues: list[IssueInfo], limit: int = 5) -> list[RankedIssue]:
    """Use Claude Code SDK to rank issues by estimated impact/importance."""
    from codeband.utility_llm import one_shot_text, parse_json_array

    summaries = [
        {
            "number": issue.number,
            "title": issue.title,
            "author": issue.author,
            "labels": issue.labels,
            "comments": issue.comments,
        }
        for issue in issues
    ]

    prompt = (
        f"You are helping a developer pick the most impactful issue to work on next.\n\n"
        f"Here are the open issues:\n{json.dumps(summaries, indent=2)}\n\n"
        f"Rank the top {limit} issues by estimated impact and importance. "
        f"Consider: labels (bug > feature > enhancement), discussion activity, "
        f"and potential value to users.\n\n"
        f"Return ONLY a JSON array of objects with 'number' and 'reason' fields, "
        f"ordered from most to least impactful. No markdown, no extra text."
    )

    text = await one_shot_text(prompt)
    rankings = parse_json_array(text)
    title_map = {issue.number: issue.title for issue in issues}
    return [
        RankedIssue(
            number=r["number"],
            title=title_map.get(r["number"], ""),
            reason=r["reason"],
        )
        for r in rankings[:limit]
    ]
