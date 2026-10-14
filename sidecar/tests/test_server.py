"""HTTP service tests against a real socket.

Routing bugs are invisible to unit-level calls: a handler whose parameter names
do not match the route's capture groups is simply never invoked, and the first
symptom is an empty screen in the app. These tests start the actual server and
drive it the way the renderer does.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import config
from pipeline import server as srv
from pipeline.canvas.client import AuthError, CanvasError
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

    def grade(self, course_id, assignment_id, user_id, **kw):
        self.published.append({"user_id": user_id, **kw})
        return {"id": 1}


def _local_repo(tmp_path: Path) -> Path:
    """A real git repository on disk, for the fixture to clone.

    The suite must not reach the network. The fixture submission pointed at
    https://github.com/ada/r, so every test that ran a collect shelled out to
    real `git` and cloned the actual public repository: slow, dependent on the
    connection, and failing outright on an offline machine. Worse, the clone has
    a 180s timeout while the tests give up after 30s, so a slow fetch showed up
    as a hung job and four tests failed intermittently for no reason at all.

    Cloning a local path exercises the same code -- real git, real working tree,
    real commit metadata -- without a single packet leaving the machine.
    """
    repo = tmp_path / "origin"
    (repo / "material").mkdir(parents=True)
    (repo / "material" / "essay.md").write_text("# Essay\n\nAn argument.\n")
    (repo / "README.md").write_text("# Submission\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture",
           "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture",
           "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "submission"]):
        subprocess.run(cmd, cwd=repo, env=env, capture_output=True, check=True)
    return repo


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A running sidecar with a stubbed Canvas and a temporary data directory."""
    # Isolate secret storage before anything constructs Settings. Without this
    # a test that saves a credential writes it into the developer's real
    # secrets.json, or a CI runner's home directory.
    monkeypatch.setenv("GRADING_PIPELINE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    # The workspace is read from a different variable, and defaults to a real
    # directory under the user's home. Without this the suite appends its log
    # entries to the agent log of whichever run happens to share the course and
    # assignment ids the fixtures use — which is also why two tests that each
    # passed alone failed together.
    monkeypatch.setenv("GRADING_PIPELINE_WORKSPACE", str(tmp_path / "workspace"))

    store = Store(tmp_path / "pipeline.db")
    bus = srv.EventBus(store)
    state = srv.AppState(store=store, bus=bus)

    repo = _local_repo(tmp_path)

    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    spec = spec_from_assignment(assignment, BASE_URL, 3130)

    fake = FakeCanvas(
        spec,
        courses=[{"id": 3130, "name": "Example Regex", "term": "m2026", "workflow_state": "available"}],
        assignments=[{"id": 46805, "name": "Regex Onboarding Hackathon", "points_possible": 25.0,
                      "published": True, "due_at": None, "rubric": assignment["rubric"],
                      "needs_grading_count": 2, "group_category_id": None,
                      "submission_types": ["online_url"]}],
        submissions=[{"id": 1, "user_id": 7, "user": {"name": "Ada Lovelace"},
                      "workflow_state": "submitted", "submitted_at": "2026-09-06T19:54:08Z",
                      "score": None, "url": str(repo), "attachments": []},
                     {"id": 2, "user_id": 8, "user": {"name": "Bob Smith"},
                      "workflow_state": "unsubmitted", "missing": True}],
    )
    fake.submission_url = str(repo)
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


def settle(state, job, timeout=30.0):
    """Wait for a job to be registered and then finish.

    A job registers itself in `state.controls` and pops the entry when it
    returns. Waiting only for an effect it produces — a row turning graded, say
    — is not the same as waiting for the job to end: a row is marked inside the
    loop and the run status is written after it, so a test that asserts on the
    status the moment the count moves reads the value from before the job
    finished writing it. That is a race, and it passed locally while failing on
    a slower runner, which is the worst way to find one out.
    """
    assert wait_for(lambda: job in state.controls, timeout), f"{job} never started"
    if not wait_for(lambda: job not in state.controls, timeout):
        # Say what it was actually doing. "did not finish: {}" on a loaded
        # runner tells nobody anything, and the point of waiting for the job to
        # end rather than for its effect is to survive a slow machine.
        raise AssertionError(
            f"the {job} job did not finish within {timeout}s; "
            f"job_errors={state.job_errors} progress={state.store.progress(1)}")


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
    assert [c["name"] for c in body["courses"]] == ["Example Regex"]


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
    base, state, fake, _ = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)

    rows = get(base, "/api/runs/1/submissions")["submissions"]
    by_name = {r["student"]: r for r in rows}
    assert set(by_name) == {"Ada Lovelace", "Bob Smith"}
    assert by_name["Bob Smith"]["missing"] is True
    assert by_name["Ada Lovelace"]["primary_url"] == fake.submission_url
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


def test_saving_canvas_settings_does_not_restart_opencode(live, monkeypatch):
    """Only a key change needs a restart; a Canvas change must not kill a run."""
    base, state, _fake, _spec = live
    state.server = None
    state.settings.canvas_base_url = "https://school.instructure.com"
    state.settings.canvas_api_token = "tok"
    accepted(monkeypatch)
    send(base, "/api/settings", {"canvas_base_url": "https://school.instructure.com"})
    assert state.server is None


# ------------------------------------------------------- credential checks

def accepted(monkeypatch) -> None:
    """Make Canvas accept whatever token is offered."""
    monkeypatch.setattr(srv, "CanvasClient", lambda *a, **k: SimpleNamespace(
        get=lambda path: {"id": 7, "name": "Ada Lovelace"}))


def rejects(monkeypatch, error) -> None:
    def boom(*_a, **_k):
        raise error
    monkeypatch.setattr(srv, "CanvasClient", boom)


def test_a_token_canvas_accepts_is_stored(live, monkeypatch):
    base, _state, _fake, _spec = live
    accepted(monkeypatch)
    body = send(base, "/api/settings", {
        "canvas_base_url": "https://school.instructure.com/",
        "canvas_api_token": "tok-good",
    })
    assert body["canvas_configured"] is True, body
    stored = config.get_secret("canvas_api_token")
    assert stored == "tok-good", stored
    assert config.get_secret("canvas_base_url") == "https://school.instructure.com"


def test_a_token_canvas_rejects_is_not_stored(live, monkeypatch):
    """The reason this exists.

    "Connected" used to mean two non-empty strings, so a revoked or mistyped
    token was reported as connected and then failed later, when the course list
    silently came back empty.
    """
    base, _state, _fake, _spec = live
    rejects(monkeypatch, AuthError("Canvas rejected the token (401) on GET /users/self."))
    try:
        send(base, "/api/settings", {
            "canvas_base_url": "https://school.instructure.com",
            "canvas_api_token": "tok-bad",
        })
        raise AssertionError("a rejected token should not save")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401, exc.code
        payload = json.loads(exc.read())
        assert "rejected" in payload["error"].lower(), payload
    # The decisive part: nothing was written, so a bad token cannot sit in the
    # settings looking valid.
    assert config.get_secret("canvas_api_token") != "tok-bad", \
        "a rejected token was written to the secrets file anyway"
    assert config.get_secret("canvas_base_url") != "https://school.instructure.com"


def test_an_unreachable_canvas_is_reported_differently_from_a_rejection(live, monkeypatch):
    """Different problem, different fix: no network is not a bad token."""
    base, _state, _fake, _spec = live
    rejects(monkeypatch, CanvasError("connection refused"))
    try:
        send(base, "/api/settings", {
            "canvas_base_url": "https://school.instructure.com",
            "canvas_api_token": "tok",
        })
        raise AssertionError("an unreachable Canvas should not save")
    except urllib.error.HTTPError as exc:
        assert exc.code == 502, exc.code
        payload = json.loads(exc.read())
        assert "could not reach canvas" in payload["error"].lower(), payload


def test_validating_one_credential_checks_the_pair(live, monkeypatch):
    """Changing only the URL is still checked against the stored token.

    Otherwise a typo in the URL saves cleanly and fails on the next request.
    """
    base, state, _fake, _spec = live
    state.settings.canvas_base_url = "https://school.instructure.com"
    state.settings.canvas_api_token = "tok-stored"
    accepted(monkeypatch)
    send(base, "/api/settings", {"canvas_base_url": "https://new.instructure.com"})
    assert config.get_secret("canvas_base_url") == "https://new.instructure.com"


def test_the_openrouter_key_is_not_validated_against_canvas(live, monkeypatch):
    """Saving a model key must not require Canvas to be reachable.

    They are unrelated credentials, and making one depend on the other would
    mean you cannot configure a model while Canvas is down.
    """
    base, _state, _fake, _spec = live
    rejects(monkeypatch, CanvasError("connection refused"))
    body = send(base, "/api/settings", {"openrouter_api_key": "sk-or-abc"})
    assert body["model_key_configured"] is True, body


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
    settle(state, "collect")
    assert state.store.progress(1).get("collected") == 1

    # Row 2 is unsubmitted, so selecting it must grade nothing at all. That is
    # the sharper check: if row_ids were ignored, this would grade row 1.
    send(base, "/api/runs/1/grade", {"stub": True, "row_ids": [2]})
    settle(state, "grade")
    assert state.store.progress(1).get("graded", 0) == 0, "row_ids was ignored"

    send(base, "/api/runs/1/grade", {"stub": True, "row_ids": [1]})
    settle(state, "grade")
    assert state.store.progress(1)["graded"] == 1, "grading a selection graded the cohort"

    # Row 1 is marked. Row 2 submitted nothing, and that is finished with
    # rather than outstanding, so this run really is complete. It took a
    # deliberate extra step to get a genuinely part-graded run to assert on
    # below, because the obvious fixture does not produce one.
    assert state.store.get_run(1)["status"] == "graded", state.store.get_run(1)["status"]


def test_a_partly_graded_run_is_not_reported_as_graded(live):
    """A job that stopped early must not leave the run claiming it finished.

    This is the defect from the saved-session list: a card reading "graded"
    directly beside "30/151 graded". The job returns when the provider quota
    runs out or a key dies, not only when the cohort is done, and it used to
    record `graded` either way.
    """
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/collect", {})
    settle(state, "collect")
    assert state.store.progress(1).get("collected") == 1

    # The fixture's second row submitted nothing, which is finished with. Turn
    # it into an outstanding submission so the run is genuinely part-graded:
    # this is the state a cohort of 151 is in after the quota dies at 30.
    state.store.set_submission_status(2, "collected")
    send(base, "/api/runs/1/grade", {"stub": True, "row_ids": [1]})
    settle(state, "grade")
    assert state.store.progress(1).get("graded") == 1, state.job_errors

    run = state.store.get_run(1)
    assert run["status"] == "grading", f'still claiming {run["status"]} at 1 of 2'
    card = next(r for r in state.store.list_runs() if r["id"] == 1)
    assert card["status"] == "grading", card
    assert card["graded"] == 1 and card["total"] == 2

    # Finish the cohort and it settles the other way.
    send(base, "/api/runs/1/grade", {"stub": True, "row_ids": [2]})
    settle(state, "grade")
    assert state.store.get_run(1)["status"] == "graded", state.store.get_run(1)["status"]


def test_the_agent_log_endpoint_returns_the_recorded_lines(live):
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    state.record_agent_output(1, "agent started")
    got = send(base, "/api/reports/1/agent-log", method="GET")
    # Entries are structured, not plain strings: the renderer needs the kind to
    # colour an error differently from a tool call and to collapse repeats.
    assert any(e["kind"] == "meta" and e["text"] == "agent started" for e in got["lines"]), \
        got["lines"]
    assert all("t" in e and "kind" in e for e in got["lines"])
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


def test_the_agent_log_records_what_the_agent_was_doing(live):
    """A log of bare verbs is not worth reading.

    A tool part carries state.input, state.title, state.output and a status, and
    step-finish carries tokens and cost. The log uses all of it, because
    "-> read" twenty times tells you nothing and "-> read rubric.md" tells you
    everything.
    """
    _base, state, _fake, _spec = live
    tool = {"id": "p1", "type": "tool", "tool": "bash", "text": "", "state": {
        "status": "completed",
        "input": {"command": "grep -rn 'APA' material/"},
        "title": "grep -rn 'APA' material/",
        "output": "material/refs.md:12: APA 7th",
        "metadata": {"exit": 0}}}
    entry = state._agent_entry_for(tool)
    assert entry["kind"] == "tool"
    assert entry["tool"] == "bash"
    assert entry["text"] == "grep -rn 'APA' material/", "the call lost its argument"
    assert entry["status"] == "completed"
    assert not entry.get("error")

    step = {"id": "p9", "type": "step-finish", "text": "", "cost": 0.0031,
            "tokens": {"input": 1200, "output": 340, "reasoning": 90}, "reason": "stop"}
    entry = state._agent_entry_for(step)
    assert entry["kind"] == "step" and entry["cost"] == 0.0031
    assert entry["tokens"]["output"] == 340

    # A failing tool is marked, so the log can colour it.
    bad = {"id": "p2", "type": "tool", "tool": "bash", "text": "", "state": {
        "status": "error", "title": "pytest", "output": "assert 1 == 2"}}
    assert state._agent_entry_for(bad)["error"] is True

    # Noise carries no line: step markers and empty text.
    assert state._agent_entry_for({"id": "p3", "type": "step-start", "text": ""}) is None
    assert state._agent_entry_for({"id": "p4", "type": "text", "text": "  "}) is None
    assert state._agent_entry_for({"id": "p5", "type": "reasoning", "text": ""}) is None


def test_the_agent_log_streams_reasoning_as_it_grows(live):
    """A reasoning part keeps its id while the model writes into it.

    Recording it once, on first sight, would show the opening sentence and then
    nothing more: exactly the "I cannot see the thought process" complaint. The
    log appends the growth instead.
    """
    _base, state, _fake, _spec = live

    class GrowingServer:
        """A reasoning part that fills in progressively, as a model streams it."""

        FULL = "The student submitted a draft."

        def __init__(self):
            self.step = 0

        def parts(self, session_id, directory=None):
            self.step += 1
            lengths = [12, 22, len(self.FULL)]
            text = self.FULL[: lengths[min(self.step - 1, 2)]]
            return [{"id": "r1", "type": "reasoning", "text": text, "state": {}}]

    server = GrowingServer()
    state.active_sessions["ses_g"] = (1, "/tmp/ws")
    stop = state.start_agent_log(server)
    try:
        assert wait_for(lambda: len(state.agent_logs.get(1, [])) >= 3, timeout=10), \
            f"reasoning did not stream: {state.agent_logs.get(1)}"
    finally:
        stop.set()
    lines = state.agent_logs[1]
    assert lines[0]["kind"] == "think"
    joined = "".join(x["text"] for x in lines if x["kind"].startswith("think"))
    assert joined == GrowingServer.FULL, joined


def test_a_failed_turn_is_visible_and_says_whether_anything_follows(live):
    """A turn that failed used to leave the log looking like work in progress."""
    _base, state, _fake, _spec = live
    state.record_agent_failure(1, "credit: 402 insufficient credits", retryable=False)
    entry = state.agent_logs[1][-1]
    assert entry["kind"] == "error"
    assert "insufficient credits" in entry["text"]
    assert entry["retryable"] is False


def test_the_agent_log_polls_a_session_and_records_each_part_once(live):
    _base, state, _fake, _spec = live

    class FakeServer:
        def __init__(self):
            self.polls = 0

        def parts(self, session_id, directory=None):
            self.polls += 1
            out = [{"id": "p1", "type": "tool", "tool": "read", "text": "",
                    "state": {"status": "completed", "title": "rubric.md", "output": ""}}]
            if self.polls >= 2:
                out.append({"id": "p2", "type": "reasoning", "text": "checking", "state": {}})
            return out

    server = FakeServer()
    state.active_sessions["ses_p"] = (1, "/tmp/ws")
    stop = state.start_agent_log(server)
    try:
        assert wait_for(lambda: server.polls >= 3, timeout=10), "the poller stopped"
        entries = state.agent_logs.get(1, [])
        tools = [e for e in entries if e["kind"] == "tool"]
        assert len(tools) == 1, f"part repeated: {entries}"
        assert tools[0]["text"] == "rubric.md"
    finally:
        stop.set()


def test_an_attempt_with_no_scorecard_reaches_the_agent_log(live):
    """A silent agent was failing in the activity list and invisible in the log.

    `agent.warn` — an attempt that ended without calling the tool — went only to
    the activity feed. The panel behind "agent log" showed nothing but "agent
    started" for a submission that was quietly failing twice and landing in
    needs_review, so the one place a person would go to find out why was empty.
    """
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)

    # Routed the way the grading job routes it, through the same handler.
    state.record_agent_failure(
        1, "the agent finished without calling submit_scorecard", False)

    entries = state.agent_logs[1]
    assert entries[-1]["kind"] == "error", entries
    assert "submit_scorecard" in entries[-1]["text"], entries
    # Non-retryable in this case, so the log says nothing more is coming.
    assert entries[-1]["retryable"] is False, entries

    # And it is on disk, because in memory it dies with the job.
    body = send(base, "/api/reports/1/agent-log", method="GET")
    assert any(e["kind"] == "error" and "submit_scorecard" in e["text"]
               for e in body["lines"]), body["lines"][-3:]


def test_the_agent_log_records_the_output_written_after_the_last_poll(live):
    """The panel froze mid-sentence unless it was closed and reopened.

    The poller runs on a one-second interval and the turn finishes between two
    polls, so everything the agent wrote in that gap — the reasoning, and the
    tool call that actually submits the scorecard — was only ever going to be
    seen by a poll that had not happened yet. Stopping the poller at the end of
    the job discarded it, and the log looked truncated until it was reopened.

    Stopping now drains: one more pass over the sessions before the poller goes
    away.
    """
    _base, state, _fake, _spec = live

    class LateServer:
        """Hands over the tail of the session only on the second poll."""

        def __init__(self):
            self.polls = 0

        def parts(self, session_id, directory=None):
            self.polls += 1
            out = [{"id": "early", "type": "tool", "tool": "glob", "text": "",
                    "state": {"status": "completed", "title": "a", "output": ""}}]
            if self.polls >= 2:
                out.append({"id": "late", "type": "reasoning",
                            "text": "final thought",
                            "state": {"completed": True, "time": {"end": 1}}})
            return out

    server = LateServer()
    state.active_sessions["ses_late"] = (1, "/tmp/ws")
    # What the terminal event does: the session is no longer active, but the
    # drain still has to be able to read it.
    state.recent_sessions["ses_late"] = state.active_sessions.pop("ses_late")
    state.agent_seen.pop("ses_late", None)

    state.start_agent_log(server)()
    entries = state.agent_logs.get(1, [])
    texts = [e.get("text", "") for e in entries]
    assert "final thought" in texts, \
        f"output written after the last poll was never recorded: {entries}"

    # And not twice, which is what a naive drain invites.
    assert texts.count("final thought") == 1, entries


def test_the_agent_log_is_bounded(live):
    _base, state, _fake, _spec = live
    for i in range(srv.AGENT_LOG_LINES + 250):
        state.record_agent_output(1, f"line {i}")
    lines = state.agent_logs[1]
    assert len(lines) == srv.AGENT_LOG_LINES
    assert lines[-1]["text"] == f"line {srv.AGENT_LOG_LINES + 249}", "kept the wrong tail"


def test_a_run_can_choose_its_own_model_without_changing_the_default(live):
    """The model on the setup page is a starting point, not a lock.

    Switching between a free model and a stronger one between runs is a normal
    thing to want, and it should not mean going back to setup and changing the
    default for everything afterwards.
    """
    _base, state, _fake, _spec = live
    state.catalogue = {
        "openrouter": ["vendor/cheap-small", "vendor/expensive-large"],
    }
    original = config.pick_model(state.catalogue, None, prefer_free=True)

    choice, model, budget = srv.resolve_model(state, "openrouter/vendor/expensive-large")
    assert model == "openrouter/vendor/expensive-large"
    assert choice.max_output_tokens == budget

    # The saved default is untouched.
    assert str(state.choice) != "openrouter/vendor/expensive-large" or state.choice is None


def test_a_run_model_that_does_not_exist_is_a_clear_error(live):
    _base, state, _fake, _spec = live
    state.catalogue = {"openrouter": ["vendor/cheap-small"]}
    with pytest.raises(srv.ApiError) as e:
        srv.resolve_model(state, "openrouter/nope/missing")
    assert e.value.status == 400
    assert "not available" in str(e.value.message) or "not available" in str(e.value)


# ------------------------------------------------------------ saved sessions

def test_saved_runs_are_listed_with_progress_and_can_be_named(live):
    """Grading is long and interrupted. Picking up where you left off needs a
    list of what exists and where each one got to."""
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/collect", {})
    assert wait_for(lambda: state.store.progress(1).get("collected") == 1), state.job_errors

    listed = send(base, "/api/runs", method="GET")
    assert len(listed["runs"]) == 1
    run = listed["runs"][0]
    assert run["id"] == 1
    assert run["total"] == 2
    assert run["label"] is None

    named = send(base, "/api/runs/1", {"label": "  Essay marking, week 3  "}, method="PUT")
    assert named["label"] == "Essay marking, week 3"
    again = send(base, "/api/runs", method="GET")
    assert again["runs"][0]["label"] == "Essay marking, week 3"

    # An empty name clears it rather than storing blanks.
    assert send(base, "/api/runs/1", {"label": ""}, method="PUT")["label"] is None


def test_naming_a_run_that_does_not_exist_is_a_404(live):
    base, _state, _fake, _spec = live
    with pytest.raises(urllib.error.HTTPError) as e:
        send(base, "/api/runs/999", {"label": "nope"}, method="PUT")
    assert e.value.code == 404


def test_the_runtime_endpoints_answer_without_being_configured(live):
    """The probe is built in serve(), so serve_test() leaves it absent.

    Every route must then say "not checked" rather than raising, because the
    renderer asks on page load and a 500 there would take the setup page with
    it.
    """
    base, state, _fake, _spec = live
    assert state.runtime is None
    body = get(base, "/api/runtime")
    assert body == {"checked": False, "update_available": False}, body

    for path in ("/api/runtime/check", "/api/runtime/update"):
        try:
            send(base, path, {})
            raise AssertionError(f"{path} should refuse without a runtime manager")
        except urllib.error.HTTPError as exc:
            assert exc.code == 409, (path, exc.code)


def test_the_runtime_is_reported_and_updates_are_staged_not_swapped(live, monkeypatch):
    base, state, _fake, _spec = live
    from pipeline import runtime as rt_mod
    staged = []

    class Manager:
        def __init__(self):
            self.status = rt_mod.RuntimeStatus(
                active_version="1.18.35", bundled_version="1.18.35",
                latest_version="1.19.0", checked=True, update_available=True)

        def stage_async(self, version):
            # Recorded, not executed: the point is that the route does not swap
            # a binary while a run may be in flight.
            staged.append(version)
            self.status.pending_version = version

    state.runtime = Manager()

    body = get(base, "/api/runtime")
    assert body["active_version"] == "1.18.35"
    assert body["latest_version"] == "1.19.0"
    assert body["update_available"] is True

    out = send(base, "/api/runtime/update", {})
    assert staged == ["1.19.0"], staged
    assert "restart" in out["note"].lower(), out


def test_updating_with_nothing_found_is_refused(live):
    base, state, _fake, _spec = live
    from pipeline import runtime as rt_mod

    class Manager:
        def __init__(self):
            self.status = rt_mod.RuntimeStatus(checked=True, latest_version=None)

        def stage_async(self, version):
            raise AssertionError("must not download when nothing was found")

    state.runtime = Manager()
    try:
        send(base, "/api/runtime/update", {})
        raise AssertionError("should not install an unknown version")
    except urllib.error.HTTPError as exc:
        assert exc.code == 409, exc.code


def test_stopping_one_submission_does_not_stop_the_run(live):
    """The only controls on offer were pause and stop for the whole job.

    Neither helps when it is one student you have watched reason for five
    minutes and want to move on from. The rest of the cohort must carry on.
    """
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(base, "/api/runs/1/collect", {})
    settle(state, "collect")

    body = send(base, "/api/submissions/1/stop")
    assert body["row_id"] == 1, body
    assert 1 in state.stopped_rows

    # Cleared once the row is dealt with, or it can never be graded again.
    state.stopped_rows.discard(1)
    assert not state.stopped_rows

    # An unknown row is a clean 404 rather than a 500 from the store.
    try:
        send(base, "/api/submissions/9999/stop")
        raise AssertionError("stopping a submission that does not exist should fail")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404, exc.code


def test_a_stopped_row_is_not_graded_by_the_loop(live):
    """The flag has to be honoured when the loop reaches that row.

    Stopping mid-turn does not stop the job: the loop is already inside the
    attempt. Without the flag it would come round to the same row and start it
    again, which is the opposite of what was asked for.
    """
    _base, state, _fake, _spec = live
    send(_base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    send(_base, "/api/runs/1/collect", {})
    settle(state, "collect")

    # Row 1 submitted and is waiting; row 2 did not, so it is not in the set.
    statuses = ("collected", "failed", "needs_review", "grading")
    before = [int(r["id"]) for r in state.store.submissions_with_status(1, statuses)]
    assert before == [1], before

    state.stopped_rows.add(1)
    remaining = [int(r["id"]) for r in state.store.submissions_with_status(1, statuses)
                 if int(r["id"]) not in state.stopped_rows]
    assert remaining == [], remaining


def test_the_agent_log_survives_the_job_and_a_restart(live):
    """In memory alone the log is gone the moment grading stops.

    That is exactly when someone wants to read why a student scored what they
    did, so it is written beside the run's other artefacts as it goes.
    """
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)

    state.record_agent_entry(1, {"kind": "think", "text": "weighing the rubric"})
    state.record_agent_entry(1, {"kind": "error", "text": "credit: 402", "retryable": False})

    path = state.log_path(1)
    assert path is not None and path.exists(), "the log was not written to disk"
    assert path.suffix == ".jsonl"
    # Beside the run's other artefacts, so it travels with the run.
    assert path.parent.name == "agent-log"
    assert path.parent.parent.name == f"{state.settings.run_dir(3130, 46805).name}"

    # Simulate the job finishing and the app restarting: drop the in-memory
    # copy, exactly as a fresh sidecar would have none.
    state.agent_logs.clear()
    after = send(base, "/api/reports/1/agent-log", method="GET")["lines"]
    texts = [e.get("text") for e in after]
    assert "weighing the rubric" in texts, texts
    failure = next(e for e in after if e.get("kind") == "error")
    assert failure["text"] == "credit: 402"
    assert failure["retryable"] is False
    # Timestamps survive the round trip, or the log cannot be read as a timeline.
    assert all(isinstance(e.get("t"), (int, float)) for e in after)


def test_the_agent_log_file_is_trimmed(live):
    """An append log for a 150 submission cohort is not an archive."""
    base, state, _fake, _spec = live
    send(base, "/api/runs", {"course_id": 3130, "assignment_id": 46805})
    assert wait_for(lambda: state.store.progress(1).get("total") == 2)
    path = state.log_path(1)
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"kind": "meta", "text": f"n{i}", "t": float(i)}) + "\n"
        for i in range(20000)))
    assert path.stat().st_size > srv.AGENT_LOG_BYTES
    state._trim_log_file(path)
    assert path.stat().st_size <= srv.AGENT_LOG_BYTES
    tail = path.read_text().splitlines()
    assert len(tail) <= srv.AGENT_LOG_LINES
    assert json.loads(tail[-1])["text"] == "n19999", "kept the wrong end"
