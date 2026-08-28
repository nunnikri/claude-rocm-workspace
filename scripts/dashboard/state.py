#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
State tracking: remembers what has been reviewed/triaged so unchanged
items are not re-processed on every poll run.

State is keyed by item URL. Each entry stores the last seen updated_at
timestamp and the output file path. If updated_at changes, the item is
re-processed. If it hasn't changed, the previous review/triage is reused.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import STATE_FILE
from github_client import Issue, PR
from jira_client import JiraIssue


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"prs": {}, "issues": {}, "last_run": None}


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# PR state helpers
# ---------------------------------------------------------------------------

def pr_needs_review(pr: PR, state: dict) -> bool:
    """True if the PR is new or has been updated since last review."""
    entry = state["prs"].get(pr.url, {})
    return entry.get("updated_at") != pr.updated_at


def mark_pr_reviewed(pr: PR, state: dict) -> None:
    state["prs"][pr.url] = {
        "url": pr.url,
        "repo": pr.repo,
        "number": pr.number,
        "title": pr.title,
        "author": pr.author,
        "updated_at": pr.updated_at,
        "review_file": pr.review_file,
        "review_status": pr.review_status,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }


def restore_pr_review(pr: PR, state: dict) -> None:
    """Populate pr.review_file and pr.review_status from previous state."""
    entry = state["prs"].get(pr.url, {})
    pr.review_file = entry.get("review_file", "")
    pr.review_status = entry.get("review_status", "pending")


# ---------------------------------------------------------------------------
# Issue state helpers
# ---------------------------------------------------------------------------

def issue_needs_triage(issue: Issue, state: dict) -> bool:
    """True if the issue is new or has been updated since last triage."""
    entry = state["issues"].get(issue.url, {})
    return entry.get("updated_at") != issue.updated_at


def mark_issue_triaged(issue: Issue, state: dict) -> None:
    state["issues"][issue.url] = {
        "url": issue.url,
        "repo": issue.repo,
        "number": issue.number,
        "title": issue.title,
        "assignee": issue.assignee,
        "updated_at": issue.updated_at,
        "triage_file": issue.triage_file,
        "triage_status": issue.triage_status,
        "triaged_at": datetime.now(timezone.utc).isoformat(),
    }


def restore_issue_triage(issue: Issue, state: dict) -> None:
    """Populate issue.triage_file and issue.triage_status from previous state."""
    entry = state["issues"].get(issue.url, {})
    issue.triage_file = entry.get("triage_file", "")
    issue.triage_status = entry.get("triage_status", "pending")


# ---------------------------------------------------------------------------
# Jira issue state helpers
# ---------------------------------------------------------------------------

def jira_issue_needs_triage(issue: JiraIssue, state: dict) -> bool:
    """True if the Jira issue is new or has been updated since last triage."""
    entry = state.setdefault("jira_issues", {}).get(issue.url, {})
    return entry.get("updated") != issue.updated


def mark_jira_issue_triaged(issue: JiraIssue, state: dict) -> None:
    state.setdefault("jira_issues", {})[issue.url] = {
        "url": issue.url,
        "key": issue.key,
        "summary": issue.summary,
        "status": issue.status,
        "priority": issue.priority,
        "updated": issue.updated,
        "triage_file": issue.triage_file,
        "triage_status": issue.triage_status,
        "triaged_at": datetime.now(timezone.utc).isoformat(),
    }


def restore_jira_issue_triage(issue: JiraIssue, state: dict) -> None:
    """Populate issue.triage_file and issue.triage_status from previous state."""
    entry = state.setdefault("jira_issues", {}).get(issue.url, {})
    issue.triage_file = entry.get("triage_file", "")
    issue.triage_status = entry.get("triage_status", "pending")
