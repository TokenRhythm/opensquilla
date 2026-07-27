from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.observability import network_policy, update_check


@pytest.fixture(autouse=True)
def _reset_module_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # The module memoizes the last result in a global; clear it between tests.
    monkeypatch.setattr(update_check, "_CACHED_INFO", {})
    monkeypatch.setattr(update_check, "_REFRESH_LOCKS", {})
    monkeypatch.setattr(update_check, "_BACKGROUND_THREADS", {})


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        network_policy.NETWORK_OBSERVABILITY_DISABLED_ENV,
        update_check.UPDATE_CHECK_DISABLED_ENV,
        update_check.TELEMETRY_DISABLED_ENV,
        update_check.UPDATE_CHECK_ENDPOINT_ENV,
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        update_check.TELEMETRY_TESTING_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_fetch(
    tag: str | None,
    url: str | None = "https://example.test/r",
    error: str | None = None,
):
    calls: list[str] = []

    def fetch(endpoint: str, current_version: str, *, timeout: float):
        calls.append(endpoint)
        return tag, url, error

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _manifest(
    *,
    tag: str = "v0.5.0rc5",
    version: str = "0.5.0-rc5",
    base: str = "0.5.0",
    prerelease: object = True,
    release_url: str | None = None,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "tag": tag,
        "version": version,
        "baseVersion": base,
        "prerelease": prerelease,
        "publishedAt": "2026-07-15T00:00:00Z",
        "releaseUrl": release_url
        or f"https://github.com/opensquilla/opensquilla/releases/tag/{tag}",
        # Python intentionally does not consume platform assets, but including
        # them keeps fixtures representative of the shared desktop manifest.
        "sha256sums": "SHA256SUMS",
        "platforms": {},
    }


def _install_httpx_payload(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    *,
    expected_endpoint: str,
    status_code: int = 200,
) -> None:
    import httpx

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is True

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, endpoint: str, *, headers: dict[str, str]):
            assert endpoint == expected_endpoint
            assert headers["Accept"] == "application/json"
            assert headers["User-Agent"].startswith("opensquilla/")
            return SimpleNamespace(status_code=status_code, json=lambda: payload)

    monkeypatch.setattr(httpx, "Client", FakeClient)


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.5.0", "0.4.1", True),
        ("0.4.1", "0.4.1", False),
        ("0.4.0", "0.4.1", False),
        ("v0.5.0", "0.4.1", True),  # leading v tolerated
        ("0.4.1", "0.4.1rc1", True),  # release supersedes its own pre-release
        # Pre-release ordinals are intentionally NOT compared: releases/latest
        # never returns a pre-release, so two same-core pre-releases are "equal".
        ("0.4.1rc2", "0.4.1rc1", False),
        ("0.5.0", "0.0.0+unknown", False),  # dev/source checkout is never nagged
        ("0.5.0", None, False),
    ],
)
def test_is_newer(latest: str, current: str | None, expected: bool) -> None:
    assert update_check._is_newer(latest, current) is expected


def test_refresh_detects_and_persists_update(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    fetch = _fake_fetch("0.5.0", "https://example.test/release")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    state_path = tmp_path / "update_check.json"

    info = update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    assert info.update_available is True
    assert info.latest_version == "0.5.0"
    assert info.release_url == "https://example.test/release"
    assert info.to_public_dict()["available"] is True
    state = _load(state_path)
    assert state["latest_version"] == "0.5.0"
    assert isinstance(state["checked_ts"], int)
    assert fetch.calls == [update_check.DEFAULT_UPDATE_CHECK_ENDPOINT]


def test_refresh_reports_no_update(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(update_check, "_fetch_latest_release", _fake_fetch("0.4.1"))
    state_path = tmp_path / "update_check.json"

    info = update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    assert info.latest_version == "0.4.1"
    assert info.update_available is False


def test_candidate_without_exact_url_uses_generic_releases_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        update_check,
        "_fetch_latest_release",
        _fake_fetch("0.5.0", None),
    )

    info = update_check.refresh_update_check(
        state_path=tmp_path / "update_check.json",
        version="0.4.1",
    )

    assert info.update_available is True
    assert info.release_url == update_check.DEFAULT_RELEASES_INDEX_PAGE
    assert not info.release_url.endswith("/latest")


def test_refresh_uses_cache_within_ttl(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    fetch = _fake_fetch("0.5.0")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    state_path = tmp_path / "update_check.json"

    first = update_check.refresh_update_check(state_path=state_path, version="0.4.1")
    # Drop the in-memory cache so the TTL path is exercised via the state file.
    monkeypatch.setattr(update_check, "_CACHED_INFO", None)
    second = update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    assert first.update_available is True
    assert second.update_available is True
    assert second.from_cache is True
    assert len(fetch.calls) == 1  # second call served from cache, no network


def test_force_bypasses_ttl(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    fetch = _fake_fetch("0.5.0")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    state_path = tmp_path / "update_check.json"

    update_check.refresh_update_check(state_path=state_path, version="0.4.1")
    update_check.refresh_update_check(state_path=state_path, version="0.4.1", force=True)

    assert len(fetch.calls) == 2


def test_disabled_env_skips_network(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv(update_check.UPDATE_CHECK_DISABLED_ENV, "1")
    fetch = _fake_fetch("0.5.0")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    state_path = tmp_path / "update_check.json"

    info = update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    assert info.disabled is True
    assert info.update_available is False
    assert fetch.calls == []


def test_disabled_env_skips_forced_network_check(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv(update_check.UPDATE_CHECK_DISABLED_ENV, "1")
    fetch = _fake_fetch("0.5.0")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)

    info = update_check.refresh_update_check(
        state_path=tmp_path / "update_check.json",
        version="0.4.1",
        force=True,
    )

    assert info.disabled is True
    assert info.update_available is False
    assert fetch.calls == []


def test_telemetry_disable_also_silences_update_check(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv(update_check.TELEMETRY_DISABLED_ENV, "1")
    fetch = _fake_fetch("0.5.0")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)

    info = update_check.refresh_update_check(
        state_path=tmp_path / "update_check.json", version="0.4.1"
    )

    assert info.disabled is True
    assert fetch.calls == []


def test_privacy_config_disable_skips_refresh_cached_and_background(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _enable(monkeypatch)
    fetch = _fake_fetch("0.5.0")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    state_path = tmp_path / "update_check.json"
    config = SimpleNamespace(
        privacy=SimpleNamespace(disable_network_observability=True),
    )

    refreshed = update_check.refresh_update_check(
        config=config,
        state_path=state_path,
        version="0.4.1",
        force=True,
    )
    cached = update_check.get_cached_update_info(
        config=config,
        state_path=state_path,
        version="0.4.1",
    )
    thread = update_check.start_background_update_check(
        config=config,
        state_path=state_path,
        version="0.4.1",
    )

    assert refreshed.disabled is True
    assert refreshed.update_available is False
    assert cached is None
    assert thread is None
    assert fetch.calls == []


@pytest.mark.parametrize(
    "env_name",
    [
        network_policy.NETWORK_OBSERVABILITY_DISABLED_ENV,
        update_check.TELEMETRY_DISABLED_ENV,
        update_check.UPDATE_CHECK_DISABLED_ENV,
    ],
)
def test_all_privacy_env_controls_prevent_network_and_thread_creation(
    env_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setenv(env_name, "true")
    fetch = _fake_fetch("0.5.0rc5")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)

    class UnexpectedThread:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("privacy-disabled checks must not create a thread")

    monkeypatch.setattr(update_check.threading, "Thread", UnexpectedThread)
    state_path = tmp_path / "update_check_rc.json"

    info = update_check.refresh_update_check(
        state_path=state_path,
        version="0.5.0rc4",
        force=True,
    )
    background = update_check.start_background_update_check(
        state_path=state_path,
        version="0.5.0rc4",
    )

    assert info.disabled is True
    assert background is None
    assert fetch.calls == []
    assert not state_path.exists()


def test_get_cached_honors_disabled_env_after_prior_check(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(update_check, "_fetch_latest_release", _fake_fetch("0.5.0"))
    state_path = tmp_path / "update_check.json"
    update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    monkeypatch.setenv(update_check.UPDATE_CHECK_DISABLED_ENV, "1")
    info = update_check.get_cached_update_info(state_path=state_path, version="0.4.1")

    assert info is None


def test_get_cached_recomputes_against_current_version(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(update_check, "_fetch_latest_release", _fake_fetch("0.5.0"))
    state_path = tmp_path / "update_check.json"
    update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    # The just-upgraded process (now 0.5.0) should no longer see an update.
    info = update_check.get_cached_update_info(state_path=state_path, version="0.5.0")
    assert info is not None
    assert info.update_available is False

    # A still-older process keeps seeing it.
    stale = update_check.get_cached_update_info(state_path=state_path, version="0.4.1")
    assert stale is not None
    assert stale.update_available is True


def test_get_cached_returns_none_before_first_check(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    info = update_check.get_cached_update_info(
        state_path=tmp_path / "update_check.json", version="0.4.1"
    )
    assert info is None


def test_fetch_failure_keeps_prior_cache(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check.json"
    monkeypatch.setattr(update_check, "_fetch_latest_release", _fake_fetch("0.5.0"))
    update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    # A later check fails (offline); the previously-known latest must survive.
    monkeypatch.setattr(update_check, "_fetch_latest_release", _fake_fetch(None, None, "offline"))
    monkeypatch.setattr(update_check, "_CACHED_INFO", None)
    info = update_check.refresh_update_check(state_path=state_path, version="0.4.1", force=True)

    assert info.latest_version == "0.5.0"
    assert info.update_available is True
    assert info.error == "offline"


@pytest.mark.parametrize(
    "value",
    ["0.5.0rc4", "0.5.0-rc4", "0.5.0-rc.4", "v0.5.0-RC4"],
)
def test_rc_channel_accepts_supported_tag_spellings(value: str) -> None:
    channel = update_check._channel_for(value)

    assert channel.kind == "rc"
    assert channel.scope == "rc:0.5.0"
    assert channel.state_file == "update_check_rc.json"


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.5.0rc5", "0.5.0rc4", True),
        ("0.5.0-rc.5", "0.5.0-rc4", True),
        ("0.5.0rc10", "0.5.0rc9", True),
        ("0.5.0rc9", "0.5.0rc10", False),
        ("0.5.0", "0.5.0rc10", True),
        ("0.6.0rc1", "0.5.0rc4", False),
    ],
)
def test_rc_channel_comparison_is_numeric_and_same_base(
    latest: str, current: str, expected: bool
) -> None:
    channel = update_check._channel_for(current)

    assert update_check._is_newer_for_channel(latest, current, channel) is expected


def test_channel_endpoints_are_static_and_rc_scoped(monkeypatch) -> None:
    stable = update_check._channel_for("0.4.1")
    preview = update_check._channel_for("0.5.0rc4")

    assert stable.endpoint == (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/channels/stable.json"
    )
    assert preview.endpoint == (
        "https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/"
        "releases/channels/preview/0.5.0.json"
    )

    monkeypatch.setenv(
        update_check.UPDATE_CHECK_ENDPOINT_ENV,
        " https://updates.example.test/custom.json ",
    )
    assert update_check._endpoint(stable) == "https://updates.example.test/custom.json"
    assert update_check._endpoint(preview) == "https://updates.example.test/custom.json"


@pytest.mark.parametrize(
    ("current", "payload", "expected"),
    [
        (
            "0.4.1",
            _manifest(
                tag="v0.5.0",
                version="0.5.0",
                base="0.5.0",
                prerelease=False,
            ),
            (
                "0.5.0",
                "https://github.com/opensquilla/opensquilla/releases/tag/v0.5.0",
                None,
            ),
        ),
        (
            "0.5.0rc4",
            _manifest(),
            (
                "0.5.0-rc5",
                "https://github.com/opensquilla/opensquilla/releases/tag/v0.5.0rc5",
                None,
            ),
        ),
        (
            "0.5.0rc10",
            _manifest(
                tag="v0.5.0",
                version="0.5.0",
                base="0.5.0",
                prerelease=False,
            ),
            (
                "0.5.0",
                "https://github.com/opensquilla/opensquilla/releases/tag/v0.5.0",
                None,
            ),
        ),
    ],
)
def test_fetch_accepts_valid_static_channel_manifest(
    current: str,
    payload: object,
    expected: tuple[str | None, str | None, str | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = update_check._channel_for(current).endpoint
    _install_httpx_payload(monkeypatch, payload, expected_endpoint=endpoint)

    assert (
        update_check._fetch_latest_release(
            endpoint,
            current,
            timeout=1.0,
        )
        == expected
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {**_manifest(), "schemaVersion": True},
        {**_manifest(), "schemaVersion": 2},
        {**_manifest(), "tag": "vv0.5.0rc5"},
        {
            **_manifest(tag="V0.5.0RC5"),
            "releaseUrl": ("https://github.com/opensquilla/opensquilla/releases/tag/V0.5.0RC5"),
        },
        {**_manifest(), "version": "0.5.0rc5"},
        {**_manifest(), "version": "0.5.0-RC5"},
        {**_manifest(), "version": "0.5.0-rc6"},
        {**_manifest(), "baseVersion": "0.6.0"},
        {**_manifest(), "prerelease": 1},
        {**_manifest(), "prerelease": False},
        {
            **_manifest(),
            "releaseUrl": "https://example.test/opensquilla/v0.5.0rc5",
        },
        _manifest(
            tag="v0.6.0rc1",
            version="0.6.0-rc1",
            base="0.6.0",
            prerelease=True,
        ),
    ],
)
def test_rc_fetch_rejects_invalid_or_cross_base_manifest(
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = "0.5.0rc4"
    endpoint = update_check._channel_for(current).endpoint
    _install_httpx_payload(monkeypatch, payload, expected_endpoint=endpoint)

    assert update_check._fetch_latest_release(
        endpoint,
        current,
        timeout=1.0,
    ) == (None, None, "manifest_invalid")


def test_stable_fetch_rejects_prerelease_manifest(monkeypatch) -> None:
    current = "0.4.1"
    endpoint = update_check._channel_for(current).endpoint
    _install_httpx_payload(monkeypatch, _manifest(), expected_endpoint=endpoint)

    assert update_check._fetch_latest_release(
        endpoint,
        current,
        timeout=1.0,
    ) == (None, None, "manifest_invalid")


def test_channel_manifest_http_failure_is_not_a_successful_empty_result(
    monkeypatch,
) -> None:
    current = "0.5.0rc4"
    endpoint = update_check._channel_for(current).endpoint
    _install_httpx_payload(
        monkeypatch,
        _manifest(),
        expected_endpoint=endpoint,
        status_code=503,
    )

    assert update_check._fetch_latest_release(
        endpoint,
        current,
        timeout=1.0,
    ) == (None, None, "http_status_503")


def test_manifest_failure_keeps_prior_same_scope_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    real_fetch = update_check._fetch_latest_release
    monkeypatch.setattr(
        update_check,
        "_fetch_latest_release",
        _fake_fetch("0.5.0-rc5", "https://example.test/rc5"),
    )
    update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")

    endpoint = update_check._channel_for("0.5.0rc4").endpoint
    _install_httpx_payload(monkeypatch, [], expected_endpoint=endpoint)
    monkeypatch.setattr(update_check, "_fetch_latest_release", real_fetch)
    monkeypatch.setattr(update_check, "_CACHED_INFO", {})
    info = update_check.refresh_update_check(
        state_path=state_path,
        version="0.5.0rc4",
        force=True,
    )

    assert info.latest_version == "0.5.0-rc5"
    assert info.update_available is True
    assert info.error == "manifest_invalid"


def test_legacy_stable_cache_without_scope_remains_compatible(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "latest_version": "0.5.0",
                "release_url": "https://example.test/stable",
                "checked_at": "2026-07-13T00:00:00Z",
                "checked_ts": update_check._now_ts(),
            }
        ),
        encoding="utf-8",
    )

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("fresh legacy stable state must not access the network")

    monkeypatch.setattr(update_check, "_fetch_latest_release", fail_fetch)

    info = update_check.refresh_update_check(state_path=state_path, version="0.4.1")

    assert info.from_cache is True
    assert info.update_available is True
    assert info.latest_version == "0.5.0"


def test_rc_refresh_uses_separate_scoped_state_file(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    stable_path = tmp_path / "update_check.json"
    stable_state = {
        "schema_version": 1,
        "latest_version": "0.4.1",
        "release_url": "https://example.test/stable",
        "checked_ts": update_check._now_ts(),
    }
    stable_path.write_text(json.dumps(stable_state), encoding="utf-8")
    original_stable_bytes = stable_path.read_bytes()
    fetch = _fake_fetch("0.5.0rc5", "https://example.test/rc5")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    config = SimpleNamespace(state_dir=str(tmp_path), privacy=None)

    info = update_check.refresh_update_check(config=config, version="0.5.0rc4")

    assert info.update_available is True
    assert info.latest_version == "0.5.0rc5"
    assert stable_path.read_bytes() == original_stable_bytes
    rc_state = _load(tmp_path / "update_check_rc.json")
    assert rc_state["cache_scope"] == "rc:0.5.0"
    assert rc_state["latest_version"] == "0.5.0rc5"
    assert fetch.calls == [update_check._channel_for("0.5.0rc4").endpoint]


def test_rc_cached_candidate_recomputes_for_upgrade_and_downgrade(
    tmp_path: Path, monkeypatch
) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    monkeypatch.setattr(
        update_check,
        "_fetch_latest_release",
        _fake_fetch("0.5.0rc5", "https://example.test/rc5"),
    )
    update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")

    current = update_check.get_cached_update_info(state_path=state_path, version="0.5.0rc5")
    downgraded = update_check.get_cached_update_info(state_path=state_path, version="0.5.0rc4")

    assert current is not None and current.update_available is False
    assert downgraded is not None and downgraded.update_available is True


def test_rc_scope_mismatch_never_falls_back_on_failure(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "cache_scope": "rc:0.5.0",
                "latest_version": "0.5.0rc5",
                "release_url": "https://example.test/rc5",
                "checked_at": "2026-07-13T00:00:00Z",
                "checked_ts": update_check._now_ts(),
                "last_attempt_ts": update_check._now_ts(),
            }
        ),
        encoding="utf-8",
    )
    fetch = _fake_fetch(None, None, "offline")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)

    info = update_check.refresh_update_check(state_path=state_path, version="0.6.0rc1")

    assert info.latest_version is None
    assert info.update_available is False
    assert info.error == "offline"
    assert len(fetch.calls) == 1
    state = _load(state_path)
    assert state["cache_scope"] == "rc:0.6.0"
    assert "latest_version" not in state


def test_successful_empty_rc_result_is_cached_and_clears_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    monkeypatch.setattr(update_check, "_fetch_latest_release", _fake_fetch("0.5.0rc5"))
    update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")

    empty_fetch = _fake_fetch(None, None, None)
    monkeypatch.setattr(update_check, "_fetch_latest_release", empty_fetch)
    emptied = update_check.refresh_update_check(
        state_path=state_path,
        version="0.5.0rc4",
        force=True,
    )
    monkeypatch.setattr(update_check, "_CACHED_INFO", {})
    cached = update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")

    assert emptied.latest_version is None
    assert cached.latest_version is None
    assert cached.from_cache is True
    assert len(empty_fetch.calls) == 1
    state = _load(state_path)
    assert state["latest_version"] is None
    assert state["last_error"] is None
    assert isinstance(state["checked_ts"], int)


def test_failed_attempt_is_throttled_across_memory_reset(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    fetch = _fake_fetch(None, None, "offline")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)

    first = update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")
    monkeypatch.setattr(update_check, "_CACHED_INFO", {})
    second = update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")

    assert first.error == "offline"
    assert second.error == "offline"
    assert second.from_cache is True
    assert len(fetch.calls) == 1
    state = _load(state_path)
    assert isinstance(state["last_attempt_ts"], int)
    assert "checked_ts" not in state


def test_background_update_check_is_single_flight(tmp_path: Path, monkeypatch) -> None:
    _enable(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fetch(endpoint: str, current_version: str, *, timeout: float):
        calls.append(endpoint)
        entered.set()
        assert release.wait(timeout=5)
        return "0.5.0rc5", "https://example.test/rc5", None

    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    state_path = tmp_path / "update_check_rc.json"

    first = update_check.start_background_update_check(state_path=state_path, version="0.5.0rc4")
    assert first is not None
    assert entered.wait(timeout=5)
    second = update_check.start_background_update_check(state_path=state_path, version="0.5.0rc4")

    assert second is first
    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert len(calls) == 1


def test_background_update_check_does_not_spawn_for_fresh_state(
    tmp_path: Path, monkeypatch
) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    fetch = _fake_fetch("0.5.0rc5")
    monkeypatch.setattr(update_check, "_fetch_latest_release", fetch)
    update_check.refresh_update_check(state_path=state_path, version="0.5.0rc4")

    class UnexpectedThread:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("fresh state must not create a background thread")

    monkeypatch.setattr(update_check.threading, "Thread", UnexpectedThread)

    assert (
        update_check.start_background_update_check(state_path=state_path, version="0.5.0rc4")
        is None
    )
    assert len(fetch.calls) == 1


@pytest.mark.parametrize(
    "corrupt_bytes",
    [
        b"{not-json",
        b"\xff\xfe\xfa",
        b'{"cache_scope":"rc:0.5.0","last_attempt_ts":NaN}',
        b'{"cache_scope":"rc:0.5.0","last_attempt_ts":Infinity}',
    ],
)
def test_corrupt_state_is_softly_replaced_by_a_valid_scoped_result(
    corrupt_bytes: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    state_path.write_bytes(corrupt_bytes)
    monkeypatch.setattr(
        update_check,
        "_fetch_latest_release",
        _fake_fetch("0.5.0rc5", "https://example.test/rc5"),
    )

    info = update_check.refresh_update_check(
        state_path=state_path,
        version="0.5.0rc4",
    )

    assert info.update_available is True
    state = _load(state_path)
    assert state["schema_version"] == 2
    assert state["cache_scope"] == "rc:0.5.0"
    assert state["latest_version"] == "0.5.0rc5"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_state_replace_is_private_and_leaves_no_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    state_path = tmp_path / "update_check_rc.json"
    state_path.write_text("{}", encoding="utf-8")
    state_path.chmod(0o644)
    monkeypatch.setattr(
        update_check,
        "_fetch_latest_release",
        _fake_fetch("0.5.0rc5", "https://example.test/rc5"),
    )

    update_check.refresh_update_check(
        state_path=state_path,
        version="0.5.0rc4",
        force=True,
    )

    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".update_check_rc.json.*.tmp")) == []
