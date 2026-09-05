"""Routing policy public surface with dependency-light lazy exports.

Importing a focused submodule (notably the headless Benchmark worker) must not
eagerly import the Provider stack through :mod:`.policy`.  Public package
attributes retain their existing names and are resolved on first access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.engine.routing.calibration import (
        CalibrationState,
        aggregate_calibration,
        apply_bias,
        calibration_path,
        effective_threshold,
        load_calibration,
        save_calibration,
    )
    from opensquilla.engine.routing.policy import (
        AntiDowngradeResult,
        BudgetGateInput,
        BudgetGateResult,
        CapabilityGateAction,
        CapabilityGateResult,
        ComplaintUpgradeResult,
        ConfidenceGateResult,
        PolicyInputs,
        PolicyResult,
        ProviderMismatchOutcome,
        ProviderMismatchVeto,
        RoutingDecision,
        RoutingPolicyEngine,
        TierCapability,
        anti_downgrade,
        apply_budget_gate,
        bind,
        budget_gate,
        capability_gate,
        complaint_upgrade,
        confidence_gate,
        detect_complaint,
        large_context_floor,
        large_context_min_tier,
        previous_final_entry,
        previous_final_tier,
        provider_mismatch,
        provider_mismatch_veto,
        reconcile_controller_with_final_tier,
        record_budget_gate_trail,
        record_capability_gate_trail,
        record_provider_mismatch_veto_trail,
        route_class_for_tier,
        tier_for_route_class,
    )

_CALIBRATION_EXPORTS = {
    "CalibrationState",
    "aggregate_calibration",
    "apply_bias",
    "calibration_path",
    "effective_threshold",
    "load_calibration",
    "save_calibration",
}

_POLICY_EXPORTS = {
    "AntiDowngradeResult",
    "BudgetGateInput",
    "BudgetGateResult",
    "CapabilityGateAction",
    "CapabilityGateResult",
    "ComplaintUpgradeResult",
    "ConfidenceGateResult",
    "PolicyInputs",
    "PolicyResult",
    "ProviderMismatchOutcome",
    "ProviderMismatchVeto",
    "RoutingDecision",
    "RoutingPolicyEngine",
    "TierCapability",
    "anti_downgrade",
    "apply_budget_gate",
    "bind",
    "budget_gate",
    "capability_gate",
    "complaint_upgrade",
    "confidence_gate",
    "detect_complaint",
    "large_context_floor",
    "large_context_min_tier",
    "previous_final_entry",
    "previous_final_tier",
    "provider_mismatch",
    "provider_mismatch_veto",
    "reconcile_controller_with_final_tier",
    "record_budget_gate_trail",
    "record_capability_gate_trail",
    "record_provider_mismatch_veto_trail",
    "route_class_for_tier",
    "tier_for_route_class",
}


def __getattr__(name: str) -> Any:
    if name in _CALIBRATION_EXPORTS:
        module = importlib.import_module("opensquilla.engine.routing.calibration")
    elif name in _POLICY_EXPORTS:
        module = importlib.import_module("opensquilla.engine.routing.policy")
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "AntiDowngradeResult",
    "BudgetGateInput",
    "BudgetGateResult",
    "CalibrationState",
    "CapabilityGateAction",
    "CapabilityGateResult",
    "ComplaintUpgradeResult",
    "ConfidenceGateResult",
    "PolicyInputs",
    "PolicyResult",
    "ProviderMismatchOutcome",
    "ProviderMismatchVeto",
    "RoutingDecision",
    "RoutingPolicyEngine",
    "TierCapability",
    "aggregate_calibration",
    "anti_downgrade",
    "apply_bias",
    "apply_budget_gate",
    "bind",
    "budget_gate",
    "calibration_path",
    "capability_gate",
    "complaint_upgrade",
    "confidence_gate",
    "detect_complaint",
    "effective_threshold",
    "large_context_floor",
    "large_context_min_tier",
    "load_calibration",
    "previous_final_entry",
    "previous_final_tier",
    "provider_mismatch",
    "provider_mismatch_veto",
    "reconcile_controller_with_final_tier",
    "record_budget_gate_trail",
    "record_capability_gate_trail",
    "record_provider_mismatch_veto_trail",
    "route_class_for_tier",
    "save_calibration",
    "tier_for_route_class",
]
