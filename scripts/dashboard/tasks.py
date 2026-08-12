#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Local task management.

Tasks are markdown files in tasks/active/ with optional YAML frontmatter:

    ---
    assignee: raramakr
    priority: P1
    status: in_progress
    repo: ROCm/TheRock
    ---

    Task description body...

Tasks without an assignee field are treated as unassigned (shown on main page).
Tasks without a status field default to "open".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from config import TASKS_DIR


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class Task:
    file: Path
    name: str                        # filename stem
    assignee: str = ""               # github username or empty
    priority: str = ""               # P0 / P1 / P2 / P3
    status: str = "open"             # open / in_progress / blocked / done
    repo: str = ""                   # associated repo (optional)
    title: str = ""                  # first non-empty line of body
    body: str = ""                   # full body text (after frontmatter)
    url: str = ""                    # link to file in workspace


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter_dict, body_text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip().lower()] = v.strip().strip('"').strip("'")
    body = text[m.end():]
    return fm, body


# ---------------------------------------------------------------------------
# Load tasks
# ---------------------------------------------------------------------------

def load_tasks(log_fn=print) -> list[Task]:
    """Read all .md files from tasks/active/ and return Task objects."""
    if not TASKS_DIR.exists():
        return []

    tasks: list[Task] = []
    for path in sorted(TASKS_DIR.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            log_fn(f"  WARNING: could not read task {path.name}: {e}")
            continue

        fm, body = _parse_frontmatter(text)

        # Extract title: first non-empty non-heading line of body
        title = fm.get("title", "")
        if not title:
            for line in body.splitlines():
                line = line.strip().lstrip("#").strip()
                if line:
                    title = line
                    break
        if not title:
            title = path.stem

        tasks.append(Task(
            file=path,
            name=path.stem,
            assignee=fm.get("assignee", ""),
            priority=fm.get("priority", ""),
            status=fm.get("status", "open"),
            repo=fm.get("repo", ""),
            title=title,
            body=body.strip(),
            url=f"tasks/active/{path.name}",
        ))

    return tasks


def tasks_for(user: str, all_tasks: list[Task]) -> list[Task]:
    """Return tasks assigned to `user` (case-insensitive)."""
    return [t for t in all_tasks if t.assignee.lower() == user.lower()]


def unassigned_tasks(all_tasks: list[Task]) -> list[Task]:
    """Return tasks with no assignee."""
    return [t for t in all_tasks if not t.assignee]
