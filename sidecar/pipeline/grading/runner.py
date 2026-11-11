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
import re
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


def require_v2_opencode(binary: str) -> None:
    """Refuse an opencode whose server API this pipeline does not speak.

    The pipeline drives `opencode serve` over the v2 HTTP API under /api
    with basic auth. A v1 binary answers those paths with 404s or its own
    shapes, and a PATH fallback can silently find the wrong major version.
    Fail fast with the fix instead of failing far from the cause, as a
    JSON error in the model list.
    """
    from ..runtime import parse_version, read_version

    version = read_version(binary)
    parsed = parse_version(version)
    if parsed is not None and parsed[0] != 2:
        raise RunnerError(
            f"opencode {parsed[0]}.{parsed[1]}.{parsed[2]} is not v2: the "
            "pipeline speaks the v2 server API. The app uses its own "
            "bundled runtime for exactly this reason; point "
            "GRADING_PIPELINE_OPENCODE at the bundled binary instead of a "
            "system opencode."
        )


def ensure_loopback_proxy_bypass(environ: dict | None = None) -> None:
    """Keep loopback HTTP out of any configured proxy.

    `requests` honours HTTP_PROXY/HTTPS_PROXY even for 127.0.0.1 unless
    NO_PROXY covers it, and a proxy's HTML or empty reply then surfaces as a
    JSON error far from its cause ("Expecting value: line 1 column 1").
    The agent runtime is always loopback, so it must never be proxied.
    """
    env = os.environ if environ is None else environ
    for var in ("NO_PROXY", "no_proxy"):
        parts = [p.strip() for p in (env.get(var) or "").split(",") if p.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in parts:
                parts.append(host)
        env[var] = ",".join(parts)


class RunnerError(RuntimeError):
    pass


# Why a turn failed, and whether trying again could possibly help. Order
# matters: the first match wins, so the more specific patterns come first.
FAILURE_PATTERNS: tuple[tuple[str, str, bool], ...] = (
    # Billing is a state, not a hiccup: a second attempt spends the same tokens
    # and fails the same way. Quota is different and belongs with rate limits.
    ("credit", r"insufficient[_ ]credits|credit balance|out of credits"
               r"|payment required|\b402\b|billing", False),
    ("auth", r"\b401\b|unauthori[sz]ed|invalid api ?key|forbidden|\b403\b", False),
    ("model", r"model .*not found|no such model|\b404\b|does not exist", False),
    ("context", r"context length|maximum context|too many tokens|prompt is too long"
                r"|input length and .* exceed", False),
    ("output limit", r"max_tokens|finish_reason.{0,10}length|output budget", True),
    ("rate limit", r"\b429\b|rate ?limit|too many requests|overloaded"
                   r"|quota exceeded|insufficient_quota", True),
    ("timeout", r"timed? ?out|timeout|deadline exceeded", True),
    ("refused", r"provider returned no usable output|empty response", True),
)


def classify_failure(message: str) -> tuple[bool, str]:
    """(retryable, short reason) for a failed turn.

    The reason is what the person sees, so it names the cause rather than
    repeating a stack trace: "out of credits" is actionable and
    "RunnerError: prompt failed: 402 {...}" is not.
    """
    text = (message or "").lower()
    for label, pattern, retryable in FAILURE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return retryable, f"{label}: {message.strip()[:300]}"
    return True, message.strip()[:300]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def mcp_command(executable: str, code_root: Path, *,
                frozen: bool | None = None) -> list[str]:
    """How opencode should start the scorecard MCP server.

    From a source checkout the module is a real file, so the running interpreter
    can execute it. A frozen onefile build has neither: ``sys.executable`` is
    the sidecar binary and ``pipeline/grading/mcp_server.py`` exists only inside
    the PyInstaller archive. There, the binary re-enters itself with ``--mcp``.

    ``frozen`` is a parameter rather than a bare ``sys.frozen`` read so both
    branches can be tested without pretending the test process is packaged.
    Passing ``None`` detects the current process.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return [executable, "--mcp"]
    return [executable,
            str(Path(code_root) / "pipeline" / "grading" / "mcp_server.py")]


@dataclass
class OpenCodeServer:
    """A headless opencode v2 process, reachable over its HTTP API.

    One server serves one project directory (its cwd): v2 sessions always
    live in the server's project. Per-student isolation therefore means
    one server per workspace, cached by the caller.
    """

    binary: str = "opencode"
    model: str = ""
    hostname: str = "127.0.0.1"
    port: int = field(default_factory=free_port)
    timeout: int = 60
    cwd: str | Path | None = None
    # A metered or free key can refuse a large max_tokens even when small
    # requests succeed, so the ceiling measured at preflight is enforced here
    # rather than letting the agent request its 32k default and get a 402.
    max_output_tokens: int = 0
    _proc: subprocess.Popen | None = None
    _base: str = ""
    _password: str = ""

    def __post_init__(self) -> None:
        ensure_loopback_proxy_bypass()
        # The packaged app ships its own opencode and points at it through the
        # environment, so a user is not required to install one. A developer's
        # own opencode is used when nothing is specified.
        override = os.environ.get("GRADING_PIPELINE_OPENCODE", "").strip()
        # A runtime we downloaded ourselves wins over the one inside the app.
        # That is what makes a staged update take effect on the next launch
        # rather than a moment sooner, and it keeps anything code-signed from
        # ever being rewritten on disk.
        try:
            from ..runtime import RuntimeLayout, resolve_binary
            layout = RuntimeLayout(Path(
                os.environ.get("GRADING_PIPELINE_HOME", Path.home())))
            chosen, _source = resolve_binary(layout, override)
            self.binary = chosen
        except Exception:
            if override:
                self.binary = override
        self._base = f"http://{self.hostname}:{self.port}"
        # v2 requires basic auth on every API call. We choose the password
        # ourselves (via OPENCODE_SERVER_PASSWORD) instead of scraping the
        # generated one from the server's output.
        import secrets
        self._password = secrets.token_urlsafe(24)

    def _auth_header(self) -> str:
        import base64
        return "Basic " + base64.b64encode(
            f"opencode:{self._password}".encode()).decode()

    def _request(self, method: str, path: str, body: Any = None,
                 timeout: int = 30):
        """One authenticated v2 API call. Paths are given without /api."""
        headers = {"Authorization": self._auth_header()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        return requests.request(
            method, self._base + "/api" + path, headers=headers,
            json=body, timeout=timeout)

    @staticmethod
    def _data(response) -> Any:
        """Unwrap v2's {"data": ...} envelope; bare payloads pass through."""
        try:
            payload = response.json()
        except ValueError:
            raise RunnerError(
                f"unexpected non-JSON response (HTTP {response.status_code})")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def start(self, extra_env: dict[str, str] | None = None) -> None:
        if not Path(self.binary).is_file() and shutil.which(self.binary) is None:
            raise RunnerError(
                f"'{self.binary}' is not on PATH. Install opencode, or set "
                "GRADING_PIPELINE_OPENCODE to its path."
            )
        require_v2_opencode(self.binary)
        env = {**os.environ, **SYSTEM_ENV, **(extra_env or {})}
        env["OPENCODE_SERVER_PASSWORD"] = self._password
        if self.max_output_tokens:
            env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(self.max_output_tokens)
        self._proc = subprocess.Popen(
            [self.binary, "serve", "--hostname", self.hostname, "--port", str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
            cwd=str(self.cwd) if self.cwd else None,
        )
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or "")[:800] if self._proc.stderr else ""
                raise RunnerError(f"opencode serve exited immediately: {err}")
            try:
                r = self._request("GET", "/config", timeout=3)
                if r.ok:
                    r.json()  # any JSON answers means the v2 API is up
                    return
            except (requests.RequestException, ValueError):
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

    # ---------------------------------------------------------------- API

    def url(self, path: str, directory: str | None = None) -> str:
        # v2 sessions live in the server's project directory; there is no
        # per-call directory. Kept for call-site compatibility.
        return self._base + "/api" + path

    def create_session(self, title: str, directory: str | None = None,
                       model: str | None = None,
                       agent: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"title": title}
        if model:
            parts = model.split("/", 1)
            if len(parts) == 2:
                body["model"] = {"id": parts[1], "providerID": parts[0]}
        # v2 selects the agent at creation; a per-prompt agent field is
        # ignored, so sessions created without it silently run as `build`
        # and never hear about the scorecard tool.
        if agent:
            body["agent"] = agent
        r = self._request("POST", "/session", body, timeout=30)
        if not r.ok:
            raise RunnerError(f"create session failed: {r.status_code} {r.text[:200]}")
        data = self._data(r)
        return {"id": data.get("id", ""), "raw": data}

    def __enter__(self) -> "OpenCodeServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---------------------------------------------------------------- API

    def url(self, path: str, directory: str | None = None) -> str:
        # v2 sessions live in the server's project directory; there is no
        # per-call directory. Kept for call-site compatibility.
        return self._base + "/api" + path

    def prompt(self, session_id: str, text: str, system: str | None = None,
               agent: str | None = None, model: str | None = None,
               directory: str | None = None, done_when: Any = None,
               grace: float = 20.0, timeout: int = 1800) -> dict[str, Any]:
        """Send a prompt and wait for the turn to finish.

        v2 accepts the message and returns the user message immediately;
        the turn runs in the background. Completion is therefore polled
        from the message list until a new assistant message completes.
        The model is chosen at session creation in v2, so `model` here is
        accepted for compatibility and ignored.

        `done_when` says when the work is finished, which for a grading turn
        is the scorecard landing rather than the agent falling silent. A model
        that submits and then keeps writing must not hold the row in
        `grading` for as long as it feels like, so once the work is done the
        wait ends after a short grace period for the agent to finish its
        sentence.
        """
        body: dict[str, Any] = {"text": text}
        if system:
            body["system"] = system
        if agent:
            body["agent"] = agent
        before_ids = {m.get("id") for m in self._messages(session_id)}
        r = self._request("POST", f"/session/{session_id}/prompt", body, timeout=30)
        if not r.ok:
            raise RunnerError(f"prompt failed: {r.status_code} {r.text[:300]}")
        deadline = time.time() + timeout
        finished_at: float | None = None
        while time.time() < deadline:
            if done_when is not None:
                try:
                    if done_when():
                        if finished_at is None:
                            finished_at = time.time()
                        if time.time() - finished_at >= grace:
                            return {"info": {}, "parts": self.parts(session_id),
                                    "taken_over": True}
                except Exception:
                    pass
            messages = self._messages(session_id)
            # v2 lists the messages newest first, so the one to look at is the
            # newest of the new ones. Taking the oldest meant the runner kept
            # reading the first message of the turn, which asked for tool
            # calls, and so never saw the turn end.
            fresh = sorted(
                (m for m in messages
                 if m.get("id") not in before_ids
                 and m.get("type") == "assistant"
                 and (m.get("time") or {}).get("completed")),
                key=lambda m: (m.get("time") or {}).get("created") or 0)
            if fresh:
                assistant = fresh[-1]
                # A completed assistant message that requested tool calls is
                # not the end of the turn: opencode executes the tools and
                # the model continues in a later assistant message. Treating
                # it as finished made the pipeline declare "no scorecard"
                # while the agent was still working.
                finish = (assistant.get("finish") or "").lower()
                if finish in ("tool-calls", "tool_use", "tool-call"):
                    # More work follows; keep polling for the next message.
                    pass
                elif self._session_busy(session_id):
                    # The message completed but the session has not gone
                    # idle yet (tools running / next step queued). Wait for
                    # the session to settle so we don't read mid-turn.
                    pass
                else:
                    error = assistant.get("error") or {}
                    info: dict[str, Any] = {}
                    if error:
                        info["error"] = {"message": error.get("message") or "",
                                         "name": error.get("type") or "error"}
                    return {"info": info, "parts": self.parts(session_id)}
            time.sleep(2)
        raise RunnerError("timed out waiting for the agent turn to finish")

    def prompt_async(self, session_id: str, text: str, model: str | None = None,
                     directory: str | None = None) -> bool:
        """Send a message without waiting for the turn to finish.

        This is the steering channel: the instructor can add an instruction
        while the agent is mid-turn. opencode accepts it and records it in
        the session, where the running turn picks it up. Returns False when
        the session cannot accept it (unknown session, server down).
        """
        try:
            r = self._request("POST", f"/session/{session_id}/prompt",
                              {"text": text}, timeout=15)
            return r.ok
        except requests.RequestException:
            return False

    def provider_error(self, response: dict[str, Any]) -> str | None:
        """The provider's own error message, if the turn failed."""
        err = ((response or {}).get("info") or {}).get("error") or {}
        return err.get("message") or err.get("name")

    def _session_busy(self, session_id: str) -> bool:
        """Whether the session still has work in progress.

        A prompt turn can emit a completed assistant message (for example
        one that requested tool calls) while the session continues running.
        We only treat the turn as finished once the session is no longer
        busy, so a mid-turn step is not mistaken for "ended without a
        scorecard". On any doubt we report not-busy to avoid hanging.
        """
        try:
            r = self._request("GET", f"/session/{session_id}", timeout=15)
            if not r.ok:
                return False
            data = self._data(r)
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        # A resolved outcome or an idle timestamp means the session settled.
        if data.get("outcome") or data.get("error"):
            return False
        t = data.get("time") or {}
        idle = t.get("idle")
        busy = t.get("busy")
        if idle and (not busy or idle >= busy):
            return False
        if busy and not idle:
            return True
        # Some shapes expose a status string instead of/alongside times.
        status = (data.get("status") or "").lower()
        if status in {"idle", "completed", "done", "error", "failed"}:
            return False
        if status in {"busy", "running", "working", "pending"}:
            return True
        # If the endpoint returned something unexpected (e.g. a message
        # list shape mismatch), don't block completion on it.
        return False

    def _messages(self, session_id: str) -> list[dict[str, Any]]:
        try:
            r = self._request("GET", f"/session/{session_id}/message", timeout=20)
            if not r.ok:
                return []
            data = self._data(r)
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def parts(self, session_id: str, directory: str | None = None) -> list[dict[str, Any]]:
        """The session's message parts so far, in the pipeline's part shape.

        This, not opencode's event stream, is what the live log reads from.
        The stream proved unreliable in practice: against the same server,
        curl and requests both received only the opening frame and
        heartbeats while the turn was demonstrably running, so there was
        nothing to display. The REST endpoint returns the reasoning and
        text parts as they are written, which is the same information
        without depending on a long-lived connection that a restarted
        server or a dropped socket can silently kill.
        """
        out: list[dict[str, Any]] = []
        # v2 lists messages newest-first; the log poller assumes chronological
        # order (it skips the first text part as the prompt echo and appends
        # reasoning growth), so sort by creation time before mapping.
        messages = sorted(
            self._messages(session_id),
            key=lambda m: (m.get("time") or {}).get("created") or 0)
        for msg in messages:
            mid = msg.get("id") or ""
            if not mid:
                continue
            if msg.get("type") == "user":
                text = msg.get("text") or ((msg.get("payload") or {}).get("text") or "")
                if text:
                    out.append({"id": mid, "type": "text", "text": text,
                                "tool": "", "state": {}, "cost": None,
                                "tokens": {}, "reason": None})
                continue
            if msg.get("type") != "assistant":
                continue
            for index, item in enumerate(msg.get("content") or []):
                itype = item.get("type") or ""
                if itype == "text":
                    out.append({"id": f"{mid}:{index}", "type": "text",
                                "text": item.get("text") or "", "tool": "",
                                "state": {}, "cost": None, "tokens": {},
                                "reason": None})
                elif itype == "reasoning":
                    out.append({"id": f"{mid}:{index}", "type": "reasoning",
                                "text": item.get("text") or "", "tool": "",
                                "state": {}, "cost": None, "tokens": {},
                                "reason": None})
                elif "tool" in itype:
                    error = item.get("error")
                    state = item.get("state") or {}
                    output = state.get("output")
                    if output is None:
                        # v2 carries tool output as content blocks.
                        output = "\n".join(
                            block.get("text") or ""
                            for block in (state.get("content") or [])
                            if isinstance(block, dict))
                    out.append({
                        "id": item.get("id") or f"{mid}:{index}",
                        "type": "tool",
                        "text": "",
                        "tool": item.get("name") or item.get("tool") or "",
                        "state": {
                            "title": item.get("title") or state.get("title") or "",
                            "input": state.get("input") or item.get("input") or item.get("args") or {},
                            "status": state.get("status") or item.get("status") or (
                                "error" if error else "completed"),
                            "output": str(output or (
                                (error or {}).get("message") if isinstance(error, dict)
                                else "") or ""),
                            "metadata": state.get("metadata") or item.get("metadata") or {},
                        },
                        "cost": None, "tokens": {}, "reason": None,
                    })
            tokens = msg.get("tokens") or {}
            out.append({
                "id": f"{mid}:finish", "type": "step-finish", "text": "",
                "tool": "", "state": {},
                "cost": msg.get("cost"),
                "tokens": {k: v for k, v in tokens.items()
                           if isinstance(v, (int, float))},
                "reason": msg.get("finish"),
            })
            error = msg.get("error") or {}
            if error:
                # A failed turn has no content to show, so without this the
                # log goes silent exactly when the person needs to know why
                # (a 429 rate limit, out of credits). Surfaced as an error
                # part the poller renders as an error entry.
                out.append({
                    "id": f"{mid}:error", "type": "error",
                    "text": error.get("message") or "the turn failed",
                    "tool": "",
                    "state": {"status": "error",
                              "http": error.get("status")},
                    "cost": None, "tokens": {}, "reason": None,
                })
        return out

    def events(self, stop: threading.Event, out: queue.Queue,
               directory: str | None = None) -> threading.Thread:
        def run() -> None:
            try:
                with requests.get(self.url("/event"), stream=True,
                                  headers={"Authorization": self._auth_header()},
                                  timeout=(10, 3600)) as resp:
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
        payload = self._models_payload()
        out: dict[str, list[str]] = {}
        for entry in payload:
            pid = entry.get("providerID") or ""
            mid = entry.get("modelID") or entry.get("id") or ""
            if pid and mid:
                out.setdefault(pid, []).append(mid)
        return out

    def _models_payload(self, attempts: int = 3) -> list[dict[str, Any]]:
        """GET /model, tolerating a briefly-unready server.

        Right after `serve` starts, the endpoint can answer with an empty
        or non-JSON body. Retry a few times and then say what actually
        happened, including a snippet of the offending body.
        """
        last = ""
        for attempt in range(attempts):
            try:
                response = self._request("GET", "/model", timeout=15)
                if response.ok:
                    try:
                        payload = response.json()
                    except ValueError:
                        snippet = (response.text or "")[:120].replace("\n", " ")
                        ctype = response.headers.get("Content-Type", "?")
                        last = (f"a non-JSON response "
                                f"(HTTP {response.status_code}, {ctype}: {snippet!r})")
                    else:
                        data = payload.get("data", payload) if isinstance(payload, dict) else payload
                        if isinstance(data, list):
                            return data
                        last = "a JSON response that is not a list"
                else:
                    last = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last = str(exc)[:200]
            if attempt + 1 < attempts:
                time.sleep(1)
        print(f"[models] agent runtime did not return a model list: {last}",
              file=sys.stderr, flush=True)
        raise RunnerError(
            f"the agent runtime did not return a model list ({last})"
        )

    def defaults(self) -> dict[str, str]:
        # v2 has no equivalent default-model map in the catalogue response.
        return {}

    def abort(self, session_id: str) -> None:
        try:
            self._request("POST", f"/session/{session_id}/interrupt", {}, timeout=10)
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
    def __init__(self, server_for: Callable[[Path], OpenCodeServer],
                 store: Store, spec: AssignmentSpec,
                 run_id: int, run_dir: Path, code_root: Path,
                 python_exe: str | None = None, emit: Emitter | None = None,
                 max_attempts: int = 2, agent_name: str = "grader",
                 model: str = "", budget: int = 8192,
                 should_stop: Callable[[int], bool] | None = None):
        # v2 serves one project directory per server, and each student
        # workspace is its own project (its config carries that student's
        # MCP environment). So the grader asks for the server of the
        # workspace it is about to grade in, rather than holding one
        # server for the whole run.
        self._server_for = server_for
        self._servers: dict[str, OpenCodeServer] = {}
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
        # submission row -> opencode session, so terminal events name it.
        self._session_by_row: dict[int, str] = {}
        # Asked whether a row has been abandoned. Checked before every attempt,
        # not just between rows: Stop arrives while a turn is running, so by the
        # time it lands the loop is inside an attempt and would otherwise go
        # straight on to the retry.
        self.should_stop = should_stop or (lambda _row_id: False)

    # ------------------------------------------------------------ helpers

    def _server(self, workspace: Path) -> OpenCodeServer:
        key = str(workspace)
        srv = self._servers.get(key)
        if srv is None:
            srv = self._server_for(workspace)
            self._servers[key] = srv
        return srv

    def _workspace_for(self, unit: SubmissionUnit) -> Path:
        from ..sources import slug
        return self.run_dir / "submissions" / slug(unit.display_name)

    def _write_opencode_config(self, workspace: Path, spec_path: Path,
                               out_path: Path, meta_path: Path) -> Path:
        cfg: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "servers": {
                    MCP_SERVER_NAME: {
                        "type": "local",
                        "command": mcp_command(self.python_exe, self.code_root),
                        # Direct native tools, not Code Mode grouping, so the
                        # model sees grader_submit_scorecard itself.
                        "codemode": False,
                        "environment": {
                            "GP_SPEC_PATH": str(spec_path),
                            "GP_OUT_PATH": str(out_path),
                            "GP_META_PATH": str(meta_path),
                            "GP_CODE_ROOT": str(self.code_root),
                            "PYTHONPATH": str(self.code_root),
                        },
                    }
                }
            },
            "agents": {
                self.agent_name: {
                    "description": "Grades one student submission against a rubric.",
                    "mode": "primary",
                    "system": GRADER_AGENT_PROMPT.replace("{tool}", EXPOSED_TOOL_NAME),
                    "permissions": [
                        {"action": "edit", "resource": "*", "effect": "deny"},
                        {"action": "webfetch", "resource": "*", "effect": "deny"},
                        {"action": "websearch", "resource": "*", "effect": "deny"},
                        {"action": "subagent", "resource": "*", "effect": "deny"},
                    ],
                }
            },
        }
        if self.model:
            cfg["model"] = self.model
            # The output ceiling lives in the model config in v2, not in a
            # server env var: without it the provider is asked for its full
            # default (hundreds of thousands of tokens) and a key with a
            # small credit balance refuses outright. Rewritten on every
            # budget reduction, which restarts the workspace server.
            provider, _, model_id = self.model.partition("/")
            if provider and model_id:
                cfg["providers"] = {
                    provider: {"models": {model_id: {
                        "limit": {"output": int(self.budget)}}}}
                }
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
        srv = self._server(workspace)
        session = srv.create_session(
            f"{self.spec.title} — {unit.display_name} (attempt {attempt_no})",
            directory=str(workspace), model=self.model or None,
            agent=self.agent_name)
        session_id = session.get("id", "")
        # Recorded per submission so the retry loop can name the session it is
        # reporting on. It used to keep its own empty local, so every terminal
        # event carried session_id="" and the live poller never learned that a
        # session had ended.
        self._session_by_row[submission_row_id] = session_id
        self.store.finish_attempt(attempt_id, "running", session_id)
        self.emit("agent.start", {
            "submission_row_id": submission_row_id, "student": unit.display_name,
            "session_id": session_id, "attempt": attempt_no, "model": self.model,
            "directory": str(workspace),
        })
        response = srv.prompt(
            session_id, message, system=system, agent=self.agent_name,
            model=self.model or None, directory=str(workspace),
            done_when=lambda: self._scorecard_accepted(out_path))
        if response.get("taken_over"):
            # The scorecard is in and the grading is done. The agent's turn
            # can carry on for as long as it likes, and a second submission
            # would overwrite the scorecard that was just read.
            srv.abort(session_id)

        refusal = srv.provider_error(response)
        if not refusal:
            return response

        reduced = budget_from_error(refusal, self.budget)
        if reduced >= self.budget:
            return response

        self.emit("agent.budget", {"submission_row_id": submission_row_id,
                                   "from": self.budget, "to": reduced,
                                   "reason": refusal[:160]})
        self.budget = reduced
        srv.max_output_tokens = reduced
        srv.stop()
        srv.start()
        self._write_opencode_config(workspace, spec_path, out_path, meta_path)

        system = build_system_prompt(self.spec, self.manual_ids, budget=self.budget)
        if attempt_no == 1:
            message = build_task_prompt(self.spec, unit, sources, str(workspace),
                                        manual_ids=self.manual_ids,
                                        test_results=test_results, anchors=anchors,
                                        budget=self.budget)
        session = srv.create_session(
            f"{self.spec.title} — {unit.display_name} (attempt {attempt_no}, reduced budget)",
            directory=str(workspace), model=self.model or None,
            agent=self.agent_name)
        self.store.finish_attempt(attempt_id, "running", session.get("id", ""))
        return srv.prompt(
            session.get("id", ""), message, system=system, agent=self.agent_name,
            model=self.model or None, directory=str(workspace),
            done_when=lambda: self._scorecard_accepted(out_path))

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

        last_error: str | None = None
        last_issues: list[str] = []
        for attempt_no in range(1, self.max_attempts + 1):
            if self.should_stop(submission_row_id):
                self.store.set_submission_status(submission_row_id, "collected")
                self.emit("grade.stopped", {
                    "submission_row_id": submission_row_id,
                    "student": unit.display_name,
                    "attempt": attempt_no,
                })
                return GradeResult(submission_row_id, self._session_by_row.get(
                    submission_row_id, ""), attempt_no, "stopped")
            attempt_id = self.store.start_attempt(submission_row_id, self.model or None)
            # Each attempt starts with no scorecard on disk, so a leftover
            # from an earlier attempt cannot be mistaken for this one.
            for stale in (out_path, meta_path):
                stale.unlink(missing_ok=True)
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
                sid = self._session_by_row.get(submission_row_id, "")
                self.store.finish_attempt(attempt_id, "error", sid, last_error)
                retryable, reason = classify_failure(last_error)
                self.emit("agent.error", {"submission_row_id": submission_row_id,
                                          "session_id": sid,
                                          "message": reason or last_error,
                                          "retryable": retryable})
                # Retrying a failure that cannot succeed on a second attempt just
                # burns the same tokens again and leaves the log looking busy
                # while nothing is happening. Out of credit, a bad key or an
                # over-long context will fail identically every time.
                if not retryable or attempt_no >= self.max_attempts:
                    self.store.set_submission_status(
                        submission_row_id, "failed", reason or last_error)
                    return GradeResult(submission_row_id, sid, attempt_no, "failed",
                                       error=reason or last_error)
                continue

            record = self._read_outcome(out_path)
            if record is None:
                # "The agent finished without calling submit_scorecard" says
                # what did not happen and nothing about why, which is the only
                # question. Read the session back and say what it did instead.
                last_error, detail = self._why_no_scorecard(
                    response, self._session_by_row.get(submission_row_id, ""),
                    str(workspace))
                self.store.finish_attempt(attempt_id, "no_scorecard", self._session_by_row.get(submission_row_id, ""), last_error)
                self.emit("agent.warn", {"submission_row_id": submission_row_id,
                                         "session_id": self._session_by_row.get(
                                             submission_row_id, ""),
                                         # The activity line and the agent log
                                         # both name the student. Without it the
                                         # line reads "undefined: the agent
                                         # finished without calling
                                         # submit_scorecard" and, since a retry
                                         # follows, it looks like it belongs to
                                         # whichever row happened to be running.
                                         "student": unit.display_name,
                                         "message": last_error,
                                         "detail": detail})
                if attempt_no >= self.max_attempts:
                    self.store.set_submission_status(
                        submission_row_id, "needs_review", last_error)
                    return GradeResult(submission_row_id,
                                       self._session_by_row.get(submission_row_id, ""),
                                       attempt_no, "needs_review", error=last_error)
                last_issues = [last_error]
                continue

            scorecard = parse_scorecard(record["payload"], self.spec)
            scorecard.model = self.model
            scorecard.agent_session_id = self._session_by_row.get(submission_row_id, "")
            scorecard.prompt_version = PROMPT_VERSION

            self.store.save_scorecard(
                submission_row_id, attempt_id, scorecard,
                "valid" if record["ok"] else "invalid",
                record["earned"], record["possible"], record["issues"])

            if record["ok"]:
                self.store.finish_attempt(attempt_id, "ok", self._session_by_row.get(submission_row_id, ""))
                self.store.set_submission_status(submission_row_id, "graded")
                self.emit("agent.done", {
                    "submission_row_id": submission_row_id,
                    "session_id": self._session_by_row.get(submission_row_id, ""),
                    "student": unit.display_name,
                    "earned": record["earned"], "possible": record["possible"],
                    "attempt": attempt_no,
                })
                return GradeResult(submission_row_id, self._session_by_row.get(submission_row_id, ""), attempt_no, "graded",
                                   scorecard=scorecard, earned=record["earned"],
                                   possible=record["possible"])

            last_issues = record["issues"]
            self.store.finish_attempt(attempt_id, "invalid", self._session_by_row.get(submission_row_id, ""),
                                      "; ".join(record["issues"]))
            self.emit("agent.retry", {"submission_row_id": submission_row_id,
                                      "issues": record["issues"], "attempt": attempt_no})
            if attempt_no >= self.max_attempts:
                self.store.set_submission_status(
                    submission_row_id, "needs_review", "; ".join(record["issues"]))
                return GradeResult(submission_row_id, self._session_by_row.get(submission_row_id, ""), attempt_no,
                                   "needs_review", scorecard=scorecard,
                                   earned=record["earned"], possible=record["possible"],
                                   issues=record["issues"], error="failed validation")

        self.store.set_submission_status(submission_row_id, "needs_review", last_error)
        return GradeResult(submission_row_id, self._session_by_row.get(submission_row_id, ""), self.max_attempts,
                           "needs_review", error=last_error)

    def _why_no_scorecard(self, response: dict[str, Any] | None, session_id: str,
                          directory: str) -> tuple[str, str]:
        """Why a turn ended without a scorecard, in terms of what it did do.

        The bare "finished without calling the tool" is true of every failure
        here and actionable for none: it does not distinguish a model that
        never wrote a word, one that read the submission and then stopped, and
        one that ran out of output budget mid-thought. Those need different
        responses from whoever is watching.

        Returns the message and a detail line, both short enough for the log.
        """
        srv = self._servers.get(str(directory))
        parts = srv.parts(session_id, directory) if (session_id and srv) else []
        tools = [x for x in parts if x.get("type") == "tool"]
        texts = [x for x in parts if x.get("type") == "text" and (x.get("text") or "").strip()]
        # The first text part is the prompt echo we wrote, so it says nothing
        # about what the model did.
        said = (texts[-1]["text"].strip() if len(texts) > 1 else "")
        calls = [x.get("tool", "") for x in tools]
        tokens_in = 0
        tokens_out = 0
        for x in parts:
            tok = x.get("tokens") or {}
            tokens_in += int(tok.get("input") or 0)
            tokens_out += int(tok.get("output") or 0)

        if tools and any(c.endswith("submit_scorecard") for c in calls):
            return ("The model called the scoring tool but it wrote no scorecard. "
                    "The tool call is above in the log.",
                    "The turn may have been cut off partway through writing it.")
        if not tools:
            return ("The model ended the turn without using any tool. It never "
                    "opened the submission.",
                    f"{tokens_out} tokens out, no tool calls recorded.")
        if said:
            return (f"The model read {len(tools)} file(s) and then stopped without "
                    f"submitting a scorecard. Its last words were: \u201c{said[-220:]}\u201d",
                    f"tools used: {', '.join(dict.fromkeys(calls)) or 'none'}"
                    f" · {tokens_in} in, {tokens_out} out")
        return (f"The model made {len(tools)} tool call(s) and ended the turn without "
                f"submitting a scorecard.",
                f"tools used: {', '.join(dict.fromkeys(calls)) or 'none'}"
                f" · {tokens_in} in, {tokens_out} out")

    @staticmethod
    def _read_outcome(out_path: Path) -> dict[str, Any] | None:
        if not out_path.exists():
            return None
        try:
            return json.loads(out_path.read_text())
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _scorecard_accepted(out_path: Path) -> bool:
        """Whether the agent has submitted a scorecard that was accepted.

        The tool writes its record whether the scorecard was accepted or
        rejected, so the file appearing is not the signal: a rejected
        scorecard means the agent is still fixing it, and the runner must
        let it.
        """
        record = Grader._read_outcome(out_path)
        return bool(record and record.get("ok"))


def launch_student_session(server: OpenCodeServer, unit: SubmissionUnit,
                           spec: AssignmentSpec, workspace: Path) -> str:
    """Open a session in a student's workspace without grading (for inspection)."""
    workspace.mkdir(parents=True, exist_ok=True)
    session = server.create_session(f"{spec.title} — {unit.display_name}",
                                    directory=str(workspace))
    return session.get("id", "")