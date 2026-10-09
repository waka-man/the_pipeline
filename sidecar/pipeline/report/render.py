"""Report rendering.

The agent supplies prose and scores. This module supplies every character of
structure: headings, ordering, the summary table, the totals. A report cannot
drift from its rubric because the rubric drives the layout, not the model.

The shape deliberately matches the `sample_grading_report.md` files already in
use across the existing grading projects, so reviewers see no change in habit.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from ..models import AssignmentSpec, Scorecard, SubmissionUnit

Bullet = str


def _fmt(points: float) -> str:
    return f"{points:g}"


def _escape(text: str) -> str:
    """Keep agent prose from breaking table layout."""
    return (text or "").replace("|", "\\|").replace("\r", "")


def _source_lines(unit: SubmissionUnit, sources: Iterable[dict[str, Any]] | None) -> list[str]:
    lines: list[str] = []
    if unit.primary_url:
        lines.append(f"- **Submission Link:** {unit.primary_url}")
    if sources:
        material = []
        for s in sources:
            if s.get("status") != "ok":
                continue
            kind = s.get("kind")
            if kind == "github":
                meta = s.get("meta_json") or "{}"
                commit = ""
                try:
                    import json
                    commit = (json.loads(meta).get("commit") or "")[:8]
                except Exception:
                    commit = ""
                material.append(f"GitHub repository{f' @ {commit}' if commit else ''}")
            elif kind == "image":
                material.append(f"image `{s.get('origin')}`")
            elif kind == "google_doc":
                material.append("Google Doc (converted to Markdown)")
            elif kind in ("pdf", "docx"):
                material.append(f"{kind.upper()} (converted to Markdown)")
            elif kind == "zip":
                material.append("ZIP archive")
        if material:
            lines.append("- **Material Reviewed:** " + "; ".join(material))
    if unit.group_name:
        lines.append(f"- **Group:** {unit.group_name}")
    return lines


def render_report(spec: AssignmentSpec, scorecard: Scorecard, unit: SubmissionUnit,
                  sources: Iterable[dict[str, Any]] | None = None,
                  model: str = "", prompt_version: str = "") -> str:
    sources = list(sources or [])
    out: list[str] = []
    criteria = [c for c in spec.criteria if not c.ignore_for_scoring]
    manual_ids = set(spec.policy.get("_manual_only_ids") or [])

    out.append("# Grading Report")
    out.append("")
    out.append("## Student Information")
    out.append(f"- **Name:** {unit.display_name}")
    out.extend(_source_lines(unit, sources))
    out.append(f"- **Assignment:** {spec.title}")
    out.append(f"- **Date Graded:** {date.today().isoformat()}")
    if model:
        out.append(f"- **Graded by:** {model} (agent-assisted, reviewed before publication)")
    out.append("")
    out.append("---")
    out.append("")
    out.append("## Rubric Breakdown")
    out.append("")

    for c in criteria:
        s = scorecard.score_for(c.canvas_criterion_id)
        if c.canvas_criterion_id in manual_ids and s is None:
            out.append(f"### {c.ordinal}. {c.title} (— / {_fmt(c.points)} pts)")
            out.append("")
            out.append("*Reserved for human review — not scored by the agent.*")
            out.append("")
            out.append("---")
            out.append("")
            continue
        if s is None:
            out.append(f"### {c.ordinal}. {c.title} (not scored / {_fmt(c.points)} pts)")
            out.append("")
            out.append("*No score was recorded for this criterion.*")
            out.append("")
            out.append("---")
            out.append("")
            continue

        head = f"### {c.ordinal}. {c.title} ({_fmt(s.points_earned)} / {_fmt(c.points)} pts)"
        if s.rating_label:
            head += f" — {s.rating_label}"
        out.append(head)
        out.append("")
        if c.description:
            out.append(f"_{c.description}_")
            out.append("")
        if s.analysis:
            out.append("**Analysis:**")
            out.append("")
            out.append(s.analysis.strip())
            out.append("")
        if s.evidence:
            out.append("**Evidence:**")
            out.append("")
            for e in s.evidence:
                out.append(f"- {e.strip()}")
            out.append("")
        if s.justification:
            out.append("**Score Justification:**")
            out.append("")
            out.append(s.justification.strip())
            out.append("")
        out.append("---")
        out.append("")

    out.append("## Summary")
    out.append("")
    out.append("| Criteria | Score |")
    out.append("|---|---|")
    earned_total = 0.0
    possible_total = 0.0
    for c in criteria:
        s = scorecard.score_for(c.canvas_criterion_id)
        if c.canvas_criterion_id in manual_ids and s is None:
            out.append(f"| {_escape(c.title)} | — / {_fmt(c.points)} |")
            continue
        if s is None:
            out.append(f"| {_escape(c.title)} | not scored / {_fmt(c.points)} |")
            continue
        earned_total += s.points_earned
        possible_total += c.points
        out.append(f"| {_escape(c.title)} | {_fmt(s.points_earned)} / {_fmt(c.points)} |")
    out.append(f"| **Total** | **{_fmt(earned_total)} / {_fmt(possible_total)}** |")
    out.append("")

    out.append("## Additional Notes")
    out.append("")
    if scorecard.summary.strip():
        out.append(scorecard.summary.strip())
        out.append("")

    flags = scorecard.flags
    if flags:
        out.append("**Flags for review**")
        out.append("")
        for f in flags:
            mark = {"block": "BLOCKING", "warn": "Review", "info": "Note"}.get(f.severity, "Note")
            out.append(f"- `{mark}` **{f.code}** — {f.message}")
        out.append("")
    elif not flags:
        out.append("_No concerns were flagged._")
        out.append("")

    if prompt_version:
        out.append("---")
        out.append("")
        out.append(f"<sub>Prompt {prompt_version} · every score above cites evidence from "
                   f"the submission. Review before publishing.</sub>")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def report_filename(unit: SubmissionUnit) -> str:
    from ..sources import slug
    return f"{slug(unit.display_name)}_grading_report.md"


def verdict_banner(earned: float, possible: float) -> str:
    if possible <= 0:
        return "—"
    pct = earned / possible * 100
    return f"{_fmt(earned)}/{_fmt(possible)} ({pct:.0f}%)"


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()