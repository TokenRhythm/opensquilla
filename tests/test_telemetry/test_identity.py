from __future__ import annotations

import json
import os
import socket
import stat
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

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
