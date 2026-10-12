"""RPC payload helpers for wizard application surfaces."""

from __future__ import annotations

from typing import Any

from opensquilla.application.wizard import WizardSession, WizardStep


def wizard_start_rpc_payload(wizard_id: str, first_step: WizardStep) -> dict[str, Any]:
    """Build the RPC wire payload for ``wizard.start``."""

    return {
        "wizardId": wizard_id,
        "step": first_step.to_dict(),
    }


def wizard_next_rpc_payload(outcome: Any) -> dict[str, Any]:
    """Build the RPC wire payload for ``wizard.next``."""

    next_step = getattr(outcome, "next_step", None)
    return {
        "step": next_step.to_dict() if next_step is not None else None,
        "completed": outcome.completed,
        "result": outcome.result,
    }


def wizard_cancel_rpc_payload(wizard_id: str) -> dict[str, Any]:
    """Build the RPC wire payload for ``wizard.cancel``."""

    return {"wizardId": wizard_id, "cancelled": True}


def wizard_status_rpc_payload(
    session: WizardSession,
    *,
    total_steps: int,
) -> dict[str, Any]:
    """Build the RPC wire payload for ``wizard.status``."""

    return session.to_dict(total_steps=total_steps)


__all__ = [
    "wizard_cancel_rpc_payload",
    "wizard_next_rpc_payload",
    "wizard_start_rpc_payload",
    "wizard_status_rpc_payload",
]
