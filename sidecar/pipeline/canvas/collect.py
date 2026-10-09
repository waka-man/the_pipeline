"""Collection: pull submissions and their material into the run workspace.

Replaces the Selenium SpeedGrader scraper. Everything Canvas exposes via the
REST API is retrieved here; the only non-API path is Google Docs, which Canvas
does not integrate, handled through the public export endpoint.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import sources as S
from ..models import AssignmentSpec, SourceKind, SubmissionUnit
from ..store import Store
from .client import CanvasClient
from .spec import spec_from_assignment
from ..config import NEWLINE as _NL

Emitter = Callable[[str, dict[str, Any]], None]


@dataclass
class CollectionResult:
    run_id: int
    spec: AssignmentSpec
    units: list[SubmissionUnit]
    inserted: int
    warnings: list[str]


def units_from_submissions(raw: list[dict[str, Any]],
                           grouped: bool = False) -> list[SubmissionUnit]:
    """Normalise Canvas submissions into grading units.

    When grouped, one unit per group: the representative member carries the
    submission, and the member list is kept for publishing.
    """
    units: list[SubmissionUnit] = []
    if grouped:
        seen: dict[int, SubmissionUnit] = {}
        ungrouped: list[SubmissionUnit] = []
        for s in raw:
            group = s.get("group") or {}
            gid = group.get("id") or s.get("group_id")
            name = group.get("name") or s.get("group_name") or f"Group {gid}"
            user = s.get("user") or {}
            if gid is None:
                # No group on this row: keep it individually rather than
                # dropping it when the grouped list is returned.
                ungrouped.append(_unit_from(s))
                continue
            key = int(gid)
            if key not in seen:
                seen[key] = SubmissionUnit(
                    canvas_submission_id=int(s.get("id") or 0),
                    user_id=int(s.get("user_id") or user.get("id") or 0),
                    student_name=f"{name} (via {user.get('name', '?')})",
                    sortable_name=user.get("sortable_name", "") or "",
                    group_id=key,
                    group_name=name,
                    workflow_state=s.get("workflow_state") or "unsubmitted",
                    submitted_at=s.get("submitted_at"),
                    existing_score=s.get("score"),
                    primary_url=s.get("url"),
                    missing=bool(s.get("missing")),
                )
            units.append(seen[key])
        return list(seen.values()) + ungrouped
    return [_unit_from(s) for s in raw]


def _unit_from(s: dict[str, Any]) -> SubmissionUnit:
    user = s.get("user") or {}
    state = s.get("workflow_state") or "unsubmitted"
    return SubmissionUnit(
        canvas_submission_id=int(s.get("id") or 0),
        user_id=int(s.get("user_id") or user.get("id") or 0),
        student_name=user.get("name") or f"user {s.get('user_id')}",
        sortable_name=user.get("sortable_name") or "",
        group_id=s.get("group_id"),
        group_name=s.get("group_name"),
        workflow_state=state,
        submitted_at=s.get("submitted_at"),
        existing_score=s.get("score"),
        primary_url=s.get("url"),
        missing=bool(s.get("missing")) or state == "unsubmitted",
    )


def collect_units(client: CanvasClient, spec: AssignmentSpec) -> list[SubmissionUnit]:
    raw = client.get_submissions(spec.course_id, spec.assignment_id, grouped=spec.is_group)
    return units_from_submissions(raw, grouped=spec.is_group)


def collect_sources(client: CanvasClient, spec: AssignmentSpec, unit: SubmissionUnit,
                     row_id: int, store: Store, run_dir: Path,
                     emit: Emitter | None = None) -> list[dict[str, Any]]:
    """Gather everything needed to grade one unit into the workspace."""
    emit = emit or (lambda *_: None)
    raw_sub = _fetch_submission(client, spec, unit)
    dest = run_dir / "submissions" / S.slug(unit.display_name) / "material"
    dest.mkdir(parents=True, exist_ok=True)

    added: list[dict[str, Any]] = []
    seen: set[str] = set()

    def record(fetched: S.Fetched) -> None:
        # The digest is computed per artefact above; persist it so a stored
        # source can be verified later.
        sid = store.add_source(
            row_id, fetched.kind, fetched.origin,
            path=fetched.path, markdown_path=fetched.markdown_path,
            bytes=fetched.bytes, sha256=fetched.sha256,
            status="ok" if fetched.ok else "error",
            error=fetched.error, meta=fetched.meta)
        fetched.id = sid
        store.set_source_status(sid, "ok" if fetched.ok else "error", fetched.error)
        added.append({"id": sid, "kind": fetched.kind, "origin": fetched.origin,
                      "path": fetched.path, "markdown_path": fetched.markdown_path,
                      "status": "ok" if fetched.ok else "error",
                      "ok": fetched.ok,
                      "error": fetched.error,
                      "meta_json": _dumps(fetched.meta)})
        emit("collect.source", {"submission_row_id": row_id, "kind": fetched.kind,
                                "origin": fetched.origin, "ok": fetched.ok,
                                "error": fetched.error})

    # 1. the submitted link, if any
    url = (raw_sub or {}).get("url") or unit.primary_url
    if url:
        if S.github_repo(url):
            record(S.clone_repo(url, dest / "repo"))
        elif S.google_doc_id(url):
            record(S.fetch_google_doc(url, dest / S.safe_name(
                S.slug(url, 40)) + "_gdoc.md"))
        else:
            record(S.fetch_url(url, dest / "linked_page.md"))

    # 2. attached files
    for att in (raw_sub or {}).get("attachments") or []:
        a_url = att.get("url") or ""
        if not a_url or not att.get("id"):
            continue
        if a_url in seen:
            continue
        seen.add(a_url)
        name = att.get("display_name") or att.get("filename") or f"file_{att['id']}"
        ext = Path(name).suffix.lower()

        if ext in S.IMAGE_EXTS or (att.get("content-type") or "").startswith("image/"):
            target = dest / S.safe_name(name)
            try:
                size = client.download(a_url, target, max_bytes=S.MAX_IMAGE_BYTES)
            except Exception as exc:
                record(S.Fetched(SourceKind.IMAGE, a_url, error=str(exc)[:200]))
                continue
            if target.exists():
                record(S.Fetched(SourceKind.IMAGE, name, path=str(target), bytes=size,
                                 sha256=S.sha256_of(target)))
            continue

        target = dest / S.safe_name(name)
        try:
            size = client.download(a_url, target, max_bytes=S.MAX_TEXT_BYTES * 4)
        except Exception as exc:
            record(S.Fetched(SourceKind.FILE, name, error=str(exc)[:200]))
            continue

        md_path = target.with_suffix(".md")
        try:
            if ext == ".pdf":
                S.pdf_to_markdown(target, md_path)
                record(S.Fetched(SourceKind.PDF, name, path=str(target),
                                 markdown_path=str(md_path), bytes=size,
                                 sha256=S.sha256_of(target)))
            elif ext == ".docx":
                S.docx_to_markdown(target, md_path)
                record(S.Fetched(SourceKind.DOCX, name, path=str(target),
                                 markdown_path=str(md_path), bytes=size,
                                 sha256=S.sha256_of(target)))
            elif ext in S.ARCHIVE_EXTS:
                out = dest / (target.stem + "_extracted")
                ex = S.extract_zip(target, out)
                record(S.Fetched(SourceKind.ZIP, name, path=str(ex.path or ""),
                                 bytes=size, error=ex.error, meta=ex.meta))
            elif ext in S.TEXT_EXTS or ext in S.CODE_EXTS or ext == "":
                record(S.Fetched(SourceKind.TEXT, name, path=str(target), bytes=size,
                                 sha256=S.sha256_of(target)))
            else:
                record(S.Fetched(SourceKind.FILE, name, path=str(target), bytes=size,
                                 sha256=S.sha256_of(target)))
        except S.SourceError as exc:
            record(S.Fetched(SourceKind.FILE, name, path=str(target), error=str(exc)))

    # 3. inline comment text, which sometimes carries the real link
    body = ((raw_sub or {}).get("body") or "").strip()
    if body:
        md = dest / "comment.md"
        md.write_text(f"<!-- Canvas submission comment -->\n\n{body}", encoding="utf-8", newline=_NL)
        record(S.Fetched(SourceKind.TEXT, "canvas comment", markdown_path=str(md),
                         bytes=len(body)))

    return added


def _fetch_submission(client: CanvasClient, spec: AssignmentSpec,
                      unit: SubmissionUnit) -> dict[str, Any]:
    try:
        return client.get(
            f"/courses/{spec.course_id}/assignments/{spec.assignment_id}"
            f"/submissions/{unit.user_id}?include[]=attachments")
    except Exception:
        return {}


def _dumps(meta: dict[str, Any]) -> str:
    import json
    return json.dumps(meta or {})


def bootstrap_run(client: CanvasClient, store: Store, assignments_dir: Path,
                  course_id: int, assignment_id: int, model: str | None = None,
                  emit: Emitter | None = None) -> CollectionResult:
    """Fetch the assignment, derive the spec, and register every submission."""
    emit = emit or (lambda *_: None)
    raw = client.get_assignment(course_id, assignment_id)
    spec = spec_from_assignment(raw, client.base_url, course_id)
    from .spec import apply_overlay, spec_health
    spec = apply_overlay(spec, assignments_dir)

    run_id = store.upsert_run(spec, model)
    store.log(run_id, "bootstrap",
              f"{spec.title}: {len(spec.criteria)} criteria, "
              f"{spec.points_possible:g} points")
    for w in spec_health(spec):
        store.log(run_id, "bootstrap", w, level="warn")

    units = collect_units(client, spec)
    inserted = store.replace_submissions(run_id, units)
    store.set_run_status(run_id, "collected")
    emit("collect.done", {"run_id": run_id, "units": len(units), "new": inserted})
    return CollectionResult(run_id, spec, units, inserted, spec_health(spec))


def reset_workspace(run_dir: Path, unit_name: str, keep_material: bool = False) -> None:
    target = run_dir / "submissions" / S.slug(unit_name)
    if not target.exists():
        return
    if keep_material:
        for child in target.iterdir():
            if child.name == "material":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    else:
        shutil.rmtree(target)