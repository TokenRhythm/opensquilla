"""ProviderHealthLedger: cooldown benching for unhealthy deployments.

Covers the pinned D13 defaults (3 strikes -> 30s bench, immediate 429 bench,
Retry-After override), the never-strand single-deployment exemption, strike
reset on success, monotonic-clock parking, log hygiene, Retry-After parsing,
and the opt-in selector-fallback consult point.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import structlog

from opensquilla.engine.routing.health import (
    BENCHABLE_FAILURE_KINDS,
    DEFAULT_COOLDOWN_S,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_MAX_COOLDOWN_S,
    ProviderHealthLedger,
    get_provider_health_ledger,
)
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.provider.failures import (
    ProviderFailureKind,
    parse_retry_after,
    retry_after_from_headers,
)
from opensquilla.provider.selector import (
    ModelSelector,
    ProviderConfig,
    SelectorConfig,
)
from opensquilla.provider.types import ModelCapabilities

PROVIDER = "openrouter"
MODEL = "test-chat-model"


class FakeClock:
    """Injectable monotonic clock stand-in."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _ledger(clock: FakeClock | None = None) -> ProviderHealthLedger:
    return ProviderHealthLedger(clock=clock or FakeClock())


# ---------------------------------------------------------------------------
# Strike threshold and immediate 429 bench
# ---------------------------------------------------------------------------


def test_three_failures_bench_deployment() -> None:
    ledger = _ledger()
    for _ in range(DEFAULT_FAILURE_THRESHOLD - 1):
        assert not ledger.record_failure(
            PROVIDER, MODEL, ProviderFailureKind.TRANSPORT_TRANSIENT
        )
        assert not ledger.is_benched(PROVIDER, MODEL)
    assert ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.TRANSPORT_TRANSIENT)
    assert ledger.is_benched(PROVIDER, MODEL)


def test_rate_limited_benches_immediately() -> None:
    ledger = _ledger()
    assert ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    assert ledger.is_benched(PROVIDER, MODEL)


def test_non_benchable_kinds_never_bench() -> None:
    ledger = _ledger()
    for kind in (
        ProviderFailureKind.CONTEXT_OVERFLOW,
        ProviderFailureKind.BAD_REQUEST,
        ProviderFailureKind.AUTH_INVALID,
        ProviderFailureKind.MODEL_NOT_FOUND,
        ProviderFailureKind.UNKNOWN,
    ):
        assert kind not in BENCHABLE_FAILURE_KINDS
        for _ in range(DEFAULT_FAILURE_THRESHOLD + 2):
            assert not ledger.record_failure(PROVIDER, MODEL, kind)
    assert not ledger.is_benched(PROVIDER, MODEL)


def test_benches_are_per_deployment() -> None:
    ledger = _ledger()
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    assert ledger.is_benched(PROVIDER, MODEL)
    assert not ledger.is_benched(PROVIDER, "other-model")
    assert not ledger.is_benched("anthropic", MODEL)


# ---------------------------------------------------------------------------
# Cooldown expiry (injected clock)
# ---------------------------------------------------------------------------


def test_default_cooldown_expires_after_thirty_seconds() -> None:
    clock = FakeClock()
    ledger = _ledger(clock)
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    clock.advance(DEFAULT_COOLDOWN_S - 0.1)
    assert ledger.is_benched(PROVIDER, MODEL)
    clock.advance(0.2)
    assert not ledger.is_benched(PROVIDER, MODEL)


def test_strikes_reset_when_bench_triggers() -> None:
    """After a bench expires the deployment starts from a clean slate."""
    clock = FakeClock()
    ledger = _ledger(clock)
    for _ in range(DEFAULT_FAILURE_THRESHOLD):
        ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.PROVIDER_OVERLOADED)
    clock.advance(DEFAULT_COOLDOWN_S + 1)
    assert not ledger.is_benched(PROVIDER, MODEL)
    # One post-cooldown failure must not instantly re-bench.
    assert not ledger.record_failure(
        PROVIDER, MODEL, ProviderFailureKind.PROVIDER_OVERLOADED
    )


# ---------------------------------------------------------------------------
# Retry-After honoring
# ---------------------------------------------------------------------------


def test_retry_after_overrides_default_cooldown_on_429() -> None:
    clock = FakeClock()
    ledger = _ledger(clock)
    ledger.record_failure(
        PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED, retry_after_s=90.0
    )
    clock.advance(DEFAULT_COOLDOWN_S + 10)  # past the default, inside Retry-After
    assert ledger.is_benched(PROVIDER, MODEL)
    clock.advance(90.0 - (DEFAULT_COOLDOWN_S + 10) + 0.1)
    assert not ledger.is_benched(PROVIDER, MODEL)


def test_retry_after_shorter_than_default_is_honored() -> None:
    clock = FakeClock()
    ledger = _ledger(clock)
    ledger.record_failure(
        PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED, retry_after_s=5.0
    )
    clock.advance(5.1)
    assert not ledger.is_benched(PROVIDER, MODEL)


def test_retry_after_honored_on_overloaded_threshold_bench() -> None:
    clock = FakeClock()
    ledger = _ledger(clock)
    for _ in range(DEFAULT_FAILURE_THRESHOLD - 1):
        ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.PROVIDER_OVERLOADED)
    ledger.record_failure(
        PROVIDER, MODEL, ProviderFailureKind.PROVIDER_OVERLOADED, retry_after_s=120.0
    )
    clock.advance(100.0)
    assert ledger.is_benched(PROVIDER, MODEL)
    clock.advance(20.1)
    assert not ledger.is_benched(PROVIDER, MODEL)


def test_retry_after_is_clamped_to_max_cooldown() -> None:
    clock = FakeClock()
    ledger = _ledger(clock)
    ledger.record_failure(
        PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED, retry_after_s=10_000.0
    )
    clock.advance(DEFAULT_MAX_COOLDOWN_S - 1)
    assert ledger.is_benched(PROVIDER, MODEL)
    clock.advance(1.1)
    assert not ledger.is_benched(PROVIDER, MODEL)


# ---------------------------------------------------------------------------
# Single-deployment exemption: NEVER bench the only viable deployment
# ---------------------------------------------------------------------------


def test_only_deployment_is_never_reported_benched() -> None:
    ledger = _ledger()
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    assert ledger.is_benched(PROVIDER, MODEL)
    # Sole candidate: the bench must not strand routing.
    assert ledger.eligible(PROVIDER, MODEL, [(PROVIDER, MODEL)])
    # Empty candidate set degenerates the same way.
    assert ledger.eligible(PROVIDER, MODEL, [])


def test_benched_deployment_ineligible_when_alternative_is_healthy() -> None:
    ledger = _ledger()
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    candidates = [(PROVIDER, MODEL), (PROVIDER, "backup-model")]
    assert not ledger.eligible(PROVIDER, MODEL, candidates)
    assert ledger.eligible(PROVIDER, "backup-model", candidates)


def test_all_candidates_benched_degenerates_to_everyone_eligible() -> None:
    ledger = _ledger()
    candidates = [(PROVIDER, MODEL), (PROVIDER, "backup-model")]
    for provider, model in candidates:
        ledger.record_failure(provider, model, ProviderFailureKind.RATE_LIMITED)
        assert ledger.is_benched(provider, model)
    for provider, model in candidates:
        assert ledger.eligible(provider, model, candidates)


def test_unbenched_deployment_is_always_eligible() -> None:
    ledger = _ledger()
    assert ledger.eligible(PROVIDER, MODEL, [(PROVIDER, MODEL)])


# ---------------------------------------------------------------------------
# record_success resets strikes
# ---------------------------------------------------------------------------


def test_success_resets_strike_count() -> None:
    ledger = _ledger()
    for _ in range(DEFAULT_FAILURE_THRESHOLD - 1):
        ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.TRANSPORT_TRANSIENT)
    ledger.record_success(PROVIDER, MODEL)
    for _ in range(DEFAULT_FAILURE_THRESHOLD - 1):
        assert not ledger.record_failure(
            PROVIDER, MODEL, ProviderFailureKind.TRANSPORT_TRANSIENT
        )
    assert not ledger.is_benched(PROVIDER, MODEL)
    assert ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.TRANSPORT_TRANSIENT)


def test_success_clears_an_active_bench() -> None:
    ledger = _ledger()
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    assert ledger.is_benched(PROVIDER, MODEL)
    ledger.record_success(PROVIDER, MODEL)
    assert not ledger.is_benched(PROVIDER, MODEL)


# ---------------------------------------------------------------------------
# Monotonic parking: wall-clock drift cannot corrupt bench state
# ---------------------------------------------------------------------------


def test_default_clock_is_monotonic() -> None:
    assert ProviderHealthLedger()._clock is time.monotonic


def test_wall_clock_drift_cannot_unbench(monkeypatch) -> None:
    clock = FakeClock()
    ledger = _ledger(clock)
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    # Yank the wall clock a million seconds forward and backward: bench state
    # is keyed to the injected monotonic clock, which has not moved.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 1_000_000)
    assert ledger.is_benched(PROVIDER, MODEL)
    monkeypatch.setattr(time, "time", lambda: 0.0)
    assert ledger.is_benched(PROVIDER, MODEL)
    clock.advance(DEFAULT_COOLDOWN_S + 0.1)
    assert not ledger.is_benched(PROVIDER, MODEL)


# ---------------------------------------------------------------------------
# Log hygiene: no secrets, no raw provider error text
# ---------------------------------------------------------------------------


@contextmanager
def _capture_ledger_logs():
    """Capture at NOTSET so a leaked filtering config can't hide info events."""
    old_config = structlog.get_config()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.NOTSET))
    try:
        with structlog.testing.capture_logs() as captured:
            yield captured
    finally:
        structlog.configure(**old_config)


def test_bench_log_carries_only_structured_fields() -> None:
    ledger = _ledger()
    with _capture_ledger_logs() as captured:
        ledger.record_failure(
            PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED, retry_after_s=5.0
        )
    benched = [e for e in captured if e["event"] == "provider_health.benched"]
    assert len(benched) == 1
    allowed_keys = {
        "event",
        "log_level",
        "provider",
        "model",
        "kind",
        "cooldown_s",
        "strikes",
        "immediate",
    }
    assert set(benched[0]) <= allowed_keys
    # The kind is the enum token, never free provider error text.
    assert benched[0]["kind"] == ProviderFailureKind.RATE_LIMITED.value
    assert "message" not in benched[0]


def test_unbench_logs_are_structured_only() -> None:
    ledger = _ledger()
    ledger.record_failure(PROVIDER, MODEL, ProviderFailureKind.RATE_LIMITED)
    with _capture_ledger_logs() as captured:
        ledger.record_success(PROVIDER, MODEL)
    unbenched = [e for e in captured if e["event"] == "provider_health.unbenched"]
    assert len(unbenched) == 1
    assert set(unbenched[0]) <= {"event", "log_level", "provider", "model", "reason"}
    assert unbenched[0]["reason"] == "success"


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------


def test_parse_retry_after_integer_seconds() -> None:
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 7 ") == 7.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("1.5") == 1.5


def test_parse_retry_after_rejects_garbage() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after("nan") is None


def test_parse_retry_after_http_date() -> None:
    now_utc = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
    future = format_datetime(now_utc + timedelta(seconds=90), usegmt=True)
    parsed = parse_retry_after(future, now_utc=now_utc)
    assert parsed == 90.0
    past = format_datetime(now_utc - timedelta(seconds=90), usegmt=True)
    assert parse_retry_after(past, now_utc=now_utc) == 0.0


def test_retry_after_from_headers_is_status_guarded() -> None:
    headers = {"retry-after": "7"}
    assert retry_after_from_headers(429, headers) == 7.0
    assert retry_after_from_headers(503, headers) == 7.0
    assert retry_after_from_headers(404, headers) is None
    assert retry_after_from_headers(200, headers) is None
    assert retry_after_from_headers(429, {}) is None


# ---------------------------------------------------------------------------
# Shared-instance accessor and opt-in consult point
# ---------------------------------------------------------------------------


def test_shared_ledger_accessor_returns_one_instance() -> None:
    assert get_provider_health_ledger() is get_provider_health_ledger()


def _three_model_selector() -> ModelSelector:
    return ModelSelector(
        SelectorConfig(
            primary=ProviderConfig("ollama", "model-a"),
            fallbacks=[
                ProviderConfig("ollama", "model-b"),
                ProviderConfig("ollama", "model-c"),
            ],
        )
    )


def test_selector_fallback_wrapper_skips_benched_fallback() -> None:
    selector = _three_model_selector()
    provider = selector.resolve()
    provider = selector.next_fallback()  # failover landed on model-b
    ledger = _ledger()
    wrapper = _SelectorFallbackProvider(provider, selector, health_ledger=ledger)
    ledger.record_failure("ollama", "model-b", ProviderFailureKind.RATE_LIMITED)
    wrapper._skip_benched_fallbacks()
    assert selector.current_config.model == "model-c"


def test_selector_fallback_wrapper_keeps_last_deployment_despite_bench() -> None:
    selector = _three_model_selector()
    provider = selector.resolve()
    provider = selector.next_fallback()  # failover landed on model-b
    ledger = _ledger()
    wrapper = _SelectorFallbackProvider(provider, selector, health_ledger=ledger)
    ledger.record_failure("ollama", "model-b", ProviderFailureKind.RATE_LIMITED)
    ledger.record_failure("ollama", "model-c", ProviderFailureKind.RATE_LIMITED)
    wrapper._skip_benched_fallbacks()
    # Every remaining deployment is benched: the exemption keeps the current
    # link instead of stranding the turn.
    assert selector.current_config.model == "model-b"


def test_selector_fallback_wrapper_without_ledger_is_a_noop() -> None:
    selector = _three_model_selector()
    provider = selector.resolve()
    provider = selector.next_fallback()
    wrapper = _SelectorFallbackProvider(provider, selector)
    wrapper._skip_benched_fallbacks()
    assert selector.current_config.model == "model-b"


def test_tool_filter_skips_denial_then_benched_compatible_fallback() -> None:
    primary = ProviderConfig("ollama", "model-a")
    denied = ProviderConfig("ollama", "model-b")
    benched = ProviderConfig("ollama", "model-c")
    healthy = ProviderConfig("ollama", "model-d")
    selector = ModelSelector(
        SelectorConfig(
            primary=primary,
            fallbacks=[denied, benched, healthy],
        )
    )
    selector.resolve()
    provider = selector.next_fallback()
    ledger = _ledger()
    ledger.record_failure("ollama", "model-c", ProviderFailureKind.RATE_LIMITED)
    wrapper = _SelectorFallbackProvider(provider, selector, health_ledger=ledger)
    wrapper.configure_fallback_deployment_limits(
        [
            (denied, 0, 0, ModelCapabilities(supports_tools=False)),
            (benched, 0, 0, ModelCapabilities(supports_tools=True)),
            (healthy, 0, 0, ModelCapabilities(supports_tools=True)),
        ]
    )

    assert wrapper._advance_past_explicit_tool_denials() is True
    assert selector.current_config is healthy
    assert wrapper._pending_fallback_hops == 2


def test_tool_filter_keeps_only_compatible_candidate_despite_bench() -> None:
    primary = ProviderConfig("ollama", "model-a")
    compatible = ProviderConfig("ollama", "model-b")
    denied = ProviderConfig("ollama", "model-c")
    selector = ModelSelector(
        SelectorConfig(
            primary=primary,
            fallbacks=[compatible, denied],
        )
    )
    selector.resolve()
    provider = selector.next_fallback()
    ledger = _ledger()
    ledger.record_failure("ollama", "model-b", ProviderFailureKind.RATE_LIMITED)
    wrapper = _SelectorFallbackProvider(provider, selector, health_ledger=ledger)
    wrapper.configure_fallback_deployment_limits(
        [
            (compatible, 0, 0, ModelCapabilities(supports_tools=True)),
            (denied, 0, 0, ModelCapabilities(supports_tools=False)),
        ]
    )

    assert wrapper._advance_past_explicit_tool_denials() is True
    assert selector.current_config is compatible


def test_capacity_filter_keeps_only_compatible_candidate_despite_bench() -> None:
    primary = ProviderConfig("ollama", "model-a")
    larger = ProviderConfig("ollama", "model-b")
    too_small = ProviderConfig("ollama", "model-c")
    selector = ModelSelector(
        SelectorConfig(
            primary=primary,
            fallbacks=[larger, too_small],
        )
    )
    selector.resolve()
    provider = selector.next_fallback()
    ledger = _ledger()
    ledger.record_failure("ollama", "model-b", ProviderFailureKind.RATE_LIMITED)
    wrapper = _SelectorFallbackProvider(provider, selector, health_ledger=ledger)

    assert wrapper._advance_past_explicit_tool_denials(
        candidate_predicate=lambda candidate: candidate is larger
    ) is True
    assert selector.current_config is larger


def test_selector_remaining_chain_lists_active_and_untried() -> None:
    selector = _three_model_selector()
    assert [cfg.model for cfg in selector.remaining_chain()] == [
        "model-a",
        "model-b",
        "model-c",
    ]
    selector.next_fallback()
    assert [cfg.model for cfg in selector.remaining_chain()] == ["model-b", "model-c"]
