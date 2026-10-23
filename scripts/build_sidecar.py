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
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


class MCPCheckFailed(RuntimeError):
    """The packaged binary cannot serve the scorecard MCP tool."""


def check_mcp(target: Path) -> None:
    """Fail unless the packaged binary answers the MCP handshake.

    Serving HTTP proves nothing about the other mode of the same binary. The
    MCP server is the grader's only write path: when it cannot start, every
    turn ends in "no scorecard" and the UI blames the model. This sends the
    same messages opencode does, to the artifact that actually ships.
    """
    with tempfile.TemporaryDirectory(prefix="gp-mcp-") as tmp:
        spec_file = Path(tmp) / "spec.json"
        spec_file.write_text(
            json.dumps({"tool_schema": {"type": "object"}}), encoding="utf-8"
        )
        requests = (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        try:
            proc = subprocess.run(
                [str(target), "--mcp"],
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    **os.environ,
                    "GP_SPEC_PATH": str(spec_file),
                    "GP_OUT_PATH": str(Path(tmp) / "out.json"),
                    "GP_META_PATH": str(Path(tmp) / "meta.json"),
                    # The point is that the bundle alone can serve this. An
                    # inherited PYTHONPATH could silently supply the module from
                    # the checkout and hide a build that ships without it.
                    "PYTHONPATH": "",
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise MCPCheckFailed(
                "the binary did not exit after stdin closed; it may be serving "
                "HTTP instead of MCP"
            ) from exc
        if proc.returncode != 0:
            raise MCPCheckFailed(f"exit {proc.returncode}\n{proc.stderr[-2000:]}")
        responses: dict[int, dict] = {}
        for line in proc.stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and isinstance(message.get("id"), int):
                responses[message["id"]] = message
        if "protocolVersion" not in (responses.get(1, {}).get("result") or {}):
            raise MCPCheckFailed(
                "initialize did not return a protocolVersion\n"
                f"stdout={proc.stdout[:1000]!r}\n"
                f"stderr={proc.stderr[:1000]!r}"
            )
        tools = (responses.get(2, {}).get("result") or {}).get("tools") or []
        names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
        if "submit_scorecard" not in names:
            raise MCPCheckFailed(
                "tools/list did not advertise submit_scorecard "
                f"(got {sorted(names)!r})\n"
                f"stdout={proc.stdout[:1000]!r}\n"
                f"stderr={proc.stderr[:1000]!r}"
            )


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
    # Both modes: --selftest proves the freeze contains the modules, --smoke
    # proves the thing actually starts and answers. The second is the one that
    # catches a broken release, and it was missing.
    check = subprocess.run([str(target), "--selftest"])
    if check.returncode != 0:
        print("selftest failed: the binary does not run", file=sys.stderr)
        return 1
    print("selftest ok")

    smoke = subprocess.run([str(target), "--smoke"], timeout=120)
    if smoke.returncode != 0:
        print("smoke failed: the sidecar starts but does not serve", file=sys.stderr)
        return 1
    print("smoke ok")

    try:
        check_mcp(target)
    except MCPCheckFailed as exc:
        print(f"mcp check failed: {exc}", file=sys.stderr)
        return 1
    print("mcp ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
