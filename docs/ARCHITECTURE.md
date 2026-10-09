# Architecture

## The one idea

A scorecard is the source of truth. Markdown is a *rendering* of it, and the
Canvas payload is built from it. Nothing parses prose.

This exists because the previous system was a one-way string pipeline:

```
Canvas →[Selenium]→ JSON →[agent reads TASK.md]→ markdown →[regex+fuzzy]→ Canvas
```

Every join there was a stringly-typed guess. The consequences were visible in
the artefacts: a criterion heading that lost its justification paragraph, one
report graded twice with different scores, a JSON grade that disagreed with the
report beside it, and agent prose comparing one student to another.

## Shape

```
┌─ Electron main ────────────────────────────────────────────┐
│  spawns: python sidecar  ·  opencode serve                  │
│  window: wizard UI, markdown renderer, live agent console  │
└───────────────┬───────────────────────────────────────────┘
                │ HTTP + SSE
┌───────────────▼───────────────────────────────────────────┐
│ Python sidecar                                           │
│                                                          │
│  canvas/    client · spec (zero-config) · collect · publish
│  sources/   github pdf docx gdocs zip url  →  markdown    │
│  grading/   prompt · runner · mcp_server · validate        │
│  report/    scorecard + rubric  →  markdown (rendered)    │
│  store.py   SQLite — one row of truth per run              │
└──────────────────────────────────────────────────────────┘
```

## Non-negotiables

**All model work happens inside opencode.** The pipeline holds no model
credentials and never contacts a provider directly. It reads opencode's
catalogue to choose a model and asks opencode to run it. Model keys live in
opencode's own auth store.

**One session per student.** A fresh session per submission is the structural
fix for cross-student contamination, where an agent that has seen several
submissions starts writing comparative claims into a report.

**The agent never formats.** It authors prose and scores; the renderer supplies
every character of structure. A report cannot drift from its rubric because the
rubric drives the layout.

**`submit_scorecard` is the only write path.** It is an MCP tool. If the agent
does not call it, there is no scorecard — a detectable, retryable condition
rather than a missing file.

**Posting is optional.** Reports are the product. Manual grading from the
reports is a first-class outcome.

## Zero-config

`GET /assignments/:id?include[]=rubric` returns criterion ids, points, rating
tiers, instructions, submission types and group settings. That is enough to
derive the prompt, the validator's bounds, the report template and the posting
payload. Overlays (`assignments/<course>-<id>.yaml`) only carry what Canvas
cannot express: test commands, zero conditions, tiebreaks, criteria reserved for
a human, calibration anchors.

Because criterion ids come exact from the API, section-to-criterion fuzzy
matching is unnecessary and was retired.

## State

SQLite, one row of truth per run. Folder existence is never a status signal.
Every run is resumable: each submission carries an explicit status, so a re-run
picks up exactly where it stopped.


## Tests

    pip install -e ".[dev]" coverage

    python -m pytest sidecar/tests -q          # sidecar
    npm --prefix apps/desktop test             # Electron main-process logic

    python -m coverage run -m pytest sidecar/tests -q
    python -m coverage report

Canvas responses are replayed from fixtures recorded off the live instance, so
the tests assert on the shapes this Canvas actually returns rather than on the
documented ones:

    PYTHONPATH=sidecar python scripts/capture_canvas_fixtures.py

The recorder redacts credentials and personal identifiers before writing. Only
the network is faked — cloning, PDF and DOCX conversion and archive extraction
all run for real against files on disk.

Deliberately untested by the unit suite, and covered by hand against a live
assignment instead: the opencode HTTP calls in `grading/runner.py`, and the
WebSnappr screenshots, which are only ever read as images by the agent.

## Portability

The app targets Windows, macOS and Linux. CI (`.github/workflows/test.yml`)
runs the suite on all three across Python 3.11–3.13; the unit tests pin the
behaviour that has actually differed between them, so a regression fails on the
developer's machine first:

  * `safe_name` defuses Windows-reserved names (`CON`, `NUL`, `COM1`…) and
    trailing dots and spaces, because a submission called "CON" works on Linux
    and is unwritable on Windows.
  * `unique_path` folds case, since APFS and NTFS treat `Report.md` and
    `report.md` as one file and ext4 does not — otherwise the same run would
    emit different names per platform.
  * Generated artefacts are written with an explicit `\n`, so text mode cannot
    translate them to CRLF on Windows and make the same report differ
    byte-for-byte by host.
  * Termination walks the process tree: a POSIX process group on the others,
    `taskkill /T` on Windows, where groups do not exist. opencode is a child of
    the sidecar and would otherwise survive holding its port.
  * Interpreter discovery looks for `python.exe` under `Scripts` on Windows and
    the `bin/python3` layout elsewhere, with `py` as the launcher shim.
  * No POSIX-only module is imported at module scope and no absolute POSIX path
    appears in the package — both asserted by scanning the source.

Fixtures are recorded per platform-agnostic run; re-record them with
`scripts/capture_canvas_fixtures.py`.
