"""Tool dispatch policy pipeline.

The dispatch pipeline is composed of
declarative chain of :class:`PolicyCheck` objects. Each check inspects a
:class:`DispatchInput` and returns either an allowing or denying
:class:`PolicyDecision`. The chain runs in document order; the first denial wins.

Re-exports for backwards-compatible imports of ``private_memory_read_tool_denied``
and ``private_memory_read_tools_blocked`` come from
``opensquilla.tools.policy_helpers`` so callers that previously imported them
from ``opensquilla.tools.policy`` continue to work.
"""

from __future__ import annotations

from opensquilla.tools.policy.chain import POLICY_CHAIN, run_chain, run_chain_with_emit
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
from opensquilla.tools.policy.finalize import finalize
from opensquilla.tools.policy.types import DispatchInput, PolicyCheck, PolicyDecision

# Chain primitives — public surface of the policy pipeline.
__all__ = [
    "AllowListPolicy",
    "DenyListPolicy",
    "ExclusiveToolCeilingPolicy",
    "GuestSafePolicy",
    "DispatchInput",
    "OwnerOnlyPolicy",
    "POLICY_CHAIN",
    "PermissionMatrixPolicy",
    "PolicyCheck",
    "PolicyDecision",
    "PrivateMemoryScopePolicy",
    "ProfilePolicy",
    "finalize",
    "run_chain",
    "run_chain_with_emit",
]

# Legacy re-exports — kept for backwards compatibility so existing call sites
# importing these names from ``opensquilla.tools.policy`` continue to work.
# They are intentionally NOT in ``__all__`` so ``from ... import *`` only
# surfaces the chain primitives. New code should import these directly from
# their owning boundary modules.
from opensquilla.tools.policy_helpers import (  # noqa: E402, F401
    ToolPolicy,
    apply_tool_policy,
    apply_tool_policy_from_config,
    apply_tool_policy_layer,
)
from opensquilla.tools.policy_runtime import (  # noqa: E402, F401
    ToolSurfaceCapabilities,
    detect_runtime_tool_surface_capabilities,
    private_memory_read_tool_denied,
    private_memory_read_tools_blocked,
    resolve_runtime_tool_surface,
    tool_surface_capabilities_from_runtime,
)
