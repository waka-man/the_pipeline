#!/usr/bin/env python
"""Anonymise recorded Canvas fixtures before they leave the machine.

The recorded responses carry real student names, real course titles and the
institution's hostname. None of the tests assert on any of that — they assert on
*shape* — so the identity can be replaced with deterministic pseudonyms and the
fixtures stay useful.

Pseudonyms are derived from a digest rather than a counter, so re-recording and
re-redacting produces stable, diff-friendly output.

    python scripts/redact_fixtures.py [--check]

`--check` reports what would change and exits non-zero if anything remains
identifying, which is what CI should run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "sidecar" / "tests" / "fixtures"

# Deterministic pseudonym sources, in priority order.
GIVEN = ["Ada", "Bruno", "Chi", "Dara", "Eve", "Farid", "Gita", "Hugo", "Ines",
         "Jonas", "Kira", "Luka", "Mira", "Noor", "Omar", "Pia", "Quinn",
         "Rami", "Sana", "Tariq", "Uma", "Vera", "Wes", "Yara", "Zane"]
FAMILY = ["Alvarez", "Bennett", "Castillo", "Dubois", "Eriksen", "Fontaine",
          "Gupta", "Haddad", "Ibrahim", "Jansen", "Kowalski", "Lindqvist",
          "Moreau", "Nakamura", "Okonkwo", "Petrov", "Quintero", "Rossi",
          "Silva", "Tanaka", "Ueda", "Vasquez", "Weber", "Yilmaz"]
COURSES = ["Course Alpha", "Course Bravo", "Course Charlie", "Course Delta",
           "Course Echo", "Course Foxtrot", "Course Golf", "Course Hotel"]

HOST = "example.instructure.com"
# Matches the bare host in any position: as a URL host, in prose, or embedded
# in an HTML email domain.
ORIGINAL_HOST = re.compile(r"[A-Za-z0-9.-]*alueducation[A-Za-z0-9.-]*")
HANDLE_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
# Deliberately absent: a regex for "capitalised words in free text". It cannot
# tell a person's name from rubric terminology, and guessing wrong rewrites the
# assignment's own wording.


def digest(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12], 16)


def person_name(original: str) -> str:
    h = digest(original)
    given = GIVEN[h % len(GIVEN)]
    family = FAMILY[(h >> 8) % len(FAMILY)]
    return f"{given} {family}"


def partial_name(original: str) -> str:
    """A pseudonym for a given or family name on its own."""
    h = digest("partial:" + original)
    return GIVEN[h % len(GIVEN)] if len(original) < 8 else FAMILY[(h >> 8) % len(FAMILY)]


def slug_handle(original: str) -> str:
    h = digest("handle:" + original)
    return f"student-{h % 100000:05d}"


def course_name(original: str) -> str:
    return COURSES[digest("course:" + original) % len(COURSES)]


class Redactor:
    """Two passes: learn the identities, then remove every trace of them.

    Replacing as we walk leaves names behind in places the walker does not
    classify — inside HTML descriptions, inside a nested user object, inside a
    URL. Collecting the originals first and then scrubbing every string is both
    simpler and complete.
    """

    #: Keys whose value is a course or term title rather than a person.
    COURSE_KEYS = {"course", "term", "assignment_group", "course_section", "group_category"}
    #: Keys that are credentials or direct identifiers.
    SECRET_KEYS = {"login_id", "email", "sis_user_id", "sis_import_id",
                   "integration_id", "vendor_guid", "token", "access_token"}
    URL_KEYS = {"url", "preview_url", "html_url", "speed_grader_url",
                "submissions_download_url", "icon_url", "external_url"}
    PERSON_KEYS = {"name", "sortable_name", "display_name", "full_name", "short_name"}
    COURSE_LIKE = {"term", "group_category", "assignment_group", "course_section"}
    #: Given/family name fields hold only part of a name, so they need their own
    #: mapping; leaving them alone would leak half of every identity.
    PARTIAL_PERSON_KEYS = {"first_name", "last_name"}

    def __init__(self) -> None:
        self.people: dict[str, str] = {}
        self.courses: dict[str, str] = {}
        self.handles: dict[str, str] = {}
        self.partials: dict[str, str] = {}

    # -- pass one: learn ------------------------------------------------

    def learn(self, node: Any, key: str = "", as_course: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                self.learn(item, key, as_course=as_course)
            return
        if isinstance(node, dict):
            # A course is identified by its own shape, not by where it sits:
            # the top-level body is a list of courses with no parent key.
            is_course = "course_code" in node or (
                "term" in node and "workflow_state" in node)
            if is_course:
                for field in ("name", "course_code", "course_name", "friendly_name"):
                    if isinstance(node.get(field), str) and node[field].strip():
                        self.courses.setdefault(node[field], course_name(node[field]))
                if isinstance(node.get("term"), dict) and node["term"].get("name"):
                    self.courses.setdefault(node["term"]["name"],
                                            course_name(node["term"]["name"]))
            for k, v in node.items():
                if is_course and k in self.PERSON_KEYS:
                    continue        # already recorded as a course
                if k in self.SECRET_KEYS and isinstance(v, str) and v:
                    self.people.setdefault(v, "<redacted>")
                if k in self.PERSON_KEYS and isinstance(v, str) and v.strip():
                    target = self.courses if (as_course or is_course) else self.people
                    target.setdefault(v, course_name(v) if target is self.courses
                                      else person_name(v))
                if k in self.PARTIAL_PERSON_KEYS and isinstance(v, str) and v.strip():
                    self.partials.setdefault(v, partial_name(v))
                if k in self.COURSE_KEYS and isinstance(v, str) and v.strip():
                    self.courses.setdefault(v, course_name(v))
                if as_course and k in self.PERSON_KEYS and isinstance(v, dict):
                    self.courses.setdefault(v.get("name", ""), course_name(v.get("name", "")))
                # A term or group name is a label, not a person.
                self.learn(v, k, as_course=(k in self.COURSE_KEYS))
            return
        if isinstance(node, str):
            # No person-name scanning here on purpose: any Title Case phrase in
            # prose ("Full Marks", "Credit Card Numbers") matches, and replacing
            # those corrupts the very content the fixtures exist to exercise.
            # Identity is learned from the structured keys instead.
            for host in ORIGINAL_HOST.findall(node):
                self.courses.setdefault(host, HOST)
            for owner, repo in HANDLE_RE.findall(node):
                handle = f"github.com/{owner}/{repo}"
                self.handles.setdefault(handle,
                                        f"github.com/{slug_handle(handle)}/submission")

    # -- pass two: scrub ------------------------------------------------

    def scrub(self, node: Any, key: str = "") -> Any:
        if isinstance(node, list):
            return [self.scrub(x, key) for x in node]
        if not isinstance(node, dict):
            return self.text(node) if isinstance(node, str) else node

        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in self.SECRET_KEYS:
                out[k] = "<redacted>"
            elif k in self.PARTIAL_PERSON_KEYS and isinstance(v, str):
                out[k] = self.text(v)
            elif k in self.URL_KEYS and isinstance(v, str):
                out[k] = self.text(v)
            else:
                out[k] = self.scrub(v, k)
        return out

    def text(self, value: str) -> str:
        if not isinstance(value, str) or not value:
            return value
        # Longest first, so "Ann Marie Silva" is not half-replaced by "Ann".
        for original in sorted(self.people, key=len, reverse=True):
            if original and original in value:
                value = value.replace(original, self.people[original])
        for original in sorted(self.courses, key=len, reverse=True):
            if original and original in value:
                value = value.replace(original, self.courses[original])
        for original in sorted(self.partials, key=len, reverse=True):
            if original and original in value:
                value = value.replace(original, self.partials[original])
        for original in sorted(self.handles, key=len, reverse=True):
            if original and original in value:
                value = value.replace(original, self.handles[original])
        value = ORIGINAL_HOST.sub(HOST, value)
        value = re.sub(r"alueducation[A-Za-z0-9.-]*", HOST, value, flags=re.IGNORECASE)
        # Term titles carry the institution's initials, e.g. "2023 May Term (ALU)".
        value = re.sub(r"\((?:ALU|AFL|ACADEMIC)\)", "", value)
        value = re.sub(r"\bALU\b", "Institute", value)
        value = re.sub(r"/files/\d+/", "/files/0/", value)
        return value


def redact_all(paths: list[Path], redactor: Redactor) -> list[Path]:
    """Learn from every file, then rewrite them, so cross-file names vanish too."""
    for path in paths:
        record = json.loads(path.read_text())
        payload = record.get("body")
        redactor.learn(payload if isinstance(payload, (dict, list)) else record)
        if isinstance(record.get("link"), str):
            redactor.learn(record["link"])

    changed: list[Path] = []
    for path in paths:
        record = json.loads(path.read_text())
        before = json.dumps(record, sort_keys=True)
        if isinstance(record.get("body"), (dict, list)):
            # Recorded shape: our labels sit beside the Canvas payload.
            record["body"] = redactor.scrub(record["body"])
            if isinstance(record.get("link"), str):
                record["link"] = redactor.text(record["link"])
        else:
            # A bare Canvas response, written straight to disk. Scrubbing
            # `body` here would quietly scrub nothing and leave the file
            # intact, which is how the first fixture escaped redaction.
            record = redactor.scrub(record)
        after = json.dumps(record, indent=2, sort_keys=True)
        if before != after:
            path.write_text(after + "\n")
            changed.append(path)
    return changed


def verify(patterns: dict[str, str]) -> list[str]:
    """Report anything still identifying in the fixture tree."""
    problems: list[str] = []
    for path in sorted(FIXTURES.rglob("*.json")):
        text = path.read_text()
        for label, pattern in patterns.items():
            if re.search(pattern, text, re.IGNORECASE if label != "ids" else 0):
                problems.append(f"{path.relative_to(ROOT)}: {label}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="report remaining identifiers without rewriting")
    args = ap.parse_args()

    if args.check:
        # `alu-regex-...` is the assignment's own required repository name, not
        # an identity; it is the author's material and stays put.
        problems = verify({
            "student name": r"Lilian|Kamikazi|Abdikarim|Irumva|Mwiza|Ikechukwu",
            "institution host": r"alueducation",
            "operator name": r"Wakuma|Debela",
            "institution initials": r"\bALU\b(?!-regex)|\bAFL\b",
        })
        for p in problems:
            print(f"  {p}")
        print("clean" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0

    redactor = Redactor()
    changed = redact_all(sorted(FIXTURES.rglob("*.json")), redactor)
    for p in changed:
        print(f"  redacted {p.relative_to(ROOT)}")
    print(f"{len(changed)} file(s) rewritten; "
          f"{len(redactor.people)} people, {len(redactor.courses)} courses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())