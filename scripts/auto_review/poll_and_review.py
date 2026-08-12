#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Automated PR review and issue analysis for ROCm repositories.

Polls GitHub for:
- PRs where review is requested on GITHUB_USER
- Issues assigned to GITHUB_USER

For each new PR: runs `claude -p /review-pr <url>` and writes to reviews/
For each new issue: runs `claude -p` to generate first-level analysis to tasks/active/

Updates DAILY_SUMMARY.md with results, marking unchanged items from the previous run.

Schedule: Run via Windows Task Scheduler at 10 PM PST and 7 AM PST.
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_USER = "nunnikri"
REPOS = [
    "ROCm/TheRock",
    "ROCm/rocm-systems",
    "ROCm/rocm-libraries",
    "ROCm/rockrel",
]

WORKSPACE_DIR = Path(__file__).resolve().parent.parent.parent  # claude-rocm-workspace
REVIEWS_DIR = WORKSPACE_DIR / "reviews"
TASKS_DIR = WORKSPACE_DIR / "tasks" / "active"
STATE_FILE = Path(__file__).resolve().parent / "state.json"
SUMMARY_FILE = Path(__file__).resolve().parent / "DAILY_SUMMARY.md"

LOG_PREFIX = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def _find_exe(name: str, candidates: list[str]) -> str:
    """Return full executable path: PATH first, then known Windows install locations."""
    import shutil
    found = shutil.which(name)
    if found:
        return found  # full path, not just name — subprocess needs this on Windows
    for c in candidates:
        if Path(c).exists():
            return c
    return name  # fall through and let subprocess raise


def _gh_exe() -> str:
    return _find_exe("gh", [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Users\nunnikri\AppData\Local\Programs\gh\bin\gh.exe",
    ])



def gh(*args: str) -> dict | list | None:
    """Run a gh CLI command and return parsed JSON output."""
    cmd = [_gh_exe()] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", check=True
        )
        stdout = result.stdout or ""
        return json.loads(stdout) if stdout.strip() else None
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        log(f"ERROR: gh command failed: {' '.join(cmd)}\n{stderr}")
        return None
    except json.JSONDecodeError:
        return None
    except Exception as e:
        log(f"ERROR: gh unexpected error: {e}")
        return None


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"reviewed_prs": {}, "analyzed_issues": {}, "last_run": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def today_review_dir() -> Path:
    d = REVIEWS_DIR / date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_short(full_repo: str) -> str:
    """ROCm/TheRock → TheRock"""
    return full_repo.split("/")[-1]


# ---------------------------------------------------------------------------
# GitHub data fetching
# ---------------------------------------------------------------------------

def get_review_requests() -> list[dict]:
    """Fetch all open PRs where review is requested for GITHUB_USER."""
    items = []
    for repo in REPOS:
        data = gh("api", f"repos/{repo}/pulls?state=open&per_page=50")
        if not isinstance(data, list):
            continue
        for pr in data:
            reviewers = [r.get("login") for r in pr.get("requested_reviewers", [])]
            if GITHUB_USER in reviewers:
                items.append({
                    "url": pr["html_url"],
                    "repo": repo,
                    "number": pr["number"],
                    "title": pr["title"],
                    "author": pr.get("user", {}).get("login", ""),
                    "updated_at": pr.get("updated_at", ""),
                    "head_ref": pr.get("head", {}).get("ref", ""),
                })
    return items


def get_assigned_issues() -> list[dict]:
    """Fetch all open issues assigned to GITHUB_USER."""
    items = []
    for repo in REPOS:
        data = gh("api", f"repos/{repo}/issues?assignee={GITHUB_USER}&state=open&per_page=50")
        if not isinstance(data, list):
            continue
        for issue in data:
            if "pull_request" in issue:
                continue  # skip PRs returned by issues endpoint
            items.append({
                "url": issue["html_url"],
                "repo": repo,
                "number": issue["number"],
                "title": issue["title"],
                "updated_at": issue.get("updated_at", ""),
                "labels": [lb["name"] for lb in issue.get("labels", [])],
            })
    return items


# ---------------------------------------------------------------------------
# Anthropic API helper
# ---------------------------------------------------------------------------

def _load_dotenv() -> dict[str, str]:
    """Load key=value pairs from scripts/auto_review/.env.
    Handles 'export KEY=value' syntax and strips surrounding quotes.
    Values containing secrets are never logged.
    """
    result: dict[str, str] = {}
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return result
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


# Loaded once; env var overrides .env for each key
_DOTENV = _load_dotenv()


def _cfg(key: str) -> str:
    """Return config value: environment variable first, then .env file."""
    return os.environ.get(key) or _DOTENV.get(key, "")


def _call_api_and_write(prompt: str, output_file: Path) -> bool:
    """Call the Anthropic Messages API and write the response to output_file.

    Reads all config from scripts/auto_review/.env (or environment variables):
      ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, ANTHROPIC_CUSTOM_HEADERS, ANTHROPIC_MODEL
    Keys and credentials are never passed to log().
    """
    api_key = _cfg("ANTHROPIC_API_KEY")
    if not api_key:
        log("  ERROR: ANTHROPIC_API_KEY not set — add it to scripts/auto_review/.env")
        return False

    base_url = (_cfg("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
    model = _cfg("ANTHROPIC_MODEL") or "claude-sonnet-4-5"

    # Parse "Key: value, Key2: value2" custom headers string
    extra_headers: dict[str, str] = {}
    raw_headers = _cfg("ANTHROPIC_CUSTOM_HEADERS")
    if raw_headers:
        for part in raw_headers.split(","):
            if ":" in part:
                hk, _, hv = part.strip().partition(":")
                extra_headers[hk.strip()] = hv.strip()

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
        text = result["content"][0]["text"]
        output_file.write_text(text, encoding="utf-8")
        log(f"  Written: {output_file} ({len(text)} chars)")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"  ERROR: API HTTP {e.code}: {body[:300]}")
        return False
    except urllib.error.URLError as e:
        log(f"  ERROR: API request failed: {e.reason}")
        return False
    except Exception as e:
        log(f"  ERROR: {e}")
        return False


# ---------------------------------------------------------------------------
# Review generation
# ---------------------------------------------------------------------------

def fetch_pr_content(pr_url: str, repo: str, number: int) -> str | None:
    """Pre-fetch PR metadata and diff using gh so claude doesn't need git access."""
    gh_exe = _gh_exe()

    # PR metadata
    meta = gh("api", f"repos/{repo}/pulls/{number}")
    if not meta:
        return None
    title = meta.get("title", "")
    body = meta.get("body", "") or ""
    author = meta.get("user", {}).get("login", "")
    base_ref = meta.get("base", {}).get("ref", "main")
    head_ref = meta.get("head", {}).get("ref", "")

    # Unified diff via gh pr diff — write to temp file to avoid pipe buffer deadlock
    import tempfile
    diff_text = "(diff unavailable)"
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as tf:
            tmp_path = tf.name
        with open(tmp_path, "w", encoding="utf-8", errors="replace") as tf:
            diff_result = subprocess.run(
                [gh_exe, "pr", "diff", str(number), "--repo", repo],
                stdout=tf, stderr=subprocess.DEVNULL, timeout=90,
            )
        if diff_result.returncode == 0:
            diff_text = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
        Path(tmp_path).unlink(missing_ok=True)
    except Exception as e:
        log(f"  WARNING: gh pr diff failed: {e}")

    # Truncate very large diffs to avoid blowing the prompt
    MAX_DIFF = 40_000
    if len(diff_text) > MAX_DIFF:
        diff_text = diff_text[:MAX_DIFF] + f"\n\n[... diff truncated at {MAX_DIFF} chars ...]"

    return (
        f"PR URL: {pr_url}\n"
        f"Repository: {repo}\n"
        f"PR #{number}: {title}\n"
        f"Author: {author}\n"
        f"Branch: {head_ref} → {base_ref}\n\n"
        f"## PR Description\n\n{body}\n\n"
        f"## Unified Diff\n\n```diff\n{diff_text}\n```"
    )


def run_review(pr_url: str, pr_meta: dict, review_file: Path) -> bool:
    """Pre-fetch PR content with gh, then pass to claude for analysis.

    Avoids running git inside claude (which hangs on Windows credential prompts).
    """
    repo = pr_meta["repo"]
    number = pr_meta["number"]

    log(f"  Fetching PR content for {repo}#{number}...")
    content = fetch_pr_content(pr_url, repo, number)
    if not content:
        log("  ERROR: could not fetch PR content")
        return False

    guidelines_path = WORKSPACE_DIR / "reviews" / "REVIEW_GUIDELINES.md"
    guidelines = guidelines_path.read_text(encoding="utf-8") if guidelines_path.exists() else ""

    prompt = (
        f"You are performing a code review. The PR content has been pre-fetched below.\n\n"
        f"Review guidelines:\n{guidelines}\n\n"
        f"---\n\n"
        f"{content}\n\n"
        f"---\n\n"
        f"Write a thorough code review covering: correctness, style, tests, architecture, "
        f"and any blocking issues. Use severity markers: ❌ BLOCKING, ⚠️ IMPORTANT, "
        f"💡 SUGGESTION, 📋 FUTURE WORK.\n\n"
        f"Format the review as markdown with sections: Problem, Summary of changes, "
        f"Overall Assessment, Detailed Review, Recommendations, Conclusion."
    )

    log(f"  Calling Anthropic API directly (prompt: {len(prompt)} chars)")
    return _call_api_and_write(prompt, review_file)


def run_issue_analysis(issue: dict, analysis_file: Path) -> bool:
    """Run claude -p to do first-level issue analysis."""
    repo = issue["repo"]
    number = issue["number"]
    title = issue["title"]
    labels = ", ".join(issue.get("labels", [])) or "none"

    # Fetch issue body via gh so the API prompt has full context
    issue_data = gh("api", f"repos/{repo}/issues/{number}")
    issue_body = (issue_data or {}).get("body", "") or ""

    prompt = (
        f"Analyze this GitHub issue as a first-level triage and write a structured\n"
        f"analysis to the file: tasks/active/{repo_short(repo)}-{number}.md\n\n"
        f"Repository: {repo}\n"
        f"Issue #{number}: {title}\n"
        f"Labels: {labels}\n"
        f"URL: {issue['url']}\n\n"
        f"## Issue description\n\n{issue_body}\n\n"
        f"## Analysis to write\n\n"
        f"1. Problem summary (2-3 sentences)\n"
        f"2. Root cause hypothesis\n"
        f"3. Affected components\n"
        f"4. Priority assessment (P0/P1/P2/P3)\n"
        f"5. Suggested next steps\n"
    )

    log(f"  Calling Anthropic API for issue analysis {repo}#{number}")
    return _call_api_and_write(prompt, analysis_file)


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def prepend_to_summary(section: str) -> None:
    """Add a new section at the top of DAILY_SUMMARY.md."""
    existing = SUMMARY_FILE.read_text(encoding="utf-8") if SUMMARY_FILE.exists() else ""
    SUMMARY_FILE.write_text(section + "\n\n" + existing, encoding="utf-8")


def build_summary_section(
    run_time: str,
    pr_results: list[dict],
    issue_results: list[dict],
) -> str:
    lines = [
        f"## {date.today().isoformat()} — Run at {run_time}",
        "",
        "### PR Reviews",
    ]

    if pr_results:
        for r in pr_results:
            status = r["status"]
            if status == "reviewed":
                lines.append(
                    f"- ✅ **NEW** [{r['repo']}#{r['number']}]({r['url']}) "
                    f"— {r['title']} → `{r['review_file']}`"
                )
            elif status == "no_change":
                lines.append(
                    f"- ⏸ NO CHANGE [{r['repo']}#{r['number']}]({r['url']}) "
                    f"— {r['title']} (last reviewed: {r.get('last_reviewed', 'never')})"
                )
            elif status == "failed":
                lines.append(
                    f"- ❌ FAILED [{r['repo']}#{r['number']}]({r['url']}) "
                    f"— {r['title']}"
                )
    else:
        lines.append("- No review requests found")

    lines += ["", "### Issues"]

    if issue_results:
        for r in issue_results:
            status = r["status"]
            if status == "analyzed":
                lines.append(
                    f"- ✅ **NEW** [{r['repo']}#{r['number']}]({r['url']}) "
                    f"— {r['title']} → `{r['analysis_file']}`"
                )
            elif status == "no_change":
                lines.append(
                    f"- ⏸ NO CHANGE [{r['repo']}#{r['number']}]({r['url']}) "
                    f"— {r['title']} (last analyzed: {r.get('last_analyzed', 'never')})"
                )
            elif status == "failed":
                lines.append(
                    f"- ❌ FAILED [{r['repo']}#{r['number']}]({r['url']}) "
                    f"— {r['title']}"
                )
    else:
        lines.append("- No assigned issues found")

    lines += ["", "---"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log("=== poll_and_review.py starting ===")
    log(f"Workspace: {WORKSPACE_DIR}")

    state = load_state()
    run_time = datetime.now().strftime("%H:%M:%S")
    pr_results = []
    issue_results = []

    # --- PR review requests ---
    log("Fetching PR review requests...")
    review_requests = get_review_requests()
    log(f"  Found {len(review_requests)} PRs requesting review")

    for pr in review_requests:
        url = pr["url"]
        repo = pr["repo"]
        number = pr["number"]
        updated_at = pr["updated_at"]
        key = url

        prev = state["reviewed_prs"].get(key, {})
        prev_updated = prev.get("updated_at", "")

        if prev and prev_updated == updated_at:
            # No change since last run
            log(f"  NO CHANGE: {repo}#{number}")
            pr_results.append({
                **pr,
                "status": "no_change",
                "last_reviewed": prev.get("reviewed_at", "never"),
            })
            continue

        # New or updated — generate review
        log(f"  NEW/UPDATED: {repo}#{number} — {pr['title']}")
        review_dir = today_review_dir()
        review_filename = f"pr_{repo_short(repo)}_{number}.md"
        review_file = review_dir / review_filename

        success = run_review(url, pr, review_file)
        reviewed_at = date.today().isoformat()

        state["reviewed_prs"][key] = {
            "url": url,
            "repo": repo,
            "number": number,
            "title": pr["title"],
            "updated_at": updated_at,
            "reviewed_at": reviewed_at,
            "review_file": str(review_file.relative_to(WORKSPACE_DIR)),
        }

        pr_results.append({
            **pr,
            "status": "reviewed" if success else "failed",
            "review_file": str(review_file.relative_to(WORKSPACE_DIR)),
        })

    # --- Assigned issues ---
    log("Fetching assigned issues...")
    assigned_issues = get_assigned_issues()
    log(f"  Found {len(assigned_issues)} assigned issues")

    for issue in assigned_issues:
        url = issue["url"]
        repo = issue["repo"]
        number = issue["number"]
        updated_at = issue["updated_at"]
        key = url

        prev = state["analyzed_issues"].get(key, {})
        prev_updated = prev.get("updated_at", "")

        if prev and prev_updated == updated_at:
            log(f"  NO CHANGE: {repo}#{number}")
            issue_results.append({
                **issue,
                "status": "no_change",
                "last_analyzed": prev.get("analyzed_at", "never"),
            })
            continue

        log(f"  NEW/UPDATED: {repo}#{number} — {issue['title']}")
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        analysis_file = TASKS_DIR / f"{repo_short(repo)}-{number}.md"

        success = run_issue_analysis(issue, analysis_file)
        analyzed_at = date.today().isoformat()

        state["analyzed_issues"][key] = {
            "url": url,
            "repo": repo,
            "number": number,
            "title": issue["title"],
            "updated_at": updated_at,
            "analyzed_at": analyzed_at,
            "analysis_file": str(analysis_file.relative_to(WORKSPACE_DIR)),
        }

        issue_results.append({
            **issue,
            "status": "analyzed" if success else "failed",
            "analysis_file": str(analysis_file.relative_to(WORKSPACE_DIR)),
        })

    # --- Save state and write summary ---
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    summary = build_summary_section(run_time, pr_results, issue_results)
    prepend_to_summary(summary)

    log(f"Summary written to {SUMMARY_FILE}")
    log(f"State saved to {STATE_FILE}")
    log("=== Done ===")

    # Print summary to stdout for Task Scheduler log
    print("\n" + summary)


if __name__ == "__main__":
    main()
