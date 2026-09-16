"""Golden-oracle checks for the ApprovalCenter v4 Contract seam.

The fixture is shared with the WebUI Adapter tests.  These tests validate the
generated Python side and the authored compatibility projection; no Gateway
handler, queue, or production UI path is imported.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.contracts.adapters.approval_center_contract import (
    APPROVAL_EVENT_WIRE_NAMES,
    APPROVAL_METHOD_ALIASES,
    ApprovalCenterContractError,
    ApprovalEventProjection,
    approval_request_contract_errors,
    build_approval_request,
    canonicalize_approval_event,
    observe_approval_event,
    project_approval_status,
    validate_approval_event_payload,
    validate_approval_http_snapshot,
    validate_approval_response_frame,
    validate_approval_result,
)
from opensquilla.contracts.generated.v4.approval_events import (
    ApprovalEventPayload,
)
from opensquilla.contracts.generated.v4.approval_events_metadata import (
    APPROVAL_EVENTS_EVENT_METADATA,
    APPROVAL_EVENTS_SCHEMA_VERSION,
)
from opensquilla.contracts.generated.v4.approval_extend import (
    ApprovalExtendRequestFrame,
    ApprovalExtendResponseFrame,
    ApprovalExtendResult,
)
from opensquilla.contracts.generated.v4.approval_resolve import (
    ApprovalResolveRequestFrame,
    ApprovalResolveResponseFrame,
    ApprovalResolveResult,
)
from opensquilla.contracts.generated.v4.approval_snapshot import (
    ExecApprovalSnapshotRequestFrame,
    ExecApprovalSnapshotResponseFrame,
    ExecApprovalSnapshotResult,
)
from opensquilla.contracts.generated.v4.approval_status import (
    ApprovalStatusRequestFrame,
    ApprovalStatusResponseFrame,
    ApprovalStatusResult,
)
from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_EVENT_CONTRACTS,
    GATEWAY_METHOD_CONTRACTS,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "contracts/gateway/v4/approvals/fixtures/approval-center.json"
SCHEMA_ROOT = ROOT / "contracts/gateway/v4/approvals"


def _document() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _response_frame(method: str, result: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    return {
        "type": "res",
        "id": request_id,
        "ok": True,
        "payload": result,
        "error": None,
    }


REQUEST_MODELS = {
    "exec.approval.snapshot": ExecApprovalSnapshotRequestFrame,
    "exec.approval.status": ApprovalStatusRequestFrame,
    "plugin.approval.status": ApprovalStatusRequestFrame,
    "exec.approval.resolve": ApprovalResolveRequestFrame,
    "plugin.approval.resolve": ApprovalResolveRequestFrame,
    "exec.approval.extend": ApprovalExtendRequestFrame,
    "plugin.approval.extend": ApprovalExtendRequestFrame,
}
RESPONSE_MODELS = {
    "exec.approval.snapshot": ExecApprovalSnapshotResponseFrame,
    "exec.approval.status": ApprovalStatusResponseFrame,
    "plugin.approval.status": ApprovalStatusResponseFrame,
    "exec.approval.resolve": ApprovalResolveResponseFrame,
    "plugin.approval.resolve": ApprovalResolveResponseFrame,
    "exec.approval.extend": ApprovalExtendResponseFrame,
    "plugin.approval.extend": ApprovalExtendResponseFrame,
}
RESULT_MODELS = {
    "exec.approval.snapshot": ExecApprovalSnapshotResult,
    "exec.approval.status": ApprovalStatusResult,
    "plugin.approval.status": ApprovalStatusResult,
    "exec.approval.resolve": ApprovalResolveResult,
    "plugin.approval.resolve": ApprovalResolveResult,
    "exec.approval.extend": ApprovalExtendResult,
    "plugin.approval.extend": ApprovalExtendResult,
}


def test_metadata_and_registry_pin_the_existing_approval_boundary() -> None:
    assert APPROVAL_EVENTS_SCHEMA_VERSION == 1
    assert APPROVAL_EVENTS_EVENT_METADATA["delivery"] == "live-only-best-effort"
    assert APPROVAL_EVENTS_EVENT_METADATA["legacyUnversioned"] is True
    assert APPROVAL_EVENTS_EVENT_METADATA["redaction"] == "server-projected-display-only"
    assert set(APPROVAL_EVENT_WIRE_NAMES) == {
        "exec.approval.requested",
        "exec.approval.updated",
        "exec.approval.resolved",
        "plugin.approval.requested",
        "plugin.approval.updated",
        "plugin.approval.resolved",
    }
    assert GATEWAY_EVENT_CONTRACTS["approval.events"].payload_model is ApprovalEventPayload
    for method in (
        "exec.approval.snapshot",
        "exec.approval.status",
        "exec.approval.resolve",
        "exec.approval.extend",
    ):
        descriptor = GATEWAY_METHOD_CONTRACTS[method]
        assert descriptor.scope == "operator.approvals"
        assert descriptor.protocol == "opensquilla-websocket-json"
        assert descriptor.wire_version == 4
        assert descriptor.guest_allowed is False
    assert GATEWAY_METHOD_CONTRACTS["exec.approval.extend"].idempotency == "non-idempotent"
    assert APPROVAL_METHOD_ALIASES["plugin.approval.status"] == "exec.approval.status"
    assert APPROVAL_METHOD_ALIASES["plugin.approval.resolve"] == "exec.approval.resolve"
    assert APPROVAL_METHOD_ALIASES["plugin.approval.extend"] == "exec.approval.extend"


def test_fixture_case_ids_are_unique_and_cover_all_lifecycle_shapes() -> None:
    document = _document()
    cases: list[dict[str, Any]] = []
    for method in document["methods"].values():
        for group in method.values():
            if isinstance(group, list):
                cases.extend(item for item in group if isinstance(item, dict) and "id" in item)
    cases.extend(document["events"])
    ids = [str(case["id"]) for case in cases]
    assert len(ids) == len(set(ids))
    assert {case["wire_name"] for case in document["events"]} == {
        "exec.approval.requested",
        "exec.approval.resolved",
        "plugin.approval.updated",
    }
    assert {method for method in document["methods"]["status"]["methods"]} == {
        "exec.approval.status",
        "plugin.approval.status",
    }
    assert "resolve-unsupported-flag" in ids


@pytest.mark.parametrize(
    "case",
    [
        case
        for method in _document()["methods"].values()
        for case in method.get("requests", [])
    ],
    ids=lambda case: case["id"],
)
def test_request_fixtures_round_trip_exact_json_tree(case: dict[str, Any]) -> None:
    wire = case["wire"]
    method = wire["method"]
    parsed = REQUEST_MODELS[method].model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire
    if "params" in wire:
        rebuilt = build_approval_request(
            method,
            wire["params"],
            request_id=wire["id"],
        )
    else:
        rebuilt = build_approval_request(method, request_id=wire["id"])
    assert rebuilt == wire


@pytest.mark.parametrize(
    "method,case",
    [
        ("exec.approval.snapshot", case)
        for case in _document()["methods"]["snapshot"]["responses"]
    ]
    + [
        (method, case)
        for method in ("exec.approval.status", "plugin.approval.status")
        for case in _document()["methods"]["status"]["results"]
    ]
    + [
        (method, case)
        for method in ("exec.approval.resolve", "plugin.approval.resolve")
        for case in _document()["methods"]["resolve"]["results"]
    ]
    + [
        (method, case)
        for method in ("exec.approval.extend", "plugin.approval.extend")
        for case in _document()["methods"]["extend"]["results"]
    ],
    ids=lambda value: str(value),
)
def test_result_fixtures_round_trip_and_preserve_extensions(
    method: str,
    case: dict[str, Any],
) -> None:
    wire = case["wire"]["payload"] if method == "exec.approval.snapshot" else case["wire"]
    parsed = RESULT_MODELS[method].model_validate(wire)
    assert parsed.model_dump(mode="json", exclude_unset=True) == wire
    assert validate_approval_result(method, wire) is wire


def test_snapshot_response_and_unsupported_flag_error_keep_v4_envelope() -> None:
    document = _document()
    snapshot_wire = document["methods"]["snapshot"]["responses"][0]["wire"]
    snapshot_frame = _response_frame(
        "exec.approval.snapshot",
        snapshot_wire["payload"],
        request_id=snapshot_wire["id"],
    )
    parsed = ExecApprovalSnapshotResponseFrame.model_validate(snapshot_frame)
    assert parsed.model_dump(mode="json", exclude_unset=True) == snapshot_frame
    assert (
        validate_approval_response_frame("exec.approval.snapshot", snapshot_frame)
        is snapshot_frame
    )

    error_wire = document["methods"]["resolve"]["errors"][0]["wire"]
    parsed_error = ApprovalResolveResponseFrame.model_validate(error_wire)
    assert parsed_error.model_dump(mode="json", exclude_unset=True) == error_wire
    assert validate_approval_response_frame("plugin.approval.resolve", error_wire) is error_wire


def test_request_observation_does_not_rewrite_legacy_or_removed_fields() -> None:
    assert approval_request_contract_errors(
        "plugin.approval.status",
        {"id": "approval-plugin-1", "legacy_extension": True},
    ) == ()
    assert approval_request_contract_errors("exec.approval.status", {})
    # The server deliberately rejects truthy flags with UNSUPPORTED_PARAM;
    # accepting them here is what preserves that public error semantics.
    assert approval_request_contract_errors(
        "exec.approval.resolve",
        {"id": "approval-exec-1", "approved": True, "allowAlways": True},
    ) == ()
    assert approval_request_contract_errors("exec.approval.status", "legacy") == ()


@pytest.mark.parametrize("case", _document()["events"], ids=lambda case: case["id"])
def test_event_fixtures_validate_without_mutating_the_wire(case: dict[str, Any]) -> None:
    wire = case["wire"]
    original = copy.deepcopy(wire)
    validated = validate_approval_event_payload(wire)
    assert validated is wire
    assert wire == original
    projection = canonicalize_approval_event(case["wire_name"], wire)
    assert isinstance(projection, ApprovalEventProjection)
    assert projection.approval_id == wire.get("approval_id", wire.get("approvalId"))
    assert projection.namespace == wire["namespace"]


def test_event_discriminator_and_alias_rules_are_explicit() -> None:
    canonical = {"approval_id": "a", "schema_version": 1}
    assert validate_approval_event_payload(canonical) is canonical
    with pytest.raises(ApprovalCenterContractError, match="schema_version"):
        validate_approval_event_payload({"approval_id": "a", "schema_version": 2})
    with pytest.raises(ApprovalCenterContractError, match="requires approval_id"):
        validate_approval_event_payload({"namespace": "exec"})
    with pytest.raises(ApprovalCenterContractError, match="missing schema_version"):
        validate_approval_event_payload({"approval_id": "a"}, allow_legacy=False)
    with pytest.raises(ApprovalCenterContractError, match="conflicting aliases"):
        canonicalize_approval_event(
            "plugin.approval.updated",
            {"approval_id": "a", "approvalId": "b"},
        )
    with pytest.raises(ApprovalCenterContractError, match="does not match event"):
        canonicalize_approval_event(
            "plugin.approval.updated",
            {"approval_id": "a", "namespace": "exec"},
        )


def test_event_projection_redacts_nested_display_arguments() -> None:
    projection = canonicalize_approval_event(
        "exec.approval.requested",
        {
            "approval_id": "exec-1",
            "namespace": "exec",
            "args": {
                "command": "curl",
                "headers": {
                    "authorization": "Bearer secret",
                    "accept": "application/json",
                },
                "nested": {"token": "nested-secret", "visible": True},
                "nullable": None,
            },
            "future_metadata": {
                "review_action": "claim",
                "label": "safe",
            },
        },
    )
    assert projection.args == {
        "command": "curl",
        "headers": {"accept": "application/json"},
        "nested": {"visible": True},
        "nullable": None,
    }


def test_best_effort_event_observation_returns_invalid_wire_unchanged() -> None:
    invalid = {"approval_id": "a", "schema_version": 7}
    assert observe_approval_event(
        "exec.approval.requested",
        invalid,
        source="test",
    ) is invalid


def test_status_projection_distinguishes_missing_and_claimed_entries() -> None:
    document = _document()
    pending = document["methods"]["status"]["results"][0]["wire"]
    missing = document["methods"]["status"]["results"][3]["wire"]
    claimed = document["methods"]["status"]["results"][1]["wire"]
    assert project_approval_status("exec.approval.status", pending).pending is True
    assert project_approval_status("exec.approval.status", missing).found is False
    assert project_approval_status(
        "plugin.approval.status", claimed,
    ).resolution_in_progress is True
    assert project_approval_status(
        "exec.approval.resolve",
        document["methods"]["resolve"]["results"][0]["wire"],
        namespace="exec",
    ).resolved is True
    with pytest.raises(ApprovalCenterContractError, match="does not match"):
        project_approval_status(
            "exec.approval.status",
            {**pending, "namespace": "plugin"},
        )


def test_http_companion_is_display_only_and_redacted() -> None:
    snapshot = _document()["http_snapshot"]
    assert validate_approval_http_snapshot(snapshot) is snapshot
    item = snapshot["pending"][0]
    assert item["args"]["headers"]["Authorization"] == "[REDACTED]"
    assert item["created_at"] == 1730000002.5
    assert item["actionKind"] == "http_request"
    assert item["mode"] == "prompt"
    with pytest.raises(ApprovalCenterContractError, match="non-display field"):
        validate_approval_http_snapshot({
            **snapshot,
            "pending": [{"id": "a", "namespace": "exec", "params": {"token": "x"}}],
        })
    with pytest.raises(ApprovalCenterContractError, match="non-display field"):
        validate_approval_http_snapshot({
            **snapshot,
            "pending": [{"id": "a", "namespace": "exec", "claimToken": "opaque"}],
        })
    with pytest.raises(ApprovalCenterContractError, match="invalid mode"):
        validate_approval_http_snapshot({**snapshot, "mode": []})
    with pytest.raises(ApprovalCenterContractError, match="namespace is invalid"):
        validate_approval_http_snapshot({
            **snapshot,
            "pending": [{"id": "a", "namespace": []}],
        })
    with pytest.raises(ApprovalCenterContractError, match="aliases conflict"):
        validate_approval_http_snapshot({
            **snapshot,
            "pending": [{"id": "a", "namespace": "exec", "created_at": 1, "createdAt": 2}],
        })
    legacy_without_namespace = {
        **snapshot,
        "pending": [{"id": "legacy", "sessionKey": "s1"}],
    }
    assert validate_approval_http_snapshot(legacy_without_namespace) is legacy_without_namespace


def test_generated_event_union_still_accepts_current_and_legacy_payloads() -> None:
    current = {"approval_id": "current", "schema_version": 1}
    legacy = {"approvalId": "legacy"}
    assert ApprovalEventPayload.model_validate(current).model_dump(
        mode="json", exclude_unset=True,
    ) == current
    assert ApprovalEventPayload.model_validate(legacy).model_dump(
        mode="json", exclude_unset=True,
    ) == legacy


@pytest.mark.parametrize("method", sorted(RESPONSE_MODELS))
def test_response_models_are_registered_for_both_namespaces(method: str) -> None:
    assert method in APPROVAL_METHOD_ALIASES
    assert RESPONSE_MODELS[method] is RESPONSE_MODELS[APPROVAL_METHOD_ALIASES[method]]
