"""Sidecar HTTP API.

The Electron renderer talks only to this. It is the boundary that keeps the UI
simple: every long operation runs as a background job whose events stream back
over SSE, so the wizard can show live progress without polling.

Deliberately dependency-free (http.server + urllib) so the sidecar starts with
nothing but the standard library, which matters when it is bundled for a
faculty machine that has no Python.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import config
from .canvas.client import CanvasClient, CanvasError
from .canvas.collect import bootstrap_run, collect_sources
from .canvas.spec import manual_only_criteria
from .grading.runner import Grader, OpenCodeServer
from .models import SubmissionUnit
from .report.render import render_report, report_filename
from .store import Store
from .config import NEWLINE as _NL

# How much agent output to keep per submission for the live log panel.
AGENT_LOG_LINES = 400

CODE_ROOT = Path(__file__).resolve().parents[1]

# The Electron renderer is served from here rather than from file://, because
# Chromium refuses to load ES modules over file:// (opaque origin, so every
# import is cross-origin and fails silently).
RENDERER_DIR = Path(os.environ.get("GRADING_PIPELINE_RENDERER", "")) if os.environ.get(
    "GRADING_PIPELINE_RENDERER") else None

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".woff": "font/woff",
}


def serve_static(rel: str) -> tuple[bytes, str] | None:
    """Read a renderer asset, refusing anything outside the renderer directory."""
    if RENDERER_DIR is None:
        return None
    clean = rel.lstrip("/") or "index.html"
    if clean.endswith("/"):
        clean += "index.html"
    target = (RENDERER_DIR / clean).resolve()
    if not str(target).startswith(str(RENDERER_DIR.resolve())) or not target.is_file():
        return None
    ctype = STATIC_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return target.read_bytes(), ctype


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------------ events

class EventBus:
    """Fan-out of run events to every connected SSE client."""

    def __init__(self, store: Store):
        self.store = store
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data, "ts": _now()}
        with self._lock:
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subs.discard(q)


def _now() -> str:
    from .models import utcnow
    return utcnow()


# ------------------------------------------------------------------- state

@dataclass
class AppState:
    store: Store
    bus: EventBus
    settings: config.Settings = field(default_factory=config.Settings.load)
    server: OpenCodeServer | None = None
    catalogue: dict[str, list[str]] = field(default_factory=dict)
    choice: Any = None
    jobs: dict[str, threading.Thread] = field(default_factory=dict)
    job_errors: dict[str, str] = field(default_factory=dict)
    controls: dict[str, "JobControl"] = field(default_factory=dict)
    # Ring buffer of recent agent output per submission, for the live log panel.
    agent_logs: dict[int, list[str]] = field(default_factory=dict)
    # opencode session id -> submission row, so its event stream can be routed.
    session_rows: dict[str, int] = field(default_factory=dict)

    def client(self) -> CanvasClient:
        self.settings.check_canvas()
        return CanvasClient(self.settings.canvas_base_url, self.settings.canvas_api_token)

    def record_agent_output(self, row_id: int, text: str) -> None:
        """Keep a bounded tail of one submission's agent output.

        Ring buffer rather than a growing list: a long agent run would otherwise
        grow this without limit for every submission in a 150-row cohort.
        """
        lines = self.agent_logs.setdefault(row_id, [])
        lines.extend(text.splitlines())
        if len(lines) > AGENT_LOG_LINES:
            del lines[: len(lines) - AGENT_LOG_LINES]

    def _agent_log_line(self, ev: dict[str, Any]) -> tuple[int, str] | None:
        """Turn one opencode event into a line for the live log panel.

        Written against the event schema opencode actually emits rather than a
        guess: every event carries properties.sessionID, text arrives as
        message.part.updated with part.type "text", tool calls as
        message.part.updated with part.type "tool", and the turn ends with
        session.idle. Anything unrecognised is skipped rather than rendered, so a
        schema change cannot flood the panel with noise.
        """
        props = ev.get("properties") or {}
        sid = props.get("sessionID")
        if not sid:
            return None
        row_id = self.session_rows.get(sid)
        if row_id is None:
            return None
        kind = ev.get("type") or ""
        if kind == "message.part.updated":
            part = props.get("part") or {}
            ptype = part.get("type")
            if ptype == "tool":
                return row_id, f"-> {part.get('tool') or 'tool'}"
            if ptype == "text":
                text = " ".join((part.get("text") or "").split())
                if text:
                    return row_id, text[:240]
            return None
        if kind == "session.idle":
            return row_id, "turn complete"
        if kind == "session.status":
            status = (props.get("status") or {}).get("type")
            return (row_id, f"status: {status}") if status else None
        return None

    def start_agent_log(self, srv: OpenCodeServer) -> threading.Event:
        """Follow opencode's event stream for the duration of a grading job.

        `OpenCodeServer.events` existed but was never called, so there was no way
        to see the agent work.

        The stream is supervised rather than opened once. A single open is not
        enough: the connection can fail outright, and the Grader deliberately
        restarts opencode when a provider refuses a large output budget, which
        moves the server onto a new port and leaves the old stream pointing at
        nothing. Without a reconnect the log goes permanently quiet after the
        first such event, which looks exactly like a bug in the feature.

        Deltas are deliberately not logged: a reasoning model streams thousands
        of them and the panel would be unreadable. Tool calls and completed text
        parts are the useful signal.
        """
        import queue as _queue

        stop = threading.Event()

        def supervise() -> None:
            backoff = 0.5
            while not stop.is_set():
                out: _queue.Queue = _queue.Queue()
                try:
                    srv.events(stop, out)
                except Exception as exc:
                    self.bus.emit("agent.log", {
                        "message": f"agent log unavailable: {type(exc).__name__}"})
                    time.sleep(backoff)
                    continue
                while not stop.is_set():
                    try:
                        ev = out.get(timeout=0.5)
                    except Exception:
                        continue
                    if ev.get("__error__"):
                        # The stream ended. Back off and reopen, which picks up
                        # both a transient failure and a restarted server.
                        break
                    try:
                        line = self._agent_log_line(ev)
                    except Exception:
                        continue
                    if line is None:
                        continue
                    row_id, text = line
                    self.record_agent_output(row_id, text)
                    self.bus.emit("agent.log", {"submission_row_id": row_id, "line": text})
                if stop.is_set():
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

        threading.Thread(target=supervise, name="agent-log", daemon=True).start()
        return stop

    def opencode_env(self) -> dict[str, str]:
        """Environment for the opencode process, including any configured key."""
        extra: dict[str, str] = {}
        key = self.settings.openrouter_api_key
        if key:
            # opencode reads this directly, so it works regardless of how it
            # resolves its data directory on a given platform.
            extra["OPENROUTER_API_KEY"] = key
            data_home = self.settings.data / "opencode-home"
            try:
                config.write_opencode_auth(key, data_home)
                extra["XDG_DATA_HOME"] = str(data_home)
            except OSError:
                # The env var alone is enough; the auth file is belt and braces.
                pass
        return extra

    def stop_opencode(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None
            self.catalogue = {}

    def ensure_opencode(self) -> OpenCodeServer:
        if self.server is None:
            srv = OpenCodeServer()
            srv.start(self.opencode_env())
            self.server = srv
            try:
                self.catalogue = srv.catalogue()
            except Exception:
                self.catalogue = {}
        return self.server


# -------------------------------------------------------------------- jobs

class JobControl:
    """Pause, resume and stop signals for one background job.

    A 150-submission collection clones every repository and downloads every
    attachment. Faculty need to be able to stop that, and to hold it while they
    look at something, without killing the sidecar and losing their run.

    Pausing waits *between* units rather than interrupting one: a half-cloned
    repository or a half-written Markdown file is worse than finishing the
    current student. So `wait_if_paused` blocks here, not inside the work.
    """

    def __init__(self) -> None:
        self.resume = threading.Event()
        self.resume.set()          # set means "running"
        self.stopped = threading.Event()

    @property
    def paused(self) -> bool:
        return not self.resume.is_set()

    def pause(self) -> None:
        self.resume.clear()

    def request_stop(self) -> None:
        self.stopped.set()
        # A stop must also release a paused job, or it waits forever.
        self.resume.set()

    def wait_if_paused(self) -> bool:
        """Block while paused. Returns False when the job was asked to stop."""
        while not self.resume.wait(timeout=0.25):
            if self.stopped.is_set():
                return False
        return not self.stopped.is_set()


def run_job(state: AppState, name: str, fn: Callable[[Any], Any]) -> None:
    """Start a background job, replacing any previous run of the same name."""

    def runner(control: JobControl) -> None:
        state.job_errors.pop(name, None)
        try:
            fn(control)
        except Exception as exc:
            state.job_errors[name] = f"{type(exc).__name__}: {exc}"
            state.bus.emit("job.error", {"job": name, "message": f"{type(exc).__name__}: {exc}"})
            state.bus.emit("job.done", {"job": name, "ok": False})
            traceback.print_exc()
        finally:
            state.controls.pop(name, None)

    control = JobControl()
    state.controls[name] = control
    t = threading.Thread(target=runner, args=(control,), name=f"job-{name}", daemon=True)
    state.jobs[name] = t
    t.start()


def job_state(state: AppState, name: str) -> dict[str, Any]:
    t = state.jobs.get(name)
    control = state.controls.get(name)
    if name in state.job_errors:
        return {"job": name, "status": "error", "error": state.job_errors[name]}
    if t is None or not t.is_alive():
        return {"job": name, "status": "done" if t is not None else "idle",
                "paused": False}
    status = "paused" if control and control.paused else "running"
    return {"job": name, "status": status, "paused": status == "paused"}


# ------------------------------------------------------------- operations

def _unit_from_row(row: dict[str, Any]) -> SubmissionUnit:
    return SubmissionUnit(
        canvas_submission_id=int(row["canvas_submission_id"]), user_id=int(row["user_id"]),
        student_name=row["student_name"], sortable_name=row["sortable_name"] or "",
        group_id=row["group_id"], group_name=row["group_name"],
        workflow_state=row["workflow_state"] or "unsubmitted",
        submitted_at=row["submitted_at"], existing_score=row["existing_score"],
        primary_url=row["primary_url"], missing=bool(row["missing"]))


def op_status(state: AppState) -> dict[str, Any]:
    st = state.settings
    return {
        "canvas_configured": bool(st.canvas_base_url and st.canvas_api_token),
        # Reported as a flag only. A key is never echoed back to the renderer,
        # so it cannot end up in a DOM node or a screenshot.
        "model_key_configured": bool(st.openrouter_api_key),
        "workspace": str(st.workspace),
        "data_dir": str(st.data),
        "jobs": {n: job_state(state, n) for n in
                 ("bootstrap", "collect", "grade", "render", "publish")},
    }


def op_save_settings(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    if body.get("canvas_base_url"):
        config.set_secret("canvas_base_url", str(body["canvas_base_url"]).rstrip("/"))
    if body.get("canvas_api_token"):
        config.set_secret("canvas_api_token", str(body["canvas_api_token"]))
    if "openrouter_api_key" in body:
        key = str(body["openrouter_api_key"]).strip()
        if key:
            config.set_secret("openrouter_api_key", key)
        else:
            # An empty field means "remove the key". Ignoring it would leave a
            # user with no way to unconfigure one they pasted by mistake.
            config.delete_secret("openrouter_api_key")
    state.settings = config.Settings.load()
    # The opencode process holds the old environment, so it has to be restarted
    # before a new or removed key takes effect.
    if "openrouter_api_key" in body and state.server is not None:
        state.stop_opencode()
    return op_status(state)


def op_models(state: AppState) -> dict[str, Any]:
    srv = state.ensure_opencode()
    catalogue = state.catalogue or srv.catalogue()
    state.catalogue = catalogue
    candidates = config.candidates_from_catalogue(
        catalogue, prefer_free=state.settings.model != "no-prefer-free")
    return {
        "providers": sorted(catalogue.keys()),
        "counts": {k: len(v) for k, v in catalogue.items()},
        "free_count": len(config.rank_free_models(catalogue.get("openrouter", []))),
        "selected": str(state.choice) if state.choice else None,
        "candidates": [{"id": f"{c.provider}/{c.model}", "provider": c.provider,
                        "model": c.model, "tier": c.tier} for c in candidates[:40]],
    }


def op_select_model(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    catalogue = state.catalogue or state.ensure_opencode().catalogue()
    state.catalogue = catalogue
    prefer = body.get("model") != "no-prefer-free"
    model = body.get("model") or None
    if model == "no-prefer-free":
        model = None
    try:
        choice = config.pick_model(catalogue, model, prefer_free=prefer)
    except RuntimeError as exc:
        raise ApiError(str(exc), 400) from exc
    if choice is None:
        raise ApiError("opencode reports no usable models. Add a key with "
                       "`opencode auth login`, then retry.", 400)
    choice.max_output_tokens = int(body.get("output_budget") or config.DEFAULT_OUTPUT_BUDGET)
    state.choice = choice
    state.settings.model = str(choice)
    return {"selected": str(choice), "tier": choice.tier,
            "output_budget": choice.max_output_tokens}


def op_courses(state: AppState) -> dict[str, Any]:
    client = state.client()
    out = []
    for c in client.list_courses():
        # `term` arrives as an object from Canvas but a bare name from some
        # gateways and proxies; accept either rather than raising mid-list.
        raw_term = c.get("term")
        term = raw_term.get("name") if isinstance(raw_term, dict) else raw_term
        out.append({"id": c.get("id"), "name": c.get("name"),
                    "term": term, "code": c.get("course_code"),
                    "workflow_state": c.get("workflow_state")})
    return {"courses": out}


def op_assignments(state: AppState, course_id: int) -> dict[str, Any]:
    client = state.client()
    out = []
    for a in client.request(
            "GET", f"/courses/{course_id}/assignments?per_page=100&include[]=rubric"):
        rubric = a.get("rubric") or []
        out.append({
            "id": a.get("id"), "name": a.get("name"),
            "points_possible": a.get("points_possible"),
            "published": a.get("published"),
            "due_at": a.get("due_at"),
            "criteria": len([c for c in rubric if not c.get("ignore_for_scoring")]),
            "has_rubric": bool(rubric),
            "needs_grading": a.get("needs_grading_count"),
            "group": bool(a.get("group_category_id")),
            "submission_types": a.get("submission_types") or [],
        })
    return {"assignments": out}


def op_bootstrap(state: AppState, body: dict[str, Any]) -> dict[str, Any]:
    course_id = int(body["course_id"])
    assignment_id = int(body["assignment_id"])
    store, bus = state.store, state.bus

    def work(_control: JobControl) -> None:
        bus.emit("job.start", {"job": "bootstrap"})
        client = state.client()
        client.probe()
        res = bootstrap_run(client, store, Path(body.get("assignments_dir") or "assignments"),
                            course_id, assignment_id,
                            model=str(state.choice) if state.choice else None,
                            emit=lambda e, d: bus.emit(e, d))
        bus.emit("run.ready", {"run_id": res.run_id, "title": res.spec.title,
                               "criteria": len(res.spec.criteria),
                               "points": res.spec.points_possible,
                               "submissions": len(res.units),
                               "warnings": res.warnings})
        bus.emit("job.done", {"job": "bootstrap", "ok": True})

    run_job(state, "bootstrap", work)
    return {"job": "bootstrap"}


def op_run(state: AppState, run_id: int) -> dict[str, Any]:
    run = state.store.get_run(run_id)
    spec = run["spec"]
    return {
        "run_id": run_id,
        "title": run["title"],
        "status": run["status"],
        "model": run.get("model"),
        "points_possible": spec.points_possible,
        "group": spec.is_group,
        "criteria": [{"id": c.canvas_criterion_id, "ordinal": c.ordinal,
                      "title": c.title, "points": c.points, "scale": c.scale}
                     for c in spec.criteria if not c.ignore_for_scoring],
        "progress": state.store.progress(run_id),
    }


def op_submissions(state: AppState, run_id: int) -> dict[str, Any]:
    rows = state.store.submissions_for_run(run_id)
    out = []
    for r in rows:
        sc = state.store.best_scorecard(int(r["id"]))
        report = state.store.report_for(int(r["id"]))
        out.append({
            "row_id": int(r["id"]),
            "student": r["student_name"],
            "group": r["group_name"],
            "status": r["status"],
            "workflow_state": r["workflow_state"],
            "missing": bool(r["missing"]),
            "existing_score": r["existing_score"],
            "primary_url": r["primary_url"],
            "error": r["error"],
            "score": (sc.total_earned if sc else None),
            "published": state.store.already_published(int(r["id"])),
            "report_path": report["path"] if report else None,
            "flags": [f.model_dump() for f in (sc.flags if sc else [])],
        })
    return {"submissions": out}


def op_collect(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    store, bus = state.store, state.bus

    def work(control: JobControl) -> None:
        bus.emit("job.start", {"job": "collect"})
        run = store.get_run(run_id)
        spec = run["spec"]
        client = state.client()
        run_dir = state.settings.run_dir(spec.course_id, spec.assignment_id)
        rows = store.submissions_for_run(run_id)
        if body.get("only_pending"):
            rows = [r for r in rows if r["status"] == "pending"]
        if body.get("limit"):
            # Useful for trying the pipeline on a handful before committing to
            # a full cohort of clones.
            rows = rows[: int(body["limit"])]
        done = failed = stopped = 0
        for i, row in enumerate(rows, 1):
            if not control.wait_if_paused():
                stopped = len(rows) - i + 1
                bus.emit("collect.stopped", {"run_id": run_id, "remaining": stopped})
                break
            unit = _unit_from_row(row)
            rid = int(row["id"])
            if not unit.is_submitted:
                store.set_submission_status(rid, "no_submission")
                done += 1
                continue
            store.set_submission_status(rid, "collecting")
            try:
                found = collect_sources(client, spec, unit, rid, store, run_dir,
                                        emit=lambda e, d: bus.emit(e, d))
                store.set_submission_status(rid, "collected")
                done += 1
            except Exception as exc:
                store.set_submission_status(rid, "collect_failed", str(exc)[:300])
                failed += 1
                bus.emit("collect.error", {"submission_row_id": rid,
                                           "student": unit.display_name,
                                           "message": str(exc)[:200]})
            bus.emit("collect.progress", {
                "run_id": run_id, "index": i, "total": len(rows),
                "student": unit.display_name,
                # The authoritative counters travel with the event. The renderer
                # reads them from the run object, so without this the meters only
                # moved when something else happened to refresh the run.
                "progress": store.progress(run_id),
            })
        if not stopped:
            store.set_run_status(run_id, "collected")
        bus.emit("job.done", {"job": "collect", "ok": not stopped, "stopped": bool(stopped),
                              "message": (f"stopped with {stopped} remaining" if stopped
                                          else f"collected {done}, failed {failed}")})

    run_job(state, "collect", work)
    return {"job": "collect"}


def op_grade(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    store, bus = state.store, state.bus
    stub = bool(body.get("stub"))
    workers = int(body.get("workers") or state.settings.workers)

    def work(control: JobControl) -> None:
        bus.emit("job.start", {"job": "grade", "stub": stub, "workers": workers})
        run = store.get_run(run_id)
        spec = run["spec"]

        if stub:
            _grade_stub(state, run_id, spec, workers, body.get("row_ids"))
            bus.emit("job.done", {"job": "grade", "ok": True, "stub": True,
                                  "progress": store.progress(run_id)})
            return

        srv = state.ensure_opencode()
        model = str(state.choice) if state.choice else run.get("model")
        if not model:
            raise ApiError("No model selected.", 409)
        budget = state.choice.max_output_tokens if state.choice else config.DEFAULT_OUTPUT_BUDGET
        def emit_agent(event: str, data: dict[str, Any]) -> None:
            if event == "agent.start":
                sid = str(data.get("session_id") or "")
                rid = data.get("submission_row_id")
                if sid and rid is not None:
                    state.session_rows[sid] = int(rid)
                    state.record_agent_output(int(rid),
                                              f"agent started ({data.get('model') or model}, "
                                              f"attempt {data.get('attempt')})")
            bus.emit(event, data)

        log_stop = state.start_agent_log(srv)
        grader = Grader(srv, store, spec, run_id,
                        state.settings.run_dir(spec.course_id, spec.assignment_id),
                        CODE_ROOT, python_exe=_python_exe(),
                        emit=emit_agent, model=model, budget=budget)
        rows = store.submissions_with_status(
            run_id, ("collected", "failed", "needs_review", "grading"))
        if body.get("only_pending"):
            rows = [r for r in rows if r["status"] == "collected"]
        # Grade Selected. An explicit selection is honoured exactly, so grading
        # "these five" cannot quietly turn into grading the rest of the cohort.
        wanted = body.get("row_ids")
        if wanted is not None:
            keep = {int(x) for x in wanted}
            rows = [r for r in rows if int(r["id"]) in keep]
        if body.get("limit"):
            rows = rows[: int(body["limit"])]

        from concurrent.futures import ThreadPoolExecutor, as_completed
        graded = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {}
            for row in rows:
                unit = _unit_from_row(row)
                # wait_if_paused before submitting: pausing must stop new work
                # starting rather than only stopping completions.
                if not control.wait_if_paused():
                    break
                futures[pool.submit(grader.grade, int(row["id"]), unit,
                                    store.sources_for(int(row["id"])))] = unit
            if not futures:
                bus.emit("job.done", {"job": "grade", "ok": True, "graded": 0,
                                      "stopped": True})
                return
            for fut in as_completed(futures):
                unit = futures[fut]
                try:
                    res = fut.result()
                    graded += 1 if res.status == "graded" else 0
                    bus.emit("grade.progress", {
                        "run_id": run_id, "student": unit.display_name,
                        "status": res.status, "earned": res.earned,
                        "possible": res.possible,
                        "progress": store.progress(run_id)})
                except Exception as exc:
                    bus.emit("grade.error", {"student": unit.display_name,
                                             "message": str(exc)[:200]})
        store.set_run_status(run_id, "graded")
        log_stop.set()
        bus.emit("job.done", {"job": "grade", "ok": True, "graded": graded,
                              "of": len(rows)})

    run_job(state, "grade", work)
    return {"job": "grade", "stub": stub}


def _grade_stub(state: AppState, run_id: int, spec: Any, workers: int,
                row_ids: list[int] | None = None) -> None:
    from .stub import stub_scorecard
    from .models import validate_scorecard

    store, bus = state.store, state.bus
    rows = store.submissions_with_status(run_id, ("collected", "pending", "needs_review"))
    # The stub path honours a selection too. It used to ignore it, so Grade
    # Selected silently graded the whole cohort whenever stub grading was on,
    # which is exactly the mode used for trying the pipeline out.
    if row_ids is not None:
        keep = {int(x) for x in row_ids}
        rows = [r for r in rows if int(r["id"]) in keep]
    for i, row in enumerate(rows, 1):
        rid = int(row["id"])
        unit = _unit_from_row(row)
        store.set_submission_status(rid, "grading")
        if not unit.is_submitted:
            store.set_submission_status(rid, "no_submission")
            continue
        sources = store.sources_for(rid)
        sc = stub_scorecard(spec, unit.display_name, sources)
        result = validate_scorecard(sc, spec)
        attempt = store.start_attempt(rid, "stub")
        store.save_scorecard(rid, attempt, sc, "valid" if result.ok else "invalid",
                             result.earned, result.possible, result.issues)
        store.finish_attempt(attempt, "ok")
        store.set_submission_status(rid, "graded")
        bus.emit("grade.progress", {"run_id": run_id, "student": unit.display_name,
                                    "status": "graded", "earned": result.earned,
                                    "possible": result.possible, "stub": True})
    store.set_run_status(run_id, "graded")


def op_render(state: AppState, run_id: int) -> dict[str, Any]:
    store, bus = state.store, state.bus

    def work(_control: JobControl) -> None:
        bus.emit("job.start", {"job": "render"})
        run = store.get_run(run_id)
        spec = run["spec"]
        spec.policy["_manual_only_ids"] = (
            manual_only_criteria(spec) if spec.policy.get("manual_only_criteria") else [])
        out_dir = state.settings.run_dir(spec.course_id, spec.assignment_id) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        rows = store.submissions_with_status(run_id, ("graded", "needs_review"))
        for row in rows:
            md, _path = render_one(state, int(row["id"]))
            if md:
                written += 1
        bus.emit("job.done", {"job": "render", "ok": True, "written": written,
                              "dir": str(out_dir)})

    run_job(state, "render", work)
    return {"job": "render"}


def render_one(state: AppState, row_id: int) -> tuple[str, str]:
    """Render one report from its scorecard and persist it.

    Shared by the render job and by report reads. A scorecard is the source of
    truth and the Markdown is a pure function of it, so a missing report file
    means the file was never written, not that there is nothing to show. Reading
    a report regenerates it rather than returning an empty string, which is what
    left the review page blank for anyone who graded and moved on without
    pressing Render.
    """
    store = state.store
    row = store.submission_row(row_id)
    sc = store.best_scorecard(row_id)
    if sc is None:
        return "", ""
    run = store.get_run(int(row["run_id"]))
    spec = run["spec"]
    spec.policy["_manual_only_ids"] = (
        manual_only_criteria(spec) if spec.policy.get("manual_only_criteria") else [])
    out_dir = state.settings.run_dir(spec.course_id, spec.assignment_id) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(spec, sc, _unit_from_row(row), sources=store.sources_for(row_id),
                       model=sc.model or run.get("model") or "",
                       prompt_version=sc.prompt_version)
    path = out_dir / report_filename(_unit_from_row(row))
    path.write_text(md, encoding="utf-8", newline=_NL)
    store.save_report(row_id, str(path))
    return md, str(path)


CONTROLLABLE = ("collect", "grade")


def op_job_control(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Pause, resume or stop a long-running job.

    Only collect and grade are controllable. Bootstrap and publish are short, and
    interrupting publish halfway would leave Canvas and the store disagreeing
    about what was written.
    """
    action = str(body.get("action") or "")
    job = str(body.get("job") or "")
    if action not in ("pause", "resume", "stop"):
        raise ApiError(f"unknown action: {action}", 400)
    if job not in CONTROLLABLE:
        raise ApiError(f"{job or 'that job'} cannot be paused or stopped", 400)
    control = state.controls.get(job)
    if control is None:
        raise ApiError(f"{job} is not running", 409)
    if action == "pause":
        control.pause()
    elif action == "resume":
        control.resume.set()
    else:
        control.request_stop()
    state.bus.emit("job.control", {"job": job, "action": action})
    return {"job": job, "action": action, "state": job_state(state, job)}


def op_agent_log(state: AppState, row_id: int) -> dict[str, Any]:
    """Recent agent output for one submission, for the live log panel.

    Polled repeatedly while a run is still bootstrapping, so an unknown row is
    an empty log rather than a 500. The student name is a convenience; the lines
    are keyed by row id and are what the panel needs.
    """
    # Route captures arrive as strings. The log is keyed by int, so without
    # this every lookup misses and the panel is permanently empty.
    row_id = int(row_id)
    try:
        student = state.store.submission_row(row_id)["student_name"]
    except KeyError:
        student = ""
    return {"row_id": row_id, "student": student,
            "lines": state.agent_logs.get(row_id, [])}


def op_report(state: AppState, row_id: int) -> dict[str, Any]:
    row = state.store.submission_row(row_id)
    rep = state.store.report_for(row_id)
    markdown, path = "", None
    if rep and Path(rep["path"]).exists():
        markdown = Path(rep["path"]).read_text(encoding="utf-8")
        path = rep["path"]
    else:
        # No report on disk yet. Render it from the scorecard rather than
        # handing the renderer an empty body, which is indistinguishable from a
        # broken report.
        markdown, path = render_one(state, row_id)
    sc = state.store.best_scorecard(row_id)
    return {
        "row_id": row_id,
        "student": row["student_name"],
        "markdown": markdown,
        "scorecard": sc.model_dump(mode="json") if sc else None,
        "path": path,
    }


def op_save_report(state: AppState, row_id: int, body: dict[str, Any]) -> dict[str, Any]:
    text = body.get("markdown")
    if text is None:
        raise ApiError("markdown is required")
    rep = state.store.report_for(row_id)
    if not rep:
        raise ApiError("no report to save", 404)
    Path(rep["path"]).write_text(text, encoding="utf-8", newline=_NL)
    with state.store.connect() as db:
        db.execute("UPDATE reports SET edited=1 WHERE submission_row_id=?", (row_id,))
    return {"row_id": row_id, "saved": True, "path": rep["path"]}


def op_publish(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    """Plan a publish, or execute it when `confirm` is set."""
    store, bus = state.store, state.bus
    run = store.get_run(run_id)
    spec = run["spec"]
    rows = store.submissions_with_status(run_id, ("graded",))
    selected = body.get("row_ids") or [int(r["id"]) for r in rows]
    selected = set(int(x) for x in selected)

    plan = []
    for row in rows:
        if int(row["id"]) not in selected:
            continue
        sc = store.best_scorecard(int(row["id"]))
        if sc is None:
            continue
        unit = _unit_from_row(row)
        payload, total = _canvas_payload(spec, sc)
        plan.append({
            "row_id": int(row["id"]), "student": unit.display_name,
            "existing": row["existing_score"], "new": total,
            "blocking": [f.code for f in sc.flags if f.severity == "block"],
            "payload": payload,
        })

    if not body.get("confirm"):
        return {"mode": "plan", "entries": plan,
                "message": f"{len(plan)} submission(s) would be written to Canvas."}

    def work(_control: JobControl) -> None:
        bus.emit("job.start", {"job": "publish"})
        client = state.client()
        ok = bad = 0
        for entry in plan:
            row = store.submission_row(entry["row_id"])
            unit = _unit_from_row(row)
            try:
                resp = client.grade(spec.course_id, spec.assignment_id, unit.user_id,
                                    posted_grade=entry["new"],
                                    rubric_assessment=entry["payload"])
                store.record_publication(entry["row_id"], entry["payload"],
                                         json.dumps(resp)[:400] if resp else None)
                ok += 1
                bus.emit("publish.progress", {"student": entry["student"], "ok": True})
            except Exception as exc:
                bad += 1
                bus.emit("publish.progress", {"student": entry["student"], "ok": False,
                                              "message": str(exc)[:200]})
        bus.emit("job.done", {"job": "publish", "ok": True, "published": ok,
                              "failed": bad})

    run_job(state, "publish", work)
    return {"mode": "executing", "entries": len(plan)}


def _canvas_payload(spec: Any, scorecard: Any) -> tuple[dict[str, Any], float]:
    assessment: dict[str, Any] = {}
    total = 0.0
    for c in spec.criteria:
        if c.ignore_for_scoring:
            continue
        s = scorecard.score_for(c.canvas_criterion_id)
        if s is None:
            continue
        total += s.points_earned
        comment = "\n\n".join(x for x in (s.justification, *s.evidence) if x).strip()
        assessment[c.canvas_criterion_id] = {"points": s.points_earned,
                                             "comments": comment[:500]}
    return assessment, total


def _python_exe() -> str:
    import sys
    return sys.executable


# ------------------------------------------------------------------ router

def _invoke(handler: Callable[..., Any], state: "AppState",
            ctx: dict[str, Any]) -> Any:
    """Call a route handler, binding arguments by name.

    Route parameters come from the path pattern; a parameter named `body`
    receives the parsed request body. Handlers that take only the state are
    called with the state alone, so read-only endpoints stay terse.
    """
    import inspect

    try:
        names = list(inspect.signature(handler).parameters)[1:]
    except (TypeError, ValueError):
        return handler(state, ctx.get("body", {}))

    if not names:
        return handler(state)

    kwargs = {}
    for name in names:
        if name == "body":
            kwargs[name] = ctx.get("body") or {}
        elif name in ctx:
            kwargs[name] = ctx[name]
        else:
            kwargs[name] = ctx.get("body") or {}
    return handler(state, **kwargs)


Route = Callable[..., Any]


@dataclass
class Route_:
    method: str
    pattern: str
    handler: Route


def _health(state: "AppState", body: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "service": "grading-pipeline"}


def _course_assignments(state: AppState, course_id: int) -> dict[str, Any]:
    return op_assignments(state, int(course_id))


def _get_run(state: AppState, run_id: int) -> dict[str, Any]:
    return op_run(state, int(run_id))


def _get_submissions(state: AppState, run_id: int) -> dict[str, Any]:
    return op_submissions(state, int(run_id))


def _start_collect(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    return op_collect(state, int(run_id), body)


def _start_grade(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    return op_grade(state, int(run_id), body)


def _start_render(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    return op_render(state, int(run_id))


def _start_publish(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    return op_publish(state, int(run_id), body)


def _start_control(state: AppState, run_id: int, body: dict[str, Any]) -> dict[str, Any]:
    return op_job_control(state, run_id, body)


def _get_agent_log(state: AppState, row_id: int) -> dict[str, Any]:
    return op_agent_log(state, row_id)


def _get_report(state: AppState, row_id: int) -> dict[str, Any]:
    return op_report(state, int(row_id))


def _put_report(state: AppState, row_id: int, body: dict[str, Any]) -> dict[str, Any]:
    return op_save_report(state, int(row_id), body)


ROUTES: list[Route_] = [
    Route_("GET", r"^/health$", _health),
    Route_("GET", r"^/api/status$", op_status),
    Route_("POST", r"^/api/settings$", op_save_settings),
    Route_("GET", r"^/api/models$", op_models),
    Route_("POST", r"^/api/models/select$", op_select_model),
    Route_("GET", r"^/api/courses$", op_courses),
    Route_("GET", r"^/api/courses/(?P<course_id>\d+)/assignments$", _course_assignments),
    Route_("POST", r"^/api/runs$", op_bootstrap),
    Route_("GET", r"^/api/runs/(?P<run_id>\d+)$", _get_run),
    Route_("GET", r"^/api/runs/(?P<run_id>\d+)/submissions$", _get_submissions),
    Route_("POST", r"^/api/runs/(?P<run_id>\d+)/collect$", _start_collect),
    Route_("POST", r"^/api/runs/(?P<run_id>\d+)/grade$", _start_grade),
    Route_("POST", r"^/api/runs/(?P<run_id>\d+)/render$", _start_render),
    Route_("POST", r"^/api/runs/(?P<run_id>\d+)/publish$", _start_publish),
    Route_("POST", r"^/api/runs/(?P<run_id>\d+)/control$", _start_control),
    Route_("GET", r"^/api/reports/(?P<row_id>\d+)/agent-log$", _get_agent_log),
    Route_("GET", r"^/api/reports/(?P<row_id>\d+)$", _get_report),
    Route_("PUT", r"^/api/reports/(?P<row_id>\d+)$", _put_report),
]


def make_handler(state: AppState):
    import re as _re

    compiled = [(_re.compile(r.pattern), r.method, r.handler) for r in ROUTES]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter
            pass

        # ------------------------------------------------------ plumbing

        def _send(self, status: int, payload: Any, ctype: str = "application/json") -> None:
            if isinstance(payload, bytes):
                body = payload
            elif isinstance(payload, (dict, list)):
                body = json.dumps(payload, default=str).encode()
            elif isinstance(payload, str):
                body = payload.encode()
            else:
                body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204, "")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/events":
                return self._stream_events(state)
            if parsed.path in ("/", "/app", "/app/"):
                asset = serve_static("index.html")
                if asset:
                    return self._send(200, asset[0], asset[1])
            if parsed.path.startswith("/app/"):
                asset = serve_static(parsed.path[len("/app/"):])
                if asset:
                    return self._send(200, asset[0], asset[1])
                return self._send(404, {"error": "not found"})
            self._dispatch("GET", parsed)

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST", urlparse(self.path))

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT", urlparse(self.path))

        def _dispatch(self, method: str, parsed: Any) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON body"})
            for rx, m, handler in compiled:
                if m != method:
                    continue
                match = rx.match(parsed.path)
                if not match:
                    continue
                try:
                    params = {k: v for k, v in match.groupdict().items()}
                    result = _invoke(handler, state, {"body": body, **params})
                    return self._send(200, result)
                except ApiError as exc:
                    return self._send(exc.status, {"error": exc.message})
                except CanvasError as exc:
                    return self._send(502, {"error": f"Canvas: {exc}"})
                except Exception as exc:
                    traceback.print_exc()
                    return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            self._send(404, {"error": f"no route for {method} {parsed.path}"})

        def _stream_events(self, state: AppState) -> None:
            q = state.bus.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        payload = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    blob = json.dumps(payload, default=str)
                    self.wfile.write(f"data: {blob}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass
            finally:
                state.bus.unsubscribe(q)

    return Handler


def serve_test(state: AppState, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    """Start a server around a caller-supplied state. Used by the test suite."""
    httpd = ThreadingHTTPServer((host, 0), make_handler(state))
    httpd.daemon_threads = True
    bound = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, bound


def serve(port: int = 0, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, int]:
    store = Store(config.Settings.load().db_path)
    state = AppState(store=store, bus=EventBus(store))
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    bound = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, bound