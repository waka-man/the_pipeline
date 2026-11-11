"""The Docker deployment, and the sharing with the installed app.

The container and the desktop app have to agree on where the data lives. The
sessions, the Canvas settings and the rendered reports are one set of files,
and someone who has been using the installed app expects the container to
pick up where they left off rather than to start empty.

These tests read the deployment files rather than running a container: they
pin the promises the compose file makes, so a change that quietly breaks
the sharing fails here.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"
ENTRYPOINT = ROOT / "docker" / "entrypoint.py"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _service() -> dict:
    return _compose()["services"]["grading-pipeline"]


def _mounts() -> list[dict]:
    return [_m for _m in _service()["volumes"] if _m.get("type") == "bind"]


def _entrypoint():
    spec = importlib.util.spec_from_file_location("docker_entrypoint", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_container_shares_the_desktop_data_directory():
    """The database with the saved sessions lives in the data directory.

    Without this the container has its own empty database and knows nothing
    about the sessions the installed app started.
    """
    data = [m for m in _mounts() if m["target"] == "/data"]
    assert data, "the container does not mount a data directory"
    assert "Grading Pipeline" in data[0]["source"], data[0]["source"]


def test_the_workspace_is_shared_at_the_same_path():
    """The database records absolute paths, so both versions must agree.

    A report written by the desktop app says where it is on the host. The
    container has to see that path, or it cannot open what was written
    before it.
    """
    workspace = [m for m in _mounts() if m["target"] != "/data"]
    assert workspace, "the workspace is not shared"
    assert workspace[0]["source"] == workspace[0]["target"], workspace[0]
    env = _service()["environment"]
    assert env["GRADING_PIPELINE_WORKSPACE"] == workspace[0]["target"], env


def test_the_opencode_runtime_stays_inside_the_container():
    """The runtime is a platform binary and cannot be shared.

    The desktop app downloads a runtime for the machine it runs on. A
    container that adopted it would be running a binary built for another
    operating system, and opencode would not start at all.
    """
    env = _service()["environment"]
    assert env["GRADING_PIPELINE_RUNTIME_HOME"] == "/runtime", env
    assert env["GRADING_PIPELINE_RUNTIME_HOME"] != "/data"


def test_opencode_reads_the_store_of_the_person_using_it():
    """The accounts they signed into are the models they pay for.

    opencode keeps them in a store of their own, and both versions read
    that one: the container has it mounted, and the desktop app reads it
    where it is.
    """
    env = _service()["environment"]
    assert env["XDG_DATA_HOME"] == "/host-opencode", env
    mounted = [m for m in _mounts() if m["target"] == "/host-opencode/opencode"]
    assert mounted, "the person's opencode store is not mounted"
    assert "opencode" in mounted[0]["source"], mounted[0]["source"]


def test_the_container_becomes_the_host_user(monkeypatch):
    """Files the container writes have to belong to the host user.

    A file created by root cannot be written by the desktop app afterwards,
    and the two versions would take turns breaking each other.
    """
    entry = _entrypoint()
    monkeypatch.setattr(entry, "_owner", lambda path: (1000, 1000))
    monkeypatch.setenv("GRADING_PIPELINE_HOME", "/data")
    monkeypatch.setenv("GRADING_PIPELINE_WORKSPACE", "/ws")
    assert entry._host_identity() == (1000, 1000)


def test_the_container_stays_root_when_there_is_nothing_to_share(monkeypatch):
    """A first run with no desktop app behind it has no user to be."""
    entry = _entrypoint()
    monkeypatch.setattr(entry, "_owner", lambda path: (0, 0))
    monkeypatch.setenv("GRADING_PIPELINE_HOME", "/data")
    monkeypatch.setenv("GRADING_PIPELINE_WORKSPACE", "/ws")
    assert entry._host_identity() is None


def test_the_data_directory_is_the_one_the_desktop_app_uses(monkeypatch, tmp_path):
    """The path the container shares is the path the app writes to.

    The desktop app keeps its data in the Electron user directory, and the
    sidecar follows GRADING_PIPELINE_HOME when it is set. The container
    points it at the same place, so both versions read one database.
    """
    entry = _entrypoint()
    monkeypatch.setenv("GRADING_PIPELINE_HOME", str(tmp_path))
    monkeypatch.setenv("GRADING_PIPELINE_WORKSPACE", str(tmp_path / "ws"))
    assert entry._shared_paths()[0] == str(tmp_path)
    assert os.path.isdir(str(tmp_path))
