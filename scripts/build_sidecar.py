"""Build the sidecar as a single standalone binary.

The packaged desktop app must work on a machine with no Python installed, so the
sidecar is frozen with PyInstaller. PyInstaller cannot cross-compile: the macOS
binary has to be built on macOS, the Windows binary on Windows. CI runs this once
per OS.

pymupdf4llm needs help. It imports pymupdf lazily and reaches for data files
relative to its package, and PyInstaller's static analysis cannot see either, so
the result imports fine and then fails when it tries to open a PDF. Those are the
hidden imports and datas below; if a new converter is added, check it against
this list rather than waiting for a user's first PDF to fail.

Usage:
    python scripts/build_sidecar.py [--output DIR] [--debug]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "sidecar"
DEFAULT_OUTPUT = ROOT / "apps" / "desktop" / "resources" / "sidecar"

BINARY_NAME = "grading-pipeline-sidecar"

# Imported for side effects: each registers a PyInstaller hook that bundles its
# own extension modules and data files.
HIDDEN_IMPORTS = [
    "pymupdf4llm",
    "pymupdf",
    "fitz",
    "pipeline.server",
    "pipeline.sources.pdf",
    "pipeline.sources.docx",
    "pipeline.sources.office",
    "pipeline.sources.web",
]

# pymupdf ships native libraries and lookup tables that static analysis misses.
collect_all = [
    "pymupdf4llm",
    "pymupdf",
    "docx",
    "html2text",
    "pydantic",
]


def binary_path(output: Path) -> Path:
    return output / (f"{BINARY_NAME}.exe" if sys.platform == "win32" else BINARY_NAME)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--debug",
        action="store_true",
        help="build a console build that keeps the window open and skips UPX",
    )
    args = parser.parse_args(argv)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "PyInstaller is not installed.\n"
            "  python -m pip install pyinstaller",
            file=sys.stderr,
        )
        return 1

    output: Path = args.output
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    name = f"{BINARY_NAME}-debug" if args.debug else BINARY_NAME
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        # Console build even on Windows: the launcher reads the port from stdout,
        # so the binary must have a stdout pipe rather than a GUI subsystem.
        "--console",
        "--name", name,
        "--distpath", str(output),
        "--workpath", str(output.parent / ".sidecar-build"),
        "--specpath", str(output.parent / ".sidecar-build"),
        "--paths", str(SIDECAR),
        "--hidden-import", HIDDEN_IMPORTS[0],
    ]
    for mod in HIDDEN_IMPORTS[1:]:
        cmd += ["--hidden-import", mod]
    for pkg in collect_all:
        cmd += ["--collect-all", pkg]
    if args.debug:
        cmd.append("--debug=all")
    cmd.append(str(SIDECAR / "grading_sidecar.py"))

    print(" ".join(cmd), file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode

    target = binary_path(output)
    if name != BINARY_NAME:
        shutil.move(str(output / (name + (".exe" if sys.platform == "win32" else ""))),
                    str(target))

    if not target.exists():
        print(f"expected {target} after a successful build", file=sys.stderr)
        return 1

    size_mb = target.stat().st_size / (1024 * 1024)
    print(f"{target}  ({size_mb:.1f} MiB)")

    # Prove the artifact runs before it ships. A frozen binary that imports but
    # fails on its first real call is the failure mode this catches.
    check = subprocess.run([str(target), "--selftest"])
    if check.returncode != 0:
        print("selftest failed: the binary does not run", file=sys.stderr)
        return 1
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
