"""The bundled opencode runtime: what is installed, what is newer, and swapping.

The app ships its own opencode so nobody has to install one, which means it also
ships a *pinned* one. Harnesses change; a pin that is only ever moved by hand
means a faculty machine quietly runs a year-old runtime for the whole of a
cohort, and nobody finds out until something breaks in a way nobody can explain.

So this module probes npm for the current `opencode-ai`, compares it with what
is actually installed, and can stage a newer one.

Three rules, each of which exists because of how this thing can go wrong:

  - **Never silently.** opencode is what produces the grades. A newer version
    can change output for reasons that have nothing to do with the student in
    front of you, so a swap is something a person agrees to and sees applied.
  - **Never mid-run.** A binary that changes between two submissions of the same
    cohort is worse than an old one. Staged updates are promoted at startup,
    which is the only moment nothing is in flight.
  - **Never block.** No network at startup, no waiting on a lock. Every call
    here has a timeout and returns something usable when offline.

The binary itself is fetched from npm's platform packages (`opencode-darwin-
arm64` and friends), which is what the npm package's own postinstall does. That
means a plain tarball fetch: no GitHub URLs to keep in step, and no node.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

REGISTRY = "https://registry.npmjs.org"
PACKAGE = "opencode-ai"
# The binary itself is not in that package. opencode-ai's postinstall resolves
# one of these per-platform packages, each holding nothing but the binary:
#   "optionalDependencies": { "opencode-darwin-arm64": "1.19.0", ... }
# Note the prefix is `opencode-`, not `opencode-ai-`.
BINARY_PREFIX = "opencode"

# Short on purpose. This runs while somebody is waiting to click something, and
# a probe that has not answered by now has told us what we need to know.
PROBE_TIMEOUT = 6
# A runtime download is tens of megabytes. Generous, because failing here is
# recoverable and a truncated binary is not.
DOWNLOAD_TIMEOUT = 900

_lock = threading.Lock()
_state: "RuntimeStatus | None" = None


# ------------------------------------------------------------------ versions

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """A comparable triple from a version string.

    Loosely parsed on purpose: `opencode --version` prints the bare version
    today, and a build that prefixes it with a commit hash or a `v` should not
    take the whole feature down. Returns None for anything unparseable so the
    caller can say "unknown" rather than compare nonsense.
    """
    if not text:
        return None
    m = _VERSION_RE.search(str(text))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def read_version(binary: str | Path | None) -> str | None:
    """The version of an installed opencode, or None if it will not say.

    Run with a short timeout because this is on the path to reporting to a user.
    A binary that hangs here is itself worth knowing about, but it must not
    hold up the page that is asking.
    """
    if not binary:
        return None
    try:
        proc = subprocess.run([str(binary), "--version"], capture_output=True,
                              text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    return out if parse_version(out) else None


def latest_version(timeout: int = PROBE_TIMEOUT) -> str | None:
    """The newest published version of the harness, or None if unreachable."""
    try:
        r = requests.get(f"{REGISTRY}/{PACKAGE}/latest", timeout=timeout,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        version = (r.json() or {}).get("version")
    except Exception:
        # Offline, DNS failure, a proxy that eats the request, a registry that
        # answers with something else. All of it means the same thing here: we
        # do not know, and that is not an error the user needs to see.
        return None
    return version if parse_version(version) else None


# ------------------------------------------------------------------ platform

def platform_package() -> list[str]:
    """npm packages that may hold a binary for this machine, best first.

    Mirrors the fallback order in opencode's own postinstall. The `-baseline`
    variants are for x64 CPUs without AVX2, which is a real thing on older
    teaching machines, so they are tried rather than assumed away.
    """
    sysname = _platform.system().lower()
    machine = _platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64"}.get(machine, machine)
    plat = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(sysname)
    if not plat or arch not in ("x64", "arm64"):
        return []
    base = f"{BINARY_PREFIX}-{plat}-{arch}"
    if plat == "linux" and arch == "x64":
        return [base, f"{base}-baseline"]
    if plat in ("darwin", "windows") and arch == "x64":
        return [base, f"{base}-baseline"]
    return [base]


# -------------------------------------------------------------------- layout

class RuntimeLayout:
    """Where runtimes live on disk.

    Two directories, and the distinction is the whole design. `bundled` is
    inside the app: read-only, code-signed, and replaced only by a new release.
    `managed` is ours, under application data, and is where an update goes so
    that nothing inside the signed bundle is ever rewritten.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data = Path(data_dir)

    @property
    def managed_root(self) -> Path:
        return self.data / "runtime"

    def managed_binary(self, version: str) -> Path:
        name = "opencode.exe" if os.name == "nt" else "opencode"
        return self.managed_root / version / name

    @property
    def pending_file(self) -> Path:
        return self.managed_root / "pending.json"

    @property
    def active_file(self) -> Path:
        """Which downloaded runtime startup has actually put into use.

        Separate from `pending.json` on purpose. Listing whatever is on disk
        would mean a downloaded binary was adopted the moment it landed, and the
        sidecar would quietly switch builds between two submissions of the same
        cohort — the exact thing "takes effect on the next launch" exists to
        prevent.
        """
        return self.managed_root / "active.json"

    def write_active(self, version: str) -> None:
        self.managed_root.mkdir(parents=True, exist_ok=True)
        tmp = self.active_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
        tmp.replace(self.active_file)

    def active_version(self) -> str | None:
        try:
            data = json.loads(self.active_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        version = data.get("version")
        if not parse_version(version):
            return None
        # An active version whose binary has gone is not an active version.
        return version if self.managed_binary(version).is_file() else None

    def write_pending(self, version: str) -> None:
        self.managed_root.mkdir(parents=True, exist_ok=True)
        tmp = self.pending_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
        tmp.replace(self.pending_file)

    def read_pending(self) -> str | None:
        try:
            data = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        version = data.get("version")
        return version if parse_version(version) else None

    def clear_pending(self) -> None:
        try:
            self.pending_file.unlink(missing_ok=True)
        except OSError:
            pass

    def installed_versions(self) -> list[tuple[int, int, int, str]]:
        """Every runtime we have downloaded, newest first."""
        if not self.managed_root.is_dir():
            return []
        out = []
        for child in self.managed_root.iterdir():
            if not child.is_dir():
                continue
            parsed = parse_version(child.name)
            if parsed and self.managed_binary(child.name).is_file():
                out.append((*parsed, child.name))
        return sorted(out, reverse=True)

    def promote_pending(self) -> str | None:
        """Move a staged runtime into place. Called once, at startup.

        Startup is the only moment this is safe: nothing is mid-submission, so
        the version cannot change between two students of the same cohort.
        """
        version = self.read_pending()
        if not version:
            return None
        if not self.managed_binary(version).is_file():
            # A download that never finished. Clear the marker rather than
            # pointing the next run at a missing file.
            self.clear_pending()
            return None
        self.write_active(version)
        self.clear_pending()
        return version


def resolve_binary(layout: RuntimeLayout, env_binary: str = "") -> tuple[str, str]:
    """The binary to run, and where it came from.

    A managed runtime wins over the bundled one. That is what makes a staged
    update take effect on the next launch and not a moment sooner.

    Returns (path, source) where source is "managed", "bundled" or "path".
    """
    active = layout.active_version()
    if active:
        return str(layout.managed_binary(active)), "managed"
    if env_binary:
        return env_binary, "bundled"
    found = shutil.which("opencode")
    return (found, "path") if found else ("opencode", "path")


# -------------------------------------------------------------------- status

@dataclass
class RuntimeStatus:
    """What we last learned, and whether it is worth trusting yet."""

    active_version: str | None = None
    active_source: str = "unknown"
    bundled_version: str | None = None
    latest_version: str | None = None
    checked: bool = False
    update_available: bool = False
    pending_version: str | None = None
    downloading: bool = False
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_version": self.active_version,
            "active_source": self.active_source,
            "bundled_version": self.bundled_version,
            "latest_version": self.latest_version,
            "checked": self.checked,
            "update_available": self.update_available,
            "pending_version": self.pending_version,
            "downloading": self.downloading,
            "error": self.error,
        }


class RuntimeManager:
    """Owns the probe and the download for one sidecar process."""

    def __init__(self, data_dir: Path, env_binary: str = "") -> None:
        self.layout = RuntimeLayout(data_dir)
        self.env_binary = env_binary
        self.status = RuntimeStatus()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ resolving

    def resolve(self) -> tuple[str, str]:
        return resolve_binary(self.layout, self.env_binary)

    def promote(self) -> str | None:
        """Called once at startup, before anything can use the binary."""
        return self.layout.promote_pending()

    # -------------------------------------------------------------- probing

    def probe(self) -> RuntimeStatus:
        """Read what is installed and what npm has. Cheap, cached, timeout-bounded."""
        with _lock:
            binary, source = self.resolve()
            active = read_version(binary)
            latest = latest_version()
            st = self.status
            st.active_version = active
            st.active_source = source
            st.latest_version = latest
            st.pending_version = self.layout.read_pending()
            st.checked = True
            if source == "managed":
                st.bundled_version = st.bundled_version or read_version(self.env_binary)
            else:
                st.bundled_version = active
            here = parse_version(active)
            there = parse_version(latest)
            st.update_available = bool(here and there and there > here)
            return st

    def probe_async(self) -> None:
        """Kick the probe off without waiting for it.

        Deliberately a thread and not an await: the app must be usable while
        this happens, and on a machine with no network it finishes in the
        timeout and changes nothing.
        """
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._safe_probe, name="runtime-probe",
                                        daemon=True)
        self._thread.start()

    def _safe_probe(self) -> None:
        try:
            self.probe()
        except Exception:
            pass

    # ------------------------------------------------------------- updating

    def download(self, version: str, on_progress=None) -> Path:
        """Fetch a runtime for this platform and stage it.

        Raises on failure rather than returning None: the caller shows the user
        an error, and a staged runtime that is quietly missing would leave them
        clicking Update again for a reason nobody could name.
        """
        packages = platform_package()
        if not packages:
            raise RuntimeError(
                f"No opencode build is published for {os.name} {_platform.machine()}.")
        wanted = parse_version(version)
        last: Exception | None = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in packages:
                # The variant packages are versioned alongside the main one, so
                # ask for this exact version rather than "latest".
                wanted_pkg = _package_version(name, wanted)
                if not wanted_pkg:
                    continue
                try:
                    archive = root / f"{name}.tgz"
                    _fetch_tarball(name, wanted_pkg, archive)
                    binary = _extract_binary(archive, root / name)
                    if binary is None:
                        continue
                    final = self.layout.managed_binary(version)
                    final.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(binary, final.with_suffix(".partial"))
                    os.replace(final.with_suffix(".partial"), final)
                    if os.name != "nt":
                        final.chmod(0o755)
                    return final
                except Exception as exc:  # try the next variant
                    last = exc
        raise RuntimeError(f"could not download opencode {version}: {last}")

    def stage(self, version: str, on_progress=None) -> str:
        """Download, then mark it pending. Takes effect at the next launch."""
        with _lock:
            self.status.downloading = True
        try:
            path = self.download(version, on_progress)
            if (read_version(path) or "") != version:
                # A binary that reports a different version is not the one that
                # was asked for, and promoting it would be worse than not
                # updating at all.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise RuntimeError(
                    f"downloaded opencode reports {read_version(path) or 'nothing'}, "
                    f"not {version}")
            self.layout.write_pending(version)
            with _lock:
                self.status.pending_version = version
                self.status.update_available = False
            return version
        finally:
            with _lock:
                self.status.downloading = False

    def stage_async(self, version: str) -> None:
        def run() -> None:
            try:
                self.stage(version)
            except Exception:
                pass

        threading.Thread(target=run, name="runtime-update", daemon=True).start()


# ------------------------------------------------------------------ fetching

def _package_version(name: str, wanted: tuple[int, int, int]) -> str | None:
    """The published version of a platform package, if it matches what we want."""
    try:
        r = requests.get(f"{REGISTRY}/{name}", timeout=PROBE_TIMEOUT,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        versions = (r.json() or {}).get("versions") or {}
    except Exception:
        return None
    for published in versions:
        if parse_version(published) == wanted:
            return published
    return None


def _fetch_tarball(name: str, version: str, dest: Path) -> None:
    url = f"{REGISTRY}/{name}/-/{name}-{version}.tgz"
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    fh.write(chunk)


def _extract_binary(archive: Path, into: Path) -> Path | None:
    """Pull the binary out of a platform tarball, refusing anything else.

    Refusing rather than trusting: this file is about to be executed with the
    user's privileges, and a tarball that does not contain exactly the expected
    path is not one we should be pulling executables out of.
    """
    into.mkdir(parents=True, exist_ok=True)
    wanted = "package/bin/opencode.exe" if os.name == "nt" else "package/bin/opencode"
    try:
        with tarfile.open(archive, "r:gz") as tf:
            try:
                member = tf.getmember(wanted)
            except KeyError:
                return None
            if not member.isfile():
                return None
            tf.extract(member, into, filter="data")
    except (tarfile.TarError, OSError):
        return None
    out = into / wanted
    return out if out.is_file() else None