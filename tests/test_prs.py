"""Tests for codeband prs — PR discovery and prioritization."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers: repo URL → owner/repo slug
# ---------------------------------------------------------------------------

class TestRepoSlug:
    """Extract GitHub owner/repo from various URL formats."""

    def test_https_url(self):
        from codeband.github.prs import repo_slug

        assert repo_slug("https://github.com/acme/widgets.git") == "acme/widgets"

    def test_https_url_no_dot_git(self):
        from codeband.github.prs import repo_slug

        assert repo_slug("https://github.com/acme/widgets") == "acme/widgets"

    def test_ssh_url(self):
        from codeband.github.prs import repo_slug

        assert repo_slug("git@github.com:acme/widgets.git") == "acme/widgets"

    def test_non_github_raises(self):
        from codeband.github.prs import repo_slug

        with pytest.raises(ValueError, match="GitHub"):
            repo_slug("https://gitlab.com/acme/widgets.git")


# ---------------------------------------------------------------------------
# PR fetching via gh CLI
# ---------------------------------------------------------------------------

SAMPLE_PRS = [
    {
        "number": 42,
        "title": "Add caching layer",
        "author": {"login": "alice"},
        "labels": [{"name": "enhancement"}],
        "createdAt": "2026-03-20T10:00:00Z",
        "updatedAt": "2026-03-28T14:00:00Z",
        "additions": 120,
        "deletions": 30,
        "changedFiles": 5,
        "comments": {"totalCount": 8},
        "reviews": [{"id": 1}, {"id": 2}, {"id": 3}],
    },
    {
        "number": 55,
        "title": "Fix typo in README",
        "author": {"login": "bob"},
        "labels": [],
        "createdAt": "2026-03-29T08:00:00Z",
        "updatedAt": "2026-03-29T09:00:00Z",
        "additions": 1,
        "deletions": 1,
        "changedFiles": 1,
        "comments": {"totalCount": 0},
        "reviews": [],
    },
    {
        "number": 30,
        "title": "Major API overhaul",
        "author": {"login": "carol"},
        "labels": [{"name": "breaking-change"}],
        "createdAt": "2026-03-01T12:00:00Z",
        "updatedAt": "2026-03-25T16:00:00Z",
        "additions": 800,
        "deletions": 400,
        "changedFiles": 25,
        "comments": {"totalCount": 15},
        "reviews": [{"id": i} for i in range(12)],
    },
]


class TestFetchPRs:
    """Test PR fetching from gh CLI output."""

    def test_fetch_returns_parsed_prs(self):
        from codeband.github.prs import fetch_open_prs

        gh_output = json.dumps(SAMPLE_PRS)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = gh_output
            mock_run.return_value.returncode = 0
            prs = fetch_open_prs("acme/widgets", limit=10)

        assert len(prs) == 3
        assert prs[0]["number"] == 42

    def test_fetch_gh_not_installed(self):
        from codeband.github.prs import fetch_open_prs

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="gh"):
                fetch_open_prs("acme/widgets")


# ---------------------------------------------------------------------------
# Sorting / prioritization
# ---------------------------------------------------------------------------

class TestSortPRs:
    """Test deterministic sort modes."""

    def _prs(self):
        from codeband.github.prs import PRInfo

        return [PRInfo.from_gh(p) for p in SAMPLE_PRS]

    def test_sort_newest(self):
        from codeband.github.prs import sort_prs

        result = sort_prs(self._prs(), "newest")
        assert result[0].number == 55  # most recently updated

    def test_sort_oldest(self):
        from codeband.github.prs import sort_prs

        result = sort_prs(self._prs(), "oldest")
        assert result[0].number == 30  # earliest created

    def test_sort_smallest(self):
        from codeband.github.prs import sort_prs

        result = sort_prs(self._prs(), "smallest")
        assert result[0].number == 55  # 1+1 = 2 changed lines

    def test_sort_largest(self):
        from codeband.github.prs import sort_prs

        result = sort_prs(self._prs(), "largest")
        assert result[0].number == 30  # 800+400 = 1200 changed lines

    def test_sort_most_discussed(self):
        from codeband.github.prs import sort_prs

        result = sort_prs(self._prs(), "most-discussed")
        assert result[0].number == 30  # 15+12 = 27 total comments

    def test_invalid_sort_raises(self):
        from codeband.github.prs import sort_prs

        with pytest.raises(ValueError, match="Unknown sort"):
            sort_prs(self._prs(), "by-vibes")


# ---------------------------------------------------------------------------
# Smart ranking (AI-powered)
# ---------------------------------------------------------------------------

class TestSmartRank:
    """Test AI-powered PR ranking."""

    @pytest.mark.asyncio
    async def test_smart_rank_returns_ordered_prs_with_rationale(self):
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        from codeband.github.prs import PRInfo, smart_rank

        prs = [PRInfo.from_gh(p) for p in SAMPLE_PRS]

        ranked_payload = json.dumps([
            {"number": 30, "reason": "Major API change with broad impact"},
            {"number": 42, "reason": "Caching improves performance"},
            {"number": 55, "reason": "Low-impact typo fix"},
        ])

        async def fake_query(*, prompt, options):
            yield AssistantMessage(
                content=[TextBlock(text=ranked_payload)],
                model="claude-sonnet-4-6",
            )

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            ranked = await smart_rank(prs, limit=3)

        assert len(ranked) == 3
        assert ranked[0].number == 30
        assert ranked[0].reason == "Major API change with broad impact"
        assert ranked[2].number == 55
