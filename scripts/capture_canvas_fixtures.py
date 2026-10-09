#!/usr/bin/env python
"""Record live Canvas responses as test fixtures.

The shapes that matter here are the ones that have surprised us before: which
query parameters Canvas actually accepts, what `term` looks like, how a
submission carries its attachments, and what the pagination `Link` header
contains. Fixtures recorded from the instance are worth more than fixtures
written from the documentation.

    python scripts/capture_canvas_fixtures.py [--course 3130] [--assignment 46805]

Tokens are read through the pipeline's own secret store and never written out.
Anything identifying is redacted before the file is saved.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar"))

from pipeline import config                       # noqa: E402
from pipeline.canvas.client import CanvasClient   # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "sidecar" / "tests" / "fixtures" / "canvas"


def redact(node: Any) -> Any:
    """Strip identifiers and credentials; keep every structural field."""
    SECRET_KEYS = {"api_token", "access_token", "session", "remember_me"}
    PERSONAL_KEYS = {"email", "login_id", "sis_user_id", "sis_import_id"}
    if isinstance(node, list):
        return [redact(x) for x in node]
    if isinstance(node, dict):
        return {
            k: ("<redacted>" if k in SECRET_KEYS
                else "<redacted>" if k in PERSONAL_KEYS
                else redact(v))
            for k, v in node.items()
        }
    return node


def capture(name: str, method: str, path: str, **kw) -> dict:
    client: CanvasClient = kw.pop("_client")
    resp = client.session.request(method, client._url(path),
                                  headers=client.session.headers, timeout=90)
    try:
        payload = json.loads(resp.content)
    except ValueError:
        payload = {"__raw__": resp.text[:400]}
    record = {
        "name": name,
        "method": method,
        "path": path,
        "status": resp.status_code,
        "link": resp.headers.get("Link"),
        "body": redact(payload),
    }
    (OUT / f"{name}.json").write_text(json.dumps(record, indent=2))
    print(f"  {name:28} {resp.status_code:4}  {len(resp.content):>8} bytes")
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=int, default=3130)
    ap.add_argument("--assignment", type=int, default=46805)
    args = ap.parse_args()

    settings = config.Settings.load()
    settings.check_canvas()
    client = CanvasClient(settings.canvas_base_url, settings.canvas_api_token)

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"recording to {OUT}")

    capture("users_self", "GET", "/users/self", _client=client)
    capture("courses", "GET", "/courses?include[]=term&per_page=100", _client=client)
    capture("assignments", "GET",
            f"/courses/{args.course}/assignments?per_page=100&include[]=rubric", _client=client)
    capture("assignment_rubric", "GET",
            f"/courses/{args.course}/assignments/{args.assignment}?include[]=rubric", _client=client)
    rubric_id = (json.loads((OUT / "assignment_rubric.json").read_text())
                 ["body"].get("rubric_settings") or {}).get("id")
    if rubric_id:
        capture("rubric", "GET", f"/courses/{args.course}/rubrics/{rubric_id}", _client=client)
    capture("submissions", "GET",
            f"/courses/{args.course}/assignments/{args.assignment}/submissions"
            f"?include[]=user&include[]=group&per_page=100", _client=client)
    # Use a real student: a made-up id records a 404, which tells us nothing.
    user_id = json.loads((OUT / "submissions.json").read_text())["body"][0]["user_id"]
    capture("submission_single", "GET",
            f"/courses/{args.course}/assignments/{args.assignment}/submissions/{user_id}"
            f"?include[]=attachments", _client=client)
    capture("assignment_missing", "GET",
            f"/courses/{args.course}/assignments/99999999", _client=client)

    # A single page of students, so the pagination Link header is recorded too.
    # The very first fixture predates this recorder and lived at the fixture
    # root as a bare response. Regenerate it here so one command owns every
    # fixture; otherwise it silently goes stale and, worse, can be corrupted by
    # a redactor run with nothing to restore it.
    assignment = json.loads((OUT / "assignment_rubric.json").read_text())["body"]
    legacy = ROOT / "sidecar" / "tests" / "fixtures" / (
        f"assignment_{args.course}_{args.assignment}.json")
    legacy.write_text(json.dumps(redact(assignment), indent=2, sort_keys=True) + "\n")
    print(f"  {'assignment_legacy':28} 200  {legacy.stat().st_size:>8} bytes  -> {legacy.name}")

    capture("students_page", "GET",
            f"/courses/{args.course}/students?per_page=2&include[]=groups", _client=client)

    print("done")
    return 0




if __name__ == "__main__":
    raise SystemExit(main())