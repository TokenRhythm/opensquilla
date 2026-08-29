"""Golden-oracle checks for the v4 ``sessions.changed`` event Contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from opensquilla.contracts.adapters.sessions_changed_contract import (
    SESSIONS_CHANGED_EVENT,
    SESSIONS_CHANGED_SCHEMA_VERSION,
    SessionsChangedContractError,
    canonicalize_sessions_changed_payload,
    observe_sessions_changed_payload,
    validate_sessions_changed_payload,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_EVENT_CONTRACTS,
)
from opensquilla.contracts.generated.v4.sessions_changed import (
    SessionsChangedCanonicalPayload,
    SessionsChangedEventPayload,
    SessionsChangedLegacyPayload,
)
from opensquilla.contracts.generated.v4.sessions_changed_metadata import (
    SESSIONS_CHANGED_EVENT_METADATA,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "contracts/gateway/v4/sessions/sessions-changed.schema.json"
FIXTURES = SCHEMA.parent / "fixtures/sessions-changed"


def _document(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _event_cases() -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(case["id"]), case["wire"])
        for case in _document("events.json")["cases"]
    ]


def _error_cases() -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(case["id"]), case["wire"])
        for case in _document("errors.json")["cases"]
    ]


def test_event_metadata_and_registry_are_language_neutral() -> None:
    document = json.loads(SCHEMA.read_text(encoding="utf-8"))
    event = document["x-opensquilla-event"]
    assert event["name"] == SESSIONS_CHANGED_EVENT == "sessions.changed"
    assert event["delivery"] == "live-only-best-effort"
    assert event["schemaVersion"] == 1
    assert SESSIONS_CHANGED_SCHEMA_VERSION == event["schemaVersion"]
    assert event["compatibility"] == "canonical-or-legacy"
    assert SESSIONS_CHANGED_EVENT_METADATA == event

    descriptor = GATEWAY_EVENT_CONTRACTS[SESSIONS_CHANGED_EVENT]
    assert descriptor.name == SESSIONS_CHANGED_EVENT
    assert descriptor.schema_version == 1
    assert descriptor.payload_model is SessionsChangedEventPayload


def test_fixture_ids_and_coverage_are_explicit_and_unique() -> None:
    documents = [_document("events.json"), _document("errors.json")]
    cases = [case for document in documents for case in document["cases"]]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert all(case.get("coverage") for case in cases)
    assert {case.get("expectation") for case in cases} == {"exact", "reject"}


@pytest.mark.parametrize(("case_id", "wire"), _event_cases())
def test_event_fixtures_round_trip_exact_json_tree(
    case_id: str,
    wire: dict[str, Any],
) -> None:
    validated = validate_sessions_changed_payload(wire)
    parsed = SessionsChangedEventPayload.model_validate(validated)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire, case_id


def test_generated_models_keep_canonical_and_legacy_branches_distinct() -> None:
    canonical = {
        "schema_version": 1,
        "key": "agent:main:canonical",
        "reason": "created",
        "future": {"enabled": True},
    }
    legacy = {
        "key": "agent:main:legacy",
        "reason": "cron_system_event",
        "future": {"enabled": True},
    }
    assert SessionsChangedCanonicalPayload.model_validate(canonical).model_dump(
        mode="json", exclude_unset=True
    ) == canonical
    assert SessionsChangedLegacyPayload.model_validate(legacy).model_dump(
        mode="json", exclude_unset=True
    ) == legacy


@pytest.mark.parametrize(("case_id", "wire"), _error_cases())
def test_invalid_event_fixtures_are_rejected(case_id: str, wire: dict[str, Any]) -> None:
    with pytest.raises(SessionsChangedContractError):
        validate_sessions_changed_payload(wire)


def test_generated_union_is_used_after_the_adapter_fences_version_discriminator() -> None:
    """The authored adapter owns the JSON-Schema ``not`` edge case.

    datamodel-code-generator models open additive objects as ``extra=allow``
    and cannot encode an object-level absence assertion.  The adapter checks
    that discriminator before selecting the generated canonical/legacy model;
    valid payloads still use the generated union for the rest of validation.
    """

    payload = {"schema_version": 1, "key": "k", "reason": "created"}
    validated = validate_sessions_changed_payload(payload)
    parsed = SessionsChangedEventPayload.model_validate(validated)
    assert isinstance(parsed.root, SessionsChangedCanonicalPayload)


@pytest.mark.parametrize(
    "payload_type",
    [SessionsChangedCanonicalPayload, SessionsChangedLegacyPayload],
)
def test_generated_event_models_reject_explicit_null_for_non_nullable_run_status(
    payload_type: type[Any],
) -> None:
    with pytest.raises(ValidationError):
        payload_type.model_validate(
            {
                **(
                    {"schema_version": 1}
                    if payload_type is SessionsChangedCanonicalPayload
                    else {}
                ),
                "key": "k",
                "reason": "created",
                "run_status": None,
            }
        )


def test_legacy_payload_can_be_canonicalized_without_mutating_wire_input() -> None:
    legacy = {
        "key": "agent:main:legacy",
        "reason": "auto_titled",
        "title": "A title",
    }
    canonical = canonicalize_sessions_changed_payload(legacy)
    assert canonical == {"schema_version": 1, **legacy}
    assert legacy == {
        "key": "agent:main:legacy",
        "reason": "auto_titled",
        "title": "A title",
    }
    assert canonical is not legacy


def test_canonicalization_also_returns_a_copy_for_canonical_payload() -> None:
    canonical_input = {
        "schema_version": 1,
        "key": "agent:main:canonical",
        "reason": "created",
    }
    canonical = canonicalize_sessions_changed_payload(canonical_input)
    assert canonical == canonical_input
    assert canonical is not canonical_input


def test_canonicalization_rejects_unknown_schema_version() -> None:
    with pytest.raises(SessionsChangedContractError, match="schema_version"):
        canonicalize_sessions_changed_payload(
            {"schema_version": 2, "key": "k", "reason": "created"}
        )


def test_observation_is_fail_open_for_best_effort_delivery() -> None:
    invalid = {"key": "k", "reason": 7}
    assert observe_sessions_changed_payload(invalid, source="test") is invalid


def test_observation_can_report_missing_version_at_canonical_boundaries() -> None:
    legacy = {"key": "k", "reason": "created"}
    assert observe_sessions_changed_payload(
        legacy,
        source="test",
        allow_legacy=False,
    ) is legacy


@pytest.mark.parametrize("invalid", [None, [], ["not-an-object"]])
def test_observation_is_fail_open_for_non_object_payload(invalid: Any) -> None:
    assert observe_sessions_changed_payload(invalid, source="test") is invalid
