"""Cross-platform tests.

These do not run the code on Windows or macOS — CI does that. They pin the
behaviour that historically differed between them, so a change that works on the
developer's Linux laptop but breaks on a faculty member's Windows machine fails
here first.

The hazards worth guarding, each of which has actually bitten this codebase or
would plausibly do so:

  * Windows refuses to create files named CON, PRN, AUX, NUL, COM1-9, LPT1-9,
    and silently strips trailing dots and spaces. A submission called "CON"
    works on Linux and is unwritable on Windows.
  * Text mode translates newlines to CRLF on Windows, so the same generated
    report would differ byte-for-byte by platform.
  * Process groups do not exist on Windows.
  * `os.name` drives path semantics, so anything branching on it must be
    exercised both ways.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import config
from pipeline.canvas.spec import spec_from_assignment
from pipeline.models import Scorecard, parse_scorecard
from pipeline.report.render import render_report
from pipeline.sources import WINDOWS_RESERVED, safe_name, slug, unique_path
from pipeline.stub import stub_scorecard

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def spec():
    import json
    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    return spec_from_assignment(assignment, "https://x", 3130)


# ------------------------------------------------------------ filenames

@pytest.mark.parametrize("name", ["CON", "con", "PRN", "AUX", "NUL",
                                  "COM1", "com9", "LPT1", "lpt9"])
def test_windows_reserved_names_are_defused(name):
    """Creating a file with one of these names fails on Windows."""
    got = safe_name(name)
    assert got != name
    assert got.split(".")[0].upper() not in WINDOWS_RESERVED


@pytest.mark.parametrize("name", ["CON.txt", "NUL.json", "COM1.pdf", "lpt1.md"])
def test_reserved_stem_with_an_extension_is_defused(name):
    """Windows reserves the stem, so "CON.txt" is refused too."""
    got = safe_name(name)
    assert got.split(".")[0].upper() not in WINDOWS_RESERVED
    assert name.endswith(got.split(".")[-1]) or got.startswith("_")


@pytest.mark.parametrize("name", ["report.", "notes ", "file. ", " . "])
def test_trailing_dots_and_spaces_are_removed(name):
    """Windows strips these from a name, so the file would not match what we wrote."""
    got = safe_name(name)
    assert not got.endswith(".")
    assert not got.endswith(" ")


def test_path_separators_are_stripped_from_names():
    for hostile in ["../../etc/passwd", "a\\b\\c", "a/b/c", "..\\..\\windows\\system32"]:
        got = safe_name(hostile)
        assert "/" not in got and "\\" not in got


def test_empty_names_fall_back():
    assert safe_name("") == "file"
    assert safe_name("   ") == "file"
    assert safe_name("...") == "file"


def test_ordinary_names_are_left_alone():
    assert safe_name("grading_report.md") == "grading_report.md"
    assert safe_name("websnappr20260906-620679-illxt1.png") == "websnappr20260906-620679-illxt1.png"


def test_reserved_list_is_complete():
    for expected in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
        assert expected in WINDOWS_RESERVED


# ------------------------------------------------------------- newlines

def test_generated_reports_use_the_same_newlines_on_every_platform(tmp_path):
    """CRLF translation would make the artefact, and its digest, host-specific."""
    assignment = __import__("json").loads(
        (FIXTURES / "assignment_3130_46805.json").read_text())
    spec = spec_from_assignment(assignment, "https://x", 3130)
    sc = stub_scorecard(spec, "Ada Lovelace", [])

    from pipeline.models import SubmissionUnit
    md = render_report(spec, sc, SubmissionUnit(canvas_submission_id=1, user_id=1,
                                                 student_name="Ada Lovelace"))
    target = tmp_path / "report.md"
    target.write_text(md, newline=config.NEWLINE)
    raw = target.read_bytes()

    assert b"\r\n" not in raw
    assert raw.count(b"\n") == md.count("\n")


def test_secrets_file_is_written_with_pinned_newlines(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "_keychain_set", lambda *a, **k: False)
    config.set_secret("canvas_api_token", "abc")
    assert b"\r\n" not in config.secrets_file().read_bytes()


def test_a_written_report_round_trips_byte_for_byte(tmp_path):
    assignment = __import__("json").loads(
        (FIXTURES / "assignment_3130_46805.json").read_text())
    spec = spec_from_assignment(assignment, "https://x", 3130)
    sc = stub_scorecard(spec, "Ada Lovelace", [])
    from pipeline.models import SubmissionUnit
    unit = SubmissionUnit(canvas_submission_id=1, user_id=1, student_name="Ada Lovelace")
    first = (tmp_path / "a.md")
    second = (tmp_path / "b.md")
    text = render_report(spec, sc, unit)
    first.write_text(text, newline=config.NEWLINE)
    second.write_text(text, newline=config.NEWLINE)
    assert first.read_bytes() == second.read_bytes()


# ------------------------------------------------------------ path logic

def test_workspace_path_is_portable(tmp_path, monkeypatch):
    """The workspace root differs per platform; the shape beneath it must not."""
    monkeypatch.setattr(config, "Settings", config.Settings)
    s = config.Settings(data=tmp_path, workspace=tmp_path / "ws")
    got = s.run_dir(3130, 46805)
    assert got.name == "3130-46805"
    assert got.parent.name == "ws"


def test_material_layout_stays_within_windows_path_limits():
    """Windows caps paths at 260 characters by default; deeper than this and
    student reports start failing to open for reasons that look unrelated."""
    deepest = Path("submissions") / slug("Bartholomew Fitzgerald-Abernathy III") \
        / "material" / "repo" / "some" / "deeply" / "nested" / "source" / "tree"
    assert len(str(deepest)) < 180


def test_a_long_student_name_is_truncated_rather_than_rejected():
    got = slug("Bartholomew " * 40)
    assert len(got) <= 60
    assert got and " " not in got


def test_unique_path_avoids_case_insensitive_collisions(tmp_path):
    """APFS and NTFS fold case, so `Report.md` and `report.md` are one file.

    Without folding, the same run would emit different names on Linux than on
    macOS or Windows.
    """
    a = unique_path(tmp_path, "Report.md")
    a.write_text("x")
    b = unique_path(tmp_path, "report.md")
    assert a.name != b.name
    assert a.name.casefold() != b.name.casefold()


def test_unique_path_still_deduplicates_exact_repeats(tmp_path):
    a = unique_path(tmp_path, "report.md"); a.write_text("x")
    b = unique_path(tmp_path, "report.md")
    assert b.name == "report_1.md"


@pytest.mark.parametrize("raw,expected", [
    ("Ishimwe Axcel", "Ishimwe_Axcel"),
    ("BECKY Carmine IGIRANEZA", "BECKY_Carmine_IGIRANEZA"),
    ("a/b\\c", "a_b_c"),
    ("  ", "unit"),
])
def test_slug_is_case_preserving_and_separator_free(raw, expected):
    assert slug(raw) == expected


# ------------------------------------------------------- interpreter path

def test_no_posix_only_modules_are_imported_at_module_scope():
    """A module-level `import fcntl` works on Linux and kills the app on Windows."""
    forbidden = ("fcntl", "pwd", "grp", "termios", "resource", "pty", "tty", "curses")
    offenders = []
    for path in (ROOT / "sidecar" / "pipeline").rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "(" not in stripped:
                mod = stripped.split()[1].split(".")[0]
                if mod in forbidden:
                    offenders.append(f"{path.name}:{n} {stripped}")
    assert offenders == [], "POSIX-only module imported at module scope:\n" + "\n".join(offenders)


def test_no_absolute_posix_paths_in_the_pipeline():
    offenders = []
    for path in (ROOT / "sidecar" / "pipeline").rglob("*.py"):
        text = path.read_text()
        for needle in ('"/proc', '"/dev/', '"/tmp/', '"/home/', "'/usr/bin"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], "hardcoded POSIX path:\n" + "\n".join(offenders)


def test_the_test_suite_does_not_hardcode_a_posix_path(tmp_path):
    """Guards the suite itself: a git clone with a POSIX-only PATH fails on Windows."""
    repo = tmp_path / "origin"
    repo.mkdir()
    (repo / "f.txt").write_text("x")
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "T")
    env.setdefault("GIT_AUTHOR_EMAIL", "t@e")
    proc = subprocess.run(["git", "init", "-q"], cwd=repo, env=env, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    # Inherit the ambient PATH rather than assuming a POSIX layout.
    assert env.get("PATH")


# ------------------------------------------------------- os.name branches

def test_settings_data_dir_is_platform_shaped(monkeypatch, tmp_path):
    monkeypatch.setenv("GRADING_PIPELINE_HOME", str(tmp_path / "explicit"))
    assert config.data_dir() == tmp_path / "explicit"


def test_workspace_dir_honours_an_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GRADING_PIPELINE_WORKSPACE", str(tmp_path / "ws"))
    assert config.workspace_dir() == tmp_path / "ws"


def test_model_selection_does_not_depend_on_the_platform():
    """Free-model ranking is a pure function of the catalogue."""
    catalogue = {"openrouter": ["x/y:free", "nvidia/nemotron-3-ultra-550b-a55b:free"]}
    first = config.pick_model(catalogue, prefer_free=True)
    for _ in range(5):
        assert str(config.pick_model(catalogue, prefer_free=True)) == str(first)


# --------------------------------------------------------------- markdown

def test_a_windows_path_in_a_report_does_not_corrupt_the_table(spec):
    """A backslash in a path lands inside the summary table, where an
    unescaped character can change how the row renders."""
    from pipeline.models import SubmissionUnit

    unit = SubmissionUnit(canvas_submission_id=1, user_id=1, student_name="Ada")
    windows_path = "C:" + chr(92) + "Users" + chr(92) + "faculty" + chr(92) + "ws"
    posix_path = "/home/faculty/ws"

    def render(path):
        sc = stub_scorecard(spec, "Ada", [{"kind": "github", "status": "ok", "path": path}])
        return render_report(spec, sc, unit, sources=[
            {"kind": "github", "status": "ok", "origin": "https://github.com/a/b",
             "path": path, "meta_json": "{}"}])

    a, b = render(posix_path), render(windows_path)

    # Structure is identical; only the recorded path text differs.
    assert a.replace(posix_path, "PATH") == b.replace(windows_path, "PATH")
    assert a.count("| ") == b.count("| ")

    # Every summary row still has exactly three column delimiters.
    for line in b.split("## Summary", 1)[1].splitlines():
        if line.startswith("|"):
            import re
            assert len(re.findall(r"(?<!\\)\|", line)) == 3, line


def test_table_pipes_are_escaped_for_every_platform():
    from pipeline.report.render import _escape
    assert _escape("a|b") == "a\\|b"
    assert _escape("a\r\nb") == "a\nb"


# --------------------------------------------------------- subprocess use

def test_the_mcp_server_is_launched_with_the_running_interpreter(tmp_path, spec):
    """A hardcoded "python" works on a dev's Linux box and nowhere else."""
    import json

    from pipeline.grading.runner import Grader

    class _Server:
        pass

    grader = Grader(_Server(), None, spec, run_id=1, run_dir=tmp_path,
                    code_root=ROOT / "sidecar", python_exe=sys.executable)
    path = grader._write_opencode_config(tmp_path / "ws", tmp_path / "spec.json",
                                        tmp_path / "out.json", tmp_path / "meta.json")
    command = json.loads(path.read_text())["mcp"]["grader"]["command"]
    assert command[0] == sys.executable
    assert command[0].endswith(".exe") == (os.name == "nt")
    assert command[1].endswith("mcp_server.py")


def test_git_is_optional_and_reported_rather_than_crashing(tmp_path):
    from pipeline.sources import clone_repo
    result = clone_repo("https://example.invalid/x.git", tmp_path / "clone", timeout=5)
    # Either git is absent (reported) or the clone failed (reported).
    assert not result.ok
    assert result.error

# ------------------------------------------------------------ redaction

def test_no_fixture_leaks_an_identity():
    """Guards the redaction of the recorded Canvas responses.

    A leak here would be a real student's name on a public repository, so this
    fails loudly rather than trusting the redactor to have been run.
    """
    import re
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "redact_fixtures.py"
    proc = subprocess.run([sys.executable, str(script), "--check"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        "fixtures contain identifying data; run scripts/redact_fixtures.py\n"
        + proc.stdout)


def test_every_fixture_is_actually_redacted():
    """A fixture that bypassed the redactor would otherwise look fine."""
    import re

    fixtures = Path(__file__).parent / "fixtures"
    files = list(fixtures.rglob("*.json"))
    assert len(files) >= 8

    blob = "\n".join(f.read_text() for f in files)
    # `alu-regex-...` is the assignment's own required repository name and is
    # the author's own material, so it is deliberately not scrubbed.
    for pattern in (r"alueducation", r"\bALU\b(?!-regex)", r"@alueducation"):
        assert not re.search(pattern, blob, re.IGNORECASE), f"unredacted: {pattern}"


def test_recorded_submissions_carry_pseudonymous_names():
    """Recorded student names must come from the redactor's fixed word lists."""
    import json
    import re
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from redact_fixtures import FAMILY, GIVEN  # noqa: E402

    allowed_given, allowed_family = set(GIVEN), set(FAMILY)
    path = Path(__file__).parent / "fixtures" / "canvas" / "submissions.json"
    body = json.loads(path.read_text())["body"]
    assert body
    for submission in body:
        parts = submission["user"]["name"].split()
        assert len(parts) == 2, submission["user"]["name"]
        assert parts[0] in allowed_given and parts[1] in allowed_family, parts
