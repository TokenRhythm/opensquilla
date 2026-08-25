from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import opensquilla.sandbox.upgrade_migration as upgrade_migration
from opensquilla.sandbox.upgrade_migration import SandboxUpgradeCoordinator


@pytest.mark.parametrize(
    ("legacy_mode", "canonical"),
    [
        ("standard", "safe"),
        ("trusted", "safe"),
        ("managed", "safe"),
        ("full", "full"),
    ],
)
def test_direct_update_preserves_mode_comments_and_unknown_fields(
    tmp_path: Path,
    legacy_mode: str,
    canonical: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
# retained comment
unknown_top = "keep"

[sandbox]
run_mode = "{legacy_mode}" # retained inline
mystery = 42
""".lstrip(),
        encoding="utf-8",
    )
    preferences = tmp_path / "desktop-preferences.json"
    preferences.write_text(
        json.dumps({"runMode": legacy_mode, "unknown": {"keep": True}}),
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.canonical_mode == canonical
    text = config.read_text(encoding="utf-8")
    assert "# retained comment" in text
    assert "# retained inline" in text
    parsed = tomllib.loads(text)
    assert parsed["sandbox"]["run_mode"] == canonical
    assert parsed["sandbox"]["mystery"] == 42
    assert parsed["unknown_top"] == "keep"
    assert json.loads(preferences.read_text()) == {
        "runMode": canonical,
        "unknown": {"keep": True},
    }


def test_direct_update_is_idempotent_and_keeps_one_snapshot(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "trusted"\n',
        encoding="utf-8",
    )
    database = tmp_path / "state" / "sessions.db"
    database.parent.mkdir()
    database.write_bytes(b"legacy-database")

    first = SandboxUpgradeCoordinator(tmp_path).run()
    second = SandboxUpgradeCoordinator(tmp_path).run()

    assert first.ok and second.ok
    assert second.status == "committed"
    assert (tmp_path / ".sandbox-upgrade-snapshot" / "state" / "sessions.db").read_bytes() == (
        b"legacy-database"
    )
    assert len(list(tmp_path.glob(".sandbox-upgrade-snapshot*"))) == 1


def test_prepared_agent_config_retries_to_committed_without_losing_the_agent(
    tmp_path: Path,
) -> None:
    raw = b'''agents = [
    { id = "qa-agent", name = "QA Agent", enabled = true },
]
'''
    config = tmp_path / "config.toml"
    config.write_bytes(raw)
    snapshot = tmp_path / upgrade_migration.SNAPSHOT_NAME
    snapshot.mkdir()
    (snapshot / "config.toml").write_bytes(raw)
    (snapshot / "manifest.json").write_text(
        json.dumps({"stores": [{"path": "config.toml"}]}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / upgrade_migration.JOURNAL_NAME).write_text(
        json.dumps(
            {
                "migrationVersion": upgrade_migration.MIGRATION_VERSION,
                "status": "prepared",
                "stores": ["config.toml"],
                "snapshot": str(snapshot),
                "error": (
                    "LosslessTomlPatchError: unsupported TOML key expression: { id"
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == "committed"
    assert (snapshot / "config.toml").read_bytes() == raw
    patched = config.read_bytes()
    assert b'{ id = "qa-agent", name = "QA Agent", enabled = true },' in patched
    assert tomllib.loads(patched.decode("utf-8"))["sandbox"]["run_mode"] == "full"
    journal = json.loads((tmp_path / upgrade_migration.JOURNAL_NAME).read_text())
    assert journal["status"] == "committed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_direct_update_snapshot_is_owner_only_on_posix(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "trusted"\n', encoding="utf-8")
    database = tmp_path / "state" / "sessions.db"
    database.parent.mkdir()
    database.write_bytes(b"legacy-database")
    config.chmod(0o644)
    database.chmod(0o644)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    snapshot = tmp_path / ".sandbox-upgrade-snapshot"
    directories = [snapshot, snapshot / "state"]
    files = [
        snapshot / "config.toml",
        snapshot / "state" / "sessions.db",
        snapshot / "manifest.json",
    ]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)


def _mock_windows_acl(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, Path, tuple[str, ...]]],
    *,
    fail_when: Any | None = None,
) -> None:
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        upgrade_migration,
        "_current_windows_user_sid",
        lambda **_kwargs: "S-1-5-21-1234",
    )

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[0] == "whoami":
            return SimpleNamespace(
                returncode=0,
                stdout=b'"DESKTOP\\owner","S-1-5-21-1234"\n',
                stderr=b"",
            )
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["OPENSQUILLA_UPGRADE_ACL_USER_SID"] == "S-1-5-21-1234"
        payload = kwargs["input"]
        assert isinstance(payload, bytes)
        items = json.loads(payload.decode("ascii"))
        assert isinstance(items, list) and items
        paths = [Path(item["path"]) for item in items]
        for path in paths:
            events.append(("acl", path, tuple(command)))
        failed = any(
            fail_when is not None and fail_when(path, events)
            for path in paths
        )
        result = {
            "count": len(items),
            "ids": [str(index) for index in range(len(items))],
            "pathUtf8Base64": [
                base64.b64encode(item["path"].encode("utf-8")).decode("ascii")
                for item in items
            ],
            "pathHashes": [
                upgrade_migration._acl_path_hash(Path(item["path"]))
                for item in items
            ],
        }
        assert kwargs["timeout"] > 0
        assert kwargs["creationflags"] == int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        assert "text" not in kwargs
        assert Path(command[0]).stem.casefold() in {"powershell", "pwsh"}
        assert tuple(command[1:5]) == (
            "-NoProfile",
            "-NonInteractive",
            "-NoLogo",
            "-EncodedCommand",
        )
        return SimpleNamespace(
            returncode=5 if failed else 0,
            stdout=json.dumps(result).encode("ascii"),
            stderr=b"access denied" if failed else b"",
        )

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)


def test_windows_acl_script_keeps_host_specific_acl_apis_bounded() -> None:
    script = upgrade_migration._WINDOWS_PRIVATE_ACL_SCRIPT

    assert "Get-Acl -LiteralPath $target" in script
    assert "Set-Acl -LiteralPath $target -AclObject $acl" in script
    assert "Select-Object" not in script
    assert "Where-Object" not in script
    assert "[System.IO.Directory]::GetAccessControl($target)" in script
    assert "[System.IO.File]::GetAccessControl($target)" in script
    assert "pathHashes" in script
    assert "pathUtf8Base64 = $verifiedPathUtf8Base64.ToArray()" in script
    assert "[Console]::OutputEncoding = $utf8WithoutBom" in script
    assert "paths = $verifiedPaths.ToArray()" not in script
    assert "-ne $fullControl" in script


def test_windows_acl_batch_round_trips_non_ascii_path_over_ascii_protocol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "中文快照"
    target.mkdir()

    def run(_command: list[str], **kwargs: object) -> SimpleNamespace:
        payload = kwargs["input"]
        assert isinstance(payload, bytes)
        assert payload.isascii()
        items = json.loads(payload.decode("ascii"))
        result = {
            "count": 1,
            "ids": ["0"],
            "pathUtf8Base64": [
                base64.b64encode(items[0]["path"].encode("utf-8")).decode("ascii")
            ],
            "pathHashes": [upgrade_migration._acl_path_hash(target)],
        }
        stdout = json.dumps(result, separators=(",", ":")).encode("ascii")
        assert stdout.isascii()
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    upgrade_migration._protect_windows_acl_batch(
        ((target, True),),
        windows_user_sid="S-1-5-21-1234",
    )


def test_windows_acl_batch_falls_back_to_windows_powershell_on_privilege_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "snapshot"
    target.mkdir()
    calls: list[str] = []
    timeouts: list[float] = []

    monkeypatch.setattr(
        upgrade_migration,
        "_windows_acl_shells",
        lambda: ("pwsh", "powershell"),
    )

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command[0])
        timeout = kwargs["timeout"]
        assert isinstance(timeout, (int, float))
        timeouts.append(timeout)
        if command[0] == "pwsh":
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"SeSecurityPrivilege",
            )
        result = {
            "count": 1,
            "ids": ["0"],
            "pathUtf8Base64": [base64.b64encode(str(target).encode()).decode("ascii")],
            "pathHashes": [upgrade_migration._acl_path_hash(target)],
        }
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(result).encode("ascii"),
            stderr=b"",
        )

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    upgrade_migration._protect_windows_acl_batch(
        ((target, True),),
        windows_user_sid="S-1-5-21-1234",
        deadline=upgrade_migration.time.monotonic() + 30,
    )

    assert calls == ["pwsh", "powershell"]
    assert 0 < timeouts[1] <= timeouts[0]


@pytest.mark.parametrize(
    "case",
    ["malformed_base64", "invalid_utf8", "count", "path", "hash"],
)
def test_windows_acl_batch_rejects_malformed_or_mismatched_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
) -> None:
    target = tmp_path / "中文快照"
    target.mkdir()
    encoded_path = base64.b64encode(str(target).encode("utf-8")).decode("ascii")
    result: dict[str, object] = {
        "count": 1,
        "ids": ["0"],
        "pathUtf8Base64": [encoded_path],
        "pathHashes": [upgrade_migration._acl_path_hash(target)],
    }
    if case == "malformed_base64":
        result["pathUtf8Base64"] = ["not base64!"]
    elif case == "invalid_utf8":
        result["pathUtf8Base64"] = [
            base64.b64encode(b"\xff").decode("ascii")
        ]
    elif case == "count":
        result["count"] = 2
    elif case == "path":
        result["pathUtf8Base64"] = [
            base64.b64encode(b"C:\\wrong").decode("ascii")
        ]
    else:
        result["pathHashes"] = ["0" * 64]

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(result).encode("ascii"),
            stderr=b"",
        )

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    with pytest.raises(OSError, match="Windows ACL helper"):
        upgrade_migration._protect_windows_acl_batch(
            ((target, True),),
            windows_user_sid="S-1-5-21-1234",
        )


def test_whoami_fallback_parses_sid_from_non_utf8_bytes_and_caps_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_native_sid() -> str:
        raise OSError("synthetic native SID failure")

    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        assert command == ["whoami", "/user", "/fo", "csv", "/nh"]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                b'"'
                + "中文用户".encode("cp936")
                + b'","S-1-5-21-1234-5678-9012-1001"\r\n'
            ),
            stderr=b"",
        )

    monkeypatch.setattr(upgrade_migration, "_native_windows_user_sid", fail_native_sid)
    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    sid = upgrade_migration._current_windows_user_sid(
        deadline=upgrade_migration.time.monotonic() + 30
    )

    assert sid == "S-1-5-21-1234-5678-9012-1001"
    timeout = observed["timeout"]
    assert isinstance(timeout, (int, float))
    assert 0 < timeout <= upgrade_migration._WINDOWS_IDENTITY_FALLBACK_TIMEOUT_SECONDS
    assert observed["stdin"] == subprocess.DEVNULL
    assert "text" not in observed


@pytest.mark.parametrize(
    "stdout",
    [
        b'"DESKTOP\\owner","missing"\r\n',
        (
            b'"DESKTOP\\owner","S-1-5-21-1234-5678-9012-1001" '
            b'"S-1-5-21-1234-5678-9012-1002"\r\n'
        ),
    ],
)
def test_whoami_fallback_rejects_missing_or_multiple_sids(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
) -> None:
    def fail_native_sid() -> str:
        raise OSError("synthetic native SID failure")

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(upgrade_migration, "_native_windows_user_sid", fail_native_sid)
    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    with pytest.raises(OSError, match="cannot resolve"):
        upgrade_migration._current_windows_user_sid()


def test_windows_acl_batch_rejects_incomplete_helper_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        upgrade_migration,
        "_current_windows_user_sid",
        lambda **_kwargs: "S-1-5-21-1234",
    )
    target = tmp_path / "snapshot"
    target.mkdir()

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "count": 1,
                    "ids": ["0"],
                    "pathUtf8Base64": [],
                    "pathHashes": [],
                }
            ).encode("ascii"),
            stderr=b"",
        )

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    with pytest.raises(OSError, match="requested paths exactly"):
        upgrade_migration._protect_windows_acl_batch(
            ((target, True),),
            windows_user_sid="S-1-5-21-1234",
        )


def test_windows_acl_batch_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    target = tmp_path / "snapshot"
    target.mkdir()

    def run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired("powershell", 0.01)

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    with pytest.raises(TimeoutError, match="ACL migration stage timed out"):
        upgrade_migration._protect_windows_acl_batch(
            ((target, True),),
            windows_user_sid="S-1-5-21-1234",
            deadline=upgrade_migration.time.monotonic() + 1,
        )


def test_private_tree_rejects_reparse_points_without_following_them(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = root / "redirect"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(OSError, match="unsafe type"):
        upgrade_migration._private_tree_entries(root)


def test_private_tree_rejects_file_root(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(OSError, match="snapshot root is not a directory"):
        upgrade_migration._private_tree_entries(root)


def test_windows_acl_process_timeout_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(
            command,
            upgrade_migration.WINDOWS_ACL_STAGE_TIMEOUT_SECONDS,
        )

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    with pytest.raises(OSError, match="Windows ACL hardening timed out"):
        upgrade_migration._protect_private_path(
            snapshot,
            directory=True,
            windows_user_sid="S-1-5-21-1234",
        )


def test_frozen_windows_acl_process_restores_packaged_dll_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    events: list[tuple[str, str | None]] = []
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    monkeypatch.setattr(upgrade_migration.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        upgrade_migration.sys,
        "_MEIPASS",
        "C:/synthetic/bundle/_internal",
        raising=False,
    )
    monkeypatch.setattr(
        upgrade_migration,
        "_set_windows_dll_directory",
        lambda path: events.append(("dll", path)),
    )

    def run(_command: list[str], **kwargs: object) -> SimpleNamespace:
        events.append(("run", None))
        payload = kwargs.get("input")
        if isinstance(payload, bytes):
            items = json.loads(payload.decode("ascii"))
            stdout = json.dumps(
                {
                    "count": len(items),
                    "ids": [str(index) for index in range(len(items))],
                    "pathUtf8Base64": [
                        base64.b64encode(item["path"].encode("utf-8")).decode("ascii")
                        for item in items
                    ],
                    "pathHashes": [
                        upgrade_migration._acl_path_hash(Path(item["path"]))
                        for item in items
                    ],
                }
            ).encode("ascii")
        else:
            stdout = b'"DESKTOP\\owner","S-1-5-21-1234"\n'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(upgrade_migration.subprocess, "run", run)

    upgrade_migration._protect_private_path(
        snapshot,
        directory=True,
        windows_user_sid="S-1-5-21-1234",
    )

    assert events == [
        ("dll", None),
        ("run", None),
        ("dll", "C:/synthetic/bundle/_internal"),
    ]


def test_frozen_windows_acl_process_restores_dll_directory_after_launch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, str | None]] = []
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    monkeypatch.setattr(upgrade_migration.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        upgrade_migration.sys,
        "_MEIPASS",
        "C:/synthetic/bundle/_internal",
        raising=False,
    )
    monkeypatch.setattr(
        upgrade_migration,
        "_set_windows_dll_directory",
        lambda path: events.append(("dll", path)),
    )

    with pytest.raises(OSError, match="synthetic launch failure"):
        with upgrade_migration._system_windows_process_context():
            events.append(("run", None))
            raise OSError("synthetic launch failure")

    assert events == [
        ("dll", None),
        ("run", None),
        ("dll", "C:/synthetic/bundle/_internal"),
    ]


def test_windows_snapshot_acl_is_applied_in_copy_and_promotion_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "trusted"\n', encoding="utf-8")
    database = tmp_path / "state" / "sessions.db"
    database.parent.mkdir()
    database.write_bytes(b"legacy-database")
    events: list[tuple[str, Path, tuple[str, ...]]] = []
    _mock_windows_acl(monkeypatch, events)

    real_copyfileobj = upgrade_migration.shutil.copyfileobj

    def copyfileobj(source: Any, destination: Any, length: int = 0) -> None:
        events.append(("copy", config, ()))
        real_copyfileobj(source, destination, length)

    monkeypatch.setattr(upgrade_migration.shutil, "copyfileobj", copyfileobj)
    real_replace = upgrade_migration.os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        target = Path(destination)
        if target.name == upgrade_migration.SNAPSHOT_NAME:
            events.append(("promote", target, ()))
        real_replace(source, destination)

    monkeypatch.setattr(upgrade_migration.os, "replace", replace)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    staging_acl = next(
        index
        for index, event in enumerate(events)
        if event[0] == "acl" and event[1].name.endswith(".tmp")
    )
    copy = next(index for index, event in enumerate(events) if event[0] == "copy")
    copied_file_acl = next(
        index
        for index, event in enumerate(events)
        if event[0] == "acl"
        and event[1].name == "config.toml"
        and upgrade_migration.SNAPSHOT_NAME not in event[1].parts
    )
    promote = next(index for index, event in enumerate(events) if event[0] == "promote")
    final_revalidation = next(
        index
        for index, event in enumerate(events)
        if index > promote
        and event[0] == "acl"
        and event[1] == tmp_path / upgrade_migration.SNAPSHOT_NAME
    )
    assert staging_acl < copy < copied_file_acl < promote < final_revalidation

    protected_paths = {event[1] for event in events if event[0] == "acl"}
    assert tmp_path / upgrade_migration.SNAPSHOT_NAME / "config.toml" in protected_paths
    assert (
        tmp_path / upgrade_migration.SNAPSHOT_NAME / "state" / "sessions.db"
        in protected_paths
    )
    assert tmp_path / upgrade_migration.SNAPSHOT_NAME / "manifest.json" in protected_paths
    for _kind, _path, command in (event for event in events if event[0] == "acl"):
        assert Path(command[0]).stem.casefold() in {"powershell", "pwsh"}
        assert tuple(command[1:5]) == (
            "-NoProfile",
            "-NonInteractive",
            "-NoLogo",
            "-EncodedCommand",
        )


@pytest.mark.parametrize("slow_phase", ["copy", "digest"])
def test_windows_snapshot_materialization_does_not_consume_acl_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    slow_phase: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nrun_mode = "trusted"\n', encoding="utf-8")
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        upgrade_migration,
        "_current_windows_user_sid",
        lambda **_kwargs: "S-1-5-21-1234",
    )
    clock = [100.0]
    monkeypatch.setattr(upgrade_migration.time, "monotonic", lambda: clock[0])
    remaining_before_acl: list[float] = []

    def protect_acl(
        _entries: tuple[tuple[Path, bool], ...],
        *,
        windows_user_sid: str,
        deadline: float | None = None,
    ) -> None:
        assert windows_user_sid == "S-1-5-21-1234"
        remaining_before_acl.append(
            upgrade_migration._remaining_windows_acl_time(deadline)
        )
        if len(remaining_before_acl) == 1:
            clock[0] += 5.0

    monkeypatch.setattr(
        upgrade_migration,
        "_protect_windows_acl_batch",
        protect_acl,
    )

    real_copyfileobj = upgrade_migration.shutil.copyfileobj
    real_digest = upgrade_migration._digest

    def slow_copyfileobj(source: Any, destination: Any, length: int = 0) -> None:
        clock[0] += upgrade_migration.WINDOWS_ACL_STAGE_TIMEOUT_SECONDS + 1
        real_copyfileobj(source, destination, length)

    def slow_digest(path: Path) -> str:
        clock[0] += upgrade_migration.WINDOWS_ACL_STAGE_TIMEOUT_SECONDS + 1
        return real_digest(path)

    if slow_phase == "copy":
        monkeypatch.setattr(
            upgrade_migration.shutil,
            "copyfileobj",
            slow_copyfileobj,
        )
    else:
        monkeypatch.setattr(upgrade_migration, "_digest", slow_digest)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    assert report.status == "committed"
    assert clock[0] == 136.0
    assert remaining_before_acl == pytest.approx([30.0, 25.0, 25.0])


def test_windows_acl_budget_exhaustion_requires_manual_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "trusted"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(upgrade_migration, "_running_on_windows", lambda: True)
    monkeypatch.setattr(
        upgrade_migration,
        "_current_windows_user_sid",
        lambda **_kwargs: "S-1-5-21-1234",
    )
    clock = [100.0]
    monkeypatch.setattr(upgrade_migration.time, "monotonic", lambda: clock[0])
    copied = False

    def exhaust_acl_budget(
        _entries: tuple[tuple[Path, bool], ...],
        *,
        windows_user_sid: str,
        deadline: float | None = None,
    ) -> None:
        assert windows_user_sid == "S-1-5-21-1234"
        clock[0] += upgrade_migration.WINDOWS_ACL_STAGE_TIMEOUT_SECONDS + 1
        upgrade_migration._remaining_windows_acl_time(deadline)

    def copyfileobj(_source: Any, _destination: Any, _length: int = 0) -> None:
        nonlocal copied
        copied = True

    monkeypatch.setattr(
        upgrade_migration,
        "_protect_windows_acl_batch",
        exhaust_acl_budget,
    )
    monkeypatch.setattr(upgrade_migration.shutil, "copyfileobj", copyfileobj)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert report.status == "manual_recovery_required"
    assert report.snapshot_path is None
    assert copied is False
    assert "Windows ACL hardening timed out" in str(report.error)
    journal = json.loads(
        (tmp_path / upgrade_migration.JOURNAL_NAME).read_text(encoding="utf-8")
    )
    assert journal["status"] == "prepared"


def test_committed_legacy_snapshot_is_hardened_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / upgrade_migration.SNAPSHOT_NAME
    snapshot.mkdir()
    copied_config = snapshot / "config.toml"
    copied_config.write_text("legacy snapshot", encoding="utf-8")
    (tmp_path / upgrade_migration.JOURNAL_NAME).write_text(
        json.dumps(
            {
                "migrationVersion": upgrade_migration.MIGRATION_VERSION,
                "status": "committed",
                "stores": ["config.toml"],
            }
        ),
        encoding="utf-8",
    )
    events: list[tuple[str, Path, tuple[str, ...]]] = []
    _mock_windows_acl(monkeypatch, events)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is True
    protected_paths = {event[1] for event in events if event[0] == "acl"}
    assert snapshot in protected_paths
    assert copied_config in protected_paths


def test_committed_legacy_snapshot_acl_failure_requires_manual_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / upgrade_migration.SNAPSHOT_NAME
    snapshot.mkdir()
    (snapshot / "config.toml").write_text("legacy snapshot", encoding="utf-8")
    (tmp_path / upgrade_migration.JOURNAL_NAME).write_text(
        json.dumps(
            {
                "migrationVersion": upgrade_migration.MIGRATION_VERSION,
                "status": "committed",
                "stores": ["config.toml"],
            }
        ),
        encoding="utf-8",
    )
    events: list[tuple[str, Path, tuple[str, ...]]] = []
    _mock_windows_acl(
        monkeypatch,
        events,
        fail_when=lambda path, _events: path == snapshot,
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert report.status == "manual_recovery_required"
    assert report.snapshot_path == snapshot
    journal = json.loads((tmp_path / upgrade_migration.JOURNAL_NAME).read_text(encoding="utf-8"))
    assert journal["status"] == "prepared"
    assert "cannot protect upgrade snapshot path" in journal["error"]


def test_committed_journal_without_snapshot_fails_closed(tmp_path: Path) -> None:
    (tmp_path / upgrade_migration.JOURNAL_NAME).write_text(
        json.dumps(
            {
                "migrationVersion": upgrade_migration.MIGRATION_VERSION,
                "status": "committed",
                "stores": ["config.toml"],
                "snapshot": str(tmp_path / upgrade_migration.SNAPSHOT_NAME),
            }
        ),
        encoding="utf-8",
    )

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert report.status == "manual_recovery_required"
    assert "committed sandbox upgrade snapshot is missing" in str(report.error)
    journal = json.loads(
        (tmp_path / upgrade_migration.JOURNAL_NAME).read_text(encoding="utf-8")
    )
    assert journal["status"] == "prepared"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL smoke")
def test_windows_private_acl_replaces_explicit_everyone_grants(tmp_path: Path) -> None:
    directory = tmp_path / "\u4e2d\u6587\u5feb\u7167"
    directory.mkdir()
    file_path = directory / "config.toml"
    file_path.write_text("sensitive", encoding="utf-8")
    subprocess.run(
        ["icacls", str(directory), "/grant:r", "*S-1-1-0:(OI)(CI)F", "/L"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["icacls", str(file_path), "/grant:r", "*S-1-1-0:F", "/L"],
        check=True,
        capture_output=True,
    )
    user_sid = upgrade_migration._current_windows_user_sid()

    upgrade_migration._protect_private_path(
        directory,
        directory=True,
        windows_user_sid=user_sid,
    )
    upgrade_migration._protect_private_path(
        file_path,
        directory=False,
        windows_user_sid=user_sid,
    )

    verifier = r"""
$ErrorActionPreference = "Stop"
$target = $env:OPENSQUILLA_TEST_ACL_TARGET
$attributes = [System.IO.File]::GetAttributes($target)
$isDirectory = ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0
$acl = if ($isDirectory) {
    [System.IO.Directory]::GetAccessControl($target)
} else {
    [System.IO.File]::GetAccessControl($target)
}
$sids = @($acl.Access | ForEach-Object {
    $_.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
} | Sort-Object -Unique)
[ordered]@{
    protected = $acl.AreAccessRulesProtected
    count = @($acl.Access).Count
    inherited = @($acl.Access | Where-Object { $_.IsInherited }).Count
    sids = $sids
} | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(verifier.encode("utf-16-le")).decode("ascii")
    expected_sids = sorted({user_sid, "S-1-5-18", "S-1-5-32-544"})
    for path in (directory, file_path):
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            env={**os.environ, "OPENSQUILLA_TEST_ACL_TARGET": str(path)},
            check=True,
            capture_output=True,
            text=True,
        )
        verified = json.loads(completed.stdout)
        assert verified == {
            "protected": True,
            "count": len(expected_sids),
            "inherited": 0,
            "sids": expected_sids,
        }


@pytest.mark.parametrize("failure_phase", ["staging", "copied_file", "final"])
def test_windows_snapshot_acl_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_phase: str,
) -> None:
    (tmp_path / "config.toml").write_text(
        '[sandbox]\nrun_mode = "trusted"\n',
        encoding="utf-8",
    )
    events: list[tuple[str, Path, tuple[str, ...]]] = []

    def fail_when(path: Path, current: list[tuple[str, Path, tuple[str, ...]]]) -> bool:
        promoted = any(event[0] == "promote" for event in current)
        if failure_phase == "staging":
            return path.name.endswith(".tmp") and len(current) == 1
        if failure_phase == "copied_file":
            return path.name == "config.toml" and not promoted
        return failure_phase == "final" and path == tmp_path / upgrade_migration.SNAPSHOT_NAME

    _mock_windows_acl(monkeypatch, events, fail_when=fail_when)
    real_replace = upgrade_migration.os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        target = Path(destination)
        if target.name == upgrade_migration.SNAPSHOT_NAME:
            events.append(("promote", target, ()))
        real_replace(source, destination)

    monkeypatch.setattr(upgrade_migration.os, "replace", replace)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert report.status == "manual_recovery_required"
    assert report.snapshot_path is None
    assert not (tmp_path / upgrade_migration.SNAPSHOT_NAME).exists()
    assert not list(tmp_path.glob(f".{upgrade_migration.SNAPSHOT_NAME}.*.tmp"))
    journal = json.loads((tmp_path / upgrade_migration.JOURNAL_NAME).read_text(encoding="utf-8"))
    assert journal["status"] == "prepared"
    assert "cannot protect upgrade snapshot path" in journal["error"]


def test_windows_staging_is_rechecked_empty_before_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text("sensitive", encoding="utf-8")
    events: list[tuple[str, Path, tuple[str, ...]]] = []
    _mock_windows_acl(monkeypatch, events)
    real_protect = upgrade_migration._protect_private_path
    injected = False

    def protect(
        path: Path,
        *,
        directory: bool,
        windows_user_sid: str | None,
        deadline: float | None = None,
    ) -> None:
        nonlocal injected
        real_protect(
            path,
            directory=directory,
            windows_user_sid=windows_user_sid,
            deadline=deadline,
        )
        if directory and path.name.endswith(".tmp") and not injected:
            (path / "unexpected").write_text("injected", encoding="utf-8")
            injected = True

    monkeypatch.setattr(upgrade_migration, "_protect_private_path", protect)
    copied = False

    def copyfileobj(_source: Any, _destination: Any, _length: int = 0) -> None:
        nonlocal copied
        copied = True

    monkeypatch.setattr(upgrade_migration.shutil, "copyfileobj", copyfileobj)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert copied is False
    assert "staging is not empty after hardening" in str(report.error)
    assert not list(tmp_path.glob(f".{upgrade_migration.SNAPSHOT_NAME}.*.tmp"))


def test_windows_snapshot_cleanup_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "config.toml").write_text("sensitive", encoding="utf-8")
    events: list[tuple[str, Path, tuple[str, ...]]] = []

    def fail_final(path: Path, current: list[tuple[str, Path, tuple[str, ...]]]) -> bool:
        return (
            any(event[0] == "promote" for event in current)
            and path == tmp_path / upgrade_migration.SNAPSHOT_NAME
        )

    _mock_windows_acl(monkeypatch, events, fail_when=fail_final)
    real_replace = upgrade_migration.os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        target = Path(destination)
        if target.name == upgrade_migration.SNAPSHOT_NAME:
            events.append(("promote", target, ()))
        real_replace(source, destination)

    monkeypatch.setattr(upgrade_migration.os, "replace", replace)

    def refuse_cleanup(_path: Path) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(upgrade_migration.shutil, "rmtree", refuse_cleanup)

    report = SandboxUpgradeCoordinator(tmp_path).run()

    assert report.ok is False
    assert report.status == "manual_recovery_required"
    assert "upgrade snapshot failed and cleanup failed" in str(report.error)
