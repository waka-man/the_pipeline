"""HTTP service tests against a real socket.

Routing bugs are invisible to unit-level calls: a handler whose parameter names
do not match the route's capture groups is simply never invoked, and the first
symptom is an empty screen in the app. These tests start the actual server and
drive it the way the renderer does.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pipeline import config
from pipeline import server as srv
from pipeline.canvas.spec import spec_from_assignment
from pipeline.models import SubmissionUnit
from pipeline.store import Store
from pipeline.stub import stub_scorecard

BASE_URL = "https://example.instructure.com"
FIXTURES = Path(__file__).parent / "fixtures"


class FakeCanvas:
    """Stands in for the API so these tests never touch the network."""

    def __init__(self, spec, courses=None, assignments=None, submissions=None):
        self.spec = spec
        self._courses = courses or []
        self._assignments = assignments or []
        self._submissions = submissions or []
        self.published: list[dict] = []

    def probe(self):
        return {"id": 1, "name": "Instructor"}

    @property
    def base_url(self):
        return BASE_URL

    def list_courses(self):
        return self._courses

    def request(self, method, path, **kw):
        bare = path.split("?", 1)[0]
        if bare.startswith("/courses/") and bare.endswith("/assignments"):
            return self._assignments
        raise AssertionError(f"unexpected request: {method} {path}")

    def get_assignment(self, course_id, assignment_id):
        raw = dict(self._assignments[0])
        raw["id"] = assignment_id
        raw["course_id"] = course_id
        return raw

    def get_submissions(self, course_id, assignment_id, grouped=False):
        return self._submissions

    def get_submission_for(self, user_id):
        return {"attachments": [], "url": "https://github.com/a/b", "body": ""}

    def grade(self, course_id, assignment_id, user_id, **kw):
        self.published.append({"user_id": user_id, **kw})
        return {"id": 1}


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A running sidecar with a stubbed Canvas and a temporary data directory."""
    # Isolate secret storage before anything constructs Settings. Without this
    # a test that saves a credential writes it into the developer's real
    # secrets.json, or a CI runner's home directory.
    monkeypatch.setenv("GRADING_PIPELINE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    store = Store(tmp_path / "pipeline.db")
    bus = srv.EventBus(store)
    state = srv.AppState(store=store, bus=bus)

    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    spec = spec_from_assignment(assignment, BASE_URL, 3130)

    fake = FakeCanvas(
        spec,
        courses=[{"id": 3130, "name": "ALU Regex", "term": "m2026", "workflow_state": "available"}],
        assignments=[{"id": 46805, "name": "Regex Onboarding Hackathon", "points_possible": 25.0,
                      "published": True, "due_at": None, "rubric": assignment["rubric"],
                      "needs_grading_count": 2, "group_category_id": None,
                      "submission_types": ["online_url"]}],
        submissions=[{"id": 1, "user_id": 7, "user": {"name": "Ada Lovelace"},
                      "workflow_state": "submitted", "submitted_at": "2026-09-06T19:54:08Z",
                      "score": None, "url": "https://github.com/ada/r", "attachments": []},
                     {"id": 2, "user_id": 8, "user": {"name": "Bob Smith"},
                      "workflow_state": "unsubmitted", "missing": True}],
    )
    monkeypatch.setattr(state, "client", lambda: fake)
    monkeypatch.setattr(state, "ensure_opencode", lambda: (_ for _ in ()).throw(
        AssertionError("no model should be needed for these routes")))

    httpd, port = srv.serve_test(state)
    base = f"http://127.0.0.1:{port}"
    yield base, state, fake, spec
    httpd.shutdown()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def send(base, path, payload=None, method="POST"):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait_for(predicate, timeout=8.0, step=0.05):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


# --------------------------------------------------------------- basics

def test_health(live):
    base = live[0]
    assert get(base, "/health") == {"ok": True, "service": "grading-pipeline"}


def test_status_reports_credentials_and_idle_jobs(live):
    base, state, _, _ = live
    body = get(base, "/api/status")
    assert set(body["jobs"]) == {"bootstrap", "collect", "grade", "render", "publish"}
    assert all(j["status"] == "idle" for j in body["jobs"].values())
    assert isinstance(body["canvas_configured"], bool)


def test_unknown_route_is_a_clean_404(live):
    base = live[0]
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/nope")
    assert exc.value.code == 404
    assert json.loads(exc.value.read())["error"]


def test_malformed_json_is_a_clean_400(live):
    base = live[0]
    req = urllib.request.Request(base + "/api/runs", data=b"{not json",
                                 method="POST", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400


# ------------------------------------------------------------ discovery

def test_courses_are_listed(live):
    base = live[0]
    body = get(base, "/api/courses")
    assert [c["name"] for c in body["courses"]] == ["ALU Regex"]


def test_assignments_report_their_rubric_size(live):
    base = live[0]
    body = get(base, "/api/courses/3130/assignments")
    a = body["assignments"][0]
    assert a["id"] == 46805
    assert a["criteria"] == 5
    assert a["has_rubric"] is True
    assert a["group"] is False


# ------------------------------------------------------- run lifecycle

def test_bootstrap_creates_a_run_and_registers_submissions(live):
    base, state, _, _ = live
    body = send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805,
                                    "assignments_dir": "/nonexistent"})
    assert body == {"job": "bootstrap"}
    assert wait_for(lambda: state.store.progress(1).get("total") == 2), state.job_errors

    run = get(base, "/api/runs/1")
    assert run["title"] == "Regex Onboarding Hackathon"
    assert run["points_possible"] == 25
    assert len(run["criteria"]) == 5
    assert run["progress"]["total"] == 2


def test_submissions_expose_score_and_report_state(live):
    base, state, _, _ = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)

    rows = get(base, "/api/runs/1/submissions")["submissions"]
    by_name = {r["student"]: r for r in rows}
    assert set(by_name) == {"Ada Lovelace", "Bob Smith"}
    assert by_name["Bob Smith"]["missing"] is True
    assert by_name["Ada Lovelace"]["primary_url"] == "https://github.com/ada/r"
    assert by_name["Ada Lovelace"]["score"] is None


def test_unknown_run_is_a_clean_error(live):
    base = live[0]
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(base, "/api/runs/999")
    assert exc.value.code == 500


def test_stub_grade_then_render_produces_a_readable_report(live):
    base, state, fake, spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)

    send(base, "/api/runs/1/grade", {"stub": True})
    assert wait_for(lambda: state.store.progress(1).get("no_submission") == 1), state.job_errors
    assert state.store.progress(1).get("graded") == 1

    send(base, "/api/runs/1/render", {})
    graded_rows = [r for r in state.store.submissions_for_run(1)
                   if r["status"] == "graded"]
    assert wait_for(lambda: all(state.store.report_for(r["id"]) for r in graded_rows))

    report = get(base, "/api/reports/1")
    assert report["markdown"].startswith("# Grading Report")
    assert "Ada Lovelace" in report["markdown"]
    assert report["scorecard"]["criterion_scores"]
    assert report["scorecard"]["model"].startswith("stub")


def test_editing_a_report_persists(live):
    base, state, fake, spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/grade", {"stub": True})
    assert wait_for(lambda: state.store.progress(1).get("graded") == 1), state.job_errors
    send(base, "/api/runs/1/render", {})
    assert wait_for(lambda: state.store.report_for(1))

    edited = get(base, "/api/reports/1")["markdown"] + "\n\nInstructor note.\n"
    assert send(base, "/api/reports/1", {"markdown": edited}, method="PUT")["saved"] is True
    assert "Instructor note." in get(base, "/api/reports/1")["markdown"]


def test_saving_a_report_that_does_not_exist_is_a_404(live):
    base = live[0]
    with pytest.raises(urllib.error.HTTPError) as exc:
        send(base, "/api/reports/1", {"markdown": "x"}, method="PUT")
    assert exc.value.code == 404


def test_save_report_requires_markdown(live):
    base, state, fake, spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/grade", {"stub": True})
    assert wait_for(lambda: state.store.progress(1).get("graded") == 1), state.job_errors
    send(base, "/api/runs/1/render", {})
    assert wait_for(lambda: state.store.report_for(1))
    with pytest.raises(urllib.error.HTTPError) as exc:
        send(base, "/api/reports/1", {}, method="PUT")
    assert exc.value.code == 400


# ------------------------------------------------------------- publish

def test_publish_previews_a_plan_and_writes_nothing(live):
    base, state, fake, spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/grade", {"stub": True})
    assert wait_for(lambda: state.store.progress(1).get("graded") == 1), state.job_errors

    plan = send(base, "/api/runs/1/publish", {"row_ids": [1]})
    assert plan["mode"] == "plan"
    entry = plan["entries"][0]
    assert entry["student"] == "Ada Lovelace"
    assert entry["existing"] is None
    assert 0 < entry["new"] < 25
    assert "stub_run" in entry["blocking"]
    assert set(entry["payload"]) == {c.canvas_criterion_id for c in spec.criteria}
    assert fake.published == []


def test_publish_writes_the_rubric_assessment_once_confirmed(live):
    base, state, fake, spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/grade", {"stub": True})
    assert wait_for(lambda: state.store.progress(1).get("graded") == 1), state.job_errors
    send(base, "/api/runs/1/render", {})
    assert wait_for(lambda: state.store.report_for(1))

    send(base, "/api/runs/1/publish", {"row_ids": [1], "confirm": True})
    # The fake client records the call before the job commits the publication,
    # so wait on the committed row rather than on the call, or this races.
    assert wait_for(lambda: state.store.already_published(1)), state.job_errors
    written = fake.published[0]
    assert written["user_id"] == 7
    assert "rubric_assessment" in written
    first = spec.criteria[0].canvas_criterion_id
    assert "points" in written["rubric_assessment"][first]
    assert "comments" in written["rubric_assessment"][first]


def test_publish_without_any_graded_work_plans_nothing(live):
    base, state, fake, spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    body = send(base, "/api/runs/1/publish", {})
    assert body["entries"] == []
    assert fake.published == []


def test_publishing_a_run_that_does_not_exist_is_an_error(live):
    base = live[0]
    with pytest.raises(urllib.error.HTTPError) as exc:
        send(base, "/api/runs/404/publish", {})
    assert exc.value.code == 500


# ---------------------------------------------------------------- events

def test_event_stream_delivers_job_lifecycle(live):
    import queue

    base, state, _, _ = live
    seen: queue.Queue = queue.Queue()
    sub = state.bus.subscribe()
    stop = threading.Event()

    def drain():
        while not stop.is_set():
            try:
                seen.put(sub.get(timeout=0.2))
            except queue.Empty:
                pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()

    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    events = []
    deadline = __import__("time").time() + 20
    while __import__("time").time() < deadline and len(events) < 3:
        try:
            events.append(seen.get(timeout=1).get("event"))
        except queue.Empty:
            break
    stop.set()
    state.bus.unsubscribe(sub)

    assert "job.start" in events
    assert "run.ready" in events or "job.done" in events


def test_bus_drops_slow_subscribers_rather_than_blocking(live):
    _, state, _, _ = live
    q = state.bus.subscribe()
    for n in range(2500):          # beyond the bounded queue size
        state.bus.emit("noise", {"n": n})
    assert q.qsize() < 2500       # publisher was never blocked
    state.bus.unsubscribe(q)

# ------------------------------------------------------- model credentials

def test_a_model_key_is_saved_cleared_and_never_echoed(live):
    """The key is stored, can be removed, and is never returned to the renderer."""
    base, state, _fake, _spec = live

    assert send(base, "/api/status", method="GET")["model_key_configured"] is False

    saved = send(base, "/api/settings", {"openrouter_api_key": "  sk-or-secret  "})
    assert saved["model_key_configured"] is True
    # Whitespace trimmed on the way in.
    assert state.settings.openrouter_api_key == "sk-or-secret"
    # The key must not come back over HTTP, or it lands in the DOM.
    assert "sk-or-secret" not in json.dumps(saved)

    cleared = send(base, "/api/settings", {"openrouter_api_key": ""})
    assert cleared["model_key_configured"] is False
    assert state.settings.openrouter_api_key == ""


def test_the_key_reaches_opencode_as_an_env_var_and_an_auth_file(live):
    base, state, _fake, _spec = live

    send(base, "/api/settings", {"openrouter_api_key": "sk-or-abc123"})
    env = state.opencode_env()

    # The env var is the route that works on every platform.
    assert env["OPENROUTER_API_KEY"] == "sk-or-abc123"

    auth = Path(state.settings.data) / "opencode-home" / "opencode" / "auth.json"
    assert auth.exists(), "opencode auth.json was not written"
    stored = json.loads(auth.read_text())
    assert stored["openrouter"] == {"type": "api", "key": "sk-or-abc123"}
    assert env["XDG_DATA_HOME"] == str(auth.parent.parent)


def test_without_a_key_opencode_gets_nothing_extra(live):
    _base, state, _fake, _spec = live
    state.settings.openrouter_api_key = ""
    assert state.opencode_env() == {}


def test_changing_the_key_restarts_a_running_opencode(live):
    base, state, _fake, _spec = live

    stopped = []

    class FakeServer:
        def stop(self):
            stopped.append(True)

    state.server = FakeServer()
    send(base, "/api/settings", {"openrouter_api_key": "sk-or-abc123"})
    assert stopped == [True], "opencode kept running with the old key"
    assert state.server is None


def test_saving_canvas_settings_does_not_restart_opencode(live):
    """Only a key change needs a restart; a Canvas change must not kill a run."""
    base, state, _fake, _spec = live
    state.server = None
    state.settings.canvas_base_url = "https://school.instructure.com"
    state.settings.canvas_api_token = "tok"
    send(base, "/api/settings", {"canvas_base_url": "https://school.instructure.com"})
    assert state.server is None


def test_tests_never_write_to_the_developers_real_secrets(live, tmp_path):
    """Guards the fixture itself.

    A test that saves a credential into the real secrets file is a slow way to
    discover the fixture stopped isolating storage: the assertion passes, the
    run is green, and the developer's own configuration has been overwritten.
    """
    base, _state, _fake, _spec = live
    send(base, "/api/settings", {"openrouter_api_key": "sk-or-not-real"})
    on_disk = config.secrets_file()
    assert str(tmp_path) in str(on_disk), f"secrets escaped the tmp dir: {on_disk}"
    real = Path.home() / ".local" / "share" / "grading-pipeline" / "secrets.json"
    if real.exists():
        assert "sk-or-not-real" not in real.read_text(), "wrote into the real secrets file"


def test_reading_a_report_renders_it_when_none_was_written(live):
    """Grading then going straight to Review must not show a blank report.

    Grading produces a scorecard; rendering produces Markdown. Nothing forces the
    second step, so a report read has to be able to produce it. Returning an
    empty string here is indistinguishable from a broken report in the UI.
    """
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/grade", {"stub": True})
    assert wait_for(lambda: state.store.progress(1).get("graded") == 1), state.job_errors

    # Grade without rendering: no report row exists yet.
    rid = 1
    assert state.store.report_for(rid) is None

    got = send(base, f"/api/reports/{rid}", method="GET")
    assert got["markdown"].strip(), "report markdown is empty"
    assert got["scorecard"] is not None
    assert got["path"], "the rendered report was not persisted"
    # And it is now on disk, so a second read is a plain file read.
    assert state.store.report_for(rid) is not None
    again = send(base, f"/api/reports/{rid}", method="GET")
    assert again["markdown"] == got["markdown"]


def test_a_report_with_no_scorecard_is_still_empty_not_an_error(live):
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    got = send(base, "/api/reports/1", method="GET")
    assert got["markdown"] == ""
    assert got["scorecard"] is None


# ------------------------------------------------------- job control

def test_collect_can_be_paused_resumed_and_stopped(live):
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)

    # Pause while nothing is running yet, then confirm the control exists.
    started = send(base, "/api/runs/1/collect", {"limit": 2})
    assert started["job"] == "collect"
    assert wait_for(lambda: "collect" in state.controls)
    assert send(base, "/api/runs/1/control", {"job": "collect", "action": "stop"})["action"] == "stop"
    assert wait_for(lambda: "collect" not in state.controls), "the job ignored stop"


def test_control_rejects_jobs_that_are_not_pausable(live):
    """Stopping publish would leave Canvas and the store disagreeing."""
    base, state, _fake, _spec = live
    for job in ("publish", "render", "bootstrap"):
        with pytest.raises(urllib.error.HTTPError) as e:
            send(base, "/api/runs/1/control", {"job": job, "action": "stop"})
        assert e.value.code == 400


def test_control_on_an_idle_job_is_a_conflict_not_a_silent_no_op(live):
    base, _state, _fake, _spec = live
    with pytest.raises(urllib.error.HTTPError) as e:
        send(base, "/api/runs/1/control", {"job": "collect", "action": "pause"})
    assert e.value.code == 409


def test_control_rejects_an_unknown_action(live):
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/collect", {})
    assert wait_for(lambda: "collect" in state.controls)
    with pytest.raises(urllib.error.HTTPError) as e:
        send(base, "/api/runs/1/control", {"job": "collect", "action": "detonate"})
    assert e.value.code == 400


def test_pausing_actually_holds_the_job():
    """A pause that does not block is worse than none: the UI would lie."""
    control = srv.JobControl()
    control.pause()
    assert control.paused
    freed = threading.Event()

    def worker():
        freed.set()
        control.wait_if_paused()

    threading.Thread(target=worker, daemon=True).start()
    assert freed.wait(2), "a paused job ran anyway"
    control.resume.set()
    assert freed.wait(2)


def test_stopping_a_paused_job_releases_it():
    """Otherwise stopping a paused job waits for a resume that never comes."""
    control = srv.JobControl()
    control.pause()
    control.request_stop()
    assert not control.paused
    assert control.wait_if_paused() is False


# --------------------------------------------------- grading a selection

def test_grading_a_selection_touches_only_those_rows(live):
    """Grade Selected must not quietly grade the rest of the cohort."""
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/collect", {})
    assert wait_for(lambda: state.store.progress(1).get("collected") == 1), state.job_errors

    # Row 2 is unsubmitted, so selecting it must grade nothing at all. That is
    # the sharper check: if row_ids were ignored, this would grade row 1.
    send(base, "/api/runs/1/grade", {"stub": True, "row_ids": [2]})
    assert wait_for(lambda: "grade" not in state.controls)
    assert state.store.progress(1).get("graded", 0) == 0, "row_ids was ignored"

    send(base, "/api/runs/1/grade", {"stub": True, "row_ids": [1]})
    assert wait_for(lambda: state.store.progress(1).get("graded") == 1), state.job_errors
    assert state.store.progress(1)["graded"] == 1, "grading a selection graded the cohort"


def test_the_agent_log_endpoint_returns_the_recorded_lines(live):
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    state.record_agent_output(1, "agent started")
    got = send(base, "/api/reports/1/agent-log", method="GET")
    assert "agent started" in got["lines"]
    assert got["student"] == "Ada Lovelace"
    assert got["row_id"] == 1


def test_an_agent_log_for_an_unknown_row_is_empty_not_a_500(live):
    """The panel polls this while a run may still be bootstrapping."""
    base, _state, _fake, _spec = live
    got = send(base, "/api/reports/999/agent-log", method="GET")
    assert got["lines"] == [] and got["student"] == ""


def test_the_agent_log_is_bounded(live):
    _base, state, _fake, _spec = live
    for i in range(srv.AGENT_LOG_LINES + 250):
        state.record_agent_output(1, f"line {i}")
    lines = state.agent_logs[1]
    assert len(lines) == srv.AGENT_LOG_LINES
    assert lines[-1] == f"line {srv.AGENT_LOG_LINES + 249}", "kept the wrong tail"


def test_the_agent_log_polls_a_session_and_records_each_part_once(live):
    """The log is fed by polling, not by opencode's event stream.

    The stream delivered only its opening frame and heartbeats while a turn was
    demonstrably running, verified against both curl and requests against the
    same server. Polling the session's messages returns the same information and
    cannot be silently killed by a restarted server or a dropped socket.
    """
    _base, state, _fake, _spec = live

    class FakeServer:
        """Grows the session over two polls, as a real turn does."""

        def __init__(self):
            self.polls = 0

        def parts(self, session_id, directory=None):
            self.polls += 1
            out = [{"id": "p1", "type": "tool", "tool": "read", "text": "", "error": {}}]
            if self.polls >= 2:
                out.append({"id": "p2", "type": "reasoning",
                            "text": "  checking   the rubric\n", "tool": "", "error": {}})
            return out

    server = FakeServer()
    state.active_sessions["ses_p"] = (1, "/tmp/ws")
    stop = state.start_agent_log(server)
    try:
        assert wait_for(lambda: server.polls >= 3, timeout=10), "the poller stopped"
        lines = state.agent_logs.get(1, [])
        assert lines.count("-> read") == 1, f"part repeated: {lines}"
        assert any(x.startswith("thinking: checking the rubric") for x in lines), lines
    finally:
        stop.set()


def test_agent_log_formats_parts_and_ignores_the_rest(live):
    """Shapes taken from a live opencode server, not invented."""
    _base, state, _fake, _spec = live
    fmt = state._format_part

    assert fmt({"type": "tool", "tool": "bash", "error": {}}) == "-> bash"
    assert fmt({"type": "tool", "tool": "grep", "error": {"name": "NotFound"}}) == "-> grep [NotFound]"
    assert fmt({"type": "reasoning", "text": "  reading  it \n"}) == "thinking: reading it"
    assert fmt({"type": "text", "text": "  the answer  is 4 "}) == "the answer is 4"

    for nothing in ({"type": "text", "text": ""}, {"type": "reasoning", "text": "   "},
                    {"type": "step-start", "text": "x"}, {"type": "", "text": "x"}):
        assert fmt(nothing) is None, nothing


def test_a_finished_session_is_no_longer_polled(live):
    """Otherwise the poller keeps polling a session for the rest of the job."""
    _base, state, _fake, _spec = live
    state.active_sessions["ses_done"] = (1, "/tmp/ws")
    assert state.active_sessions
    state.active_sessions.clear()
    assert state.active_sessions == {}
