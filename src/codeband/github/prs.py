"""Fetch, sort, and rank open GitHub PRs for task selection."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Repo URL → owner/repo slug
# ---------------------------------------------------------------------------

_HTTPS_RE = re.compile(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")
_SSH_RE = re.compile(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$")


def repo_slug(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL (HTTPS or SSH)."""
    for pattern in (_HTTPS_RE, _SSH_RE):
        m = pattern.match(url)
        if m:
            return m.group(1)
    raise ValueError(f"Cannot extract GitHub owner/repo from URL: {url}")


def pr_url(slug: str, number: int) -> str:
    """Build a GitHub PR URL."""
    return f"https://github.com/{slug}/pull/{number}"




# ---------------------------------------------------------------------------
# PR dataclass
# ---------------------------------------------------------------------------

@dataclass
class PRInfo:
    """Parsed pull-request metadata."""

    number: int
    title: str
    author: str
    labels: list[str]
    created_at: str
    updated_at: str
    additions: int
    deletions: int
    changed_files: int
    comments: int
    review_comments: int

    @classmethod
    def from_gh(cls, data: dict) -> PRInfo:
        return cls(
            number=data["number"],
            title=data["title"],
            author=data["author"]["login"],
            labels=[label["name"] for label in data.get("labels", [])],
            created_at=data["createdAt"],
            updated_at=data["updatedAt"],
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changed_files=data.get("changedFiles", 0),
            comments=data.get("comments", {}).get("totalCount", 0),
            review_comments=len(data.get("reviews", [])),
        )

    @property
    def total_lines(self) -> int:
        return self.additions + self.deletions

    @property
    def total_comments(self) -> int:
        return self.comments + self.review_comments

    def summary_line(self, slug: str | None = None) -> str:
        labels = f" [{', '.join(self.labels)}]" if self.labels else ""
        link = f"  {pr_url(slug, self.number)}" if slug else ""
        return (
            f"#{self.number:<6} {self.title[:50]:<50}  "
            f"by {self.author:<12} "
            f"+{self.additions}/-{self.deletions} ({self.changed_files} files)  "
            f"{self.total_comments} comments{labels}{link}"
        )


# ---------------------------------------------------------------------------
# Fetch from gh CLI
# ---------------------------------------------------------------------------

_GH_FIELDS = [
    "number", "title", "author", "labels", "createdAt", "updatedAt",
    "additions", "deletions", "changedFiles", "comments", "reviews",
]


def fetch_open_prs(slug: str, limit: int = 30) -> list[dict]:
    """Fetch open PRs via the gh CLI. Returns raw JSON dicts."""
    cmd = [
        "gh", "pr", "list",
        "--repo", slug,
        "--state", "open",
        "--limit", str(limit),
        "--json", ",".join(_GH_FIELDS),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError(
            "The 'gh' CLI is not installed. "
            "Install it from https://cli.github.com and run 'gh auth login'."
        )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Deterministic sorting
# ---------------------------------------------------------------------------

_SORT_KEYS = {
    "newest": lambda pr: pr.updated_at,
    "oldest": lambda pr: pr.created_at,
    "smallest": lambda pr: pr.total_lines,
    "largest": lambda pr: pr.total_lines,
    "most-discussed": lambda pr: pr.total_comments,
}

_SORT_REVERSE = {
    "newest": True,
    "oldest": False,
    "smallest": False,
    "largest": True,
    "most-discussed": True,
}


def sort_prs(prs: list[PRInfo], mode: str) -> list[PRInfo]:
    """Sort PRs by the given mode."""
    if mode not in _SORT_KEYS:
        raise ValueError(f"Unknown sort mode: {mode!r}. Choose from: {list(_SORT_KEYS)}")
    return sorted(prs, key=_SORT_KEYS[mode], reverse=_SORT_REVERSE[mode])


# ---------------------------------------------------------------------------
# AI-powered ranking
# ---------------------------------------------------------------------------

@dataclass
class RankedPR:
    """A PR with an AI-generated rationale."""

    number: int
    title: str
    reason: str


async def smart_rank(prs: list[PRInfo], limit: int = 5) -> list[RankedPR]:
    """Use Claude Code SDK to rank PRs by estimated impact/importance."""
    from codeband.utility_llm import one_shot_text, parse_json_array

    summaries = []
    for pr in prs:
        summaries.append({
            "number": pr.number,
            "title": pr.title,
            "author": pr.author,
            "labels": pr.labels,
            "lines_changed": pr.total_lines,
            "files_changed": pr.changed_files,
            "comments": pr.total_comments,
        })

    prompt = (
        f"You are helping a developer pick the most impactful PR to work on next.\n\n"
        f"Here are the open pull requests:\n{json.dumps(summaries, indent=2)}\n\n"
        f"Rank the top {limit} PRs by estimated impact and importance. "
        f"Consider: scope of changes, labels, discussion activity, and potential value.\n\n"
        f"Return ONLY a JSON array of objects with 'number' and 'reason' fields, "
        f"ordered from most to least impactful. No markdown, no extra text."
    )

    text = await one_shot_text(prompt)
    rankings = parse_json_array(text)

    title_map = {pr.number: pr.title for pr in prs}
    return [
        RankedPR(
            number=r["number"],
            title=title_map.get(r["number"], ""),
            reason=r["reason"],
        )
        for r in rankings[:limit]
    ]
