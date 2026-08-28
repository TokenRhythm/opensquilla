"""Principal-aware sandbox run-mode authorization helpers."""

from __future__ import annotations

from typing import Any

from opensquilla.sandbox.run_mode import RunMode, normalize_run_mode

_OWNER_ALLOWED_RUN_MODES = (RunMode.SAFE, RunMode.FULL)
_NON_OWNER_ALLOWED_RUN_MODES = (RunMode.SAFE,)


def principal_is_owner(principal: Any) -> bool:
    return getattr(principal, "is_owner", False) is True


def principal_has_host_execute(principal: Any) -> bool:
    capabilities = getattr(principal, "capabilities", ())
    return "host.execute" in capabilities


def allowed_run_modes_for_principal(principal: Any) -> tuple[RunMode, ...]:
    if principal_has_host_execute(principal):
        return _OWNER_ALLOWED_RUN_MODES
    return _NON_OWNER_ALLOWED_RUN_MODES


def default_run_mode_for_principal(principal: Any) -> RunMode:
    return RunMode.FULL if principal_has_host_execute(principal) else RunMode.SAFE


def run_mode_allowed_for_principal(mode: Any, principal: Any) -> bool:
    try:
        normalized = normalize_run_mode(mode, default=default_run_mode_for_principal(principal))
    except ValueError:
        return False
    return normalized in allowed_run_modes_for_principal(principal)


def coerce_run_mode_for_principal(mode: Any, principal: Any) -> RunMode:
    default = default_run_mode_for_principal(principal)
    try:
        normalized = normalize_run_mode(mode, default=default)
    except ValueError:
        # Missing values inherit the principal default through normalize_run_mode,
        # but malformed values are never allowed to fail open for host owners.
        return RunMode.SAFE
    if normalized in allowed_run_modes_for_principal(principal):
        return normalized
    return default


def principal_payload(principal: Any) -> dict[str, Any]:
    scopes = getattr(principal, "scopes", ())
    capabilities = getattr(principal, "capabilities", ())
    return {
        "role": getattr(principal, "role", None),
        "scopes": sorted(str(scope) for scope in scopes),
        "capabilities": sorted(str(capability) for capability in capabilities),
        "isOwner": principal_is_owner(principal),
        "authenticated": bool(getattr(principal, "authenticated", False)),
        "authState": getattr(principal, "auth_state", None),
        "tokenPublicId": getattr(principal, "token_public_id", None),
    }


def run_mode_policy_payload(principal: Any) -> dict[str, Any]:
    allowed = allowed_run_modes_for_principal(principal)
    return {
        "allowedRunModes": [mode.value for mode in allowed],
        "defaultRunMode": default_run_mode_for_principal(principal).value,
        "fullHostAccessDisabledReason": (
            None if principal_has_host_execute(principal) else "host_capability_required"
        ),
    }


def hello_auth_payload(principal: Any) -> dict[str, Any]:
    return {
        "principal": principal_payload(principal),
        "runModePolicy": run_mode_policy_payload(principal),
    }


__all__ = [
    "allowed_run_modes_for_principal",
    "coerce_run_mode_for_principal",
    "default_run_mode_for_principal",
    "hello_auth_payload",
    "principal_has_host_execute",
    "principal_is_owner",
    "principal_payload",
    "run_mode_allowed_for_principal",
    "run_mode_policy_payload",
]
