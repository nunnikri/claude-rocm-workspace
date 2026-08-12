#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
GitHub API client using the gh CLI.

Lessons from auto_review/poll_and_review.py:
- Use URL query params (?key=val), NOT -f flags — those trigger POST → HTTP 422
- Always specify encoding="utf-8" — Windows defaults to cp1252
- Return shutil.which() full path, not just the name — subprocess needs full path
- Inherit parent env — custom env dicts strip AMD cert vars → TLS failures
- Write large outputs to temp files — capture_output=True deadlocks on big diffs
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# gh executable resolution
# ---------------------------------------------------------------------------

def _gh_exe() -> str:
    found = shutil.which("gh")
    if found:
        return found
    candidates = [
        r"C:\Program Files\GitHub CLI\gh.exe",
        "/usr/bin/gh",
        "/usr/local/bin/gh",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return "gh"


# ---------------------------------------------------------------------------
# Raw API call
# ---------------------------------------------------------------------------

def _gh_api(endpoint: str, log_fn=print) -> Any:
    """
    GET request to GitHub API via gh CLI.
    endpoint: e.g. 'repos/ROCm/TheRock/pulls?state=open&per_page=100'
    Returns parsed JSON or None on error.
    """
    cmd = [_gh_exe(), "api", endpoint]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        stdout = result.stdout or ""
        return json.loads(stdout) if stdout.strip() else None
    except subprocess.CalledProcessError as e:
        log_fn(f"  gh api error [{endpoint}]: {(e.stderr or '').strip()[:200]}")
        return None
    except json.JSONDecodeError:
        return None
    except Exception as e:
        log_fn(f"  gh unexpected error [{endpoint}]: {e}")
        return None


def _gh_pr_diff(repo: str, pr_number: int, log_fn=print) -> str:
    """
    Fetch unified diff for a PR via gh pr diff.
    Writes to a temp file to avoid pipe buffer deadlock on large diffs.
    """
    gh = _gh_exe()
    diff_text = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".diff", encoding="utf-8",
            errors="replace", delete=False
        ) as tf:
            tmp = Path(tf.name)

        with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run(
                [gh, "pr", "diff", str(pr_number), "--repo", repo],
                stdout=fh,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
        if r.returncode == 0:
            diff_text = tmp.read_text(encoding="utf-8", errors="replace")
        tmp.unlink(missing_ok=True)
    except Exception as e:
        log_fn(f"  gh pr diff error [{repo}#{pr_number}]: {e}")
    return diff_text


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PR:
    url: str
    repo: str
    number: int
    title: str
    author: str
    head_ref: str
    base_ref: str
    draft: bool
    updated_at: str
    review_requested_from: list[str] = field(default_factory=list)
    diff: str = ""           # populated by fetch_pr_diff()
    review_file: str = ""    # set by poll after AI review
    review_status: str = ""  # "reviewed" | "failed" | "pending"


@dataclass
class Issue:
    url: str
    repo: str
    number: int
    title: str
    assignee: str
    labels: list[str]
    updated_at: str
    body: str = ""
    triage_file: str = ""    # set by poll after AI triage
    triage_status: str = ""  # "triaged" | "failed" | "pending"


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

def _search_prs(query: str, log_fn=print) -> list[dict]:
    """
    Search PRs using GitHub Search API, handling pagination.
    query: GitHub search query string (without 'is:pr')
    Returns raw item dicts from the search response.
    """
    items: list[dict] = []
    page = 1
    while True:
        data = _gh_api(
            f"search/issues?q=is:pr+is:open+{query}&per_page=100&page={page}",
            log_fn=log_fn,
        )
        if not isinstance(data, dict):
            break
        batch = data.get("items", [])
        items.extend(batch)
        # Stop if we've received all results
        if len(items) >= data.get("total_count", 0) or len(batch) < 100:
            break
        page += 1
    return items


def _pr_from_search_item(item: dict, repo: str) -> PR:
    """Build a PR dataclass from a GitHub Search API result item.
    head_ref/base_ref are not in search results — fetched later if needed for AI review.
    """
    return PR(
        url=item["html_url"],
        repo=repo,
        number=item["number"],
        title=item["title"],
        author=item.get("user", {}).get("login", ""),
        head_ref="",   # populated by fetch_pr_details() when needed
        base_ref="",
        draft=item.get("draft", False),
        updated_at=item.get("updated_at", ""),
    )


def fetch_pr_details(pr: PR, log_fn=print) -> None:
    """Populate head_ref and base_ref in-place (needed for AI review prompt)."""
    if pr.head_ref:
        return
    data = _gh_api(f"repos/{pr.repo}/pulls/{pr.number}", log_fn=log_fn)
    if isinstance(data, dict):
        pr.head_ref = data.get("head", {}).get("ref", "")
        pr.base_ref = data.get("base", {}).get("ref", "main")
        pr.draft = data.get("draft", False)


def fetch_prs_created_by(user: str, repos: list[str], log_fn=print) -> list[PR]:
    """
    All open, non-draft PRs authored by `user` across `repos`.
    Uses Search API so results are not capped at 100 total open PRs per repo.
    """
    results: list[PR] = []
    for repo in repos:
        repo_q = repo.replace("/", "%2F")
        items = _search_prs(f"author:{user}+repo:{repo_q}", log_fn=log_fn)
        for item in items:
            if item.get("draft", False):
                continue
            results.append(_pr_from_search_item(item, repo))
    return results


def fetch_review_requests(user: str, repos: list[str], log_fn=print) -> list[PR]:
    """
    All open PRs where `user` is a requested reviewer.
    Uses Search API so results are not capped at 100 total open PRs per repo.
    """
    results: list[PR] = []
    for repo in repos:
        repo_q = repo.replace("/", "%2F")
        items = _search_prs(f"review-requested:{user}+repo:{repo_q}", log_fn=log_fn)
        for item in items:
            results.append(_pr_from_search_item(item, repo))
    return results


def fetch_assigned_issues(user: str, repos: list[str], log_fn=print) -> list[Issue]:
    """
    All open GitHub issues assigned to `user` across `repos`.
    Filters out PRs (issues endpoint returns both).
    """
    results: list[Issue] = []
    for repo in repos:
        data = _gh_api(
            f"repos/{repo}/issues?assignee={user}&state=open&per_page=100",
            log_fn=log_fn,
        )
        if not isinstance(data, list):
            continue
        for issue in data:
            if "pull_request" in issue:
                continue
            results.append(Issue(
                url=issue["html_url"],
                repo=repo,
                number=issue["number"],
                title=issue["title"],
                assignee=user,
                labels=[lb["name"] for lb in issue.get("labels", [])],
                updated_at=issue.get("updated_at", ""),
                body=issue.get("body", "") or "",
            ))
    return results


def fetch_pr_diff(pr: PR, log_fn=print) -> None:
    """Populate pr.diff in-place. Truncates at 40K chars."""
    diff = _gh_pr_diff(pr.repo, pr.number, log_fn=log_fn)
    MAX = 40_000
    if len(diff) > MAX:
        diff = diff[:MAX] + f"\n\n[... truncated at {MAX} chars ...]"
    pr.diff = diff


def fetch_issue_body(issue: Issue, log_fn=print) -> None:
    """Populate issue.body in-place if not already set."""
    if issue.body:
        return
    data = _gh_api(f"repos/{issue.repo}/issues/{issue.number}", log_fn=log_fn)
    if isinstance(data, dict):
        issue.body = data.get("body", "") or ""
