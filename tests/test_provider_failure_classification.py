from __future__ import annotations

import pytest

from opensquilla.engine.fallback import FallbackPolicy
from opensquilla.provider.failures import (
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)
from opensquilla.provider.registry import list_provider_specs


@pytest.mark.parametrize(
    "provider",
    [
        "deepseek",
        "gemini",
        "dashscope",
        "bailian_coding",
        "bailian_coding_cn",
        "moonshot",
        "mistral",
        "groq",
        "zhipu",
        "siliconflow",
        "volcengine",
        "byteplus",
        "qianfan",
        "aihubmix",
        "minimax_openai",
        "vllm",
        "lm_studio",
        "ovms",
    ],
)
def test_openai_compatible_providers_share_common_failure_classification(provider: str) -> None:
    assert (
        classify_provider_error(provider, 401, message="invalid api key")
        is ProviderFailureKind.AUTH_INVALID
    )
    assert (
        classify_provider_error(provider, 429, message="rate limit exceeded")
        is ProviderFailureKind.RATE_LIMITED
    )
    assert (
        classify_provider_error(provider, 404, message="model not found")
        is ProviderFailureKind.MODEL_NOT_FOUND
    )
    assert (
        classify_provider_error(provider, 400, message="unsupported parameter")
        is ProviderFailureKind.UNSUPPORTED_FEATURE
    )


def test_every_registry_spec_has_a_handled_failure_family() -> None:
    handled = {"openai_compat", "anthropic", "ollama"}
    for spec in list_provider_specs():
        assert spec.failure_family in handled, (
            f"{spec.provider_id} declares failure_family "
            f"{spec.failure_family!r} with no branch in classify_provider_error"
        )


def test_openai_responses_uses_openai_compat_classification() -> None:
    # openai_responses (failure_family 'openai_compat') now routes through the
    # shared block rather than the generic tail.
    assert (
        classify_provider_error("openai_responses", 401, message="invalid api key")
        is ProviderFailureKind.AUTH_INVALID
    )


def test_unknown_provider_falls_through_to_generic_tail() -> None:
    # Unknown provider id -> no family -> generic tail still handles 429, but a
    # bare 401 is not promoted to AUTH_INVALID.
    assert (
        classify_provider_error("totally-unknown", 429, message="rate limit")
        is ProviderFailureKind.RATE_LIMITED
    )
    assert (
        classify_provider_error("totally-unknown", 401, message="")
        is ProviderFailureKind.UNKNOWN
    )


@pytest.mark.parametrize("provider", ["minimax", "minimax_cn", "minimax_global"])
def test_minimax_region_profiles_use_anthropic_failure_classification(provider: str) -> None:
    assert (
        classify_provider_error(provider, 401, raw_code="authentication_error")
        is ProviderFailureKind.AUTH_INVALID
    )


@pytest.mark.parametrize(
    "message",
    [
        "Request error: connection reset by peer",
        "Request error: All connection attempts failed",
        "ReadTimeout while contacting provider",
        "ConnectTimeout while contacting provider",
    ],
)
def test_agent_fallback_retries_transport_transient_errors(message: str) -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error(message)

    assert kind is ProviderFailureKind.TRANSPORT_TRANSIENT
    assert policy.should_retry(kind, attempt=0) is True


def test_agent_fallback_retries_timeout_code_when_message_is_sparse() -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error(
        "Request timed out: ",
        provider_name="openrouter",
        raw_code="timeout",
    )

    assert kind is ProviderFailureKind.TRANSPORT_TRANSIENT
    assert policy.should_retry(kind, attempt=0) is True


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 520: upstream provider returned an unknown error",
        "HTTP 522",
        "HTTP 523",
        "HTTP 524",
        "HTTP 504",
        "status_code: 523",
    ],
)
def test_agent_fallback_retries_gateway_transient_http_errors(message: str) -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error(message)

    assert kind is ProviderFailureKind.PROVIDER_OVERLOADED
    assert policy.should_retry(kind, attempt=0) is True


@pytest.mark.parametrize(
    "message",
    [
        "Cloudflare returned 520",
        "upstream returned 522",
        "OpenRouter upstream error 520",
        "provider backend returned 524",
        "524 from backend failure",
    ],
)
def test_agent_fallback_retries_gateway_context_transient_codes(message: str) -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error(message)

    assert kind is ProviderFailureKind.PROVIDER_OVERLOADED
    assert policy.should_retry(kind, attempt=0) is True


@pytest.mark.parametrize(
    "message",
    [
        "random value 520",
        "line 520",
        "520 tokens",
        "issue 520",
        "the provider sent 520 tokens",
        "edge case 520",
        "proxy line 520",
        "gateway request id 520",
        "openrouter model id 520",
        "upstream metadata 520",
        "Cloudflare is configured. " + ("x" * 120) + " value 520",
    ],
)
def test_agent_fallback_does_not_retry_unscoped_gateway_numbers(message: str) -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error(message)

    assert kind is ProviderFailureKind.UNKNOWN
    assert policy.should_retry(kind, attempt=0) is False


@pytest.mark.parametrize("provider", ["openrouter", "deepseek"])
@pytest.mark.parametrize(
    "message",
    [
        "Cloudflare returned 520",
        "upstream returned 522",
        "OpenRouter upstream error 520",
        "provider backend returned 524",
        "HTTP 523",
    ],
)
def test_provider_failure_classifies_gateway_transient_errors(
    provider: str, message: str
) -> None:
    assert (
        classify_provider_error(provider, None, message=message)
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize(
    "message",
    [
        "random value 520",
        "line 520",
        "520 tokens",
        "the provider sent 520 tokens",
        "edge case 520",
        "proxy line 520",
        "gateway request id 520",
        "openrouter model id 520",
        "upstream metadata 520",
        "Cloudflare is configured. " + ("x" * 120) + " value 520",
    ],
)
def test_provider_failure_does_not_classify_unscoped_gateway_numbers(message: str) -> None:
    assert (
        classify_provider_error("openrouter", None, message=message)
        is ProviderFailureKind.UNKNOWN
    )


def test_agent_fallback_still_does_not_retry_auth_failures() -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error("invalid api key")

    assert kind is ProviderFailureKind.AUTH_INVALID
    assert policy.should_retry(kind, attempt=0) is False


def test_anthropic_model_not_found_classifies_for_fallback() -> None:
    # An Anthropic 404 previously fell through to UNKNOWN and surfaced raw.
    kind = classify_provider_error(
        "anthropic",
        404,
        message='{"type":"not_found_error","message":"model: claude-nope"}',
    )
    assert kind is ProviderFailureKind.MODEL_NOT_FOUND
    assert decide_recovery_action(kind) is ProviderRecoveryAction.FALLBACK_PROVIDER


def test_anthropic_low_credit_balance_classifies_as_insufficient_credits() -> None:
    kind = classify_provider_error(
        "anthropic",
        400,
        message="Your credit balance is too low to access the Anthropic API.",
    )
    assert kind is ProviderFailureKind.INSUFFICIENT_CREDITS


def test_ollama_auth_errors_classify_as_auth_invalid() -> None:
    # Ollama Cloud / secured remote hosts return standard auth statuses.
    kind = classify_provider_error("ollama", 401, message="HTTP 401: unauthorized")
    assert kind is ProviderFailureKind.AUTH_INVALID
    assert decide_recovery_action(kind) is ProviderRecoveryAction.FAIL_CONFIG


@pytest.mark.parametrize("provider", ["openrouter", "openai"])
def test_openai_compatible_region_unavailable_403_falls_back_to_next_model(
    provider: str,
) -> None:
    kind = classify_provider_error(
        provider,
        403,
        message="HTTP 403: This model is not available in your region.",
    )

    assert kind is ProviderFailureKind.MODEL_NOT_FOUND
    assert decide_recovery_action(kind) is ProviderRecoveryAction.FALLBACK_PROVIDER


def test_openrouter_plain_403_still_fails_as_auth_configuration() -> None:
    kind = classify_provider_error("openrouter", 403, message="HTTP 403: forbidden")

    assert kind is ProviderFailureKind.AUTH_INVALID
    assert decide_recovery_action(kind) is ProviderRecoveryAction.FAIL_CONFIG


@pytest.mark.parametrize(
    ("raw_code", "message"),
    [
        ("empty_response", ""),
        ("", "Provider returned an empty response"),
        ("", "empty_response"),
    ],
)
def test_provider_failure_classifies_empty_responses(
    raw_code: str, message: str
) -> None:
    assert (
        classify_provider_error("openrouter", None, raw_code=raw_code, message=message)
        is ProviderFailureKind.EMPTY_RESPONSE
    )


def test_provider_failure_keeps_empty_http_gateway_body_transient() -> None:
    assert (
        classify_provider_error(
            "openai",
            500,
            message="OpenAI chat request failed (HTTP 500): empty response body",
        )
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


def test_agent_fallback_identifies_but_does_not_retry_empty_responses() -> None:
    policy = FallbackPolicy(max_retries=2)

    kind = policy.classify_error("Provider returned an empty response")

    assert kind is ProviderFailureKind.EMPTY_RESPONSE
    assert policy.should_retry(kind, attempt=0) is False


@pytest.mark.parametrize(
    "status_code",
    [499, 500, 521, 529],
)
def test_new_transient_status_codes_classify_as_provider_overloaded(
    status_code: int,
) -> None:
    assert (
        classify_provider_error("openrouter", status_code, message="")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )
    assert (
        classify_provider_error("anthropic", status_code, message="")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize("status_code", [520, 521])
def test_anthropic_gateway_status_codes_use_canonical_transient_set(
    status_code: int,
) -> None:
    assert (
        classify_provider_error("anthropic", status_code, message="")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


def test_anthropic_gateway_status_codes_match_via_text() -> None:
    assert (
        classify_provider_error("anthropic", None, message="HTTP 520")
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 499",
        "HTTP 500",
        "HTTP 502",
        "HTTP 503",
        "HTTP 521",
        "HTTP 529",
        "status_code: 500",
        "error code 521",
    ],
)
def test_new_transient_status_codes_match_via_text(message: str) -> None:
    assert (
        classify_provider_error("openrouter", None, message=message)
        is ProviderFailureKind.PROVIDER_OVERLOADED
    )
