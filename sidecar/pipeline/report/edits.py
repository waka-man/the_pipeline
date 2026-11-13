"""Reading the figures back out of an edited report.

The scorecard is the source of truth for a grade: the report is rendered
from it, and the Canvas payload is built from it. That is the right way
round — prose is not a score, and a grade should not depend on how someone
phrased a sentence.

But a person editing a report is making a decision. When they change a
figure in the rubric breakdown, that figure is the grade they mean to give,
and a publish that sends the agent's number instead of theirs is not what
they asked for. So the figures are read back out of the text they wrote and
written into the scorecard, which is what publishing uses.

Only the figures are read. The prose around them is not interpreted, and a
report whose figures cannot be read leaves the scorecard exactly as it was.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import AssignmentSpec, Scorecard

# "### 2. Correct extraction of required data types (7 / 9 pts)"
#
# The title is matched greedily so that a criterion whose own name contains
# brackets ("Correctness (of the regex)") still leaves the figures to the
# last pair on the line, which is the one the renderer wrote.
_HEADING = re.compile(r"^\s{0,3}#{2,4}\s*(\d+)\.\s*(.*?)\s*\(([^()]*)\)\s*$")

# "| Criterion | 7 / 9 |" and "| **Total** | **21 / 25** |"
_ROW = re.compile(r"^\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")

# "7 / 9", "-0.5 / 4"
_FIGURE = re.compile(r"(-?\d+(?:\.\d+)?)\s*/\s*(-?\d+(?:\.\d+)?)")

# Markdown emphasis around a cell: the renderer writes "**21 / 25**".
_EMPHASIS = re.compile(r"[*_]+")


@dataclass(frozen=True)
class Figure:
    """One score as it appears in a report."""

    earned: float
    possible: float


@dataclass(frozen=True)
class ScoreChange:
    """A criterion whose score an edit changed."""

    criterion: str
    ordinal: int
    title: str
    was: float
    now: float


def _figure(text: str) -> Figure | None:
    """The figures in a cell, or None when there are none.

    A cell that says "not scored" or "—" has no figures in it, and neither
    does one that says "7 / 9 pts" wrongly. Only a pair of numbers counts.
    """
    m = _FIGURE.search(text or "")
    if not m:
        return None
    return Figure(earned=float(m.group(1)), possible=float(m.group(2)))


def _plain(text: str) -> str:
    """A cell without the markdown around it."""
    return _EMPHASIS.sub("", text or "").replace("\\|", "|").strip()


def parse_breakdown(markdown: str) -> dict[int, Figure]:
    """Criterion ordinal -> figure, from the rubric breakdown headings.

    A criterion that was not scored has no figures in its heading, so it is
    simply absent: an edit cannot invent a score for it.
    """
    out: dict[int, Figure] = {}
    for line in (markdown or "").splitlines():
        m = _HEADING.match(line)
        if not m:
            continue
        figure = _figure(m.group(3))
        if figure is None:
            continue
        out[int(m.group(1))] = figure
    return out


def parse_summary(markdown: str) -> dict[str, Figure]:
    """Criterion title -> figure, from the summary table."""
    out: dict[str, Figure] = {}
    for line in (markdown or "").splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        title = _plain(m.group(1))
        if not title or title.lower() == "total":
            continue
        figure = _figure(_plain(m.group(2)))
        if figure is None:
            continue
        out[title.casefold()] = figure
    return out


def parse_total(markdown: str) -> Figure | None:
    """The total the summary table claims."""
    for line in (markdown or "").splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        if _plain(m.group(1)).lower() != "total":
            continue
        return _figure(_plain(m.group(2)))
    return None


def score_changes(scorecard: Scorecard, spec: AssignmentSpec,
                  markdown: str) -> list[ScoreChange]:
    """The criterion scores an edited report asks for.

    Both places a score can be written are read, the rubric breakdown and
    the summary table, and the one that changed is the edit. When both
    changed and they disagree, the breakdown wins: it is the part a person
    reads to see what a student was given.
    """
    breakdown = parse_breakdown(markdown)
    summary = parse_summary(markdown)
    by_id = spec.criterion_by_id
    titles = {c.title.casefold(): c for c in spec.criteria}

    changes: list[ScoreChange] = []
    for score in scorecard.criterion_scores:
        criterion = by_id.get(score.criterion)
        if criterion is None:
            continue
        current = score.points_earned
        candidates: list[float] = []
        head = breakdown.get(criterion.ordinal)
        if head is not None:
            candidates.append(head.earned)
        row = summary.get(criterion.title.casefold())
        if row is not None:
            candidates.append(row.earned)
        if not candidates:
            continue

        changed = [v for v in candidates if abs(v - current) > 1e-9]
        if not changed:
            continue
        wanted = changed[0] if len(changed) == 1 else (
            head.earned if head is not None else candidates[0])
        if abs(wanted - current) <= 1e-9:
            continue
        changes.append(ScoreChange(criterion=criterion.canvas_criterion_id,
                                    ordinal=criterion.ordinal,
                                    title=criterion.title,
                                    was=current, now=wanted))
    return changes


def apply_report_edits(scorecard: Scorecard, spec: AssignmentSpec,
                       markdown: str) -> tuple[Scorecard, list[ScoreChange]]:
    """The scorecard as the edited report asks for it.

    The scorecard is copied, so the agent's verdict is left alone: the edit
    is a decision on top of it, not a replacement of the record.
    """
    changes = score_changes(scorecard, spec, markdown)
    if not changes:
        return scorecard, []
    edited = scorecard.model_copy(deep=True)
    for change in changes:
        score = edited.score_for(change.criterion)
        if score is not None:
            score.points_earned = change.now
    return edited, changes
