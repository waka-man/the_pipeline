"""Reading the scores back out of an edited report.

A person editing a report is making a decision, and when they change a
figure in the rubric breakdown that figure is the grade they mean to give.
These pin what is read back out of the text, and what is left alone.
"""

from __future__ import annotations

import json

import pytest

from pipeline.models import AssignmentSpec, Criterion, Scorecard, CriterionScore
from pipeline.report.edits import (apply_report_edits, parse_breakdown,
                                    parse_summary, parse_total, score_changes)

from pipeline.canvas.spec import spec_from_assignment

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def spec():
    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    return spec_from_assignment(assignment, "https://x", 3130)


@pytest.fixture()
def scorecard(spec):
    scores = []
    for c in spec.criteria:
        if c.ignore_for_scoring:
            continue
        scores.append(CriterionScore(criterion=c.canvas_criterion_id,
                                     points_earned=c.points / 2))
    return Scorecard(criterion_scores=scores)


def _report(spec, scorecard, total=None) -> str:
    """A report in the shape the renderer writes."""
    lines = ["# Grading Report", "", "## Rubric Breakdown", ""]
    for c in spec.criteria:
        if c.ignore_for_scoring:
            continue
        s = scorecard.score_for(c.canvas_criterion_id)
        if s is None:
            lines.append(f"### {c.ordinal}. {c.title} (not scored / {c.points:g} pts)")
        else:
            lines.append(f"### {c.ordinal}. {c.title} "
                         f"({s.points_earned:g} / {c.points:g} pts)")
        lines.append("")
    lines += ["## Summary", "", "| Criteria | Score |", "|---|---|"]
    earned = 0.0
    possible = 0.0
    for c in spec.criteria:
        if c.ignore_for_scoring:
            continue
        s = scorecard.score_for(c.canvas_criterion_id)
        if s is None:
            continue
        earned += s.points_earned
        possible += c.points
        lines.append(f"| {c.title} | {s.points_earned:g} / {c.points:g} |")
    lines.append(f"| **Total** | **{earned:g} / {possible:g}** |")
    if total is not None:
        lines[-1] = f"| **Total** | **{total:g} / {possible:g}** |"
    return "\n".join(lines) + "\n"


def test_the_figures_of_the_breakdown_are_read(spec, scorecard):
    md = _report(spec, scorecard)
    breakdown = parse_breakdown(md)
    assert set(breakdown) == {c.ordinal for c in spec.criteria
                               if not c.ignore_for_scoring}
    first = spec.criteria[0]
    assert breakdown[first.ordinal].earned == scorecard.score_for(
        first.canvas_criterion_id).points_earned


def test_the_total_of_the_summary_table_is_read(spec, scorecard):
    total = parse_total(_report(spec, scorecard))
    assert total is not None
    assert total.earned == pytest.approx(scorecard.total_earned)


def test_an_unedited_report_asks_for_no_change(spec, scorecard):
    md = _report(spec, scorecard)
    assert score_changes(scorecard, spec, md) == []


def test_a_figure_changed_in_the_breakdown_is_the_score(spec, scorecard):
    """The whole point: what the person wrote is the grade."""
    md = _report(spec, scorecard)
    first = spec.criteria[0]
    current = scorecard.score_for(first.canvas_criterion_id).points_earned
    wanted = min(first.points, current + 1)
    edited = md.replace(f"({current:g} / {first.points:g} pts)",
                        f"({wanted:g} / {first.points:g} pts)", 1)
    assert edited != md, "the fixture did not change"

    changes = score_changes(scorecard, spec, edited)
    assert len(changes) == 1
    assert changes[0].criterion == first.canvas_criterion_id
    assert changes[0].was == pytest.approx(current)
    assert changes[0].now == pytest.approx(wanted)


def test_a_figure_changed_in_the_summary_table_is_the_score(spec, scorecard):
    """The table is the other place a score is written."""
    md = _report(spec, scorecard)
    first = spec.criteria[0]
    current = scorecard.score_for(first.canvas_criterion_id).points_earned
    wanted = min(first.points, current + 1)
    edited = md.replace(f"| {first.title} | {current:g} /",
                        f"| {first.title} | {wanted:g} /", 1)
    changes = score_changes(scorecard, spec, edited)
    assert len(changes) == 1
    assert changes[0].now == pytest.approx(wanted)


def test_the_breakdown_wins_when_the_two_disagree(spec, scorecard):
    """It is the part a person reads to see what a student was given."""
    md = _report(spec, scorecard)
    first = spec.criteria[0]
    current = scorecard.score_for(first.canvas_criterion_id).points_earned
    other = min(first.points, current + 2)
    edited = md.replace(f"({current:g} / {first.points:g} pts)",
                        f"({other:g} / {first.points:g} pts)", 1)
    edited = edited.replace(f"| {first.title} | {current:g} /",
                            f"| {first.title} | {current:g} /", 1)
    changes = score_changes(scorecard, spec, edited)
    assert len(changes) == 1
    assert changes[0].now == pytest.approx(other)


def test_the_agents_verdict_is_left_alone(spec, scorecard):
    """The edit is a decision on top of the record, not a replacement."""
    md = _report(spec, scorecard)
    first = spec.criteria[0]
    current = scorecard.score_for(first.canvas_criterion_id).points_earned
    wanted = min(first.points, current + 1)
    edited = md.replace(f"({current:g} / {first.points:g} pts)",
                        f"({wanted:g} / {first.points:g} pts)", 1)
    new_card, changes = apply_report_edits(scorecard, spec, edited)
    assert changes
    assert new_card.score_for(first.canvas_criterion_id).points_earned == wanted
    # The card that came from the agent still says what it said.
    assert scorecard.score_for(first.canvas_criterion_id).points_earned == current


def test_a_report_with_no_figures_leaves_the_score_alone(spec, scorecard):
    assert score_changes(scorecard, spec, "# Grading Report\n\nNothing here.\n") == []


def test_a_criterion_with_brackets_in_its_name_still_reads(spec):
    """A criterion may be called "Correctness (of the regex)"."""
    spec = spec.model_copy(deep=True)
    spec.criteria = [Criterion(canvas_criterion_id="c1", ordinal=1,
                                title="Correctness (of the regex)", points=10)]
    card = Scorecard(criterion_scores=[CriterionScore(criterion="c1",
                                                      points_earned=6)])
    md = ("## Rubric Breakdown\n\n"
          "### 1. Correctness (of the regex) (8 / 10 pts)\n")
    changes = score_changes(card, spec, md)
    assert len(changes) == 1
    assert changes[0].now == 8
