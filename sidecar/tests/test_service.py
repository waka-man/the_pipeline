"""Tests for the state machine, the prompt builder and the service layer.

The first suite covers rubric logic in isolation. This one covers the parts that
hold state or talk to a process, which is where a regression actually costs a
grading run:

  * `store` is the only record of what has been done, and folder existence is
    deliberately not a status signal — so its transitions are tested directly.
  * `prompt` encodes every grading rule; a silent edit changes every grade.
  * `mcp_server` is the agent's only write path, driven here over real stdio.
  * `server` handlers are exercised against a real socket, because routing bugs
    (an unbound lambda parameter) are invisible to unit-level calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pipeline.canvas.collect import units_from_submissions
from pipeline.canvas.spec import spec_from_assignment
from pipeline.grading.prompt import (
    PROMPT_VERSION,
    build_system_prompt,
    build_task_prompt,
    length_guidance,
    retry_prompt,
)
from pipeline.grading.mcp_server import EXPOSED_TOOL_NAME, MCP_SERVER_NAME, TOOL_NAME
from pipeline.models import (
    Flag,
    Scorecard,
    Scorecard as ScorecardModel,
    SubmissionUnit,
    parse_scorecard,
    scorecard_json_schema,
    validate_scorecard,
)
from pipeline.store import Store
from pipeline.stub import stub_scorecard

CODE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://example.instructure.com"


@pytest.fixture(scope="module")
def spec():
    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    return spec_from_assignment(assignment, BASE_URL, 3130)


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pipeline.db")


def unit(**kw) -> SubmissionUnit:
    base = dict(canvas_submission_id=1, user_id=7, student_name="Ada Lovelace")
    base.update(kw)
    return SubmissionUnit(**base)


# ------------------------------------------------------------------- store

def test_run_round_trips_through_sqlite(store, spec):
    run_id = store.upsert_run(spec, model="openrouter/test:free")
    run = store.get_run(run_id)
    assert run["title"] == spec.title
    assert run["model"] == "openrouter/test:free"
    assert run["spec"].criteria[0].canvas_criterion_id == spec.criteria[0].canvas_criterion_id


def test_upsert_run_is_idempotent_per_assignment(store, spec):
    first = store.upsert_run(spec)
    second = store.upsert_run(spec)
    assert first == second
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM runs").fetchone()["c"] == 1


def test_submission_insert_refresh_rather_than_duplicate(store, spec):
    run_id = store.upsert_run(spec)
    store.replace_submissions(run_id, [unit(), unit(canvas_submission_id=2, student_name="Bob")])
    assert store.progress(run_id)["total"] == 2

    # Re-collecting the same cohort must not create rows or lose status.
    store.set_submission_status(1, "collected")
    store.replace_submissions(run_id, [unit(existing_score=18.0), unit(canvas_submission_id=2,
                                                                        student_name="Bob")])
    assert store.progress(run_id)["total"] == 2
    assert store.submission_row(1)["status"] == "collected"
    assert store.submission_row(1)["existing_score"] == 18.0


def test_progress_counts_by_status(store, spec):
    run_id = store.upsert_run(spec)
    store.replace_submissions(run_id, [unit(), unit(canvas_submission_id=2, student_name="B")])
    store.set_submission_status(1, "graded")
    store.set_submission_status(2, "no_submission")
    counts = store.progress(run_id)
    assert counts["graded"] == 1
    assert counts["no_submission"] == 1
    assert counts["total"] == 2


def test_only_the_valid_scorecard_is_returned(store, spec):
    run_id = store.upsert_run(spec)
    store.replace_submissions(run_id, [unit()])
    good = stub_scorecard(spec, "Ada Lovelace", [])
    bad = stub_scorecard(spec, "Ada Lovelace", [])
    bad.criterion_scores = bad.criterion_scores[:2]

    store.save_scorecard(1, None, good, "valid", 20, 25, [])
    store.save_scorecard(1, None, bad, "invalid", 10, 25, ["incomplete"])

    best = store.best_scorecard(1)
    assert best is not None and len(best.criterion_scores) == len(spec.criteria)
    assert store.invalid_reason(1) == "incomplete"


def test_best_scorecard_prefers_the_newest_valid(store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    older = stub_scorecard(spec, "Ada Lovelace", [])
    newer = stub_scorecard(spec, "Ada Lovelace", [])
    newer.summary = "second attempt"
    store.save_scorecard(1, None, older, "valid", 10, 25, [])
    store.save_scorecard(1, None, newer, "valid", 12, 25, [])
    assert store.best_scorecard(1).summary == "second attempt"


def test_scorecard_is_none_before_grading(store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    assert store.best_scorecard(1) is None
    assert store.invalid_reason(1) is None


def test_attempts_increment_and_close(store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    a1 = store.start_attempt(1, "m")
    a2 = store.start_attempt(1, "m")
    store.finish_attempt(a1, "ok")
    store.finish_attempt(a2, "invalid", error="missing evidence")
    with store.connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT n, status, error FROM attempts ORDER BY n")]
    assert [r["n"] for r in rows] == [1, 2]
    assert rows[1]["status"] == "invalid"
    assert rows[1]["error"] == "missing evidence"


def test_sources_are_recorded_with_their_outcome(store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    sid = store.add_source(1, "github", "https://github.com/a/b",
                           path="/w/repo", status="ok")
    store.set_source_status(sid, "error", "clone failed")
    rows = store.sources_for(1)
    assert rows[0]["kind"] == "github"
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "clone failed"


def test_reports_and_publications_are_recorded(store, spec, tmp_path):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    path = tmp_path / "r.md"
    path.write_text("# report")
    store.save_report(1, str(path))
    assert store.report_for(1)["path"] == str(path)
    assert store.already_published(1) is False

    store.record_publication(1, {"_753": {"points": 6}})
    assert store.already_published(1) is True


def test_events_stream_in_order(store, spec):
    run_id = store.upsert_run(spec)
    for n in range(5):
        store.log(run_id, "collect", f"event {n}", level="warn")
    events = store.events_since(run_id)
    assert [e["message"] for e in events] == [f"event {n}" for n in range(5)]
    assert all(e["level"] == "warn" for e in events)
    assert len(store.events_since(run_id, after_id=events[2]["id"])) == 2


def test_missing_run_raises_clearly(store):
    with pytest.raises(KeyError):
        store.get_run(999)


def test_unknown_submission_row_raises_clearly(store, spec):
    with pytest.raises(KeyError):
        store.submission_row(42)


# ---------------------------------------------------- submission units

def test_individual_submissions_become_units():
    raw = [
        {"id": 10, "user_id": 1, "user": {"name": "Ada", "sortable_name": "Lovelace, Ada"},
         "workflow_state": "submitted", "submitted_at": "2026-09-06T19:54:08Z",
         "score": None, "url": "https://github.com/a/b", "missing": False,
         "attachments": [{"id": 1}]},
        {"id": 11, "user_id": 2, "user": {"name": "Bob"}, "workflow_state": "unsubmitted",
         "missing": True},
    ]
    units = units_from_submissions(raw)
    assert [u.student_name for u in units] == ["Ada", "Bob"]
    assert units[0].display_name == "Ada"
    assert units[0].is_submitted is True
    assert units[1].is_submitted is False


def test_grouped_submissions_collapse_to_one_unit_per_group():
    raw = [
        {"id": 10, "user_id": 1, "user": {"name": "Ada"}, "group": {"id": 7, "name": "Team 3C2"},
         "workflow_state": "submitted", "url": "https://github.com/t/r"},
        {"id": 11, "user_id": 2, "user": {"name": "Bob"}, "group": {"id": 7, "name": "Team 3C2"},
         "workflow_state": "submitted", "url": "https://github.com/t/r"},
        {"id": 12, "user_id": 3, "user": {"name": "Cy"}, "group": {"id": 8, "name": "Team 4C2"},
         "workflow_state": "submitted", "url": "https://github.com/u/s"},
    ]
    units = units_from_submissions(raw, grouped=True)
    assert [u.display_name for u in units] == ["Team 3C2", "Team 4C2"]
    assert units[0].group_id == 7


def test_grouped_submission_without_a_group_falls_back_to_individual():
    raw = [{"id": 10, "user_id": 1, "user": {"name": "Ada"}, "workflow_state": "submitted"}]
    units = units_from_submissions(raw, grouped=True)
    assert len(units) == 1 and units[0].display_name == "Ada"


def test_missing_flag_and_state_agree():
    assert unit(workflow_state="unsubmitted").is_submitted is False
    assert unit(missing=True).is_submitted is False
    assert unit(workflow_state="submitted").is_submitted is True


# ------------------------------------------------------------- prompts

def test_system_prompt_carries_the_rubric_and_the_real_tool_name(spec):
    text = build_system_prompt(spec)
    assert EXPOSED_TOOL_NAME in text
    assert "{tool}" not in text, "the tool placeholder was left unsubstituted"
    for c in spec.criteria:
        assert c.title in text
        assert c.canvas_criterion_id in text


def test_system_prompt_states_the_isolation_rule(spec):
    text = build_system_prompt(spec).lower()
    assert "isolation" in text
    assert "other student" in text


def test_system_prompt_includes_instructions(spec):
    assert "regex" in build_system_prompt(spec).lower()


def test_system_prompt_honours_manual_only_criteria(spec):
    first = spec.criteria[0].canvas_criterion_id
    text = build_system_prompt(spec, manual_ids=[first])
    assert "reserved for human review" in text.lower()


def test_partial_credit_is_explained_for_binary_rubrics(spec):
    text = build_system_prompt(spec)
    assert "partial credit" in text.lower()
    assert "Full (" in text  # names the Full/No Marks ceiling


def test_system_prompt_explains_rating_tier_rubrics():
    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    assignment["rubric"] = [{
        "id": "_1", "points": 10, "description": "Argument", "long_description": "",
        "ratings": [
            {"id": "a", "points": 10, "description": "Excellent", "long_description": ""},
            {"id": "b", "points": 6, "description": "Proficient", "long_description": ""},
            {"id": "c", "points": 0, "description": "Needs Improvement", "long_description": ""},
        ],
    }]
    spec = spec_from_assignment(assignment, BASE_URL, 3130)
    assert spec.is_rating_scored
    assert "rating_label" in build_system_prompt(spec)


def test_policy_overlay_reaches_the_prompt(spec):
    spec.policy = {
        "zero_conditions": ["no team participation sheet"],
        "evidence_hints": {"extraction": "quote each pattern"},
        "tiebreak": "err on the side of the student",
    }
    text = build_system_prompt(spec)
    assert "no team participation sheet" in text
    assert "quote each pattern" in text
    assert "err on the side of the student" in text


def test_task_prompt_lists_the_collected_material(spec):
    sources = [
        {"kind": "github", "status": "ok", "path": "/w/repo", "meta_json": '{"commit":"abc123"}'},
        {"kind": "image", "status": "ok", "path": "/w/shot.png"},
        {"kind": "pdf", "status": "ok", "markdown_path": "/w/doc.md"},
        {"kind": "url", "status": "error", "error": "HTTP 404"},
    ]
    text = build_task_prompt(spec, unit(), sources, "/w")
    assert "/w/repo" in text and "abc123" in text
    assert "shot.png" in text and "look at it" in text.lower()
    assert "doc.md" in text
    assert "unavailable" in text and "HTTP 404" in text


def test_task_prompt_short_circuits_for_an_unsubmitted_student(spec):
    text = build_task_prompt(spec, unit(workflow_state="unsubmitted"), [], "/w")
    assert "no submission" in text.lower()
    assert "rubric" not in text.lower()


def test_task_prompt_carries_test_results_and_anchors(spec):
    text = build_task_prompt(spec, unit(), [], "/w",
                             test_results={"extraction": {"passed": 12, "total": 15}},
                             anchors=[{"label": "Strong", "earned": 22, "possible": 25,
                                       "note": "matches expectations"}])
    assert '"passed": 12' in text
    assert "Calibration anchors" in text
    assert "matches expectations" in text


def test_task_prompt_names_the_reserved_criteria(spec):
    text = build_task_prompt(spec, unit(), [], "/w",
                             manual_ids=[spec.criteria[-1].canvas_criterion_id])
    assert "Reserved for human review" in text
    assert spec.criteria[-1].title in text


@pytest.mark.parametrize("budget,expected", [
    (8192, "normal length"),
    (2048, "Be concise"),
    (1024, "tight"),
])
def test_length_guidance_scales_with_the_budget(budget, expected):
    assert expected in length_guidance(budget, 5)


def test_retry_prompt_repeats_the_problems_and_the_tool_name():
    text = retry_prompt(["no evidence recorded", "scores exceed maximum"])
    assert "no evidence recorded" in text
    assert "scores exceed maximum" in text
    assert EXPOSED_TOOL_NAME in text


def test_prompt_version_is_declared():
    assert PROMPT_VERSION


# ---------------------------------------------------------------- stub

def test_stub_produces_a_valid_scorecard(spec):
    sc = stub_scorecard(spec, "Ada Lovelace", [{"kind": "github", "status": "ok",
                                                "path": "/w/repo"}])
    result = validate_scorecard(sc, spec)
    assert result.ok, result.issues
    assert 0 < result.earned < result.possible


def test_stub_is_deterministic(spec):
    a = stub_scorecard(spec, "Ada Lovelace", [])
    b = stub_scorecard(spec, "Ada Lovelace", [])
    assert [s.points_earned for s in a.criterion_scores] == [s.points_earned for s in b.criterion_scores]


def test_stub_varies_by_student(spec):
    a = stub_scorecard(spec, "Ada Lovelace", [])
    b = stub_scorecard(spec, "Bob Smith", [])
    assert a.total_earned != b.total_earned


def test_stub_scores_stay_within_the_rubric(spec):
    for name in [f"Student {n}" for n in range(40)]:
        result = validate_scorecard(stub_scorecard(spec, name, []), spec)
        assert result.ok, (name, result.issues)
        assert result.earned <= result.possible


def test_stub_flags_itself_as_blocking(spec):
    sc = stub_scorecard(spec, "Ada Lovelace", [])
    assert any(f.code == "stub_run" and f.severity == "block" for f in sc.flags)
    assert sc.model.startswith("stub")
    assert sc.prompt_version == "stub"


def test_stub_cites_the_material_it_was_given(spec):
    sc = stub_scorecard(spec, "Ada", [{"kind": "image", "status": "ok", "path": "/w/x.png"}])
    evidence = sc.criterion_scores[0].evidence
    assert any("x.png" in e for e in evidence)


# ------------------------------------------------------- mcp tool (stdio)

def _mcp_exchange(spec, calls, tmp_path):
    """Drive the MCP server over real stdio, exactly as opencode would."""
    spec_blob = {
        "spec": spec.model_dump(mode="json"),
        "manual_only": [],
        "tool_schema": scorecard_json_schema(spec),
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec_blob))
    out_path = tmp_path / "scorecard.json"

    env = {
        **os.environ,
        "GP_SPEC_PATH": str(spec_path),
        "GP_OUT_PATH": str(out_path),
        "GP_CODE_ROOT": str(CODE_ROOT),
        "PYTHONPATH": str(CODE_ROOT),
    }
    proc = subprocess.Popen(
        [sys.executable, str(CODE_ROOT / "pipeline/grading/mcp_server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=env)

    lines = []
    lines.append({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
    for i, (name, args) in enumerate(calls, start=2):
        lines.append({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})
    payload = "\n".join(json.dumps(x) for x in lines) + "\n"
    out, _ = proc.communicate(payload, timeout=60)

    replies = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        replies[msg.get("id")] = msg
    record = json.loads(out_path.read_text()) if out_path.exists() else None
    return replies, record


def _payload(spec, earned=None):
    return {"scores": {
        c.canvas_criterion_id: {
            "points_earned": c.points if earned is None else earned.get(c.canvas_criterion_id, 0),
            "analysis": "Looks right.", "justification": "Meets it.",
            "evidence": ["found in main.py"],
        } for c in spec.criteria
    }, "summary": "Fine."}


def test_mcp_handshake_and_tool_listing(spec, tmp_path):
    replies, _ = _mcp_exchange(spec, [], tmp_path)
    init = replies[1]["result"]
    assert init["serverInfo"]["name"] == "grading-pipeline"
    assert "tools" in init["capabilities"]


def test_mcp_accepts_a_valid_scorecard(spec, tmp_path):
    replies, record = _mcp_exchange(spec, [(TOOL_NAME, _payload(spec))], tmp_path)
    result = replies[2]["result"]
    assert result["isError"] is False
    assert record["ok"] is True
    assert record["earned"] == 25
    assert record["possible"] == 25


def test_mcp_rejects_and_explains_a_bad_scorecard(spec, tmp_path):
    bad = _payload(spec, {c.canvas_criterion_id: 99 for c in spec.criteria})
    replies, record = _mcp_exchange(spec, [(TOOL_NAME, bad)], tmp_path)
    assert replies[2]["result"]["isError"] is True
    assert record["ok"] is False
    assert any("exceeds maximum" in i for i in record["issues"])
    assert "Rejected" in replies[2]["result"]["content"][0]["text"]


def test_mcp_records_the_rejection_even_when_invalid(spec, tmp_path):
    """A rejected call must still leave an outcome file, or the run stalls."""
    bad = _payload(spec, {c.canvas_criterion_id: 99 for c in spec.criteria})
    _replies, record = _mcp_exchange(spec, [(TOOL_NAME, bad)], tmp_path)
    assert record is not None and record["ok"] is False


def test_mcp_rejects_an_unknown_tool(spec, tmp_path):
    replies, _ = _mcp_exchange(spec, [("not_a_tool", {})], tmp_path)
    assert replies[2]["error"]["code"] == -32601


def test_mcp_reports_the_tool_name_the_model_actually_sees():
    assert EXPOSED_TOOL_NAME == f"{MCP_SERVER_NAME}_{TOOL_NAME}"
    assert EXPOSED_TOOL_NAME.endswith("_submit_scorecard")


def test_mcp_scores_survive_a_round_trip_through_the_tool(spec, tmp_path):
    _replies, record = _mcp_exchange(spec, [(TOOL_NAME, _payload(spec))], tmp_path)
    scorecard = parse_scorecard(record["payload"], spec)
    assert validate_scorecard(scorecard, spec).ok
    assert scorecard.total_earned == 25

# --------------------------------------------------------------- runner

class FakeServer:
    """Stands in for the opencode HTTP server.

    Writes a real MCP outcome file on the first turn so the runner's happy path
    is exercised end to end without a model, and can be told to write nothing,
    to write a rejected card, or to raise.
    """

    def __init__(self, mode="accept", spec=None, manual=None):
        self.mode = mode
        self.spec = spec
        self.manual = manual or []
        self.turns: list[dict] = []
        self.budget = 4096
        self.max_output_tokens = 4096
        self.started = 0
        self.stopped = 0

    def start(self, *a, **k):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def create_session(self, title, directory):
        self.turns.append({"title": title, "directory": directory, "system": None, "text": None})
        return {"id": f"ses_{len(self.turns)}"}

    def prompt(self, session_id, text, system=None, agent=None, model=None,
               directory=None, timeout=1800):
        self.turns[-1].update(system=system, text=text, agent=agent, model=model)
        if self.mode == "raise":
            raise RuntimeError("opencode refused")
        if self.mode == "silent":
            return {"info": {}}

        work = Path(self.turns[-1]["directory"]) / ".grading"
        if self.mode == "reject":
            payload = {"scores": {c.canvas_criterion_id: {
                "points_earned": 99, "analysis": "x", "justification": "y",
                "evidence": ["z"]} for c in self.spec.criteria}, "summary": "no"}
            ok, issues, earned, possible = False, ["exceeds maximum"], 0, 25
        else:
            payload = {"scores": {c.canvas_criterion_id: {
                "points_earned": c.points, "analysis": "Fine.", "justification": "Meets it.",
                "evidence": ["main.py"]} for c in self.spec.criteria}, "summary": "Solid."}
            ok, issues, earned, possible = True, [], 25, 25
        (work / "scorecard.json").write_text(json.dumps(
            {"ok": ok, "issues": issues, "earned": earned, "possible": possible,
             "payload": payload}))
        return {"info": {}}

    def provider_error(self, response):
        return None

    def catalogue(self):
        return {"openrouter": ["a/b:free"]}


def _grader(tmp_path, store, spec, mode="accept", manual=None, attempts=2):
    from pipeline.grading.runner import Grader
    server = FakeServer(mode=mode, spec=spec, manual=manual)
    grader = Grader(server, store, spec, run_id=1, run_dir=tmp_path / "run",
                    code_root=CODE_ROOT, python_exe=sys.executable,
                    max_attempts=attempts, model="test/model", budget=4096)
    grader.manual_ids = manual or []
    return grader, server


def test_runner_records_a_graded_scorecard_on_success(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec)

    result = grader.grade(1, unit(), [{"kind": "github", "status": "ok", "path": "/w/r"}])

    assert result.status == "graded"
    assert result.earned == 25 and result.possible == 25
    assert store.submission_row(1)["status"] == "graded"
    assert store.best_scorecard(1) is not None
    assert store.best_scorecard(1).model == "test/model"


def test_runner_writes_a_workspace_and_agent_config(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec)
    grader.grade(1, unit(), [])

    workspace = tmp_path / "run" / "submissions" / "Ada_Lovelace"
    assert (workspace / "opencode.json").is_file()
    assert (workspace / ".grading" / "spec.json").is_file()

    config = json.loads((workspace / "opencode.json").read_text())
    assert MCP_SERVER_NAME in config["mcp"]
    assert config["model"] == "test/model"
    assert config["agent"]["grader"]["tools"]["edit"] is False
    assert config["agent"]["grader"]["tools"]["write"] is False
    # The tool schema is narrowed to this assignment's real criterion ids.
    blob = json.loads((workspace / ".grading" / "spec.json").read_text())
    assert blob["tool_schema"]["properties"]["scores"]["required"] == [
        c.canvas_criterion_id for c in spec.criteria]


def test_runner_sends_the_rubric_and_the_real_tool_name(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec)
    grader.grade(1, unit(), [{"kind": "github", "status": "ok", "path": "/w/r"}])

    turn = server.turns[0]
    assert EXPOSED_TOOL_NAME in turn["system"]
    for c in spec.criteria:
        assert c.title in turn["system"]
    assert "/w/r" in turn["text"]
    assert turn["agent"] == "grader"


def test_runner_uses_a_fresh_session_per_attempt(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec, mode="reject", attempts=2)
    result = grader.grade(1, unit(), [])

    assert result.status == "needs_review"
    assert len({t["title"] for t in server.turns}) == 2   # distinct sessions
    assert any("attempt 1" in t["title"] for t in server.turns)
    assert any("attempt 2" in t["title"] for t in server.turns)


def test_runner_retries_a_rejected_scorecard_then_gives_up(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec, mode="reject", attempts=2)
    result = grader.grade(1, unit(), [])

    assert result.status == "needs_review"
    assert any("exceeds maximum" in i for i in result.issues)
    assert store.submission_row(1)["status"] == "needs_review"
    assert store.invalid_reason(1)                      # the reason is kept
    # The retry must tell the agent what was wrong.
    assert "exceeds maximum" in server.turns[-1]["text"]


def test_runner_flags_a_silent_agent_that_never_calls_the_tool(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec, mode="silent", attempts=2)
    result = grader.grade(1, unit(), [])

    assert result.status == "needs_review"
    assert "submit_scorecard" in (result.error or "")
    assert store.submission_row(1)["status"] == "needs_review"
    assert store.best_scorecard(1) is None


def test_runner_marks_a_transport_failure_as_failed(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    grader, server = _grader(tmp_path, store, spec, mode="raise", attempts=1)
    result = grader.grade(1, unit(), [])

    assert result.status == "failed"
    assert "opencode refused" in (result.error or "")
    assert store.submission_row(1)["status"] == "failed"


def test_runner_passes_manual_only_criteria_through(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    reserved = spec.criteria[-1].canvas_criterion_id
    grader, server = _grader(tmp_path, store, spec, manual=[reserved])
    grader.grade(1, unit(), [])

    turn = server.turns[0]
    assert spec.criteria[-1].title in turn["system"]
    assert "Reserved for human review" in turn["system"]
    blob = json.loads((tmp_path / "run" / "submissions" / "Ada_Lovelace"
                       / ".grading" / "spec.json").read_text())
    assert blob["manual_only"] == [reserved]


def test_runner_survives_an_unsubmitted_unit(tmp_path, store, spec):
    store.replace_submissions(store.upsert_run(spec), [unit(workflow_state="unsubmitted",
                                                             missing=True)])
    grader, server = _grader(tmp_path, store, spec)
    # The pipeline should not even hand this to the agent.
    assert server.turns == []
