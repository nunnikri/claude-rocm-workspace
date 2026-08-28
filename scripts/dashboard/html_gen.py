#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Generate dashboard.html — single self-contained file with:
- Summary bar: team-wide counts
- Per-member tabs: PRs created, review requests, GitHub issues, Jira issues, tasks
- All links open review/triage files or GitHub/Jira URLs
- No external CDN dependencies — CSS and JS are inline
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from config import OUTPUT_HTML, TEAM
from github_client import Issue, PR
from jira_client import JiraIssue
from tasks import Task


# ---------------------------------------------------------------------------
# Status badge helpers
# ---------------------------------------------------------------------------

_STATUS_BADGE = {
    "reviewed":  ('<span class="badge badge-ok">✅ reviewed</span>', True),
    "triaged":   ('<span class="badge badge-ok">✅ triaged</span>',  True),
    "failed":    ('<span class="badge badge-err">❌ failed</span>',  False),
    "pending":   ('<span class="badge badge-warn">⏳ pending</span>', False),
    "":          ('<span class="badge badge-warn">⏳ pending</span>', False),
}

_PRIORITY_BADGE = {
    "P0": '<span class="badge badge-err">P0</span>',
    "P1": '<span class="badge badge-warn">P1</span>',
    "P2": '<span class="badge badge-info">P2</span>',
    "P3": '<span class="badge badge-muted">P3</span>',
}

_TASK_STATUS_BADGE = {
    "open":        '<span class="badge badge-warn">open</span>',
    "in_progress": '<span class="badge badge-info">in progress</span>',
    "blocked":     '<span class="badge badge-err">blocked</span>',
    "done":        '<span class="badge badge-ok">done</span>',
}


def _e(s: str) -> str:
    return html.escape(str(s))


def _pr_row(pr: PR, kind: str) -> str:
    """Render a single PR table row."""
    badge_html, _ = _STATUS_BADGE.get(pr.review_status, _STATUS_BADGE[""])
    review_link = (
        f'<a href="{_e(pr.review_file)}" target="_blank">view review</a>'
        if pr.review_file else "—"
    )
    draft = ' <span class="badge badge-muted">draft</span>' if pr.draft else ""
    repo_short = pr.repo.split("/")[-1]
    return (
        f"<tr>"
        f'<td><a href="{_e(pr.url)}" target="_blank">#{pr.number}</a></td>'
        f"<td>{_e(repo_short)}</td>"
        f'<td>{_e(pr.title)}{draft}</td>'
        f"<td>{badge_html}</td>"
        f"<td>{review_link}</td>"
        f"</tr>\n"
    )


def _issue_row(issue: Issue) -> str:
    """Render a single issue table row."""
    badge_html, _ = _STATUS_BADGE.get(issue.triage_status, _STATUS_BADGE[""])
    triage_link = (
        f'<a href="{_e(issue.triage_file)}" target="_blank">view triage</a>'
        if issue.triage_file else "—"
    )
    labels = " ".join(f'<span class="label">{_e(lb)}</span>' for lb in issue.labels[:3])
    repo_short = issue.repo.split("/")[-1]
    return (
        f"<tr>"
        f'<td><a href="{_e(issue.url)}" target="_blank">#{issue.number}</a></td>'
        f"<td>{_e(repo_short)}</td>"
        f'<td>{_e(issue.title)} {labels}</td>'
        f"<td>{badge_html}</td>"
        f"<td>{triage_link}</td>"
        f"</tr>\n"
    )


def _jira_row(issue: JiraIssue) -> str:
    """Render a single Jira issue table row."""
    badge_html, _ = _STATUS_BADGE.get(issue.triage_status, _STATUS_BADGE[""])
    triage_link = (
        f'<a href="{_e(issue.triage_file)}" target="_blank">view triage</a>'
        if issue.triage_file else "—"
    )
    priority_short = issue.priority.split(":")[0].strip() if issue.priority else ""
    priority_badge = _PRIORITY_BADGE.get(priority_short, f'<span class="badge badge-muted">{_e(issue.priority)}</span>' if issue.priority else "")
    labels = " ".join(f'<span class="label">{_e(lb)}</span>' for lb in issue.labels[:3])
    return (
        f"<tr>"
        f'<td><a href="{_e(issue.url)}" target="_blank">{_e(issue.key)}</a></td>'
        f"<td>{_e(issue.status)}</td>"
        f"<td>{priority_badge}</td>"
        f'<td>{_e(issue.summary)} {labels}</td>'
        f"<td>{badge_html}</td>"
        f"<td>{triage_link}</td>"
        f"</tr>\n"
    )


def _jira_table(issues: list[JiraIssue], empty_msg: str) -> str:
    if not issues:
        return f'<p class="empty">{empty_msg}</p>'
    rows = "".join(_jira_row(i) for i in issues)
    return (
        "<table>"
        "<thead><tr><th>Key</th><th>Status</th><th>Priority</th><th>Summary</th>"
        "<th>Triage</th><th>Link</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _task_row(task: Task) -> str:
    status_badge = _TASK_STATUS_BADGE.get(task.status, _TASK_STATUS_BADGE["open"])
    priority_badge = _PRIORITY_BADGE.get(task.priority, "")
    repo_short = task.repo.split("/")[-1] if task.repo else "-"
    return (
        f"<tr>"
        f'<td><a href="{_e(task.url)}" target="_blank">{_e(task.name)}</a></td>'
        f"<td>{_e(repo_short)}</td>"
        f"<td>{_e(task.title)}</td>"
        f"<td>{priority_badge}</td>"
        f"<td>{status_badge}</td>"
        f"</tr>\n"
    )


def _pr_table(prs: list[PR], kind: str, empty_msg: str) -> str:
    if not prs:
        return f'<p class="empty">{empty_msg}</p>'
    rows = "".join(_pr_row(pr, kind) for pr in prs)
    return (
        "<table>"
        "<thead><tr><th>#</th><th>Repo</th><th>Title</th>"
        "<th>Review</th><th>Link</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _issue_table(issues: list[Issue], empty_msg: str) -> str:
    if not issues:
        return f'<p class="empty">{empty_msg}</p>'
    rows = "".join(_issue_row(i) for i in issues)
    return (
        "<table>"
        "<thead><tr><th>#</th><th>Repo</th><th>Title</th>"
        "<th>Triage</th><th>Link</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _task_table(tasks: list[Task], empty_msg: str) -> str:
    if not tasks:
        return f'<p class="empty">{empty_msg}</p>'
    rows = "".join(_task_row(t) for t in tasks)
    return (
        "<table>"
        "<thead><tr><th>Task</th><th>Repo</th><th>Title</th>"
        "<th>Priority</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


# ---------------------------------------------------------------------------
# Per-member tab content
# ---------------------------------------------------------------------------

def _member_section(
    user: str,
    prs_created: list[PR],
    review_requests: list[PR],
    issues: list[Issue],
    jira_issues: list[JiraIssue],
    tasks: list[Task],
) -> str:
    return f"""
<div class="member-section" id="member-{_e(user)}">
  <h2>@{_e(user)}</h2>

  <div class="section-block">
    <h3>PRs Created <span class="count">{len(prs_created)}</span></h3>
    {_pr_table(prs_created, "created", "No open PRs created.")}
  </div>

  <div class="section-block">
    <h3>Review Requests <span class="count">{len(review_requests)}</span></h3>
    {_pr_table(review_requests, "requested", "No pending review requests.")}
  </div>

  <div class="section-block">
    <h3>GitHub Issues <span class="count">{len(issues)}</span></h3>
    {_issue_table(issues, "No assigned issues.")}
  </div>

  <div class="section-block">
    <h3>Jira Issues <span class="count">{len(jira_issues)}</span></h3>
    {_jira_table(jira_issues, "No assigned Jira issues.")}
  </div>

  <div class="section-block">
    <h3>Tasks <span class="count">{len(tasks)}</span></h3>
    {_task_table(tasks, "No tasks assigned.")}
  </div>
</div>
"""


# ---------------------------------------------------------------------------
# CSS + JS (inline, no CDN)
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #c9d1d9; --muted: #8b949e; --link: #58a6ff;
  --ok: #3fb950; --warn: #d29922; --err: #f85149; --info: #58a6ff;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, sans-serif; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }

/* Header */
header { background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
header h1 { font-size: 18px; }
header .updated { font-size: 12px; color: var(--muted); margin-left: auto; }

/* Summary bar */
.summary-bar { display: flex; gap: 16px; padding: 16px 24px; flex-wrap: wrap; }
.summary-card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px 20px; min-width: 140px; }
.summary-card .num { font-size: 28px; font-weight: bold; }
.summary-card .label { font-size: 12px; color: var(--muted); }

/* Tabs */
.tabs { display: flex; gap: 0; border-bottom: 1px solid var(--border);
  padding: 0 24px; overflow-x: auto; }
.tab-btn { background: none; border: none; color: var(--muted); cursor: pointer;
  padding: 10px 16px; font-size: 14px; border-bottom: 2px solid transparent; }
.tab-btn:hover { color: var(--text); }
.tab-btn.active { color: var(--link); border-bottom-color: var(--link); }

/* Content */
.tab-content { display: none; padding: 24px; }
.tab-content.active { display: block; }
.member-section h2 { font-size: 16px; margin-bottom: 16px; }
.section-block { margin-bottom: 28px; }
.section-block h3 { font-size: 13px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .05em; margin-bottom: 10px; }
.count { font-size: 12px; background: var(--border); border-radius: 10px;
  padding: 1px 7px; font-weight: normal; }

/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500;
  padding: 6px 10px; border-bottom: 1px solid var(--border); }
td { padding: 7px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: var(--surface); }

/* Badges */
.badge { display: inline-block; font-size: 11px; padding: 1px 6px;
  border-radius: 4px; font-weight: 500; }
.badge-ok   { background: #1a3a1f; color: var(--ok); }
.badge-warn { background: #3a2e0d; color: var(--warn); }
.badge-err  { background: #3a1a1a; color: var(--err); }
.badge-info { background: #1a2a3a; color: var(--info); }
.badge-muted{ background: var(--border); color: var(--muted); }
.label { display: inline-block; font-size: 11px; padding: 1px 5px;
  border-radius: 3px; background: var(--border); color: var(--muted); margin: 1px; }
.empty { color: var(--muted); font-style: italic; font-size: 13px; padding: 8px 0; }
"""

_JS = """
function showTab(tabId) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector('[data-tab="' + tabId + '"]').classList.add('active');
  document.getElementById('tab-' + tabId).classList.add('active');
  location.hash = tabId;
}
window.addEventListener('DOMContentLoaded', () => {
  const hash = location.hash.replace('#', '');
  const tabs = document.querySelectorAll('.tab-btn');
  const target = hash && document.querySelector('[data-tab="' + hash + '"]');
  if (target) { showTab(hash); }
  else if (tabs.length) { tabs[0].click(); }
});
"""


# ---------------------------------------------------------------------------
# Top-level summary tab
# ---------------------------------------------------------------------------

def _summary_tab(
    member_data: dict[str, dict],
    all_tasks: list[Task],
) -> str:
    total_created  = sum(len(v["prs_created"]) for v in member_data.values())
    total_requests = sum(len(v["review_requests"]) for v in member_data.values())
    total_issues   = sum(len(v["issues"]) for v in member_data.values())
    total_jira     = sum(len(v.get("jira_issues", [])) for v in member_data.values())
    total_tasks    = len([t for t in all_tasks if t.status not in ("done",)])

    cards = f"""
<div class="summary-bar">
  <div class="summary-card">
    <div class="num">{total_created}</div>
    <div class="label">Open PRs created</div>
  </div>
  <div class="summary-card">
    <div class="num">{total_requests}</div>
    <div class="label">Review requests</div>
  </div>
  <div class="summary-card">
    <div class="num">{total_issues}</div>
    <div class="label">GitHub issues</div>
  </div>
  <div class="summary-card">
    <div class="num">{total_jira}</div>
    <div class="label">Jira issues</div>
  </div>
  <div class="summary-card">
    <div class="num">{total_tasks}</div>
    <div class="label">Open tasks</div>
  </div>
</div>
"""

    # Per-member summary grid
    rows = ""
    for user in TEAM:
        d = member_data.get(user, {})
        c = len(d.get("prs_created", []))
        r = len(d.get("review_requests", []))
        i = len(d.get("issues", []))
        j = len(d.get("jira_issues", []))
        t = len(d.get("tasks", []))
        rows += (
            f'<tr>'
            f'<td><a href="#" onclick="showTab(\'{_e(user)}\');return false;">'
            f'@{_e(user)}</a></td>'
            f"<td>{c}</td><td>{r}</td><td>{i}</td><td>{j}</td><td>{t}</td>"
            f"</tr>\n"
        )

    member_table = (
        "<table><thead><tr>"
        "<th>Member</th><th>PRs created</th><th>Review requests</th>"
        "<th>GH issues</th><th>Jira issues</th><th>Tasks</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )

    return cards + '<div style="padding:0 24px 24px">' + member_table + "</div>"


# ---------------------------------------------------------------------------
# Main HTML generation
# ---------------------------------------------------------------------------

def generate(
    member_data: dict[str, dict],
    all_tasks: list[Task],
    log_fn=print,
) -> None:
    """
    Generate dashboard.html.

    member_data: { github_user: {
        prs_created: list[PR],
        review_requests: list[PR],
        issues: list[Issue],
        jira_issues: list[JiraIssue],
        tasks: list[Task],
    }}
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build tabs list: Overview + one per team member
    tab_defs = [("overview", "Overview")] + [(u, f"@{u}") for u in TEAM]

    tab_buttons = "\n".join(
        f'<button class="tab-btn" data-tab="{tid}" onclick="showTab(\'{tid}\')">'
        f"{label}</button>"
        for tid, label in tab_defs
    )

    # Overview tab content
    overview_content = _summary_tab(member_data, all_tasks)

    # Per-member tab content
    member_tabs = ""
    for user in TEAM:
        d = member_data.get(user, {})
        content = _member_section(
            user=user,
            prs_created=d.get("prs_created", []),
            review_requests=d.get("review_requests", []),
            issues=d.get("issues", []),
            jira_issues=d.get("jira_issues", []),
            tasks=d.get("tasks", []),
        )
        member_tabs += (
            f'<div class="tab-content" id="tab-{_e(user)}">{content}</div>\n'
        )

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROCm Team Dashboard</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>🔧 ROCm Team Dashboard</h1>
  <span class="updated">Last updated: {_e(now)}</span>
</header>

<div class="tabs">
{tab_buttons}
</div>

<div class="tab-content active" id="tab-overview">
{overview_content}
</div>

{member_tabs}

<script>{_JS}</script>
</body>
</html>
"""

    OUTPUT_HTML.write_text(doc, encoding="utf-8")
    log_fn(f"Dashboard written: {OUTPUT_HTML}")
