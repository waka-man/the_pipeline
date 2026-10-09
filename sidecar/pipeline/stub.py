"""Deterministic stub grading.

Lets the whole pipeline be exercised end to end — including the renderer, the
review surface and the publish payload — without spending a token. Scores are
derived from the submission's own material so they look plausible and are
stable across runs, which makes it usable as a demo and as a UI fixture.

Never used unless explicitly requested.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .models import AssignmentSpec, Scorecard, parse_scorecard, scorecard_json_schema


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12], 16)


def _evidence_for(criterion_title: str, sources: list[dict[str, Any]],
                  unit_name: str) -> list[str]:
    """Point at something real in the collected material."""
    have = [s for s in sources if s.get("status") == "ok"]
    quotes = []
    for s in have[:3]:
        if s.get("kind") == "github" and s.get("path"):
            quotes.append(f"repository material at `{s['path']}`")
        elif s.get("kind") == "image" and s.get("path"):
            quotes.append(f"attached screenshot `{Path(s['path']).name}`")
        elif s.get("markdown_path"):
            quotes.append(f"converted document `{Path(s['markdown_path']).name}`")
    if not quotes:
        quotes.append(f"no material was collected for {unit_name}")
    return quotes[:2]


def stub_scorecard(spec: AssignmentSpec, unit_name: str,
                   sources: list[dict[str, Any]],
                   model: str = "stub/deterministic") -> Scorecard:
    """Build a valid scorecard without calling a model."""
    digest = _seed(unit_name)
    scores: dict[str, Any] = {}

    for i, c in enumerate(spec.criteria):
        if c.ignore_for_scoring:
            continue
        h = (digest >> (i * 5)) & 0x1F
        # Bias high so the demo looks like a plausible cohort rather than noise.
        fraction = 0.35 + (h % 61) / 100.0
        points = round(c.points * fraction, 1)
        if points > c.points:
            points = c.points
        entry: dict[str, Any] = {
            "points_earned": points,
            "analysis": (
                f"[stub] Simulated review of {c.title.lower()}. The collected "
                f"material was inspected across {len(sources)} artifact(s) and a "
                f"deterministic score was assigned for demonstration purposes."
            ),
            "justification": (
                f"[stub] {c.title} awarded {points:g} of {c.points:g}. This is a "
                f"fixture, not an assessment of the student's work."
            ),
            "evidence": _evidence_for(c.title, sources, unit_name),
            "confidence": "low",
        }
        if c.scale == "ratings":
            nearest = min(c.ratings, key=lambda r: abs(r.points - points),
                          default=None)
            if nearest:
                entry["rating_label"] = nearest.label
        scores[c.canvas_criterion_id] = entry

    payload = {
        "scores": scores,
        "summary": (
            "[stub] This report was produced without calling a model. It exists so "
            "the collection, rendering, review and publishing paths can be "
            "exercised end to end. Do not treat these numbers as a grade."
        ),
        "flags": [{
            "code": "stub_run",
            "severity": "block",
            "message": "Synthetic scorecard. Not a real grade.",
        }],
    }
    scorecard = parse_scorecard(payload, spec)
    scorecard.model = model
    scorecard.prompt_version = "stub"
    return scorecard


from pathlib import Path  # noqa: E402  (used by _evidence_for)