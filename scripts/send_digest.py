#!/usr/bin/env python3
"""
Daily GitHub activity digest mailer for nirmal@dell-rack-13.

Sends a 6pm digest email summarizing:
- PRs awaiting your review (with links to auto-generated review files)
- PRs assigned to you
- Issues assigned to you

Only sends email if there is something to report OR if --force is passed.
Skips sending if nothing has changed since the last digest.

Email is sent via msmtp using Gmail SMTP. See README.md for setup.

Usage:
    python3 send_digest.py [--force] [--dry-run]

    --force      Send even if nothing is new
    --dry-run    Print the email body without sending

Cron entry (6pm daily, Mon-Fri):
    0 18 * * 1-5 /home/nirmal/Project/Claude-Workspace/scripts/venv/bin/python3 \
        /home/nirmal/Project/Claude-Workspace/scripts/send_digest.py \
        >> /home/nirmal/Project/Claude-Workspace/scripts/mailer.log 2>&1
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

SENDER = "amdnirmal26@gmail.com"
RECIPIENT = "nirmal.unnikrishnan@amd.com"
WORKSPACE = Path.home() / "Project" / "Claude-Workspace"
STATE_FILE = WORKSPACE / "activity_state.json"
REVIEWS_DIR = WORKSPACE / "reviews"
DIGEST_STATE_FILE = WORKSPACE / "scripts" / "last_digest_state.json"
LOG_PREFIX = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_activity_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"reviewed_prs": [], "seen_assigned_prs": [], "seen_assigned_issues": []}


def load_digest_state() -> dict:
    if DIGEST_STATE_FILE.exists():
        return json.loads(DIGEST_STATE_FILE.read_text())
    return {"last_sent_hash": None, "last_sent_date": None}


def save_digest_state(state: dict) -> None:
    DIGEST_STATE_FILE.write_text(json.dumps(state, indent=2))


def state_hash(activity: dict) -> str:
    """Simple hash to detect if anything changed since last digest."""
    import hashlib
    key = json.dumps({
        "reviewed_prs": sorted(activity.get("reviewed_prs", [])),
        "seen_assigned_prs": sorted(activity.get("seen_assigned_prs", [])),
        "seen_assigned_issues": sorted(activity.get("seen_assigned_issues", [])),
    }, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Review file lookup
# ---------------------------------------------------------------------------

def find_review_file(pr_url: str) -> Path | None:
    """Find the most recent review file generated for a PR URL."""
    parts = pr_url.rstrip("/").split("/")
    try:
        repo = parts[-3]
        number = parts[-1]
        filename = f"pr_{repo}_{number}.md"
    except IndexError:
        return None

    # Search dated folders, most recent first
    dated_dirs = sorted(REVIEWS_DIR.glob("????-??-??"), reverse=True)
    for d in dated_dirs:
        candidate = d / filename
        if candidate.exists():
            return candidate
    return None


def review_summary(pr_url: str) -> str:
    """Extract a one-line summary from a review file, or return placeholder."""
    review_file = find_review_file(pr_url)
    if review_file is None:
        return "  (review pending or not yet generated)"
    # Pull first non-empty, non-heading line as summary
    for line in review_file.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return f"  Review: {review_file.name} — {stripped[:120]}"
    return f"  Review: {review_file.name}"


# ---------------------------------------------------------------------------
# Email building
# ---------------------------------------------------------------------------

def build_email_body(activity: dict) -> str:
    today = date.today().strftime("%A, %B %d %Y")
    now = datetime.now().strftime("%H:%M")
    lines = [
        f"GitHub Activity Digest — {today} at {now}",
        "=" * 60,
        "",
    ]

    # --- PRs awaiting review ---
    review_prs = activity.get("reviewed_prs", [])
    lines.append(f"PRs AWAITING YOUR REVIEW ({len(review_prs)})")
    lines.append("-" * 40)
    if review_prs:
        for url in review_prs:
            lines.append(f"  {url}")
            lines.append(review_summary(url))
            lines.append("")
    else:
        lines.append("  None")
        lines.append("")

    # --- Assigned PRs ---
    assigned_prs = activity.get("seen_assigned_prs", [])
    lines.append(f"PRs ASSIGNED TO YOU ({len(assigned_prs)})")
    lines.append("-" * 40)
    if assigned_prs:
        for url in assigned_prs:
            lines.append(f"  {url}")
    else:
        lines.append("  None")
    lines.append("")

    # --- Assigned issues ---
    assigned_issues = activity.get("seen_assigned_issues", [])
    lines.append(f"ISSUES ASSIGNED TO YOU ({len(assigned_issues)})")
    lines.append("-" * 40)
    if assigned_issues:
        for url in assigned_issues:
            lines.append(f"  {url}")
    else:
        lines.append("  None")
    lines.append("")

    lines.append("=" * 60)
    lines.append("Generated by check_github_activity.py on nirmal@dell-rack-13")

    return "\n".join(lines)


def build_subject(activity: dict) -> str:
    n_reviews = len(activity.get("reviewed_prs", []))
    n_prs = len(activity.get("seen_assigned_prs", []))
    n_issues = len(activity.get("seen_assigned_issues", []))
    today = date.today().strftime("%Y-%m-%d")
    return (
        f"[GitHub Digest {today}] "
        f"{n_reviews} review(s), {n_prs} assigned PR(s), {n_issues} issue(s)"
    )


# ---------------------------------------------------------------------------
# Sending via msmtp
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, dry_run: bool = False) -> bool:
    message = (
        f"From: {SENDER}\n"
        f"To: {RECIPIENT}\n"
        f"Subject: {subject}\n"
        f"Content-Type: text/plain; charset=utf-8\n"
        f"\n"
        f"{body}"
    )

    if dry_run:
        print("--- EMAIL (DRY RUN) ---")
        print(message)
        print("--- END EMAIL ---")
        return True

    try:
        result = subprocess.run(
            ["msmtp", "--account=gmail", RECIPIENT],
            input=message,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"{LOG_PREFIX} ERROR: msmtp failed: {result.stderr.strip()}", file=sys.stderr)
            return False
        print(f"{LOG_PREFIX} Email sent to {RECIPIENT}")
        return True
    except FileNotFoundError:
        print(f"{LOG_PREFIX} ERROR: msmtp not found. Install with: sudo apt install msmtp", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Send GitHub activity digest email")
    parser.add_argument("--force", action="store_true", help="Send even if nothing is new")
    parser.add_argument("--dry-run", action="store_true", help="Print email without sending")
    args = parser.parse_args()

    activity = load_activity_state()
    digest_state = load_digest_state()

    current_hash = state_hash(activity)
    last_hash = digest_state.get("last_sent_hash")
    last_date = digest_state.get("last_sent_date")
    today = date.today().isoformat()

    has_anything = (
        activity.get("reviewed_prs")
        or activity.get("seen_assigned_prs")
        or activity.get("seen_assigned_issues")
    )

    # Skip if nothing to report and nothing changed since last digest today
    if not args.force and not args.dry_run:
        if not has_anything:
            print(f"{LOG_PREFIX} Nothing to report. Skipping.")
            return
        if current_hash == last_hash and last_date == today:
            print(f"{LOG_PREFIX} No changes since last digest today. Skipping.")
            return

    subject = build_subject(activity)
    body = build_email_body(activity)

    sent = send_email(subject, body, dry_run=args.dry_run)

    if sent and not args.dry_run:
        digest_state["last_sent_hash"] = current_hash
        digest_state["last_sent_date"] = today
        save_digest_state(digest_state)


if __name__ == "__main__":
    main()
