#!/usr/bin/env bash
# Run the full pipeline for one course/assignment:
#   bootstrap -> collect -> grade -> render -> publish (dry run)
#
# Usage:
#   scripts/run_pipeline.sh <course> <assignment> [extra grade flags...]
#
# Publish is a dry run. Re-run the publish command yourself with --yes once you
# have read the reports.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $(basename "$0") <course> <assignment> [grade flags...]" >&2
  exit 2
fi

COURSE="$1"
ASSIGNMENT="$2"
shift 2

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [ -x "$ROOT/.venv/bin/grading-pipeline" ]; then
  GP=("$ROOT/.venv/bin/grading-pipeline")
elif command -v grading-pipeline >/dev/null 2>&1; then
  GP=(grading-pipeline)
else
  echo "grading-pipeline not found; run: pip install -e \".[dev]\"" >&2
  exit 1
fi

step() { echo; echo "=== $* ==="; }

step "bootstrap $COURSE/$ASSIGNMENT"
"${GP[@]}" bootstrap --course "$COURSE" --assignment "$ASSIGNMENT"

step "collect"
"${GP[@]}" collect --course "$COURSE" --assignment "$ASSIGNMENT"

step "grade"
"${GP[@]}" grade --course "$COURSE" --assignment "$ASSIGNMENT" "$@"

step "render"
"${GP[@]}" render --course "$COURSE" --assignment "$ASSIGNMENT"

step "publish (dry run)"
"${GP[@]}" publish --course "$COURSE" --assignment "$ASSIGNMENT"

step "status"
"${GP[@]}" status --course "$COURSE" --assignment "$ASSIGNMENT"

echo
echo "Reports are in the run directory. Review them, then publish for real:"
echo "  ${GP[*]} publish --course $COURSE --assignment $ASSIGNMENT --yes"
