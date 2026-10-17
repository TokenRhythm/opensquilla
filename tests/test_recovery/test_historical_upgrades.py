from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from opensquilla.recovery import inspect_profile, reconcile_profile

FIXTURE_ROOT = Path(__file__).with_name("fixtures")
DESKTOP_MANIFEST = FIXTURE_ROOT / "desktop" / "released-profiles.json"
DESKTOP_SNAPSHOTS = FIXTURE_ROOT / "desktop" / "frozen-profile-snapshots.json"


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload


DESKTOP_CASES = _load_manifest(DESKTOP_MANIFEST)["cases"]
DESKTOP_SNAPSHOT_CASES = {
    snapshot["id"]: snapshot for snapshot in _load_manifest(DESKTOP_SNAPSHOTS)["snapshots"]
}


def _toml_string(value: Path) -> str:
    # JSON basic strings use the same escaping required by TOML basic strings.
    return json.dumps(str(value), ensure_ascii=False)


def _write_config(home: Path, template_name: str) -> None:
    template = (FIXTURE_ROOT / "templates" / template_name).read_text(encoding="utf-8")
    rendered = template.replace("{{STATE_DIR_TOML}}", _toml_string(home / "state"))
    rendered = rendered.replace(
        "{{LEGACY_WORKSPACE_TOML}}", _toml_string(home / "state" / "workspace")
    )
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(rendered, encoding="utf-8")


def _write_workspace(path: Path, *, identity: str) -> None:
    path.mkdir(parents=True, exist_ok=False)
    for name in ("USER.md", "SOUL.md", "IDENTITY.md", "MEMORY.md"):
        (path / name).write_text(f"synthetic {identity} {name}\n", encoding="utf-8")


def _write_session_database(home: Path, *, release_tag: str) -> None:
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / "sessions.db") as connection:
        connection.execute(
            "CREATE TABLE synthetic_sessions (session_id TEXT PRIMARY KEY, transcript TEXT)"
        )
        connection.execute(
            "INSERT INTO synthetic_sessions VALUES (?, ?)",
            (f"session-{release_tag}", "synthetic transcript; contains no user data"),
        )


def _write_released_config(home: Path, template_name: str) -> None:
    template_path = DESKTOP_SNAPSHOTS.parent / template_name
    template = template_path.read_text(encoding="utf-8")
    rendered = template.replace("{{STATE_DIR_TOML}}", _toml_string(home / "state"))
    rendered = rendered.replace(
        "{{LEGACY_WORKSPACE_TOML}}", _toml_string(home / "state" / "workspace")
    )
    assert "{{" not in rendered, f"unresolved fixture token in {template_path}"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(rendered, encoding="utf-8")


def _materialize_snapshot_entry(
    home: Path, case: dict[str, Any], entry: dict[str, Any]
) -> None:
    relative = Path(entry["path"])
    assert not relative.is_absolute() and ".." not in relative.parts
    target = home / relative
    kind = entry["kind"]
    if kind == "config":
        assert relative == Path("config.toml")
        _write_released_config(home, entry["template"])
    elif kind == "sqlite_sessions":
        assert relative == Path("state/sessions.db")
        _write_session_database(home, release_tag=case["release_tag"])
    elif kind == "identity_markdown":
        target.parent.mkdir(parents=True, exist_ok=True)
        identity = entry.get("identity", "release-layout")
        target.write_text(
            f"synthetic {case['id']} {identity} {target.name}\n", encoding="utf-8"
        )
    elif kind == "text":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry["content"], encoding="utf-8")
    else:  # pragma: no cover - malformed checked-in fixture
        raise AssertionError(f"unknown frozen snapshot entry kind: {kind}")


def _build_desktop_fixture(tmp_path: Path, case: dict[str, Any]) -> Path:
    home = tmp_path / "ApplicationData" / "OpenSquilla" / "opensquilla"
    snapshot = DESKTOP_SNAPSHOT_CASES[case["id"]]
    assert snapshot["release_tag"] == case["release_tag"]
    seen: set[str] = set()
    for entry in snapshot["tree"]:
        assert entry["path"] not in seen, f"duplicate frozen path: {entry['path']}"
        seen.add(entry["path"])
        _materialize_snapshot_entry(home, case, entry)
    return home


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | None]]:
    snapshot: dict[str, tuple[str, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = ("directory", None)
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        else:
            snapshot[relative] = ("special", None)
    return snapshot


def _read_session(home: Path) -> tuple[str, str]:
    with sqlite3.connect(f"file:{home / 'state' / 'sessions.db'}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT session_id, transcript FROM synthetic_sessions").fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


@pytest.mark.parametrize("case", DESKTOP_CASES, ids=lambda case: case["id"])
def test_released_desktop_profile_inspect_and_reconcile(
    tmp_path: Path, case: dict[str, Any]
) -> None:
    home = _build_desktop_fixture(tmp_path, case)
    database = home / "state" / "sessions.db"
    database_identity = (database.stat().st_dev, database.stat().st_ino)
    session_before = _read_session(home)
    tree_before_inspect = _tree_snapshot(home)

    inspected = inspect_profile(home, profile_kind="desktop-primary")

    assert (inspected.outcome, inspected.stable_code) == tuple(case["expected_inspect"])
    assert inspected.effective_workspace == home / case["expected_effective_workspace"]
    assert _tree_snapshot(home) == tree_before_inspect, "inspect must be completely read-only"

    canonical_before = _tree_snapshot(home / "workspace") if (home / "workspace").is_dir() else None
    legacy_before = (
        _tree_snapshot(home / "state" / "workspace")
        if (home / "state" / "workspace").is_dir()
        else None
    )

    reconciled = reconcile_profile(home, profile_kind="desktop-primary")

    assert (reconciled.outcome, reconciled.stable_code) == tuple(case["expected_reconcile"])
    assert reconciled.effective_workspace == home / case["expected_effective_workspace"]
    assert database.is_file()
    assert (database.stat().st_dev, database.stat().st_ino) == database_identity
    assert _read_session(home) == session_before
    assert not (home / "state" / "state" / "workspace").exists()

    layout = case["layout_template"]
    if layout == "pre-rc3-nested" and case["config_template"] == "desktop-unpinned.toml":
        assert (home / "workspace" / "SOUL.md").is_file()
        assert not (home / "state" / "workspace").exists()
        if case["include_nested_roles"]:
            assert (home / "skills" / "synthetic-skill" / "SKILL.md").is_file()
            assert (home / "session-archive" / "archive.json").is_file()
            assert (home / "router" / "calibration.json").is_file()
            assert (home / ".env").is_file()
            assert (home / "state" / "approvals.json").is_file()
    elif layout == "pre-rc3-nested":
        assert not (home / "workspace").exists()
        assert _tree_snapshot(home / "state" / "workspace") == legacy_before
    elif layout == "rc3-relocated-clean":
        assert _tree_snapshot(home / "workspace") == canonical_before
        assert not (home / "state" / "workspace").exists()
    else:
        # The RC3 stale pin conflict is intentionally preserved byte-for-byte.
        assert _tree_snapshot(home / "workspace") == canonical_before
        assert _tree_snapshot(home / "state" / "workspace") == legacy_before

    assert (home / "media" / "synthetic.txt").is_file()
    assert not (home / "state" / "state" / "sessions.db").exists()
