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
- **Your decision is the one that is published.** Change a figure in the
  rubric breakdown of a report and save it, and that figure becomes the
  score: it is written into the scorecard, which is what publishing reads.
  The agent's own verdict is kept beside it, so the two can be compared.

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
| macOS (Apple silicon) | `-mac-arm64.dmg` | Drag to Applications |
| macOS (Intel) | `-mac-x64.dmg` | Separate build: the frozen sidecar is native, not universal |
| Windows | `-win-x64.exe` | NSIS installer |
| Linux | `-linux-x86_64.AppImage`, `-linux-amd64.deb` | `chmod +x` the AppImage, or install the `.deb` |

Windows on ARM is not built.

**These builds are not code-signed.** macOS Gatekeeper will report the app as
"damaged" and Windows SmartScreen will warn about an unknown publisher. Both are
expected for an unsigned build and neither indicates a problem with the software.
See [Signing](#signing) below.

### Docker (recommended, any OS)

One container, no signing, no per-arch installers, no Python install. The
sidecar and the UI run in the container; you open it in your browser.

```sh
docker compose up --build
# open http://127.0.0.1:8765/app/
```

- The port is bound to `127.0.0.1` only. The sidecar holds your Canvas token
  and model key and has no login of its own, so never publish it on `0.0.0.0`.
- Needs Docker Desktop (macOS/Windows) or Docker Engine (Linux). Works on
  Apple Silicon, Intel, and Windows/WSL2 alike — there is one image.
- Without Docker, use the CLI or the desktop installer above.

#### It shares the data with the installed app

The container mounts the desktop app's data directory and its workspace, so
the two versions are the same app with the same history: a session you
started in the installed app is waiting for you in the container, and a
session you started in the container is waiting for you in the installed
app. The reports, the collected submissions and the model credentials are
one set of files.

- The data directory is the one the installed app uses. On Linux that is
  `~/.config/Grading Pipeline`; on macOS it is
  `~/Library/Application Support/Grading Pipeline`; on Windows it is the
  `AppData\Roaming\Grading Pipeline` folder. The default in `compose.yaml` is
  the Linux path, so on macOS and Windows put the right one in a `.env` file
  next to `compose.yaml` (`.env.example` lists the settings):
  ```sh
  GRADING_PIPELINE_HOST_DATA=~/Library/Application\ Support/Grading\ Pipeline
  ```
- The workspace is mounted at the same absolute path it has on the host
  (`~/grading-pipeline-workspace`), because the database records absolute
  paths to the collected submissions and the rendered reports. Set
  `GRADING_PIPELINE_HOST_WORKSPACE` in `.env` to move it.
- The container takes the identity of the owner of that directory, so the
  files it writes can still be written by the installed app afterwards.
- A Canvas token saved by the installed app usually lives in the OS
  keychain, which a container cannot reach. On a host without a keychain the
  token is in the shared `secrets.json` and is picked up as it is; when the
  host has one, put the token in `.env` next to `compose.yaml`:
  ```sh
  GRADING_PIPELINE_CANVAS_BASE_URL=https://school.instructure.com
  GRADING_PIPELINE_CANVAS_API_TOKEN=...
  OPENROUTER_API_KEY=...        # optional, the free models work without it
  ```
  Without them the app starts but Canvas calls fail with a missing-credentials
  error naming the exact variables.
- The opencode runtime stays inside the container: it is a platform binary,
  and the one the installed app downloaded is not the one a container can run.
- Two versions writing the same run at the same time will fight over it.
  Run one of them at a time.

#### Your own opencode account

The app uses the models you already have access to. Sign in the way you
normally do

```sh
opencode auth login
```

and those models are there in the app — a Go subscription, GitHub Copilot,
anything else you have signed into. The accounts are **copied** into the
app's own opencode store rather than shared, because two opencode processes
on one database is what a Windows machine with the opencode desktop app
installed runs into: the second one cannot configure the database and
opencode never starts, leaving the app with no models at all.

An OpenRouter key saved in the app still reaches the OpenRouter models: it
is handed to opencode as an environment variable, which opencode honours
alongside the accounts in the store.

The container mounts the same store, so it sees the same models. On Linux
that is `~/.local/share/opencode`, with the configuration in
`~/.config/opencode`; on macOS both live in
`~/Library/Application Support/opencode`. Set
`GRADING_PIPELINE_HOST_OPENCODE` and `GRADING_PIPELINE_HOST_OPENCODE_CONFIG`
in `.env` for those.

#### Installing on macOS

Gatekeeper refuses an unsigned app outright, and the wording is alarming: *"is
damaged and can't be opened"*. Nothing is wrong with the download. It is the one
thing an unsigned build cannot do.

Open the `.dmg`, drag **Grading Pipeline** into **Applications**, then:

```sh
xattr -dr com.apple.quarantine "/Applications/Grading Pipeline.app"
```

That removes the quarantine flag the browser or `curl` attaches to a downloaded
file, which is the thing Gatekeeper is actually refusing. Launch it normally
afterwards.

Without a terminal: open **System Settings → Privacy & Security**, scroll to the
bottom, and press **Open Anyway** under the blocked-app entry. That appears only
after you have tried to open the app once. Control-clicking the app and choosing
**Open** works too.

The app then runs with no further prompts. It is not signed or notarized, so
moving it between machines or re-downloading it means doing this again.

If macOS reports the app as damaged *after* clearing the quarantine flag, the
download is incomplete — re-download it rather than bypassing further checks.

#### Building the desktop app yourself

```sh
cd apps/desktop
npm install
python3 ../../scripts/build_sidecar.py     # writes resources/sidecar/
npm run build
```

Outputs land in `apps/desktop/dist/`.

## Model setup

The desktop app **ships its own opencode**, so there is nothing to install and
nothing to authenticate by hand. The pipeline still never calls a provider
itself: all model work happens inside that opencode process, and the pipeline
holds no provider client.

**OpenRouter key.** Free OpenRouter models work without one, so it is only
needed for paid models. Enter it in **Setup → OpenRouter key**. It is stored in
your system keychain, passed to opencode as `OPENROUTER_API_KEY`, and also
written to an app-owned `auth.json` so a bundled opencode picks it up. Clearing
the field and saving removes it. The key is never sent back to the renderer, so
it cannot appear in the DOM.

Using the CLI instead of the app? Install and sign in opencode yourself:

```sh
npm install -g opencode
opencode auth login
```

Free models are preferred by default, read from opencode's own catalogue.
`--no-prefer-free` opts out.

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

The publish stage has a **Select all** toggle, so a full cohort is one click
rather than one click per student. Already-published rows are excluded. Selecting
everything and publishing is still a two-step confirmation.

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

cd apps/desktop
npm install
npm run lint            # no-undef catches handlers that reference names which do not exist
npm test                # main-process tests, then renderer tests
```

The renderer tests run the real renderer in a DOM and click every button in
every stage. That is deliberate: two shipping bugs were invisible to tests that
read source rather than executing it, one of which was a button that threw on
every click. A test that clicks a handler also asserts the click reached the API,
because the app's own error handling swallows the exception that would otherwise
have been the signal.

Recorded Canvas responses in `sidecar/tests/fixtures/` are replayed rather than
hand-written, so the transport tests exercise real response shapes. They are
anonymised by `scripts/redact_fixtures.py`, and CI fails if any identifier
reappears.

```sh
python scripts/redact_fixtures.py --check
```

## The bundled agent runtime

Every grading decision goes through opencode, so the app ships its own copy
rather than asking anyone to install one. That means it also ships a *pinned*
one, and a pin that only ever moves at release time leaves a machine quietly on
last year's harness for a whole cohort.

So the sidecar probes npm for the current `opencode-ai` at startup and reports
what it found on the **Setup → Agent runtime** card. From there a newer version
can be installed.

Three rules, each because of how this can go wrong:

- **Nothing happens silently.** A newer opencode can change output for reasons
  that have nothing to do with the student in front of you. The update is
  offered, never pushed.
- **Nothing happens mid-run.** A binary that changes between two submissions
  of one cohort is worse than an old one. An installed update is staged and
  promoted at the next launch, which is the only moment nothing is in flight.
- **Nothing blocks.** The probe is a background thread with a six-second
  timeout. Offline means "could not check", never a failure.

The downloaded binary goes under the app's own data directory, never inside the
app bundle. Rewriting a code-signed bundle would invalidate its signature the
first time somebody installed certificates.

Self-update stops at the harness. The app itself still updates by installing a
new release.

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
