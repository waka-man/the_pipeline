"""Domain models.

The scorecard is the single source of truth for a grade. Markdown reports are
rendered from it; Canvas payloads are built from it. Nothing parses prose.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

RatingScale = Literal["points", "ratings"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Rating(BaseModel):
    """A Canvas rubric rating tier."""

    id: str
    label: str
    points: float
    description: str = ""


class Criterion(BaseModel):
    """One rubric criterion, bound to its Canvas criterion ID."""

    canvas_criterion_id: str
    ordinal: int
    title: str
    description: str = ""
    points: float
    use_range: bool = False
    ignore_for_scoring: bool = False
    ratings: list[Rating] = Field(default_factory=list)

    @property
    def scale(self) -> RatingScale:
        """Ratings are only a scale when they offer more than full/nothing."""
        labels = {r.label.strip().lower() for r in self.ratings}
        trivial = labels <= {"full marks", "no marks", "full", "none", "0"}
        return "points" if (len(self.ratings) < 3 or trivial) else "ratings"

    @property
    def rating_points(self) -> set[float]:
        return {r.points for r in self.ratings}


class AssignmentSpec(BaseModel):
    """Everything the pipeline needs to grade an assignment.

    Derived from the Canvas API by default. Optional overlay files may extend
    `policy`, `tests`, `calibration`, `sources` and `report` — they never
    replace criteria, which always come from Canvas.
    """

    canvas_base_url: str
    course_id: int
    assignment_id: int
    title: str
    instructions_markdown: str = ""
    points_possible: float = 0.0
    grading_type: str = "points"
    submission_types: list[str] = Field(default_factory=list)
    allowed_extensions: list[str] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    group_category_id: int | None = None
    grade_group_students_individually: bool = False
    due_at: str | None = None
    needs_grading_count: int = 0
    free_form_criterion_comments: bool = False
    # Overlay-provided
    policy: dict[str, Any] = Field(default_factory=dict)
    tests: dict[str, Any] = Field(default_factory=dict)
    calibration: dict[str, Any] = Field(default_factory=dict)
    sources_policy: dict[str, Any] = Field(default_factory=dict)
    report_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("submission_types", "allowed_extensions", mode="before")
    @classmethod
    def _none_to_list(cls, v: Any) -> Any:
        return [] if v is None else v

    @property
    def is_group(self) -> bool:
        return self.group_category_id is not None

    @property
    def criterion_by_id(self) -> dict[str, Criterion]:
        return {c.canvas_criterion_id: c for c in self.criteria}

    @property
    def is_rating_scored(self) -> bool:
        """True when at least one criterion has a genuine multi-tier scale."""
        return any(c.scale == "ratings" for c in self.criteria)

    def total_points(self) -> float:
        return sum(c.points for c in self.criteria if not c.ignore_for_scoring)


class SubmissionUnit(BaseModel):
    """One grading unit: a student, or a group when the assignment is grouped."""

    canvas_submission_id: int
    user_id: int
    student_name: str
    sortable_name: str = ""
    group_id: int | None = None
    group_name: str | None = None
    workflow_state: str = "submitted"
    submitted_at: str | None = None
    existing_score: float | None = None
    primary_url: str | None = None
    missing: bool = False

    @property
    def display_name(self) -> str:
        return self.group_name or self.student_name

    @property
    def is_submitted(self) -> bool:
        return self.workflow_state == "submitted" and not self.missing


class SourceKind(str):
    GITHUB = "github"
    IMAGE = "image"
    PDF = "pdf"
    DOCX = "docx"
    GOOGLE_DOC = "google_doc"
    ZIP = "zip"
    URL = "url"
    TEXT = "text"
    FILE = "file"


class Source(BaseModel):
    """A collected artifact, normalized for the agent to read."""

    id: int | None = None
    submission_id: int
    kind: str
    origin: str
    path: str | None = None
    markdown_path: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    status: str = "pending"
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class CriterionScore(BaseModel):
    """One criterion's score plus the prose that justifies it."""

    criterion: str
    points_earned: float
    rating_label: str | None = None
    analysis: str = ""
    justification: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"

    @field_validator("evidence")
    @classmethod
    def _strip_empty(cls, v: list[str]) -> list[str]:
        return [e.strip() for e in v if e and e.strip()]


class Flag(BaseModel):
    """Something a human should look at before the grade is trusted."""

    code: str
    severity: Literal["info", "warn", "block"] = "warn"
    message: str = ""


class Scorecard(BaseModel):
    """The agent's structured verdict for one grading unit."""

    criterion_scores: list[CriterionScore]
    summary: str = ""
    flags: list[Flag] = Field(default_factory=list)
    test_results: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    agent_session_id: str = ""
    prompt_version: str = ""
    created_at: str = Field(default_factory=utcnow)

    def score_for(self, criterion_id: str) -> CriterionScore | None:
        return next((s for s in self.criterion_scores if s.criterion == criterion_id), None)

    @property
    def total_earned(self) -> float:
        """Sum of awarded points. Possible points come from the spec, not here."""
        return sum(s.points_earned for s in self.criterion_scores)

    def blocking_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == "block"]


class ValidationResult(BaseModel):
    ok: bool
    issues: list[str] = Field(default_factory=list)
    earned: float = 0.0
    possible: float = 0.0

    @property
    def summary(self) -> str:
        return "valid" if self.ok else f"{len(self.issues)} issue(s): " + "; ".join(self.issues[:3])


def validate_scorecard(scorecard: Scorecard, spec: AssignmentSpec,
                       manual_only: list[str] | None = None) -> ValidationResult:
    """Check a scorecard against its rubric.

    Deliberately strict: a scorecard that passes this cannot produce a report
    whose arithmetic disagrees with its table, or that silently omits a
    criterion.
    """
    issues: list[str] = []
    manual = set(manual_only or [])
    scored = [c for c in spec.criteria if not c.ignore_for_scoring]
    required = [c for c in scored if c.canvas_criterion_id not in manual]

    if not scorecard.criterion_scores:
        return ValidationResult(ok=False, issues=["no criterion scores present"])

    seen: set[str] = set()
    for s in scorecard.criterion_scores:
        if s.criterion in seen:
            issues.append(f"criterion {s.criterion} scored more than once")
        seen.add(s.criterion)
        if s.criterion not in spec.criterion_by_id:
            issues.append(f"unknown criterion id {s.criterion}")

    missing = [c.canvas_criterion_id for c in required if c.canvas_criterion_id not in seen]
    if missing:
        titles = ", ".join(spec.criterion_by_id[m].title for m in missing if m in spec.criterion_by_id)
        issues.append(f"criteria not scored: {titles or ', '.join(missing)}")

    earned = 0.0
    possible = 0.0
    for c in required:
        possible += c.points
        s = scorecard.score_for(c.canvas_criterion_id)
        if s is None:
            continue
        earned += s.points_earned
        if s.points_earned < 0:
            issues.append(f"'{c.title}': negative score")
        if s.points_earned > c.points + 1e-6:
            issues.append(f"'{c.title}': {s.points_earned} exceeds maximum {c.points:g}")
        if not s.evidence:
            issues.append(f"'{c.title}': no evidence recorded")
        if c.scale == "ratings" and s.rating_label:
            labels = {r.label for r in c.ratings}
            if s.rating_label not in labels:
                issues.append(f"'{c.title}': rating '{s.rating_label}' is not a rubric tier")
        elif s.rating_label and c.scale == "points":
            issues.append(f"'{c.title}': rating given but this criterion is point-scored")

    if abs(earned - possible) > 1e-6 and possible and earned > possible + 1e-6:
        issues.append(f"earned {earned:g} exceeds possible {possible:g}")

    return ValidationResult(ok=not issues, issues=issues, earned=earned, possible=possible)


def scorecard_json_schema(spec: AssignmentSpec) -> dict[str, Any]:
    """JSON Schema for the scorecard, narrowed to this assignment's criteria.

    Emitted verbatim as the tool schema so the model is constrained to real
    criterion IDs rather than free-text names.
    """
    cprops: dict[str, Any] = {}
    required: list[str] = []
    for c in spec.criteria:
        if c.ignore_for_scoring:
            continue
        entry: dict[str, Any] = {
            "type": "number",
            "minimum": 0,
            "maximum": c.points,
            "description": f"{c.title} — max {c.points:g} points. {c.description}".strip(),
        }
        if c.scale == "ratings":
            entry = {
                "type": "object",
                "properties": {
                    "points_earned": entry,
                    "rating_label": {"type": "string", "enum": [r.label for r in c.ratings]},
                    "analysis": {"type": "string"},
                    "justification": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["points_earned", "analysis", "justification", "evidence"],
                "additionalProperties": False,
            }
        else:
            entry = {
                "type": "object",
                "properties": {
                    "points_earned": entry,
                    "analysis": {"type": "string"},
                    "justification": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["points_earned", "analysis", "justification", "evidence"],
                "additionalProperties": False,
            }
        cprops[c.canvas_criterion_id] = entry
        required.append(c.canvas_criterion_id)

    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "object",
                "properties": cprops,
                "required": required,
                "additionalProperties": False,
            },
            "summary": {
                "type": "string",
                "description": "Two to four sentences on overall performance, "
                               "including the most significant strength and weakness.",
            },
            "flags": {
                "type": "array",
                "description": "Anything a human must check before trusting this grade.",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "severity": {"type": "string", "enum": ["info", "warn", "block"]},
                        "message": {"type": "string"},
                    },
                    "required": ["code", "severity", "message"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["scores", "summary"],
        "additionalProperties": False,
    }


def parse_scorecard(payload: dict[str, Any], spec: AssignmentSpec) -> Scorecard:
    """Convert the tool's nested `scores` object into a flat Scorecard."""
    scores: list[CriterionScore] = []
    for cid, entry in (payload.get("scores") or {}).items():
        if not isinstance(entry, dict):
            continue
        scores.append(
            CriterionScore(
                criterion=cid,
                points_earned=float(entry.get("points_earned", 0) or 0),
                rating_label=entry.get("rating_label"),
                analysis=entry.get("analysis", "") or "",
                justification=entry.get("justification", "") or "",
                evidence=list(entry.get("evidence") or []),
                confidence=entry.get("confidence", "medium"),
            )
        )
    return Scorecard(
        criterion_scores=scores,
        summary=payload.get("summary", "") or "",
        flags=[Flag(**f) for f in (payload.get("flags") or []) if isinstance(f, dict)],
        test_results=payload.get("test_results") or {},
    )