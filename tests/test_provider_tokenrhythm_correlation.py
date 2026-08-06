from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

from opensquilla.observability import install_telemetry
from opensquilla.observability.network_policy import provider_install_id_disabled
from opensquilla.provider import tokenrhythm_correlation as tokenrhythm
from opensquilla.provider.tokenrhythm_correlation import (
    TOKENRHYTHM_CALL_KIND_HEADER,
    TOKENRHYTHM_EXECUTION_ID_HEADER,
    TOKENRHYTHM_INSTALL_ID_HEADER,
    TOKENRHYTHM_SESSION_ID_HEADER,
    TOKENRHYTHM_TURN_ID_HEADER,
    _reset_tokenrhythm_install_id_cache_for_tests,
    is_tokenrhythm_correlation_target,
    prewarm_tokenrhythm_install_id,
    redact_tokenrhythm_install_ids,
    tokenrhythm_correlation_headers,
    tokenrhythm_install_id_headers,
)
from opensquilla.provider.types import (
    ChatConfig,
    ProviderRequestCorrelation,
    derive_provider_request_correlation,
)


@pytest.fixture(autouse=True)
def _isolated_install_id_cache(monkeypatch: pytest.MonkeyPatch):
    _reset_tokenrhythm_install_id_cache_for_tests()
    for name in (
        "GITHUB_ACTIONS",
        "OPENSQUILLA_TESTING",
        "PYTEST_CURRENT_TEST",
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        "OPENSQUILLA_TELEMETRY_DISABLED",
        "OPENSQUILLA_UPDATE_CHECK_DISABLED",
        "OPENSQUILLA_TRUST_ENV",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    # Pytest refreshes this variable between fixture setup and the test-call
    # phase. Keep production's auto-suppression intact while allowing these
    # focused resolver tests to opt out of pytest's own marker value.
    def _policy_without_runner_marker(*, config=None):
        env = dict(os.environ)
        marker = env.get("PYTEST_CURRENT_TEST", "")
        if marker.endswith((" (setup)", " (call)", " (teardown)")):
            env.pop("PYTEST_CURRENT_TEST", None)
        return provider_install_id_disabled(config=config, env=env)

    monkeypatch.setattr(
        tokenrhythm,
        "provider_install_id_disabled",
        _policy_without_runner_marker,
    )
    yield
    _reset_tokenrhythm_install_id_cache_for_tests()


def _config(state_dir, *, privacy_disabled: bool = False):
    return SimpleNamespace(
        state_dir=str(state_dir),
        privacy=SimpleNamespace(
            disable_network_observability=privacy_disabled,
        ),
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://tokenrhythm.studio/v1",
        "https://api.tokenrhythm.studio/v1",
    ],
)
def test_tokenrhythm_correlation_accepts_official_https_origins(
    base_url: str,
) -> None:
    assert is_tokenrhythm_correlation_target("tokenrhythm", base_url)


@pytest.mark.parametrize(
    ("provider_kind", "base_url"),
    [
        ("openrouter", "https://tokenrhythm.studio/v1"),
        ("tokenrhythm", "http://tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://api-tokenrhythm.example/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio.example.com/v1"),
        ("tokenrhythm", "https://eviltokenrhythm.studio/v1"),
        ("tokenrhythm", "https://user@tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://@tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio:8443/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio:/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio:0443/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio:invalid/v1"),
        ("tokenrhythm", "https://proxy.example.com/v1"),
        ("tokenrhythm", "tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://[tokenrhythm.studio"),
    ],
)
def test_tokenrhythm_correlation_rejects_untrusted_targets(
    provider_kind: str,
    base_url: str,
) -> None:
    assert not is_tokenrhythm_correlation_target(provider_kind, base_url)


def test_tokenrhythm_correlation_builds_only_safe_nonempty_headers() -> None:
    headers = tokenrhythm_correlation_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        ProviderRequestCorrelation(
            session_id="2a202e18-8c4d-4f76-bc1e-fbe5b5ed2513",
            turn_id="turn_123",
            execution_id="execution:123",
            call_kind="agent.chat",
        ),
    )

    assert headers == {
        TOKENRHYTHM_SESSION_ID_HEADER: "2a202e18-8c4d-4f76-bc1e-fbe5b5ed2513",
        TOKENRHYTHM_TURN_ID_HEADER: "turn_123",
        TOKENRHYTHM_EXECUTION_ID_HEADER: "execution:123",
        TOKENRHYTHM_CALL_KIND_HEADER: "agent.chat",
    }


def test_tokenrhythm_correlation_accepts_explicit_standard_https_port() -> None:
    assert is_tokenrhythm_correlation_target(
        "tokenrhythm",
        "https://tokenrhythm.studio:443/v1",
    )


def test_tokenrhythm_correlation_drops_all_headers_when_one_id_is_invalid() -> None:
    headers = tokenrhythm_correlation_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        ProviderRequestCorrelation(
            session_id="session\r\ninjected",
            turn_id="turn-1",
            execution_id="execution-1",
            call_kind="agent.chat",
        ),
    )

    assert headers == {}


@pytest.mark.parametrize(
    "missing_field",
    [
        "session_id",
        "turn_id",
        "execution_id",
        "call_kind",
    ],
)
def test_tokenrhythm_correlation_requires_all_four_values(
    missing_field: str,
) -> None:
    values = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "execution_id": "execution-1",
        "call_kind": "agent.chat",
    }
    values[missing_field] = ""

    headers = tokenrhythm_correlation_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        ProviderRequestCorrelation(**values),
    )

    assert headers == {}


@pytest.mark.parametrize(
    "call_kind",
    [
        "agent.chat",
        "subagent.chat",
        "auxiliary.meta",
        "auxiliary.image_generation",
        "auxiliary.image_generation.provider_fallback",
        "agent.ensemble.proposer",
        "subagent.ensemble.aggregator",
        "agent.chat.provider_fallback",
        "auxiliary.vision_gate.provider_fallback",
    ],
)
def test_tokenrhythm_correlation_accepts_closed_call_kind_combinations(
    call_kind: str,
) -> None:
    headers = tokenrhythm_correlation_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="execution-1",
            call_kind=call_kind,
        ),
    )

    assert headers[TOKENRHYTHM_CALL_KIND_HEADER] == call_kind


@pytest.mark.parametrize(
    "call_kind",
    [
        "agent",
        "agent.chat.extra",
        "agent.ensemble.unknown",
        "auxiliary.user_supplied",
        "auxiliary.meta.chat",
        "auxiliary.compaction.ensemble.fallback_single",
        "auxiliary.vision_gate.ensemble.aggregator.provider_fallback",
        "subagent.chat.provider_fallback.extra",
        "agent.chat.provider_fallback.provider_fallback",
    ],
)
def test_tokenrhythm_correlation_rejects_untrusted_call_kind_combinations(
    call_kind: str,
) -> None:
    assert (
        tokenrhythm_correlation_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
            ProviderRequestCorrelation(
                session_id="session-1",
                turn_id="turn-1",
                execution_id="execution-1",
                call_kind=call_kind,
            ),
        )
        == {}
    )


def test_tokenrhythm_correlation_rejects_call_kind_over_96_characters() -> None:
    assert (
        tokenrhythm_correlation_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
            ProviderRequestCorrelation(
                session_id="session-1",
                turn_id="turn-1",
                execution_id="execution-1",
                call_kind="a" * 97,
            ),
        )
        == {}
    )


def test_tokenrhythm_correlation_does_not_send_to_custom_host() -> None:
    assert (
        tokenrhythm_correlation_headers(
            "tokenrhythm",
            "https://company-proxy.example/v1",
            ProviderRequestCorrelation(
                session_id="session-1",
                turn_id="turn-1",
                execution_id="execution-1",
                call_kind="agent.chat",
            ),
        )
        == {}
    )


def test_direct_privacy_env_suppresses_complete_correlation_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        "TRUE",
    )

    assert (
        tokenrhythm_correlation_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
            ProviderRequestCorrelation(
                session_id="session-1",
                turn_id="turn-1",
                execution_id="execution-1",
                call_kind="agent.chat",
            ),
        )
        == {}
    )


@pytest.mark.parametrize(
    "legacy_env",
    [
        "OPENSQUILLA_TELEMETRY_DISABLED",
        "OPENSQUILLA_UPDATE_CHECK_DISABLED",
    ],
)
def test_legacy_privacy_env_does_not_suppress_provider_correlation(
    monkeypatch: pytest.MonkeyPatch,
    legacy_env: str,
) -> None:
    monkeypatch.setenv(legacy_env, "true")

    headers = tokenrhythm_correlation_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        ProviderRequestCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            execution_id="execution-1",
            call_kind="agent.chat",
        ),
    )

    assert len(headers) == 4


def test_provider_request_correlation_is_not_serialized_or_represented() -> None:
    config = ChatConfig(
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="private-session-id",
            turn_id="private-turn-id",
            execution_id="private-execution-id",
            call_kind="agent.chat",
        )
    )

    assert "provider_request_correlation" not in config.model_dump()
    assert "private-session-id" not in repr(config)
    assert "private-turn-id" not in repr(config)
    assert "private-execution-id" not in repr(config)


def test_derive_provider_request_correlation_changes_only_requested_fields() -> None:
    correlation = ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-1",
        call_kind="agent.chat",
    )

    derived = derive_provider_request_correlation(
        correlation,
        execution_id="execution-2",
        call_kind="subagent.chat",
    )

    assert derived == ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="turn-1",
        execution_id="execution-2",
        call_kind="subagent.chat",
    )
    assert derive_provider_request_correlation(None, call_kind="agent.chat") is None


def test_install_id_prewarm_is_daemon_nonblocking_and_cached(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_id = "a" * 64
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[object, object, bool]] = []
    config = _config(tmp_path / "profile-state")

    def _ensure(*, config, state_path):
        calls.append((config, state_path, threading.current_thread().daemon))
        entered.set()
        assert release.wait(timeout=2)
        return install_id

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)

    worker = prewarm_tokenrhythm_install_id(config=config)
    assert worker is not None
    assert worker.daemon is True
    assert entered.wait(timeout=2)

    # The current request does not wait for state I/O.
    assert (
        tokenrhythm_install_id_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
        )
        == {}
    )

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert calls == [
        (
            config,
            tmp_path / "profile-state" / "install_telemetry.json",
            True,
        )
    ]
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: install_id}
    assert install_id not in repr(tokenrhythm._INSTALL_ID_CACHE)
    assert prewarm_tokenrhythm_install_id(config=config) is None
    assert len(calls) == 1


def test_install_id_concurrent_cold_prewarm_uses_one_worker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    call_count = 0
    call_count_lock = threading.Lock()
    config = _config(tmp_path / "state")

    def _ensure(*, config, state_path):
        del config, state_path
        nonlocal call_count
        with call_count_lock:
            call_count += 1
        entered.set()
        assert release.wait(timeout=2)
        return "b" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)
    barrier = threading.Barrier(8)
    results: list[threading.Thread | None] = []

    def _prewarm() -> None:
        barrier.wait(timeout=2)
        results.append(prewarm_tokenrhythm_install_id(config=config))

    callers = [threading.Thread(target=_prewarm) for _ in range(8)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=2)

    assert entered.wait(timeout=2)
    assert len(results) == 8
    assert results[0] is not None
    assert all(result is results[0] for result in results)
    assert call_count == 1
    release.set()
    results[0].join(timeout=2)


def test_install_id_cache_is_isolated_by_profile_state_path(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_config = _config(tmp_path / "first")
    second_config = _config(tmp_path / "second")
    paths = []

    def _ensure(*, config, state_path):
        del config
        paths.append(state_path)
        return "1" * 64 if state_path.parent.name == "first" else "2" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)

    first_worker = prewarm_tokenrhythm_install_id(config=first_config)
    second_worker = prewarm_tokenrhythm_install_id(config=second_config)
    assert first_worker is not None
    assert second_worker is not None
    first_worker.join(timeout=2)
    second_worker.join(timeout=2)

    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        config=first_config,
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "1" * 64}
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://api.tokenrhythm.studio/v1",
        config=second_config,
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "2" * 64}
    assert {path.parent.name for path in paths} == {"first", "second"}


def test_install_id_resolution_failure_retries_after_sixty_seconds(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    calls = 0

    def _ensure(*, config, state_path):
        del config, state_path
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("state unavailable")
        return "c" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)
    monkeypatch.setattr(tokenrhythm.time, "monotonic", lambda: now[0])
    state_path = tmp_path / "install_telemetry.json"

    first = prewarm_tokenrhythm_install_id(state_path=state_path)
    assert first is not None
    first.join(timeout=2)
    assert calls == 1

    now[0] = 159.999
    assert prewarm_tokenrhythm_install_id(state_path=state_path) is None
    assert calls == 1

    now[0] = 160.0
    second = prewarm_tokenrhythm_install_id(state_path=state_path)
    assert second is not None
    second.join(timeout=2)
    assert calls == 2
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        state_path=state_path,
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "c" * 64}


@pytest.mark.parametrize(
    "unsafe_install_id",
    [
        "",
        "a" * 129,
        "contains a space",
        "safe-prefix\r\nX-Injected: value",
        "192.0.2.42",
        "2001:db8::42",
        "aabbccddeeff",
        "aa:bb:cc:dd:ee:ff",
        "aa-bb-cc-dd-ee-ff",
        "aabb.ccdd.eeff",
    ],
)
def test_unsafe_install_id_is_omitted_without_rewriting_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_install_id: str,
) -> None:
    state_path = tmp_path / "install_telemetry.json"
    original = (
        '{"install_id": '
        + json.dumps(unsafe_install_id)
        + ', "install_id_source": "random-persisted", "custom": true}\n'
    ).encode()
    state_path.write_bytes(original)
    ensure_calls = 0

    def _ensure(*, config, state_path):
        del config, state_path
        nonlocal ensure_calls
        ensure_calls += 1
        return unsafe_install_id

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)

    worker = prewarm_tokenrhythm_install_id(state_path=state_path)
    assert worker is not None
    worker.join(timeout=2)

    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
        state_path=state_path,
    ) == {}
    assert ensure_calls == 0
    assert state_path.read_bytes() == original


def test_install_id_accepts_safe_value_at_header_length_limit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_id = "a" * 128
    monkeypatch.setattr(
        install_telemetry,
        "ensure_install_telemetry_id",
        lambda **_kwargs: install_id,
    )
    worker = prewarm_tokenrhythm_install_id(
        state_path=tmp_path / "install_telemetry.json"
    )
    assert worker is not None
    worker.join(timeout=2)

    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://api.tokenrhythm.studio/v1",
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: install_id}


def test_install_id_privacy_config_is_rechecked_for_every_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path / "state")
    monkeypatch.setattr(
        install_telemetry,
        "ensure_install_telemetry_id",
        lambda **_kwargs: "d" * 64,
    )
    worker = prewarm_tokenrhythm_install_id(config=config)
    assert worker is not None
    worker.join(timeout=2)

    assert TOKENRHYTHM_INSTALL_ID_HEADER in tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
    )
    config.privacy.disable_network_observability = True
    assert (
        tokenrhythm_install_id_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
        )
        == {}
    )
    config.privacy.disable_network_observability = False
    assert TOKENRHYTHM_INSTALL_ID_HEADER in tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
    )


@pytest.mark.parametrize(
    "disable_env",
    [
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        "OPENSQUILLA_TELEMETRY_DISABLED",
        "GITHUB_ACTIONS",
        "OPENSQUILLA_TESTING",
        "PYTEST_CURRENT_TEST",
    ],
)
def test_install_id_disable_environments_prevent_generation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    disable_env: str,
) -> None:
    calls = 0

    def _ensure(**_kwargs):
        nonlocal calls
        calls += 1
        return "e" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)
    monkeypatch.setenv(disable_env, "true")

    assert (
        prewarm_tokenrhythm_install_id(
            state_path=tmp_path / "install_telemetry.json"
        )
        is None
    )
    assert (
        tokenrhythm_install_id_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
        )
        == {}
    )
    assert calls == 0


def test_update_check_disable_does_not_suppress_install_id(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_UPDATE_CHECK_DISABLED", "true")
    monkeypatch.setattr(
        install_telemetry,
        "ensure_install_telemetry_id",
        lambda **_kwargs: "f" * 64,
    )

    worker = prewarm_tokenrhythm_install_id(
        state_path=tmp_path / "install_telemetry.json"
    )
    assert worker is not None
    worker.join(timeout=2)
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "f" * 64}


@pytest.mark.parametrize(
    ("provider_kind", "base_url"),
    [
        ("openrouter", "https://tokenrhythm.studio/v1"),
        ("tokenrhythm", "http://tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio.example/v1"),
        ("tokenrhythm", "https://user@tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio:444/v1"),
        ("tokenrhythm", "https://proxy.example/v1"),
    ],
)
def test_install_id_untrusted_targets_do_not_start_resolution(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    base_url: str,
) -> None:
    calls = 0

    def _ensure(**_kwargs):
        nonlocal calls
        calls += 1
        return "0" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)

    assert tokenrhythm_install_id_headers(provider_kind, base_url) == {}
    assert calls == 0


def test_install_id_rejects_explicit_proxy_before_and_after_prewarm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _ensure(**_kwargs):
        nonlocal calls
        calls += 1
        return "7" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)
    base_url = "https://tokenrhythm.studio/v1"
    proxy = "https://company-proxy.example"

    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        base_url,
        proxy=proxy,
    ) == {}
    assert calls == 0

    worker = prewarm_tokenrhythm_install_id(
        state_path=tmp_path / "install_telemetry.json"
    )
    assert worker is not None
    worker.join(timeout=2)
    assert calls == 1
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        base_url,
        proxy=proxy,
    ) == {}
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        base_url,
        proxy="  ",
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "7" * 64}


@pytest.mark.parametrize(
    "proxy_env",
    [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ],
)
def test_install_id_trusted_environment_proxy_does_not_start_resolution(
    monkeypatch: pytest.MonkeyPatch,
    proxy_env: str,
) -> None:
    calls = 0

    def _ensure(**_kwargs):
        nonlocal calls
        calls += 1
        return "8" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "true")
    monkeypatch.setenv(proxy_env, "http://127.0.0.1:3128")
    monkeypatch.setenv("NO_PROXY", "tokenrhythm.studio")

    assert (
        tokenrhythm_install_id_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
        )
        == {}
    )
    assert calls == 0


def test_install_id_ignores_environment_proxy_when_trust_env_is_off(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "false")
    monkeypatch.setattr(
        install_telemetry,
        "ensure_install_telemetry_id",
        lambda **_kwargs: "6" * 64,
    )
    worker = prewarm_tokenrhythm_install_id(
        state_path=tmp_path / "install_telemetry.json"
    )
    assert worker is not None
    worker.join(timeout=2)

    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "6" * 64}


def test_cached_short_install_id_is_redacted_from_diagnostic_text(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install_telemetry,
        "ensure_install_telemetry_id",
        lambda **_kwargs: "AbC",
    )
    worker = prewarm_tokenrhythm_install_id(
        state_path=tmp_path / "install_telemetry.json"
    )
    assert worker is not None
    worker.join(timeout=2)

    assert (
        redact_tokenrhythm_install_ids("before AbC after abc and ABC")
        == "before *** after *** and ***"
    )
    assert redact_tokenrhythm_install_ids("unrelated") == "unrelated"

    def _broken_validator(_value):
        raise RuntimeError("validator unavailable")

    monkeypatch.setattr(tokenrhythm, "_safe_install_id", _broken_validator)
    assert redact_tokenrhythm_install_ids("diagnostic text") == "***"


def test_install_id_cold_header_starts_default_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def _ensure(**_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return "9" * 64

    monkeypatch.setattr(install_telemetry, "ensure_install_telemetry_id", _ensure)

    assert (
        tokenrhythm_install_id_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
        )
        == {}
    )
    assert entered.wait(timeout=2)
    worker = prewarm_tokenrhythm_install_id()
    assert worker is not None
    release.set()
    worker.join(timeout=2)
    assert tokenrhythm_install_id_headers(
        "tokenrhythm",
        "https://tokenrhythm.studio/v1",
    ) == {TOKENRHYTHM_INSTALL_ID_HEADER: "9" * 64}


def test_install_id_helpers_fail_open_when_context_resolution_raises() -> None:
    class _BrokenConfig:
        @property
        def state_dir(self):
            raise RuntimeError("broken state path")

    config = _BrokenConfig()

    assert prewarm_tokenrhythm_install_id(config=config) is None
    assert (
        tokenrhythm_install_id_headers(
            "tokenrhythm",
            "https://tokenrhythm.studio/v1",
            config=config,
        )
        == {}
    )
