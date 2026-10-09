"""`submit_scorecard` MCP server.

Exposed to the grading agent as the single write path. Because the scorecard
can only arrive through this tool, "the agent forgot to score" becomes a
detectable, retryable condition rather than a missing file.

Implements the MCP stdio transport directly (JSON-RPC 2.0 over stdin/stdout) to
avoid taking a runtime dependency for one tool.

Configuration comes from the environment so the process is stateless:

    GP_SPEC_PATH   JSON file: AssignmentSpec + manual_only ids
    GP_OUT_PATH    where to write the accepted (or rejected) payload
    GP_META_PATH   where to write session metadata
    GP_ATTEMPT     attempt number, for the run log
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

PROTOCOL_VERSION = "2024-11-05"

TOOL_NAME = "submit_scorecard"

# opencode exposes MCP tools as "<server name>_<tool name>". The grading runner
# registers this server under MCP_SERVER_NAME, so the name the model actually
# sees is MCP_SERVER_NAME + "_" + TOOL_NAME. Prompts must quote that full name.
MCP_SERVER_NAME = "grader"
EXPOSED_TOOL_NAME = f"{MCP_SERVER_NAME}_{TOOL_NAME}"

TOOL_DESCRIPTION = """\
Record your final scores for this submission.

Call this exactly once when you have worked through every rubric criterion.
The scores are validated before they are accepted: every criterion must be
present exactly once, every score must be within that criterion's maximum,
and every criterion must carry at least one piece of specific evidence drawn
from the submission.

If the call is rejected, the tool returns the list of problems. Fix them and
call it again."""


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _result(req_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _tool_schema() -> dict[str, Any]:
    path = os.environ.get("GP_SPEC_PATH", "")
    try:
        with open(path) as fh:
            spec_blob = json.load(fh)
        schema = spec_blob["tool_schema"]
    except Exception as exc:  # pragma: no cover
        _log(f"[mcp] cannot load schema from {path!r}: {exc}")
        raise SystemExit(2)
    return schema


def _handle_call(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the arguments and persist the outcome. Returns (record, tool_result)."""
    sys.path.insert(0, os.environ.get("GP_CODE_ROOT", "."))
    from pipeline.models import parse_scorecard, validate_scorecard, AssignmentSpec

    spec_blob = json.load(open(os.environ["GP_SPEC_PATH"]))
    spec = AssignmentSpec.model_validate(spec_blob["spec"])
    manual = spec_blob.get("manual_only") or []

    scorecard = parse_scorecard(arguments, spec)
    result = validate_scorecard(scorecard, spec, manual_only=manual)

    record = {
        "ok": result.ok,
        "issues": result.issues,
        "earned": result.earned,
        "possible": result.possible,
        "payload": arguments,
    }
    out = os.environ["GP_OUT_PATH"]
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh, indent=2)
    os.replace(tmp, out)

    if result.ok:
        lines = [f"Accepted. Total {result.earned:g} / {result.possible:g} points."]
        if scorecard.flags:
            lines.append(f"{len(scorecard.flags)} flag(s) recorded for the reviewer.")
        return record, _text_result("\n".join(lines))
    body = "Rejected. Fix these problems and call submit_scorecard again:\n" + "\n".join(
        f"- {i}" for i in result.issues
    )
    return record, _text_result(body, is_error=True)


def main() -> int:
    try:
        schema = _tool_schema()
    except SystemExit as exc:
        return int(exc.code or 1)

    _log(f"[mcp] {TOOL_NAME} ready")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log("[mcp] skipping unparseable line")
            continue

        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            _result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "grading-pipeline", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            _result(req_id, {})
        elif method == "tools/list":
            _result(req_id, {"tools": [{
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": schema,
            }]})
        elif method == "tools/call":
            if params.get("name") != TOOL_NAME:
                _error(req_id, -32601, f"unknown tool {params.get('name')!r}")
                continue
            try:
                _, tool_result = _handle_call(params.get("arguments") or {})
                _result(req_id, tool_result)
            except Exception as exc:  # pragma: no cover
                _log(f"[mcp] tool error: {exc}")
                _result(req_id, _text_result(f"Internal error recording scorecard: {exc}",
                                             is_error=True))
        elif method and req_id is not None:
            _error(req_id, -32601, f"method not found: {method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())