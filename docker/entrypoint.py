"""Container entry point.

The container shares the host's data directory with the desktop app: the
database with its saved sessions, the Canvas settings, the rendered reports
and the opencode sessions all live there. That is what lets the Docker
version discover a session the installed app started.

It also means the files the container writes into that directory have to
belong to the host user. A file created by root cannot be written by the
desktop app afterwards, and a file created by the desktop app cannot be
written by the container: the two versions would take turns breaking each
other. So the container takes the identity of the owner of the directory it
was given, and only stays root when there is no host user to be.

The second job is simply to start the sidecar.
"""

from __future__ import annotations

import os
import sys

# Where the container keeps its own writable home. It is not the host user's
# home: the container has no business writing there.
HOME = "/home/pipeline"


def _owner(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_uid, st.st_gid)


def _shared_paths() -> list[str]:
    return [p for p in (os.environ.get("GRADING_PIPELINE_HOME"),
                        os.environ.get("GRADING_PIPELINE_WORKSPACE")) if p]


def _host_identity() -> tuple[int, int] | None:
    """The host user whose directory we were given, if there is one.

    The owner of the shared directory is the person the files belong to.
    Nothing to share means there is nobody to be, and the container stays
    root.
    """
    owners = [o for o in (_owner(p) for p in _shared_paths()) if o and o[0] != 0]
    return owners[0] if owners else None


def _take_identity() -> None:
    """Become the host user whose files we are sharing."""
    if os.geteuid() != 0:
        return
    identity = _host_identity()
    if identity is None:
        # Nothing to share yet: a first run with no desktop app behind it.
        # The directories Docker created for the mounts are root's, and the
        # desktop app would not be able to write in them later, so they are
        # opened up instead — and the files written into them are left
        # writable by whoever turns up, which is the person who installs the
        # desktop app afterwards.
        os.umask(0)
        for path in _shared_paths():
            if not path or not os.path.isdir(path):
                continue
            if _owner(path) == (0, 0) and not os.listdir(path):
                try:
                    os.chmod(path, 0o777)
                except OSError:
                    pass
        return
    uid, gid = identity
    # Everything we write into has to belong to that user as well.
    for path in (os.environ.get("GRADING_PIPELINE_RUNTIME_HOME", ""),
                 *(_shared_paths()), HOME):
        if not path or not os.path.isdir(path):
            continue
        own = _owner(path)
        if own and own[0] == 0:
            try:
                os.chown(path, uid, gid)
            except OSError:
                pass
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = HOME


def main() -> int:
    _take_identity()
    if len(sys.argv) < 2:
        print("usage: entrypoint.py <command> [args...]", file=sys.stderr)
        return 2
    os.execvp(sys.argv[1], sys.argv[1:])
    return 0  # execvp does not return


if __name__ == "__main__":
    raise SystemExit(main())
