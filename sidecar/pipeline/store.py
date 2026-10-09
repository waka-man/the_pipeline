"""SQLite persistence.

Every run is a resumable state machine. Folder existence is never used as a
status signal; each row carries an explicit `status`, so a re-run picks up
exactly where the last one stopped.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import AssignmentSpec, Criterion, Rating, Scorecard, SubmissionUnit, utcnow

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canvas_base_url TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    assignment_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    model TEXT,
    spec_json TEXT,
    UNIQUE (canvas_base_url, course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    canvas_submission_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    student_name TEXT NOT NULL,
    sortable_name TEXT,
    group_id INTEGER,
    group_name TEXT,
    workflow_state TEXT,
    submitted_at TEXT,
    existing_score REAL,
    primary_url TEXT,
    missing INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    UNIQUE (run_id, canvas_submission_id)
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_row_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    path TEXT,
    markdown_path TEXT,
    bytes INTEGER,
    sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_row_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    n INTEGER NOT NULL DEFAULT 1,
    agent_session_id TEXT,
    model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT
);

CREATE TABLE IF NOT EXISTS scorecards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_row_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    attempt_id INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'valid',
    model TEXT,
    prompt_version TEXT,
    agent_session_id TEXT,
    earned REAL,
    possible REAL,
    payload_json TEXT NOT NULL,
    issues_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_row_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    rendered_at TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    UNIQUE (submission_row_id)
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_row_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    published_at TEXT NOT NULL,
    canvas_response TEXT,
    status TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    phase TEXT NOT NULL,
    submission_row_id INTEGER,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_subs_run ON submissions(run_id, status);
CREATE INDEX IF NOT EXISTS idx_sources_sub ON sources(submission_row_id);
CREATE INDEX IF NOT EXISTS idx_scorecards_sub ON scorecards(submission_row_id, status);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ---------------------------------------------------------------- runs

    def upsert_run(self, spec: AssignmentSpec, model: str | None = None) -> int:
        spec_json = spec.model_dump_json()
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM runs WHERE canvas_base_url=? AND course_id=? AND assignment_id=?",
                (spec.canvas_base_url, spec.course_id, spec.assignment_id),
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE runs SET title=?, updated_at=?, spec_json=?, model=COALESCE(?, model) WHERE id=?",
                    (spec.title, utcnow(), spec_json, model, row["id"]),
                )
                return int(row["id"])
            cur = db.execute(
                "INSERT INTO runs (canvas_base_url, course_id, assignment_id, title,"
                " created_at, updated_at, status, model, spec_json)"
                " VALUES (?,?,?,?,?,?,'created',?,?)",
                (spec.canvas_base_url, spec.course_id, spec.assignment_id, spec.title,
                 utcnow(), utcnow(), model, spec_json),
            )
            return int(cur.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"no run {run_id}")
        d = dict(row)
        d["spec"] = AssignmentSpec.model_validate_json(d.pop("spec_json"))
        return d

    def find_run(self, course_id: int, assignment_id: int) -> int | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM runs WHERE course_id=? AND assignment_id=?",
                (course_id, assignment_id),
            ).fetchone()
        return int(row["id"]) if row else None

    def set_run_status(self, run_id: int, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?",
                       (status, utcnow(), run_id))

    def set_run_model(self, run_id: int, model: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE runs SET model=?, updated_at=? WHERE id=?",
                       (model, utcnow(), run_id))

    # --------------------------------------------------------- submissions

    def replace_submissions(self, run_id: int, units: list[SubmissionUnit]) -> int:
        """Insert units that are new; refresh mutable fields on existing ones."""
        inserted = 0
        with self.connect() as db:
            for u in units:
                row = db.execute(
                    "SELECT id FROM submissions WHERE run_id=? AND canvas_submission_id=?",
                    (run_id, u.canvas_submission_id),
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE submissions SET workflow_state=?, submitted_at=?,"
                        " existing_score=?, primary_url=?, missing=?, group_id=?, group_name=?"
                        " WHERE id=?",
                        (u.workflow_state, u.submitted_at, u.existing_score, u.primary_url,
                         int(u.missing), u.group_id, u.group_name, row["id"]),
                    )
                else:
                    db.execute(
                        "INSERT INTO submissions (run_id, canvas_submission_id, user_id,"
                        " student_name, sortable_name, group_id, group_name, workflow_state,"
                        " submitted_at, existing_score, primary_url, missing)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, u.canvas_submission_id, u.user_id, u.student_name,
                         u.sortable_name, u.group_id, u.group_name, u.workflow_state,
                         u.submitted_at, u.existing_score, u.primary_url, int(u.missing)),
                    )
                    inserted += 1
        return inserted

    def set_submission_status(self, row_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE submissions SET status=?, error=? WHERE id=?",
                       (status, error, row_id))

    def submission_row(self, row_id: int) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM submissions WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(f"no submission row {row_id}")
        return dict(row)

    def submission_row_for_unit(self, run_id: int, canvas_submission_id: int) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM submissions WHERE run_id=? AND canvas_submission_id=?",
                (run_id, canvas_submission_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"submission {canvas_submission_id} not in run {run_id}")
        return int(row["id"])

    def submissions_for_run(self, run_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM submissions WHERE run_id=? ORDER BY student_name", (run_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def progress(self, run_id: int) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) n FROM submissions WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def submissions_with_status(self, run_id: int, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(statuses))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM submissions WHERE run_id=? AND status IN ({placeholders})"
                " ORDER BY student_name",
                (run_id, *statuses),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- sources

    def add_source(self, submission_row_id: int, kind: str, origin: str, **kw: Any) -> int:
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO sources (submission_row_id, kind, origin, path, markdown_path,"
                " bytes, sha256, status, error, meta_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (submission_row_id, kind, origin, kw.get("path"), kw.get("markdown_path"),
                 kw.get("bytes"), kw.get("sha256"), kw.get("status", "pending"),
                 kw.get("error"), json.dumps(kw.get("meta") or {})),
            )
            return int(cur.lastrowid)

    def set_source_status(self, source_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE sources SET status=?, error=? WHERE id=?", (status, error, source_id))

    def sources_for(self, submission_row_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE submission_row_id=? ORDER BY id",
                (submission_row_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- attempts

    def start_attempt(self, submission_row_id: int, model: str | None) -> int:
        with self.connect() as db:
            n = db.execute(
                "SELECT COALESCE(MAX(n),0)+1 n FROM attempts WHERE submission_row_id=?",
                (submission_row_id,),
            ).fetchone()["n"]
            cur = db.execute(
                "INSERT INTO attempts (submission_row_id, n, model, started_at, status)"
                " VALUES (?,?,?,?,'running')",
                (submission_row_id, n, model, utcnow()),
            )
            return int(cur.lastrowid)

    def finish_attempt(self, attempt_id: int, status: str,
                       session_id: str | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE attempts SET status=?, ended_at=?, agent_session_id=?, error=? WHERE id=?",
                (status, utcnow(), session_id, error, attempt_id),
            )

    # --------------------------------------------------------- scorecards

    def save_scorecard(self, submission_row_id: int, attempt_id: int | None,
                       scorecard: Scorecard, status: str, earned: float,
                       possible: float, issues: list[str]) -> int:
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO scorecards (submission_row_id, attempt_id, status, model,"
                " prompt_version, agent_session_id, earned, possible, payload_json,"
                " issues_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (submission_row_id, attempt_id, status, scorecard.model,
                 scorecard.prompt_version, scorecard.agent_session_id, earned, possible,
                 scorecard.model_dump_json(), json.dumps(issues), utcnow()),
            )
            return int(cur.lastrowid)

    def best_scorecard(self, submission_row_id: int) -> Scorecard | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload_json FROM scorecards WHERE submission_row_id=? AND status='valid'"
                # created_at only resolves to the second, so two cards written in
                # the same second would tie and "best" could return either.
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (submission_row_id,),
            ).fetchone()
        return Scorecard.model_validate_json(row["payload_json"]) if row else None

    def invalid_reason(self, submission_row_id: int) -> str | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT issues_json FROM scorecards WHERE submission_row_id=? AND status='invalid'"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (submission_row_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            issues = json.loads(row["issues_json"])
        except Exception:
            return None
        return "; ".join(issues) if issues else "failed validation"

    # ------------------------------------------------------------ reports

    def save_report(self, submission_row_id: int, path: str, edited: bool = False) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO reports (submission_row_id, path, rendered_at, edited)"
                " VALUES (?,?,?,?) ON CONFLICT(submission_row_id)"
                " DO UPDATE SET path=excluded.path, rendered_at=excluded.rendered_at",
                (submission_row_id, path, utcnow(), int(edited)),
            )

    def report_for(self, submission_row_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM reports WHERE submission_row_id=?", (submission_row_id,)
            ).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------- publication

    def record_publication(self, submission_row_id: int, payload: dict[str, Any],
                           response: str | None = None, status: str = "ok") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO publications (submission_row_id, payload_json, published_at,"
                " canvas_response, status) VALUES (?,?,?,?,?)",
                (submission_row_id, json.dumps(payload), utcnow(), response, status),
            )

    def already_published(self, submission_row_id: int) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM publications WHERE submission_row_id=? LIMIT 1",
                (submission_row_id,),
            ).fetchone()
        return row is not None

    # -------------------------------------------------------------- events

    def log(self, run_id: int, phase: str, message: str, level: str = "info",
            submission_row_id: int | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events (run_id, ts, level, phase, submission_row_id, message)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, utcnow(), level, phase, submission_row_id, message),
            )

    def events_since(self, run_id: int, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                (run_id, after_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def spec_from_row(row: dict[str, Any]) -> AssignmentSpec:
    return AssignmentSpec.model_validate_json(row["spec_json"])


def criteria_from_spec(spec: AssignmentSpec) -> list[Criterion]:
    return spec.criteria


def ratings_from_api(raw: list[dict[str, Any]] | None) -> list[Rating]:
    return [
        Rating(id=r.get("id", ""), label=r.get("description", ""), points=float(r.get("points") or 0),
               description=r.get("long_description", "") or "")
        for r in (raw or [])
    ]