"""Zero-config spec derivation.

Everything a run needs is derived from two Canvas responses. Overlay YAML may
add policy, tests, calibration and report options; it may never redefine a
criterion, because criteria are the contract with Canvas and must carry its
own IDs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import html2text
import yaml

from ..models import AssignmentSpec, Criterion, Rating

_h = html2text.HTML2Text()
_h.ignore_links = False
_h.ignore_images = False
_h.body_width = 0
_h.single_line_break = False


def html_to_markdown(html: str | None) -> str:
    if not html:
        return ""
    text = _h.handle(html)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def criteria_from_api(rubric: list[dict[str, Any]] | None) -> list[Criterion]:
    out: list[Criterion] = []
    for i, c in enumerate(rubric or []):
        if not c.get("id"):
            continue
        ratings = [
            Rating(id=r.get("id", ""), label=(r.get("description") or "").strip(),
                   points=float(r.get("points") or 0),
                   description=(r.get("long_description") or "").strip())
            for r in (c.get("ratings") or [])
        ]
        out.append(
            Criterion(
                canvas_criterion_id=c["id"],
                ordinal=i + 1,
                title=(c.get("description") or f"Criterion {i + 1}").strip(),
                description=(c.get("long_description") or "").strip(),
                points=float(c.get("points") or 0),
                use_range=bool(c.get("criterion_use_range")),
                ignore_for_scoring=bool(c.get("ignore_for_scoring")),
                ratings=ratings,
            )
        )
    return out


def spec_from_assignment(assignment: dict[str, Any], base_url: str,
                         course_id: int) -> AssignmentSpec:
    """Build a complete spec from `GET /assignments/:id?include[]=rubric`."""
    return AssignmentSpec(
        canvas_base_url=base_url,
        course_id=course_id,
        assignment_id=int(assignment["id"]),
        title=assignment.get("name") or f"Assignment {assignment['id']}",
        instructions_markdown=html_to_markdown(assignment.get("description")),
        points_possible=float(assignment.get("points_possible") or 0),
        grading_type=assignment.get("grading_type") or "points",
        submission_types=assignment.get("submission_types") or [],
        allowed_extensions=assignment.get("allowed_extensions") or [],
        criteria=criteria_from_api(assignment.get("rubric")),
        group_category_id=assignment.get("group_category_id"),
        grade_group_students_individually=bool(assignment.get("grade_group_students_individually")),
        due_at=assignment.get("due_at"),
        needs_grading_count=int(assignment.get("needs_grading_count") or 0),
        free_form_criterion_comments=bool(assignment.get("free_form_criterion_comments")),
    )


# --------------------------------------------------------------- overlays

OVERLAY_KEYS = {"policy", "tests", "calibration", "sources", "report"}


def overlay_path(assignments_dir: Path, course_id: int, assignment_id: int) -> Path:
    return Path(assignments_dir) / f"{course_id}-{assignment_id}.yaml"


def apply_overlay(spec: AssignmentSpec, assignments_dir: Path) -> AssignmentSpec:
    """Merge an optional overlay onto a derived spec. Absent file is normal."""
    path = overlay_path(Path(assignments_dir), spec.course_id, spec.assignment_id)
    if not path.exists():
        return spec
    data = yaml.safe_load(path.read_text()) or {}
    unknown = set(data) - OVERLAY_KEYS
    if unknown:
        raise ValueError(
            f"{path}: unknown key(s) {sorted(unknown)}. Overlays may only set "
            f"{sorted(OVERLAY_KEYS)}; criteria come from Canvas."
        )
    if "policy" in data:
        spec.policy = data["policy"] or {}
    if "tests" in data:
        spec.tests = data["tests"] or {}
    if "calibration" in data:
        spec.calibration = data["calibration"] or {}
    if "sources" in data:
        spec.sources_policy = data["sources"] or {}
    if "report" in data:
        spec.report_options = data["report"] or {}
    return spec


def manual_only_criteria(spec: AssignmentSpec) -> list[str]:
    """Criterion IDs a human must own; the agent is told to leave them blank."""
    raw = spec.policy.get("manual_only_criteria") or []
    by_title = {c.title.strip().lower(): c.canvas_criterion_id for c in spec.criteria}
    ids: list[str] = []
    for item in raw:
        if item in spec.criterion_by_id:
            ids.append(item)
        elif str(item).strip().lower() in by_title:
            ids.append(by_title[str(item).strip().lower()])
        else:
            raise ValueError(
                f"manual_only_criteria: '{item}' matches no criterion id or title "
                f"(available: {[c.title for c in spec.criteria]})"
            )
    return ids


def spec_health(spec: AssignmentSpec) -> list[str]:
    """Warnings a human should see before grading starts."""
    warn: list[str] = []
    if not spec.criteria:
        warn.append("This assignment has no rubric attached. Add one in Canvas; "
                    "the pipeline derives criteria from it.")
        return warn
    total = spec.total_points()
    if total and abs(total - spec.points_possible) > 0.01:
        warn.append(f"Rubric criteria total {total:g} but the assignment is worth "
                    f"{spec.points_possible:g}. Canvas will use the assignment total.")
    free = [c.title for c in spec.criteria if c.scale == "points" and len(c.ratings) >= 2]
    if free:
        warn.append(
            "These criteria only offer Full/No Marks tiers but award partial credit. "
            "Scores will be published as points, not as a rating tier: "
            + ", ".join(free)
        )
    if not spec.instructions_markdown.strip():
        warn.append("The assignment has no description text, so the grader agent "
                    "will have no instructions to work from.")
    return warn