from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.observability import install_telemetry as telemetry
from opensquilla.observability import network_policy

TEST_ENDPOINT = "https://telemetry.example.test/v1/install"
PRODUCTION_ENDPOINT = "https://telemetry.opensquilla.ai/v1/install"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _enable_telemetry_for_test(monkeypatch):
    monkeypatch.delenv(
        network_policy.NETWORK_OBSERVABILITY_DISABLED_ENV,
        raising=False,
    )
    monkeypatch.delenv(telemetry.TELEMETRY_DISABLED_ENV, raising=False)
    monkeypatch.delenv(
        network_policy.LEGACY_UPDATE_CHECK_DISABLED_ENV,
        raising=False,
    )
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv(telemetry.TELEMETRY_TESTING_ENV, raising=False)


def _set_stable_sources(
    monkeypatch,
    *,
    macs: list[str] | None = None,
    ips: list[str] | None = None,
) -> None:
    monkeypatch.setattr(telemetry, "_collect_mac_address_candidates", lambda: macs or [])
    monkeypatch.setattr(telemetry, "_collect_ip_address_candidates", lambda: ips or [])


def test_default_endpoint_uploads_install_once_and_dedupes(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.delenv(telemetry.TELEMETRY_ENDPOINT_ENV, raising=False)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:01"])
    state_path = tmp_path / "install_telemetry.json"
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        calls.append((endpoint, payload))
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", fake_post)

    first = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")
    second = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert first.sent is True
    assert first.uploaded is True
    assert first.event == "install"
    assert second.sent is False
    assert second.skipped_reason == "already_uploaded"
    assert len(calls) == 1
    endpoint, payload = calls[0]
    assert endpoint == PRODUCTION_ENDPOINT
    assert payload["event"] == "install"
    assert payload["opensquilla_version"] == "1.0.0"
    state = _load(state_path)
    assert state["install_id"] == telemetry._stable_install_id("mac", ["020000000001"])
    assert state["install_id_source"] == "stable-v2-mac"
    assert state["uploaded_install"] is True
    assert state["uploaded_versions"] == ["1.0.0"]


def test_endpoint_empty_creates_install_id_without_upload(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.delenv(telemetry.TELEMETRY_ENDPOINT_ENV, raising=False)
    monkeypatch.setattr(telemetry, "DEFAULT_TELEMETRY_ENDPOINT", "")
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:02"])
    state_path = tmp_path / "install_telemetry.json"

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.sent is False
    assert result.uploaded is False
    assert result.event == "install"
    assert result.skipped_reason == "endpoint_empty"
    state = _load(state_path)
    assert state["install_id"]
    assert state["uploaded_install"] is False
    assert state["uploaded_versions"] == []
    assert state["last_skip_reason"] == "endpoint_empty"


def test_configured_endpoint_uploads_install_once_and_dedupes(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:03"])
    state_path = tmp_path / "install_telemetry.json"
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        calls.append((endpoint, payload))
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", fake_post)

    first = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")
    second = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert first.sent is True
    assert first.uploaded is True
    assert first.event == "install"
    assert second.sent is False
    assert second.skipped_reason == "already_uploaded"
    assert len(calls) == 1
    endpoint, payload = calls[0]
    assert endpoint == TEST_ENDPOINT
    assert payload["event"] == "install"
    assert payload["opensquilla_version"] == "1.0.0"
    assert set(payload) == {
        "schema_version",
        "event",
        "install_id",
        "opensquilla_version",
        "install_method",
        "os",
        "os_version",
        "architecture",
        "python_version",
        "first_seen_at",
        "sent_at",
        "ci_environment",
    }
    assert payload["ci_environment"] is False
    state = _load(state_path)
    assert state["uploaded_install"] is True
    assert state["uploaded_versions"] == ["1.0.0"]


def test_new_version_uploads_version_seen(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:04"])
    state_path = tmp_path / "install_telemetry.json"
    events: list[str] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        events.append(str(payload["event"]))
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", fake_post)

    telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")
    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.1.0")

    assert result.sent is True
    assert result.uploaded is True
    assert result.event == "version_seen"
    assert events == ["install", "version_seen"]
    assert _load(state_path)["uploaded_versions"] == ["1.0.0", "1.1.0"]


def test_disabled_env_skips_without_creating_state(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_DISABLED_ENV, "true")
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    state_path = tmp_path / "install_telemetry.json"

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.disabled is True
    assert result.sent is False
    assert result.skipped_reason == "disabled"
    assert not state_path.exists()


def test_privacy_config_disable_skips_without_creating_state(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    state_path = tmp_path / "install_telemetry.json"
    config = SimpleNamespace(
        privacy=SimpleNamespace(disable_network_observability=True),
    )

    result = telemetry.collect_install_telemetry(
        config=config,
        state_path=state_path,
        version="1.0.0",
    )

    assert result.disabled is True
    assert result.sent is False
    assert result.skipped_reason == "disabled"
    assert not state_path.exists()


def test_hot_privacy_opt_out_before_post_starts_no_request(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:11"])
    state_path = tmp_path / "install_telemetry.json"
    config = SimpleNamespace(
        privacy=SimpleNamespace(disable_network_observability=False),
    )
    original_build_payload = telemetry._build_payload
    post_calls = 0

    def build_payload(*args, **kwargs):
        payload = original_build_payload(*args, **kwargs)
        config.privacy.disable_network_observability = True
        return payload

    def unexpected_post(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        return True, None

    monkeypatch.setattr(telemetry, "_build_payload", build_payload)
    monkeypatch.setattr(telemetry, "_post_payload", unexpected_post)

    result = telemetry.collect_install_telemetry(
        config=config,
        state_path=state_path,
        version="1.0.0",
    )

    assert result.disabled is True
    assert result.sent is False
    assert result.skipped_reason == "disabled"
    assert post_calls == 0
    assert not state_path.exists()


def test_github_actions_env_skips_without_creating_state(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    state_path = tmp_path / "install_telemetry.json"

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.disabled is True
    assert result.sent is False
    assert result.skipped_reason == "environment:GITHUB_ACTIONS"
    assert not state_path.exists()


def test_pytest_current_test_env_skips_without_creating_state(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_file.py::test_name (call)")
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    state_path = tmp_path / "install_telemetry.json"

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.disabled is True
    assert result.sent is False
    assert result.skipped_reason == "environment:PYTEST_CURRENT_TEST"
    assert not state_path.exists()


def test_opensquilla_testing_env_skips_without_creating_state(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_TESTING_ENV, "true")
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    state_path = tmp_path / "install_telemetry.json"

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.disabled is True
    assert result.sent is False
    assert result.skipped_reason == "environment:OPENSQUILLA_TESTING"
    assert not state_path.exists()


def test_payload_marks_ci_environment_when_detected(monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    payload = telemetry._build_payload(
        {
            "install_id": "stable-install-id",
            "first_seen_at": "2026-06-29T00:00:00Z",
        },
        event="install",
        current_version="1.0.0",
        sent_at="2026-06-29T00:00:01Z",
    )

    assert payload["ci_environment"] is True


def test_upload_failure_does_not_mark_install_uploaded(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:05"])
    state_path = tmp_path / "install_telemetry.json"

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        return False, "network_down"

    monkeypatch.setattr(telemetry, "_post_payload", fake_post)

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.sent is True
    assert result.uploaded is False
    assert result.error == "network_down"
    state = _load(state_path)
    assert state["uploaded_install"] is False
    assert state["uploaded_versions"] == []
    assert state["last_error"] == "network_down"


def test_successful_upload_uses_longer_timeout_for_result_merge(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:06"])
    state_path = tmp_path / "install_telemetry.json"
    lock_timeouts: list[float] = []

    from opensquilla import profile_operation_lock

    class RecordingLock:
        def __init__(self, _path, *, timeout):
            lock_timeouts.append(timeout)

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return None

    monkeypatch.setattr(profile_operation_lock, "ProfileOperationLock", RecordingLock)
    monkeypatch.setattr(telemetry, "_post_payload", lambda *_args, **_kwargs: (True, None))

    result = telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    assert result.uploaded is True
    assert lock_timeouts == [
        telemetry._STATE_TRANSACTION_TIMEOUT_SECONDS,
        telemetry._RESULT_STATE_TRANSACTION_TIMEOUT_SECONDS,
    ]


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_state_replace_retries_transient_windows_errors(
    tmp_path,
    monkeypatch,
    winerror,
):
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    source.write_text("next", encoding="utf-8")
    destination.write_text("current", encoding="utf-8")
    real_replace = os.replace
    attempts = 0
    delays: list[float] = []

    def flaky_replace(source_path, destination_path):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("synthetic transient Windows state reader")
            error.winerror = winerror
            raise error
        real_replace(source_path, destination_path)

    monkeypatch.setattr(telemetry.os, "name", "nt")
    monkeypatch.setattr(telemetry.os, "replace", flaky_replace)
    monkeypatch.setattr(telemetry.time, "sleep", delays.append)

    telemetry._replace_state_file(source, destination)

    assert attempts == 3
    assert delays == [0.02, 0.05]
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "next"


def test_state_replace_does_not_retry_permanent_windows_error(tmp_path, monkeypatch):
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    source.write_text("next", encoding="utf-8")
    destination.write_text("current", encoding="utf-8")
    attempts = 0

    def denied_replace(_source_path, _destination_path):
        nonlocal attempts
        attempts += 1
        error = PermissionError("synthetic permanent Windows denial")
        error.winerror = 50
        raise error

    monkeypatch.setattr(telemetry.os, "name", "nt")
    monkeypatch.setattr(telemetry.os, "replace", denied_replace)

    with pytest.raises(PermissionError, match="permanent Windows denial"):
        telemetry._replace_state_file(source, destination)

    assert attempts == 1
    assert source.read_text(encoding="utf-8") == "next"
    assert destination.read_text(encoding="utf-8") == "current"


def test_state_replace_does_not_retry_transient_code_off_windows(tmp_path, monkeypatch):
    source = tmp_path / "state.tmp"
    destination = tmp_path / "state.json"
    source.write_text("next", encoding="utf-8")
    destination.write_text("current", encoding="utf-8")
    attempts = 0

    def denied_replace(_source_path, _destination_path):
        nonlocal attempts
        attempts += 1
        error = PermissionError("synthetic non-Windows denial")
        error.winerror = 32
        raise error

    monkeypatch.setattr(telemetry.os, "name", "posix")
    monkeypatch.setattr(telemetry.os, "replace", denied_replace)

    with pytest.raises(PermissionError, match="non-Windows denial"):
        telemetry._replace_state_file(source, destination)

    assert attempts == 1
    assert source.read_text(encoding="utf-8") == "next"
    assert destination.read_text(encoding="utf-8") == "current"


def test_write_state_cleans_temp_after_transient_windows_retries_exhausted(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "state.json"
    destination.write_text('{"value":"current"}\n', encoding="utf-8")
    attempts = 0
    delays: list[float] = []

    def blocked_replace(_source_path, _destination_path):
        nonlocal attempts
        attempts += 1
        error = PermissionError("synthetic persistent Windows sharing violation")
        error.winerror = 32
        raise error

    monkeypatch.setattr(telemetry.os, "name", "nt")
    monkeypatch.setattr(telemetry.os, "replace", blocked_replace)
    monkeypatch.setattr(telemetry.time, "sleep", delays.append)

    with pytest.raises(PermissionError, match="sharing violation"):
        telemetry._write_state(destination, {"value": "next"})

    assert attempts == 5
    assert delays == [0.02, 0.05, 0.1, 0.2]
    assert destination.read_text(encoding="utf-8") == '{"value":"current"}\n'
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_desktop_env_sets_install_method(monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.delenv(telemetry.TELEMETRY_INSTALL_METHOD_ENV, raising=False)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")

    assert telemetry._detect_install_method() == "desktop"


def test_mac_addresses_generate_stable_install_id(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(
        monkeypatch,
        macs=["02:00:00:00:00:0A", "ff:ff:ff:ff:ff:ff", "01:00:5e:00:00:fb"],
        ips=["10.0.0.5"],
    )
    state_path = tmp_path / "install_telemetry.json"
    calls: list[dict[str, Any]] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        calls.append(payload)
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", fake_post)

    telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    expected = telemetry._stable_install_id("mac", ["02000000000a"])
    state = _load(state_path)
    assert state["install_id"] == expected
    assert state["install_id_source"] == "stable-v2-mac"
    assert calls[0]["install_id"] == expected


def test_mac_address_order_does_not_change_install_id():
    first = telemetry._normalized_mac_addresses(
        ["02:00:00:00:00:0b", "02:00:00:00:00:0a"]
    )
    second = telemetry._normalized_mac_addresses(
        ["02:00:00:00:00:0A", "02:00:00:00:00:0B"]
    )

    assert first == ["02000000000a", "02000000000b"]
    assert telemetry._stable_install_id("mac", first) == telemetry._stable_install_id(
        "mac",
        second,
    )


def test_ip_fallback_generates_stable_install_id_when_no_usable_mac(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(
        monkeypatch,
        macs=["00:00:00:00:00:00", "01:00:5e:00:00:fb"],
        ips=["127.0.0.1", "169.254.10.20", "10.0.0.8"],
    )
    state_path = tmp_path / "install_telemetry.json"

    monkeypatch.setattr(telemetry, "_post_payload", lambda *args, **kwargs: (True, None))

    telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    state = _load(state_path)
    assert state["install_id"] == telemetry._stable_install_id("ip", ["10.0.0.8"])
    assert state["install_id_source"] == "stable-v2-ip"


def test_loopback_endpoint_host_does_not_influence_install_id(tmp_path, monkeypatch):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(
        telemetry.TELEMETRY_ENDPOINT_ENV,
        "http://127.0.0.1:8787/v1/install",
    )
    _set_stable_sources(monkeypatch, macs=[], ips=["127.0.0.1", "10.0.0.9"])
    state_path = tmp_path / "install_telemetry.json"
    calls: list[dict[str, Any]] = []

    def fake_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        calls.append(payload)
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", fake_post)

    telemetry.collect_install_telemetry(state_path=state_path, version="1.0.0")

    expected = telemetry._stable_install_id("ip", ["10.0.0.9"])
    assert _load(state_path)["install_id"] == expected
    assert calls[0]["install_id"] == expected


def test_background_collection_does_not_wait_for_blocked_post_and_preserves_state(
    tmp_path,
    monkeypatch,
):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    _set_stable_sources(monkeypatch, macs=["02:00:00:00:00:10"])
    state_path = tmp_path / "install_telemetry.json"
    post_started = Event()
    release_post = Event()
    payloads: list[dict[str, Any]] = []
    results: list[telemetry.InstallTelemetryResult] = []

    def blocking_post(
        endpoint: str,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[bool, str | None]:
        payloads.append(payload)
        post_started.set()
        if not release_post.wait(timeout=2):
            return False, "test_release_timeout"
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", blocking_post)
    thread = telemetry.start_background_install_telemetry(
        state_path=state_path,
        version="1.0.0",
        on_result=results.append,
    )
    try:
        assert thread is not None
        assert thread.daemon is True
        assert post_started.wait(timeout=1)
        assert thread.is_alive()

        # The worker persisted its install id before entering network I/O, so a
        # concurrent usage uploader sees the same id and valid JSON.
        install_id = telemetry.ensure_install_telemetry_id(state_path=state_path)
        assert install_id == payloads[0]["install_id"]
        assert _load(state_path)["install_id"] == install_id
    finally:
        release_post.set()
        if thread is not None:
            thread.join(timeout=2)

    assert thread is not None and not thread.is_alive()
    assert len(results) == 1
    assert results[0].uploaded is True
    state = _load(state_path)
    assert state["uploaded_install"] is True
    assert state["uploaded_versions"] == ["1.0.0"]


def test_background_collection_honors_privacy_without_thread_or_state(
    tmp_path,
    monkeypatch,
):
    _enable_telemetry_for_test(monkeypatch)
    monkeypatch.setenv(telemetry.TELEMETRY_ENDPOINT_ENV, TEST_ENDPOINT)
    state_path = tmp_path / "install_telemetry.json"
    config = SimpleNamespace(
        privacy=SimpleNamespace(disable_network_observability=True),
    )
    results: list[telemetry.InstallTelemetryResult] = []
    post_calls = 0

    def unexpected_post(*args, **kwargs):
        nonlocal post_calls
        post_calls += 1
        return True, None

    monkeypatch.setattr(telemetry, "_post_payload", unexpected_post)
    thread = telemetry.start_background_install_telemetry(
        config=config,
        state_path=state_path,
        version="1.0.0",
        on_result=results.append,
    )

    assert thread is None
    assert post_calls == 0
    assert not state_path.exists()
    assert len(results) == 1
    assert results[0].disabled is True
    assert results[0].skipped_reason == "disabled"


def test_concurrent_process_results_merge_without_holding_lock_across_network(
    tmp_path,
    monkeypatch,
):
    _enable_telemetry_for_test(monkeypatch)
    state_path = tmp_path / "state" / "install_telemetry.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "install_id": "synthetic-install-id",
                "install_id_source": "stable-v2-mac",
                "first_seen_at": "2026-01-01T00:00:00Z",
                "uploaded_install": True,
                "uploaded_versions": ["0.9.0"],
                "future_field": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "1")
    monkeypatch.setenv("OPENSQUILLA_USER_STATE_DIR", str(tmp_path / "lock-state"))
    from opensquilla.profile_operation_lock import ProfileOperationLock

    # Seed the stable lock inode before the workers race. ProfileOperationLock
    # deliberately fails closed if two untrusted paths race first creation;
    # the daemon/restart overlap modelled here starts after an earlier collector
    # has already established this exact telemetry lock.
    with ProfileOperationLock(state_path):
        pass

    go_path = tmp_path / "go"
    ready_paths = [tmp_path / "ready-a", tmp_path / "ready-b"]
    worker_source = """
import json
import sys
import time
from pathlib import Path

from opensquilla.observability import install_telemetry as telemetry

state_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
go_path = Path(sys.argv[3])
version = sys.argv[4]

def blocked_post(endpoint, payload, *, timeout):
    ready_path.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not go_path.exists():
        if time.monotonic() >= deadline:
            return False, "barrier_timeout"
        time.sleep(0.01)
    return True, None

telemetry._post_payload = blocked_post
result = telemetry.collect_install_telemetry(
    state_path=state_path,
    version=version,
)
print(json.dumps({
    "sent": result.sent,
    "uploaded": result.uploaded,
    "skipped_reason": result.skipped_reason,
    "error": result.error,
}))
"""
    env = os.environ.copy()
    env[telemetry.TELEMETRY_ENDPOINT_ENV] = TEST_ENDPOINT
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                worker_source,
                str(state_path),
                str(ready_path),
                str(go_path),
                version,
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for ready_path, version in zip(
            ready_paths,
            ("1.0.0", "2.0.0"),
            strict=True,
        )
    ]
    outputs: list[tuple[str, str]] = []
    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_paths):
            for worker in workers:
                if worker.poll() is not None:
                    stdout, stderr = worker.communicate()
                    raise AssertionError(
                        "telemetry worker exited before the network barrier: "
                        f"rc={worker.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                    )
            if time.monotonic() >= deadline:
                raise AssertionError("both workers must reach the network barrier")
            time.sleep(0.01)
        # Both workers reaching this barrier proves the process lock is not held
        # across the simulated network operation.
        go_path.write_text("go", encoding="utf-8")
        outputs = [worker.communicate(timeout=10) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)

    for worker, (stdout, stderr) in zip(workers, outputs, strict=True):
        assert worker.returncode == 0, stderr
        result = json.loads(stdout)
        assert result["sent"] is True, (
            f"telemetry worker result={result!r}, stderr={stderr!r}"
        )
        assert result["uploaded"] is True, (
            f"telemetry worker result={result!r}, stderr={stderr!r}"
        )
    state = _load(state_path)
    assert set(state["uploaded_versions"]) == {"0.9.0", "1.0.0", "2.0.0"}
    assert state["future_field"] == {"preserve": True}
