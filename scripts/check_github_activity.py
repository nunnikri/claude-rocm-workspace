#!/usr/bin/env python3
"""
GitHub activity poller for nirmal@dell-rack-13.

Polls GitHub for:
- PRs where you are a requested reviewer
- PRs assigned to you
- Issues assigned to you

For each NEW review request (not previously seen), triggers a Claude Code
review via `claude -p "/review-pr <URL>"` and saves output to a dated folder.

State is persisted in activity_state.json so duplicate triggers are avoided
across runs.

Usage:
    python3 check_github_activity.py [--dry-run] [--no-review]

    --dry-run    Print what would happen without triggering reviews or writing state
    --no-review  Poll and update state but skip triggering Claude reviews

Cron entry (every 15 minutes):
    */15 * * * * /home/nirmal/Project/Claude-Workspace/scripts/venv/bin/python3 \
        /home/nirmal/Project/Claude-Workspace/scripts/check_github_activity.py \
        >> /home/nirmal/Project/Claude-Workspace/scripts/poller.log 2>&1
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_USER = "nunnikri"
WORKSPACE = Path.home() / "Project" / "Claude-Workspace"
STATE_FILE = WORKSPACE / "activity_state.json"
REVIEWS_DIR = WORKSPACE / "reviews"
SCRIPTS_DIR = WORKSPACE / "scripts"
LOG_PREFIX = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "reviewed_prs": [],       # PR URLs already reviewed
        "seen_assigned_prs": [],  # PR URLs already noted (assigned to me)
        "seen_assigned_issues": [],  # Issue URLs already noted
        "last_poll": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh(*args: str) -> list[dict]:
    """Run a gh api command and return parsed JSON."""
    cmd = ["gh", "api", "--paginate"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"{LOG_PREFIX} ERROR: gh api failed: {result.stderr.strip()}", file=sys.stderr)
        return []
    try:
        data = json.loads(result.stdout)
        # gh --paginate may return a list of pages concatenated as JSON arrays
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        # Some paginated responses are concatenated JSON arrays — try splitting
        items = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
                if isinstance(chunk, list):
                    items.extend(chunk)
                else:
                    items.append(chunk)
            except json.JSONDecodeError:
                pass
        return items


def get_review_requests() -> list[dict]:
    """PRs where I am a requested reviewer (across all orgs/repos I have access to)."""
    items = gh("search/issues",
               "-f", f"q=is:pr is:open review-requested:{GITHUB_USER}",
               "-f", "per_page=100")
    results = []
    for page in items if isinstance(items[0], list) else [items]:
        results.extend(page if isinstance(page, list) else [page])
    # gh search returns {"items": [...]}
    all_items = []
    for item in results:
        if isinstance(item, dict) and "items" in item:
            all_items.extend(item["items"])
        elif isinstance(item, dict) and "html_url" in item:
            all_items.append(item)
    return all_items


def get_assigned_prs() -> list[dict]:
    """PRs assigned to me."""
    items = gh("search/issues",
               "-f", f"q=is:pr is:open assignee:{GITHUB_USER}",
               "-f", "per_page=100")
    all_items = []
    for item in items:
        if isinstance(item, dict) and "items" in item:
            all_items.extend(item["items"])
        elif isinstance(item, dict) and "html_url" in item:
            all_items.append(item)
    return all_items


def get_assigned_issues() -> list[dict]:
    """Issues assigned to me."""
    items = gh("search/issues",
               "-f", f"q=is:issue is:open assignee:{GITHUB_USER}",
               "-f", "per_page=100")
    all_items = []
    for item in items:
        if isinstance(item, dict) and "items" in item:
            all_items.extend(item["items"])
        elif isinstance(item, dict) and "html_url" in item:
            all_items.append(item)
    return all_items


# ---------------------------------------------------------------------------
# Review triggering
# ---------------------------------------------------------------------------

def trigger_review(pr_url: str, dry_run: bool = False) -> Path | None:
    """Run `claude -p "/review-pr <URL>"` and save output to dated review folder."""
    today = date.today().isoformat()
    review_dir = REVIEWS_DIR / today
    review_dir.mkdir(parents=True, exist_ok=True)

    # Derive filename from URL: .../ROCm/TheRock/pull/4910 -> pr_TheRock_4910.md
    parts = pr_url.rstrip("/").split("/")
    try:
        repo = parts[-3]
        number = parts[-1]
        filename = f"pr_{repo}_{number}.md"
    except IndexError:
        filename = f"pr_unknown_{datetime.now().strftime('%H%M%S')}.md"

    output_file = review_dir / filename

    if dry_run:
        print(f"{LOG_PREFIX} [DRY RUN] Would trigger review: {pr_url} -> {output_file}")
        return output_file

    print(f"{LOG_PREFIX} Triggering review for {pr_url} -> {output_file}")

    try:
        result = subprocess.run(
            ["claude", "-p", f"/review-pr {pr_url}"],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout per review
        )
        content = result.stdout
        if result.stderr:
            content += f"\n\n---\nSTDERR:\n{result.stderr}"
        output_file.write_text(content)
        print(f"{LOG_PREFIX} Review saved: {output_file}")
        return output_file
    except subprocess.TimeoutExpired:
        print(f"{LOG_PREFIX} ERROR: Review timed out for {pr_url}", file=sys.stderr)
        output_file.write_text(f"Review timed out after 300s for {pr_url}\n")
        return output_file
    except FileNotFoundError:
        print(f"{LOG_PREFIX} ERROR: `claude` CLI not found. Install it first.", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Poll GitHub activity and trigger reviews")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--no-review", action="store_true", help="Poll only, skip review trigger")
    args = parser.parse_args()

    print(f"{LOG_PREFIX} Polling GitHub activity for {GITHUB_USER}...")

    state = load_state()

    # --- Review requests ---
    review_requests = get_review_requests()
    new_review_prs = []
    for item in review_requests:
        url = item.get("html_url", "")
        if url and url not in state["reviewed_prs"]:
            new_review_prs.append(item)

    print(f"{LOG_PREFIX} Review requests: {len(review_requests)} total, {len(new_review_prs)} new")

    for item in new_review_prs:
        url = item["html_url"]
        title = item.get("title", "unknown")
        print(f"{LOG_PREFIX}   NEW review request: [{title}] {url}")

        if not args.no_review:
            trigger_review(url, dry_run=args.dry_run)

        if not args.dry_run:
            state["reviewed_prs"].append(url)

    # --- Assigned PRs ---
    assigned_prs = get_assigned_prs()
    new_assigned_prs = []
    for item in assigned_prs:
        url = item.get("html_url", "")
        if url and url not in state["seen_assigned_prs"]:
            new_assigned_prs.append(item)
            if not args.dry_run:
                state["seen_assigned_prs"].append(url)

    print(f"{LOG_PREFIX} Assigned PRs: {len(assigned_prs)} total, {len(new_assigned_prs)} new")
    for item in new_assigned_prs:
        print(f"{LOG_PREFIX}   NEW assigned PR: [{item.get('title', '?')}] {item['html_url']}")

    # --- Assigned issues ---
    assigned_issues = get_assigned_issues()
    new_assigned_issues = []
    for item in assigned_issues:
        url = item.get("html_url", "")
        if url and url not in state["seen_assigned_issues"]:
            new_assigned_issues.append(item)
            if not args.dry_run:
                state["seen_assigned_issues"].append(url)

    print(f"{LOG_PREFIX} Assigned issues: {len(assigned_issues)} total, {len(new_assigned_issues)} new")
    for item in new_assigned_issues:
        print(f"{LOG_PREFIX}   NEW assigned issue: [{item.get('title', '?')}] {item['html_url']}")

    # --- Persist state ---
    if not args.dry_run:
        state["last_poll"] = datetime.now().isoformat()
        save_state(state)
        print(f"{LOG_PREFIX} State saved.")

    print(f"{LOG_PREFIX} Done.")


if __name__ == "__main__":
    main()
