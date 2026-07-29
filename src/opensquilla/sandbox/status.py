"""Shared sandbox posture status payloads."""

from __future__ import annotations

from typing import Any

from opensquilla.sandbox.default_allowlist import default_allowlist_payload
from opensquilla.sandbox.package_bundles import PACKAGE_BUNDLES
from opensquilla.sandbox.run_mode import (
    RunMode,
    config_run_mode,
    display_name,
    execution_target,
    project_default_run_mode,
    sandbox_runtime_capability_mode,
)


def posture(config: Any) -> str:
    return config_run_mode(config).value


def status_payload(config: Any, *, restart_required: bool = False) -> dict[str, Any]:
    run_mode = config_run_mode(config)
    project_default = project_default_run_mode(config)
    runtime_capability = sandbox_runtime_capability_mode(config)
    sandbox_cfg = config.sandbox
    network_default = str(getattr(sandbox_cfg, "network_default", "none"))
    target = execution_target(run_mode)
    sandbox_enabled = target == "sandbox" and bool(sandbox_cfg.sandbox)
    security_grading = target == "sandbox" and bool(sandbox_cfg.security_grading)
    permissions_default_mode = str(config.permissions.default_mode)
    managed_network = (
        "ready"
        if target == "sandbox"
        and sandbox_enabled
        and network_default == "proxy_allowlist"
        else "inactive" if target == "host" else "blocked"
    )
    return {
        "run_mode": run_mode.value,
        "run_mode_label": display_name(run_mode),
        "execution_target": target,
        "owner_execution_target": target,
        "sandbox_required_for_owner_default": target == "sandbox",
        "sandbox_backend_configured": str(getattr(sandbox_cfg, "backend", "auto")),
        "project_default_run_mode": project_default.value,
        "runtime_capability_run_mode": runtime_capability.value,
        "runtime_sandbox_required": runtime_capability is not RunMode.FULL,
        "posture": run_mode.value,
        "backend": str(getattr(sandbox_cfg, "backend", "auto")),
        "managed_network": managed_network,
        "sandbox": {
            "sandbox": sandbox_enabled,
            "security_grading": security_grading,
            "network_default": network_default,
        },
        "default_allowlist": default_allowlist_payload(),
        "bundle_catalog": [
            {
                "bundle_id": bundle_id,
                "domains": list(domains),
                "enabled_by_default": True,
            }
            for bundle_id, domains in PACKAGE_BUNDLES.items()
        ],
        "permissions": {
            "default_mode": permissions_default_mode,
            "effective_mode": "full" if target == "host" else run_mode.value,
        },
        "restart_required": restart_required,
    }
