"""Grading runner.

Owns the `opencode serve` process and drives one *fresh session per student*.
Freshness is deliberate: it is the structural fix for cross-student
contamination, where an agent that has seen several submissions starts writing
comparative claims ("the best of those I reviewed") into the report.

The scorecard arrives by two independent routes that must agree:
  1. the MCP tool validated it and wrote an outcome file, and
  2. the transcript recorded a `submit_scorecard` call.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import requests

from ..canvas.spec import manual_only_criteria
from ..config import budget_from_error
from ..models import (
    AssignmentSpec,
    Scorecard,
    SubmissionUnit,
    parse_scorecard,
    scorecard_json_schema,
    validate_scorecard,
)
from ..store import Store
from .mcp_server import MCP_SERVER_NAME, EXPOSED_TOOL_NAME
from .prompt import PROMPT_VERSION, build_system_prompt, build_task_prompt, retry_prompt
from ..config import NEWLINE as _NL

Emitter = Callable[[str, dict[str, Any]], None]

SYSTEM_ENV = {
    # A grader session must not inherit the operator's personal agent config.
    "OPENCODE_DISABLE_CLAUDE_CODE": "true",
    "OPENCODE_DISABLE_AUTOUPDATE": "true",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
    "OPENCODE_DISABLE_MODELS_FETCH": "false",
}


class RunnerError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class OpenCodeServer:
    """A headless opencode process, reachable over its HTTP API."""

    binary: str = "opencode"
    model: str = ""
    hostname: str = "127.0.0.1"
    port: int = field(default_factory=free_port)
    timeout: int = 60
    # A metered or free key can refuse a large max_tokens even when small
    # requests succeed, so the ceiling measured at preflight is enforced here
    # rather than letting the agent request its 32k default and get a 402.
    max_output_tokens: int = 0
    _proc: subprocess.Popen | None = None
    _base: str = ""

    def __post_init__(self) -> None:
        # The packaged app ships its own opencode and points at it through the
        # environment, so a user is not required to install one. A developer's
        # own opencode is used when nothing is specified.
        override = os.environ.get("GRADING_PIPELINE_OPENCODE", "").strip()
        if override:
            self.binary = override
        self._base = f"http://{self.hostname}:{self.port}"

    def start(self, extra_env: dict[str, str] | None = None) -> None:
        if not Path(self.binary).is_file() and shutil.which(self.binary) is None:
            raise RunnerError(
                f"'{self.binary}' is not on PATH. Install opencode, or set "
                "GRADING_PIPELINE_OPENCODE to its path."
            )
        env = {**os.environ, **SYSTEM_ENV, **(extra_env or {})}
        if self.max_output_tokens:
            env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(self.max_output_tokens)
        self._proc = subprocess.Popen(
            [self.binary, "serve", "--hostname", self.hostname, "--port", str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
        )
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or "")[:800] if self._proc.stderr else ""
                raise RunnerError(f"opencode serve exited immediately: {err}")
            try:
                r = requests.get(f"{self._base}/global/health", timeout=3)
                if r.ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.3)
        self.stop()
        raise RunnerError(f"opencode server did not become healthy within {self.timeout}s")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def __enter__(self) -> "OpenCodeServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---------------------------------------------------------------- API

    def url(self, path: str, directory: str | None = None) -> str:
        u = f"{self._base}{path}"
        if directory:
            u += ("&" if "?" in u else "?") + f"directory={directory}"
        return u

    def create_session(self, title: str, directory: str) -> dict[str, Any]:
        r = requests.post(self.url("/session", directory), json={"title": title}, timeout=30)
        r.raise_for_status()
        return r.json()

    def prompt(self, session_id: str, text: str, system: str | None = None,
               agent: str | None = None, model: str | None = None,
               directory: str | None = None, timeout: int = 1800) -> dict[str, Any]:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if system:
            body["system"] = system
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = {"providerID": model.split("/")[0],
                             "modelID": "/".join(model.split("/")[1:])}
        r = requests.post(self.url(f"/session/{session_id}/message", directory),
                          json=body, timeout=timeout)
        if not r.ok:
            raise RunnerError(f"prompt failed: {r.status_code} {r.text[:300]}")
        return r.json()

    def provider_error(self, response: dict[str, Any]) -> str | None:
        """The provider's own error message, if the turn failed."""
        err = ((response or {}).get("info") or {}).get("error") or {}
        data = err.get("data") or {}
        return data.get("message") or err.get("name")

    def parts(self, session_id: str, directory: str | None = None) -> list[dict[str, Any]]:
        """The session's message parts so far.

        This, not opencode's /event stream, is what the live log reads from. The
        stream proved unreliable in practice: against the same server, curl and
        requests both received only the opening frame and heartbeats while the
        turn was demonstrably running, so there was nothing to display. The REST
        endpoint returns the reasoning and text parts as they are written, which
        is the same information without depending on a long-lived connection
        that a restarted server or a dropped socket can silently kill.
        """
        try:
            r = requests.get(self.url(f"/session/{session_id}/message", directory), timeout=20)
            if not r.ok:
                return []
            payload = r.json()
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for msg in (payload if isinstance(payload, list) else [payload]):
            for part in (msg.get("parts") or []):
                pid = part.get("id") or ""
                if not pid:
                    continue
                out.append({
                    "id": pid,
                    "type": part.get("type") or "",
                    "text": part.get("text") or "",
                    "tool": part.get("tool") or "",
                    "error": (part.get("state") or {}).get("error") or {},
                })
        return out

    def events(self, stop: threading.Event, out: queue.Queue,
               directory: str | None = None) -> threading.Thread:
        def run() -> None:
            try:
                with requests.get(self.url("/event", directory), stream=True,
                                  timeout=(10, 3600)) as resp:
                    # resp.raw.stream_lines, not resp.iter_lines. requests'
                    # iter_lines sits on iter_content, which buffers instead of
                    # yielding as the socket fills: against an endpoint that never
                    # ends, that is an event stream that delivers nothing at all,
                    # forever, with no error. Verified here - curl against the
                    # same URL receives every event while requests received only
                    # the opening frame and heartbeats. urllib3's stream_lines
                    # exists for exactly this and yields per line.
                    for raw in resp.raw.stream_lines(decode_unicode=True):
                        if stop.is_set():
                            return
                        if not raw or not raw.startswith("data:"):
                            continue
                        try:
                            out.put(json.loads(raw[5:].strip()))
                        except json.JSONDecodeError:
                            continue
            except Exception as exc:
                out.put({"__error__": str(exc)})

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def catalogue(self) -> dict[str, list[str]]:
        """Model ids opencode can currently run, grouped by provider.

        This is the pipeline's only view of available models. Model
        credentials live in opencode's auth store and are never read here.
        """
        payload = requests.get(f"{self._base}/config/providers", timeout=60).json()
        out: dict[str, list[str]] = {}
        for provider in payload.get("providers") or []:
            pid = provider.get("id")
            if not pid:
                continue
            models = provider.get("models") or {}
            if isinstance(models, dict):
                ids = [k for k in models.keys()]
            else:
                ids = [m.get("id") for m in models if m.get("id")]
            out[pid] = [m for m in ids if m]
        return out

    def defaults(self) -> dict[str, str]:
        try:
            return requests.get(f"{self._base}/config/providers", timeout=60).json().get(
                "default") or {}
        except Exception:
            return {}

    def abort(self, session_id: str) -> None:
        try:
            requests.post(f"{self._base}/session/{session_id}/abort", timeout=10)
        except requests.RequestException:
            pass


GRADER_AGENT_PROMPT = """\
You grade a single student submission against a rubric and record your scores \
through the `{tool}` tool.

Work only inside the directory you were given. It contains this student's \
material: a cloned repository and any files they attached. Read before you \
conclude; never assume a file exists because the README mentions it.

Use grep and glob to find what you need rather than guessing filenames. \
Read the source, the sample input, and the sample output — claims in a \
README are not evidence, the artefacts are.

You have no visibility into other students' work and must not reason \
comparatively. Judge only against the rubric you were given.

You may run read-only shell commands to inspect the repository. Do not \
modify, create or delete anything.

When you have worked through every criterion, call the scorecard tool once. \
If it is rejected, fix what it reports and call it again. Do not write any \
files yourself — your scores are the only thing that gets recorded."""


@dataclass
class GradeResult:
    submission_row_id: int
    session_id: str
    attempt: int
    status: str
    scorecard: Scorecard | None = None
    earned: float = 0.0
    possible: float = 0.0
    issues: list[str] = field(default_factory=list)
    error: str | None = None


class Grader:
    def __init__(self, server: OpenCodeServer, store: Store, spec: AssignmentSpec,
                 run_id: int, run_dir: Path, code_root: Path,
                 python_exe: str | None = None, emit: Emitter | None = None,
                 max_attempts: int = 2, agent_name: str = "grader",
                 model: str = "", budget: int = 8192):
        self.server = server
        self.store = store
        self.spec = spec
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.code_root = Path(code_root)
        self.python_exe = python_exe or sys.executable
        self.emit = emit or (lambda *_: None)
        self.max_attempts = max_attempts
        self.agent_name = agent_name
        self.model = model
        self.budget = budget
        self.manual_ids = manual_only_criteria(spec) if spec.policy.get(
            "manual_only_criteria") else []

    # ------------------------------------------------------------ helpers

    def _workspace_for(self, unit: SubmissionUnit) -> Path:
        from ..sources import slug
        return self.run_dir / "submissions" / slug(unit.display_name)

    def _write_opencode_config(self, workspace: Path, spec_path: Path,
                               out_path: Path, meta_path: Path) -> Path:
        cfg: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                MCP_SERVER_NAME: {
                    "type": "local",
                    "command": [self.python_exe,
                                str(self.code_root / "pipeline/grading/mcp_server.py")],
                    "enabled": True,
                    "environment": {
                        "GP_SPEC_PATH": str(spec_path),
                        "GP_OUT_PATH": str(out_path),
                        "GP_META_PATH": str(meta_path),
                        "GP_CODE_ROOT": str(self.code_root),
                        "PYTHONPATH": str(self.code_root),
                    },
                }
            },
            "agent": {
                self.agent_name: {
                    "description": "Grades one student submission against a rubric.",
                    "mode": "primary",
                    "prompt": GRADER_AGENT_PROMPT.replace("{tool}", EXPOSED_TOOL_NAME),
                    "tools": {
                        "read": True,
                        "write": False,
                        "edit": False,
                        "bash": True,
                        "grep": True,
                        "glob": True,
                        "webfetch": False,
                        "task": False,
                        "todowrite": True,
                        "websearch": False,
                        "patch": False,
                    },
                    "permission": {"edit": "deny", "webfetch": "deny"},
                    "maxSteps": 60,
                }
            },
            "permission": {"edit": "deny", "webfetch": "deny"},
        }
        if self.model:
            cfg["model"] = self.model
        # A reasoning model spends the output budget on thinking and can finish
        # with no answer at all. On a tight budget, ask for reasoning to be off.
        if self.budget < 4096:
            cfg["agent"][self.agent_name]["options"] = {"reasoning_effort": "none"}
            cfg["agent"][self.agent_name]["variant"] = "minimal"
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "opencode.json"
        path.write_text(json.dumps(cfg, indent=2), newline=_NL)
        return path

    # -------------------------------------------------------------- grade

    def _turn(self, submission_row_id: int, attempt_id: int, workspace: Path,
              spec_path: Path, out_path: Path, meta_path: Path, unit: SubmissionUnit,
              sources: list[dict[str, Any]],
              test_results: dict[str, Any] | None, anchors: list[dict[str, Any]] | None,
              message: str, system: str, attempt_no: int) -> dict[str, Any]:
        """Run one agent turn, lowering the output budget if the key refuses.

        A key with a low credit ceiling rejects a large output budget outright.
        Rather than failing the student, the ceiling is read out of opencode's
        error and the turn is retried at a size the key can afford.
        """
        session = self.server.create_session(
            f"{self.spec.title} — {unit.display_name} (attempt {attempt_no})",
            directory=str(workspace))
        session_id = session.get("id", "")
        self.store.finish_attempt(attempt_id, "running", session_id)
        self.emit("agent.start", {
            "submission_row_id": submission_row_id, "student": unit.display_name,
            "session_id": session_id, "attempt": attempt_no, "model": self.model,
            "directory": str(workspace),
        })
        response = self.server.prompt(
            session_id, message, system=system, agent=self.agent_name,
            model=self.model or None, directory=str(workspace))

        refusal = self.server.provider_error(response)
        if not refusal:
            return response

        reduced = budget_from_error(refusal, self.budget)
        if reduced >= self.budget:
            return response

        self.emit("agent.budget", {"submission_row_id": submission_row_id,
                                   "from": self.budget, "to": reduced,
                                   "reason": refusal[:160]})
        self.budget = reduced
        self.server.max_output_tokens = reduced
        self.server.stop()
        self.server.start()
        self._write_opencode_config(workspace, spec_path, out_path, meta_path)

        system = build_system_prompt(self.spec, self.manual_ids, budget=self.budget)
        if attempt_no == 1:
            message = build_task_prompt(self.spec, unit, sources, str(workspace),
                                        manual_ids=self.manual_ids,
                                        test_results=test_results, anchors=anchors,
                                        budget=self.budget)
        session = self.server.create_session(
            f"{self.spec.title} — {unit.display_name} (attempt {attempt_no}, reduced budget)",
            directory=str(workspace))
        self.store.finish_attempt(attempt_id, "running", session.get("id", ""))
        return self.server.prompt(
            session.get("id", ""), message, system=system, agent=self.agent_name,
            model=self.model or None, directory=str(workspace))

    def grade(self, submission_row_id: int, unit: SubmissionUnit,
              sources: list[dict[str, Any]],
              test_results: dict[str, Any] | None = None,
              anchors: list[dict[str, Any]] | None = None) -> GradeResult:
        self.store.set_submission_status(submission_row_id, "grading")
        workspace = self._workspace_for(unit)
        workspace.mkdir(parents=True, exist_ok=True)

        work = workspace / ".grading"
        work.mkdir(exist_ok=True)
        spec_blob = {
            "spec": self.spec.model_dump(mode="json"),
            "manual_only": self.manual_ids,
            "tool_schema": scorecard_json_schema(self.spec),
        }
        spec_path = work / "spec.json"
        spec_path.write_text(json.dumps(spec_blob, indent=2), newline=_NL)
        out_path = work / "scorecard.json"
        meta_path = work / "meta.json"
        for stale in (out_path, meta_path):
            stale.unlink(missing_ok=True)

        self._write_opencode_config(workspace, spec_path, out_path, meta_path)

        system = build_system_prompt(self.spec, self.manual_ids,
                                    budget=self.budget)
        task = build_task_prompt(self.spec, unit, sources, str(workspace),
                                 manual_ids=self.manual_ids, test_results=test_results,
                                 anchors=anchors, budget=self.budget)

        session_id = ""
        last_error: str | None = None
        last_issues: list[str] = []
        for attempt_no in range(1, self.max_attempts + 1):
            attempt_id = self.store.start_attempt(submission_row_id, self.model or None)
            message = task if attempt_no == 1 else retry_prompt(last_issues)
            try:
                # _turn owns session creation so a budget-triggered retry gets
                # its own session too, and so exactly one session is opened per
                # attempt.
                response = self._turn(submission_row_id, attempt_id, workspace,
                                      spec_path, out_path, meta_path, unit, sources,
                                      test_results, anchors, message, system,
                                      attempt_no)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self.store.finish_attempt(attempt_id, "error", session_id, last_error)
                self.emit("agent.error", {"submission_row_id": submission_row_id,
                                          "session_id": session_id,
                                          "message": last_error})
                if attempt_no >= self.max_attempts:
                    self.store.set_submission_status(submission_row_id, "failed", last_error)
                    return GradeResult(submission_row_id, session_id, attempt_no, "failed",
                                       error=last_error)
                continue

            record = self._read_outcome(out_path)
            if record is None:
                last_error = ("the agent finished without calling submit_scorecard")
                self.store.finish_attempt(attempt_id, "no_scorecard", session_id, last_error)
                self.emit("agent.warn", {"submission_row_id": submission_row_id,
                                         "session_id": session_id,
                                         "message": last_error})
                if attempt_no >= self.max_attempts:
                    self.store.set_submission_status(
                        submission_row_id, "needs_review", last_error)
                    return GradeResult(submission_row_id, session_id, attempt_no,
                                       "needs_review", error=last_error)
                last_issues = [last_error]
                continue

            scorecard = parse_scorecard(record["payload"], self.spec)
            scorecard.model = self.model
            scorecard.agent_session_id = session_id
            scorecard.prompt_version = PROMPT_VERSION

            self.store.save_scorecard(
                submission_row_id, attempt_id, scorecard,
                "valid" if record["ok"] else "invalid",
                record["earned"], record["possible"], record["issues"])

            if record["ok"]:
                self.store.finish_attempt(attempt_id, "ok", session_id)
                self.store.set_submission_status(submission_row_id, "graded")
                self.emit("agent.done", {
                    "submission_row_id": submission_row_id,
                    "session_id": session_id,
                    "student": unit.display_name,
                    "earned": record["earned"], "possible": record["possible"],
                    "attempt": attempt_no,
                })
                return GradeResult(submission_row_id, session_id, attempt_no, "graded",
                                   scorecard=scorecard, earned=record["earned"],
                                   possible=record["possible"])

            last_issues = record["issues"]
            self.store.finish_attempt(attempt_id, "invalid", session_id,
                                      "; ".join(record["issues"]))
            self.emit("agent.retry", {"submission_row_id": submission_row_id,
                                      "issues": record["issues"], "attempt": attempt_no})
            if attempt_no >= self.max_attempts:
                self.store.set_submission_status(
                    submission_row_id, "needs_review", "; ".join(record["issues"]))
                return GradeResult(submission_row_id, session_id, attempt_no,
                                   "needs_review", scorecard=scorecard,
                                   earned=record["earned"], possible=record["possible"],
                                   issues=record["issues"], error="failed validation")

        self.store.set_submission_status(submission_row_id, "needs_review", last_error)
        return GradeResult(submission_row_id, session_id, self.max_attempts,
                           "needs_review", error=last_error)

    @staticmethod
    def _read_outcome(out_path: Path) -> dict[str, Any] | None:
        if not out_path.exists():
            return None
        try:
            return json.loads(out_path.read_text())
        except json.JSONDecodeError:
            return None


def launch_student_session(server: OpenCodeServer, unit: SubmissionUnit,
                           spec: AssignmentSpec, workspace: Path) -> str:
    """Open a session in a student's workspace without grading (for inspection)."""
    workspace.mkdir(parents=True, exist_ok=True)
    session = server.create_session(f"{spec.title} — {unit.display_name}",
                                    directory=str(workspace))
    return session.get("id", "")