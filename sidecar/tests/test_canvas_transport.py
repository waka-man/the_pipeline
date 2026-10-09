"""Transport-level tests for the Canvas client.

The client is the one component where a silent change quietly corrupts a whole
cohort, and it is the least exercised code in the project. These tests replay
responses recorded from the live instance through a stubbed transport, so they
assert on the shapes Canvas actually returns rather than on the shapes the
documentation implies.

Nothing here touches the network. Credentials are fake; only the recorded
bodies are real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.canvas.client import (
    BROWSER_HEADERS,
    RETRY_DELAYS,
    AuthError,
    CanvasClient,
    CanvasError,
    NotFoundError,
)
from pipeline.canvas.collect import units_from_submissions

FIXTURES = Path(__file__).parent / "fixtures" / "canvas"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ------------------------------------------------------- fake transport

class FakeResponse:
    def __init__(self, status: int, body=None, headers: dict | None = None,
                 content: bytes | None = None, text: str | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        if content is not None:
            self.content = content
        elif text is not None:
            self.content = text.encode()
        else:
            self.content = json.dumps(body if body is not None else {}).encode()
        self.text = text if text is not None else self.content.decode("utf-8", "replace")

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body

    @property
    def ok(self):
        return 200 <= self.status_code < 300


class FakeSession:
    """Replays recorded responses and records every request made."""

    def __init__(self, script: list[FakeResponse] | None = None, headers: dict | None = None):
        self.headers: dict[str, str] = dict(headers or {})
        self.script = list(script or [])
        self.requests: list[dict] = []
        self.payload = b""

    def _next(self) -> FakeResponse:
        if not self.script:
            raise AssertionError("client made an unscripted request")
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    def request(self, method, url, timeout=None, **kw):
        self.requests.append({"method": method, "url": url, "json": kw.get("json")})
        return self._next()

    def get(self, url, timeout=None, stream=False, **kw):
        self.requests.append({"method": "GET", "url": url, "stream": stream})
        if stream:
            session = self

            class _Stream:
                status_code = 200
                ok = True
                headers = {"content-length": str(len(session.payload))}

                def iter_content(self, chunk_size=1):
                    for i in range(0, len(session.payload), chunk_size):
                        yield session.payload[i : i + chunk_size]

            return _Stream()
        return self._next()


@pytest.fixture()
def client(monkeypatch):
    """A CanvasClient wired to a fake transport and an instant clock."""
    made: dict[str, object] = {}

    def build(script=None):
        c = CanvasClient("https://example.instructure.com", "fake-token")
        session = FakeSession(script or [], headers=dict(c.session.headers))
        c.session = session
        made["session"] = session
        made["client"] = c
        return c

    monkeypatch.setattr("pipeline.canvas.client.time.sleep", lambda _s: None)
    return build


@pytest.fixture()
def no_sleep(monkeypatch):
    """Record the backoff delays instead of actually waiting."""
    delays: list[float] = []
    monkeypatch.setattr("pipeline.canvas.client.time.sleep", delays.append)
    return delays


# ------------------------------------------------------------- headers

def test_requests_carry_a_browser_user_agent_not_the_library_default(client):
    c = client()
    assert c.session.headers["Authorization"] == "Bearer fake-token"
    # A bare "python-canvasapi/x.y" agent is what the WAF rejects.
    assert c.session.headers["User-Agent"] == BROWSER_HEADERS["User-Agent"]
    assert "Chrome" in c.session.headers["User-Agent"]


def test_missing_credentials_are_refused_at_construction():
    with pytest.raises(AuthError):
        CanvasClient("", "token")
    with pytest.raises(AuthError):
        CanvasClient("https://x.instructure.com", "")


# ----------------------------------------------------------- pagination

def test_include_becomes_repeated_query_parameters_not_a_request_kwarg(client, no_sleep):
    """`include=` as a requests kwarg raises; it has to reach the query string."""
    c = client([FakeResponse(200, [])])
    assert list(c.paginate("/courses", include=["term", "account"])) == []
    url = c.session.requests[0]["url"]
    assert "include[]=term" in url and "include[]=account" in url
    assert "per_page=100" in url


def test_include_is_added_alongside_existing_query_parameters(client):
    c = client([FakeResponse(200, [])])
    list(c.paginate("/courses/1/students?foo=bar", include=["groups"]))
    url = c.session.requests[0]["url"]
    assert "/api/v1/courses/1/students?foo=bar" in url
    assert "include[]=groups" in url and "per_page=100" in url


def test_pagination_follows_the_link_header_until_exhausted(client):
    page2 = "https://example.instructure.com/api/v1/courses?page=2"
    script = [
        FakeResponse(200, [{"id": 1}],
                     headers={"Link": f'<{page2}>; rel="next"'}),
        FakeResponse(200, [{"id": 2}]),
    ]
    c = client(script)
    got = list(c.paginate("/courses", per_page=1))
    assert [r["id"] for r in got] == [1, 2]
    assert c.session.requests[1]["url"] == page2


def test_pagination_stops_when_link_has_no_next(client):
    header = '<https://example.instructure.com/api/v1/courses?page=9>; rel="last"'
    c = client([FakeResponse(200, [{"id": 1}], headers={"Link": header})])
    assert len(list(c.paginate("/courses"))) == 1
    assert len(c.session.requests) == 1


def test_pagination_ignores_a_malformed_link_header(client):
    c = client([FakeResponse(200, [{"id": 1}], headers={"Link": "garbage"})])
    assert len(list(c.paginate("/courses"))) == 1


def test_pagination_on_a_non_list_body_yields_nothing(client):
    c = client([FakeResponse(200, {"error": "unexpected"})])
    assert list(c.paginate("/courses")) == []


# ---------------------------------------------------------------- retry

def test_403_is_retried_then_succeeds(client, no_sleep):
    """CloudFront returns a bare 403 for WAF throttling, indistinguishable
    from a permission error, so it must be retried rather than raised."""
    c = client([FakeResponse(403, {"errors": [{"message": "Forbidden"}]}), FakeResponse(200, {"ok": 1})])
    assert c.get("/courses") == {"ok": 1}
    assert len(no_sleep) == 1
    base = RETRY_DELAYS[0]
    assert base <= no_sleep[0] <= base * 1.2          # delay plus jitter


def test_retry_delays_increase_and_are_jittered(client, no_sleep):
    c = client([FakeResponse(429, {"e": 1}), FakeResponse(503, {"e": 1}), FakeResponse(200, {})])
    c.get("/courses")
    assert len(no_sleep) == 2
    assert no_sleep[0] < no_sleep[1]                 # backoff increases
    for delay, base in zip(no_sleep, RETRY_DELAYS):
        assert base <= delay <= base * 1.2           # plus jitter, never less


def test_401_is_never_retried(client, no_sleep):
    c = client([FakeResponse(401, {"errors": [{"message": "unauthorized"}]})])
    with pytest.raises(AuthError):
        c.get("/courses")
    assert no_sleep == []
    assert len(c.session.requests) == 1


def test_404_is_never_retried(client, no_sleep):
    c = client([FakeResponse(404, {"errors": [{"message": "not found"}]})])
    with pytest.raises(NotFoundError):
        c.get("/courses/1")
    assert no_sleep == []


def test_exhausted_retries_raise_a_descriptive_error(client, no_sleep):
    c = CanvasClient("https://example.instructure.com", "t", max_attempts=3)
    session = FakeSession([FakeResponse(429, {"e": 1})])
    c.session = session
    with pytest.raises(CanvasError, match="throttling"):
        c.get("/courses")
    assert len(session.requests) == 3
    assert len(no_sleep) == 2      # no sleep before the first attempt


def test_other_client_errors_are_not_retried(client, no_sleep):
    c = client([FakeResponse(400, {"errors": [{"message": "bad request"}]})])
    with pytest.raises(CanvasError, match="400"):
        c.get("/courses")
    assert no_sleep == []


def test_a_transport_error_is_retried(client, no_sleep, monkeypatch):
    import requests as rq

    calls = {"n": 0}

    def flaky(method, url, timeout=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise rq.ConnectionError("reset by peer")
        return FakeResponse(200, {"ok": True})

    c = client()
    monkeypatch.setattr(c.session, "request", flaky)
    assert c.get("/courses") == {"ok": True}
    assert calls["n"] == 2


# --------------------------------------------------------------- domain

def test_courses_are_parsed_from_the_recorded_response(client):
    body = load("courses")["body"]
    c = client([FakeResponse(200, body)])
    courses = c.list_courses()
    assert len(courses) == len(body)
    assert {"id", "name", "term"} <= set(courses[0])


def test_term_is_an_object_on_this_instance(client):
    """The shape that broke op_courses when it assumed a string."""
    term = load("courses")["body"][0].get("term")
    assert isinstance(term, dict) and "name" in term


def test_assignment_with_rubric_exposes_criterion_ids(client):
    body = load("assignment_rubric")["body"]
    c = client([FakeResponse(200, body)])
    a = c.get_assignment(3130, 46805)
    assert a["id"] == 46805
    ids = [r["id"] for r in a["rubric"]]
    assert ids == ["_753", "_7607", "_2500", "_4568", "_7796"]
    assert [r["points"] for r in a["rubric"]] == [9, 4, 6, 4, 2]


def test_ratings_carry_ids_and_points(client):
    rubric = load("assignment_rubric")["body"]["rubric"]
    first = rubric[0]["ratings"][0]
    assert set(first) >= {"id", "points", "description", "long_description"}


def test_submissions_parse_into_units_with_attachments(client):
    body = load("submissions")["body"]
    c = client([FakeResponse(200, body)])
    units = units_from_submissions(c.get_submissions(3130, 46805))

    assert len(units) == 37
    assert all(u.canvas_submission_id for u in units)
    assert all(u.user_id for u in units)
    assert all(u.student_name for u in units)

    submitted = [u for u in units if u.is_submitted]
    assert len(submitted) == 36
    assert all(u.primary_url for u in submitted)

    unsubmitted = [u for u in units if not u.is_submitted]
    assert len(unsubmitted) == 1
    assert unsubmitted[0].missing is True


def test_submission_with_an_attachment_is_representable(client):
    body = load("submissions")["body"]
    with_attachments = [s for s in body if s.get("attachments")]
    assert with_attachments, "expected at least one attachment in the fixture"
    att = with_attachments[0]["attachments"][0]
    assert set(att) >= {"id", "url", "display_name", "content-type"}


def test_single_submission_include_is_sent(client):
    body = load("submission_single")["body"]
    c = client([FakeResponse(200, body)])
    result = c.get("/courses/3130/assignments/46805/submissions/1?include[]=attachments")
    assert result["id"]


def test_grouped_flag_is_passed_as_a_query_parameter(client):
    c = client([FakeResponse(200, [])])
    c.get_submissions(3130, 46805, grouped=True)
    assert "grouped=true" in c.session.requests[0]["url"]
    assert "include[]=group" in c.session.requests[0]["url"]


def test_missing_assignment_raises_not_found(client):
    body = load("assignment_missing")
    c = client([FakeResponse(body["status"], body["body"])])
    with pytest.raises(NotFoundError):
        c.get_assignment(3130, 99999999)


def test_probe_returns_the_current_user(client):
    c = client([FakeResponse(200, load("users_self")["body"])])
    assert c.probe()["id"]


# ------------------------------------------------------------- download

def test_download_streams_to_disk(client, tmp_path):
    c = client()
    c.session.payload = b"pdf-bytes" * 500
    target = tmp_path / "file.pdf"

    written = c.download("https://example.instructure.com/files/1/download", target)
    assert written == len(c.session.payload)
    assert target.read_bytes() == c.session.payload


def test_download_enforces_the_size_ceiling(client, tmp_path):
    c = client()
    c.session.payload = b"x" * 5000
    with pytest.raises(CanvasError, match="exceeded"):
        c.download("https://example.instructure.com/files/1/download",
                   tmp_path / "f.pdf", max_bytes=100)


def test_download_failure_is_reported(client, tmp_path, monkeypatch):
    c = client()

    class _Bad:
        status_code = 500
        ok = False
        content = b""
        text = "server error"

    monkeypatch.setattr(c.session, "get", lambda *a, **k: _Bad())
    with pytest.raises(CanvasError, match="500"):
        c.download("https://example.instructure.com/files/1/download", tmp_path / "f")


# ---------------------------------------------------------------- grade

def test_grade_builds_a_rubric_assessment_payload(client):
    c = client([FakeResponse(200, {"id": 1, "score": 18})])
    c.grade(3130, 46805, 7,
            posted_grade=18.0,
            rubric_assessment={"_753": {"points": 6, "comments": "because"}},
            comment="Overall")
    body = c.session.requests[0]["json"]
    assert body["posted_grade"] == "18.0"
    assert body["rubric_assessment"]["_753"]["points"] == 6
    assert body["comment"]["text_comment"] == "Overall"
    assert c.session.requests[0]["method"] == "PUT"
    assert c.session.requests[0]["url"].endswith("/submissions/7")


def test_grade_refuses_an_empty_payload(client):
    c = client([FakeResponse(200, {})])
    with pytest.raises(CanvasError, match="nothing to publish"):
        c.grade(3130, 46805, 7)


def test_grade_accepts_a_prebuilt_comment_object(client):
    c = client([FakeResponse(200, {})])
    c.grade(3130, 46805, 7, comment={"text_comment": "Prebuilt"})
    assert c.session.requests[0]["json"]["comment"] == {"text_comment": "Prebuilt"}


def test_grade_can_excuse_without_a_score(client):
    c = client([FakeResponse(200, {"excused": True})])
    c.grade(3130, 46805, 7, excuse=True)
    assert c.session.requests[0]["json"]["excuse"] is True


# --------------------------------------------------------- misc guards

def test_an_unscripted_request_fails_loudly_rather_than_silently(client):
    c = client([])   # empty script
    with pytest.raises(AssertionError, match="unscripted request"):
        c.get("/courses")


def test_base_url_trailing_slash_is_normalised():
    c = CanvasClient("https://x.instructure.com/", "t")
    assert c._url("/courses") == "https://x.instructure.com/api/v1/courses"


def test_absolute_urls_are_passed_through(client):
    c = client([FakeResponse(200, {"ok": 1})])
    c.request("GET", "https://elsewhere.example/thing")
    assert c.session.requests[0]["url"] == "https://elsewhere.example/thing"


def test_next_page_urls_are_not_prefixed_twice(client):
    """A next-page URL is already absolute; prefixing it corrupts the path."""
    nxt = "https://example.instructure.com/api/v1/courses?page=2"
    c = client([FakeResponse(200, [{"id": 1}], headers={"Link": f'<{nxt}>; rel="next"'}),
                FakeResponse(200, [{"id": 2}])])
    list(c.paginate("/courses"))
    assert c.session.requests[1]["url"] == nxt
    assert "/api/v1https" not in c.session.requests[1]["url"]


def test_a_self_referential_next_link_does_not_loop_forever(client):
    same = "https://example.instructure.com/api/v1/courses?page=1"
    c = client([FakeResponse(200, [{"id": 1}], headers={"Link": f'<{same}>; rel="next"'})])
    got = list(c.paginate("/courses"))
    # The duplicate page is fetched and then discarded rather than yielded.
    assert len(got) == 1
    assert len(c.session.requests) == 2
    assert got.count({"id": 1}) == 1