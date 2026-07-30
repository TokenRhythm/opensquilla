from __future__ import annotations

import pytest

from opensquilla.provider.tokenrhythm_correlation import (
    TOKENRHYTHM_CALL_KIND_HEADER,
    TOKENRHYTHM_EXECUTION_ID_HEADER,
    TOKENRHYTHM_SESSION_ID_HEADER,
    TOKENRHYTHM_TURN_ID_HEADER,
    is_tokenrhythm_correlation_target,
    tokenrhythm_correlation_headers,
)
from opensquilla.provider.types import (
    ChatConfig,
    ProviderRequestCorrelation,
    derive_provider_request_correlation,
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
        ("tokenrhythm", "https://tokenrhythm.studio.example.com/v1"),
        ("tokenrhythm", "https://eviltokenrhythm.studio/v1"),
        ("tokenrhythm", "https://user@tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://@tokenrhythm.studio/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio:8443/v1"),
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
