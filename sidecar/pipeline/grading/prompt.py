"""Prompt construction.

The prompt is generated from the spec, so it stays in step with the rubric
automatically. Every lesson encoded here came from a real failure mode in the
hand-written TASK.md files this replaces.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..models import AssignmentSpec, Criterion, SubmissionUnit

PROMPT_VERSION = "v1"

# opencode prefixes MCP tools with the server name, so the identifier the model
# sees is not the bare tool name. Importing it here keeps prompt text and the
# registered server from drifting apart.
from .mcp_server import EXPOSED_TOOL_NAME as SCORECARD_TOOL

SYSTEM_PREAMBLE = """\
You are grading one student submission for a university assignment. A human \
instructor will read every score you produce before anything is published, so \
your job is to be accurate and legible, not fast.

Ground rules, in order of importance:

1. Score only what the submission actually contains. Never infer work you \
cannot see, and never award credit for something described but not present.
2. Every score must cite evidence from the submission — a specific regex \
pattern, a line of input, a quoted README sentence, a value in an output \
file. Evidence you could show a student.
3. You are grading this submission in isolation. You have not seen any other \
student's work and must not reason comparatively ("best I've seen", \
"compared to others"). Judge against the rubric only.
4. Quote code exactly as it appears. Do not paraphrase a regex, paraphrase it \
in your own words, or "fix" it silently.
5. Partial credit is expected. Most criteria award points on a continuum.
   A criterion worth 9 is not 9 or 0.
6. When you are genuinely uncertain, still record a score, set confidence to \
"low", and raise a flag. Do not silently guess.
7. When the submission genuinely lacks something a criterion asks for, score \
it accordingly and say so plainly in the analysis.

You finish by calling the `{tool}` tool exactly once with your scores. \
Nothing is recorded until you call it."""


def _criterion_block(c: Criterion, manual: bool = False) -> str:
    lines = [f"### {c.ordinal}. {c.title}  (id `{c.canvas_criterion_id}`, max {c.points:g} points)"]
    if c.description:
        lines.append(c.description)
    if manual:
        lines.append("**Reserved for human review. Do not score this criterion.**")
        return "\n".join(lines)
    if c.scale == "ratings":
        tiers = ", ".join(f"{r.label} ({r.points:g})" for r in c.ratings)
        lines.append(f"Rating tiers: {tiers}. Set `rating_label` to the tier you choose "
                     f"and `points_earned` to its point value.")
    else:
        tiers = {r.label.lower() for r in c.ratings}
        if tiers and tiers <= {"full marks", "no marks"}:
            lines.append(f"This criterion offers only Full ({c.points:g}) and No (0) marks in "
                         f"Canvas, but partial credit is expected. Record the numeric score in "
                         f"`points_earned`.")
    lines.append("Evidence should be: " + (c.description[:120] or "specific observations"))
    return "\n".join(lines)


def length_guidance(budget: int, criteria_count: int) -> str:
    """Tell the agent how much room it has, derived from the real token budget.

    A scorecard that overruns the output budget is truncated mid-JSON and
    scores nothing, so the prose limits below are a correctness requirement,
    not a style preference.
    """
    per = max(120, budget // max(criteria_count, 1) - 80)
    if per >= 420:
        return ("Write at normal length: up to 4 sentences of analysis and 2 of "
                "justification per criterion.")
    if per >= 260:
        return (f"Be concise. Per criterion write at most 3 sentences of analysis, "
                f"1 sentence of justification, and at most 3 pieces of evidence of "
                f"about {per} characters each.")
    return (f"The output budget is tight: about {per} characters per criterion in "
            f"total. Per criterion give one short paragraph of analysis, one sentence "
            f"of justification, and at most 2 short evidence quotes. Do not pad. "
            f"Completing every criterion matters far more than elaborating any one.")


def build_system_prompt(spec: AssignmentSpec,
                        manual_ids: list[str] | None = None,
                        tool: str = SCORECARD_TOOL,
                        budget: int = 8192) -> str:
    manual = set(manual_ids or [])
    parts = [SYSTEM_PREAMBLE.replace("{tool}", tool), "", "# Assignment", spec.title, ""]

    scored_all = [c for c in spec.criteria if not c.ignore_for_scoring]
    parts += ["## Length budget", "",
              length_guidance(budget, max(len(scored_all), 1)), ""]

    if spec.instructions_markdown.strip():
        parts += ["## What students were asked to do", "",
                  spec.instructions_markdown.strip(), ""]

    scored = scored_all
    parts += ["# Rubric", "",
              f"This assignment is worth {spec.points_possible:g} points across "
              f"{len(scored)} criteria. Score every criterion except any marked "
              f"reserved for human review.", ""]
    for c in scored:
        parts += [_criterion_block(c, manual=c.canvas_criterion_id in manual), ""]

    policy = spec.policy or {}
    extras: list[str] = []
    if policy.get("zero_conditions"):
        extras.append("Score **0 for the entire assignment** if any of these hold:\n"
                      + "\n".join(f"- {c}" for c in policy["zero_conditions"]))
    if policy.get("evidence_hints"):
        extras.append("Where to look for evidence:\n"
                      + "\n".join(f"- **{k}**: {v}" for k, v in policy["evidence_hints"].items()))
    if policy.get("tiebreak"):
        extras.append(f"When a score sits between two levels, {policy['tiebreak']}")
    if extras:
        parts += ["# Additional grading policy", "", *extras, ""]

    return "\n".join(parts).rstrip() + "\n"


def _describe_sources(sources: Iterable[dict[str, Any]]) -> str:
    rows: list[str] = []
    for s in sources:
        if s.get("status") != "ok":
            rows.append(f"- `{s.get('origin')}` — **unavailable** ({s.get('error') or 'not collected'})")
            continue
        kind = s.get("kind")
        path = s.get("path")
        md = s.get("markdown_path")
        if kind == "github":
            meta = {}
            try:
                meta = json.loads(s.get("meta_json") or "{}")
            except Exception:
                pass
            commit = (meta.get("commit") or "")[:8]
            rows.append(f"- Repository cloned to `{path}`"
                        + (f" (HEAD {commit})" if commit else ""))
        elif kind == "image":
            rows.append(f"- Screenshot: `{path}` — **open this image and look at it**")
        elif md:
            rows.append(f"- Converted to Markdown: `{md}` (from {kind.upper()})")
        elif path:
            rows.append(f"- File: `{path}` ({kind})")
    return "\n".join(rows) if rows else "- No material was collected."


def build_task_prompt(spec: AssignmentSpec, unit: SubmissionUnit,
                      sources: Iterable[dict[str, Any]],
                      workspace: str,
                      manual_ids: list[str] | None = None,
                      test_results: dict[str, Any] | None = None,
                      anchors: list[dict[str, Any]] | None = None,
                      tool: str = SCORECARD_TOOL,
                      budget: int = 8192) -> str:
    manual = set(manual_ids or [])
    parts = [f"# Submission to grade: {unit.display_name}", ""]

    if unit.primary_url:
        parts += [f"Submitted link: {unit.primary_url}", ""]
    if unit.submitted_at:
        parts += [f"Submitted at: {unit.submitted_at}", ""]

    if not unit.is_submitted:
        parts += ["## No submission", "",
                  "Canvas reports this student has not submitted anything. "
                  "Flag this and score nothing further.", ""]
        return "\n".join(parts)

    parts += ["## Material you have", "", _describe_sources(sources), ""]
    parts += ["## Length budget", "",
              length_guidance(budget, len([c for c in spec.criteria
                                           if not c.ignore_for_scoring])), ""]

    parts += [
        "## How to work", "",
        f"Everything is under `{workspace}`. Read the source files directly — "
        "use grep and glob rather than guessing at filenames. If the repository "
        "contains a README or sample output, read those too: they are part of the "
        "evidence.", "",
        "Check each criterion against the actual code and the actual output. If a "
        "program claims to produce output but no output file exists, that absence "
        "is itself evidence and belongs in your analysis.", "",
    ]

    if test_results:
        parts += ["## Automated test results", "",
                  "```json", json.dumps(test_results, indent=2)[:4000], "```", "",
                  "Treat these as evidence, not as the verdict. A failing test can "
                  "be a real defect or a false negative from a framework assumption.", ""]

    if anchors:
        parts += ["## Calibration anchors", "",
                  "These previously-graded submissions were approved by the instructor "
                  "and define what each score level means here. Match their standard.", ""]
        for a in anchors:
            parts += [f"### {a.get('label')} — {a.get('earned')}/{a.get('possible')}",
                      a.get("note", ""), ""]

    if manual:
        titles = [c.title for c in spec.criteria if c.canvas_criterion_id in manual]
        parts += ["## Reserved for human review", "",
                  "Do not score these: " + "; ".join(titles) +
                  ". A human will complete them.", ""]

    parts += ["## Finish", "",
              f"Work through the criteria one at a time, then call `{tool}` once with "
              "all of your scores, your overall summary, and any flags."]
    return "\n".join(parts).rstrip() + "\n"


def retry_prompt(issues: list[str], tool: str = SCORECARD_TOOL) -> str:
    """Follow-up sent when a scorecard fails validation."""
    return (
        "Your scorecard was rejected. The following problems must be fixed before "
        "it can be recorded:\n\n"
        + "\n".join(f"- {i}" for i in issues)
        + f"\n\nRe-read the submission where needed, then call `{tool}` "
          "again with a corrected scorecard. Every criterion must be present, every "
          "score within bounds, and every criterion must carry at least one piece of "
          "specific evidence."
    )