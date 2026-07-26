"""Gateway RPC package.

This package preserves the former flat RPC import surface verbatim: every existing

    from opensquilla.gateway.rpc import RpcContext, RpcDispatcher, get_dispatcher

still resolves, because the names are re-exported from
:mod:`opensquilla.gateway.rpc.registry`. The package additionally exposes
:class:`RpcRegistry` and :func:`get_registry` as the canonical names —
``RpcDispatcher``/``get_dispatcher`` are thin shims.

Sibling modules ``opensquilla.gateway.rpc_*`` live outside this package and are
imported below so their handler registrations execute at boot time, which
is what the original ``rpc.py`` did.
"""

from __future__ import annotations

from opensquilla.gateway.rpc.registry import (
    RpcContext,
    RpcDispatcher,
    RpcHandlerError,
    RpcHandlerFn,
    RpcMethodEntry,
    RpcRegistry,
    RpcUnavailableError,
    ScopeDriftError,
    get_dispatcher,
    get_registry,
    validate_classification,
)

__all__ = [
    "RpcContext",
    "RpcDispatcher",
    "RpcHandlerError",
    "RpcHandlerFn",
    "RpcMethodEntry",
    "RpcRegistry",
    "RpcUnavailableError",
    "ScopeDriftError",
    "get_dispatcher",
    "get_registry",
    "validate_classification",
]

# Import sibling submodules to trigger handler registration against the
# module-level singleton. The import surface intentionally omits the deprecated
# product RPC methods listed in ``REMOVED_PRODUCT_METHODS`` in
# tests/test_gateway/test_rpc_extended.py. These methods MUST NOT register
# handlers at boot — the release surface is contracted to reject them with
# METHOD_NOT_FOUND.
import opensquilla.gateway.rpc_agents  # noqa: E402, F401
import opensquilla.gateway.rpc_approvals  # noqa: E402, F401
import opensquilla.gateway.rpc_channels  # noqa: E402, F401
import opensquilla.gateway.rpc_chat  # noqa: E402, F401
import opensquilla.gateway.rpc_commands  # noqa: E402, F401
import opensquilla.gateway.rpc_config  # noqa: E402, F401
import opensquilla.gateway.rpc_cron  # noqa: E402, F401
import opensquilla.gateway.rpc_diagnostics  # noqa: E402, F401
import opensquilla.gateway.rpc_doctor  # noqa: E402, F401
import opensquilla.gateway.rpc_logs  # noqa: E402, F401
import opensquilla.gateway.rpc_memory  # noqa: E402, F401
import opensquilla.gateway.rpc_meta_runs  # noqa: E402, F401
import opensquilla.gateway.rpc_migration  # noqa: E402, F401
import opensquilla.gateway.rpc_models  # noqa: E402, F401
import opensquilla.gateway.rpc_onboarding  # noqa: E402, F401
import opensquilla.gateway.rpc_proposals  # noqa: E402, F401
import opensquilla.gateway.rpc_router  # noqa: E402, F401
import opensquilla.gateway.rpc_routing  # noqa: E402, F401
import opensquilla.gateway.rpc_sandbox  # noqa: E402, F401
import opensquilla.gateway.rpc_secrets  # noqa: E402, F401
import opensquilla.gateway.rpc_sessions  # noqa: E402, F401
import opensquilla.gateway.rpc_skills  # noqa: E402, F401
import opensquilla.gateway.rpc_system  # noqa: E402, F401
import opensquilla.gateway.rpc_tools  # noqa: E402, F401
import opensquilla.gateway.rpc_usage  # noqa: E402, F401
import opensquilla.gateway.rpc_wizard  # noqa: E402, F401

# Fail fast if any registered handler disagrees with ``gateway.scopes``.
validate_classification()
