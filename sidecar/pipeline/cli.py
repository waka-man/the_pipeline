"""Command line entry point.

    grading-pipeline bootstrap   --course 3130 --assignment 46805
    grading-pipeline collect     --run 1 [--limit 3]
    grading-pipeline grade       --run 1 [--limit 3] [--workers 3] [--dry-run]
    grading-pipeline render      --run 1
    grading-pipeline publish     --run 1 [--row 5] [--yes]
    grading-pipeline status      --run 1
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import config
from .canvas.collect import bootstrap_run, collect_sources
from .canvas.client import CanvasClient
from .canvas.spec import manual_only_criteria, spec_health
from .grading.runner import Grader, OpenCodeServer
from .models import SubmissionUnit
from .report.render import render_report, report_filename
from .store import Store
from .config import NEWLINE as _NL

DEFAULT_ASSIGNMENTS_DIR = Path(__file__).resolve().parents[3] / "assignments"
CODE_ROOT = Path(__file__).resolve().parents[1]


def _client() -> CanvasClient:
    s = config.Settings.load()
    s.check_canvas()
    return CanvasClient(s.canvas_base_url, s.canvas_api_token)


def _store() -> Store:
    return Store(config.Settings.load().db_path)


def _unit_from_row(row: dict[str, Any]) -> SubmissionUnit:
    from .models import SubmissionUnit as U
    return U(canvas_submission_id=int(row["canvas_submission_id"]), user_id=int(row["user_id"]),
             student_name=row["student_name"], sortable_name=row["sortable_name"] or "",
             group_id=row["group_id"], group_name=row["group_name"],
             workflow_state=row["workflow_state"] or "unsubmitted",
             submitted_at=row["submitted_at"], existing_score=row["existing_score"],
             primary_url=row["primary_url"], missing=bool(row["missing"]))


def _resolve_run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    store = _store()
    if getattr(args, "run", None):
        return args.run, store.get_run(args.run)
    run_id = store.find_run(args.course, args.assignment)
    if run_id is None:
        raise SystemExit(
            f"No run for course {args.course} assignment {args.assignment}. "
            "Run `bootstrap` first.")
    return run_id, store.get_run(run_id)


def _emit_printer(verbose: bool) -> Any:
    def emit(event: str, data: dict[str, Any]) -> None:
        if not verbose:
            return
        bits = " ".join(f"{k}={v}" for k, v in data.items() if k != "issues")
        print(f"  · {event:16} {bits}", flush=True)
    return emit


# --------------------------------------------------------------- commands

def cmd_bootstrap(args: argparse.Namespace) -> int:
    client = _client()
    client.probe()
    res = bootstrap_run(client, _store(), Path(args.assignments_dir),
                        args.course, args.assignment, model=args.model,
                        emit=_emit_printer(args.verbose))
    print(f"run_id           {res.run_id}")
    print(f"assignment       {res.spec.title}")
    print(f"criteria         {len(res.spec.criteria)} "
          f"({res.spec.total_points():g} of {res.spec.points_possible:g} points)")
    print(f"submissions      {len(res.units)} ({res.inserted} new)")
    print(f"group assignment {res.spec.is_group}")
    print(f"instructions     {len(res.spec.instructions_markdown)} chars of markdown")
    if res.warnings:
        print("\nwarnings:")
        for w in res.warnings:
            print(f"  ! {w}")
    print(f"\nnext: grading-pipeline collect --run {res.run_id}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    run_id, run = _resolve_run(args)
    spec = run["spec"]
    store = _store()
    client = _client()
    run_dir = config.Settings.load().run_dir(spec.course_id, spec.assignment_id)
    emit = _emit_printer(args.verbose)

    rows = store.submissions_for_run(run_id)
    if args.only_missing:
        rows = [r for r in rows if r["workflow_state"] != "submitted" or r["missing"]]
    if args.limit:
        rows = rows[: args.limit]

    ok = fail = 0
    for row in rows:
        unit = _unit_from_row(row)
        store.set_submission_status(int(row["id"]), "collecting")
        if not unit.is_submitted:
            store.set_submission_status(int(row["id"]), "no_submission")
            print(f"  – {unit.display_name}: no submission")
            ok += 1
            continue
        try:
            found = collect_sources(client, spec, unit, int(row["id"]), store,
                                    run_dir, emit=emit)
            store.set_submission_status(int(row["id"]), "collected")
            kinds = ", ".join(f"{f['kind']}{'' if f['ok'] else '!'}" for f in found) or "nothing"
            print(f"  ✓ {unit.display_name}: {kinds}")
            ok += 1
        except Exception as exc:
            store.set_submission_status(int(row["id"]), "collect_failed", str(exc)[:300])
            print(f"  ✗ {unit.display_name}: {exc}")
            fail += 1
    print(f"\ncollected {ok}, failed {fail} → {run_dir}")
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    run_id, run = _resolve_run(args)
    spec = run["spec"]
    store = _store()
    settings = config.Settings.load()
    run_dir = settings.run_dir(spec.course_id, spec.assignment_id)

    probe_server = OpenCodeServer()
    try:
        probe_server.start()
        catalogue = probe_server.catalogue()
    except Exception as exc:
        raise SystemExit(f"could not start opencode: {exc}")
    finally:
        probe_server.stop()

    available = sum(len(v) for v in catalogue.values())
    free = len(config.rank_free_models(catalogue.get("openrouter", [])))
    print(f"opencode offers  {available} models across {len(catalogue)} providers "
          f"({free} free)")

    choice = config.pick_model(catalogue, args.model, prefer_free=not args.no_prefer_free)
    if choice is None:
        raise SystemExit(
            "opencode reports no usable models. Add a key with "
            "`opencode auth login --provider openrouter`, or pass --model."
        )
    chosen = str(choice)
    store.set_run_model(run_id, chosen)
    print(f"model            {chosen}")
    print(f"model tier       {choice.tier}")
    print(f"output budget    {choice.max_output_tokens} tokens (lowered automatically if the key refuses)")

    GRADEABLE = ("collected", "grading", "failed", "needs_review")

    rows = store.submissions_with_status(run_id, GRADEABLE)
    if not args.include_needs_review:
        rows = [r for r in rows if r["status"] in ("collected", "grading", "failed")]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("nothing to grade; run `collect` first.")
        return 0

    server = OpenCodeServer(model=chosen,
                            max_output_tokens=choice.max_output_tokens)
    grader = Grader(server, store, spec, run_id, run_dir, CODE_ROOT,
                    python_exe=sys.executable,
                    emit=_emit_printer(args.verbose),
                    max_attempts=args.attempts, model=chosen,
                    budget=choice.max_output_tokens)

    if args.dry_run:
        from .grading.prompt import build_system_prompt, build_task_prompt
        row = rows[0]
        unit = _unit_from_row(row)
        sources = store.sources_for(int(row["id"]))
        print("\n--- system prompt (first 2000 chars) ---")
        print(build_system_prompt(spec, manual_only_criteria(spec))[:2000])
        print("\n--- task prompt ---")
        print(build_task_prompt(spec, unit, sources, str(run_dir)))
        return 0

    print(f"grading {len(rows)} submission(s), {args.workers} worker(s)\n")
    try:
        server.start()
    except Exception as exc:
        raise SystemExit(f"could not start opencode: {exc}")

    results: list[Any] = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for row in rows:
                unit = _unit_from_row(row)
                sources = store.sources_for(int(row["id"]))
                futures[pool.submit(grader.grade, int(row["id"]), unit, sources)] = unit
            for fut in as_completed(futures):
                unit = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    print(f"  ✗ {unit.display_name}: {type(exc).__name__}: {exc}")
                    store.set_submission_status(int(row["id"]), "failed", str(exc)[:300])
                    continue
                results.append(res)
                if res.status == "graded":
                    print(f"  ✓ {unit.display_name}: {res.earned:g}/{res.possible:g}")
                else:
                    print(f"  ! {unit.display_name}: {res.status} — "
                          f"{res.error or '; '.join(res.issues)[:120]}")
    finally:
        server.stop()

    graded = sum(1 for r in results if r.status == "graded")
    print(f"\ngraded {graded}/{len(rows)}")
    print(f"next: grading-pipeline render --run {run_id}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    run_id, run = _resolve_run(args)
    spec = run["spec"]
    store = _store()
    settings = config.Settings.load()
    run_dir = settings.run_dir(spec.course_id, spec.assignment_id)
    out_dir = run_dir / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    manual = manual_only_criteria(spec) if spec.policy.get("manual_only_criteria") else []
    spec.policy["_manual_only_ids"] = manual

    rows = store.submissions_with_status(run_id, ("graded", "needs_review", "reviewed"))
    if args.limit:
        rows = rows[: args.limit]
    written = 0
    for row in rows:
        sc = store.best_scorecard(int(row["id"]))
        if sc is None:
            continue
        unit = _unit_from_row(row)
        sources = store.sources_for(int(row["id"]))
        md = render_report(spec, sc, unit, sources=sources,
                           model=sc.model or run.get("model") or "",
                           prompt_version=sc.prompt_version)
        path = out_dir / report_filename(unit)
        path.write_text(md, encoding="utf-8", newline=_NL)
        store.save_report(int(row["id"]), str(path))
        written += 1
    print(f"rendered {written} report(s) → {out_dir}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    run_id, run = _resolve_run(args)
    spec = run["spec"]
    store = _store()
    client = _client()

    rows = store.submissions_with_status(run_id, ("graded", "reviewed"))
    if args.row:
        rows = [r for r in rows if int(r["id"]) == args.row]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("nothing to publish")
        return 0

    planned: list[tuple[dict[str, Any], dict[str, Any], float]] = []
    for row in rows:
        sc = store.best_scorecard(int(row["id"]))
        if sc is None:
            continue
        unit = _unit_from_row(row)
        payload, total = _canvas_payload(spec, sc)
        planned.append((row, payload, total))

    print(f"{'student':38} {'change':>14}  flags")
    print("-" * 78)
    for row, payload, total in planned:
        unit = _unit_from_row(row)
        before = row["existing_score"]
        change = "new" if before is None else f"{before:g} → {total:g}"
        nflags = sum(1 for _ in (payload.get("rubric_assessment") or {}))
        print(f"{unit.display_name[:37]:38} {change:>14}")

    if args.dry_run:
        print("\ndry run; nothing written to Canvas")
        print(json.dumps(planned[0][1], indent=2)[:1200] if planned else "")
        return 0
    if not args.yes:
        print("\nRe-run with --yes to write these to Canvas.")
        return 0

    written = failed = 0
    for row, payload, total in planned:
        unit = _unit_from_row(row)
        targets = [unit.user_id]
        if spec.is_group and not spec.grade_group_students_individually:
            # Canvas propagates a single group grade to every member.
            targets = [unit.user_id]
        for uid in targets:
            try:
                resp = client.grade(spec.course_id, spec.assignment_id, uid,
                                    posted_grade=total, rubric_assessment=payload)
                store.record_publication(int(row["id"]), payload,
                                         json.dumps(resp)[:500] if resp else None)
                written += 1
                print(f"  ✓ published {unit.display_name} → {total:g}")
            except Exception as exc:
                failed += 1
                print(f"  ✗ {unit.display_name}: {exc}")
    print(f"\npublished {written}, failed {failed}")
    return 0


def _canvas_payload(spec: Any, scorecard: Any) -> tuple[dict[str, Any], float]:
    """Build a rubric_assessment payload plus the overall posted grade."""
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
        assessment[c.canvas_criterion_id] = {
            "points": s.points_earned,
            "comments": comment[:500],
        }
    summary = scorecard.summary.strip()
    if summary:
        assessment["_comment"] = {"comments": summary[:1000]}
    return {k: v for k, v in assessment.items() if not k.startswith("_")}, total


def cmd_status(args: argparse.Namespace) -> int:
    run_id, run = _resolve_run(args)
    store = _store()
    counts = store.progress(run_id)
    print(f"run {run_id}: {run['title']}  [{run['status']}]  model={run.get('model')}")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for row in store.events_since(run_id)[-args.events:]:
        print(f"  [{row['level']:5}] {row['phase']:10} {row['message']}")
    return 0


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grading-pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bootstrap", help="fetch assignment + rubric, register submissions")
    b.add_argument("--course", type=int, required=True)
    b.add_argument("--assignment", type=int, required=True)
    b.add_argument("--model")
    b.add_argument("--assignments-dir", default=str(DEFAULT_ASSIGNMENTS_DIR))
    b.set_defaults(func=cmd_bootstrap)

    c = sub.add_parser("collect", help="download and normalise submission material")
    c.add_argument("--run", type=int)
    c.add_argument("--course", type=int)
    c.add_argument("--assignment", type=int)
    c.add_argument("--limit", type=int)
    c.add_argument("--only-missing", action="store_true")
    c.set_defaults(func=cmd_collect)

    g = sub.add_parser("grade", help="run the grading agent")
    g.add_argument("--run", type=int)
    g.add_argument("--course", type=int)
    g.add_argument("--assignment", type=int)
    g.add_argument("--limit", type=int)
    g.add_argument("--workers", type=int, default=3)
    g.add_argument("--attempts", type=int, default=2)
    g.add_argument("--model")
    g.add_argument("--no-prefer-free", action="store_true",
                   help="prefer the strongest model over the free tier")
    g.add_argument("--include-needs-review", action="store_true",
                   help="also retry submissions the agent previously failed on")
    g.add_argument("--dry-run", action="store_true", help="print prompts, grade nothing")
    g.set_defaults(func=cmd_grade)

    r = sub.add_parser("render", help="render markdown reports from scorecards")
    r.add_argument("--run", type=int)
    r.add_argument("--course", type=int)
    r.add_argument("--assignment", type=int)
    r.add_argument("--limit", type=int)
    r.set_defaults(func=cmd_render)

    pb = sub.add_parser("publish", help="write grades to Canvas")
    pb.add_argument("--run", type=int)
    pb.add_argument("--course", type=int)
    pb.add_argument("--assignment", type=int)
    pb.add_argument("--row", type=int)
    pb.add_argument("--limit", type=int)
    pb.add_argument("--yes", action="store_true", help="actually write to Canvas")
    pb.add_argument("--dry-run", action="store_true")
    pb.set_defaults(func=cmd_publish)

    s = sub.add_parser("status", help="progress and recent events")
    s.add_argument("--run", type=int)
    s.add_argument("--course", type=int)
    s.add_argument("--assignment", type=int)
    s.add_argument("--events", type=int, default=12)
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())