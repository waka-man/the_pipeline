#!/usr/bin/env bash
# Start the desktop app in dev mode. The app drives the whole pipeline itself:
# bootstrap, collect, grade, render and publish are all done from its UI.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/apps/desktop"

# Electron refuses to run as root, and `sudo ... --no-sandbox` gets past that
# check only by breaking the GPU process, so catch it here with an explanation
# instead of a stack of unrelated Chromium errors.
if [ "$(id -u)" -eq 0 ]; then
  echo "do not run the app as root; run this script as your normal user." >&2
  echo "if the SUID sandbox complains, either:" >&2
  echo "  sudo chown root:root $APP/node_modules/electron/dist/chrome-sandbox" >&2
  echo "  sudo chmod 4755 $APP/node_modules/electron/dist/chrome-sandbox" >&2
  echo "or pass --no-sandbox to this script." >&2
  exit 1
fi

if [ ! -x "$APP/node_modules/.bin/electron" ]; then
  echo "electron not installed; running npm install in apps/desktop" >&2
  (cd "$APP" && npm install)
fi

# The sidecar runs from the checkout's venv, not a frozen binary.
if [ -z "${GRADING_PIPELINE_PYTHON:-}" ] && [ -x "$ROOT/.venv/bin/python" ]; then
  export GRADING_PIPELINE_PYTHON="$ROOT/.venv/bin/python"
fi

export GRADING_PIPELINE_DEV=1
cd "$APP"
exec ./node_modules/.bin/electron . "$@"
