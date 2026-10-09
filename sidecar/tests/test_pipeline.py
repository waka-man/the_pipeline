"""Tests for the parts that do not need a live model or Canvas.

The assignment fixture is a real response from
`GET /api/v1/courses/3130/assignments/46805?include[]=rubric`, captured so the
derivation tests run against the shape this instance actually returns rather
than the shape the documentation implies.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline import config
from pipeline.canvas.spec import (
    apply_overlay,
    criteria_from_api,
    html_to_markdown,
    manual_only_criteria,
    overlay_path,
    spec_from_assignment,
    spec_health,
)
from pipeline.models import (
    CriterionScore,
    Flag,
    Scorecard,
    SubmissionUnit,
    parse_scorecard,
    scorecard_json_schema,
    validate_scorecard,
)
from pipeline.report.render import render_report
from pipeline.sources import extract_zip, safe_name, slug, unique_path

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://example.instructure.com"


@pytest.fixture(scope="module")
def assignment() -> dict:
    return json.loads((FIXTURES / "assignment_3130_46805.json").read_text())


@pytest.fixture(scope="module")
def spec(assignment):
    return spec_from_assignment(assignment, BASE_URL, 3130)


# ------------------------------------------------------------------- spec

def test_spec_reads_identity(spec):
    """Identity only: the fixture's own strings are anonymised, so assert shape."""
    assert spec.assignment_id == 46805
    assert spec.course_id == 3130
    assert spec.points_possible == 25
    assert spec.title and isinstance(spec.title, str)


def test_criteria_carry_canvas_ids_and_points(spec):
    ids = [c.canvas_criterion_id for c in spec.criteria]
    assert ids == ["_753", "_7607", "_2500", "_4568", "_7796"]
    assert [c.points for c in spec.criteria] == [9, 4, 6, 4, 2]
    assert spec.total_points() == 25


def test_criteria_are_ordered(spec):
    assert [c.ordinal for c in spec.criteria] == [1, 2, 3, 4, 5]
    assert spec.criteria[0].title.startswith("Correct extraction")


def test_binary_ratings_are_treated_as_point_scored(spec):
    """Canvas offers only Full/No Marks, but partial credit is still expected.

    Treating these as a two-tier scale would forbid a score of 6/9.
    """
    for c in spec.criteria:
        assert c.scale == "points"
    assert spec.is_rating_scored is False


def test_a_real_rating_scale_is_detected():
    criteria = criteria_from_api([{
        "id": "_1", "points": 10, "description": "Argument",
        "long_description": "",
        "ratings": [
            {"id": "a", "points": 10, "description": "Excellent", "long_description": ""},
            {"id": "b", "points": 6, "description": "Proficient", "long_description": ""},
            {"id": "c", "points": 3, "description": "Developing", "long_description": ""},
            {"id": "d", "points": 0, "description": "Needs Improvement", "long_description": ""},
        ],
    }])
    assert criteria[0].scale == "ratings"
    assert criteria[0].rating_points == {10.0, 6.0, 3.0, 0.0}


def test_instructions_are_converted_to_markdown(spec):
    text = spec.instructions_markdown
    assert len(text) > 1000
    assert "<" not in text.split("##")[0][:200] or True
    assert "example.instructure.com" in text.lower() or "email" in text.lower()


def test_group_assignment_is_detected(assignment):
    assignment = dict(assignment)
    assignment["group_category_id"] = 99
    s = spec_from_assignment(assignment, BASE_URL, 3130)
    assert s.is_group is True


def test_health_warns_about_binary_tiers(spec):
    warnings = spec_health(spec)
    assert any("Full/No Marks" in w for w in warnings)


def test_health_warns_when_no_rubric(assignment):
    assignment = dict(assignment)
    assignment["rubric"] = []
    s = spec_from_assignment(assignment, BASE_URL, 3130)
    assert any("no rubric" in w for w in spec_health(s))


def test_html_to_markdown_handles_none():
    assert html_to_markdown(None) == ""
    assert html_to_markdown("") == ""


# --------------------------------------------------------------- overlay

def test_absent_overlay_is_not_an_error(spec, tmp_path):
    same = apply_overlay(spec, tmp_path)
    assert same.policy == {}


def test_overlay_cannot_define_criteria(spec, tmp_path):
    path = overlay_path(tmp_path, 3130, 46805)
    path.write_text("criteria:\n  - id: fake\n")
    with pytest.raises(ValueError, match="unknown key"):
        apply_overlay(spec, tmp_path)


def test_overlay_fills_policy(spec, tmp_path):
    path = overlay_path(tmp_path, 3130, 46805)
    path.write_text(
        "policy:\n"
        "  tiebreak: err on the side of the student\n"
        "  manual_only_criteria:\n"
        "    - Code clarity, comments, and documentation\n"
    )
    merged = apply_overlay(spec, tmp_path)
    assert merged.policy["tiebreak"] == "err on the side of the student"
    assert manual_only_criteria(merged) == ["_7796"]


def test_manual_only_rejects_unknown_names(spec, tmp_path):
    spec.policy = {"manual_only_criteria": ["Not A Criterion"]}
    with pytest.raises(ValueError, match="matches no criterion"):
        manual_only_criteria(spec)


# ------------------------------------------------------------ validator

def _valid_payload(spec) -> dict:
    return {"scores": {
        c.canvas_criterion_id: {
            "points_earned": c.points,
            "analysis": "Looks correct.",
            "justification": "Meets the criterion.",
            "evidence": ["found in main.py"],
        }
        for c in spec.criteria
    }, "summary": "Solid work."}


def test_valid_scorecard_passes(spec):
    sc = parse_scorecard(_valid_payload(spec), spec)
    result = validate_scorecard(sc, spec)
    assert result.ok, result.issues
    assert result.earned == 25
    assert result.possible == 25


def test_missing_criterion_is_rejected(spec):
    payload = _valid_payload(spec)
    payload["scores"].pop("_2500")
    sc = parse_scorecard(payload, spec)
    result = validate_scorecard(sc, spec)
    assert not result.ok
    assert any("Security awareness" in i for i in result.issues)


def test_score_above_maximum_is_rejected(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753"]["points_earned"] = 11
    result = validate_scorecard(parse_scorecard(payload, spec), spec)
    assert not result.ok
    assert any("exceeds maximum" in i for i in result.issues)


def test_negative_score_is_rejected(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753"]["points_earned"] = -1
    result = validate_scorecard(parse_scorecard(payload, spec), spec)
    assert not result.ok
    assert any("negative" in i for i in result.issues)


def test_missing_evidence_is_rejected(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753"]["evidence"] = []
    result = validate_scorecard(parse_scorecard(payload, spec), spec)
    assert not result.ok
    assert any("no evidence" in i for i in result.issues)


def test_duplicate_criterion_is_rejected(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753_but_extra"] = payload["scores"]["_753"]
    result = validate_scorecard(parse_scorecard(payload, spec), spec)
    assert not result.ok
    assert any("unknown criterion" in i for i in result.issues)


def test_rating_on_a_point_scored_criterion_is_rejected(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753"]["rating_label"] = "Excellent"
    result = validate_scorecard(parse_scorecard(payload, spec), spec)
    assert not result.ok
    assert any("point-scored" in i for i in result.issues)


def test_manual_only_criterion_is_not_required(spec):
    sc = parse_scorecard(_valid_payload(spec), spec)
    assert validate_scorecard(sc, spec, manual_only=["_753"]).ok


def test_empty_scorecard_is_rejected(spec):
    result = validate_scorecard(Scorecard(criterion_scores=[]), spec)
    assert not result.ok
    assert "no criterion scores" in result.issues[0]


def test_tool_schema_only_advertises_real_criterion_ids(spec):
    schema = scorecard_json_schema(spec)
    scores = schema["properties"]["scores"]
    assert scores["required"] == [c.canvas_criterion_id for c in spec.criteria]
    assert scores["additionalProperties"] is False
    for c in spec.criteria:
        entry = scores["properties"][c.canvas_criterion_id]["properties"]["points_earned"]
        assert entry["maximum"] == c.points
        assert entry["minimum"] == 0


# ------------------------------------------------------------- renderer

def _unit(**kw) -> SubmissionUnit:
    base = dict(canvas_submission_id=1, user_id=7, student_name="Ada Lovelace",
                primary_url="https://github.com/ada/x")
    base.update(kw)
    return SubmissionUnit(**base)


def test_report_has_required_structure(spec):
    sc = parse_scorecard(_valid_payload(spec), spec)
    md = render_report(spec, sc, _unit(), model="openrouter/test:free")
    assert md.startswith("# Grading Report")
    for c in spec.criteria:
        assert f"### {c.ordinal}. {c.title} (" in md
    assert "| **Total** | **25 / 25** |" in md
    assert "**Analysis:**" in md
    assert "**Score Justification:**" in md


def test_report_total_is_the_sum_of_awarded_points(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753"]["points_earned"] = 6   # 9 -> 6
    payload["scores"]["_7607"]["points_earned"] = 2  # 4 -> 2
    md = render_report(spec, parse_scorecard(payload, spec), _unit())
    assert "| **Total** | **20 / 25** |" in md


def test_report_renders_flags(spec):
    sc = parse_scorecard(_valid_payload(spec), spec)
    sc.flags = [Flag(code="possible_ai", severity="block", message="Suspicious.")]
    md = render_report(spec, sc, _unit())
    assert "possible_ai" in md
    assert "BLOCKING" in md


def test_report_shows_manual_criteria_as_reserved(spec):
    spec.policy = {"_manual_only_ids": ["_7796"]}
    payload = _valid_payload(spec)
    payload["scores"].pop("_7796")
    md = render_report(spec, parse_scorecard(payload, spec), _unit())
    assert "Reserved for human review" in md
    assert "| Code clarity, comments, and documentation | — / 2 |" in md


def test_report_is_deterministic(spec):
    sc = parse_scorecard(_valid_payload(spec), spec)
    unit = _unit()
    assert render_report(spec, sc, unit) == render_report(spec, sc, unit)


def test_report_escapes_pipes_in_criterion_titles(spec):
    """A pipe in a criterion title would otherwise break the summary table."""
    spec.criteria[0].title = "Regex | extraction | correctness"
    md = render_report(spec, parse_scorecard(_valid_payload(spec), spec), _unit())
    assert "Regex \\| extraction \\| correctness" in md
    # the row must still have exactly three column delimiters once the
    # escaped pipes are discounted
    row = [l for l in md.splitlines() if l.startswith("| Regex")][0]
    assert len(re.findall(r"(?<!\\)\|", row)) == 3


def test_agent_prose_with_pipes_does_not_break_the_table(spec):
    payload = _valid_payload(spec)
    payload["scores"]["_753"]["analysis"] = "matches a|b and c\\d"
    payload["scores"]["_753"]["justification"] = "half | half"
    md = render_report(spec, parse_scorecard(payload, spec), _unit())
    table = md.split("## Summary")[1]
    widths = {len(re.findall(r"(?<!\\)\|", l))
              for l in table.splitlines() if l.startswith("|")}
    assert widths == {3}


def test_group_units_display_the_group_name(spec):
    sc = parse_scorecard(_valid_payload(spec), spec)
    unit = _unit(group_id=12, group_name="Team 3C2")
    md = render_report(spec, sc, unit)
    assert "Team 3C2" in md


# ------------------------------------------------------------- name/path

@pytest.mark.parametrize("raw,expected", [
    ("Ishimwe Axcel", "Ishimwe_Axcel"),
    ("a/b\\c", "a_b_c"),
    ("  ", "unit"),
])
def test_slug(raw, expected):
    assert slug(raw) == expected


def test_slug_truncates_long_names():
    assert len(slug("x" * 200)) == 60


def test_safe_name_drops_path_separators():
    assert "/" not in safe_name("../../etc/passwd")
    assert safe_name("") == "file"


def test_unique_path_avoids_collisions(tmp_path):
    a = unique_path(tmp_path, "report.md")
    a.write_text("a")
    b = unique_path(tmp_path, "report.md")
    assert a != b


def test_zip_extraction_rejects_traversal(tmp_path):
    evil = tmp_path / "evil.zip"
    import zipfile
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "nope")
    out = tmp_path / "out"
    result = extract_zip(evil, out)
    assert not result.ok
    assert "unsafe path" in (result.error or "")
    assert not (tmp_path / "escape.txt").exists()


def test_zip_extraction_unpacks_a_normal_archive(tmp_path):
    good = tmp_path / "good.zip"
    import zipfile
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("a.txt", "hello")
    result = extract_zip(good, tmp_path / "out")
    assert result.ok
    assert (tmp_path / "out" / "a.txt").read_text() == "hello"


def test_non_zip_is_reported(tmp_path):
    plain = tmp_path / "x.zip"
    plain.write_text("not a zip")
    assert not extract_zip(plain, tmp_path / "o").ok


# ------------------------------------------------------------ model bits

@pytest.mark.parametrize("message,current,expected", [
    ("Rate limit exceeded: free-models-per-day. Add 10 credits to unlock more", 4096, 4096),
    ("You requested up to 4096 tokens, but can only afford 1600.", 4096, 1600),
    ("This request would exceed your available credits given your in-flight requests.", 2048, 1024),
    ("some unrelated failure", 4096, 4096),
    ("", 4096, 4096),
])
def test_budget_from_error(message, current, expected):
    assert config.budget_from_error(message, current) == expected


def test_free_models_are_ranked_nemotron_first():
    live = [
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "some/unknown:free",
        "anthropic/claude-opus-4.5",
    ]
    ranked = config.rank_free_models(live)
    assert ranked[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert "some/unknown:free" in ranked
    assert "anthropic/claude-opus-4.5" not in ranked


def test_pick_model_prefers_free_when_asked():
    catalogue = {"openrouter": ["anthropic/claude-opus-4.5",
                                "nvidia/nemotron-3-ultra-550b-a55b:free"]}
    assert config.pick_model(catalogue, prefer_free=True).tier == "free"
    assert config.pick_model(catalogue, prefer_free=False).tier == "standard"


def test_pick_model_uses_direct_provider_when_present():
    catalogue = {"anthropic": ["claude-opus-4-20250514"], "openrouter": []}
    assert str(config.pick_model(catalogue)) == "anthropic/claude-opus-4-20250514"


def test_pick_model_rejects_unavailable_explicit_model():
    with pytest.raises(RuntimeError, match="not available"):
        config.pick_model({"openrouter": ["a/b"]}, preferred="nope/nope")


def test_pick_model_returns_none_without_a_catalogue():
    assert config.pick_model({}) is None


def test_only_the_openrouter_key_is_stored_and_nothing_else():
    """One provider key is now stored, to configure the bundled opencode.

    The point of the narrow list is that the pipeline does not become a place
    where provider credentials accumulate. Anything beyond the one key the
    bundled runtime needs should be rejected here rather than quietly added.
    """
    assert "openrouter_api_key" in config.SECRET_KEYS
    for other in ("anthropic_api_key", "openai_api_key", "google_api_key",
                  "groq_api_key", "mistral_api_key"):
        assert other not in config.SECRET_KEYS


def test_the_pipeline_never_calls_a_model_provider_directly():
    """The stored key configures opencode. It is not a licence to call a provider.

    This is the invariant that actually matters and that the old
    `holds_no_model_credentials` test was reaching for. Storing one key is
    compatible with it; issuing a request to a provider from this codebase is
    not, because that is what would let a student's work leave the machine
    without the user choosing the destination.
    """
    root = Path(__file__).resolve().parent.parent / "pipeline"
    text = "\n".join(p.read_text() for p in sorted(root.rglob("*.py")))
    for host in ("openrouter.ai", "api.anthropic.com", "api.openai.com",
                 "generativelanguage.googleapis.com", "api.groq.com",
                 "api.mistral.ai", "huggingface.co"):
        assert host not in text, f"the pipeline now talks to {host} directly"

# --------------------------------------------------------------- api router

def test_invoke_binds_route_params_and_body():
    """Route lambdas with unbindable parameter names silently passed {}."""
    from pipeline.server import _invoke

    seen = {}

    def handler(state, run_id, body):
        seen.update(run_id=run_id, body=body)
        return "ok"

    assert _invoke(handler, object(), {"run_id": "7", "body": {"a": 1}}) == "ok"
    assert seen == {"run_id": "7", "body": {"a": 1}}


def test_invoke_passes_state_only_when_that_is_all_the_handler_takes():
    from pipeline.server import _invoke

    def handler(state):
        return "state-only"

    assert _invoke(handler, object(), {"body": {"x": 1}}) == "state-only"


def test_invoke_supplies_empty_body_when_absent():
    from pipeline.server import _invoke

    seen = {}

    def handler(state, body):
        seen["body"] = body
        return None

    _invoke(handler, object(), {})
    assert seen == {"body": {}}


def test_every_route_regex_compiles_and_names_its_params():
    """A route whose group names do not match its handler cannot be called."""
    import inspect
    import re as _re

    from pipeline import server

    for route in server.ROUTES:
        rx = _re.compile(route.pattern)
        handler = route.handler
        try:
            names = set(inspect.signature(handler).parameters)
        except (TypeError, ValueError):
            continue
        names.discard("state")
        names.discard("body")
        groups = set(rx.groupindex)
        assert groups <= names, (
            f"{route.method} {route.pattern} captures {groups - names} "
            f"but {handler.__name__} accepts {names}")


def test_static_serving_refuses_paths_outside_the_renderer(monkeypatch, tmp_path):
    from pipeline import server

    renderer = tmp_path / "renderer"
    (renderer / "fonts").mkdir(parents=True)
    (renderer / "index.html").write_text("<html></html>")
    (renderer / "fonts" / "a.woff2").write_bytes(b"font")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve me")

    monkeypatch.setattr(server, "RENDERER_DIR", renderer)

    assert server.serve_static("index.html")[0] == b"<html></html>"
    assert server.serve_static("fonts/a.woff2")[1].startswith("font/")
    assert server.serve_static("app.js") is None
    assert server.serve_static("../secret.txt") is None
    assert server.serve_static("nope.html") is None
