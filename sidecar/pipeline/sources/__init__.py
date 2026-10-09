"""Source collection: turn a submission into material an agent can read.

Every fetcher produces one or more artifacts under the run directory and
records a `Source` row. Two output shapes are supported:

  * **text** — converted to Markdown, read directly by the agent
  * **image** — left as-is; the agent views it natively

Nothing is graded by the fetcher. It only normalises.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..models import SourceKind
from ..config import NEWLINE as _NL

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXTS = {".md", ".markdown", ".txt", ".rst", ".org", ".tex"}
CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".go",
    ".rs", ".rb", ".php", ".sh", ".bash", ".sql", ".html", ".css", ".scss", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".xml", ".csv", ".kt", ".swift", ".dart", ".lua",
    ".r", ".m", ".scala", ".pl", ".vue", ".svelte", ".ipynb", ".env.example", ".gitignore",
}
ARCHIVE_EXTS = {".zip"}
GDOC_URL_RE = re.compile(
    r"https?://docs\.google\.com/(document|spreadsheets|presentation)/d/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
GITHUB_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


class SourceError(RuntimeError):
    pass


@dataclass
class Fetched:
    kind: str
    origin: str
    path: str | None = None
    markdown_path: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Windows refuses to create files with these stems, and silently strips
# trailing dots and spaces from the ones it will. A submission called "CON"
# therefore becomes an unwritable path there and works fine on Linux, so the
# name has to be made safe unconditionally rather than per platform.
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, fallback: str = "file") -> str:
    keep = "".join(c for c in name if c.isalnum() or c in " .-_")
    keep = keep.strip().rstrip(".")
    # Trailing spaces survive .strip() when a dot follows them, and Windows
    # drops both, so the resulting name on disk would not match what we wrote.
    keep = keep.rstrip()
    if not keep:
        return fallback
    if keep.split(".")[0].upper() in WINDOWS_RESERVED:
        keep = f"_{keep}"
    return keep


def slug(name: str, maxlen: int = 60) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("_.-")
    return (out or "unit")[:maxlen]


def _taken(directory: Path, name: str) -> bool:
    """True if a file of this name is already there, folding case.

    macOS and Windows filesystems treat `Report.md` and `report.md` as one file;
    Linux does not. Checking case-insensitively keeps the generated layout
    identical on every platform instead of quietly differing by host.
    """
    if (directory / name).exists():
        return True
    try:
        target = name.casefold()
        return any(entry.name.casefold() == target for entry in directory.iterdir())
    except OSError:
        return False


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    base, ext = Path(safe_name(filename)).stem, Path(safe_name(filename)).suffix
    candidate = directory / f"{base}{ext}"
    i = 1
    while _taken(directory, candidate.name):
        candidate = directory / f"{base}_{i}{ext}"
        i += 1
    return candidate


# ------------------------------------------------------------ text formats

def pdf_to_markdown(src: Path, dest: Path) -> str:
    try:
        import pymupdf4llm
    except ImportError as exc:  # pragma: no cover
        raise SourceError(f"pymupdf4llm unavailable: {exc}") from exc
    try:
        text = pymupdf4llm.to_markdown(str(src))
    except Exception as exc:
        raise SourceError(f"PDF conversion failed: {exc}") from exc
    dest.write_text(text, encoding="utf-8", newline=_NL)
    return text


def docx_to_markdown(src: Path, dest: Path) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        raise SourceError(f"python-docx unavailable: {exc}") from exc
    try:
        document = docx.Document(str(src))
    except Exception as exc:
        raise SourceError(f"DOCX conversion failed: {exc}") from exc
    lines: list[str] = []
    for para in document.paragraphs:
        text = para.text.rstrip()
        style = (para.style.name or "").lower()
        if not text:
            continue
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "1"
            lines.append(f"{'#' * min(int(level), 6)} {text}")
        elif style.startswith("list"):
            lines.append(f"- {text}")
        else:
            lines.append(text)
    for table in document.tables:
        rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in table.rows]
        if not rows:
            continue
        lines.append("")
        lines.append("| " + " | ".join(rows[0]) + " |")
        lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
        for row in rows[1:]:
            lines.append("| " + " | ".join(row) + " |")
    out = "\n\n".join(lines)
    dest.write_text(out, encoding="utf-8", newline=_NL)
    return out


def plain_text_to_markdown(src: Path, dest: Path) -> str:
    text = src.read_text(encoding="utf-8", errors="replace")
    if src.suffix.lower() == ".md":
        dest.write_text(text, encoding="utf-8", newline=_NL)
        return text
    dest.write_text(text, encoding="utf-8", newline=_NL)
    return text


# ------------------------------------------------------------------ github

def github_repo(url: str) -> tuple[str, str] | None:
    m = GITHUB_REPO_RE.match(url.strip())
    return (m.group(1), m.group(2)) if m else None


def clone_repo(url: str, dest: Path, depth: int = 1, timeout: int = 180) -> Fetched:
    """Shallow-clone a repository into the run workspace.

    Cloning (rather than fetching individual raw files, as the older prompts
    instructed) gives the agent grep, glob and real file access, which is what
    makes evidence-based grading possible.
    """
    if shutil.which("git") is None:
        return Fetched(SourceKind.GITHUB, url, error="git is not installed")
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", str(depth), "--single-branch", url, str(dest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Fetched(SourceKind.GITHUB, url, path=str(dest),
                       error=f"clone timed out after {timeout}s")
    if proc.returncode != 0:
        return Fetched(SourceKind.GITHUB, url, path=str(dest),
                       error=f"git clone failed: {proc.stderr.strip()[:300]}")
    meta: dict[str, Any] = {}
    try:
        head = subprocess.run(["git", "-C", str(dest), "log", "-1", "--format=%H%n%an%n%aI"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        parts = head.split("\n")
        meta = {"commit": parts[0] if parts else None,
                "last_author": parts[1] if len(parts) > 1 else None,
                "last_commit_date": parts[2] if len(parts) > 2 else None}
    except Exception:
        pass
    return Fetched(SourceKind.GITHUB, url, path=str(dest), meta=meta)


# -------------------------------------------------------------- google doc

def google_doc_id(url: str) -> tuple[str, str] | None:
    m = GDOC_URL_RE.search(url)
    return (m.group(1).lower(), m.group(2)) if m else None


def fetch_google_doc(url: str, dest: Path, timeout: int = 45) -> Fetched:
    """Export a publicly-shared Google Doc/Sheet/Slide to Markdown.

    Canvas has no Google Docs integration, so this uses the public
    `export?format=html` endpoint. Works only for "anyone with the link".
    """
    import html2text as h2t
    import requests

    parsed = google_doc_id(url)
    if parsed is None:
        return Fetched(SourceKind.GOOGLE_DOC, url, error="not a Google Docs URL")
    doc_type, doc_id = parsed
    export = f"https://docs.google.com/{doc_type}/d/{doc_id}/export?format=html"
    try:
        resp = requests.get(export, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"})
    except Exception as exc:
        return Fetched(SourceKind.GOOGLE_DOC, url, error=f"export request failed: {exc}")
    if resp.status_code != 200:
        return Fetched(SourceKind.GOOGLE_DOC, url,
                       error=f"export returned {resp.status_code} "
                             f"(document is probably not shared publicly)")
    conv = h2t.HTML2Text()
    conv.ignore_links = False
    conv.ignore_images = False
    conv.body_width = 0
    md = conv.handle(resp.text)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"<!-- Source: {url} -->\n\n{md}", encoding="utf-8", newline=_NL)
    return Fetched(SourceKind.GOOGLE_DOC, url, markdown_path=str(dest),
                   bytes=len(md.encode()), meta={"export_url": export})


# -------------------------------------------------------------------- misc

def fetch_url(url: str, dest: Path, timeout: int = 45) -> Fetched:
    import html2text as h2t
    import requests

    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"})
    except Exception as exc:
        return Fetched(SourceKind.URL, url, error=f"fetch failed: {exc}")
    if resp.status_code != 200:
        return Fetched(SourceKind.URL, url, error=f"HTTP {resp.status_code}")
    ctype = (resp.headers.get("content-type") or "").lower()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        dest.write_bytes(resp.content)
        return Fetched(SourceKind.PDF, url, path=str(dest), bytes=len(resp.content))
    conv = h2t.HTML2Text()
    conv.ignore_links = False
    conv.ignore_images = True
    conv.body_width = 0
    dest.write_text(conv.handle(resp.text), encoding="utf-8", newline=_NL)
    return Fetched(SourceKind.URL, url, markdown_path=str(dest))


def extract_zip(src: Path, dest: Path, max_total: int = 400 * 1024 * 1024) -> Fetched:
    """Extract an archive, rejecting absolute paths and traversal."""
    if not zipfile.is_zipfile(src):
        return Fetched(SourceKind.ZIP, str(src), error="not a zip archive")
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with zipfile.ZipFile(src) as zf:
            for member in zf.namelist():
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    return Fetched(SourceKind.ZIP, str(src),
                                   error=f"unsafe path in archive: {member}")
                total += zf.getinfo(member).file_size
                if total > max_total:
                    return Fetched(SourceKind.ZIP, str(src), path=str(dest),
                                   error=f"archive exceeds {max_total} bytes uncompressed")
            zf.extractall(dest)
    except Exception as exc:
        return Fetched(SourceKind.ZIP, str(src), path=str(dest), error=f"extract failed: {exc}")
    return Fetched(SourceKind.ZIP, str(src), path=str(dest),
                   meta={"files": sum(1 for _ in dest.rglob("*") if _.is_file())})