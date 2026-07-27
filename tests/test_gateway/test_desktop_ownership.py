from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.desktop_ownership import (
    DESKTOP_GATEWAY_INSTANCE_NONCE_ENV,
    DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV,
    DESKTOP_GATEWAY_OWNERSHIP_FILENAME,
    DESKTOP_GATEWAY_OWNERSHIP_LOCK_FILENAME,
    DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
    DesktopGatewayOwnership,
    activate_desktop_gateway_ownership,
    canonical_identity_payload,
    canonical_shutdown_payload,
    release_active_desktop_gateway_ownership,
)
from opensquilla.recovery.locking import profile_lock_key

_NONCE = "n" * 43
_CHALLENGE = "c" * 43
_OWNER_PEER = ("127.0.0.1", 51000)
_REMOTE_PEER = ("203.0.113.7", 51000)


def _owner(tmp_path: Path, monkeypatch, *, port: int = 18791) -> DesktopGatewayOwnership:
    profile_home = tmp_path / "profile"
    ownership_dir = (
        tmp_path / "gateway-ownership" / profile_lock_key(profile_home)
    )
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE", _NONCE)
    monkeypatch.setenv(DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV, str(ownership_dir))
    owner = DesktopGatewayOwnership.from_environment(
        profile_home=profile_home,
        port=port,
    )
    assert owner is not None
    assert DESKTOP_GATEWAY_INSTANCE_NONCE_ENV not in os.environ
    assert DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV not in os.environ
    return owner


def test_desktop_ownership_is_disabled_without_explicit_desktop_nonce(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.delenv("OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE", raising=False)

    assert (
        DesktopGatewayOwnership.from_environment(
            profile_home=tmp_path / "profile",
            port=18791,
        )
        is None
    )


def test_desktop_ownership_record_is_profile_scoped_private_and_path_free(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path, monkeypatch)
    owner.acquire()
    record_path = owner.path

    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record == owner.record
        assert record["schema_version"] == 1
        assert record["protocol"] == DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL
        assert record["instance_nonce"] == _NONCE
        assert record["pid"] == os.getpid()
        assert record["port"] == 18791
        assert len(record["profile_fingerprint"]) == 64
        assert str(tmp_path) not in record_path.read_text(encoding="utf-8")
        if os.name != "nt":
            assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    finally:
        owner.release()

    assert not record_path.exists()
    assert (owner.state_dir / DESKTOP_GATEWAY_OWNERSHIP_LOCK_FILENAME).is_file()


def test_desktop_ownership_release_does_not_remove_a_successor_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path, monkeypatch)
    owner.acquire()
    record_path = owner.path
    successor = {**owner.record, "instance_nonce": "s" * 43}
    record_path.write_text(json.dumps(successor), encoding="utf-8")

    owner.release()

    assert json.loads(record_path.read_text(encoding="utf-8")) == successor


def test_desktop_ownership_release_serializes_compare_and_unlink_with_successor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from opensquilla.gateway import desktop_ownership

    owner = _owner(tmp_path, monkeypatch)
    owner.acquire()
    successor = DesktopGatewayOwnership(
        state_dir=owner.state_dir,
        profile_fingerprint=owner.profile_fingerprint,
        port=owner.port,
        instance_nonce="s" * 43,
    )
    original_read = desktop_ownership._read_record
    original_lock = desktop_ownership._ownership_record_lock
    successor_attempted = threading.Event()
    successor_thread: threading.Thread | None = None

    @contextmanager
    def observed_lock(state_dir: Path):
        if threading.current_thread() is successor_thread:
            successor_attempted.set()
        with original_lock(state_dir):
            yield

    def read_then_start_successor(path: Path):
        nonlocal successor_thread
        current = original_read(path)
        successor_thread = threading.Thread(target=successor.acquire)
        successor_thread.start()
        assert successor_attempted.wait(timeout=2)
        return current

    monkeypatch.setattr(desktop_ownership, "_ownership_record_lock", observed_lock)
    monkeypatch.setattr(desktop_ownership, "_read_record", read_then_start_successor)

    owner.release()
    assert successor_thread is not None
    successor_thread.join(timeout=2)
    assert not successor_thread.is_alive()
    assert json.loads(owner.path.read_text(encoding="utf-8")) == successor.record

    monkeypatch.setattr(desktop_ownership, "_read_record", original_read)
    successor.release()
    assert not owner.path.exists()
    assert (owner.state_dir / DESKTOP_GATEWAY_OWNERSHIP_LOCK_FILENAME).is_file()


def test_desktop_ownership_uses_control_dir_when_runtime_state_is_external(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from opensquilla.gateway.boot import _desktop_ownership_profile_home

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    external_state = tmp_path / "external-state"
    config = GatewayConfig(
        config_path=str(profile_home / "config.toml"),
        state_dir=str(external_state),
    )

    selected_home = _desktop_ownership_profile_home(config)
    assert selected_home == profile_home.resolve()
    ownership_dir = (
        tmp_path / "electron-user-data" / "gateway-ownership" / profile_lock_key(profile_home)
    )

    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE", _NONCE)
    monkeypatch.setenv(DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV, str(ownership_dir))
    owner = DesktopGatewayOwnership.from_environment(
        profile_home=selected_home,
        port=18791,
    )
    assert owner is not None
    owner.acquire()
    try:
        assert owner.path == ownership_dir / DESKTOP_GATEWAY_OWNERSHIP_FILENAME
        assert owner.record["profile_fingerprint"] == profile_lock_key(profile_home)
        assert not (external_state / DESKTOP_GATEWAY_OWNERSHIP_FILENAME).exists()
        assert not (profile_home / "state").exists()
    finally:
        owner.release()


def test_desktop_ownership_requires_absolute_profile_bound_control_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE", _NONCE)

    for invalid in (
        "",
        "relative/gateway-ownership",
        str(tmp_path / "gateway-ownership" / ("f" * 64)),
    ):
        if invalid:
            monkeypatch.setenv(DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV, invalid)
        else:
            monkeypatch.delenv(DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV, raising=False)
        with pytest.raises(ValueError):
            DesktopGatewayOwnership.from_environment(
                profile_home=profile_home,
                port=18791,
            )


def test_desktop_identity_proof_has_cross_language_golden_vector() -> None:
    public_record = {
        "schema_version": 1,
        "protocol": DESKTOP_GATEWAY_OWNERSHIP_PROTOCOL,
        "profile_fingerprint": "0123456789abcdef" * 4,
        "pid": 4242,
        "start_identity": "opaque-start-identity",
        "port": 18791,
        "version": "1.2.3",
    }
    challenge = "0123456789abcdef0123456789abcdef"
    nonce = "abcdefghijklmnopqrstuvwxyzABCDEFG"

    canonical = canonical_identity_payload(public_record, challenge)

    assert canonical.decode("ascii") == (
        '{"challenge":"0123456789abcdef0123456789abcdef","pid":4242,'
        '"port":18791,"profile_fingerprint":"0123456789abcdef0123456789abcdef'
        '0123456789abcdef0123456789abcdef","protocol":"opensquilla-desktop-gateway-'
        'ownership-v1","schema_version":1,"start_identity":"opaque-start-identity",'
        '"version":"1.2.3"}'
    )
    assert hmac.new(nonce.encode("ascii"), canonical, hashlib.sha256).hexdigest() == (
        "67f44cb9dd44df65360c36f5ab7090bcbd30a11c710b8131b960e3ed1f33e0cb"
    )

    shutdown_canonical = canonical_shutdown_payload(public_record, challenge)
    assert shutdown_canonical.decode("ascii") == (
        '{"action":"shutdown","challenge":"0123456789abcdef0123456789abcdef",'
        '"pid":4242,"port":18791,"profile_fingerprint":"0123456789abcdef0123456789'
        'abcdef0123456789abcdef0123456789abcdef","protocol":"opensquilla-desktop-'
        'gateway-ownership-v1","schema_version":1,"start_identity":"opaque-start-'
        'identity","version":"1.2.3"}'
    )
    assert hmac.new(
        nonce.encode("ascii"), shutdown_canonical, hashlib.sha256
    ).hexdigest() == (
        "68b2c749e4d727fbbc92cffa8b4e6bbe1e7c7c0ad4175a1671f903d0be2eb5d9"
    )


def test_desktop_identity_endpoint_returns_verified_path_free_proof(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path, monkeypatch)
    owner.acquire()
    config = GatewayConfig(
        host="127.0.0.1",
        auth={"mode": "token", "token": "unrelated-http-token"},
    )
    app = create_gateway_app(config)
    app.state.desktop_gateway_ownership = owner

    try:
        with TestClient(app, client=_OWNER_PEER) as client:
            response = client.post(
                "/api/desktop/identity",
                json={"challenge": _CHALLENGE},
            )

        assert response.status_code == 200, response.text
        payload = response.json()
        proof = payload.pop("proof")
        assert payload["challenge"] == _CHALLENGE
        assert "instance_nonce" not in payload
        assert str(tmp_path) not in response.text
        expected = hmac.new(
            _NONCE.encode("ascii"),
            canonical_identity_payload(owner.public_record, _CHALLENGE),
            hashlib.sha256,
        ).hexdigest()
        assert proof == expected
        assert len(proof) == 64
        assert proof == proof.lower()
        assert response.headers["cache-control"] == "no-store"
    finally:
        owner.release()


def test_desktop_identity_endpoint_is_unavailable_without_desktop_record() -> None:
    app = create_gateway_app(GatewayConfig())

    with TestClient(app, client=_OWNER_PEER) as client:
        response = client.post(
            "/api/desktop/identity",
            json={"challenge": _CHALLENGE},
        )

    assert response.status_code == 404


def test_desktop_identity_endpoint_rejects_remote_or_malformed_challenges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path, monkeypatch)
    owner.acquire()
    app = create_gateway_app(GatewayConfig(host="127.0.0.1"))
    app.state.desktop_gateway_ownership = owner

    try:
        with TestClient(app, client=_REMOTE_PEER) as client:
            remote = client.post(
                "/api/desktop/identity",
                json={"challenge": _CHALLENGE},
            )
        with TestClient(app, client=_OWNER_PEER) as client:
            malformed = client.post(
                "/api/desktop/identity",
                json={"challenge": "too-short"},
            )
            cross_origin = client.post(
                "/api/desktop/identity",
                json={"challenge": _CHALLENGE},
                headers={"Origin": "https://attacker.example"},
            )

        assert remote.status_code == 403
        assert malformed.status_code == 400
        assert cross_origin.status_code == 403
    finally:
        owner.release()


def test_desktop_shutdown_requires_nonce_proof_and_uses_distinct_hmac_domain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path, monkeypatch)
    owner.acquire()
    calls: list[str] = []
    config = GatewayConfig(
        host="127.0.0.1",
        auth={"mode": "token", "token": "unrelated-http-token"},
    )
    app = create_gateway_app(config)
    app.state.desktop_gateway_ownership = owner
    app.state.request_shutdown = calls.append
    valid_proof = hmac.new(
        _NONCE.encode("ascii"),
        canonical_shutdown_payload(owner.public_record, _CHALLENGE),
        hashlib.sha256,
    ).hexdigest()
    identity_proof = hmac.new(
        _NONCE.encode("ascii"),
        canonical_identity_payload(owner.public_record, _CHALLENGE),
        hashlib.sha256,
    ).hexdigest()

    try:
        with TestClient(app, client=_OWNER_PEER) as client:
            wrong_domain = client.post(
                "/api/desktop/shutdown",
                json={"challenge": _CHALLENGE, "proof": identity_proof},
            )
            accepted = client.post(
                "/api/desktop/shutdown",
                json={"challenge": _CHALLENGE, "proof": valid_proof},
            )
        with TestClient(app, client=_REMOTE_PEER) as client:
            remote = client.post(
                "/api/desktop/shutdown",
                json={"challenge": _CHALLENGE, "proof": valid_proof},
            )

        assert wrong_domain.status_code == 403
        assert accepted.status_code == 202
        assert accepted.json() == {"status": "accepted"}
        assert remote.status_code == 403
        assert calls == ["desktop_api_shutdown"]
    finally:
        owner.release()


def test_desktop_shutdown_requested_before_cli_handler_is_replayed() -> None:
    from opensquilla.gateway.boot import _GatewayShutdownRelay

    relay = _GatewayShutdownRelay()
    reasons: list[str] = []

    relay("desktop_api_shutdown")
    relay("later_duplicate")
    assert reasons == []

    relay.install(reasons.append)
    assert reasons == ["desktop_api_shutdown"]

    relay("signal_after_install")
    assert reasons == ["desktop_api_shutdown", "signal_after_install"]


def test_active_desktop_record_cleanup_is_ownership_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv("OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE", _NONCE)
    monkeypatch.setenv(
        DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV,
        str(tmp_path / "gateway-ownership" / profile_lock_key(tmp_path / "profile")),
    )
    owner = activate_desktop_gateway_ownership(
        profile_home=tmp_path / "profile",
        port=18791,
    )

    assert owner is not None
    record_path = owner.path
    assert record_path.exists()

    release_active_desktop_gateway_ownership()

    assert not record_path.exists()


def test_gateway_cli_removes_record_after_profile_writer_lock_exits(
    monkeypatch,
) -> None:
    from opensquilla import recovery
    from opensquilla.cli import gateway_cmd, main
    from opensquilla.gateway import desktop_ownership

    events: list[str] = []

    @contextmanager
    def guarded_profile():
        events.append("profile_lock_acquired")
        try:
            yield None
        finally:
            events.append("profile_lock_released")

    monkeypatch.setattr(recovery, "guarded_desktop_profile", guarded_profile)
    monkeypatch.setattr(
        gateway_cmd,
        "run_gateway",
        lambda **_kwargs: events.append("gateway_stopped"),
    )
    monkeypatch.setattr(
        desktop_ownership,
        "release_active_desktop_gateway_ownership",
        lambda: events.append("record_removed"),
    )

    main.gateway_run(
        port=18791,
        bind="127.0.0.1",
        listen="",
        debug=False,
        config_path=None,
    )

    assert events == [
        "profile_lock_acquired",
        "gateway_stopped",
        "profile_lock_released",
        "record_removed",
    ]
