"""Execution: safe deletion + quiesce-before-delete ordering."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensquilla.uninstall import actions
from opensquilla.uninstall.actions import ActionResult, _safe_remove, execute
from opensquilla.uninstall.inventory import Inventory
from opensquilla.uninstall.plan import Action, UninstallPlan


def _bare_inventory(home: Path) -> Inventory:
    return Inventory(
        method="pip",
        home=home,
        state_root=home / "state",
        config_path=None,
        entrypoints=[],
        program_paths=[],
        package_uninstall=None,
        buckets=[],
        services=[],
        receipt=None,
        notes=[],
    )


def test_safe_remove_within_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / "state"
    target.mkdir(parents=True)
    (target / "f").write_text("x")
    ok, detail = _safe_remove(target, home=home, trusted_roots=set(), is_root_removal=False)
    assert ok and detail == "removed-tree"
    assert not target.exists()


def test_safe_remove_refuses_outside_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    ok, detail = _safe_remove(outside, home=home, trusted_roots=set(), is_root_removal=False)
    assert not ok and "outside" in detail
    assert outside.exists()


def test_safe_remove_unlinks_symlink_not_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target_dir = tmp_path / "real"
    target_dir.mkdir()
    (target_dir / "keep").write_text("important")
    link = home / "link"
    try:
        link.symlink_to(target_dir)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symbolic-link privilege is unavailable")
        raise
    ok, detail = _safe_remove(link, home=home, trusted_roots=set(), is_root_removal=False)
    assert ok and detail == "removed-symlink"
    assert not link.exists()
    assert (target_dir / "keep").exists()  # target tree untouched


def test_safe_remove_refuses_protected_root(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ok, detail = _safe_remove(
        fake_home, home=fake_home, trusted_roots={fake_home.resolve()}, is_root_removal=True
    )
    assert not ok and "protected root" in detail
    assert fake_home.exists()


def test_safe_remove_root_refuses_untrusted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    # A non-protected dir that is NOT in the trusted-root allowlist must be refused
    # for a tree-root removal (guards against a plan/receipt bug widening scope).
    target = tmp_path / "apps" / "something"
    target.mkdir(parents=True)
    ok, detail = _safe_remove(target, home=tmp_path, trusted_roots=set(), is_root_removal=True)
    assert not ok and "not a trusted removal root" in detail
    assert target.exists()


def test_safe_remove_root_allows_trusted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    target = tmp_path / "apps" / "venv-abc"
    target.mkdir(parents=True)
    ok, detail = _safe_remove(
        target, home=tmp_path, trusted_roots={target.resolve()}, is_root_removal=True
    )
    assert ok and detail == "removed-tree"
    assert not target.exists()


def test_execute_aborts_when_gateway_cannot_stop(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    monkeypatch.setattr(
        actions,
        "_quiesce_gateway",
        lambda inv, _ls=None: ActionResult("stop-gateway", "running", ok=False, detail="unmanaged"),
    )
    plan = UninstallPlan(method="pip", home=str(home))
    plan.actions = [
        Action("stop-gateway", "stop"),
        Action("remove-path", "Delete state", paths=[str(state)]),
    ]
    result = execute(plan, _bare_inventory(home))
    assert result.aborted is True
    assert result.ok is False
    assert state.exists()  # nothing deleted after the failed quiesce


def test_execute_removes_paths_after_successful_quiesce(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    (state / "sessions.db").write_text("x")
    monkeypatch.setattr(
        actions,
        "_quiesce_gateway",
        lambda inv, _ls=None: ActionResult("stop-gateway", "stopped", ok=True),
    )
    plan = UninstallPlan(method="pip", home=str(home))
    plan.actions = [
        Action("stop-gateway", "stop"),
        Action("remove-path", "Delete state", paths=[str(state)]),
    ]
    result = execute(plan, _bare_inventory(home))
    assert result.ok is True
    assert not state.exists()


def test_execute_runs_package_uninstall(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        actions,
        "_quiesce_gateway",
        lambda inv, _ls=None: ActionResult("stop-gateway", "ok", ok=True),
    )
    monkeypatch.setattr(actions, "_run_one", lambda cmd: calls.append(cmd) or 0)
    plan = UninstallPlan(method="pip", home=str(home))
    plan.actions = [
        Action("stop-gateway", "stop"),
        Action(
            "run-package-uninstall",
            "uninstall",
            commands=[["pip", "uninstall", "-y", "opensquilla"]],
        ),
    ]
    result = execute(plan, _bare_inventory(home))
    assert result.ok is True
    assert calls == [["pip", "uninstall", "-y", "opensquilla"]]


def test_execute_unregister_service_removes_unit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    unit = tmp_path / ".config/systemd/user/opensquilla.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]")
    monkeypatch.setattr(
        actions,
        "_quiesce_gateway",
        lambda inv, _ls=None: ActionResult("stop-gateway", "ok", ok=True),
    )
    monkeypatch.setattr(actions, "_run_one", lambda cmd: 0)
    plan = UninstallPlan(method="pip", home=str(tmp_path / "home"))
    plan.actions = [
        Action("stop-gateway", "stop"),
        Action(
            "unregister-service",
            "Unregister systemd",
            paths=[str(unit)],
            commands=[["systemctl", "--user", "disable", "--now", "opensquilla.service"]],
        ),
    ]
    result = execute(plan, _bare_inventory(tmp_path / "home"))
    assert result.ok is True
    assert not unit.exists()


def test_quiesce_fails_closed_on_live_gateway_pidfile(monkeypatch, tmp_path: Path) -> None:
    """A live gateway.pid (any port, written by `gateway run`) must block deletion."""
    import json
    import os
    from types import SimpleNamespace

    from opensquilla.cli import gateway_lifecycle as gl
    from opensquilla.gateway import config as gwconfig

    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    (state / "gateway.pid").write_text(json.dumps({"pid": os.getpid(), "start_ts": "t"}))

    # Isolate: lifecycle reports not_started; config resolves no relocated state dir.
    monkeypatch.setattr(
        gl.GatewayLifecycleManager, "status", lambda self: SimpleNamespace(state="not_started")
    )
    monkeypatch.setattr(
        gwconfig.GatewayConfig,
        "load",
        classmethod(
            lambda cls, p=None: SimpleNamespace(host="127.0.0.1", port=18791, state_dir=None)
        ),
    )

    r = actions._quiesce_gateway(_bare_inventory(home))
    assert r.ok is False
    assert "still running" in r.summary.lower()


def test_quiesce_ok_when_gateway_pidfile_is_stale(monkeypatch, tmp_path: Path) -> None:
    import json
    from types import SimpleNamespace

    from opensquilla.cli import gateway_lifecycle as gl
    from opensquilla.gateway import config as gwconfig

    home = tmp_path / "home"
    state = home / "state"
    state.mkdir(parents=True)
    # A PID that is not alive — stale pidfile, no real gateway.
    (state / "gateway.pid").write_text(json.dumps({"pid": 2147480000, "start_ts": "t"}))

    monkeypatch.setattr(
        gl.GatewayLifecycleManager, "status", lambda self: SimpleNamespace(state="not_started")
    )
    monkeypatch.setattr(
        gwconfig.GatewayConfig,
        "load",
        classmethod(
            lambda cls, p=None: SimpleNamespace(host="127.0.0.1", port=18791, state_dir=None)
        ),
    )

    r = actions._quiesce_gateway(_bare_inventory(home))
    assert r.ok is True
