from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from opensquilla.telemetry import device_identity
from opensquilla.telemetry.identity import (
    IDENTITY_SCHEMA_VERSION,
    IdentityStateError,
    TelemetryIdentityKind,
    delete_identity,
    generate_random_identity,
    identity_state_path,
    load_or_create_identity,
    read_identity,
)


@pytest.mark.parametrize("platform", ["macos", "windows", "linux"])
def test_device_identity_normalizes_machine_uuid_and_separates_platforms(platform) -> None:
    first = device_identity.derive_device_id(platform, " 00112233-4455-6677-8899-AABBCCDDEEFF\n")
    second = device_identity.derive_device_id(platform, "00112233445566778899aabbccddeeff")
    assert first == second
    assert first is not None and len(first) == 64
    assert first != device_identity.derive_device_id(platform, "112233445566778899aabbccddeeff00")
    assert len({
        device_identity.derive_device_id(os_name, "00112233445566778899aabbccddeeff")
        for os_name in ("macos", "windows", "linux")
    }) == 3


@pytest.mark.parametrize("machine_id", ["", "0" * 32, "f" * 32, "host-name", "00:11:22:33:44:55"])
def test_device_identity_rejects_missing_or_untrustworthy_values(machine_id) -> None:
    assert device_identity.derive_device_id("macos", machine_id) is None
    assert device_identity.derive_device_id("unknown", "00112233445566778899aabbccddeeff") is None


def test_macos_identity_uses_bounded_fixed_command(monkeypatch) -> None:
    def run(command, **kwargs):
        assert command == ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]
        assert kwargs["timeout"] == 1.0
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs.get("shell", False) is False
        return SimpleNamespace(stdout='"IOPlatformUUID" = "00112233-4455-6677-8899-AABBCCDDEEFF"')

    monkeypatch.setattr(device_identity.subprocess, "run", run)
    assert device_identity._macos_machine_id() == "00112233-4455-6677-8899-AABBCCDDEEFF"


def test_windows_identity_reads_machine_registry_in_64_bit_view(monkeypatch) -> None:
    from contextlib import nullcontext

    monkeypatch.setattr(device_identity.sys, "platform", "win32")
    fake_key = object()
    def open_key(root, name, reserved, access):
        assert root == "machine"
        assert name == r"SOFTWARE\Microsoft\Cryptography"
        assert reserved == 0 and access == 3
        return nullcontext(fake_key)

    def query_value(key, name):
        assert key is fake_key and name == "MachineGuid"
        return "00112233-4455-6677-8899-aabbccddeeff", 1

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        OpenKey=open_key, QueryValueEx=query_value, HKEY_LOCAL_MACHINE="machine",
        KEY_READ=1, KEY_WOW64_64KEY=2, REG_SZ=1,
    ))
    assert device_identity._windows_machine_id() == "00112233-4455-6677-8899-aabbccddeeff"


def test_device_identity_linux_fallback_and_process_cache(monkeypatch) -> None:
    paths = []
    def read(path):
        paths.append(path.as_posix())
        return None if path.as_posix() == "/etc/machine-id" else "00112233445566778899aabbccddeeff"

    monkeypatch.setattr(device_identity.sys, "platform", "linux")
    monkeypatch.setattr(device_identity, "_read_machine_id_file", read)
    device_identity.get_device_id.cache_clear()
    try:
        first = device_identity.get_device_id()
        assert first == device_identity.derive_device_id(
            "linux", "00112233445566778899aabbccddeeff"
        )
        assert device_identity.get_device_id() == first
        assert paths == ["/etc/machine-id", "/var/lib/dbus/machine-id"]
    finally:
        device_identity.get_device_id.cache_clear()


def test_machine_id_file_read_is_bounded(tmp_path) -> None:
    path = tmp_path / "machine-id"
    path.write_text("00112233445566778899aabbccddeeff\n")
    assert device_identity._read_machine_id_file(path) == "00112233445566778899aabbccddeeff\n"
    path.write_text("a" * 129)
    assert device_identity._read_machine_id_file(path) is None
    path.write_bytes(b"\xff")
    assert device_identity._read_machine_id_file(path) is None


def test_device_identity_lookup_failure_never_uses_network_or_random_fallback(monkeypatch) -> None:
    def unavailable():
        raise subprocess.TimeoutExpired("ioreg", 1)

    def forbidden(*args, **kwargs):
        pytest.fail("device identity must not use network or random identity")

    monkeypatch.setattr(device_identity.sys, "platform", "darwin")
    monkeypatch.setattr(device_identity, "_macos_machine_id", unavailable)
    monkeypatch.setattr(uuid, "uuid4", forbidden)
    monkeypatch.setattr(uuid, "getnode", forbidden)
    monkeypatch.setattr(socket, "gethostname", forbidden)
    device_identity.get_device_id.cache_clear()
    try:
        assert device_identity.get_device_id() is None
    finally:
        device_identity.get_device_id.cache_clear()


def test_runtime_device_identity_is_gated_by_upload_policy(tmp_path, monkeypatch) -> None:
    from opensquilla.telemetry.consent import TelemetryScope
    from opensquilla.telemetry.runtime import ScopedTelemetryRuntime

    calls = []
    monkeypatch.setattr(
        "opensquilla.telemetry.runtime.get_device_id", lambda: calls.append(1) or "a" * 64
    )
    config = SimpleNamespace(state_dir=str(tmp_path), privacy=SimpleNamespace(
        disable_network_observability=True,
    ))
    runtime = ScopedTelemetryRuntime(config=config, env={})
    assert runtime.device_id_for(TelemetryScope.RELIABILITY) is None
    assert runtime.device_id_for(TelemetryScope.GROWTH) is None
    assert calls == []
    config.privacy.disable_network_observability = False
    assert runtime.device_id_for(TelemetryScope.RELIABILITY) == "a" * 64
    assert calls == [1]
    ci_runtime = ScopedTelemetryRuntime(config=config, env={"CI": "1"})
    assert ci_runtime.device_id_for(TelemetryScope.RELIABILITY) is None
    assert calls == [1]

UUID_ONE = uuid.UUID("123e4567-e89b-42d3-a456-426614174000")
UUID_TWO = uuid.UUID("123e4567-e89b-42d3-b456-426614174000")


def test_generate_identity_is_canonical_uuid4_and_normalizes_time_to_utc() -> None:
    identity = generate_random_identity(
        TelemetryIdentityKind.ANALYTICS_USER,
        now=datetime(2026, 9, 1, 16, 0, tzinfo=timezone(timedelta(hours=8))),
        uuid_factory=lambda: UUID_ONE,
    )

    assert identity.value == str(UUID_ONE)
    assert uuid.UUID(identity.value).version == 4
    assert identity.created_at_utc == "2026-09-01T08:00:00Z"
    assert identity.schema_version == IDENTITY_SCHEMA_VERSION


def test_identity_generation_rejects_non_random_uuid_versions() -> None:
    with pytest.raises(ValueError, match="UUIDv4"):
        generate_random_identity(
            TelemetryIdentityKind.ANALYTICS_USER,
            uuid_factory=lambda: uuid.uuid1(),
        )


def test_identity_generation_never_reads_hardware_or_network_identity(monkeypatch) -> None:
    def _forbidden_source(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("hardware or network identity source was consulted")

    monkeypatch.setattr(uuid, "getnode", _forbidden_source)
    monkeypatch.setattr(socket, "gethostname", _forbidden_source)
    monkeypatch.setattr(socket, "gethostbyname", _forbidden_source)
    monkeypatch.setattr(uuid, "uuid4", lambda: UUID_ONE)

    identity = generate_random_identity(TelemetryIdentityKind.ANALYTICS_USER)

    assert identity.value == str(UUID_ONE)


def test_load_or_create_is_stable(tmp_path) -> None:
    path = tmp_path / "telemetry" / "growth_identity.json"
    first = load_or_create_identity(
        path,
        TelemetryIdentityKind.ANALYTICS_USER,
        now=datetime(2026, 9, 1, tzinfo=UTC),
        uuid_factory=lambda: UUID_ONE,
    )
    second = load_or_create_identity(
        path,
        TelemetryIdentityKind.ANALYTICS_USER,
        uuid_factory=lambda: UUID_TWO,
    )

    assert second == first


def test_concurrent_creation_returns_one_persisted_identity(tmp_path) -> None:
    path = tmp_path / "growth_identity.json"

    def _create(candidate: uuid.UUID):
        return load_or_create_identity(
            path,
            TelemetryIdentityKind.ANALYTICS_USER,
            uuid_factory=lambda: candidate,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(_create, (UUID_ONE, UUID_TWO)))

    assert identities[0] == identities[1]
    assert read_identity(path) == identities[0]


def test_corrupt_identity_fails_closed_instead_of_rotating(tmp_path) -> None:
    path = tmp_path / "growth_identity.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": IDENTITY_SCHEMA_VERSION,
                "kind": "analytics_user_id",
                "value": "not-a-uuid",
                "created_at_utc": "2026-09-01T08:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(IdentityStateError, match="UUIDv4"):
        load_or_create_identity(
            path,
            TelemetryIdentityKind.ANALYTICS_USER,
            uuid_factory=lambda: UUID_ONE,
        )
    assert "not-a-uuid" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {**payload, "unknown": True},
        lambda payload: {key: value for key, value in payload.items() if key != "kind"},
        lambda payload: {**payload, "schema_version": 999},
        lambda payload: {**payload, "created_at_utc": "2026-09-01T08:00:00+08:00"},
    ],
)
def test_identity_reader_rejects_noncanonical_state(tmp_path, mutation) -> None:
    path = tmp_path / "identity.json"
    payload = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "kind": "analytics_user_id",
        "value": str(UUID_ONE),
        "created_at_utc": "2026-09-01T08:00:00Z",
    }
    path.write_text(json.dumps(mutation(payload)), encoding="utf-8")

    with pytest.raises(IdentityStateError):
        read_identity(path)


def test_growth_identity_path_and_delete_are_narrow(tmp_path) -> None:
    config = SimpleNamespace(state_dir=str(tmp_path))
    growth_path = identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config)
    growth = load_or_create_identity(
        growth_path,
        TelemetryIdentityKind.ANALYTICS_USER,
        uuid_factory=lambda: UUID_TWO,
    )

    assert growth_path == tmp_path / "telemetry" / "growth_identity.json"
    assert read_identity(growth_path) == growth
    assert delete_identity(growth_path) is True
    assert delete_identity(growth_path) is False


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits are not authoritative on Windows",
)
def test_identity_file_is_private_on_posix(tmp_path) -> None:
    path = tmp_path / "identity.json"
    load_or_create_identity(
        path,
        TelemetryIdentityKind.ANALYTICS_USER,
        uuid_factory=lambda: UUID_ONE,
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
