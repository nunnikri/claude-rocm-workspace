#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
AI review and triage via Anthropic API (direct urllib — no SDK, no CLI).

Lessons from auto_review/poll_and_review.py:
- Call API directly with urllib.request — avoids claude CLI stdin/arg issues
- AMD proxy requires ANTHROPIC_BASE_URL + ANTHROPIC_CUSTOM_HEADERS + API key
- Never log api_key, base_url, or header values
- Two model tiers: model_review (full PR review) and model_triage (issue summary)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import (
    OUTPUT_HTML,
    REVIEWS_DIR,
    TASKS_DIR,
    anthropic_config,
    parse_custom_headers,
)
from github_client import Issue, PR
from jira_client import JiraIssue


def _rel(path: Path) -> str:
    """Return path relative to dashboard.html location, with forward slashes."""
    return os.path.relpath(path, OUTPUT_HTML.parent).replace("\\", "/")


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------

_GATEWAY_TIMEOUT_RETRIES = 2
_GATEWAY_TIMEOUT_BACKOFF_SECONDS = 10
_RETRYABLE_HTTP_CODES = {502, 503, 504}


def _call_api(prompt: str, model: str, log_fn=print) -> str | None:
    """
    POST to Anthropic Messages API using streaming (text/event-stream).

    Streaming keeps the TCP connection alive by sending tokens as they are
    generated, which prevents AMD proxy from closing idle connections at ~60s.
    That doesn't cover a *gateway* timeout waiting for the first token on a
    large prompt (502/503/504 before any streaming starts) — retried here
    instead of dropping that triage/review outright.
    Credentials sourced from config — never logged here.
    """
    acfg = anthropic_config()
    api_key = acfg["api_key"]
    if not api_key:
        log_fn("  ERROR: ANTHROPIC_API_KEY not set — add to scripts/dashboard/.env")
        return None

    base_url = acfg["base_url"]
    extra_headers = parse_custom_headers(acfg["custom_headers_raw"])

    payload = json.dumps({
        "model": model,
        "max_tokens": 3000,
        "stream": True,   # streaming: tokens arrive incrementally, proxy stays alive
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **extra_headers,
        },
        method="POST",
    )

    for attempt in range(_GATEWAY_TIMEOUT_RETRIES + 1):
        try:
            chunks: list[str] = []
            with urllib.request.urlopen(req, timeout=300) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            chunks.append(delta.get("text", ""))
            return "".join(chunks) or None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in _RETRYABLE_HTTP_CODES and attempt < _GATEWAY_TIMEOUT_RETRIES:
                log_fn(f"  API HTTP {e.code} (gateway timeout), retrying in {_GATEWAY_TIMEOUT_BACKOFF_SECONDS}s...")
                time.sleep(_GATEWAY_TIMEOUT_BACKOFF_SECONDS)
                continue
            log_fn(f"  ERROR: API HTTP {e.code}: {body[:300]}")
            return None
        except urllib.error.URLError as e:
            log_fn(f"  ERROR: API request failed: {e.reason}")
            return None
        except Exception as e:
            log_fn(f"  ERROR: {e}")
            return None
    return None


# ---------------------------------------------------------------------------
# PR review
# ---------------------------------------------------------------------------

_GUIDELINES_LIMIT = 2000   # keep prompt under AMD proxy timeout budget


def _pr_review_prompt(pr: PR, guidelines: str) -> str:
    return (
        f"You are performing a code review for a ROCm build infrastructure PR.\n\n"
        f"## Review guidelines (excerpt)\n\n{guidelines[:_GUIDELINES_LIMIT]}\n\n"
        f"---\n\n"
        f"## PR details\n\n"
        f"URL: {pr.url}\n"
        f"Repository: {pr.repo}\n"
        f"PR #{pr.number}: {pr.title}\n"
        f"Author: {pr.author}\n"
        f"Branch: {pr.head_ref} → {pr.base_ref}\n\n"
        f"## Unified diff\n\n```diff\n{pr.diff or '(diff not available)'}\n```\n\n"
        f"---\n\n"
        f"Write a thorough code review in markdown covering correctness, style, tests, "
        f"and architecture. Use severity markers: ❌ BLOCKING, ⚠️ IMPORTANT, "
        f"💡 SUGGESTION, 📋 FUTURE WORK.\n\n"
        f"Sections: Problem, Summary of changes, Overall Assessment (APPROVED / "
        f"CHANGES REQUESTED / REJECTED), Detailed Review, Recommendations, Conclusion."
    )


def review_pr(pr: PR, log_fn=print) -> bool:
    """
    Generate AI review for a PR and write to reviews/<date>/pr_<repo>_<number>.md.
    Sets pr.review_file and pr.review_status in-place.
    """
    from datetime import date

    acfg = anthropic_config()
    model = acfg["model_review"]

    guidelines_path = Path(__file__).parent.parent.parent / "reviews" / "REVIEW_GUIDELINES.md"
    guidelines = guidelines_path.read_text(encoding="utf-8") if guidelines_path.exists() else ""

    prompt = _pr_review_prompt(pr, guidelines)
    log_fn(f"  Reviewing {pr.repo}#{pr.number} ({len(prompt)} chars, model: {model})")

    text = _call_api(prompt, model, log_fn=log_fn)
    if text is None:
        pr.review_status = "failed"
        return False

    review_dir = REVIEWS_DIR / date.today().isoformat()
    review_dir.mkdir(parents=True, exist_ok=True)
    repo_short = pr.repo.split("/")[-1]
    review_file = review_dir / f"pr_{repo_short}_{pr.number}.md"
    review_file.write_text(text, encoding="utf-8")

    pr.review_file = _rel(review_file)
    pr.review_status = "reviewed"
    log_fn(f"  Written: {review_file}")
    return True


# ---------------------------------------------------------------------------
# Issue triage
# ---------------------------------------------------------------------------

def _issue_triage_prompt(issue: Issue) -> str:
    labels = ", ".join(issue.labels) or "none"
    return (
        f"Triage this GitHub issue for a ROCm build infrastructure repository.\n\n"
        f"Repository: {issue.repo}\n"
        f"Issue #{issue.number}: {issue.title}\n"
        f"Labels: {labels}\n"
        f"URL: {issue.url}\n\n"
        f"## Issue description\n\n{issue.body or '(no description)'}\n\n"
        f"---\n\n"
        f"Write a concise triage summary in markdown:\n"
        f"1. **Problem** (2-3 sentences)\n"
        f"2. **Root cause hypothesis**\n"
        f"3. **Affected components**\n"
        f"4. **Priority** (P0 critical / P1 high / P2 medium / P3 low) with justification\n"
        f"5. **Suggested next steps** (bullet list)\n"
    )


def triage_issue(issue: Issue, log_fn=print) -> bool:
    """
    Generate AI triage summary for an issue and write to tasks/active/<repo>-<number>.md.
    Sets issue.triage_file and issue.triage_status in-place.
    """
    acfg = anthropic_config()
    model = acfg["model_triage"]

    prompt = _issue_triage_prompt(issue)
    log_fn(f"  Triaging {issue.repo}#{issue.number} (model: {model})")

    text = _call_api(prompt, model, log_fn=log_fn)
    if text is None:
        issue.triage_status = "failed"
        return False

    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    repo_short = issue.repo.split("/")[-1]
    triage_file = TASKS_DIR / f"{repo_short}-{issue.number}.md"
    triage_file.write_text(text, encoding="utf-8")

    issue.triage_file = _rel(triage_file)
    issue.triage_status = "triaged"
    log_fn(f"  Written: {triage_file}")
    return True


# ---------------------------------------------------------------------------
# Jira issue triage
# ---------------------------------------------------------------------------

def _jira_triage_prompt(issue: JiraIssue) -> str:
    labels = ", ".join(issue.labels) or "none"
    return (
        f"Analyze this Jira ticket for a ROCm build infrastructure project. This is a\n"
        f"read-only analysis — you are not updating the ticket, just producing a written\n"
        f"summary a human can act on.\n\n"
        f"Key: {issue.key}\n"
        f"Summary: {issue.summary}\n"
        f"Type: {issue.issue_type}\n"
        f"Status: {issue.status}\n"
        f"Priority: {issue.priority}\n"
        f"Labels: {labels}\n"
        f"URL: {issue.url}\n\n"
        f"## Description\n\n{issue.description or '(no description)'}\n\n"
        f"## Comment history\n\n{issue.comments or '(no comments)'}\n\n"
        f"## Log attachments\n\n{issue.attachments_text or '(no log attachments)'}\n\n"
        f"---\n\n"
        f"Write a triage summary in markdown with exactly these 5 sections:\n\n"
        f"1. **Reported Issue Details** — restate the concrete facts (what/where/when/"
        f"severity) so this file is self-contained without reopening Jira\n"
        f"2. **Reproduction Steps** — extracted from the description/comments if present; "
        f"if the ticket doesn't provide them, say so explicitly rather than inventing steps\n"
        f"3. **Analysis Summary** — reasoning grounded in the log excerpts/attachments and "
        f"comment thread above, citing specific lines/quotes where relevant\n"
        f"4. **Root Cause & Recommended Fix** — if the comment thread already indicates "
        f"this was fixed/merged/resolved, say so explicitly and recommend closing instead "
        f"of proposing a redundant fix. Otherwise:\n"
        f"   - If evidence is inconclusive: list 2 or more distinct plausible root-cause "
        f"hypotheses, each with its own supporting reasoning — do not commit to a single "
        f"guess\n"
        f"   - If evidence is fairly conclusive: state one root cause with an explicit "
        f"confidence level (High/Medium/Low), plus concrete verification steps (e.g. which "
        f"log line or behavior to check, how to reproduce) to confirm it before anyone "
        f"implements a fix based on it\n"
        f"5. **Priority Assessment** — agree/disagree with the current priority, with "
        f"justification\n"
    )


def triage_jira_issue(issue: JiraIssue, log_fn=print) -> bool:
    """
    Generate AI triage summary for a Jira ticket and write to
    tasks/active/JIRA-<key>.md. Sets issue.triage_file and issue.triage_status.
    """
    acfg = anthropic_config()
    model = acfg["model_triage"]

    prompt = _jira_triage_prompt(issue)
    log_fn(f"  Triaging Jira {issue.key} (model: {model})")

    text = _call_api(prompt, model, log_fn=log_fn)
    if text is None:
        issue.triage_status = "failed"
        return False

    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    triage_file = TASKS_DIR / f"JIRA-{issue.key}.md"
    triage_file.write_text(text, encoding="utf-8")

    issue.triage_file = _rel(triage_file)
    issue.triage_status = "triaged"
    log_fn(f"  Written: {triage_file}")
    return True
