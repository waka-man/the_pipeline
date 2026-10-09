"""Transport-level tests for material collection.

`collect_sources` decides what the grader agent will actually see, so a silent
mistake here is expensive: a skipped screenshot or a dropped PDF becomes an
incomplete grade rather than a visible error. These tests drive it with a fake
Canvas client and real files on disk, so cloning, conversion and extraction all
run for real — only the network is replaced.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from pipeline.canvas import collect as collect_mod
from pipeline.canvas.collect import collect_sources
from pipeline.canvas.spec import spec_from_assignment
from pipeline.models import SubmissionUnit
from pipeline.store import Store
from pipeline.sources import (
    Fetched,
    clone_repo,
    docx_to_markdown,
    extract_zip,
    fetch_google_doc,
    github_repo,
    google_doc_id,
    pdf_to_markdown,
    plain_text_to_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://example.instructure.com"


@pytest.fixture(scope="module")
def spec():
    assignment = json.loads((FIXTURES / "assignment_3130_46805.json").read_text())
    return spec_from_assignment(assignment, BASE_URL, 3130)


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path / "pipeline.db")


@pytest.fixture()
def run_dir(tmp_path):
    d = tmp_path / "run"
    (d / "submissions").mkdir(parents=True)
    return d


def make_local_repo(path: Path) -> Path:
    """A real git repository the tests can clone from."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# student work\n")
    (path / "main.py").write_text("import re\nprint('hi')\n")
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(path)}
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "work"]):
        subprocess.run(cmd, cwd=path, env=env, check=True, capture_output=True)
    return path


class FakeClient:
    """Serves byte payloads for attachment ids."""

    def __init__(self, submission: dict, payloads: dict[str, bytes]):
        self.submission = submission
        self.payloads = payloads
        self.downloaded: list[str] = []

    def get(self, path, **kw):
        return self.submission

    def download(self, url, dest, max_bytes=1 << 30):
        self.downloaded.append(url)
        for key, blob in self.payloads.items():
            if key in url:
                Path(dest).write_bytes(blob)
                return len(blob)
        raise AssertionError(f"no payload registered for {url}")


def att(aid: int, name: str, ctype: str) -> dict:
    return {"id": aid, "display_name": name, "content-type": ctype,
            "url": f"https://example.instructure.com/files/{aid}/download?download_frd=1"}


def png_bytes() -> bytes:
    # Smallest valid PNG: an 8-byte signature plus a minimal IHDR chunk.
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def docx_bytes() -> bytes:
    import docx
    d = docx.Document()
    d.add_heading("Design Notes", level=1)
    d.add_paragraph("The schema normalises sender and timestamp.")
    d.add_paragraph("A second paragraph.")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "field"
    t.rows[0].cells[1].text = "type"
    t.rows[1].cells[0].text = "id"
    t.rows[1].cells[1].text = "uuid"
    import io
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def pdf_bytes() -> bytes:
    """A minimal one-page PDF with extractable text."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "ALU Database Design Document", fontsize=18)
    page.insert_text((72, 140), "Entities: User, Message, Transaction", fontsize=11)
    blob = doc.tobytes()
    doc.close()
    return blob


def zip_bytes() -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("src/main.py", "print('inside archive')\n")
        zf.writestr("README.md", "# archived submission\n")
    return buf.getvalue()


def unit(name="Ada Lovelace", url=None):
    return SubmissionUnit(canvas_submission_id=1, user_id=7, student_name=name,
                          primary_url=url)


# ---------------------------------------------------------- source utils

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/ada/r", ("ada", "r")),
    ("https://github.com/ada/r.git", ("ada", "r")),
    ("https://www.github.com/ada/r/", ("ada", "r")),
    ("https://github.enterprise.com/ada/r", None),
    ("https://gitlab.com/ada/r", None),
])
def test_github_repo_detection(url, expected):
    assert github_repo(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://docs.google.com/document/d/abc123/edit?usp=sharing", ("document", "abc123")),
    ("https://docs.google.com/spreadsheets/d/XYZ_9-_-", ("spreadsheets", "XYZ_9-_-")),
    ("https://docs.google.com/presentation/d/p1/preview", ("presentation", "p1")),
    ("https://drive.google.com/file/d/abc", None),
])
def test_google_doc_detection(url, expected):
    assert google_doc_id(url) == expected


def test_clone_records_the_head_commit(tmp_path):
    repo = make_local_repo(tmp_path / "origin")
    result = clone_repo(str(repo), tmp_path / "clone")
    assert result.ok, result.error
    assert (tmp_path / "clone" / "main.py").is_file()
    assert len(result.meta["commit"]) == 40


def test_clone_replaces_a_previous_clone(tmp_path):
    repo = make_local_repo(tmp_path / "origin")
    dest = tmp_path / "clone"
    dest.mkdir()
    (dest / "stale.txt").write_text("left over")
    result = clone_repo(str(repo), dest)
    assert result.ok
    assert not (dest / "stale.txt").exists()


def test_clone_of_a_missing_remote_fails_cleanly(tmp_path):
    result = clone_repo(str(tmp_path / "nope"), tmp_path / "clone")
    assert not result.ok
    assert "clone failed" in (result.error or "")


def test_pdf_becomes_markdown(tmp_path):
    src = tmp_path / "doc.pdf"
    src.write_bytes(pdf_bytes())
    dest = tmp_path / "doc.md"
    text = pdf_to_markdown(src, dest)
    assert "Database Design" in text or "Design Document" in text
    assert dest.is_file()


def test_docx_becomes_markdown_with_headings_and_tables(tmp_path):
    src = tmp_path / "notes.docx"
    src.write_bytes(docx_bytes())
    dest = tmp_path / "notes.md"
    text = docx_to_markdown(src, dest)
    assert "# Design Notes" in text
    assert "normalises sender" in text
    assert "| field | type |" in text


def test_plain_text_passes_through(tmp_path):
    src = tmp_path / "notes.md"
    src.write_text("# Already markdown\n")
    assert "Already markdown" in plain_text_to_markdown(src, tmp_path / "out.md")


def test_zip_extracts_and_counts(tmp_path):
    src = tmp_path / "a.zip"
    src.write_bytes(zip_bytes())
    result = extract_zip(src, tmp_path / "out")
    assert result.ok
    assert (tmp_path / "out" / "src" / "main.py").read_text().strip() == "print('inside archive')"
    assert result.meta["files"] == 2


def test_google_doc_failure_is_reported_not_raised(tmp_path):
    result = fetch_google_doc("https://example.com/not-a-doc", tmp_path / "x.md")
    assert not result.ok
    assert "not a Google Docs URL" in (result.error or "")


# --------------------------------------------------------- collect_sources

def test_collect_clones_a_repository_and_records_the_commit(tmp_path, store, spec, run_dir,
                                                             monkeypatch):
    repo = make_local_repo(tmp_path / "origin")
    # The clone path is chosen because the URL looks like GitHub; point it at a
    # local repository so the test needs no network.
    monkeypatch.setattr(collect_mod.S, "github_repo",
                        lambda url: ("local", "origin") if url == str(repo) else None)
    store.replace_submissions(store.upsert_run(spec), [unit(url=str(repo))])
    client = FakeClient({"url": str(repo), "attachments": []}, {})

    found = collect_sources(client, spec, unit(url=str(repo)), 1, store, run_dir)

    kinds = [f["kind"] for f in found if f["ok"]]
    assert "github" in kinds
    entry = next(f for f in found if f["kind"] == "github")
    assert Path(entry["path"], "main.py").is_file()
    meta = json.loads(entry["meta_json"])
    assert len(meta["commit"]) == 40
    assert meta["last_author"] == "T"


def test_collect_downloads_a_screenshot(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(7, "proof.png", "image/png")], "body": ""},
                        {"7": png_bytes()})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    entry = next(f for f in found if f["kind"] == "image")
    assert entry["ok"] and Path(entry["path"]).suffix == ".png"
    persisted = next(r for r in store.sources_for(1) if r["kind"] == "image")
    assert persisted["bytes"] == len(png_bytes())
    assert persisted["sha256"]


def test_collect_converts_a_pdf_to_markdown(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(8, "design.pdf", "application/pdf")], "body": ""},
                        {"8": pdf_bytes()})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    entry = next(f for f in found if f["kind"] == "pdf")
    assert entry["ok"], entry.get("error")
    assert entry["markdown_path"] and Path(entry["markdown_path"]).is_file()
    assert "Design Document" in Path(entry["markdown_path"]).read_text()


def test_collect_converts_a_docx_to_markdown(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(9, "notes.docx", "application/vnd.ms-word")],
                         "body": ""}, {"9": docx_bytes()})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    entry = next(f for f in found if f["kind"] == "docx")
    assert entry["ok"], entry.get("error")
    assert "Design Notes" in Path(entry["markdown_path"]).read_text()


def test_collect_extracts_a_zip(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(10, "work.zip", "application/zip")], "body": ""},
                        {"10": zip_bytes()})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    entry = next(f for f in found if f["kind"] == "zip")
    assert entry["ok"], entry.get("error")
    assert (Path(entry["path"]) / "src" / "main.py").is_file()


def test_collect_keeps_source_files_as_they_are(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(11, "notes.txt", "text/plain")], "body": ""},
                        {"11": b"raw notes"})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    entry = next(f for f in found if f["kind"] == "text")
    assert entry["ok"] and Path(entry["path"]).read_text() == "raw notes"


def test_collect_records_a_comment_body(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [], "body": "my repo is https://github.com/ada/r"}, {})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    entry = next(f for f in found if f["origin"] == "canvas comment")
    assert entry["ok"]
    assert "github.com/ada/r" in Path(entry["markdown_path"]).read_text()


def test_a_failed_download_is_recorded_not_raised(tmp_path, store, spec, run_dir):
    """One bad attachment must not lose the rest of the submission."""
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(12, "gone.pdf", "application/pdf"),
                                         att(13, "fine.txt", "text/plain")], "body": ""},
                        {"13": b"still here"})

    found = collect_sources(client, spec, unit(), 1, store, run_dir)
    by_origin = {f["origin"]: f for f in found}
    assert by_origin["gone.pdf"]["ok"] is False
    assert by_origin["fine.txt"]["ok"] is True


def test_sources_are_persisted_with_status(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [att(14, "proof.png", "image/png")], "body": ""},
                        {"14": png_bytes()})

    collect_sources(client, spec, unit(), 1, store, run_dir)
    rows = store.sources_for(1)
    assert rows and all(r["status"] == "ok" for r in rows)
    assert rows[0]["sha256"] and rows[0]["path"].endswith(".png")


def test_collect_handles_a_submission_with_nothing(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])
    client = FakeClient({"attachments": [], "url": None, "body": ""}, {})
    assert collect_sources(client, spec, unit(), 1, store, run_dir) == []


def test_collect_survives_a_transport_error(tmp_path, store, spec, run_dir):
    store.replace_submissions(store.upsert_run(spec), [unit()])

    class Broken(FakeClient):
        def get(self, path, **kw):
            raise RuntimeError("connection reset")

    assert collect_sources(Broken({}, {}), spec, unit(), 1, store, run_dir) == []


def test_submission_fetch_failure_degrades_to_an_empty_submission(tmp_path, store, spec, run_dir):
    """A failure to read the submission must not lose the whole cohort entry."""
    store.replace_submissions(store.upsert_run(spec), [unit()])

    class Broken(FakeClient):
        def get(self, path, **kw):
            raise RuntimeError("connection reset")

    assert collect_sources(Broken({}, {}), spec, unit(), 1, store, run_dir) == []