"""Ordered chain of policy checks.

The chain order is owner_only first, then guest_safe and denied_tools before
private_memory_scope, and so on. The
:func:`run_chain` function returns the first denying decision (the
"first denial wins" contract codified in
``test_dispatch_properties.test_first_denial_wins_*``).

:func:`run_chain_with_emit` adds a logger callback so the orchestrator does
not need to inline its own copy of the loop just to emit log events. This is
the single chain-execution site that the ``ToolHook.before_tool`` hook
sits in front of.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from opensquilla.tools.policy.checks import (
    AllowListPolicy,
    DenyListPolicy,
    ExclusiveToolCeilingPolicy,
    GuestSafePolicy,
    OwnerOnlyPolicy,
    PermissionMatrixPolicy,
    PrivateMemoryScopePolicy,
    ProfilePolicy,
)
from opensquilla.tools.policy.types import DispatchInput, PolicyCheck, PolicyDecision

POLICY_CHAIN: tuple[PolicyCheck, ...] = cast(tuple[PolicyCheck, ...], (
    OwnerOnlyPolicy(),
    GuestSafePolicy(),
    DenyListPolicy(),
    PrivateMemoryScopePolicy(),
    ExclusiveToolCeilingPolicy(),
    AllowListPolicy(),
    ProfilePolicy(),
    PermissionMatrixPolicy(),
))

PolicyLogEmitter = Callable[[dict], None]

def run_chain(d: DispatchInput) -> PolicyDecision:
    """Run the chain in order; return the first denial or an allow."""
    for check in POLICY_CHAIN:
        decision = check.evaluate(d)
        if not decision.allowed:
            return decision
    return PolicyDecision(allowed=True)

def run_chain_with_emit(
    d: DispatchInput,
    emit: PolicyLogEmitter | None = None,
) -> PolicyDecision:
    """Run the chain and emit any denial log_event through ``emit``.

    The orchestrator passes ``emit=log.warning``-equivalent so the structured
    log entry is written from a single seam rather than re-implemented inside
    the orchestrator. ``emit`` is invoked at most once per call (first denial
    wins, same as :func:`run_chain`).
    """

    for check in POLICY_CHAIN:
        decision = check.evaluate(d)
        if decision.allowed:
            continue
        if emit is not None and decision.log_event is not None:
            emit(decision.log_event)
        return decision
    return PolicyDecision(allowed=True)
