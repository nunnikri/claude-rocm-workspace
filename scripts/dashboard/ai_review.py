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
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
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
_DEFAULT_MAX_TOKENS = 3000


def _call_api(
    prompt: str, model: str, max_tokens: int = _DEFAULT_MAX_TOKENS, log_fn=print
) -> str | None:
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
        "max_tokens": max_tokens,
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

_GUIDELINES_LIMIT = 2000       # keep prompt under AMD proxy timeout budget
_REVIEW_MAX_TOKENS = 8000      # a thorough multi-section review needs more room than triage
_SYNTHESIS_MAX_TOKENS = 2000   # synthesis only writes 5 short wrapper sections
_DIFF_CHUNK_MAX_CHARS = 30_000  # safe per-call diff budget (AMD proxy gateway timeout risk)


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
        f"IMPORTANT: Only comment on files and lines that literally appear in the diff "
        f"above. Never invent, assume, or infer the existence of other files, functions, "
        f"or content you have not seen. If the diff above notes that files were omitted "
        f"or truncated, say so explicitly in your review instead of describing content "
        f"from those files. If the diff is empty or unavailable, say that plainly rather "
        f"than fabricating a review.\n\n"
        f"Write a thorough code review in markdown covering correctness, style, tests, "
        f"and architecture. Use severity markers: ❌ BLOCKING, ⚠️ IMPORTANT, "
        f"💡 SUGGESTION, 📋 FUTURE WORK.\n\n"
        f"Sections: Problem, Summary of changes, Overall Assessment (APPROVED / "
        f"CHANGES REQUESTED / REJECTED), Detailed Review, Recommendations, Conclusion."
    )


_CITED_FILE_RE = re.compile(
    r"`([\w./-]+\.(?:py|sh|ps1|yml|yaml|cmake|txt|md|json|toml|ini|cfg"
    r"|c|cc|cpp|h|hpp|j2))`"
)
_DIFF_FILE_RE = re.compile(r"^diff --git a/(\S+) b/\S+$", re.MULTILINE)


def _hallucinated_files(review_text: str, diff: str) -> list[str]:
    """
    File basenames the review cites (in backticks) that don't appear anywhere
    in the diff it was actually given. Catches the model inventing plausible
    files instead of restricting itself to what the diff showed — a real
    failure mode when the diff was truncated or atypically small.
    """
    real = {Path(m).name for m in _DIFF_FILE_RE.findall(diff)}
    cited = {Path(m).name for m in _CITED_FILE_RE.findall(review_text)}
    return sorted(cited - real)


def _hallucination_banner(hallucinated: list[str], scope: str) -> str:
    cited_list = ", ".join(f"`{f}`" for f in hallucinated)
    return (
        f"> ⚠️ **Auto-review integrity warning**: {scope} references file(s) not "
        f"found in the diff it was given: {cited_list}. Treat any findings about "
        f"these file(s) as unverified — likely hallucinated rather than based on "
        f"the actual PR content.\n\n"
    )


def _write_review_file(pr: PR, text: str) -> Path:
    review_dir = REVIEWS_DIR / date.today().isoformat()
    review_dir.mkdir(parents=True, exist_ok=True)
    repo_short = pr.repo.split("/")[-1]
    review_file = review_dir / f"pr_{repo_short}_{pr.number}.md"
    review_file.write_text(text, encoding="utf-8")
    return review_file


def _review_pr_single_call(pr: PR, guidelines: str, model: str, log_fn=print) -> bool:
    """The whole diff fits in one call: today's original review flow, unchanged."""
    prompt = _pr_review_prompt(pr, guidelines)
    log_fn(f"  Reviewing {pr.repo}#{pr.number} ({len(prompt)} chars, model: {model})")

    text = _call_api(prompt, model, max_tokens=_REVIEW_MAX_TOKENS, log_fn=log_fn)
    if text is None:
        pr.review_status = "failed"
        return False

    hallucinated = _hallucinated_files(text, pr.diff or "")
    if hallucinated:
        log_fn(
            f"  WARNING: review cites file(s) not present in the diff it was given: "
            f"{', '.join(hallucinated)}"
        )
        text = _hallucination_banner(hallucinated, "this review") + text

    review_file = _write_review_file(pr, text)
    pr.review_file = _rel(review_file)
    pr.review_status = "reviewed"
    log_fn(f"  Written: {review_file}")
    return True


# ---------------------------------------------------------------------------
# Chunked PR review (diff too large for one call)
# ---------------------------------------------------------------------------

def _split_diff_into_chunks(diff: str, max_chars: int = _DIFF_CHUNK_MAX_CHARS) -> list[str]:
    """
    Split a unified diff into chunks at `diff --git` file boundaries, packing
    greedily in original order so every file lands in exactly one chunk —
    none dropped. A single file whose own diff exceeds max_chars gets its
    own oversized chunk rather than being split mid-file or dropped: hunk-
    splitting one file across two review calls risks contradictory findings
    from reviewing each half with no visibility into the other, which is a
    worse failure mode than one oversized prompt (empirically still safe —
    see _call_api's gateway-timeout retry and the ~55K-char diffs reviewed
    successfully in one call during development of this feature).
    """
    if not diff:
        return []
    if len(diff) <= max_chars:
        return [diff]

    file_starts = [m.start() for m in re.finditer(r"^diff --git ", diff, re.MULTILINE)]
    if not file_starts:
        # No recognizable file boundaries (unexpected diff format) — treat
        # as a single chunk rather than guessing a split point.
        return [diff]

    file_bounds = file_starts + [len(diff)]
    segments = [diff[file_starts[i] : file_bounds[i + 1]] for i in range(len(file_starts))]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for segment in segments:
        if current and current_len + len(segment) > max_chars:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(segment)
        current_len += len(segment)
    if current:
        chunks.append("".join(current))
    return chunks


@dataclass
class _ChunkResult:
    index: int        # 1-based
    total: int
    files: list[str]
    diff: str
    text: str | None  # None if this chunk's API call failed


def _chunk_review_prompt(pr: PR, guidelines: str, chunk_diff: str, index: int, total: int) -> str:
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
        f"This PR's diff was too large to review in one pass and was split into "
        f"{total} parts by file. This is PART {index} of {total}.\n\n"
        f"## Unified diff — part {index} of {total}\n\n```diff\n{chunk_diff}\n```\n\n"
        f"---\n\n"
        f"IMPORTANT: Only comment on files and lines that literally appear in the diff "
        f"above. Never invent, assume, or infer the existence of other files, functions, "
        f"or content — including content you'd expect in the OTHER parts of this PR "
        f"that you have not seen here. Do not speculate about what other parts contain.\n\n"
        f"First, in 2-4 sentences under a `#### What this part changes` heading, "
        f"explain what these files change and how they relate to each other — "
        f"e.g. if one file's change only makes sense in light of another change "
        f"in this SAME part's diff, say so explicitly. Use this to understand the "
        f"change as a whole before judging any individual line, so you don't flag "
        f"something as broken when a nearby hunk in this same diff already explains "
        f"or completes it.\n\n"
        f"Then write detailed findings, grouped by file under a `### <filename>` "
        f"heading, using severity markers: ❌ BLOCKING, ⚠️ IMPORTANT, 💡 SUGGESTION, "
        f"📋 FUTURE WORK. Cover correctness, style, tests, and architecture for the "
        f"files in this part only.\n\n"
        f"Do NOT include a Problem, Summary, Overall Assessment, or Conclusion "
        f"section — those are written separately once every part has been reviewed. "
        f"Just the short explanation above, then the per-file detailed findings."
    )


def _synthesis_prompt(pr: PR, guidelines: str, chunk_texts: list[str]) -> str:
    parts_text = "\n\n".join(
        f"### Part {i + 1} findings\n\n{text}" for i, text in enumerate(chunk_texts)
    )
    return (
        f"You are finishing a code review for a ROCm build infrastructure PR. Its "
        f"diff was too large for one review call, so it was split into "
        f"{len(chunk_texts)} part(s), each already reviewed in detail below. Your "
        f"only job is to write the wrapper sections from those findings — do not "
        f"re-review the code or invent any new findings not already present below.\n\n"
        f"## Review guidelines (excerpt)\n\n{guidelines[:_GUIDELINES_LIMIT]}\n\n"
        f"---\n\n"
        f"## PR details\n\n"
        f"URL: {pr.url}\n"
        f"Repository: {pr.repo}\n"
        f"PR #{pr.number}: {pr.title}\n"
        f"Author: {pr.author}\n"
        f"Branch: {pr.head_ref} → {pr.base_ref}\n\n"
        f"## Findings already written for each part\n\n{parts_text}\n\n"
        f"---\n\n"
        f"Based strictly on the findings above, write markdown with exactly these "
        f"5 sections, each starting with a `## ` heading spelled exactly as shown: "
        f"`## Problem`, `## Summary of changes`, `## Overall Assessment` (APPROVED / "
        f"CHANGES REQUESTED / REJECTED), `## Recommendations`, `## Conclusion`. If "
        f"ANY finding above is marked ❌ BLOCKING, Overall Assessment must not be "
        f"APPROVED. Do not repeat the detailed per-file findings themselves — those "
        f"are included separately in the final document."
    )


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(text: str) -> dict[str, str]:
    """Split markdown into {heading title: body} by level-2 `## ` headings."""
    matches = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[m.group(1).strip()] = text[start:end].strip()
    return sections


def _deterministic_file_list(diff: str) -> str:
    files = _DIFF_FILE_RE.findall(diff)
    return "\n".join(f"- `{f}`" for f in files) if files else "(no files found)"


def _assemble_chunked_review(
    pr: PR,
    chunk_results: list[_ChunkResult],
    synthesis_text: str | None,
    synthesis_hallucinated: list[str],
    full_diff: str,
) -> str:
    total = len(chunk_results)
    failed = [c for c in chunk_results if c.text is None]
    any_failed = bool(failed)
    any_blocking = any(c.text and "❌" in c.text for c in chunk_results)

    sections = _split_sections(synthesis_text) if synthesis_text else {}
    synthesis_degraded = synthesis_text is not None and not sections

    problem = sections.get("Problem") or (
        f"(Synthesis unavailable — this PR's diff ({len(full_diff)} chars) was "
        f"split into {total} part(s); see Detailed Review below for per-file "
        f"findings.)"
    )
    summary = sections.get("Summary of changes") or (
        "Files changed:\n\n" + _deterministic_file_list(full_diff)
    )
    recommendations = sections.get("Recommendations") or (
        "See ❌ BLOCKING / ⚠️ IMPORTANT items in the per-part findings below."
    )
    conclusion = sections.get("Conclusion") or (
        "(Synthesis unavailable — review the per-part findings below directly.)"
    )

    if any_failed:
        assessment = (
            "⚠️ **INCOMPLETE — partial review** (see banner above; the missing "
            "part(s) will be retried automatically on the next run)"
        )
    else:
        assessment = sections.get("Overall Assessment") or (
            ("⚠️ **CHANGES REQUESTED** (auto-fallback: synthesis unavailable, but "
             "❌ BLOCKING items were found below)")
            if any_blocking else
            "⚠️ **NEEDS MANUAL REVIEW** (auto-fallback: synthesis unavailable)"
        )

    lines = [
        f"# PR Review: {pr.repo}#{pr.number} — {pr.title}",
        "",
        f"**Author:** {pr.author}  **Branch:** {pr.head_ref} → {pr.base_ref}  "
        f"**URL:** {pr.url}",
        "",
    ]

    if any_failed:
        failed_files = sorted({f for c in failed for f in c.files})
        lines += [
            f"> ⚠️ **PARTIAL REVIEW** — {len(failed)} of {total} part(s) failed to "
            f"review (API error): {', '.join(failed_files) or '(unknown files)'}. "
            f"This will be retried automatically on the next run.",
            "",
        ]
    if synthesis_hallucinated:
        lines += [_hallucination_banner(synthesis_hallucinated, "the synthesized summary"), ""]
    if synthesis_text is None:
        lines += [
            "> _(Synthesis call failed — wrapper sections below are a mechanical "
            "fallback; see Detailed Review for full findings.)_",
            "",
        ]
    elif synthesis_degraded:
        lines += [
            "> _(Synthesis call returned unexpected formatting — wrapper sections "
            "below are a mechanical fallback; see Detailed Review for full "
            "findings.)_",
            "",
        ]

    lines += [
        "## Problem", "", problem, "",
        "## Summary of changes", "", summary, "",
        "## Overall Assessment", "", assessment, "",
        "## Detailed Review", "",
    ]

    for c in chunk_results:
        file_list = ", ".join(c.files) if c.files else "(unknown files)"
        lines.append(f"### Part {c.index} of {c.total} — files: {file_list}")
        lines.append("")
        if c.text is None:
            lines.append(
                f"❌ **This part failed to review** (API error) — {file_list} was "
                f"not reviewed. This will be retried automatically on the next run."
            )
        else:
            lines.append(c.text.strip())
        lines += ["", "---", ""]

    lines += [
        "## Recommendations", "", recommendations, "",
        "## Conclusion", "", conclusion, "",
        "---", "",
        f"*This review was generated in {total} part(s) because the diff "
        f"({len(full_diff)} chars across {len(_DIFF_FILE_RE.findall(full_diff))} "
        f"files) exceeded the {_DIFF_CHUNK_MAX_CHARS}-char per-call budget.*",
    ]
    return "\n".join(lines)


def _review_pr_chunked(
    pr: PR, guidelines: str, model: str, chunks: list[str], log_fn=print
) -> bool:
    total = len(chunks)
    log_fn(
        f"  Diff too large for one call ({len(pr.diff or '')} chars) — "
        f"splitting into {total} part(s)"
    )

    chunk_results: list[_ChunkResult] = []
    for i, chunk_diff in enumerate(chunks, start=1):
        files = _DIFF_FILE_RE.findall(chunk_diff)
        prompt = _chunk_review_prompt(pr, guidelines, chunk_diff, i, total)
        log_fn(f"  Reviewing part {i}/{total} ({len(prompt)} chars, {len(files)} file(s))")
        text = _call_api(prompt, model, max_tokens=_REVIEW_MAX_TOKENS, log_fn=log_fn)
        if text is not None:
            hallucinated = _hallucinated_files(text, chunk_diff)
            if hallucinated:
                log_fn(
                    f"  WARNING: part {i} cites file(s) not present in its diff: "
                    f"{', '.join(hallucinated)}"
                )
                text = _hallucination_banner(hallucinated, f"this part ({i}/{total})") + text
        else:
            log_fn(f"  Part {i}/{total} failed, will retry on next run")
        chunk_results.append(
            _ChunkResult(index=i, total=total, files=files, diff=chunk_diff, text=text)
        )

    if all(c.text is None for c in chunk_results):
        pr.review_status = "failed"
        return False

    succeeded_texts = [c.text for c in chunk_results if c.text is not None]
    log_fn(f"  Synthesizing final review from {len(succeeded_texts)}/{total} part(s)")
    synthesis_prompt = _synthesis_prompt(pr, guidelines, succeeded_texts)
    synthesis_text = _call_api(
        synthesis_prompt, model, max_tokens=_SYNTHESIS_MAX_TOKENS, log_fn=log_fn
    )
    synthesis_hallucinated: list[str] = []
    if synthesis_text is not None:
        synthesis_hallucinated = _hallucinated_files(synthesis_text, pr.diff or "")
        if synthesis_hallucinated:
            log_fn(
                f"  WARNING: synthesis cites file(s) not present in the diff: "
                f"{', '.join(synthesis_hallucinated)}"
            )
    else:
        log_fn("  Synthesis call failed, using mechanical fallback for wrapper sections")

    text = _assemble_chunked_review(
        pr, chunk_results, synthesis_text, synthesis_hallucinated, pr.diff or ""
    )
    review_file = _write_review_file(pr, text)
    pr.review_file = _rel(review_file)

    if any(c.text is None for c in chunk_results):
        pr.review_status = "partial"
        log_fn(f"  Written (partial — will retry): {review_file}")
        return False

    pr.review_status = "reviewed"
    log_fn(f"  Written: {review_file}")
    return True


def review_pr(pr: PR, log_fn=print) -> bool:
    """
    Generate AI review for a PR and write to reviews/<date>/pr_<repo>_<number>.md.
    Sets pr.review_file and pr.review_status in-place ("reviewed" / "failed" /
    "partial" — the latter when the diff needed multiple parts and at least
    one part's API call failed; poll.py retries on any non-True return).

    Diffs that fit the per-call budget use one API call (_review_pr_single_call).
    Larger diffs are split at file boundaries into multiple parts, each
    reviewed separately and then synthesized into one coherent review
    (_review_pr_chunked), so no file is ever dropped from review.
    """
    acfg = anthropic_config()
    model = acfg["model_review"]

    guidelines_path = Path(__file__).parent.parent.parent / "reviews" / "REVIEW_GUIDELINES.md"
    guidelines = guidelines_path.read_text(encoding="utf-8") if guidelines_path.exists() else ""

    chunks = _split_diff_into_chunks(pr.diff or "")
    if len(chunks) <= 1:
        return _review_pr_single_call(pr, guidelines, model, log_fn)
    return _review_pr_chunked(pr, guidelines, model, chunks, log_fn)


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
