"""Wire-shape tests for generated SandboxRuntime Contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opensquilla.contracts.generated.v4.gateway_contract_registry import (
    GATEWAY_EVENT_CONTRACTS,
    GATEWAY_METHOD_CONTRACTS,
)

EXPECTED_SCOPES = {
    "sandbox.setup.status": "operator.read",
    "sandbox.setup.ensure": "operator.write",
    "sandbox.capability.status": "operator.read",
    "sandbox.policy.get": "operator.read",
    "sandbox.policy.defaults": "operator.read",
    "sandbox.policy.update": "operator.write",
    "sandbox.run_mode.preference.get": "operator.read",
    "sandbox.run_mode.preference.set": "operator.write",
    "sandbox.runtime.status": "operator.read",
    "sandbox.runtime.install": "operator.admin",
    "sandbox.runtime.cancel": "operator.admin",
    "sandbox.runtime.remove": "operator.admin",
    "sandbox.runtime.discard_download": "operator.admin",
    "sandbox.resume": "operator.write",
}

EXPECTED_SPECIAL_ERRORS = {
    "sandbox.policy.update": {"POLICY_VERSION_CONFLICT"},
    "sandbox.run_mode.preference.set": {"SANDBOX_CAPABILITY_UNAVAILABLE"},
    "sandbox.runtime.install": {"RUNTIME_JOB_CONFLICT"},
    "sandbox.runtime.cancel": {"RUNTIME_JOB_CONFLICT"},
    "sandbox.runtime.remove": {"RUNTIME_JOB_CONFLICT"},
    "sandbox.runtime.discard_download": {
        "RUNTIME_DISCARD_FAILED",
        "RUNTIME_JOB_CONFLICT",
    },
}


def _runtime_status() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "managementSupported": True,
        "target": "darwin-arm64",
        "catalogVersion": "2026-08-21.2",
        "sourceOrder": ["github", "oss"],
        "components": [],
        "nextPollAfterMs": 5_000,
    }


def test_registry_freezes_method_scope_guest_policy_and_errors() -> None:
    for method, expected_scope in EXPECTED_SCOPES.items():
        descriptor = GATEWAY_METHOD_CONTRACTS[method]
        codes = {str(error["code"]) for error in descriptor.errors}

        assert descriptor.name == method
        assert descriptor.scope == expected_scope
        assert descriptor.guest_allowed is False
        assert descriptor.capability == {
            "kind": "method-availability",
            "name": method,
        }
        assert {"INVALID_REQUEST", "UNAUTHORIZED", "INTERNAL_ERROR"} <= codes
        assert EXPECTED_SPECIAL_ERRORS.get(method, set()) <= codes


def test_run_mode_event_freezes_canonical_name_delivery_and_payload() -> None:
    descriptor = GATEWAY_EVENT_CONTRACTS["sandbox.run_mode.preference.changed"]

    assert descriptor.delivery == "live-only-best-effort"
    assert descriptor.schema_version == 1
    descriptor.payload_model.model_validate(
        {"runMode": "safe", "source": "preference", "future": True}
    )
    with pytest.raises(ValidationError):
        descriptor.payload_model.model_validate(
            {"runMode": "managed", "source": "preference"}
        )


def test_setup_result_keeps_legacy_requires_admin_alias_and_additive_fields() -> None:
    model = GATEWAY_METHOD_CONTRACTS["sandbox.setup.status"].result_model

    model.model_validate(
        {
            "state": "not_setup",
            "platform": "win32",
            "message": "Setup is required.",
            "requires_admin": True,
            "future": {"additive": True},
        }
    )
    model.model_validate(
        {
            "state": "ready",
            "platform": "darwin",
            "message": "Ready.",
            "requiresAdmin": False,
        }
    )
    with pytest.raises(ValidationError):
        model.model_validate(
            {"state": "ready", "platform": "darwin", "message": "Ready."}
        )


def test_run_mode_params_keep_all_normalizer_aliases_but_result_is_canonical() -> None:
    descriptor = GATEWAY_METHOD_CONTRACTS["sandbox.run_mode.preference.set"]
    aliases = (
        "safe",
        "on",
        "off",
        "bypass",
        "standard",
        "standard-sandbox",
        "standard_sandbox",
        "trust",
        "trusted",
        "trusted-sandbox",
        "trusted_sandbox",
        "managed",
        "full",
        "full-host-access",
        "full_host_access",
    )

    for alias in aliases:
        descriptor.params_model.model_validate({"runMode": alias})
    with pytest.raises(ValidationError):
        descriptor.params_model.model_validate({"runMode": "unknown"})
    with pytest.raises(ValidationError):
        descriptor.result_model.model_validate(
            {"runMode": "trusted", "source": "preference"}
        )


def test_runtime_status_accepts_canonical_and_legacy_envelopes() -> None:
    model = GATEWAY_METHOD_CONTRACTS["sandbox.runtime.status"].result_model
    status = _runtime_status()

    model.model_validate(status)
    model.model_validate({"status": status})
    model.model_validate({"runtimeStatus": status})
    with pytest.raises(ValidationError):
        model.model_validate({"schemaVersion": 1, "components": []})


def test_policy_result_requires_complete_canonical_core_but_allows_extensions() -> None:
    model = GATEWAY_METHOD_CONTRACTS["sandbox.policy.get"].result_model
    policy = {
        "schemaVersion": 2,
        "policyVersion": 3,
        "files": {
            "customDenyWritePaths": [],
            "recursiveDeleteBackupEnabled": True,
            "backupQuotaBytes": 1024,
        },
        "commands": {
            "requireApprovalPrefixes": [],
            "autoAllowPrefixes": [],
            "systemTools": "auto",
        },
        "network": {
            "blockAllNetwork": False,
            "allowDomains": [],
            "denyDomains": [],
        },
        "runtimes": {
            "enabled": True,
            "python": True,
            "node": True,
            "gitBash": True,
        },
        "future": "additive",
    }

    model.model_validate(policy)
    with pytest.raises(ValidationError):
        model.model_validate({key: value for key, value in policy.items() if key != "network"})
