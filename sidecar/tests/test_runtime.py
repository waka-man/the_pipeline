"""The bundled agent runtime: version comparison, resolution and staging.

The tests here never touch the network and never run a binary. Both are the
whole risk of this module, so both are injected rather than reached for.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from pipeline import runtime as rt


@pytest.fixture()
def layout(tmp_path):
    return rt.RuntimeLayout(tmp_path / "data")


def write_runtime(layout, version, text="#!/bin/sh\necho v\n"):
    binary = layout.managed_binary(version)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(text)
    return binary


def stub_probe(monkeypatch, active="1.18.35", latest="1.19.0"):
    monkeypatch.setattr(rt, "read_version", lambda _b: active)
    monkeypatch.setattr(rt, "latest_version", lambda timeout=rt.PROBE_TIMEOUT: latest)


# ------------------------------------------------------------------ versions

def test_versions_compare_as_numbers_not_as_text():
    # The bug this prevents: "1.9.0" > "1.18.35" as strings, because '9' > '1'.
    assert rt.parse_version("1.19.0") > rt.parse_version("1.18.35")
    assert rt.parse_version("1.18.35") > rt.parse_version("1.18.9")
    assert rt.parse_version("2.0.0") > rt.parse_version("1.99.99")
    assert rt.parse_version("1.18.35") == rt.parse_version("v1.18.35")


def test_an_unparseable_version_is_unknown_rather_than_wrong():
    # A build that prefixes the version with a commit hash should not take the
    # feature down, and should not be compared as nonsense either.
    assert rt.parse_version("opencode 1.18.35 (abc1234)") == (1, 18, 35)
    assert rt.parse_version("no version here") is None
    assert rt.parse_version(None) is None
    assert rt.parse_version("") is None


def test_an_offline_registry_leaves_the_version_unknown(monkeypatch):
    monkeypatch.setattr(rt.requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    assert rt.latest_version() is None


# ----------------------------------------------------------------- resolution

def test_a_downloaded_runtime_wins_over_the_bundled_one(layout):
    # This is what makes a staged update take effect on the next launch: the
    # resolver prefers ours, so promoting it at startup is the whole mechanism.
    write_runtime(layout, "1.19.0")
    layout.write_active("1.19.0")
    chosen, source = rt.resolve_binary(layout, "/app/Contents/Resources/opencode/opencode")
    assert source == "managed", (chosen, source)
    assert "1.19.0" in chosen


def test_with_nothing_downloaded_the_bundled_one_is_used(layout):
    chosen, source = rt.resolve_binary(layout, "/app/Contents/Resources/opencode/opencode")
    assert chosen == "/app/Contents/Resources/opencode/opencode"
    assert source == "bundled"


def test_a_staged_update_does_not_displace_a_running_one(layout):
    write_runtime(layout, "1.19.0")
    layout.write_active("1.19.0")
    write_runtime(layout, "1.20.0")
    layout.write_pending("1.20.0")
    chosen, source = rt.resolve_binary(layout, "/bundled/opencode")
    assert source == "managed"
    assert "1.19.0" in chosen, f"a staged update displaced the running one: {chosen}"


def test_an_active_version_whose_binary_vanished_falls_back(layout):
    write_runtime(layout, "1.19.0")
    layout.write_active("1.19.0")
    layout.managed_binary("1.19.0").unlink()
    chosen, source = rt.resolve_binary(layout, "/bundled/opencode")
    assert source == "bundled", (chosen, source)


def test_a_half_finished_download_is_not_adopted(layout):
    # The directory exists but the binary does not: a download that died. Using
    # it would leave the app with nothing to launch.
    (layout.managed_root / "1.19.0").mkdir(parents=True)
    chosen, source = rt.resolve_binary(layout, "/bundled/opencode")
    assert source == "bundled", (chosen, source)


# ----------------------------------------------------------------- promotion

def test_a_downloaded_runtime_is_not_used_until_it_is_promoted(layout):
    # The bug: listing whatever was on disk meant a downloaded binary was
    # adopted the moment it landed, so the sidecar would switch builds between
    # two submissions of the same cohort.
    write_runtime(layout, "1.19.0")
    layout.write_pending("1.19.0")

    chosen, source = rt.resolve_binary(layout, "/bundled/opencode")
    assert source == "bundled", "a staged runtime was adopted without a restart"
    assert chosen == "/bundled/opencode"

    # Promotion is what puts it into use, and it is idempotent.
    manager = rt.RuntimeManager(layout.data, "/bundled/opencode")
    assert manager.promote() == "1.19.0"
    assert layout.read_pending() is None
    assert rt.resolve_binary(layout, "/bundled/opencode") == (
        str(layout.managed_binary("1.19.0")), "managed")

    assert manager.promote() is None
    assert rt.resolve_binary(layout, "/bundled/opencode")[1] == "managed"


def test_a_promotion_with_nothing_downloaded_is_dropped(layout):
    # The marker said one thing and the file says another. Pointing the next
    # run at a missing binary would be worse than not updating at all.
    layout.write_pending("1.19.0")
    manager = rt.RuntimeManager(layout.data, "/bundled/opencode")
    assert manager.promote() is None
    assert layout.read_pending() is None


def test_a_corrupt_pending_marker_is_ignored(layout):
    layout.managed_root.mkdir(parents=True, exist_ok=True)
    layout.pending_file.write_text("{not json", encoding="utf-8")
    assert layout.read_pending() is None
    assert rt.RuntimeManager(layout.data).promote() is None


# -------------------------------------------------------------------- probe

def test_the_probe_reports_an_available_update(layout, monkeypatch):
    stub_probe(monkeypatch, active="1.18.35", latest="1.19.0")
    mgr = rt.RuntimeManager(layout.data, "/bundled/opencode")
    st = mgr.probe()
    assert st.update_available is True, st.as_dict()
    assert st.active_version == "1.18.35"
    assert st.latest_version == "1.19.0"


def test_the_probe_does_not_cry_an_update_when_already_current(layout, monkeypatch):
    stub_probe(monkeypatch, active="1.19.0", latest="1.19.0")
    assert rt.RuntimeManager(layout.data, "/bundled/opencode").probe().update_available is False


def test_not_being_able_to_reach_the_registry_is_not_an_update_notice(layout, monkeypatch):
    # Offline must read as "we do not know", never as "there is an update".
    stub_probe(monkeypatch, active="1.18.35", latest=None)
    st = rt.RuntimeManager(layout.data, "/bundled/opencode").probe()
    assert st.update_available is False
    assert st.latest_version is None


def test_an_unknown_installed_version_offers_no_update(layout, monkeypatch):
    # Nothing installed and nothing readable: comparing against None would be
    # comparing against nonsense.
    stub_probe(monkeypatch, active=None, latest="1.19.0")
    assert rt.RuntimeManager(layout.data).probe().update_available is False


def test_a_staged_version_is_reported_as_pending(layout, monkeypatch):
    write_runtime(layout, "1.19.0")
    layout.write_pending("1.19.0")
    stub_probe(monkeypatch, active="1.18.35", latest="1.19.0")
    st = rt.RuntimeManager(layout.data, "/bundled/opencode").probe()
    assert st.pending_version == "1.19.0"
    # Not offered again: it is already downloaded and waiting.
    assert st.update_available is True


# ------------------------------------------------------------------ staging

def _tarball(path: Path, member: str, payload: bytes) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return path


def test_staging_a_verified_runtime_marks_it_pending(layout, monkeypatch):
    # Downloading is exercised by pointing the fetch at a tarball built here, so
    # no network and no 30MB.
    monkeypatch.setattr(rt, "_fetch_tarball", lambda name, version, dest: _tarball(dest, "package/bin/opencode", b"#!/bin/sh\n"))
    monkeypatch.setattr(rt, "_package_version", lambda name, wanted: "1.19.0")
    monkeypatch.setattr(rt, "read_version", lambda _b: "1.19.0")

    mgr = rt.RuntimeManager(layout.data, "/bundled/opencode")
    assert mgr.stage("1.19.0") == "1.19.0"
    assert layout.read_pending() == "1.19.0"
    assert layout.managed_binary("1.19.0").is_file()


def test_a_binary_that_reports_the_wrong_version_is_not_staged(layout, monkeypatch):
    # Promoting something that is not what was asked for is worse than staying
    # on the bundled build.
    monkeypatch.setattr(rt, "_fetch_tarball", lambda name, version, dest: _tarball(dest, "package/bin/opencode", b"#!/bin/sh\n"))
    monkeypatch.setattr(rt, "_package_version", lambda name, wanted: "1.19.0")
    monkeypatch.setattr(rt, "read_version", lambda _b: "1.18.35")

    mgr = rt.RuntimeManager(layout.data, "/bundled/opencode")
    with pytest.raises(RuntimeError, match="not 1.19.0"):
        mgr.stage("1.19.0")
    assert layout.read_pending() is None
    assert not layout.managed_binary("1.19.0").exists()


def test_a_tarball_without_the_expected_binary_is_refused(layout, monkeypatch):
    # This file is about to be executed. A tarball that does not contain
    # exactly the expected path is not one to pull an executable out of.
    monkeypatch.setattr(rt, "_fetch_tarball", lambda name, version, dest: _tarball(dest, "package/bin/something-else", b"x"))
    monkeypatch.setattr(rt, "_package_version", lambda name, wanted: "1.19.0")

    mgr = rt.RuntimeManager(layout.data, "/bundled/opencode")
    with pytest.raises(RuntimeError):
        mgr.stage("1.19.0")
    assert not layout.managed_binary("1.19.0").exists()


def test_a_platform_with_no_published_build_says_so(layout, monkeypatch):
    monkeypatch.setattr(rt, "platform_package", lambda: [])
    with pytest.raises(RuntimeError, match="No opencode build"):
        rt.RuntimeManager(layout.data).stage("1.19.0")


def test_the_baseline_variant_is_tried_for_x86_64(monkeypatch):
    monkeypatch.setattr(rt._platform, "system", lambda: "Linux")
    monkeypatch.setattr(rt._platform, "machine", lambda: "x86_64")
    # The prefix is `opencode-`, not `opencode-ai-`: these are the names in
    # opencode-ai's own optionalDependencies, which is where its postinstall
    # gets the binary from.
    assert rt.platform_package()[0] == "opencode-linux-x64", rt.platform_package()
    assert "opencode-linux-x64-baseline" in rt.platform_package()


def test_the_platform_package_names_are_the_ones_opencode_publishes(monkeypatch):
    # Caught a real one: the packages are opencode-darwin-arm64 and friends, not
    # opencode-ai-*, and getting it wrong is a silent 404 on every update.
    for system, machine, expected in [
        ("Darwin", "arm64", ["opencode-darwin-arm64"]),
        ("Darwin", "x86_64", ["opencode-darwin-x64", "opencode-darwin-x64-baseline"]),
        ("Linux", "aarch64", ["opencode-linux-arm64"]),
        ("Windows", "AMD64", ["opencode-windows-x64", "opencode-windows-x64-baseline"]),
    ]:
        monkeypatch.setattr(rt._platform, "system", lambda s=system: s)
        monkeypatch.setattr(rt._platform, "machine", lambda m=machine: m)
        assert rt.platform_package() == expected, (system, machine)


def test_an_unknown_architecture_is_not_guessed_at(monkeypatch):
    monkeypatch.setattr(rt._platform, "system", lambda: "Linux")
    monkeypatch.setattr(rt._platform, "machine", lambda: "riscv64")
    assert rt.platform_package() == []