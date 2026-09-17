from __future__ import annotations

from opensquilla.engine.outcome import outcome_from_error


def test_max_iterations_is_partial_not_failed() -> None:
    outcome = outcome_from_error(code="max_iterations", message="hit cap")

    assert outcome.kind == "partial"
    assert outcome.reason == "max_iterations"
    assert outcome.error_class == "max_iterations"
    assert outcome.error_message == "hit cap"


def test_provider_request_budget_is_budget_limited_and_retryable() -> None:
    outcome = outcome_from_error(code="provider_request_too_large")

    assert outcome.kind == "budgetLimited"
    assert outcome.retryable is True


def test_unknown_error_remains_failed() -> None:
    outcome = outcome_from_error(code="boom")

    assert outcome.kind == "failed"
    assert outcome.retryable is False


def test_provider_failure_kind_is_preserved_in_durable_outcome() -> None:
    outcome = outcome_from_error(
        code="usage_limit_reached",
        message="provider quota exhausted",
        failure_kind="insufficient_credits",
    )

    assert outcome.to_dict()["failure_kind"] == "insufficient_credits"


def test_transient_provider_failure_kind_is_retryable() -> None:
    outcome = outcome_from_error(
        code="429",
        message="The model provider is temporarily rate limited.",
        failure_kind="rate_limited",
    )

    assert outcome.kind == "failed"
    assert outcome.retryable is True


def test_pretext_buffer_exhaustion_is_retryable() -> None:
    outcome = outcome_from_error(code="provider_pretext_buffer_exhausted")

    assert outcome.kind == "failed"
    assert outcome.retryable is True


def test_ensemble_multimodal_rejection_is_failed_and_not_retryable() -> None:
    outcome = outcome_from_error(
        code="ensemble_multimodal_unsupported",
        message="Switch to a single-model routing mode and try again.",
    )

    assert outcome.kind == "failed"
    assert outcome.reason == "ensemble_multimodal_unsupported"
    assert outcome.error_class == "ensemble_multimodal_unsupported"
    assert outcome.retryable is False
