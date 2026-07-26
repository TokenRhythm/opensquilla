"""Per-session sandbox run context persistence."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

from opensquilla.sandbox.domain_validation import validate_domain_pattern
from opensquilla.sandbox.package_bundles import expand_package_bundle
from opensquilla.sandbox.path_validation import (
    decide_path_access,
    normalize_mount_access,
    normalize_path,
)
from opensquilla.sandbox.run_mode import RunMode, config_run_mode, normalize_run_mode
from opensquilla.sandbox.user_grants import load_user_grants_payload

RUN_CONTEXT_ORIGIN_KEY = "sandbox_run_context"
DEFAULT_ROOT_WORKSPACE = "/root/.opensquilla/workspace"


@dataclass(frozen=True)
class MountGrant:
    path: str
    access: str = "ro"
    scope: str = "chat"


@dataclass(frozen=True)
class DomainGrant:
    domain: str
    scope: str = "chat"
    source: str = "manual"


@dataclass(frozen=True)
class PackageBundleGrant:
    bundle_id: str
    scope: str = "workspace"
    source: str = "manual"


@dataclass(frozen=True)
class PublicNetworkGrant:
    scope: str = "chat"
    source: str = "manual"


@dataclass(frozen=True)
class TemporaryGrant:
    kind: str
    value: str
    fingerprint: str
    expires_after: str = "once"


@dataclass(frozen=True)
class RunContext:
    run_mode: RunMode
    workspace: str | None = None
    mounts: tuple[MountGrant, ...] = ()
    domains: tuple[DomainGrant, ...] = ()
    bundles: tuple[PackageBundleGrant, ...] = ()
    public_network: tuple[PublicNetworkGrant, ...] = ()
    temporary_grants: tuple[TemporaryGrant, ...] = ()
    source: str = "default"

    def to_origin_payload(self) -> dict[str, Any]:
        return {
            "run_mode": self.run_mode.value,
            "workspace": self.workspace,
            "mounts": [
                {"path": grant.path, "access": grant.access, "scope": grant.scope}
                for grant in self.mounts
            ],
            "domains": [
                {
                    "domain": grant.domain,
                    "scope": grant.scope,
                    "source": grant.source,
                }
                for grant in self.domains
            ],
            "bundles": [
                {
                    "bundle_id": grant.bundle_id,
                    "scope": grant.scope,
                    "source": grant.source,
                }
                for grant in self.bundles
            ],
            "public_network": [
                {
                    "scope": grant.scope,
                    "source": grant.source,
                }
                for grant in self.public_network
            ],
            "temporary_grants": [
                {
                    "kind": grant.kind,
                    "value": grant.value,
                    "fingerprint": grant.fingerprint,
                    "expires_after": grant.expires_after,
                }
                for grant in self.temporary_grants
            ],
        }


def run_context_for_subagent(context: RunContext) -> RunContext:
    """Keep durable grants while dropping authority scoped to one parent action."""

    return replace(
        context,
        mounts=tuple(grant for grant in context.mounts if grant.scope != "once"),
        domains=tuple(grant for grant in context.domains if grant.scope != "once"),
        bundles=tuple(grant for grant in context.bundles if grant.scope != "once"),
        public_network=tuple(
            grant for grant in context.public_network if grant.scope != "once"
        ),
        temporary_grants=(),
    )


async def _get_session_node(session_manager: Any, session_key: str) -> Any | None:
    get_session = getattr(session_manager, "get_session", None)
    if callable(get_session):
        return await get_session(session_key)

    storage = getattr(session_manager, "_storage", None)
    storage_get = getattr(storage, "get_session", None)
    if callable(storage_get):
        return await storage_get(session_key)
    return None


def _origin_dict(node: Any) -> dict[str, Any]:
    origin = getattr(node, "origin", None)
    return dict(origin) if isinstance(origin, dict) else {}


def _string_value(value: Any, default: str | None = None) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def normalize_scope(scope: Any, default: str = "chat") -> str:
    value = str(scope or default).strip().lower()
    return value if value in {"chat", "workspace", "once"} else default


def _looks_like_posix_rooted_text(path: str) -> bool:
    return os.name == "nt" and path.startswith("/") and not path.startswith("//")


def _normalize_workspace_candidate(workspace: str) -> str:
    if _looks_like_posix_rooted_text(workspace):
        return PurePosixPath(workspace).as_posix()
    return str(normalize_path(workspace))


def normalize_workspace_path(value: Any) -> str:
    workspace = _string_value(value)
    if workspace is None:
        raise ValueError("empty_workspace_path")
    try:
        normalized_workspace = _normalize_workspace_candidate(workspace)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("invalid_workspace_path")
    return normalized_workspace


def _workspace_from_payload(value: Any) -> str | None:
    try:
        return normalize_workspace_path(value)
    except ValueError:
        return None


def _mounts_from_payload(
    value: Any,
    *,
    workspace: str | None = None,
) -> tuple[MountGrant, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return ()
    mounts: dict[str, MountGrant] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        path = _string_value(item.get("path"))
        if path is None:
            continue
        access = normalize_mount_access(_string_value(item.get("access"), "ro"))
        try:
            decision = decide_path_access(
                path,
                workspace=workspace,
                mounts=(),
                write=access == "rw",
            )
        except (OSError, RuntimeError, ValueError):
            continue
        if decision.status == "blocked":
            continue
        try:
            normalized_path = str(normalize_path(path))
        except (OSError, RuntimeError, ValueError):
            continue
        grant = MountGrant(
            path=normalized_path,
            access=access,
            scope=normalize_scope(item.get("scope"), "chat"),
        )
        mounts.pop(grant.path, None)
        mounts[grant.path] = grant
    return tuple(mounts.values())


def _domains_from_payload(value: Any) -> tuple[DomainGrant, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return ()
    domains: dict[str, DomainGrant] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        domain = _string_value(item.get("domain"))
        if domain is None:
            continue
        decision = validate_domain_pattern(domain)
        if decision.status == "blocked":
            continue
        grant = DomainGrant(
            domain=decision.normalized,
            scope=normalize_scope(item.get("scope"), "chat"),
            source=_string_value(item.get("source"), "manual") or "manual",
        )
        domains.pop(grant.domain, None)
        domains[grant.domain] = grant
    return tuple(domains.values())


def _bundles_from_payload(value: Any) -> tuple[PackageBundleGrant, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return ()
    bundles: dict[str, PackageBundleGrant] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        bundle_id = _string_value(item.get("bundle_id") or item.get("bundleId"))
        if bundle_id is None:
            continue
        if not expand_package_bundle(bundle_id):
            continue
        grant = PackageBundleGrant(
            bundle_id=bundle_id,
            scope=normalize_scope(item.get("scope"), "workspace"),
            source=_string_value(item.get("source"), "manual") or "manual",
        )
        bundles.pop(grant.bundle_id, None)
        bundles[grant.bundle_id] = grant
    return tuple(bundles.values())


def _public_network_from_payload(value: Any) -> tuple[PublicNetworkGrant, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return ()
    grants: dict[str, PublicNetworkGrant] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        grant = PublicNetworkGrant(
            scope=normalize_scope(item.get("scope"), "chat"),
            source=_string_value(item.get("source"), "manual") or "manual",
        )
        grants.pop(grant.scope, None)
        grants[grant.scope] = grant
    return tuple(grants.values())


def _temporary_grants_from_payload(value: Any) -> tuple[TemporaryGrant, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, dict)):
        return ()
    grants: list[TemporaryGrant] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = _string_value(item.get("kind"))
        grant_value = _string_value(item.get("value"))
        fingerprint = _string_value(item.get("fingerprint"))
        if kind is None or grant_value is None or fingerprint is None:
            continue
        grants.append(
            TemporaryGrant(
                kind=kind,
                value=grant_value,
                fingerprint=fingerprint,
                expires_after=(
                    _string_value(
                        item.get("expires_after") or item.get("expiresAfter"),
                        "once",
                    )
                    or "once"
                ),
            )
        )
    return tuple(grants)


def _context_from_payload(payload: Any, source: str) -> RunContext | None:
    if not isinstance(payload, dict):
        return None
    if "run_mode" not in payload:
        return None
    try:
        run_mode = normalize_run_mode(payload.get("run_mode"))
    except ValueError:
        return None
    workspace = _workspace_from_payload(payload.get("workspace"))
    return RunContext(
        run_mode=run_mode,
        workspace=workspace,
        mounts=_mounts_from_payload(payload.get("mounts"), workspace=workspace),
        domains=_domains_from_payload(payload.get("domains")),
        bundles=_bundles_from_payload(payload.get("bundles")),
        public_network=_public_network_from_payload(
            payload.get("public_network") or payload.get("publicNetwork")
        ),
        temporary_grants=_temporary_grants_from_payload(payload.get("temporary_grants")),
        source=source,
    )


def _origin_item_scope(item: Any, default: str) -> str:
    if not isinstance(item, dict):
        return default
    return normalize_scope(item.get("scope"), default)


def _without_materialized_user_grants(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    filtered = dict(payload)
    mounts = payload.get("mounts")
    if isinstance(mounts, Iterable) and not isinstance(mounts, (str, bytes, dict)):
        filtered["mounts"] = [
            item for item in mounts if _origin_item_scope(item, "chat") != "workspace"
        ]
    domains = payload.get("domains")
    if isinstance(domains, Iterable) and not isinstance(domains, (str, bytes, dict)):
        filtered["domains"] = [
            item for item in domains if _origin_item_scope(item, "chat") != "workspace"
        ]
    bundles = payload.get("bundles")
    if isinstance(bundles, Iterable) and not isinstance(bundles, (str, bytes, dict)):
        filtered["bundles"] = [
            item
            for item in bundles
            if (
                _origin_item_scope(item, "workspace") != "workspace"
                or (
                    isinstance(item, dict)
                    and (_string_value(item.get("source"), "manual") or "manual")
                    == "disabled"
                )
            )
        ]
    public_network = payload.get("public_network")
    if isinstance(public_network, Iterable) and not isinstance(
        public_network,
        (str, bytes, dict),
    ):
        filtered["public_network"] = [
            item
            for item in public_network
            if _origin_item_scope(item, "chat") != "workspace"
        ]
    public_network = payload.get("publicNetwork")
    if isinstance(public_network, Iterable) and not isinstance(
        public_network,
        (str, bytes, dict),
    ):
        filtered["publicNetwork"] = [
            item
            for item in public_network
            if _origin_item_scope(item, "chat") != "workspace"
        ]
    return filtered


def run_context_from_origin_payload(
    payload: Any,
    *,
    source: str = "metadata",
    preserve_materialized_user_grants: bool = False,
) -> RunContext | None:
    """Hydrate a validated run context from serialized origin metadata.

    Invalid or malformed payloads return ``None`` so route metadata cannot
    silently become grants unless it passes the same normalization as saved
    session context.
    """
    if not preserve_materialized_user_grants:
        payload = _without_materialized_user_grants(payload)
    return _context_from_payload(payload, source)


def _merge_mount_grants(
    base: tuple[MountGrant, ...],
    overlay: tuple[MountGrant, ...],
) -> tuple[MountGrant, ...]:
    grants = {grant.path: grant for grant in base}
    for grant in overlay:
        grants[grant.path] = grant
    return tuple(grants.values())


def _merge_domain_grants(
    base: tuple[DomainGrant, ...],
    overlay: tuple[DomainGrant, ...],
) -> tuple[DomainGrant, ...]:
    grants = {grant.domain: grant for grant in base}
    for grant in overlay:
        grants[grant.domain] = grant
    return tuple(grants.values())


def _merge_bundle_grants(
    base: tuple[PackageBundleGrant, ...],
    overlay: tuple[PackageBundleGrant, ...],
) -> tuple[PackageBundleGrant, ...]:
    grants = {grant.bundle_id: grant for grant in base}
    for grant in overlay:
        grants[grant.bundle_id] = grant
    return tuple(grants.values())


def _merge_public_network_grants(
    base: tuple[PublicNetworkGrant, ...],
    overlay: tuple[PublicNetworkGrant, ...],
) -> tuple[PublicNetworkGrant, ...]:
    grants = {grant.scope: grant for grant in base}
    for grant in overlay:
        grants[grant.scope] = grant
    return tuple(grants.values())


def _with_user_grants(context: RunContext) -> RunContext:
    payload = load_user_grants_payload()
    user_mounts = _mounts_from_payload(payload.get("mounts"), workspace=context.workspace)
    user_domains = _domains_from_payload(payload.get("domains"))
    user_bundles = _bundles_from_payload(payload.get("bundles"))
    user_public_network = _public_network_from_payload(payload.get("public_network"))
    if not user_mounts and not user_domains and not user_bundles and not user_public_network:
        return context
    mounts = _merge_mount_grants(user_mounts, context.mounts)
    domains = _merge_domain_grants(user_domains, context.domains)
    bundles = _merge_bundle_grants(user_bundles, context.bundles)
    public_network = _merge_public_network_grants(
        user_public_network,
        context.public_network,
    )
    if (
        mounts == context.mounts
        and domains == context.domains
        and bundles == context.bundles
        and public_network == context.public_network
    ):
        return context
    return RunContext(
        run_mode=context.run_mode,
        workspace=context.workspace,
        mounts=mounts,
        domains=domains,
        bundles=bundles,
        public_network=public_network,
        temporary_grants=context.temporary_grants,
        source=context.source,
    )


def _session_persisted_context(context: RunContext) -> RunContext:
    # ``workspace`` grants live in the durable user store, so they are not
    # re-persisted on the session node. ``once`` grants are single-turn by
    # contract ("Allow once"): they must NOT survive into the durable session
    # origin (a gateway restart would otherwise silently re-grant them), so they
    # are stripped here and live only in the in-memory resolved overlay for the
    # remainder of the granting turn.
    mounts = tuple(grant for grant in context.mounts if grant.scope not in ("workspace", "once"))
    domains = tuple(grant for grant in context.domains if grant.scope != "workspace")
    bundles = tuple(
        grant
        for grant in context.bundles
        if grant.scope != "workspace" or grant.source == "disabled"
    )
    public_network = tuple(
        grant for grant in context.public_network if grant.scope != "workspace"
    )
    if (
        mounts == context.mounts
        and domains == context.domains
        and bundles == context.bundles
        and public_network == context.public_network
    ):
        return context
    return replace(
        context,
        mounts=mounts,
        domains=domains,
        bundles=bundles,
        public_network=public_network,
    )


async def get_run_context(
    session_manager: Any,
    session_key: str,
    *,
    config: Any,
    workspace: str | None,
    include_user_grants: bool = True,
) -> RunContext:
    configured_mode = config_run_mode(config)
    node = await _get_session_node(session_manager, session_key)
    if node is not None:
        origin = _origin_dict(node)
        saved = _context_from_payload(
            _without_materialized_user_grants(origin.get(RUN_CONTEXT_ORIGIN_KEY)),
            "saved",
        )
        if saved is not None:
            if configured_mode == RunMode.FULL and saved.run_mode != RunMode.FULL:
                saved = replace(saved, run_mode=RunMode.FULL, source="global_full")
            return _with_user_grants(saved) if include_user_grants else saved
    context = RunContext(
        run_mode=configured_mode,
        workspace=_workspace_from_payload(workspace),
        source="default",
    )
    return _with_user_grants(context) if include_user_grants else context


async def persist_run_context(
    session_manager: Any,
    session_key: str,
    context: RunContext,
) -> RunContext:
    node = await _get_session_node(session_manager, session_key)
    if node is None:
        raise KeyError(f"Session not found: {session_key}")
    origin = _origin_dict(node)
    origin[RUN_CONTEXT_ORIGIN_KEY] = (
        _session_persisted_context(context).to_origin_payload()
    )
    update = getattr(session_manager, "update", None)
    if not callable(update):
        raise RuntimeError("Session manager does not support update")
    await update(session_key, origin=origin)
    return context


async def set_run_mode(
    session_manager: Any,
    session_key: str,
    run_mode: RunMode | str,
    *,
    config: Any,
    workspace: str | None = None,
) -> RunContext:
    existing = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    updated = RunContext(
        run_mode=normalize_run_mode(run_mode),
        workspace=existing.workspace,
        mounts=existing.mounts,
        domains=existing.domains,
        bundles=existing.bundles,
        public_network=existing.public_network,
        temporary_grants=existing.temporary_grants,
        source="saved",
    )
    return await persist_run_context(session_manager, session_key, updated)


__all__ = [
    "RUN_CONTEXT_ORIGIN_KEY",
    "DEFAULT_ROOT_WORKSPACE",
    "DomainGrant",
    "MountGrant",
    "PackageBundleGrant",
    "PublicNetworkGrant",
    "RunContext",
    "TemporaryGrant",
    "get_run_context",
    "normalize_scope",
    "normalize_workspace_path",
    "persist_run_context",
    "run_context_from_origin_payload",
    "run_context_for_subagent",
    "set_run_mode",
]
