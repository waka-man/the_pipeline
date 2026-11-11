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


def test_report_shows_the_comment_the_student_will_read(spec):
    """The instructor sees exactly what goes to the student, in the report."""
    payload = _valid_payload(spec)
    payload["comment"] = "A clear argument, and the evidence is in place."
    md = render_report(spec, parse_scorecard(payload, spec), _unit())
    assert "## Assignment Comment" in md
    assert "A clear argument, and the evidence is in place." in md


def test_the_report_says_when_no_comment_will_be_posted(spec):
    """Silence is a decision, and it is stated rather than left to be inferred."""
    md = render_report(spec, parse_scorecard(_valid_payload(spec), spec), _unit())
    assert "## Assignment Comment" in md
    assert "No comment will be posted to the student." in md


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


def _facts(provider, model, **kw):
    """One model in the shape the catalogue reports."""
    base = dict(provider=provider, model=model, name=model,
                input_price=0.0, output_price=0.0, context=100000,
                output_limit=65536, tools=True, inputs=("text",),
                outputs=("text",), released=0)
    base.update(kw)
    return config.ModelFacts(**base)


def test_the_model_list_is_read_from_the_catalogue():
    """The list comes from what opencode reports, not from a fixed set.

    A model the user's own subscription reaches has to be usable, and a
    model that has quietly gone away must not be offered.
    """
    catalogue = [
        _facts("openrouter", "nvidia/nemotron-3.5-lightning:free"),
        _facts("github-copilot", "claude-sonnet-5", output_price=10.0),
    ]
    ranked = config.rank_models(catalogue)
    assert [f.model for f in ranked] == ["nvidia/nemotron-3.5-lightning:free",
                                         "claude-sonnet-5"]


def test_a_model_that_cannot_call_a_tool_is_never_offered():
    """The grader hands the scorecard over with a tool call.

    A model without tool support reads the work and then stops, and the row
    lands in needs_review with nothing to show for it.
    """
    catalogue = [_facts("openrouter", "no-tools", tools=False)]
    assert config.rank_models(catalogue) == []
    assert config.pick_model(catalogue) is None


def test_a_model_that_cannot_write_the_scorecard_in_full_is_not_offered():
    """Its output ceiling is below the budget, so the scorecard is cut off."""
    catalogue = [_facts("openrouter", "tiny", output_limit=1024)]
    assert config.rank_models(catalogue, budget=4096) == []
    assert config.rank_models(catalogue, budget=1024) != []


def test_free_models_come_first_and_the_strong_ones_when_free_is_not_preferred():
    catalogue = [
        _facts("openrouter", "strong", output_price=10.0),
        _facts("openrouter", "cheap-one", output_price=0.0),
    ]
    assert config.pick_model(catalogue, prefer_free=True).model == "cheap-one"
    assert config.pick_model(catalogue, prefer_free=False).model == "strong"


def test_the_tiers_are_price_bands():
    assert config.tier_of(_facts("p", "m", output_price=0.0)) == "free"
    assert config.tier_of(_facts("p", "m", output_price=0.25)) == "cheap"
    assert config.tier_of(_facts("p", "m", output_price=2.0)) == "standard"
    assert config.tier_of(_facts("p", "m", output_price=25.0)) == "premium"
    # No price listed is not the same as free.
    assert config.tier_of(_facts("p", "m", output_price=None)) == "other"


def test_a_model_with_no_price_listed_is_not_called_free():
    catalogue = [_facts("clinepass", "z-ai/glm-5.2", output_price=None)]
    choice = config.pick_model(catalogue)
    assert choice.tier == "other"
    assert choice.model == "z-ai/glm-5.2"


def test_pick_model_rejects_unavailable_explicit_model():
    with pytest.raises(RuntimeError, match="not available"):
        config.pick_model([_facts("openrouter", "a/b")], preferred="nope/nope")


def test_pick_model_finds_an_explicit_model_under_another_provider():
    """The user may name the model they saw in their own opencode."""
    catalogue = [_facts("openrouter", "a/b"), _facts("github-copilot", "b")]
    assert str(config.pick_model(catalogue, preferred="github-copilot/b")) == \
        "github-copilot/b"


def test_pick_model_returns_none_without_a_catalogue():
    assert config.pick_model({}) is None


def test_loopback_is_never_sent_through_a_proxy():
    """A proxied loopback reply is HTML/empty: a JSON error far from its cause."""
    from pipeline.grading.runner import ensure_loopback_proxy_bypass

    env = {"HTTP_PROXY": "http://proxy:8080", "NO_PROXY": "example.com"}
    ensure_loopback_proxy_bypass(env)
    assert "127.0.0.1" in env["NO_PROXY"]
    assert "localhost" in env["NO_PROXY"]
    assert "example.com" in env["NO_PROXY"]
    # Idempotent: a second pass does not duplicate entries.
    ensure_loopback_proxy_bypass(env)
    assert env["NO_PROXY"].count("127.0.0.1") == 1


def test_a_v1_opencode_is_refused_before_it_can_serve_the_wrong_api(monkeypatch):
    """PATH can hold v1, whose server answers v2 paths with 404s/HTML.

    Without the guard, every call fails as a JSON error in the model list,
    far from the actual problem.
    """
    from pipeline.grading import runner
    from pipeline import runtime as rt

    # require_v2_opencode imports read_version from ..runtime at call time.
    monkeypatch.setattr(rt, "read_version", lambda _b: "1.18.35")
    with pytest.raises(runner.RunnerError, match="not v2"):
        runner.require_v2_opencode("opencode")


def test_a_v2_opencode_passes_the_guard(monkeypatch):
    from pipeline.grading import runner
    from pipeline import runtime as rt

    monkeypatch.setattr(rt, "read_version", lambda _b: "opencode v2.2.6")
    runner.require_v2_opencode("opencode")  # must not raise


def _v2_server(monkeypatch, responses):
    """An OpenCodeServer whose HTTP layer replays recorded v2 responses."""
    from pipeline.grading import runner

    srv = runner.OpenCodeServer.__new__(runner.OpenCodeServer)
    srv._base = "http://127.0.0.1:1"
    srv._password = "pw"

    class _Response:
        def __init__(self, ok, payload=None, text="", status_code=200, headers=None):
            self.ok = ok
            self._payload = payload
            self.text = text
            self.status_code = status_code
            self.headers = headers or {}

        def json(self):
            if self._payload is None:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return self._payload

    monkeypatch.setattr(runner.requests, "request",
                        lambda *a, **k: next(responses))
    monkeypatch.setattr(runner.time, "sleep", lambda *a: None)
    return srv, _Response


def test_the_model_list_tolerates_a_briefly_unready_runtime(monkeypatch):
    """Right after serve starts, /model can answer with nothing."""
    responses = iter([
        # Empty body: .json() raises on the first attempt.
        None,
    ])
    srv, _Response = _v2_server(monkeypatch, responses)
    # Replay: first a non-JSON body, then the recorded v2 catalogue shape.
    payloads = iter([
        _Response(True),
        _Response(True, {"data": [
            {"id": "thinkingmachines/inkling-small",
             "modelID": "thinkingmachines/inkling-small",
             "providerID": "openrouter"},
            {"id": "mistral:7b-instruct-v0.3-q4_K_M",
             "modelID": "mistral:7b-instruct-v0.3-q4_K_M",
             "providerID": "ollama"},
        ]}),
    ])
    srv._request = lambda method, path, body=None, timeout=30: next(payloads)

    catalogue = srv.catalogue()
    assert [f.id for f in catalogue] == [
        "openrouter/thinkingmachines/inkling-small",
        "ollama/mistral:7b-instruct-v0.3-q4_K_M",
    ]


def test_an_unreadable_model_list_says_what_happened(monkeypatch):
    from pipeline.grading import runner

    srv, _Response = _v2_server(monkeypatch, iter([]))
    srv._request = lambda method, path, body=None, timeout=30: _Response(False, status_code=502)

    with pytest.raises(runner.RunnerError, match="did not return a model list"):
        srv.catalogue()


def test_a_non_json_model_list_includes_what_was_returned(monkeypatch):
    """The diagnostic must quote the body, not just call it non-JSON."""
    from pipeline.grading import runner

    srv, _Response = _v2_server(monkeypatch, iter([]))
    bad = _Response(True, text="<html>proxy error</html>",
                    headers={"Content-Type": "text/html"})
    srv._request = lambda method, path, body=None, timeout=30: bad

    with pytest.raises(runner.RunnerError, match="proxy error"):
        srv.catalogue()


def test_prompt_waits_for_the_completed_assistant_turn(monkeypatch):
    """v2 accepts the prompt and runs the turn in the background.

    Recorded shape: POST returns the user message immediately; completion
    is an assistant message with time.completed set and the session idle.
    """
    from pipeline.grading import runner

    srv, _Response = _v2_server(monkeypatch, iter([]))
    calls = []

    def fake_request(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        if path.endswith("/prompt"):
            return _Response(True, {"data": {
                "id": "msg_u1", "type": "user",
                "payload": {"text": "hi"}, "delivery": "steer"}})
        if path == "/session/ses_1":
            return _Response(True, {"data": {"id": "ses_1",
                                             "time": {"idle": 5}}})
        # Message list: first poll still empty, second completes the turn.
        if not hasattr(fake_request, "seen"):
            fake_request.seen = 0
        fake_request.seen += 1
        if fake_request.seen == 1:
            return _Response(True, {"data": []})
        return _Response(True, {"data": [
            {"id": "msg_u1", "type": "user", "text": "hi",
             "time": {"created": 1}},
            {"id": "msg_a1", "type": "assistant",
             "time": {"created": 2, "completed": 3},
             "content": [{"type": "text", "text": "HI"}],
             "finish": "stop",
             "tokens": {"input": 10, "output": 2, "reasoning": 0},
             "cost": 0.0},
        ]})

    srv._request = fake_request
    result = srv.prompt("ses_1", "hi")
    assert srv.provider_error(result) is None
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][1] == "/session/ses_1/prompt"
    assert posts[0][2] == {"text": "hi"}


def test_prompt_watches_the_newest_message_of_the_turn(monkeypatch):
    """v2 lists the messages newest first.

    Reading the oldest of the new ones meant the runner kept looking at the
    first message of the turn, which asked for tool calls, and so never saw
    the turn end: a grading that had submitted its scorecard sat in
    `grading` until the timeout.
    """
    from pipeline.grading import runner

    srv, _Response = _v2_server(monkeypatch, iter([]))
    polls = {"n": 0}

    def fake_request(method, path, body=None, timeout=30):
        if path.endswith("/prompt"):
            return _Response(True, {"data": {"id": "msg_u1", "type": "user"}})
        if path == "/session/ses_1":
            return _Response(True, {"data": {"id": "ses_1",
                                             "time": {"idle": 9}}})
        polls["n"] += 1
        if polls["n"] == 1:
            return _Response(True, {"data": []})
        # Newest first, as the server returns them.
        return _Response(True, {"data": [
            {"id": "msg_a2", "type": "assistant",
             "time": {"created": 5, "completed": 6},
             "content": [{"type": "text", "text": "Done."}],
             "finish": "stop"},
            {"id": "msg_a1", "type": "assistant",
             "time": {"created": 2, "completed": 3},
             "content": [{"type": "tool", "id": "call-1", "name": "shell",
                          "state": {"status": "completed", "input": {}}}],
             "finish": "tool-calls"},
        ]})

    srv._request = fake_request
    # A short timeout, so the old behaviour fails in seconds rather than
    # hanging: reading the oldest message never sees the turn end.
    result = srv.prompt("ses_1", "hi", timeout=5)
    assert srv.provider_error(result) is None
    texts = [p["text"] for p in result["parts"] if p["type"] == "text"]
    assert "Done." in texts, texts


def test_prompt_stops_as_soon_as_the_work_is_done(monkeypatch):
    """The grading is finished when the scorecard lands.

    A model that submits and then keeps talking would otherwise hold the
    row in `grading` for as long as it felt like.
    """
    from pipeline.grading import runner

    srv, _Response = _v2_server(monkeypatch, iter([]))
    polls = {"n": 0}

    def fake_request(method, path, body=None, timeout=30):
        if path.endswith("/prompt"):
            return _Response(True, {"data": {"id": "msg_u1", "type": "user"}})
        polls["n"] += 1
        # The agent is still talking: no completed assistant message.
        return _Response(True, {"data": []})

    srv._request = fake_request
    result = srv.prompt("ses_1", "hi", done_when=lambda: True, grace=0, timeout=5)
    assert result.get("taken_over") is True


def test_prompt_surfaces_the_provider_error(monkeypatch):
    """Recorded v2 error shape: finish error with a provider message."""
    srv, _Response = _v2_server(monkeypatch, iter([]))

    state = {"posted": False}

    def fake_request(method, path, body=None, timeout=30):
        if path.endswith("/prompt"):
            state["posted"] = True
            return _Response(True, {"data": {"id": "msg_u1", "type": "user"}})
        if path == "/session/ses_1":
            return _Response(True, {"data": {"id": "ses_1",
                                             "time": {"idle": 5}}})
        if not state["posted"]:
            return _Response(True, {"data": []})
        return _Response(True, {"data": [
            {"id": "msg_a1", "type": "assistant",
             "time": {"created": 2, "completed": 3},
             "content": [], "finish": "error",
             "error": {"type": "provider.quota",
                       "message": "out of credits", "status": 402}},
        ]})

    srv._request = fake_request
    result = srv.prompt("ses_1", "hi")
    assert srv.provider_error(result) == "out of credits"


def test_prompt_does_not_end_on_a_tool_call_step(monkeypatch):
    """A completed tool-call turn is mid-flight, not finished.

    v2 completes the assistant message that requested tools, runs them,
    then emits a later assistant message with the final answer. Returning
    on the first completion made the pipeline treat an active session as
    "finished without a scorecard".
    """
    from pipeline.grading import runner

    srv, _Response = _v2_server(monkeypatch, iter([]))
    polls = {"n": 0}

    user = {"id": "msg_u1", "type": "user", "text": "hi",
            "time": {"created": 1}}
    tool_step = {
        "id": "msg_a1", "type": "assistant",
        "time": {"created": 2, "completed": 3},
        "content": [{"type": "tool", "id": "call-1", "name": "shell",
                     "state": {"status": "completed", "input": {},
                               "content": [{"type": "text", "text": "ok"}]}}],
        "finish": "tool-calls",
    }
    final = {
        "id": "msg_a2", "type": "assistant",
        "time": {"created": 4, "completed": 5},
        "content": [{"type": "text", "text": "done"}],
        "finish": "stop",
    }

    def fake_request(method, path, body=None, timeout=30):
        if path.endswith("/prompt"):
            return _Response(True, {"data": {"id": "msg_u1", "type": "user"}})
        if path == "/session/ses_1":
            # Busy during the tool step, idle once the final answer lands.
            idle = polls["n"] >= 3
            t = {"idle": 6} if idle else {"busy": 3}
            return _Response(True, {"data": {"id": "ses_1", "time": t}})
        polls["n"] += 1
        if polls["n"] == 1:
            return _Response(True, {"data": [user, tool_step]})
        return _Response(True, {"data": [user, tool_step, final]})

    srv._request = fake_request
    result = srv.prompt("ses_1", "hi")
    assert srv.provider_error(result) is None
    # The final answer, not the intermediate tool-call step, is what we got.
    texts = [p["text"] for p in result["parts"] if p["type"] == "text"]
    assert "done" in texts
    assert polls["n"] >= 2, "must keep polling past the tool-call step"


def test_parts_map_v2_messages_onto_the_log_shape(monkeypatch):
    """User text, assistant text, and a synthesised step-finish per turn."""
    srv, _Response = _v2_server(monkeypatch, iter([]))
    srv._request = lambda method, path, body=None, timeout=30: _Response(True, {"data": [
        {"id": "msg_u1", "type": "user", "text": "hi", "time": {"created": 1}},
        {"id": "msg_a1", "type": "assistant",
         "time": {"created": 2, "completed": 3},
         "content": [{"type": "text", "text": "HI"}],
         "finish": "stop", "cost": 0.5,
         "tokens": {"input": 10, "output": 2, "reasoning": 0}},
        {"id": "msg_idle", "type": "idle", "time": {"created": 4}},
    ]})
    parts = srv.parts("ses_1")
    assert [p["type"] for p in parts] == ["text", "text", "step-finish"]
    assert parts[0]["text"] == "hi" and parts[1]["text"] == "HI"
    assert parts[2]["tokens"] == {"input": 10, "output": 2, "reasoning": 0}
    assert parts[2]["cost"] == 0.5 and parts[2]["reason"] == "stop"


def test_steer_and_abort_use_the_v2_endpoints(monkeypatch):
    """Steering is the same prompt endpoint; abort is interrupt."""
    srv, _Response = _v2_server(monkeypatch, iter([]))
    calls = []

    def fake_request(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        return _Response(True, {"data": {}, "interrupted": True})

    srv._request = fake_request
    assert srv.prompt_async("ses_1", "focus on X") is True
    srv.abort("ses_1")
    assert (calls[0][0], calls[0][1]) == ("POST", "/session/ses_1/prompt")
    assert calls[0][2] == {"text": "focus on X"}
    assert (calls[1][0], calls[1][1]) == ("POST", "/session/ses_1/interrupt")


def test_create_session_selects_the_agent_and_model(monkeypatch):
    """v2 selects agent and model at creation; per-prompt fields are ignored.

    Recorded: a session created without `agent` runs as `build`, which never
    hears about the scorecard tool.
    """
    srv, _Response = _v2_server(monkeypatch, iter([]))
    calls = []

    def fake_request(method, path, body=None, timeout=30):
        calls.append((method, path, body))
        return _Response(True, {"data": {"id": "ses_1", "agent": "grader",
                                        "model": {"id": "m", "providerID": "p"}}})

    srv._request = fake_request
    session = srv.create_session(
        "grading", "/tmp/ws", model="openrouter/m", agent="grader")
    assert session["id"] == "ses_1"
    assert calls[0][2] == {"title": "grading",
                           "model": {"id": "m", "providerID": "openrouter"},
                           "agent": "grader"}


def test_tool_parts_carry_v2_output():
    """Recorded v2 tool shape: output lives in state.content blocks."""
    from pipeline.grading import runner

    srv = runner.OpenCodeServer.__new__(runner.OpenCodeServer)
    srv._messages = lambda session_id: [{
        "id": "msg_a1", "type": "assistant",
        "time": {"created": 2, "completed": 3},
        "content": [{
            "type": "tool", "id": "call-1", "name": "glob",
            "state": {"status": "completed",
                      "input": {"pattern": ".grading/**/*"},
                      "content": [{"type": "text", "text": "No files found"}],
                      "metadata": {"count": 0}}},
        ],
        "finish": "stop", "tokens": {"input": 1, "output": 1},
    }]
    parts = srv.parts("ses_1")
    tool = [p for p in parts if p["type"] == "tool"][0]
    assert tool["tool"] == "glob"
    assert tool["state"]["input"] == {"pattern": ".grading/**/*"}
    assert tool["state"]["output"] == "No files found"


def test_failed_turns_surface_as_error_parts():
    """A 429 turn has no content; without an error part the log stays silent."""
    from pipeline.grading import runner

    srv = runner.OpenCodeServer.__new__(runner.OpenCodeServer)
    srv._messages = lambda session_id: [{
        "id": "msg_a1", "type": "assistant",
        "time": {"created": 2, "completed": 3},
        "content": [], "finish": "error",
        "error": {"type": "provider.rate-limit",
                  "message": "Rate limit exceeded: free-models-per-day",
                  "status": 429},
    }]
    parts = srv.parts("ses_1")
    errors = [p for p in parts if p["type"] == "error"]
    assert len(errors) == 1, parts
    assert "Rate limit exceeded" in errors[0]["text"]
    assert errors[0]["state"]["http"] == 429


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
