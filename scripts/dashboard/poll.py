#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Main orchestrator for the ROCm Team Dashboard.

Run this script on a schedule (Windows Task Scheduler or Linux cron).
It fetches GitHub data for all team members, runs AI reviews/triage
for new/updated items, and regenerates dashboard.html.

Usage:
    python poll.py              # full run
    python poll.py --no-ai      # fetch data + generate HTML, skip AI calls
    python poll.py --html-only  # regenerate HTML from existing state (no fetching)
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone

# All imports relative to scripts/dashboard/ — works when run as:
#   cd scripts/dashboard && python poll.py
#   python scripts/dashboard/poll.py  (from workspace root)
import ai_review
import github_client as gh
import html_gen
import jira_client
import state as st
import tasks as task_lib
from config import REPOS, TEAM, TEAM_JIRA_EMAILS


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode(sys.stdout.encoding or "ascii", errors="replace")
                  .decode(sys.stdout.encoding or "ascii"), flush=True)


# ---------------------------------------------------------------------------
# Per-member data collection
# ---------------------------------------------------------------------------

def _collect_member(
    user: str,
    current_state: dict,
    run_ai: bool,
    log_fn=_log,
) -> dict:
    """
    Fetch and review all data for one team member.
    Returns a dict with prs_created, review_requests, issues, jira_issues, tasks.
    """
    log_fn(f"--- {user} ---")

    # 1. PRs created (open, non-draft)
    log_fn(f"  Fetching PRs created by {user}...")
    prs_created = gh.fetch_prs_created_by(user, REPOS, log_fn=log_fn)
    log_fn(f"  Found {len(prs_created)} open PRs created")

    # 2. PRs where review is requested
    log_fn(f"  Fetching review requests for {user}...")
    review_requests = gh.fetch_review_requests(user, REPOS, log_fn=log_fn)
    log_fn(f"  Found {len(review_requests)} review requests")

    # 3. GitHub issues assigned
    log_fn(f"  Fetching assigned issues for {user}...")
    issues = gh.fetch_assigned_issues(user, REPOS, log_fn=log_fn)
    log_fn(f"  Found {len(issues)} assigned issues")

    # 4. Jira issues assigned (email-based lookup)
    jira_issues: list = []
    jira_email = TEAM_JIRA_EMAILS.get(user)
    if jira_email:
        log_fn(f"  Fetching Jira issues for {jira_email}...")
        jira_issues = jira_client.fetch_assigned_jira_issues(jira_email, log_fn=log_fn)
        log_fn(f"  Found {len(jira_issues)} Jira issues")
    else:
        log_fn(f"  No Jira email mapping for {user} — skipping Jira fetch")

    if not run_ai:
        # Restore previous review/triage status from state
        for pr in prs_created + review_requests:
            st.restore_pr_review(pr, current_state)
        for issue in issues:
            st.restore_issue_triage(issue, current_state)
        for jira_issue in jira_issues:
            st.restore_jira_issue_triage(jira_issue, current_state)
        return dict(
            prs_created=prs_created,
            review_requests=review_requests,
            issues=issues,
            jira_issues=jira_issues,
        )

    # 5. AI review for PRs
    all_prs = prs_created + review_requests
    for pr in all_prs:
        if st.pr_needs_review(pr, current_state):
            log_fn(f"  NEW/UPDATED PR: {pr.repo}#{pr.number} — {pr.title}")
            gh.fetch_pr_details(pr, log_fn=log_fn)  # get head/base refs
            gh.fetch_pr_diff(pr, log_fn=log_fn)
            ai_review.review_pr(pr, log_fn=log_fn)
            st.mark_pr_reviewed(pr, current_state)
        else:
            log_fn(f"  NO CHANGE: {pr.repo}#{pr.number}")
            st.restore_pr_review(pr, current_state)

    # 6. AI triage for GitHub issues
    for issue in issues:
        if st.issue_needs_triage(issue, current_state):
            log_fn(f"  NEW/UPDATED issue: {issue.repo}#{issue.number} — {issue.title}")
            gh.fetch_issue_body(issue, log_fn=log_fn)
            ai_review.triage_issue(issue, log_fn=log_fn)
            st.mark_issue_triaged(issue, current_state)
        else:
            log_fn(f"  NO CHANGE: {issue.repo}#{issue.number}")
            st.restore_issue_triage(issue, current_state)

    # 7. AI triage for Jira issues (two-tier gating — see state.py docstrings:
    #    cheap tier on `updated`, then a content-hash tier so metadata-only
    #    touches like a label/sprint change don't trigger a wasted AI call)
    for jira_issue in jira_issues:
        if st.jira_issue_needs_triage(jira_issue, current_state):
            jira_client.fetch_issue_context(jira_issue, log_fn=log_fn)
            if st.jira_issue_content_unchanged(jira_issue, current_state):
                log_fn(f"  Metadata-only change: {jira_issue.key} (skipping re-triage)")
                st.restore_jira_issue_triage(jira_issue, current_state)
                st.mark_jira_issue_seen(jira_issue, current_state)
            else:
                log_fn(f"  NEW/UPDATED Jira: {jira_issue.key} — {jira_issue.summary}")
                ai_review.triage_jira_issue(jira_issue, log_fn=log_fn)
                st.mark_jira_issue_triaged(jira_issue, current_state)
        else:
            log_fn(f"  NO CHANGE: {jira_issue.key}")
            st.restore_jira_issue_triage(jira_issue, current_state)

    return dict(
        prs_created=prs_created,
        review_requests=review_requests,
        issues=issues,
        jira_issues=jira_issues,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="ROCm Team Dashboard poller")
    parser.add_argument("--no-ai",     action="store_true",
                        help="Fetch GitHub data but skip AI review/triage calls")
    parser.add_argument("--html-only", action="store_true",
                        help="Regenerate HTML from existing state without fetching")
    args = parser.parse_args()

    _log("=== ROCm Dashboard poll starting ===")

    current_state = st.load()

    member_data: dict[str, dict] = {}

    if not args.html_only:
        for user in TEAM:
            try:
                data = _collect_member(
                    user=user,
                    current_state=current_state,
                    run_ai=not args.no_ai,
                )
                member_data[user] = data
            except Exception:
                _log(f"ERROR collecting data for {user}:\n{traceback.format_exc()}")
                member_data[user] = dict(
                    prs_created=[], review_requests=[],
                    issues=[], jira_issues=[],
                )
    else:
        _log("--html-only: skipping GitHub fetch, using state.json")
        # Reconstruct minimal data from state for HTML generation
        for user in TEAM:
            member_data[user] = dict(
                prs_created=[], review_requests=[],
                issues=[], jira_issues=[],
            )

    # Load local tasks (global, then filtered per member inside html_gen)
    _log("Loading local tasks...")
    all_tasks = task_lib.load_tasks(log_fn=_log)
    _log(f"  Found {len(all_tasks)} tasks")

    # Attach tasks per member
    for user in TEAM:
        member_data[user]["tasks"] = task_lib.tasks_for(user, all_tasks)

    # Save state
    current_state["last_run"] = datetime.now(timezone.utc).isoformat()
    st.save(current_state)

    # Generate HTML
    _log("Generating dashboard.html...")
    html_gen.generate(member_data, all_tasks, log_fn=_log)

    _log("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
