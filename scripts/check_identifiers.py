"""Fail if anything identifying was committed.

Runs over tracked files, so it covers source and fixtures alike. Kept as literal
strings in a file that is itself public: this is the one place real identifiers
have to be written down, which is why it is short and why the list is reviewed
rather than grown casually.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Real people and the institution they belong to. Nothing here may appear in a
# tracked file. Adding an entry is fine; removing one is not, because the point
# is to catch a regression in the redactor.
FORBIDDEN: dict[str, str] = {
    "student surname": r"Kamikazi|Abdikarim|Irumva|Mwiza|Ikechukwu",
    "given name": r"Lilian",
    "operator name": r"Wakuma|Debela",
    "institution host": r"alueducation",
    "institution initials": r"\bALU\b",
    "live course": r"course 3099|course_id[\"']?\s*[:=]\s*3099",
}

# Files allowed to contain the patterns above: this one, and nothing else.
SELF = Path(__file__).name


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         text=True, check=True).stdout
    return [ROOT / p for p in out.split("\0") if p]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        if path.name == SELF or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in FORBIDDEN.items():
            for m in re.finditer(pattern, text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{path.relative_to(ROOT)}:{line}: {label}")
    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"{len(problems)} identifier(s) present in tracked files")
        return 1
    print(f"no identifiers in {len(tracked_files())} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
