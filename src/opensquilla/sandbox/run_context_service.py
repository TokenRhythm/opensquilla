"""Validated mutation helpers for sandbox Run Context."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from opensquilla.sandbox import user_grants
from opensquilla.sandbox.domain_validation import validate_domain_pattern
from opensquilla.sandbox.network_guard import decide_network_access
from opensquilla.sandbox.package_bundles import expand_package_bundle
from opensquilla.sandbox.path_validation import (
    decide_path_access,
    normalize_mount_access,
    normalize_path,
)
from opensquilla.sandbox.run_context import (
    DomainGrant,
    MountGrant,
    PackageBundleGrant,
    PublicNetworkGrant,
    RunContext,
    get_run_context,
    normalize_scope,
    normalize_workspace_path,
    persist_run_context,
)
from opensquilla.sandbox.run_mode import RunMode


def _normalize_bundle_id(bundle_id: Any) -> str:
    return str(bundle_id or "").strip()


def _upsert_user_mount_grant(grant: MountGrant) -> None:
    user_grants.upsert_mount_grant(
        {"path": grant.path, "access": grant.access, "scope": grant.scope}
    )


def _mount_grant_storage_path(path: str) -> str:
    return str(normalize_path(path))


def _upsert_user_domain_grant(grant: DomainGrant) -> None:
    user_grants.upsert_domain_grant(
        {
            "domain": grant.domain,
            "scope": grant.scope,
            "source": grant.source,
        }
    )


def _upsert_user_bundle_grant(grant: PackageBundleGrant) -> None:
    user_grants.upsert_bundle_grant(
        {
            "bundle_id": grant.bundle_id,
            "scope": grant.scope,
            "source": grant.source,
        }
    )


def _upsert_user_public_network_grant(grant: PublicNetworkGrant) -> None:
    user_grants.upsert_public_network_grant(
        {
            "scope": grant.scope,
            "source": grant.source,
        }
    )


async def _persist_then_apply_user_store(
    session_manager: Any,
    session_key: str,
    *,
    existing: RunContext,
    updated: RunContext,
    apply_user_store: Callable[[], None] | None = None,
) -> RunContext:
    persisted = await persist_run_context(session_manager, session_key, updated)
    if apply_user_store is None:
        return persisted
    try:
        apply_user_store()
    except Exception as exc:
        try:
            await persist_run_context(session_manager, session_key, existing)
        except Exception as rollback_exc:
            raise exc from rollback_exc
        raise
    return persisted


async def set_workspace(
    session_manager: Any,
    session_key: str,
    *,
    workspace_path: str,
    config: Any,
    current_workspace: str | None,
) -> RunContext:
    normalized_workspace = normalize_workspace_path(workspace_path)
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=current_workspace,
    )
    existing_workspace = None
    if existing.workspace is not None:
        try:
            existing_workspace = normalize_workspace_path(existing.workspace)
        except ValueError:
            existing_workspace = existing.workspace
    if existing_workspace == normalized_workspace:
        return existing
    updated = replace(existing, workspace=normalized_workspace, source="saved")
    return await persist_run_context(session_manager, session_key, updated)


def validated_mount_grant(
    context: RunContext,
    *,
    path: str,
    access: str,
    scope: str,
    workspace: str | None,
) -> MountGrant:
    """Validate one requested mount and return its normalized grant."""

    mount_access = normalize_mount_access(access)
    decision = decide_path_access(
        path,
        workspace=context.workspace or workspace,
        mounts=context.mounts,
        write=mount_access == "rw",
    )
    if decision.status == "blocked":
        raise ValueError(decision.reason or "mount_blocked")
    return MountGrant(
        path=_mount_grant_storage_path(path),
        access=mount_access,
        scope=normalize_scope(scope),
    )


async def add_mount_grant(
    session_manager: Any,
    session_key: str,
    *,
    path: str,
    access: str,
    scope: str,
    config: Any,
    workspace: str | None,
) -> RunContext:
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    grant = validated_mount_grant(
        existing,
        path=path,
        access=access,
        scope=scope,
        workspace=workspace,
    )
    apply_user_store = None
    if grant.scope == "workspace":
        def apply_user_store(grant: MountGrant = grant) -> None:
            _upsert_user_mount_grant(grant)

    if grant.scope == "once":
        grant_key = os.path.normcase(grant.path)
        mounts = tuple(
            existing_grant
            for existing_grant in existing.mounts
            if not (
                existing_grant.scope == "once"
                and os.path.normcase(
                    _mount_grant_storage_path(existing_grant.path)
                )
                == grant_key
            )
        ) + (grant,)
        return replace(
            existing,
            mounts=mounts,
            source="resolved_overlay",
        )

    if grant in existing.mounts:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    mounts = tuple(m for m in existing.mounts if m.path != grant.path) + (grant,)
    if mounts == existing.mounts:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    return await _persist_then_apply_user_store(
        session_manager,
        session_key,
        existing=existing,
        updated=replace(existing, mounts=mounts, source="saved"),
        apply_user_store=apply_user_store,
    )


async def remove_mount_grant(
    session_manager: Any,
    session_key: str,
    *,
    path: str,
    config: Any,
    workspace: str | None,
    scope: str | None = None,
) -> RunContext:
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    decision = decide_path_access(
        path,
        workspace=existing.workspace or workspace,
        mounts=existing.mounts,
    )
    if decision.status == "blocked":
        raise ValueError(decision.reason or "mount_blocked")
    normalized_path = decision.normalized_path
    storage_path = _mount_grant_storage_path(path)
    removal_paths = {normalized_path, storage_path, path}
    target_scope = str(scope or "").strip().lower()

    session_existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
        include_user_grants=False,
    )
    if target_scope in {"chat", "workspace"}:
        remove_user = target_scope == "workspace"
        remove_chat = target_scope == "chat"
    else:
        remove_user = True
        remove_chat = True

    mounts = tuple(
        m
        for m in session_existing.mounts
        if not (m.path in removal_paths and (remove_chat or m.scope == target_scope))
    )
    if mounts != session_existing.mounts:
        await persist_run_context(
            session_manager,
            session_key,
            replace(session_existing, mounts=mounts, source="saved"),
        )
    if remove_user:
        for removal_path in removal_paths:
            user_grants.remove_mount_grant(removal_path)
    return await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )


async def add_domain_grant(
    session_manager: Any,
    session_key: str,
    *,
    domain: str,
    scope: str,
    config: Any,
    workspace: str | None,
    source: str = "manual",
) -> RunContext:
    decision = validate_domain_pattern(domain)
    if decision.status == "blocked":
        raise ValueError(decision.reason)
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    grant = DomainGrant(
        domain=decision.normalized,
        scope=normalize_scope(scope),
        source=source,
    )
    apply_user_store = None
    if grant.scope == "workspace":
        def apply_user_store(grant: DomainGrant = grant) -> None:
            _upsert_user_domain_grant(grant)

    if grant in existing.domains:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    domains = tuple(d for d in existing.domains if d.domain != grant.domain) + (grant,)
    if domains == existing.domains:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    return await _persist_then_apply_user_store(
        session_manager,
        session_key,
        existing=existing,
        updated=replace(existing, domains=domains, source="saved"),
        apply_user_store=apply_user_store,
    )


async def auto_add_trusted_domain_grant(
    session_manager: Any,
    session_key: str,
    *,
    domain: str,
    config: Any,
    workspace: str | None,
) -> RunContext:
    domain_decision = validate_domain_pattern(domain)
    if domain_decision.status == "blocked":
        raise ValueError(domain_decision.reason)
    normalized_host = domain_decision.normalized

    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    grant = DomainGrant(
        domain=normalized_host,
        scope="chat",
        source="auto_trusted",
    )
    if grant in existing.domains:
        return existing
    trusted_context = replace(existing, run_mode=RunMode.TRUSTED)
    decision = decide_network_access(normalized_host, trusted_context)
    if (
        decision.status != "allow"
        or decision.reason != "auto_trusted"
        or decision.source != "auto_trusted:chat"
    ):
        raise ValueError(decision.reason)
    domains = tuple(
        existing_domain
        for existing_domain in existing.domains
        if existing_domain.domain != grant.domain
    ) + (grant,)
    if domains == existing.domains:
        return existing
    return await persist_run_context(
        session_manager,
        session_key,
        replace(existing, domains=domains, source="saved"),
    )


async def add_public_network_grant(
    session_manager: Any,
    session_key: str,
    *,
    scope: str,
    config: Any,
    workspace: str | None,
    source: str = "manual",
) -> RunContext:
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    grant = PublicNetworkGrant(
        scope=normalize_scope(scope),
        source=str(source or "manual").strip() or "manual",
    )
    apply_user_store = None
    if grant.scope == "workspace":
        def apply_user_store(grant: PublicNetworkGrant = grant) -> None:
            _upsert_user_public_network_grant(grant)

    if grant in existing.public_network:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    public_network = tuple(
        item for item in existing.public_network if item.scope != grant.scope
    ) + (grant,)
    if public_network == existing.public_network:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    return await _persist_then_apply_user_store(
        session_manager,
        session_key,
        existing=existing,
        updated=replace(existing, public_network=public_network, source="saved"),
        apply_user_store=apply_user_store,
    )


async def remove_domain_grant(
    session_manager: Any,
    session_key: str,
    *,
    domain: str,
    config: Any,
    workspace: str | None,
    scope: str | None = None,
) -> RunContext:
    decision = validate_domain_pattern(domain)
    if decision.status == "blocked":
        raise ValueError(decision.reason)
    normalized = decision.normalized
    target_scope = str(scope or "").strip().lower()
    session_existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
        include_user_grants=False,
    )
    if target_scope in {"chat", "workspace"}:
        remove_user = target_scope == "workspace"
        remove_chat = target_scope == "chat"
    else:
        remove_user = True
        remove_chat = True

    domains = tuple(
        d
        for d in session_existing.domains
        if not (d.domain == normalized and (remove_chat or d.scope == target_scope))
    )
    if domains != session_existing.domains:
        await persist_run_context(
            session_manager,
            session_key,
            replace(session_existing, domains=domains, source="saved"),
        )
    if remove_user:
        user_grants.remove_domain_grant(normalized)
    return await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )


async def enable_bundle_grant(
    session_manager: Any,
    session_key: str,
    *,
    bundle_id: str,
    scope: str,
    config: Any,
    workspace: str | None,
) -> RunContext:
    normalized_bundle_id = _normalize_bundle_id(bundle_id)
    if not expand_package_bundle(normalized_bundle_id):
        raise ValueError("unknown_package_bundle")
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    grant = PackageBundleGrant(
        bundle_id=normalized_bundle_id,
        scope=normalize_scope(scope, "workspace"),
        source="manual",
    )
    apply_user_store = None
    if grant.scope == "workspace":
        def apply_user_store(grant: PackageBundleGrant = grant) -> None:
            _upsert_user_bundle_grant(grant)

    if grant in existing.bundles:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    bundles = tuple(b for b in existing.bundles if b.bundle_id != grant.bundle_id) + (
        grant,
    )
    if bundles == existing.bundles:
        if apply_user_store is not None:
            apply_user_store()
        return existing
    return await _persist_then_apply_user_store(
        session_manager,
        session_key,
        existing=existing,
        updated=replace(existing, bundles=bundles, source="saved"),
        apply_user_store=apply_user_store,
    )


async def disable_bundle_grant(
    session_manager: Any,
    session_key: str,
    *,
    bundle_id: str,
    config: Any,
    workspace: str | None,
) -> RunContext:
    normalized_bundle_id = _normalize_bundle_id(bundle_id)
    if not expand_package_bundle(normalized_bundle_id):
        raise ValueError("unknown_package_bundle")
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    existing_scope = next(
        (
            bundle.scope
            for bundle in existing.bundles
            if bundle.bundle_id == normalized_bundle_id
        ),
        "workspace",
    )
    grant = PackageBundleGrant(
        bundle_id=normalized_bundle_id,
        scope=normalize_scope(existing_scope, "workspace"),
        source="disabled",
    )
    bundles = tuple(b for b in existing.bundles if b.bundle_id != normalized_bundle_id)
    bundles = bundles + (grant,)
    if bundles == existing.bundles:
        user_grants.remove_bundle_grant(normalized_bundle_id)
        return existing
    return await _persist_then_apply_user_store(
        session_manager,
        session_key,
        existing=existing,
        updated=replace(existing, bundles=bundles, source="saved"),
        apply_user_store=lambda: user_grants.remove_bundle_grant(normalized_bundle_id),
    )


__all__ = [
    "add_domain_grant",
    "add_mount_grant",
    "add_public_network_grant",
    "auto_add_trusted_domain_grant",
    "disable_bundle_grant",
    "enable_bundle_grant",
    "normalize_scope",
    "remove_domain_grant",
    "remove_mount_grant",
    "set_workspace",
    "validated_mount_grant",
]
