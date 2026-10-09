# Grading Pipeline

Collect Canvas submissions, have an agent grade them against the rubric, read the
reports, and post back only what you choose to post.

The pipeline treats a **scorecard** as the source of truth. Markdown is rendered
from the scorecard and the Canvas payload is built from the scorecard, so nothing
ever has to parse prose back out. The agent writes the analysis and never formats
the page.

```
Canvas ──collect──▶ normalised Markdown ──grade──▶ scorecard
                                                        │
                                         ┌──────────────┴──────────────┐
                                         ▼                             ▼
                                  Markdown report              Canvas payload
```

## What it does not do

- **It never writes a grade without confirmation.** Publishing is an explicit
  step, and it is safe to run twice.
- **It does not hold model credentials.** All model work happens inside
  [opencode](https://opencode.ai). The pipeline has no provider keys, makes no
  direct provider calls, and cannot leak a student's work to a model vendor you
  did not choose.
- **It does not use a Selenium browser.** Collection is Canvas REST only.
- **It is not a grader of final authority.** `submit_scorecard` exposes
  `needs_review`, and the reports are written to be read by a human.

## Install

Two supported paths. The CLI is the faster one to get running.

### CLI (all platforms)

```sh
pipx install grading-pipeline      # or: uv tool install grading-pipeline
grading-pipeline bootstrap --course 3130 --assignment 46805
```

From a checkout:

```sh
pip install -e ".[dev]"
```

Requires Python 3.11+. `pipx` and `uv` give you an isolated install with the
`grading-pipeline` command on your `PATH`.

### Desktop app

Download the installer for your platform from
[Releases](https://github.com/waka-man/the_pipeline/releases). No Python needed —
the sidecar is bundled inside the app.

| Platform | Artifact | Notes |
| --- | --- | --- |
| macOS | `.dmg` | Drag to Applications |
| Windows | `-setup.exe` | NSIS installer |
| Linux | `.AppImage`, `.deb` | `chmod +x` the AppImage, or install the `.deb` |

**These builds are not code-signed.** macOS Gatekeeper will report the app as
"damaged" and Windows SmartScreen will warn about an unknown publisher. Both are
expected for an unsigned build and neither indicates a problem with the software.
See [Signing](#signing) below.

#### Building the desktop app yourself

```sh
cd apps/desktop
npm install
python3 ../../scripts/build_sidecar.py     # writes resources/sidecar/
npm run build
```

Outputs land in `apps/desktop/dist/`.

## Model setup

Grading shells out to `opencode`, so **`opencode` must be installed and signed
in separately**. The pipeline never stores a model key.

```sh
npm install -g opencode
opencode auth login
```

Free models are preferred by default, read from opencode's own catalogue, so a
default install needs no billing. `--no-prefer-free` opts out.

## Commands

```
bootstrap   fetch the assignment + rubric from Canvas, register submissions
collect     download and normalise submission material to Markdown
grade       run the grading agent
render      render Markdown reports from scorecards
publish     write grades to Canvas
status      show run progress
```

Every command accepts `--run N`; `collect`, `grade`, `render` and `publish` also
accept `--course` and `--assignment` to work without an explicit run. `grade`
takes `--limit`, `--workers`, `--attempts`, `--dry-run` and `--include-needs-review`.

**`publish` is a dry run unless you pass `--yes`.** It prints the exact payload it
would send and writes nothing. `grade --dry-run` likewise prints prompts and
grades nothing. Check the payload before you commit to it.

Run `grading-pipeline <command> --help` for the full set.

## Configuration

Canvas credentials are read from the platform config directory, never from the
repository:

| Platform | Path |
| --- | --- |
| Linux | `~/.local/share/grading-pipeline/secrets.json` |
| macOS | `~/Library/Application Support/grading-pipeline/secrets.json` |
| Windows | `%APPDATA%\grading-pipeline\secrets.json` |

```json
{
  "base_url": "https://your.instructure.com",
  "api_token": "..."
}
```

## How the assignment is configured

Setup is **zero-config**: criteria, points, rating tiers, criterion ids,
instructions and group rules are all derived from the Canvas rubric. You do not
write a config file for an assignment that follows the normal pattern.

An optional overlay carries only what Canvas cannot express — zero conditions,
criteria reserved for a human, or a rubric note. See `docs/ARCHITECTURE.md`.

## Development

```sh
python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
PYTHONPATH=sidecar python -m pytest sidecar/tests -q
cd apps/desktop && npm install && npm test
```

Recorded Canvas responses in `sidecar/tests/fixtures/` are replayed rather than
hand-written, so the transport tests exercise real response shapes. They are
anonymised by `scripts/redact_fixtures.py`, and CI fails if any identifier
reappears.

```sh
python scripts/redact_fixtures.py --check
```

## Signing

Unsigned builds work but draw security warnings. To sign:

- **macOS** — an Apple Developer ID Application certificate, plus notarization
  credentials for the `.dmg`.
- **Windows** — an Authenticode code-signing certificate.

Set them as repository or environment secrets and the release workflow picks them
up automatically. The workflow signs only when the secrets are present, so
unsigned builds keep working in the meantime.

macOS needs notarization on top of signing, since Gatekeeper refuses a signed but
un-notarized app. That job is gated on a repository **variable** rather than a
secret, because the secrets context is not available in a job-level `if:`:

| Type | Name | Value |
| --- | --- | --- |
| Variable | `APPLE_NOTARIZE` | `true` |
| Secret | `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` | Apple Developer credentials |
| Secret | `WINDOWS_CERT_PFX_BASE64` | base64 of the Authenticode `.pfx` |
| Secret | `WINDOWS_CERT_PASSWORD` | that certificate's password |

Build a release with a tag:

```sh
git tag v0.1.0 && git push --tags
```

Workflow files are linted in CI with `actionlint`, because an invalid workflow
fails as an opaque zero-second stub run rather than a useful error.

## Licence

See `LICENSE` if present; otherwise all rights reserved pending a choice of
licence.
