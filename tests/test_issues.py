"""Tests for cb issues — GitHub issue discovery and prioritization."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Issue parsing
# ---------------------------------------------------------------------------

SAMPLE_ISSUES = [
    {
        "number": 10,
        "title": "Login page crashes on mobile",
        "author": {"login": "alice"},
        "labels": [{"name": "bug"}, {"name": "critical"}],
        "createdAt": "2026-03-20T10:00:00Z",
        "updatedAt": "2026-03-28T14:00:00Z",
        "comments": [{"author": {"login": "bob"}}, {"author": {"login": "carol"}}],
        "body": "Steps to reproduce:\n1. Open login page on iOS Safari\n2. Tap password field\n3. App crashes",
    },
    {
        "number": 25,
        "title": "Add dark mode support",
        "author": {"login": "bob"},
        "labels": [{"name": "enhancement"}],
        "createdAt": "2026-03-29T08:00:00Z",
        "updatedAt": "2026-03-29T09:00:00Z",
        "comments": [],
        "body": "Would be nice to have a dark mode toggle in settings.",
    },
    {
        "number": 5,
        "title": "Slow query on user list endpoint",
        "author": {"login": "carol"},
        "labels": [{"name": "performance"}],
        "createdAt": "2026-03-01T12:00:00Z",
        "updatedAt": "2026-03-25T16:00:00Z",
        "comments": [
            {"author": {"login": "alice"}},
            {"author": {"login": "bob"}},
            {"author": {"login": "dave"}},
            {"author": {"login": "eve"}},
        ],
        "body": "The /api/users endpoint takes 3+ seconds when there are >1000 users.",
    },
]


class TestIssueInfo:
    """Test IssueInfo parsing from gh output."""

    def test_from_gh(self):
        from codeband.github.issues import IssueInfo

        issue = IssueInfo.from_gh(SAMPLE_ISSUES[0])
        assert issue.number == 10
        assert issue.title == "Login page crashes on mobile"
        assert issue.author == "alice"
        assert issue.labels == ["bug", "critical"]
        assert issue.comments == 2
        assert "Steps to reproduce" in issue.body

    def test_summary_line(self):
        from codeband.github.issues import IssueInfo

        issue = IssueInfo.from_gh(SAMPLE_ISSUES[0])
        line = issue.summary_line()
        assert "#10" in line
        assert "Login page" in line
        assert "alice" in line
        assert "bug" in line


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class TestFetchIssues:
    """Test issue fetching from gh CLI output."""

    def test_fetch_returns_raw_dicts(self):
        from codeband.github.issues import fetch_open_issues

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps(SAMPLE_ISSUES)
            mock_run.return_value.returncode = 0
            result = fetch_open_issues("acme/widgets", limit=10)

        assert len(result) == 3
        assert result[0]["number"] == 10

    def test_fetch_with_label_filter(self):
        from codeband.github.issues import fetch_open_issues

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps([SAMPLE_ISSUES[0]])
            mock_run.return_value.returncode = 0
            fetch_open_issues("acme/widgets", label="bug")

        cmd = mock_run.call_args[0][0]
        assert "--label" in cmd
        assert "bug" in cmd

    def test_fetch_gh_not_installed(self):
        from codeband.github.issues import fetch_open_issues

        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="gh"):
                fetch_open_issues("acme/widgets")

    def test_fetch_detail(self):
        from codeband.github.issues import fetch_issue_detail

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = json.dumps(SAMPLE_ISSUES[0])
            mock_run.return_value.returncode = 0
            result = fetch_issue_detail("acme/widgets", 10)

        assert result["number"] == 10
        cmd = mock_run.call_args[0][0]
        assert "view" in cmd
        assert "10" in cmd


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------

class TestSortIssues:
    """Test deterministic sort modes."""

    def _issues(self):
        from codeband.github.issues import IssueInfo

        return [IssueInfo.from_gh(i) for i in SAMPLE_ISSUES]

    def test_newest_first(self):
        from codeband.github.issues import sort_issues

        result = sort_issues(self._issues(), "newest")
        assert result[0].number == 25  # most recently updated

    def test_oldest_first(self):
        from codeband.github.issues import sort_issues

        result = sort_issues(self._issues(), "oldest")
        assert result[0].number == 5  # oldest created_at

    def test_most_discussed(self):
        from codeband.github.issues import sort_issues

        result = sort_issues(self._issues(), "most-discussed")
        assert result[0].number == 5  # 4 comments

    def test_unknown_mode_raises(self):
        from codeband.github.issues import sort_issues

        with pytest.raises(ValueError, match="Unknown sort mode"):
            sort_issues(self._issues(), "invalid")


# ---------------------------------------------------------------------------
# AI ranking
# ---------------------------------------------------------------------------

class TestSmartRank:
    """Test AI-powered issue ranking."""

    @pytest.mark.asyncio
    async def test_returns_ranked_issues(self):
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        from codeband.github.issues import IssueInfo, smart_rank

        issues = [IssueInfo.from_gh(i) for i in SAMPLE_ISSUES]

        ranked_payload = json.dumps([
            {"number": 10, "reason": "Critical bug affecting mobile users"},
            {"number": 5, "reason": "Performance issue impacting scalability"},
        ])

        async def fake_query(*, prompt, options):
            yield AssistantMessage(
                content=[TextBlock(text=ranked_payload)],
                model="claude-sonnet-4-6",
            )

        with patch("claude_agent_sdk.query", side_effect=fake_query):
            ranked = await smart_rank(issues, limit=2)

        assert len(ranked) == 2
        assert ranked[0].number == 10
        assert "Critical" in ranked[0].reason
        assert ranked[1].number == 5
