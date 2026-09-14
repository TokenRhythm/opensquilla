"""The Run Context an operator-invoked network diagnostic runs under.

Lives outside the ``rpc_*`` modules because both ``rpc_tools`` (``search.status``,
``search.query``) and ``rpc_doctor`` (``doctor.status``) need it, and a
production module may not import an RPC implementation — the loader is the only
place allowed to reach across, and ``test_production_cross_rpc_imports_are_
forbidden_outside_loader`` enforces it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from opensquilla.run_mode import RunMode
from opensquilla.sandbox.integration import get_runtime
from opensquilla.sandbox.policy_store import pin_sandbox_policy
from opensquilla.sandbox.run_context import RunContext
from opensquilla.tools.types import ToolContext, current_tool_context

log = structlog.get_logger(__name__)


def operator_network_tool_context(config: Any) -> ToolContext:
    """Build the context, pinned to the deployment's persisted network policy.

    ``search.query`` reaches the same in-process network path as the chat
    ``web_search`` tool, but an RPC never enters tool dispatch, so nothing sets
    ``current_tool_context`` and ``current_tool_run_context()`` returns ``None``.
    Under ``NetworkMode.PROXY_ALLOWLIST`` that is refused before the provider is
    reached, which is why the same gateway answers Web Chat and refuses the CLI
    (#1202).

    The context authorizes nothing by itself: no mounts, no domains, no
    public-network grant. It is the carrier the proxy path needs —
    :func:`run_in_process_network_action` still resolves the policy, still mints
    one fingerprinted ``expires_after="once"`` grant per action, and still puts
    every host through ``NetworkApprovalService``.

    The run mode is pinned to Safe, and deliberately not read from the gateway
    config. A tool context is not a description of the deployment: every guard
    that calls ``full_host_access_active()`` reads the run mode off whatever
    context is current, so publishing ``full`` here would stand down protections
    that have nothing to do with the network proxy — including the
    sensitive-payload guard that stops a query containing secrets from being
    sent to a search provider. Safe cannot widen anything, and it does not
    weaken the network decision either: the mode that resolves the policy comes
    from the graded ``SecurityLevel``, not from this field.
    """

    runtime = get_runtime()
    workspace = str(getattr(runtime, "workspace", None) or "") or None
    # Only the fields this path consumes are set. `caller_kind` and the tool
    # allow/deny lists drive tool-list building, which an RPC never does, and a
    # plausible-looking value there would be a claim nothing checks.
    context = ToolContext(
        workspace_dir=workspace,
        run_mode=RunMode.SAFE.value,
        sandbox_run_context=RunContext(
            run_mode=RunMode.SAFE,
            workspace=workspace,
            source="operator_rpc",
        ),
        source_kind="rpc",
        source_name="search",
    )
    # The persisted network policy, pinned the way turn ingress pins it. Without
    # it `active_sandbox_policy()` finds nothing on the context and falls back to
    # a blank `StoredSandboxPolicy`, so the deployment's own deny list and
    # `block_all_network` would not be applied to traffic this context makes
    # reachable — the RPC would end up with more authority than the chat tool it
    # is being brought level with, which is the opposite of the point.
    pin_sandbox_policy(context, config)
    return context


@contextmanager
def operator_network_context(config: Any) -> Iterator[None]:
    """Run the block under the operator Run Context, or unchanged if it cannot be built.

    Failing to build the context must not fail the caller: for ``search.query``
    the fallback is the previous refusal, and ``search.status`` is a diagnostic
    surface that has to keep answering even when the sandbox cannot be read.
    """

    try:
        token = current_tool_context.set(operator_network_tool_context(config))
    except Exception:  # noqa: BLE001 - fail closed to the pre-fix refusal
        # Reaching the network without the policy that governs it would be worse
        # than not reaching it, so a context that cannot be built completely is
        # not published at all: the block runs as it did before this fix, which
        # is a refusal under a managed-network posture.
        log.warning("search.operator_network_context_unavailable", exc_info=True)
        yield
        return
    try:
        yield
    finally:
        current_tool_context.reset(token)


__all__ = ["operator_network_context", "operator_network_tool_context"]
