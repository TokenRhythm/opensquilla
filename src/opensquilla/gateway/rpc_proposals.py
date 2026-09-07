"""Proposal RPC composition over the SkillProposalReview boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opensquilla.gateway.adapters.skill_proposal_review import (
    FilesystemSkillProposalStore,
    GatewayAutoProposalRuntime,
    GatewaySkillProposalReviewAdapter,
)
from opensquilla.gateway.adapters.skill_proposal_review_contract import (
    register_skill_proposal_review_contract,
)
from opensquilla.gateway.auto_propose_bridge import get_runtime
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.paths import default_opensquilla_home
from opensquilla.skills import proposals_lib

_d = get_dispatcher()


def _home() -> Path:
    from opensquilla.gateway.auto_propose_bridge import get_runtime

    rt = get_runtime()
    if rt is not None:
        return rt.home
    return default_opensquilla_home()


def _invalidate_loader(ctx: RpcContext) -> None:
    loader = getattr(ctx, "skill_loader", None)
    if loader is not None:
        invalidate = getattr(loader, "invalidate_cache", None)
        if invalidate is not None:
            invalidate()


@_d.method("exec.proposals.pending_count", scope="operator.proposals")
async def _handle_pending_count(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return proposals_lib.pending_count(_home())


async def _handle_list(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).list_proposals(params)


async def _handle_show(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).show_proposal(params)


async def _handle_accept(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).accept_proposal(params)


async def _handle_reject(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).reject_proposal(params)


async def _handle_auto_enabled_list(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).list_auto_enabled(params)


async def _handle_auto_enabled_disable(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).disable_auto_enabled(params)


async def _handle_settings_get(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).settings(params)


async def _handle_settings_set(
    params: dict | None, ctx: RpcContext,
) -> dict[str, Any]:
    return await _skill_proposal_review(ctx).update_settings(params)


def _skill_proposal_review(ctx: RpcContext) -> GatewaySkillProposalReviewAdapter:
    return GatewaySkillProposalReviewAdapter(
        FilesystemSkillProposalStore(
            _home(), invalidate_catalog=lambda: _invalidate_loader(ctx)
        ),
        GatewayAutoProposalRuntime(get_runtime()),
    )


for _proposal_method, _proposal_implementation in (
    ("exec.proposals.list", _handle_list),
    ("exec.proposals.show", _handle_show),
    ("exec.proposals.accept", _handle_accept),
    ("exec.proposals.reject", _handle_reject),
    ("exec.proposals.auto_enabled.list", _handle_auto_enabled_list),
    ("exec.proposals.auto_enabled.disable", _handle_auto_enabled_disable),
    ("exec.proposals.settings.get", _handle_settings_get),
    ("exec.proposals.settings.set", _handle_settings_set),
):
    register_skill_proposal_review_contract(
        _d,
        _proposal_method,
        _proposal_implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
