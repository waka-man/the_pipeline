"""Console entry point for the sidecar.

Two launchers share this module:

- ``main.js`` from a checkout runs it with an interpreter and inserts the
  ``sidecar`` directory on ``sys.path``.
- The packaged app runs it as a frozen binary built by
  ``scripts/build_sidecar.py``, where there is no interpreter to select.

Both need identical behaviour, and the packaged app needs a way to check the
binary works before it tries to use it. ``--selftest`` exits 0 without binding a
port so the launcher can probe without a listen/dial race.
"""

from __future__ import annotations

import json
import sys


def selftest() -> int:
    """Import everything the sidecar needs, then exit 0."""
    import pipeline.server  # noqa: F401
    return 0


def smoke() -> int:
    """Actually start the server and talk to it, then exit.

    `--selftest` only imports, so it proves the freeze contains the modules and
    nothing about whether the thing starts. The startup path is where a release
    can break -- it is the code that binds a port, forks nothing and prints the
    line the launcher waits for -- and it was untested on any platform, which is
    how a sidecar shipped that never started on a Mac.

    Deliberately does exactly what the launcher does: start, read the port from
    stdout, call /health.
    """
    import json
    import urllib.request

    from pipeline.server import serve as _serve

    httpd, port = _serve()
    print(json.dumps({"port": port}), flush=True)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as r:
            body = json.loads(r.read())
        if not body.get("ok"):
            print(f"health returned {body}", file=sys.stderr)
            return 1
        print("smoke ok", flush=True)
        return 0
    except Exception as exc:
        # The reason, which --selftest can never give: a frozen build whose
        # startup path throws otherwise fails with nothing on stdout at all.
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        httpd.shutdown()


def serve() -> int:
    """Bind a loopback port, announce it on stdout, then idle.

    The port is printed as one JSON line so the launcher can read it instead of
    scanning for a listening socket. Everything after that is a sleep loop: the
    HTTP server runs on its own thread.
    """
    import time

    from pipeline.server import serve as _serve

    httpd, port = _serve()
    print(json.dumps({"port": port}), flush=True)
    while True:
        time.sleep(3600)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--selftest":
        return selftest()
    if args and args[0] == "--smoke":
        return smoke()
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
