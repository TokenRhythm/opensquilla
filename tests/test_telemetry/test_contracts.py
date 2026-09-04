from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid1, uuid4

import pytest
from pydantic import ValidationError

import opensquilla.telemetry.contracts as telemetry_contracts
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
)
from opensquilla.telemetry.contracts import (
    CURRENT_NOTICE_VERSION_BY_SCOPE,
    EVENT_MODELS,
    GROWTH_BATCH_ADAPTER,
    MAX_GROWTH_BATCH_BYTES,
    MAX_RELIABILITY_BATCH_BYTES,
    MAX_TELEMETRY_NESTING_DEPTH,
    RELIABILITY_BATCH_ADAPTER,
    TELEMETRY_EVENT_ADAPTER,
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    TELEMETRY_PROTOCOL_MANIFEST_JSON,
    AppCrashDetected,
    AppStartResult,
    ClientLaunch,
    DownloadClick,
    DownloadServed,
    FileParseResult,
    FirstAppReady,
    FirstTurnStarted,
    FirstTurnSucceeded,
    GatewayStartResult,
    InstallResult,
    InstallStarted,
    LandingView,
    OnboardingCompleted,
    PerformanceSummary,
    RegistrationResult,
    RegistrationStarted,
    TelemetryWireError,
    TelemetryWireErrorCode,
    TelemetryWireTarget,
    ToolCallResult,
    TurnResult,
    TurnResultV2,
    TurnResultV3,
    UpdateResult,
    canonical_json,
    canonical_json_bytes,
    parse_telemetry_wire,
    telemetry_protocol_manifest,
)
from opensquilla.telemetry.ids import (
    is_uuid4,
    new_analytics_user_id,
    new_app_session_id,
    new_batch_id,
    new_event_id,
)
from opensquilla.telemetry.privacy import (
    ForbiddenTelemetryFieldError,
    assert_no_forbidden_fields,
    is_forbidden_field_name,
)

EVENT_ID = "00000000-0000-4000-8000-000000000001"
SECOND_EVENT_ID = "00000000-0000-4000-8000-000000000002"
APP_SESSION_ID = "00000000-0000-4000-8000-000000000003"
ANALYTICS_USER_ID = "00000000-0000-4000-8000-000000000004"
BATCH_ID = "00000000-0000-4000-8000-000000000005"
ACQUISITION_ID = "00000000-0000-4000-8000-000000000006"
OCCURRED_AT = "2026-09-01T01:02:03.456Z"
SENT_AT = "2026-09-01T01:03:00.000Z"


def _wire_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _reliability_base(event_name: str) -> dict[str, object]:
    return {
        "event_name": event_name,
        "event_version": 1,
        "event_id": EVENT_ID,
        "occurred_at_utc": OCCURRED_AT,
        "source": "runtime",
        "app_version": "1.2.3-rc.1",
        "platform": "macos",
        "outcome": "success",
        "error_code": None,
        "duration_ms": 120,
        "consent_scope": "reliability",
        "notice_version": "reliability-v1",
        "sample_rate": 1.0,
        "app_session_id": APP_SESSION_ID,
    }


def _growth_base(event_name: str) -> dict[str, object]:
    return {
        "event_name": event_name,
        "event_version": 1,
        "event_id": EVENT_ID,
        "occurred_at_utc": OCCURRED_AT,
        "source": "desktop",
        "app_version": "1.2.3-rc.1",
        "platform": "windows",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": "growth-v1",
        "sample_rate": 1,
        "analytics_user_id": ANALYTICS_USER_ID,
    }


def _acquisition_base(
    event_name: str,
    *,
    source: str,
    app_version: str | None,
) -> dict[str, object]:
    return {
        "event_name": event_name,
        "event_version": 1,
        "event_id": EVENT_ID,
        "occurred_at_utc": OCCURRED_AT,
        "source": source,
        "app_version": app_version,
        "platform": "windows",
        "outcome": None,
        "error_code": None,
        "duration_ms": None,
        "consent_scope": "growth",
        "notice_version": "growth-v1",
        "sample_rate": 1,
        "acquisition_id": ACQUISITION_ID,
    }


def _valid_reliability_payloads() -> list[tuple[type[object], dict[str, object]]]:
    app_start = _reliability_base("app_start_result")
    app_start.update(source="desktop", failure_stage=None)

    gateway_start = _reliability_base("gateway_start_result")
    gateway_start.update(
        source="desktop",
        failure_stage=None,
        startup_mode="spawned",
    )

    crash = _reliability_base("app_crash_detected")
    crash.update(
        source="desktop",
        outcome="detected",
        error_code="stale_session_marker",
        duration_ms=None,
        component="desktop_main",
        error_fingerprint="a" * 64,
        runtime_ms=5_000,
    )

    turn = _reliability_base("turn_result")
    turn.update(
        source="gateway",
        ttft_ms=40,
        stall_count=0,
        stall_threshold_ms=15_000,
    )

    tool = _reliability_base("tool_call_result")
    tool.update(
        source="runtime",
        tool_category="filesystem_read",
        retry_count=0,
    )

    file_parse = _reliability_base("file_parse_result")
    file_parse.update(
        source="runtime",
        file_type="pdf",
        size_bucket="100_kib_1_mib",
    )

    update = _reliability_base("update_result")
    update.update(
        source="updater",
        update_stage="check",
        old_version="1.2.3",
        new_version=None,
        result="not_available",
    )

    performance = _reliability_base("performance_summary")
    performance.update(
        source="desktop",
        duration_ms=1_000,
        summary_kind="session_end",
        coverage="complete",
        turn_count=4,
        stalled_turn_count=1,
        stall_count=2,
        stall_threshold_ms=15_000,
        monitored_request_count=5,
        slow_request_count=1,
        slow_request_threshold_ms=30_000,
        foreground_duration_ms=600,
        background_duration_ms=300,
    )

    return [
        (AppStartResult, app_start),
        (GatewayStartResult, gateway_start),
        (AppCrashDetected, crash),
        (TurnResult, turn),
        (ToolCallResult, tool),
        (FileParseResult, file_parse),
        (UpdateResult, update),
        (PerformanceSummary, performance),
    ]


def _valid_growth_payloads() -> list[tuple[type[object], dict[str, object]]]:
    onboarding = _growth_base("onboarding_result")
    onboarding.update(outcome="completed", flow_version=1)

    app_ready = _growth_base("first_app_ready")

    turn_started = _growth_base("first_turn_started")
    turn_started.update(source="gateway")

    turn_succeeded = _growth_base("first_turn_result")
    turn_succeeded.update(source="runtime", outcome="success")

    landing_view = _acquisition_base(
        "landing_view",
        source="website",
        app_version=None,
    )

    download_click = _acquisition_base(
        "download_click",
        source="website",
        app_version=None,
    )

    download_served = _acquisition_base(
        "download_served",
        source="cdn",
        app_version="1.2.3-rc.1",
    )
    download_served.update(outcome="success")

    install_started = _acquisition_base(
        "install_started",
        source="installer",
        app_version="1.2.3-rc.1",
    )

    install_result = _acquisition_base(
        "install_result",
        source="installer",
        app_version="1.2.3-rc.1",
    )
    install_result.update(outcome="success", duration_ms=2_000)

    registration_started = _acquisition_base(
        "registration_started",
        source="desktop",
        app_version="1.2.3-rc.1",
    )

    registration_result = _acquisition_base(
        "registration_result",
        source="account_service",
        app_version=None,
    )
    registration_result.update(
        outcome="success",
        duration_ms=350,
        analytics_user_id=ANALYTICS_USER_ID,
    )

    client_launch = _growth_base("client_launch")
    client_launch.update(
        source="gateway",
        surface="tui",
        entrypoint="chat",
        execution_mode="gateway",
    )

    return [
        (OnboardingCompleted, onboarding),
        (FirstAppReady, app_ready),
        (FirstTurnStarted, turn_started),
        (FirstTurnSucceeded, turn_succeeded),
        (LandingView, landing_view),
        (DownloadClick, download_click),
        (DownloadServed, download_served),
        (InstallStarted, install_started),
        (InstallResult, install_result),
        (RegistrationStarted, registration_started),
        (RegistrationResult, registration_result),
        (ClientLaunch, client_launch),
    ]


def _reliability_batch_payload() -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": BATCH_ID,
        "sent_at_utc": SENT_AT,
        "events": [_valid_reliability_payloads()[3][1]],
    }


def _growth_batch_payload() -> dict[str, object]:
    return {
        "batch_version": 1,
        "batch_id": BATCH_ID,
        "sent_at_utc": SENT_AT,
        "events": [_valid_growth_payloads()[1][1]],
    }


@pytest.mark.parametrize(("expected_type", "payload"), _valid_reliability_payloads())
def test_reliability_discriminated_union_accepts_all_eight_events(
    expected_type: type[object], payload: dict[str, object]
) -> None:
    event = TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)

    assert type(event) is expected_type
    assert event.model_dump(mode="json", exclude_none=False) == payload


@pytest.mark.parametrize(("expected_type", "payload"), _valid_growth_payloads())
def test_growth_discriminated_union_accepts_all_authoritative_events(
    expected_type: type[object], payload: dict[str, object]
) -> None:
    event = TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)

    assert type(event) is expected_type
    dumped = event.model_dump(mode="json", exclude_none=False)
    assert dumped == payload
    assert dumped["sample_rate"] == 1


def test_event_registry_is_keyed_by_name_and_version() -> None:
    expected_names = {
        payload["event_name"]
        for _, payload in [*_valid_reliability_payloads(), *_valid_growth_payloads()]
    }

    assert set(EVENT_MODELS) == {
        *((name, 1) for name in expected_names),
        ("turn_result", 2),
        ("turn_result", 3),
        ("tool_call_result", 2),
        ("file_parse_result", 2),
    }


def test_turn_v2_adds_only_closed_runtime_dimensions() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload.update(event_version=2, surface="cli", execution_mode="one_shot")

    event = TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)

    assert type(event) is TurnResultV2
    assert event.model_dump(mode="json", exclude_none=False) == payload


def test_turn_v3_requires_failure_stage_only_for_non_success() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload.update(event_version=3, failure_stage=None, surface=None, execution_mode=None)

    success = TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)

    assert type(success) is TurnResultV3
    failed = dict(payload)
    failed.update(
        outcome="fail",
        error_code="provider_unavailable",
        failure_stage="agent_execution",
        surface="tui",
        execution_mode="gateway",
    )
    event = TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(failed), strict=True)
    assert type(event) is TurnResultV3

    invalid = dict(failed)
    invalid["failure_stage"] = None
    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(invalid), strict=True)


@pytest.mark.parametrize(("_expected_type", "payload"), _valid_growth_payloads())
def test_every_growth_event_rejects_future_versions(
    _expected_type: type[object],
    payload: dict[str, object],
) -> None:
    candidate = dict(payload)
    candidate["event_version"] = 2

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(candidate), strict=True)


@pytest.mark.parametrize(
    ("event_index", "outcome"),
    [
        (0, "success"),
        (1, "success"),
        (2, "fail"),
        (3, "fail"),
        (4, "success"),
        (5, "success"),
        (6, None),
        (7, "success"),
        (8, "timeout"),
        (9, "success"),
        (10, "timeout"),
        (11, "success"),
    ],
)
def test_every_growth_event_has_a_closed_outcome(
    event_index: int,
    outcome: str | None,
) -> None:
    payload = dict(_valid_growth_payloads()[event_index][1])
    payload["outcome"] = outcome

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_external_growth_event_rejects_unknown_fields() -> None:
    payload = dict(_valid_growth_payloads()[4][1])
    payload["campaign"] = "synthetic-campaign"

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_name", "future_event"),
        ("event_version", 2),
        ("platform", "ios"),
        ("consent_scope", "growth"),
        ("source", "desktop"),
    ],
)
def test_turn_contract_rejects_unknown_or_cross_scope_values(field: str, value: object) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload[field] = value

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_contract_rejects_unknown_fields() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload["model"] = "synthetic-model"

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [("app_version", "v1/path"), ("notice_version", "v1 notice")],
)
def test_version_fields_accept_only_closed_safe_characters(field: str, value: str) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload[field] = value

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize("field", ["prompt", "fileName", "stack_trace", "arguments"])
def test_recursive_privacy_scan_rejects_forbidden_fields_without_echoing_value(
    field: str,
) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload["properties"] = {"safe": [{field: "synthetic-secret-value"}]}

    with pytest.raises(ValidationError) as exc_info:
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)

    assert "synthetic-secret-value" not in str(exc_info.value)
    assert field in str(exc_info.value)


def test_privacy_scan_allows_purpose_specific_analytics_user_id() -> None:
    assert_no_forbidden_fields({"analytics_user_id": ANALYTICS_USER_ID})
    assert not is_forbidden_field_name("analytics_user_id")


def test_privacy_scan_reports_only_structural_path() -> None:
    with pytest.raises(ForbiddenTelemetryFieldError) as exc_info:
        assert_no_forbidden_fields({"outer": [{"file-path": "synthetic-secret-value"}]})

    assert "outer.0.file-path" in str(exc_info.value)
    assert "synthetic-secret-value" not in str(exc_info.value)


def test_privacy_scan_fails_closed_on_excessive_nesting() -> None:
    payload: object = "leaf"
    for _ in range(18):
        payload = {"safe": payload}

    with pytest.raises(ForbiddenTelemetryFieldError, match="depth limit"):
        assert_no_forbidden_fields(payload)


@pytest.mark.parametrize("field", ["event_id", "app_session_id"])
def test_reliability_identifiers_must_be_uuid4(field: str) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload[field] = str(uuid1())

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_growth_analysis_identifier_must_be_uuid4() -> None:
    payload = dict(_valid_growth_payloads()[1][1])
    payload["analytics_user_id"] = str(uuid1())

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_acquisition_identifier_must_be_uuid4() -> None:
    payload = dict(_valid_growth_payloads()[4][1])
    payload["acquisition_id"] = str(uuid1())

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-01T09:02:03.456+08:00",
        "2026-09-01T01:02:03.456",
        "2026-09-01",
    ],
)
def test_event_timestamp_requires_rfc3339_utc_z(timestamp: str) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload["occurred_at_utc"] = timestamp

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_timestamp_is_normalized_to_milliseconds() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload["occurred_at_utc"] = "2026-09-01T01:02:03.456789Z"

    event = TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)

    assert event.occurred_at_utc == datetime(2026, 9, 1, 1, 2, 3, 456000, tzinfo=UTC)
    assert event.model_dump(mode="json")["occurred_at_utc"] == OCCURRED_AT


@pytest.mark.parametrize("sample_rate", [True, 0, -0.1, 1.01, float("nan"), float("inf")])
def test_sample_rate_rejects_non_numeric_boolean_or_out_of_range_values(
    sample_rate: object,
) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload["sample_rate"] = sample_rate

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize("sample_rate", [0.5, 0, True, "1"])
def test_growth_events_are_never_sampled(sample_rate: object) -> None:
    payload = dict(_valid_growth_payloads()[1][1])
    payload["sample_rate"] = sample_rate

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_onboarding_flow_version_rejects_boolean() -> None:
    payload = dict(_valid_growth_payloads()[0][1])
    payload["flow_version"] = True

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    ("event_index", "changes"),
    [
        (4, {"source": "desktop"}),
        (4, {"app_version": "1.2.3"}),
        (6, {"source": "website"}),
        (6, {"app_version": None}),
        (7, {"source": "desktop"}),
        (9, {"source": "account_service"}),
        (10, {"source": "desktop"}),
        (10, {"app_version": "1.2.3"}),
    ],
)
def test_growth_producer_and_app_version_are_event_specific(
    event_index: int,
    changes: dict[str, object],
) -> None:
    payload = dict(_valid_growth_payloads()[event_index][1])
    payload.update(changes)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "success", "error_code": "internal_error"},
        {"outcome": "fail", "error_code": None},
        {"outcome": "timeout", "error_code": "internal_error"},
    ],
)
def test_install_result_has_closed_outcomes_and_safe_error_pair(
    changes: dict[str, object],
) -> None:
    payload = dict(_valid_growth_payloads()[8][1])
    payload.update(changes)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "success", "analytics_user_id": None},
        {
            "outcome": "fail",
            "error_code": "verification_failed",
            "analytics_user_id": ANALYTICS_USER_ID,
        },
        {"outcome": "fail", "error_code": None, "analytics_user_id": None},
        {
            "outcome": "timeout",
            "error_code": "service_unavailable",
            "analytics_user_id": None,
        },
    ],
)
def test_registration_result_links_identity_only_on_success(
    changes: dict[str, object],
) -> None:
    payload = dict(_valid_growth_payloads()[10][1])
    payload.update(changes)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize("duration", [-1, True, "120"])
def test_duration_is_a_bounded_strict_integer(duration: object) -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload["duration_ms"] = duration

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    ("outcome", "error_code", "failure_stage"),
    [
        ("success", "internal_error", None),
        ("success", None, "profile"),
        ("fail", None, "profile"),
        ("fail", "internal_error", None),
    ],
)
def test_app_start_cross_field_invariants(
    outcome: str, error_code: str | None, failure_stage: str | None
) -> None:
    payload = dict(_valid_reliability_payloads()[0][1])
    payload.update(outcome=outcome, error_code=error_code, failure_stage=failure_stage)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_non_success_turn_requires_safe_error_code() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload.update(outcome="timeout", error_code=None)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_turn_ttft_cannot_exceed_total_duration() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])
    payload.update(duration_ms=100, ttft_ms=101)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"update_stage": "check", "result": None},
        {"update_stage": "check", "result": "available", "new_version": None},
        {"update_stage": "check", "result": "not_available", "new_version": "2.0.0"},
        {"update_stage": "download", "result": "available", "new_version": "2.0.0"},
        {"update_stage": "install", "result": None, "new_version": None},
    ],
)
def test_update_stage_cross_field_invariants(changes: dict[str, object]) -> None:
    payload = dict(_valid_reliability_payloads()[6][1])
    payload.update(changes)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"turn_count": 1, "stalled_turn_count": 2},
        {"stalled_turn_count": 2, "stall_count": 1},
        {"monitored_request_count": 1, "slow_request_count": 2},
        {"duration_ms": 100, "foreground_duration_ms": 60, "background_duration_ms": 50},
        {"summary_kind": "session_end", "coverage": "partial"},
        {"summary_kind": "recovered_abnormal", "coverage": "complete"},
    ],
)
def test_performance_summary_cross_field_invariants(changes: dict[str, object]) -> None:
    payload = dict(_valid_reliability_payloads()[7][1])
    payload.update(changes)

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_json(_wire_json(payload), strict=True)


def test_reliability_batch_is_closed_and_requires_unique_event_ids() -> None:
    first = dict(_valid_reliability_payloads()[3][1])
    duplicate = dict(first)
    batch = {
        "batch_version": 1,
        "batch_id": BATCH_ID,
        "sent_at_utc": SENT_AT,
        "events": [first, duplicate],
    }

    with pytest.raises(ValidationError):
        RELIABILITY_BATCH_ADAPTER.validate_json(_wire_json(batch), strict=True)


def test_reliability_batch_rejects_growth_events() -> None:
    batch = {
        "batch_version": 1,
        "batch_id": BATCH_ID,
        "sent_at_utc": SENT_AT,
        "events": [_valid_growth_payloads()[1][1]],
    }

    with pytest.raises(ValidationError):
        RELIABILITY_BATCH_ADAPTER.validate_json(_wire_json(batch), strict=True)


def test_growth_batch_accepts_closed_growth_event() -> None:
    batch_payload = {
        "batch_version": 1,
        "batch_id": BATCH_ID,
        "sent_at_utc": SENT_AT,
        "events": [_valid_growth_payloads()[1][1]],
    }

    batch = GROWTH_BATCH_ADAPTER.validate_json(_wire_json(batch_payload), strict=True)

    assert len(batch.events) == 1
    assert isinstance(batch.events[0], FirstAppReady)


def test_reliability_batch_enforces_event_limit() -> None:
    template = _valid_reliability_payloads()[3][1]
    events: list[dict[str, object]] = []
    for _ in range(101):
        event = deepcopy(template)
        event["event_id"] = str(uuid4())
        events.append(event)
    batch = {
        "batch_version": 1,
        "batch_id": BATCH_ID,
        "sent_at_utc": SENT_AT,
        "events": events,
    }

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            _wire_json(batch),
            target=TelemetryWireTarget.RELIABILITY_BATCH,
        )

    assert exc_info.value.code is TelemetryWireErrorCode.SCHEMA_INVALID


def test_batch_rejects_non_utc_timestamp_and_boolean_version() -> None:
    batch = {
        "batch_version": True,
        "batch_id": BATCH_ID,
        "sent_at_utc": "2026-09-01T09:03:00+08:00",
        "events": [_valid_growth_payloads()[1][1]],
    }

    with pytest.raises(ValidationError):
        GROWTH_BATCH_ADAPTER.validate_json(_wire_json(batch), strict=True)


def test_public_wire_parser_accepts_bytes_and_text_for_exact_batch_targets() -> None:
    reliability_wire = _wire_json(_reliability_batch_payload())
    growth_wire = _wire_json(_growth_batch_payload())

    reliability = parse_telemetry_wire(
        reliability_wire.encode("utf-8"),
        target=TelemetryWireTarget.RELIABILITY_BATCH,
    )
    growth = parse_telemetry_wire(
        growth_wire,
        target=TelemetryWireTarget.GROWTH_BATCH,
    )

    assert isinstance(reliability.events[0], TurnResult)
    assert isinstance(growth.events[0], FirstAppReady)


def test_public_wire_parser_handles_single_events_with_the_same_preflight() -> None:
    wire = _wire_json(_valid_reliability_payloads()[3][1])
    duplicate_duration = wire.replace(
        '"duration_ms":120',
        '"duration_ms":120,"duration_ms":121',
        1,
    )

    event = parse_telemetry_wire(
        wire,
        target=TelemetryWireTarget.RELIABILITY_EVENT,
    )
    assert isinstance(event, TurnResult)

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            duplicate_duration,
            target=TelemetryWireTarget.RELIABILITY_EVENT,
        )
    assert exc_info.value.code is TelemetryWireErrorCode.DUPLICATE_KEY


@pytest.mark.parametrize(
    "duplicate_wire",
    [
        lambda wire: wire.replace(
            '"batch_version":1',
            '"batch_version":1,"batch_version":1',
            1,
        ),
        lambda wire: wire.replace(
            '"duration_ms":120',
            '"duration_ms":120,"duration_ms":121',
            1,
        ),
    ],
)
def test_wire_parser_rejects_duplicate_batch_and_event_keys(duplicate_wire) -> None:
    wire = duplicate_wire(_wire_json(_reliability_batch_payload()))

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            wire,
            target=TelemetryWireTarget.RELIABILITY_BATCH,
        )

    assert exc_info.value.code is TelemetryWireErrorCode.DUPLICATE_KEY


@pytest.mark.parametrize("number_token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_wire_parser_rejects_nonstandard_or_nonfinite_numbers(number_token: str) -> None:
    wire = _wire_json(_reliability_batch_payload()).replace(
        '"sample_rate":1.0',
        f'"sample_rate":{number_token}',
        1,
    )

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            wire,
            target=TelemetryWireTarget.RELIABILITY_BATCH,
        )

    assert exc_info.value.code is TelemetryWireErrorCode.NON_FINITE_NUMBER


def test_wire_parser_rejects_excessive_structure_depth_before_schema_validation() -> None:
    nested: object = "leaf"
    for _ in range(MAX_TELEMETRY_NESTING_DEPTH + 1):
        nested = {"safe": nested}
    payload = _reliability_batch_payload()
    event = dict(_valid_reliability_payloads()[3][1])
    event["unknown_nested_value"] = nested
    payload["events"] = [event]

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            _wire_json(payload),
            target=TelemetryWireTarget.RELIABILITY_BATCH,
        )

    assert exc_info.value.code is TelemetryWireErrorCode.NESTING_TOO_DEEP


@pytest.mark.parametrize(
    ("target", "limit"),
    [
        (TelemetryWireTarget.RELIABILITY_BATCH, MAX_RELIABILITY_BATCH_BYTES),
        (TelemetryWireTarget.GROWTH_BATCH, MAX_GROWTH_BATCH_BYTES),
    ],
)
def test_wire_parser_enforces_endpoint_byte_limit_before_json_parsing(
    target: TelemetryWireTarget,
    limit: int,
) -> None:
    oversized_invalid_json = b"{" + (b" " * limit)

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(oversized_invalid_json, target=target)

    assert exc_info.value.code is TelemetryWireErrorCode.BODY_TOO_LARGE


@pytest.mark.parametrize(
    ("target", "limit", "payload"),
    [
        (
            TelemetryWireTarget.RELIABILITY_BATCH,
            MAX_RELIABILITY_BATCH_BYTES,
            _reliability_batch_payload(),
        ),
        (
            TelemetryWireTarget.GROWTH_BATCH,
            MAX_GROWTH_BATCH_BYTES,
            _growth_batch_payload(),
        ),
    ],
)
def test_wire_parser_accepts_a_valid_payload_at_the_exact_byte_limit(
    target: TelemetryWireTarget,
    limit: int,
    payload: dict[str, object],
) -> None:
    wire = _wire_json(payload).encode("utf-8")
    padded_wire = wire + (b" " * (limit - len(wire)))

    assert len(padded_wire) == limit
    parsed = parse_telemetry_wire(padded_wire, target=target)
    assert len(parsed.events) == 1


def test_wire_parser_counts_utf8_bytes_instead_of_text_characters() -> None:
    oversized_text = "测" * ((MAX_GROWTH_BATCH_BYTES // 3) + 1)

    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            oversized_text,
            target=TelemetryWireTarget.GROWTH_BATCH,
        )

    assert exc_info.value.code is TelemetryWireErrorCode.BODY_TOO_LARGE


@pytest.mark.parametrize(
    ("wire", "expected_code"),
    [
        (b"\xef\xbb\xbf{}", TelemetryWireErrorCode.UTF8_BOM),
        (b"\xff{}", TelemetryWireErrorCode.INVALID_UTF8),
    ],
)
def test_wire_parser_rejects_bom_and_invalid_utf8(
    wire: bytes,
    expected_code: TelemetryWireErrorCode,
) -> None:
    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            wire,
            target=TelemetryWireTarget.GROWTH_BATCH,
        )

    assert exc_info.value.code is expected_code


def test_wire_parser_never_cross_accepts_endpoint_schemas() -> None:
    with pytest.raises(TelemetryWireError) as exc_info:
        parse_telemetry_wire(
            _wire_json(_growth_batch_payload()),
            target=TelemetryWireTarget.RELIABILITY_BATCH,
        )

    assert exc_info.value.code is TelemetryWireErrorCode.SCHEMA_INVALID


def test_raw_wire_parser_is_the_only_recommended_public_ingress() -> None:
    assert "parse_telemetry_wire" in telemetry_contracts.__all__
    assert "TELEMETRY_EVENT_ADAPTER" not in telemetry_contracts.__all__
    assert "RELIABILITY_BATCH_ADAPTER" not in telemetry_contracts.__all__
    assert "GROWTH_BATCH_ADAPTER" not in telemetry_contracts.__all__


def test_protocol_manifest_and_fingerprint_are_stable_cross_language_golden() -> None:
    manifest = telemetry_protocol_manifest()
    fingerprint = hashlib.sha256(TELEMETRY_PROTOCOL_MANIFEST_JSON.encode("utf-8")).hexdigest()
    manifest_events = {
        (entry["event_name"], entry["event_version"]) for entry in manifest["events"]
    }

    assert fingerprint == TELEMETRY_PROTOCOL_FINGERPRINT_SHA256
    assert TELEMETRY_PROTOCOL_FINGERPRINT_SHA256 == (
        "74d821c7d6ea2f3f08b5e27280da24ff17a51a913a165d5314d413d6204c1b7b"
    )
    assert manifest_events == set(EVENT_MODELS)
    assert manifest["notice_versions"] == dict(CURRENT_NOTICE_VERSION_BY_SCOPE)
    assert manifest["notice_versions"] == {
        "growth": CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
        "reliability": CURRENT_RELIABILITY_NOTICE_VERSION,
    }
    assert manifest["batch_limits"] == {
        "growth": {"max_bytes": MAX_GROWTH_BATCH_BYTES, "max_events": 50},
        "reliability": {"max_bytes": MAX_RELIABILITY_BATCH_BYTES, "max_events": 100},
    }


def test_canonical_json_is_stable_compact_sorted_and_keeps_null_fields() -> None:
    event = TELEMETRY_EVENT_ADAPTER.validate_json(
        _wire_json(_valid_reliability_payloads()[3][1]), strict=True
    )

    first = canonical_json_bytes(event)
    second = canonical_json(event).encode("utf-8")
    decoded = first.decode("utf-8")

    assert first == second
    assert decoded.startswith('{"app_session_id":')
    assert " " not in decoded
    assert '"error_code":null' in decoded
    assert '"occurred_at_utc":"2026-09-01T01:02:03.456Z"' in decoded
    assert json.loads(decoded)["event_id"] == EVENT_ID


def test_canonical_json_revalidates_unsafe_model_copy() -> None:
    event = TELEMETRY_EVENT_ADAPTER.validate_json(
        _wire_json(_valid_reliability_payloads()[3][1]), strict=True
    )
    unsafe = event.model_copy(update={"duration_ms": -1})

    with pytest.raises(ValidationError):
        canonical_json_bytes(unsafe)


def test_contracts_are_frozen() -> None:
    event = TELEMETRY_EVENT_ADAPTER.validate_json(
        _wire_json(_valid_reliability_payloads()[3][1]), strict=True
    )

    with pytest.raises(ValidationError):
        event.duration_ms = 999  # type: ignore[misc]


def test_json_schema_forbids_additional_properties() -> None:
    schema = TurnResult.model_json_schema()

    assert schema["additionalProperties"] is False


def test_uuid_helpers_generate_distinct_rfc4122_uuid4_values() -> None:
    generated = {
        new_event_id(),
        new_batch_id(),
        new_app_session_id(),
        new_analytics_user_id(),
    }

    assert len(generated) == 4
    assert all(isinstance(value, UUID) and is_uuid4(value) for value in generated)


def test_python_mapping_with_wire_uuid_and_timestamp_strings_is_not_silently_coerced() -> None:
    payload = dict(_valid_reliability_payloads()[3][1])

    with pytest.raises(ValidationError):
        TELEMETRY_EVENT_ADAPTER.validate_python(payload, strict=True)


def test_event_key_shapes_are_closed_and_exact() -> None:
    expected_common = {
        "event_name",
        "event_version",
        "event_id",
        "occurred_at_utc",
        "source",
        "app_version",
        "platform",
        "outcome",
        "error_code",
        "duration_ms",
        "consent_scope",
        "notice_version",
        "sample_rate",
    }

    for _, payload in _valid_reliability_payloads():
        assert expected_common | {"app_session_id"} <= set(payload)
    for _, payload in _valid_growth_payloads()[:4]:
        assert expected_common | {"analytics_user_id"} <= set(payload)
        assert "acquisition_id" not in payload
    for _, payload in _valid_growth_payloads()[4:10]:
        assert expected_common | {"acquisition_id"} <= set(payload)
        assert "analytics_user_id" not in payload
    registration_result = _valid_growth_payloads()[10][1]
    assert expected_common | {"acquisition_id", "analytics_user_id"} <= set(registration_result)


def test_second_event_id_fixture_is_valid_uuid4() -> None:
    # Keep a second stable synthetic identifier available for future batch tests.
    assert is_uuid4(UUID(SECOND_EVENT_ID))
