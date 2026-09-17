"""Pure planning: flags → actions / keep / manual / warnings."""

from __future__ import annotations

from pathlib import Path

from opensquilla.uninstall.inventory import (
    METHOD_DESKTOP,
    METHOD_DOCKER,
    METHOD_PIP,
    METHOD_PORTABLE,
    METHOD_SOURCE,
    METHOD_UNKNOWN,
    DataBucket,
    Inventory,
)
from opensquilla.uninstall.plan import PlanOptions, build_plan


def _inventory(
    home: Path,
    *,
    method: str = METHOD_PIP,
    program_paths: list[Path] | None = None,
    package_uninstall: list[str] | None = None,
    extra_buckets: list[DataBucket] | None = None,
    source_checkout: Path | None = None,
    home_recognized: bool = True,
) -> Inventory:
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("x")
    (home / ".env").write_text("KEY=1")
    buckets = [
        DataBucket("config.toml", home / "config.toml", "config", "config"),
        DataBucket(".env (secrets)", home / ".env", "config", "config"),
        DataBucket("state directory", state, "user-data", "state"),
    ]
    if extra_buckets:
        buckets.extend(extra_buckets)
    return Inventory(
        method=method,
        home=home,
        state_root=state,
        config_path=None,
        entrypoints=[],
        program_paths=program_paths or [],
        package_uninstall=package_uninstall
        or (
            []
            if method in (METHOD_DOCKER, METHOD_DESKTOP, METHOD_UNKNOWN)
            else ["pip", "uninstall"]
        ),
        buckets=buckets,
        services=[],
        receipt=None,
        notes=[],
        source_checkout=source_checkout,
        home_recognized=home_recognized,
    )


def _action_kinds(plan) -> list[str]:
    return [a.kind for a in plan.actions]


def test_default_keeps_all_data(tmp_path: Path) -> None:
    inv = _inventory(tmp_path / "home")
    plan = build_plan(inv, PlanOptions())
    # program is removed; no data deletion scheduled.
    assert "run-package-uninstall" in _action_kinds(plan)
    assert "remove-path" not in _action_kinds(plan)
    assert any("All user data" in k for k in plan.keep)


def test_purge_state_removes_state_keeps_config(tmp_path: Path) -> None:
    inv = _inventory(tmp_path / "home")
    plan = build_plan(inv, PlanOptions(purge_state=True))
    removed = [p for a in plan.actions if a.kind == "remove-path" for p in a.paths]
    assert any(Path(p).name == "state" for p in removed)
    assert all("config.toml" not in p and ".env" not in p for p in removed)
    assert any("config.toml" in k for k in plan.keep)


def test_purge_state_does_not_double_schedule_managed_toolchain_child(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    toolchains = home / "state" / "toolchains"
    toolchains.mkdir(parents=True)
    inv = _inventory(
        home,
        extra_buckets=[
            DataBucket("managed toolchains", toolchains, "runtime-data", "state")
        ],
    )

    plan = build_plan(inv, PlanOptions(purge_state=True))
    removed = [Path(path) for action in plan.actions for path in action.paths]

    assert home / "state" in removed
    assert toolchains not in removed


def test_purge_config_removes_secrets_keeps_state(tmp_path: Path) -> None:
    inv = _inventory(tmp_path / "home")
    plan = build_plan(inv, PlanOptions(purge_config=True))
    removed = [p for a in plan.actions if a.kind == "remove-path" for p in a.paths]
    assert any(p.endswith("config.toml") for p in removed)
    assert any(p.endswith(".env") for p in removed)
    assert all(Path(p).name != "state" for p in removed)


def test_purge_all_schedules_whole_home_removal(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    home = tmp_path / "user" / ".opensquilla"
    inv = _inventory(home)
    plan = build_plan(inv, PlanOptions(purge_all=True))
    tree_paths = [p for a in plan.actions if a.kind == "remove-tree" for p in a.paths]
    assert str(home) in tree_paths
    # Individual in-home buckets are subsumed, not double-listed.
    assert "remove-path" not in _action_kinds(plan)


def test_purge_all_unrecognized_home_falls_back_to_buckets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    home = tmp_path / "user" / ".opensquilla"
    inv = _inventory(home, home_recognized=False)
    plan = build_plan(inv, PlanOptions(purge_all=True))
    # Not recognized as an OpenSquilla home → no blanket rmtree; per-bucket instead.
    tree_paths = [p for a in plan.actions if a.kind == "remove-tree" for p in a.paths]
    assert str(home) not in tree_paths
    assert "remove-path" in _action_kinds(plan)
    assert any("not recognized" in w for w in plan.warnings)


def test_purge_all_refuses_protected_home(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # OPENSQUILLA_STATE_DIR misconfigured to $HOME itself.
    inv = _inventory(fake_home)
    plan = build_plan(inv, PlanOptions(purge_all=True))
    # No blanket rmtree of the home; warned instead; falls back to per-file removal.
    tree_paths = [p for a in plan.actions if a.kind == "remove-tree" for p in a.paths]
    assert str(fake_home) not in tree_paths
    assert any("Refusing to recursively delete" in w for w in plan.warnings)
    assert "remove-path" in _action_kinds(plan)


def test_docker_and_desktop_refuse_program_removal(tmp_path: Path) -> None:
    for method in (METHOD_DOCKER, METHOD_DESKTOP, METHOD_UNKNOWN):
        inv = _inventory(tmp_path / f"home-{method}", method=method)
        plan = build_plan(inv, PlanOptions())
        assert "run-package-uninstall" not in _action_kinds(plan)
        assert any(a.kind == "instructions" for a in plan.manual)


def test_portable_removes_venv_tree(tmp_path: Path) -> None:
    venv = tmp_path / ".venv-abc"
    venv.mkdir()
    inv = _inventory(tmp_path / "home", method=METHOD_PORTABLE, program_paths=[venv])
    plan = build_plan(inv, PlanOptions())
    tree_paths = [p for a in plan.actions if a.kind == "remove-tree" for p in a.paths]
    assert str(venv) in tree_paths


def test_outside_home_bucket_is_manual(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "scheduler.db"
    outside.parent.mkdir(parents=True)
    outside.write_text("s")
    bucket = DataBucket(
        "scheduler DB", outside, "user-data", "state", sidecars=("-wal",), outside_home=True
    )
    inv = _inventory(tmp_path / "home", extra_buckets=[bucket])
    plan = build_plan(inv, PlanOptions(purge_state=True))
    # Relocated-outside paths are never auto-deleted, only surfaced as manual.
    removed = [p for a in plan.actions if a.kind == "remove-path" for p in a.paths]
    assert str(outside) not in removed
    assert any("outside the OpenSquilla home" in a.summary for a in plan.manual)


def test_remove_source_dir_is_manual_only(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    inv = _inventory(
        tmp_path / "home",
        method=METHOD_SOURCE,
        package_uninstall=["pip", "uninstall"],
        source_checkout=checkout,
    )
    plan = build_plan(inv, PlanOptions(remove_source_dir=True))
    # The checkout is surfaced as manual; never an auto remove-tree.
    tree_paths = [p for a in plan.actions if a.kind == "remove-tree" for p in a.paths]
    assert str(checkout) not in tree_paths
    assert any(str(checkout) in a.paths for a in plan.manual)
