#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Jira client using the Atlassian REST API v3 (direct REST, no MCP).

Auth is Basic <email>:<api_token> against the real Jira REST API — confirmed
working directly (not routed through the AMD MCP gateway, which is only
reachable from inside a Claude Code session).

Reads credentials from scripts/dashboard/.env:
  JIRA_BASE_URL        - e.g. https://amd-hub.atlassian.net
  AMD_NTID             - AMD email address (e.g. nirmal.unnikrishnan@amd.com)
  ATLASSIAN_MCP_AUTH   - Atlassian API token from
                         id.atlassian.com/manage-profile/security/api-tokens

Credentials are never logged.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from config import cfg


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class JiraIssue:
    url: str
    key: str        # e.g. ROCM-26072
    summary: str
    status: str     # e.g. "In Progress"
    priority: str   # e.g. "P1"
    assignee: str   # display name
    labels: list[str] = field(default_factory=list)
    issue_type: str = ""
    description: str = ""    # plain text, populated by fetch_issue_description()
    updated: str = ""        # ISO timestamp, used for state/change tracking
    comments: str = ""              # formatted, populated by fetch_issue_context()
    attachments_text: str = ""      # formatted, populated by fetch_issue_context()
    triage_file: str = ""    # set by ai_review after triage
    triage_status: str = ""  # "triaged" | "failed" | "pending"


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _auth_header() -> str:
    """Build Basic Auth header value from AMD_NTID (email) + ATLASSIAN_MCP_AUTH
    (API token). Never log the return value."""
    email = cfg("AMD_NTID")
    token = cfg("ATLASSIAN_MCP_AUTH")
    if not email or not token:
        raise RuntimeError(
            "AMD_NTID and ATLASSIAN_MCP_AUTH must be set in scripts/dashboard/.env"
        )
    credentials = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {credentials}"


_DEBUG = False


def _jira_get(path: str, log_fn=print) -> dict | None:
    """GET request to Jira REST API. Returns parsed JSON or None on error."""
    base_url = cfg("JIRA_BASE_URL", "https://amd-hub.atlassian.net").rstrip("/")

    url = f"{base_url}/rest/api/3{path}"
    if _DEBUG:
        log_fn(f"  [debug] GET {url}")
        log_fn(f"  [debug] AMD_NTID={'(set)' if cfg('AMD_NTID') else '(not set)'}")
        log_fn(f"  [debug] ATLASSIAN_MCP_AUTH={'(set)' if cfg('ATLASSIAN_MCP_AUTH') else '(not set)'}")
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": _auth_header(),
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        limit = len(body) if _DEBUG else 200
        log_fn(f"  ERROR: Jira API HTTP {e.code} for {path}: {body[:limit]}")
        return None
    except urllib.error.URLError as e:
        log_fn(f"  ERROR: Jira API request failed: {e.reason}")
        return None
    except RuntimeError as e:
        log_fn(f"  ERROR: {e}")
        return None
    except Exception as e:
        log_fn(f"  ERROR: Jira unexpected error: {e}")
        return None


# ---------------------------------------------------------------------------
# Plain-text extraction from Atlassian Document Format (ADF)
# ---------------------------------------------------------------------------

def _adf_to_text(node: dict | list | str | None) -> str:
    """Extract plain text from an ADF description node (Jira Cloud v3 format)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(child) for child in node.get("content", [])]
        text = "".join(parts)
        if node.get("type") == "paragraph":
            return text + "\n"
        return text
    return ""


# ---------------------------------------------------------------------------
# Issue fetching
# ---------------------------------------------------------------------------

_ISSUE_FIELDS = "summary,status,priority,assignee,labels,issuetype,description,updated"


def _parse_issue(data: dict) -> JiraIssue:
    fields = data.get("fields", {})
    base_url = cfg("JIRA_BASE_URL", "https://amd-hub.atlassian.net").rstrip("/")
    key = data.get("key", "")
    assignee = (fields.get("assignee") or {}).get("displayName", "Unassigned")
    priority = (fields.get("priority") or {}).get("name", "")
    status = (fields.get("status") or {}).get("name", "")
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    labels = fields.get("labels") or []
    description = _adf_to_text(fields.get("description")).strip()
    updated = fields.get("updated", "")
    return JiraIssue(
        url=f"{base_url}/browse/{key}",
        key=key,
        summary=fields.get("summary", ""),
        status=status,
        priority=priority,
        assignee=assignee,
        labels=labels,
        issue_type=issue_type,
        description=description,
        updated=updated,
    )


def get_issue(key: str, log_fn=print) -> JiraIssue | None:
    """Fetch a single Jira issue by key (e.g. 'ROCM-26072')."""
    data = _jira_get(f"/issue/{key}?fields={_ISSUE_FIELDS}", log_fn=log_fn)
    return _parse_issue(data) if data else None


# ---------------------------------------------------------------------------
# Context enrichment (comments + log attachments) — for triage only, not the
# lightweight bulk list fetch used for the dashboard table.
# ---------------------------------------------------------------------------

_COMMENTS_LIMIT = 4000            # combined char budget for comment history
_ATTACHMENT_EXTENSIONS = (".log", ".txt", ".out", ".yaml", ".yml", ".json", ".cfg", ".conf")
_ATTACHMENT_MAX_BYTES = 200_000    # skip anything bigger — not a text log
_ATTACHMENT_PER_FILE_LIMIT = 3000  # char budget per attachment
_ATTACHMENT_TOTAL_LIMIT = 8000     # combined char budget across all attachments


def _fetch_comments_text(key: str, log_fn=print) -> str:
    """Fetch recent comments, newest first, formatted and length-capped."""
    data = _jira_get(f"/issue/{key}/comment?orderBy=-created&maxResults=20", log_fn=log_fn)
    if not data or not data.get("comments"):
        return ""

    parts = []
    for c in data["comments"]:
        author = (c.get("author") or {}).get("displayName", "Unknown")
        created = c.get("created", "")
        text = _adf_to_text(c.get("body")).strip()
        if text:
            parts.append(f"**{author} ({created}):**\n{text}")

    combined = "\n\n".join(parts)
    if len(combined) > _COMMENTS_LIMIT:
        combined = combined[:_COMMENTS_LIMIT] + "\n\n[... earlier comments omitted ...]"
    return combined


def _download_attachment_text(url: str, log_fn=print) -> str:
    """GET raw attachment content and decode as text. Returns '' on any failure."""
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": _auth_header(), "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log_fn(f"  WARNING: could not download attachment [{url}]: {e}")
        return ""


def _fetch_attachments_text(key: str, log_fn=print) -> str:
    """Fetch log-like attachments (by extension + size), formatted and length-capped."""
    data = _jira_get(f"/issue/{key}?fields=attachment", log_fn=log_fn)
    if not data:
        return ""
    attachments = (data.get("fields") or {}).get("attachment") or []

    parts = []
    total_len = 0
    for att in attachments:
        filename = att.get("filename", "")
        if not filename.lower().endswith(_ATTACHMENT_EXTENSIONS):
            continue
        if att.get("size", 0) > _ATTACHMENT_MAX_BYTES:
            continue
        content_url = att.get("content")
        if not content_url:
            continue
        text = _download_attachment_text(content_url, log_fn=log_fn)
        if not text:
            continue
        if len(text) > _ATTACHMENT_PER_FILE_LIMIT:
            text = text[:_ATTACHMENT_PER_FILE_LIMIT] + f"\n[... truncated at {_ATTACHMENT_PER_FILE_LIMIT} chars ...]"
        if total_len + len(text) > _ATTACHMENT_TOTAL_LIMIT:
            parts.append(f"**{filename}:** [... omitted, attachment budget reached ...]")
            continue
        parts.append(f"**{filename}:**\n```\n{text}\n```")
        total_len += len(text)

    return "\n\n".join(parts)


def fetch_issue_context(issue: JiraIssue, log_fn=print) -> None:
    """
    Populate issue.comments and issue.attachments_text in-place. Best-effort:
    any failure leaves the corresponding field empty/partial rather than
    raising, since a broken comment/attachment fetch shouldn't abort triage.
    """
    issue.comments = _fetch_comments_text(issue.key, log_fn=log_fn)
    issue.attachments_text = _fetch_attachments_text(issue.key, log_fn=log_fn)


_SEARCH_PAGE_SIZE = 100   # per-page size; pagination below fetches ALL matches


def fetch_assigned_jira_issues(email: str, log_fn=print) -> list[JiraIssue]:
    """
    Fetch ALL open Jira issues assigned to `email` (AMD email address), across
    all projects visible to the authenticated account, excluding Done-category
    issues. Paginates via nextPageToken until every match is retrieved — no
    client-imposed cap on total result count.

    Uses /rest/api/3/search/jql — the old /search endpoint was removed
    (see https://developer.atlassian.com/changelog/#CHANGE-2046).
    """
    jql = f'assignee = "{email}" AND statusCategory != Done ORDER BY updated DESC'
    jql_encoded = urllib.parse.quote(jql)

    issues: list[JiraIssue] = []
    next_page_token: str | None = None

    while True:
        path = (
            f"/search/jql?jql={jql_encoded}&maxResults={_SEARCH_PAGE_SIZE}"
            f"&fields={_ISSUE_FIELDS}"
        )
        if next_page_token:
            path += f"&nextPageToken={urllib.parse.quote(next_page_token)}"

        data = _jira_get(path, log_fn=log_fn)
        if not data or "issues" not in data:
            break

        issues.extend(_parse_issue(issue) for issue in data["issues"])

        next_page_token = data.get("nextPageToken")
        if not next_page_token or data.get("isLast", True):
            break

    return issues


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import re
    import sys
    _DEBUG = "--debug" in sys.argv
    if _DEBUG:
        import jira_client as _self
        _self._DEBUG = True
        sys.argv.remove("--debug")
    arg = sys.argv[1] if len(sys.argv) > 1 else "ROCM-26072"
    # Accept either a full URL or a bare key
    m = re.search(r"([A-Z][A-Z0-9_]+-\d+)", arg)
    key = m.group(1) if m else arg
    print(f"Fetching {key}...")
    issue = get_issue(key)
    if issue:
        print(f"  Key:      {issue.key}")
        print(f"  Summary:  {issue.summary}")
        print(f"  Status:   {issue.status}")
        print(f"  Priority: {issue.priority}")
        print(f"  Assignee: {issue.assignee}")
        print(f"  Type:     {issue.issue_type}")
        print(f"  URL:      {issue.url}")
        print(f"  Description: {issue.description[:200]}")
    else:
        print("  Failed to fetch issue")
        sys.exit(1)
