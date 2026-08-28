#!/usr/bin/env python3
# Copyright Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""
Central configuration: team members, repos, paths, and .env loading.
All other modules import from here — nothing hardcoded elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file — works on Windows and Linux)
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = DASHBOARD_DIR.parent.parent        # claude-rocm-workspace root
REVIEWS_DIR   = WORKSPACE_DIR / "reviews"
TASKS_DIR     = WORKSPACE_DIR / "tasks" / "active"
STATE_FILE    = DASHBOARD_DIR / "state.json"
OUTPUT_HTML   = DASHBOARD_DIR / "dashboard.html"

# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------

TEAM: list[str] = [
    "nunnikri",
    "raramakr",
    "arvindcheru",
    "jonatluu",
    "dileepr1",
]

# GitHub username -> AMD email, used to query Jira (assignee is email-based there).
TEAM_JIRA_EMAILS: dict[str, str] = {
    "nunnikri": "nirmal.unnikrishnan@amd.com",
    "arvindcheru": "Aravindan.Cheruvally@amd.com",
    "raramakr": "Ranjith.Ramakrishnan@amd.com",
    "dileepr1": "Dileep.Ravindranathan@amd.com",
    "jonatluu": "Jonathan.Luu@amd.com",
}

# ---------------------------------------------------------------------------
# Repositories to monitor
# ---------------------------------------------------------------------------

REPOS: list[str] = [
    "ROCm/TheRock",
    "ROCm/rocm-systems",
    "ROCm/rocm-libraries",
    "ROCm/rockrel",
]

# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def _load_dotenv() -> dict[str, str]:
    """
    Load key=value pairs from scripts/dashboard/.env.
    Handles 'export KEY=value' syntax and strips surrounding quotes.
    Values are never logged — callers must not pass them to log().
    """
    result: dict[str, str] = {}
    env_file = DASHBOARD_DIR / ".env"
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


_DOTENV: dict[str, str] = _load_dotenv()


def cfg(key: str, default: str = "") -> str:
    """Return config value: scripts/dashboard/.env first, then environment variable.

    .env takes precedence deliberately: this script must be self-contained and
    not silently inherit ambient vars from whatever shell/IDE launched it (e.g.
    a Claude Code session's own ANTHROPIC_MODEL, ANTHROPIC_API_KEY, etc., which
    are unrelated to this script's own AMD proxy configuration).
    """
    return _DOTENV.get(key) or os.environ.get(key, default)


# ---------------------------------------------------------------------------
# Anthropic API settings (read via cfg(), never hardcoded)
# ---------------------------------------------------------------------------

def anthropic_config() -> dict[str, str]:
    """
    Return Anthropic API settings from env/.env.
    Keys: api_key, base_url, model_review, model_triage, custom_headers_raw.
    Never log the returned dict.
    """
    return {
        "api_key":            cfg("ANTHROPIC_API_KEY"),
        "base_url":           cfg("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"),
        "model_review":       cfg("ANTHROPIC_MODEL_REVIEW", cfg("ANTHROPIC_MODEL", "claude-sonnet-4-5")),
        "model_triage":       cfg("ANTHROPIC_MODEL_TRIAGE", cfg("ANTHROPIC_MODEL", "claude-sonnet-4-5")),
        "custom_headers_raw": cfg("ANTHROPIC_CUSTOM_HEADERS", ""),
    }


def parse_custom_headers(raw: str) -> dict[str, str]:
    """Parse 'Key: value, Key2: value2' into a dict. Used for AMD proxy headers."""
    headers: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            k, _, v = part.strip().partition(":")
            headers[k.strip()] = v.strip()
    return headers
