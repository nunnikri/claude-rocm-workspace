#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Jira client — stub placeholder.

TODO: Implement when Jira credentials and API access are available.
Expected output: list of JiraIssue objects per user.

Possible implementation paths:
- Jira REST API v3 (cloud): https://developer.atlassian.com/cloud/jira/platform/rest/v3/
- Jira Python SDK: jira package (pip install jira)
- JIRA_BASE_URL, JIRA_TOKEN in .env

For now, all functions return empty lists so the dashboard renders
with a "Jira: N/A" placeholder block.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class JiraIssue:
    url: str
    key: str        # e.g. SWDEV-12345
    summary: str
    status: str     # e.g. "In Progress"
    priority: str   # e.g. "P1"
    assignee: str


def fetch_assigned_jira_issues(user: str, log_fn=print) -> list[JiraIssue]:
    """
    Returns Jira issues assigned to `user`.
    Currently a stub — returns empty list.
    """
    # TODO: implement Jira REST API call
    return []
