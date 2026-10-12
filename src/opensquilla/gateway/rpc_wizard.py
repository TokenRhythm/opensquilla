"""Wizard domain RPC handlers.

These four handlers are the wire surface for the onboard wizard state
machine. The heavy lifting — typed schemas, state transitions, validation
— lives in :mod:`opensquilla.application.wizard`. This module is a thin translator
between the JSON-over-RPC shape (camelCase field names) and the Python
registry (snake_case attributes).

Scope on every method is ``operator.admin``. The registry is in-memory and
process-local; session persistence is deferred to a future slice.
"""

from __future__ import annotations

from typing import Any

from opensquilla.application.wizard import get_wizard_registry
from opensquilla.application.wizard_rpc import (
    wizard_cancel_rpc_payload,
    wizard_next_rpc_payload,
    wizard_start_rpc_payload,
    wizard_status_rpc_payload,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

_d = get_dispatcher()


@_d.method("wizard.start", scope="operator.admin")
async def _handle_wizard_start(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "wizardType" not in params:
        raise ValueError("params.wizardType is required")
    wizard_type = params["wizardType"]
    if not isinstance(wizard_type, str) or not wizard_type:
        raise ValueError("params.wizardType must be a non-empty string")

    registry = get_wizard_registry()
    wizard_id, first_step = registry.start(wizard_type)
    return wizard_start_rpc_payload(wizard_id, first_step)


@_d.method("wizard.next", scope="operator.admin")
async def _handle_wizard_next(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "wizardId" not in params:
        raise ValueError("params.wizardId is required")
    wizard_id = params["wizardId"]
    if not isinstance(wizard_id, str) or not wizard_id:
        raise ValueError("params.wizardId must be a non-empty string")

    answers_raw = params.get("answers", {})
    if not isinstance(answers_raw, dict):
        raise ValueError("params.answers must be an object")
    # The typed contract on the registry side is dict[str, str|int|bool];
    # coerce Nones out so "missing required" is the sole path for absent keys.
    answers: dict[str, str | int | bool] = {
        k: v for k, v in answers_raw.items() if v is not None and isinstance(v, (str, int, bool))
    }

    registry = get_wizard_registry()
    outcome = registry.advance(wizard_id, answers)
    return wizard_next_rpc_payload(outcome)


@_d.method("wizard.cancel", scope="operator.admin")
async def _handle_wizard_cancel(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "wizardId" not in params:
        raise ValueError("params.wizardId is required")
    wizard_id = params["wizardId"]
    if not isinstance(wizard_id, str) or not wizard_id:
        raise ValueError("params.wizardId must be a non-empty string")

    registry = get_wizard_registry()
    registry.cancel(wizard_id)
    return wizard_cancel_rpc_payload(wizard_id)


@_d.method("wizard.status", scope="operator.admin")
async def _handle_wizard_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "wizardId" not in params:
        raise ValueError("params.wizardId is required")
    wizard_id = params["wizardId"]
    if not isinstance(wizard_id, str) or not wizard_id:
        raise ValueError("params.wizardId must be a non-empty string")

    registry = get_wizard_registry()
    session = registry.status(wizard_id)
    return wizard_status_rpc_payload(
        session,
        total_steps=registry.total_steps(session.wizard_type),
    )
