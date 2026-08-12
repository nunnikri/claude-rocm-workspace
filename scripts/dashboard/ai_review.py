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
import urllib.error
import urllib.request
from pathlib import Path

from config import (
    REVIEWS_DIR,
    TASKS_DIR,
    anthropic_config,
    parse_custom_headers,
)
from github_client import Issue, PR


# ---------------------------------------------------------------------------
# Core API call
# ---------------------------------------------------------------------------

def _call_api(prompt: str, model: str, log_fn=print) -> str | None:
    """
    POST to Anthropic Messages API. Returns response text or None on error.
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
        "max_tokens": 8192,
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

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log_fn(f"  ERROR: API HTTP {e.code}: {body[:300]}")
        return None
    except urllib.error.URLError as e:
        log_fn(f"  ERROR: API request failed: {e.reason}")
        return None
    except Exception as e:
        log_fn(f"  ERROR: {e}")
        return None


# ---------------------------------------------------------------------------
# PR review
# ---------------------------------------------------------------------------

def _pr_review_prompt(pr: PR, guidelines: str) -> str:
    return (
        f"You are performing a code review for a ROCm build infrastructure PR.\n\n"
        f"## Review guidelines\n\n{guidelines}\n\n"
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

    pr.review_file = str(review_file.relative_to(REVIEWS_DIR.parent))
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

    issue.triage_file = str(triage_file.relative_to(TASKS_DIR.parent.parent))
    issue.triage_status = "triaged"
    log_fn(f"  Written: {triage_file}")
    return True
