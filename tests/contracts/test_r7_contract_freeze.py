"""Contract freeze for the remaining WebUI-reachable v4 wire surface."""

from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_METHOD_CONTRACTS,
)
from opensquilla.gateway.adapters.contract_method import (
    GatewayContractBinding,
    register_gateway_contract_method,
)
from opensquilla.gateway.rpc import RpcHandlerError, RpcRegistry
from scripts.contracts.generate_gateway_contracts import discover_contracts

EXPECTED_METHOD_METADATA = {
    "memory.import.info": ("operator.read", "query", "read-only"),
    "memory.import.start": ("operator.admin", "command", "idempotent"),
    "memory.import.status": ("operator.admin", "query", "read-only"),
    "memory.import.cancel": ("operator.admin", "command", "idempotent"),
    "memory.import.retry": ("operator.admin", "command", "idempotent"),
    "memory.import.apply": ("operator.admin", "command", "idempotent"),
    "memory.import.undo": ("operator.admin", "command", "idempotent"),
    "memory.import.discard": ("operator.admin", "command", "idempotent"),
    "onboarding.channel.probe": ("operator.admin", "query", "read-only"),
    "onboarding.channel.upsert": ("operator.admin", "command", "idempotent"),
    "onboarding.channel.remove": ("operator.admin", "command", "idempotent"),
    "onboarding.channel.enable": ("operator.admin", "command", "idempotent"),
    "onboarding.channel.disable": ("operator.admin", "command", "idempotent"),
    "plugin.approval.status": ("operator.approvals", "query", "read-only"),
    "plugin.approval.resolve": ("operator.approvals", "command", "idempotent"),
    "plugin.approval.extend": ("operator.approvals", "command", "non-idempotent"),
    "plans.capabilities": ("operator.read", "query", "read-only"),
    "sandbox.path.pick": ("operator.write", "command", "non-idempotent"),
    "sandbox.path.create-directory": (
        "operator.write",
        "command",
        "non-idempotent",
    ),
}

RESPONSE_VALIDATED_METHODS = (
    "sandbox.path.list",
    "workspaces.open",
    "workspaces.update",
    "workspaces.pin",
    "workspaces.remove",
    "workspaces.history.delete",
)

EXPECTED_ACCURATE_ERROR_CODES = {
    "workspaces.open": (
        "OWNER_REQUIRED",
        "INVALID_PARAMS",
        "WORKSPACE_TRUST_REQUIRED",
        "INVALID_WORKSPACE_PATH",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "workspaces.update": (
        "OWNER_REQUIRED",
        "INVALID_PARAMS",
        "WORKSPACE_NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "workspaces.pin": (
        "OWNER_REQUIRED",
        "INVALID_PARAMS",
        "WORKSPACE_NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "workspaces.remove": (
        "OWNER_REQUIRED",
        "INVALID_PARAMS",
        "WORKSPACE_NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "workspaces.history.delete": (
        "OWNER_REQUIRED",
        "INVALID_PARAMS",
        "WORKSPACE_NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.drafts.list": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.drafts.discard": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.run": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "META_DRAFT_DISCARDED",
        "META_DRAFT_UNAVAILABLE",
        "META_DRAFT_OUTBOX_FULL",
        "META_LAUNCH_BUSY",
        "IDEMPOTENCY_CONFLICT",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.runs.confirm_preflight": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.runs.recovery": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.runs.replay": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.setup.plan": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "NOT_FOUND",
        "INTERNAL_ERROR",
    ),
    "meta.setup.install": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "NOT_FOUND",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "meta.setup.status": (
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "NOT_FOUND",
        "INTERNAL_ERROR",
    ),
    "migration.sources.list": (
        "migration.invalid_params",
        "migration.unavailable",
        "UNAUTHORIZED",
        "INTERNAL_ERROR",
    ),
    "migration.sources.preview": (
        "migration.invalid_params",
        "migration.candidate_unavailable",
        "migration.unavailable",
        "migration.preview_unavailable",
        "UNAUTHORIZED",
        "INTERNAL_ERROR",
    ),
    "sandbox.path.list": (
        "UNAUTHORIZED",
        "INVALID_REQUEST",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "sandbox.path.create-directory": (
        "UNAUTHORIZED",
        "INVALID_REQUEST",
        "INTERNAL_ERROR",
    ),
    "sandbox.path.pick": (
        "UNAUTHORIZED",
        "INVALID_REQUEST",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "onboarding.channel.probe": (
        "INVALID_REQUEST",
        "onboarding.channel.invalid",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "onboarding.channel.upsert": (
        "INVALID_REQUEST",
        "onboarding.channel.invalid",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "onboarding.channel.remove": (
        "INVALID_REQUEST",
        "onboarding.channel.not_found",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "onboarding.channel.enable": (
        "INVALID_REQUEST",
        "onboarding.channel.not_found",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
    "onboarding.channel.disable": (
        "INVALID_REQUEST",
        "onboarding.channel.not_found",
        "UNAUTHORIZED",
        "UNAVAILABLE",
        "INTERNAL_ERROR",
    ),
}


def _specs_by_wire_name():
    return {spec.wire_name: spec for spec in discover_contracts()}


def test_contract_inventory_freezes_all_webui_reachable_wire_names() -> None:
    specs = discover_contracts()

    assert len(specs) == 224
    assert Counter(spec.contract_type for spec in specs) == {
        "method": 215,
        "event": 9,
    }
    assert EXPECTED_METHOD_METADATA.keys() <= {spec.wire_name for spec in specs}
    assert "models.routing.changed" in {spec.wire_name for spec in specs}


def test_remaining_method_metadata_matches_existing_gateway_policy() -> None:
    specs = _specs_by_wire_name()

    for wire_name, (scope, kind, idempotency) in EXPECTED_METHOD_METADATA.items():
        spec = specs[wire_name]
        assert spec.contract_type == "method"
        assert spec.metadata["scope"] == scope
        assert spec.metadata["kind"] == kind
        assert spec.metadata["idempotency"] == idempotency
        assert spec.metadata["guestAllowed"] is False
        assert spec.metadata["capability"] == {
            "kind": "method-availability",
            "name": wire_name,
        }


def test_response_validated_handler_contracts_declare_fail_closed_error() -> None:
    for method in RESPONSE_VALIDATED_METHODS:
        descriptor = GATEWAY_METHOD_CONTRACTS[method]
        error_codes = {str(error["code"]) for error in descriptor.errors}

        assert descriptor.name == method
        assert "INTERNAL_ERROR" in error_codes


def test_contract_error_metadata_matches_reachable_gateway_codes() -> None:
    specs = _specs_by_wire_name()

    for method, expected in EXPECTED_ACCURATE_ERROR_CODES.items():
        assert tuple(error["code"] for error in specs[method].metadata["errors"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("method", RESPONSE_VALIDATED_METHODS)
async def test_real_registration_fixture_fails_closed_with_declared_error(
    method: str,
) -> None:
    descriptor = GATEWAY_METHOD_CONTRACTS[method]
    registry = RpcRegistry()

    async def invalid_implementation(_params: object, _ctx: object) -> object:
        return {"unexpected": True}

    binding = GatewayContractBinding(
        descriptor=descriptor,
        observe_params=lambda _params: (),
        validate_result=descriptor.result_model.model_validate,
        result_validation_errors=(ValidationError,),
        response_error_message=f"{method} response violated its v4 contract",
        request_mismatch_event=f"{method}.request_contract_mismatch",
        response_violation_event=f"{method}.contract_violation",
    )
    handler = register_gateway_contract_method(
        registry,
        binding,
        invalid_implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=lambda _method: False,
    )

    with pytest.raises(RpcHandlerError) as error:
        await handler({}, object())

    assert error.value.code == "INTERNAL_ERROR"
    assert error.value.message == f"{method} response violated its v4 contract"


def test_portable_hash_manifest_includes_protocol_identity_manifest() -> None:
    from scripts.contracts import generate_gateway_contracts as runner

    manifest = runner.build_hash_manifest(runner.discover_contracts())
    artifacts = manifest["artifacts"]

    assert isinstance(artifacts, dict)
    assert (
        "contracts/gateway/v4/compatibility-manifest.generated.json"
        in artifacts
    )


def test_memory_import_contracts_freeze_real_public_shapes() -> None:
    specs = _specs_by_wire_name()
    cancel = specs["memory.import.cancel"].document["$defs"]
    discard = specs["memory.import.discard"].document["$defs"]
    start = specs["memory.import.start"].document["$defs"]

    assert cancel["MemoryImportCancelParams"]["required"] == ["jobId"]
    assert "clientRequestId" in cancel["MemoryImportCancelParams"]["properties"]
    assert discard["MemoryImportDiscardParams"]["oneOf"] == [
        {"required": ["jobId"], "not": {"required": ["previewId"]}},
        {"required": ["previewId"], "not": {"required": ["jobId"]}},
    ]
    assert start["ProfileImportFileDiff"]["required"] == [
        "target",
        "displayName",
        "relativePath",
        "status",
        "additions",
        "deletions",
        "diff",
    ]
    assert start["ProfileImportJob"]["properties"]["agentId"] == {
        "type": "string"
    }
    undo_result = specs["memory.import.undo"].document["$defs"][
        "MemoryImportUndoResult"
    ]
    assert undo_result["additionalProperties"] is False
    assert "reviewContext" not in undo_result["properties"]
    discard_params = discard["MemoryImportDiscardParams"]
    assert discard_params["properties"]["jobId"] == {
        "type": "string",
        "minLength": 1,
    }
    assert discard_params["properties"]["previewId"] == {
        "type": "string",
        "minLength": 1,
    }


def test_channel_plan_sandbox_and_routing_event_shapes_are_explicit() -> None:
    specs = _specs_by_wire_name()

    channel = specs["onboarding.channel.upsert"].document["$defs"]
    assert channel["OnboardingChannelUpsertResult"]["required"] == [
        "changed",
        "restartRequired",
        "liveApply",
        "configPath",
        "entry",
        "warnings",
    ]
    assert channel["ChannelLiveApplyOutcome"]["enum"] == [
        "started",
        "rebuilt",
        "removed",
        "unchanged",
        "pending_restart",
        "failed",
    ]

    plans = specs["plans.capabilities"].document["$defs"]
    assert plans["PlansCapabilitiesResult"]["required"] == [
        "planMode",
        "initialModeOnSend",
        "atomicInitialMode",
    ]
    assert plans["PlansCapabilitiesParams"] == {}

    create = specs["sandbox.path.create-directory"].document["$defs"]
    assert create["SandboxPathCreateDirectoryParams"]["required"] == [
        "sessionKey",
        "parentPath",
        "name",
    ]
    pick = specs["sandbox.path.pick"].document["$defs"]
    assert pick["SandboxPathPickResult"]["properties"]["path"]["type"] == [
        "string",
        "null",
    ]

    event = specs["models.routing.changed"]
    assert event.metadata == {
        "name": "models.routing.changed",
        "delivery": "live-only-best-effort",
        "schemaVersion": 1,
        "payload": "#/$defs/ModelsRoutingChangedPayload",
    }
    required = event.document["$defs"]["ModelsRoutingChangedPayload"]["required"]
    assert required == [
        "mode",
        "router_enabled",
        "ensemble_enabled",
        "rollout_phase",
        "selection_mode",
        "selection_configured",
        "activation_preview",
        "router_required_by_ensemble",
        "image_input",
        "applies_to",
        "capabilities_by_mode",
        "source",
    ]


def test_generated_models_accept_real_public_shapes_and_reject_private_undo_data() -> None:
    from opensquilla.contracts.generated.v4.memory_import_start import (
        MemoryImportStartResult,
    )
    from opensquilla.contracts.generated.v4.memory_import_undo import (
        MemoryImportUndoResult,
    )
    from opensquilla.contracts.generated.v4.models_routing_changed_event import (
        ModelsRoutingChangedPayload,
    )
    from opensquilla.contracts.generated.v4.onboarding_channel_upsert import (
        OnboardingChannelUpsertResult,
    )
    from opensquilla.contracts.generated.v4.plans_capabilities import (
        PlansCapabilitiesParams,
    )

    MemoryImportStartResult.model_validate(
        {
            "schemaVersion": 1,
            "jobId": "job-1",
            "batchId": "batch-1",
            "agentId": "main",
            "status": "queued",
            "stage": "reading",
            "provider": "synthetic",
            "model": "synthetic-model",
            "createdAt": "2026-09-03T00:00:00Z",
            "updatedAt": "2026-09-03T00:00:00Z",
            "expiresAt": "2026-09-04T00:00:00Z",
            "startedAt": None,
            "finishedAt": None,
            "attemptCount": 0,
            "previewId": None,
            "errorCode": None,
            "errorMessage": None,
            "canRetry": False,
            "preview": None,
        }
    )
    with pytest.raises(ValidationError):
        MemoryImportUndoResult.model_validate(
            {
                "schemaVersion": 1,
                "status": "reviewRequired",
                "receiptId": "receipt-1",
                "indexStatus": None,
                "preview": None,
                "agentId": "main",
                "reviewContext": {"private": "must-not-cross-the-wire"},
            }
        )

    assert PlansCapabilitiesParams.model_validate("legacy-param").root == "legacy-param"
    OnboardingChannelUpsertResult.model_validate(
        {
            "changed": True,
            "restartRequired": False,
            "liveApply": {"telegram": "rebuilt"},
            "configPath": "/synthetic/config.toml",
            "entry": {"name": "telegram", "token": "[redacted]"},
            "warnings": [],
        }
    )
    image_input = {"admission": "allowed", "reason": "provider_supports_images"}
    ModelsRoutingChangedPayload.model_validate(
        {
            "mode": "direct",
            "router_enabled": False,
            "ensemble_enabled": False,
            "rollout_phase": "observe",
            "selection_mode": "",
            "selection_configured": False,
            "activation_preview": {
                "selection_mode": "",
                "proposer_count": 0,
                "member_providers": [],
                "candidates": [],
                "blocked_reason": "llm_ensemble_missing",
            },
            "router_required_by_ensemble": True,
            "image_input": image_input,
            "applies_to": "next_accepted_turn",
            "capabilities_by_mode": {
                "direct": {"image_input": image_input},
                "router": {"image_input": image_input},
                "ensemble": {"image_input": image_input},
            },
            "source": "config.patch",
        }
    )
