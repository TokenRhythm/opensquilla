from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
import typer
from typer.testing import CliRunner

from opensquilla.cli import skills_cmd
from opensquilla.cli.main import app
from opensquilla.skills.hub.management import InstallResult
from opensquilla.skills.hub.transaction import journal_path_for_state
from opensquilla.skills.types import SkillLayer, SkillSpec


class _GatewayClient:
    connect_error: ClassVar[BaseException | None] = None
    call_payload: ClassVar[dict[str, Any] | None] = None
    called_method: ClassVar[str] = ""
    called_params: ClassVar[dict[str, Any]] = {}
    token: ClassVar[str | None] = None
    closed: ClassVar[bool] = False

    async def connect(self, _url: str, *, token: str | None = None) -> None:
        type(self).token = token
        if self.connect_error is not None:
            raise self.connect_error

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).called_method = method
        type(self).called_params = dict(params)
        if self.call_payload is not None:
            return self.call_payload
        return {"success": True, "method": method, "params": params}

    async def close(self) -> None:
        type(self).closed = True


@pytest.fixture(autouse=True)
def _gateway_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _GatewayClient.connect_error = None
    _GatewayClient.call_payload = None
    _GatewayClient.called_method = ""
    _GatewayClient.called_params = {}
    _GatewayClient.token = None
    _GatewayClient.closed = False
    monkeypatch.setattr("opensquilla.cli.gateway_client.GatewayClient", _GatewayClient)
    monkeypatch.setattr(skills_cmd, "default_gateway_token", lambda: "operator-token")


@pytest.mark.asyncio
async def test_skill_mutation_uses_gateway_token() -> None:
    payload = await skills_cmd._try_gateway_skill_mutation(
        "skills.install",
        {"identifier": "demo"},
        json_output=True,
    )

    assert _GatewayClient.token == "operator-token"
    assert payload == {
        "success": True,
        "method": "skills.install",
        "params": {"identifier": "demo"},
    }
    assert _GatewayClient.closed is True


def test_skills_list_prefers_live_gateway_and_preserves_json_list_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _GatewayClient.call_payload = {
        "skills": [
            {
                "name": "live-demo",
                "layer": "managed",
                "eligible": True,
                "description": "Published by the live catalog",
                "always": False,
                "triggers": [],
                "user_invocable": True,
                "disable_model_invocation": True,
                "file_path": "/synthetic/live-demo/SKILL.md",
                "homepage": "https://example.invalid/live-demo",
                "provenance": {
                    "origin": "community",
                    "license": "MIT",
                    "upstream_url": "https://example.invalid/upstream",
                    "maintained_by": "Publisher",
                },
            }
        ]
    }

    def _offline_scan_must_not_run() -> tuple[Any, Any]:
        raise AssertionError("live skills.list must not fall back to an offline scan")

    monkeypatch.setattr(skills_cmd, "_build_offline_skill_loader", _offline_scan_must_not_run)

    result = CliRunner().invoke(app, ["skills", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["name"] == "live-demo"
    assert payload[0]["active"] is True
    assert payload[0]["available"] is True
    assert payload[0]["catalogState"] == "live"
    assert payload[0]["userInvocable"] is True
    assert payload[0]["disableModelInvocation"] is True
    assert payload[0]["baseDir"] == "/synthetic/live-demo"
    assert payload[0]["provenance"]["upstreamUrl"] == "https://example.invalid/upstream"
    assert _GatewayClient.called_method == "skills.list"
    assert _GatewayClient.called_params == {}


@pytest.mark.parametrize(
    ("file_path", "expected_base_dir"),
    [
        ("/synthetic/live-demo/SKILL.md", "/synthetic/live-demo"),
        (r"C:\synthetic\live-demo\SKILL.md", r"C:\synthetic\live-demo"),
    ],
)
def test_gateway_skill_rows_preserve_wire_path_separator_style(
    file_path: str,
    expected_base_dir: str,
) -> None:
    rows = skills_cmd._gateway_skill_rows(
        {
            "skills": [
                {
                    "name": "live-demo",
                    "eligible": True,
                    "file_path": file_path,
                }
            ]
        }
    )

    assert rows[0]["filePath"] == file_path
    assert rows[0]["baseDir"] == expected_base_dir
    assert rows[0]["path"] == expected_base_dir


def test_offline_skills_list_marks_rows_validated_for_next_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _GatewayClient.connect_error = ConnectionError("connection refused")
    profile_home = tmp_path / "profile"
    state_root = profile_home / "state"
    managed = profile_home / "skills"
    skill_dir = managed / "offline-demo"
    skill = SkillSpec(
        name="offline-demo",
        description="Only observed on disk",
        layer=SkillLayer.MANAGED,
        always=False,
        triggers=[],
        content="Offline instructions.",
        path=skill_dir,
        file_path=str(skill_dir / "SKILL.md"),
        base_dir=str(skill_dir),
    )
    config = SimpleNamespace(
        state_dir=str(state_root),
        skills=SimpleNamespace(disabled=[], coding_mode=False),
    )
    loader = SimpleNamespace(
        managed_dir=managed,
        get_user_invocable=lambda: [skill],
    )
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(profile_home))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-root"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setattr(
        skills_cmd,
        "_build_offline_skill_loader",
        lambda: (config, loader),
    )

    result = CliRunner().invoke(app, ["skills", "list", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload[0]["eligible"] is True
    assert payload[0]["active"] is False
    assert payload[0]["available"] is False
    assert payload[0]["catalogState"] == "validated_offline"
    assert payload[0]["effectiveFrom"] == "next_start"


def test_offline_skills_list_fails_closed_during_writer_and_pending_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.profile_operation_lock import ProfileOperationLock

    _GatewayClient.connect_error = ConnectionError("connection refused")
    profile_home = tmp_path / "profile"
    state_root = profile_home / "state"
    managed = profile_home / "skills"
    config = SimpleNamespace(
        state_dir=str(state_root),
        skills=SimpleNamespace(disabled=[], coding_mode=False),
    )
    builds = 0
    scans = 0

    def _scan() -> list[Any]:
        nonlocal scans
        scans += 1
        raise AssertionError("an unsafe offline tree must not be scanned")

    loader = SimpleNamespace(managed_dir=managed, get_user_invocable=_scan)

    def _build_offline_loader() -> tuple[Any, Any]:
        nonlocal builds
        builds += 1
        return config, loader

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(profile_home))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-root"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setattr(
        skills_cmd,
        "_build_offline_skill_loader",
        _build_offline_loader,
    )

    journal = journal_path_for_state(managed, state_root)
    journal.parent.mkdir(parents=True)
    journal.write_text("{\"version\": 1, \"phase\": \"old_moved\"}\n", encoding="utf-8")

    locked = threading.Event()
    release = threading.Event()

    def _hold_writer() -> None:
        with ProfileOperationLock(profile_home):
            locked.set()
            assert release.wait(timeout=5)

    writer = threading.Thread(target=_hold_writer, daemon=True)
    writer.start()
    assert locked.wait(timeout=5)
    try:
        busy = CliRunner().invoke(app, ["skills", "list", "--json"])
    finally:
        release.set()
        writer.join(timeout=5)

    assert busy.exit_code == 1, busy.output
    busy_error = json.loads(busy.stderr)["error"]
    assert busy_error["code"] == "PROFILE_IN_USE"
    assert builds == 0
    assert scans == 0
    scans_after_busy = scans

    recovery = CliRunner().invoke(app, ["skills", "list", "--json"])

    assert recovery.exit_code == 1, recovery.output
    recovery_error = json.loads(recovery.stderr)["error"]
    assert recovery_error["code"] == "SKILL_RECOVERY_REQUIRED"
    assert recovery_error["details"]["journal"] == str(journal)
    assert journal.exists()
    assert builds == 1
    assert scans == scans_after_busy


def test_offline_skills_doctor_fails_closed_before_building_loader_during_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.profile_operation_lock import ProfileOperationLock

    _GatewayClient.connect_error = ConnectionError("connection refused")
    profile_home = tmp_path / "profile"
    builds = 0

    def _build_offline_loader() -> tuple[Any, Any]:
        nonlocal builds
        builds += 1
        raise AssertionError("Doctor must not build a loader while the profile is busy")

    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(profile_home))
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-root"))
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setattr(
        skills_cmd,
        "_build_offline_skill_loader",
        _build_offline_loader,
    )

    locked = threading.Event()
    release = threading.Event()

    def _hold_writer() -> None:
        with ProfileOperationLock(profile_home):
            locked.set()
            assert release.wait(timeout=5)

    writer = threading.Thread(target=_hold_writer, daemon=True)
    writer.start()
    assert locked.wait(timeout=5)
    try:
        result = CliRunner().invoke(app, ["skills", "doctor", "--json"])
    finally:
        release.set()
        writer.join(timeout=5)

    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "PROFILE_IN_USE"
    assert builds == 0


@pytest.mark.asyncio
async def test_skill_mutation_falls_back_only_when_gateway_is_unreachable() -> None:
    _GatewayClient.connect_error = SystemExit(
        "Cannot connect to OpenSquilla gateway at ws://localhost:18791/ws"
    )

    payload = await skills_cmd._try_gateway_skill_mutation(
        "skills.install",
        {"identifier": "demo"},
        json_output=True,
    )

    assert payload is None
    assert _GatewayClient.closed is True


@pytest.mark.asyncio
async def test_skill_mutation_does_not_write_offline_after_handshake_failure() -> None:
    _GatewayClient.connect_error = SystemExit("Handshake failed: unauthorized")

    with pytest.raises(typer.Exit) as exc_info:
        await skills_cmd._try_gateway_skill_mutation(
            "skills.install",
            {"identifier": "demo"},
            json_output=True,
        )

    assert exc_info.value.exit_code == 1
    assert _GatewayClient.closed is True


def test_offline_install_human_output_preserves_non_activation_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _OfflineService:
        async def install(self, *args: object, **kwargs: object) -> InstallResult:
            return InstallResult(
                success=True,
                name="demo",
                path="/synthetic/managed/demo",
                installed=True,
                instruction_usable=False,
                effective_from="next_start",
                message=(
                    "Validated and installed 'demo'; catalog activation and readiness "
                    "will be evaluated on next start"
                ),
            )

    class _NoopProfileLock:
        def __init__(self, _path: object) -> None:
            pass

        def __enter__(self) -> _NoopProfileLock:
            events.append("lock-entered")
            return self

        def __exit__(self, *args: object) -> None:
            return None

    async def _gateway_unreachable(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(skills_cmd, "_try_gateway_skill_mutation", _gateway_unreachable)
    def _build_service() -> tuple[_OfflineService, str]:
        events.append("service-built")
        return _OfflineService(), "/synthetic/profile"

    monkeypatch.setattr(skills_cmd, "_offline_management_service", _build_service)
    monkeypatch.setattr(
        "opensquilla.profile_operation_lock.ProfileOperationLock",
        _NoopProfileLock,
    )

    result = CliRunner().invoke(app, ["skills", "install", "demo"])

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert events[:2] == ["lock-entered", "service-built"]
    assert "catalog activation and readiness will be evaluated on next start" in output
    assert "available after the next start" not in output
