"""Install-method detection, data-bucket cartography, and service discovery."""

from __future__ import annotations

from pathlib import Path

from opensquilla.uninstall import inventory
from opensquilla.uninstall.inventory import (
    METHOD_DESKTOP,
    METHOD_DOCKER,
    METHOD_PIP,
    METHOD_PIPX,
    METHOD_PORTABLE,
    METHOD_SOURCE,
    METHOD_UNKNOWN,
    METHOD_UV_TOOL,
    DataBucket,
    build_data_buckets,
)


def _isolate_detection(monkeypatch) -> None:
    """Neutralize ambient signals so each detection test is deterministic."""
    for var in (
        "OPENSQUILLA_INSTALL_METHOD",
        "OPENSQUILLA_DESKTOP",
        "OPENSQUILLA_RUNNING_IN_CONTAINER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(inventory, "_docker_image_install", lambda: False)
    monkeypatch.setattr(inventory, "_portable_venv_dir", lambda: None)
    monkeypatch.setattr(inventory, "_is_editable_install", lambda: False)
    monkeypatch.setattr(inventory, "_venv_ancestry", lambda: None)
    monkeypatch.setattr(inventory, "_has_distribution", lambda: True)


def test_detect_docker_image_layout(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setattr(inventory, "_docker_image_install", lambda: True)
    monkeypatch.setattr(inventory, "_is_editable_install", lambda: True)  # ignored under docker
    assert inventory.detect_install_method() == METHOD_DOCKER


def test_running_in_container_env_is_docker(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setenv("OPENSQUILLA_RUNNING_IN_CONTAINER", "1")
    assert inventory.detect_install_method() == METHOD_DOCKER


def test_pip_install_in_plain_container_is_not_docker(monkeypatch) -> None:
    # A devcontainer/CI pip install (has /.dockerenv but normal home, no signal)
    # must NOT be misdetected as docker — otherwise removal is wrongly refused.
    _isolate_detection(monkeypatch)  # _docker_image_install -> False, has dist -> True
    assert inventory.detect_install_method() == METHOD_PIP


def test_detect_desktop_env(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    assert inventory.detect_install_method() == METHOD_DESKTOP


def test_detect_portable_marker(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setattr(inventory, "_portable_venv_dir", lambda: Path("/x/.venv-abc"))
    assert inventory.detect_install_method() == METHOD_PORTABLE


def test_detect_source_editable(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setattr(inventory, "_is_editable_install", lambda: True)
    assert inventory.detect_install_method() == METHOD_SOURCE


def test_detect_uv_then_pipx_then_pip(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setattr(inventory, "_venv_ancestry", lambda: METHOD_UV_TOOL)
    assert inventory.detect_install_method() == METHOD_UV_TOOL
    monkeypatch.setattr(inventory, "_venv_ancestry", lambda: METHOD_PIPX)
    assert inventory.detect_install_method() == METHOD_PIPX
    monkeypatch.setattr(inventory, "_venv_ancestry", lambda: None)
    assert inventory.detect_install_method() == METHOD_PIP


def test_detect_unknown_when_no_distribution(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setattr(inventory, "_has_distribution", lambda: False)
    assert inventory.detect_install_method() == METHOD_UNKNOWN


def test_detect_explicit_source_env_maps_to_source(monkeypatch) -> None:
    _isolate_detection(monkeypatch)
    monkeypatch.setattr(inventory, "_has_distribution", lambda: False)
    monkeypatch.setenv("OPENSQUILLA_INSTALL_METHOD", "source")
    assert inventory.detect_install_method() == METHOD_SOURCE


def test_databucket_existing_paths_includes_wal_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    db.write_text("x")
    (tmp_path / "sessions.db-wal").write_text("w")
    (tmp_path / "sessions.db-shm").write_text("s")
    bucket = DataBucket("sessions", db, "user-data", "state", sidecars=("-wal", "-shm"))
    found = {p.name for p in bucket.existing_paths()}
    assert found == {"sessions.db", "sessions.db-wal", "sessions.db-shm"}


def test_databucket_glob(tmp_path: Path) -> None:
    (tmp_path / "config.toml.backup.1").write_text("a")
    (tmp_path / "config.toml.backup.2").write_text("b")
    bucket = DataBucket("backups", tmp_path / "config.toml.backup.*", "config", "config", glob=True)
    assert len(bucket.existing_paths()) == 2


def test_build_buckets_in_home_and_relocated(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "config.toml").write_text("x")
    outside = tmp_path / "elsewhere" / "scheduler.db"
    outside.parent.mkdir(parents=True)
    outside.write_text("s")
    monkeypatch.setenv("OPENSQUILLA_SCHEDULER_DB", str(outside))

    buckets = build_data_buckets(home, config=None)
    by_name = {b.name: b for b in buckets}
    assert by_name["config.toml"].purge_flag == "config"
    assert by_name["state directory"].purge_flag == "state"
    assert by_name["managed toolchains"].path == home / "state" / "toolchains"
    assert by_name["managed toolchains"].purge_flag == "state"
    sched = by_name["scheduler DB"]
    assert sched.outside_home is True
    assert sched.sidecars == ("-wal", "-shm")


def test_build_buckets_in_home_relocation_is_auto_purged(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    inhome = home / "relocated" / "scheduler.db"
    inhome.parent.mkdir(parents=True)
    inhome.write_text("s")
    monkeypatch.setenv("OPENSQUILLA_SCHEDULER_DB", str(inhome))

    buckets = build_data_buckets(home, config=None)
    sched = next(b for b in buckets if b.name == "scheduler DB")
    # A relocation that still lands under the home is auto-purged, not orphaned.
    assert sched.outside_home is False
    assert sched.purge_flag == "state"


def test_detect_services_reads_unit_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library/LaunchAgents/ai.opensquilla.gateway.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    unit = tmp_path / ".config/systemd/user/opensquilla.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]")

    services = inventory.detect_services()
    platforms = {s.platform for s in services}
    assert "launchd" in platforms
    assert "systemd" in platforms
