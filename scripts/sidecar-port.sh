#!/usr/bin/env bash
# Print the port of the running grading-pipeline sidecar, or fail loudly.
#
# The port is resolved from the process's own socket rather than by probing
# /health across every local port, which picks up unrelated servers on a
# machine that is running anything else.
set -uo pipefail
PYTHON="${GRADING_PIPELINE_PYTHON:-python3}"

find_pid() {
  local p
  for p in $(pgrep -x python 2>/dev/null; pgrep -x python3 2>/dev/null; pgrep -x python3.12 2>/dev/null); do
    if tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "pipeline.server"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

pid="$(find_pid)" || { echo "no sidecar process" >&2; exit 1; }

port="$(ss -ltnp 2>/dev/null | grep "pid=${pid}," | grep -oE '127\.0\.0\.1:[0-9]+' | head -1 | cut -d: -f2)"

if [ -z "$port" ]; then
  port="$("$PYTHON" - "$pid" <<'PYEOF' 2>/dev/null || true
import re, sys, pathlib
pid = sys.argv[1]
inodes = set()
for fd in pathlib.Path(f"/proc/{pid}/fd").iterdir():
    try:
        m = re.match(r"socket:\[(\d+)\]", str(fd.resolve()))
        if m:
            inodes.add(m.group(1))
    except Exception:
        pass
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        rows = pathlib.Path(table).read_text().splitlines()[1:]
    except Exception:
        continue
    for line in rows:
        f = line.split()
        if len(f) > 9 and f[3] == "0A" and f[9] in inodes:
            print(int(f[1].split(":")[1], 16))
            raise SystemExit
PYEOF
)"
fi

if [ -z "$port" ]; then
  echo "sidecar pid $pid is not listening" >&2
  exit 1
fi

echo "$port"