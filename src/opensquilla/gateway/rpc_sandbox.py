"""RPC handlers for per-session sandbox run context."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.sandbox.domain_validation import validate_domain_pattern
from opensquilla.sandbox.escalation import remember_resolved_run_context
from opensquilla.sandbox.package_bundles import expand_package_bundle
from opensquilla.sandbox.path_validation import (
    decide_path_access,
    normalize_mount_access,
    normalize_path,
)
from opensquilla.sandbox.run_context import (
    RunContext,
    get_run_context,
    normalize_workspace_path,
    set_run_mode,
)
from opensquilla.sandbox.run_context_service import (
    add_domain_grant,
    add_mount_grant,
    disable_bundle_grant,
    enable_bundle_grant,
    remove_domain_grant,
    remove_mount_grant,
    set_workspace,
)
from opensquilla.sandbox.run_mode import (
    RunMode,
    display_name,
    execution_target,
    normalize_run_mode,
)
from opensquilla.sandbox.run_mode_policy import (
    coerce_run_mode_for_principal,
    run_mode_allowed_for_principal,
)
from opensquilla.sandbox.setup_runtime import (
    current_sandbox_setup_runtime_status,
    ensure_sandbox_setup_auto,
)
from opensquilla.sandbox.setup_state import (
    SandboxSetupState,
    current_sandbox_setup_status,
)
from opensquilla.sandbox.status import status_payload
from opensquilla.session.keys import parse_agent_id

_d = get_dispatcher()


def _require_params(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def _require_session_key(params: dict[str, Any]) -> str:
    session_key = params.get("sessionKey")
    if not isinstance(session_key, str) or not session_key.strip():
        raise ValueError("params.sessionKey is required")
    return session_key.strip()


def _require_string_param(
    params: dict[str, Any],
    name: str,
    message: str,
) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value


def _require_one_string_param(
    params: dict[str, Any],
    names: tuple[str, ...],
    message: str,
) -> str:
    for name in names:
        value = params.get(name)
        if isinstance(value, str) and value.strip():
            return value
    raise ValueError(message)


def _require_bundle_id(params: dict[str, Any]) -> str:
    bundle_id = _require_one_string_param(
        params,
        ("bundleId", "bundle_id"),
        "params.bundleId is required",
    )
    if not expand_package_bundle(bundle_id.strip()):
        raise ValueError("unknown_package_bundle")
    return bundle_id


def _validate_domain_param(domain: str) -> None:
    decision = validate_domain_pattern(domain)
    if decision.status == "blocked":
        raise ValueError(decision.reason)


def _validate_workspace_param(workspace: str) -> str:
    return normalize_workspace_path(workspace)


def _path_entry_payload(path: Any) -> dict[str, Any]:
    name = str(path.name or str(path))
    payload = {
        "name": name,
        "path": str(path),
        "kind": "directory" if path.is_dir() else "file",
        "selectable": True,
    }
    if name.startswith("."):
        payload["hidden"] = True
    return payload


def _parent_entry_payload(path: Any) -> dict[str, Any]:
    return {
        "name": "..",
        "path": str(path),
        "kind": "directory",
        "selectable": True,
    }


def _pick_directory_path(initial_dir: str | None = None) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - host environment dependent
        raise RpcUnavailableError("Directory picker is not available on this host.") from exc

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial_dir or "",
            mustexist=True,
        )
    except Exception as exc:  # pragma: no cover - host environment dependent
        raise RpcUnavailableError("Directory picker is not available on this host.") from exc
    finally:
        if root is not None:
            root.destroy()

    if not selected:
        raise RpcUnavailableError("Directory selection was cancelled.")
    return selected


def _require_session_manager(ctx: RpcContext) -> Any:
    manager = getattr(ctx, "session_manager", None)
    if manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    return manager


def _require_owner(ctx: RpcContext, method: str) -> None:
    if not getattr(ctx.principal, "is_owner", False):
        raise RpcHandlerError("UNAUTHORIZED", f"{method} requires owner principal.")


def _context_for_principal(context: RunContext, principal: Any) -> RunContext:
    if run_mode_allowed_for_principal(context.run_mode, principal):
        return context
    return replace(
        context,
        run_mode=coerce_run_mode_for_principal(context.run_mode, principal),
    )


async def _session_for_key(session_manager: Any, session_key: str) -> Any | None:
    get_session = getattr(session_manager, "get_session", None)
    if callable(get_session):
        return await get_session(session_key)

    storage = get_session_storage(session_manager)
    if storage is not None:
        return await storage.get_session(session_key)
    return None


async def _ensure_session_for_set(session_manager: Any, session_key: str) -> Any | None:
    session = await _session_for_key(session_manager, session_key)
    if session is not None:
        return session

    agent_id = parse_agent_id(session_key)
    get_or_create = getattr(session_manager, "get_or_create", None)
    if callable(get_or_create):
        result = await get_or_create(session_key, agent_id=agent_id)
        return result[0] if isinstance(result, tuple) else result

    create = getattr(session_manager, "create", None)
    if callable(create):
        return await create(session_key, agent_id=agent_id)
    return None


async def _workspace_for_session_or_config(
    session_manager: Any,
    session_key: str,
    config: Any,
) -> str | None:
    agent_id = parse_agent_id(session_key)
    session = await _session_for_key(session_manager, session_key)
    session_agent_id = getattr(session, "agent_id", None) if session is not None else None
    if isinstance(session_agent_id, str) and session_agent_id:
        agent_id = session_agent_id
    workspace = resolve_agent_workspace_dir(agent_id, config)
    return str(workspace) if workspace is not None else None


async def _workspace_for_session(
    session_manager: Any,
    session_key: str,
    config: Any,
) -> str | None:
    agent_id = parse_agent_id(session_key)
    session = await _session_for_key(session_manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    session_agent_id = getattr(session, "agent_id", None)
    if isinstance(session_agent_id, str) and session_agent_id:
        agent_id = session_agent_id
    workspace = resolve_agent_workspace_dir(agent_id, config)
    return str(workspace) if workspace is not None else None


def _remember_context_overlay(
    ctx: RpcContext,
    *,
    session_key: str,
    workspace: str | None,
    context: RunContext,
) -> None:
    manager = getattr(ctx, "session_manager", None)
    if manager is None:
        return
    remember_resolved_run_context(
        session_key,
        workspace,
        context,
        session_manager=manager,
        config=ctx.config,
    )


async def _validate_mount_path_for_rpc(
    session_manager: Any,
    session_key: str,
    config: Any,
    *,
    path: str,
    access: str = "ro",
) -> None:
    workspace = await _workspace_for_session_or_config(session_manager, session_key, config)
    context = await get_run_context(
        session_manager,
        session_key,
        config=config,
        workspace=workspace,
    )
    decision = decide_path_access(
        path,
        workspace=context.workspace or workspace,
        mounts=context.mounts,
        write=normalize_mount_access(access) == "rw",
    )
    if decision.status == "blocked":
        raise ValueError(decision.reason or "mount_blocked")


def _payload(context: RunContext) -> dict[str, Any]:
    origin_payload = context.to_origin_payload()
    return {
        "runMode": context.run_mode.value,
        "runModeLabel": display_name(context.run_mode),
        "executionTarget": execution_target(context.run_mode),
        "workspace": context.workspace,
        "mounts": origin_payload["mounts"],
        "domains": origin_payload["domains"],
        "bundles": origin_payload.get("bundles", []),
        "publicNetwork": origin_payload.get("public_network", []),
        "temporaryGrants": origin_payload.get("temporary_grants", []),
        "source": context.source,
    }


def _explain_messages(status: dict[str, Any]) -> list[dict[str, str]]:
    managed_network = str(status.get("managed_network", "blocked"))
    if managed_network == "ready":
        network_message = "Managed network allowlist is ready."
    else:
        network_message = "Managed network allowlist is blocked."
    return [
        {"kind": "run_mode", "message": f"Run mode is {status['run_mode']}."},
        {"kind": "managed_network", "message": network_message},
    ]


@_d.method("sandbox.status", scope="operator.read")
async def _handle_sandbox_status(params: dict | None, ctx: RpcContext) -> dict:
    return status_payload(ctx.config)


@_d.method("sandbox.setup.status", scope="operator.read")
async def _handle_sandbox_setup_status(params: dict | None, ctx: RpcContext) -> dict:
    result = await current_sandbox_setup_runtime_status(ctx.config)
    return result.to_payload()


@_d.method("sandbox.setup.ensure", scope="operator.write")
async def _handle_sandbox_setup_ensure(params: dict | None, ctx: RpcContext) -> dict:
    _require_owner(ctx, "sandbox.setup.ensure")
    result = await ensure_sandbox_setup_auto(ctx.config)
    return result.to_payload()


@_d.method("sandbox.explain", scope="operator.read")
async def _handle_sandbox_explain(params: dict | None, ctx: RpcContext) -> dict:
    params = params if isinstance(params, dict) else {}
    status = status_payload(ctx.config)
    result: dict[str, Any] = {
        "status": status,
        "messages": _explain_messages(status),
    }
    session_key = params.get("sessionKey")
    if isinstance(session_key, str) and session_key:
        manager = _require_session_manager(ctx)
        workspace = await _workspace_for_session(manager, session_key, ctx.config)
        context = await get_run_context(
            manager,
            session_key,
            config=ctx.config,
            workspace=workspace,
        )
        result["runContext"] = _payload(context)
        result["autonomousPaused"] = await _session_autonomous_paused(session_key)
    return result


async def _session_autonomous_paused(session_key: str) -> bool:
    """Whether the sandbox denial ledger has paused autonomous execution.

    Surfaced so a client can render the paused state and offer a resume action
    instead of leaving the run stuck with no visible recovery (issue #469).
    """
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
    if runtime is None:
        return False
    return await runtime.ledger.is_paused(session_key)


@_d.method("sandbox.resume", scope="operator.write")
async def _handle_sandbox_resume(params: dict | None, ctx: RpcContext) -> dict:
    """Clear a denial-ledger autonomous pause so a stuck run can continue.

    Owner-scoped recovery for the §8.5 sticky pause (issue #469): without this
    there is no surface that can perform the "human intervention" the pause
    message demands, so the only escape was restarting the gateway.
    """
    _require_owner(ctx, "sandbox.resume")
    params = _require_params(params)
    session_key = _require_session_key(params)
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
    if runtime is None:
        raise RpcHandlerError("UNAVAILABLE", "Sandbox runtime is not configured.", retryable=True)
    resumed = await runtime.ledger.clear_pause(session_key)
    return {"sessionKey": session_key, "resumed": resumed, "autonomousPaused": False}


async def _require_sandbox_setup_ready_for_mode(ctx: RpcContext, run_mode: Any) -> None:
    normalized = normalize_run_mode(run_mode)
    if normalized == RunMode.FULL:
        return
    status = await current_sandbox_setup_status(ctx.config)
    if status.state != SandboxSetupState.READY:
        raise RpcHandlerError(
            "SANDBOX_SETUP_REQUIRED",
            "Sandbox setup must be completed before enabling sandbox run modes.",
            details=status.to_payload(),
        )


@_d.method("sandbox.run_context.get", scope="operator.read")
async def _handle_sandbox_run_context_get(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    manager = _require_session_manager(ctx)
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await get_run_context(
        manager,
        session_key,
        config=ctx.config,
        workspace=workspace,
    )
    context = _context_for_principal(context, ctx.principal)
    return _payload(context)


@_d.method("sandbox.run_context.set", scope="operator.write")
async def _handle_sandbox_run_context_set(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    run_mode = normalize_run_mode(params.get("runMode"))
    if not run_mode_allowed_for_principal(run_mode, ctx.principal):
        _require_owner(ctx, "sandbox.run_context.set")
    await _require_sandbox_setup_ready_for_mode(ctx, run_mode)
    manager = _require_session_manager(ctx)
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    context = await set_run_mode(
        manager,
        session_key,
        run_mode,
        config=ctx.config,
        workspace=await _workspace_for_session(manager, session_key, ctx.config),
    )
    _remember_context_overlay(
        ctx,
        session_key=session_key,
        workspace=context.workspace,
        context=context,
    )
    return _payload(context)


@_d.method("sandbox.mount.add", scope="operator.write")
async def _handle_sandbox_mount_add(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.mount.add")
    path = _require_string_param(params, "path", "params.path is required")
    manager = _require_session_manager(ctx)
    access = str(params.get("access") or "ro")
    await _validate_mount_path_for_rpc(
        manager,
        session_key,
        ctx.config,
        path=path,
        access=access,
    )
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await add_mount_grant(
        manager,
        session_key,
        path=path,
        access=access,
        scope=str(params.get("scope") or "chat"),
        config=ctx.config,
        workspace=workspace,
    )
    _remember_context_overlay(ctx, session_key=session_key, workspace=workspace, context=context)
    return _payload(context)


@_d.method("sandbox.mount.remove", scope="operator.write")
async def _handle_sandbox_mount_remove(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.mount.remove")
    path = _require_string_param(params, "path", "params.path is required")
    manager = _require_session_manager(ctx)
    await _validate_mount_path_for_rpc(
        manager,
        session_key,
        ctx.config,
        path=path,
    )
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await remove_mount_grant(
        manager,
        session_key,
        path=path,
        scope=str(params.get("scope") or ""),
        config=ctx.config,
        workspace=workspace,
    )
    _remember_context_overlay(ctx, session_key=session_key, workspace=workspace, context=context)
    return _payload(context)


@_d.method("sandbox.domain.add", scope="operator.write")
async def _handle_sandbox_domain_add(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.domain.add")
    domain = _require_string_param(params, "domain", "params.domain is required")
    _validate_domain_param(domain)
    manager = _require_session_manager(ctx)
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await add_domain_grant(
        manager,
        session_key,
        domain=domain,
        scope=str(params.get("scope") or "workspace"),
        config=ctx.config,
        workspace=workspace,
    )
    _remember_context_overlay(ctx, session_key=session_key, workspace=workspace, context=context)
    return _payload(context)


@_d.method("sandbox.domain.remove", scope="operator.write")
async def _handle_sandbox_domain_remove(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.domain.remove")
    domain = _require_string_param(params, "domain", "params.domain is required")
    _validate_domain_param(domain)
    manager = _require_session_manager(ctx)
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await remove_domain_grant(
        manager,
        session_key,
        domain=domain,
        scope=str(params.get("scope") or ""),
        config=ctx.config,
        workspace=workspace,
    )
    _remember_context_overlay(ctx, session_key=session_key, workspace=workspace, context=context)
    return _payload(context)


@_d.method("sandbox.bundle.enable", scope="operator.write")
async def _handle_sandbox_bundle_enable(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.bundle.enable")
    bundle_id = _require_bundle_id(params)
    manager = _require_session_manager(ctx)
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await enable_bundle_grant(
        manager,
        session_key,
        bundle_id=bundle_id,
        scope=str(params.get("scope") or "workspace"),
        config=ctx.config,
        workspace=workspace,
    )
    _remember_context_overlay(ctx, session_key=session_key, workspace=workspace, context=context)
    return _payload(context)


@_d.method("sandbox.bundle.disable", scope="operator.write")
async def _handle_sandbox_bundle_disable(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.bundle.disable")
    bundle_id = _require_bundle_id(params)
    manager = _require_session_manager(ctx)
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await disable_bundle_grant(
        manager,
        session_key,
        bundle_id=bundle_id,
        config=ctx.config,
        workspace=workspace,
    )
    _remember_context_overlay(ctx, session_key=session_key, workspace=workspace, context=context)
    return _payload(context)


@_d.method("sandbox.path.list", scope="operator.read")
async def _handle_sandbox_path_list(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    _require_session_key(params)
    _require_owner(ctx, "sandbox.path.list")
    path = _require_string_param(params, "path", "params.path is required")
    kind = str(params.get("kind") or "workspace").strip().lower()
    if kind not in {"workspace", "mount"}:
        raise ValueError("params.kind must be workspace or mount")

    normalized = normalize_path(path)
    browse_children = params.get("browseChildren") is True
    listing_dir = (
        normalized
        if browse_children and normalized.is_dir()
        else normalized.parent if normalized.parent != normalized else normalized
    )
    parent_target = normalized.parent if normalized.parent != normalized else normalized
    entries = []
    try:
        entries = [_parent_entry_payload(parent_target)]
        entries.extend(
            _path_entry_payload(entry)
            for entry in sorted(
                listing_dir.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.casefold()),
            )
        )
    except (OSError, RuntimeError):
        entries = []

    return {
        "path": str(normalized),
        "parentPath": str(listing_dir),
        "entries": entries,
    }


@_d.method("sandbox.path.pick", scope="operator.write")
async def _handle_sandbox_path_pick(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.path.pick")
    kind = str(params.get("kind") or "workspace").strip().lower()
    if kind not in {"workspace", "mount"}:
        raise ValueError("params.kind must be workspace or mount")

    manager = _require_session_manager(ctx)
    initial_dir = params.get("initialPath")
    selected = _pick_directory_path(
        str(initial_dir) if isinstance(initial_dir, str) and initial_dir.strip() else None
    )

    if kind == "workspace":
        return {"path": _validate_workspace_param(selected), "kind": kind}

    access = str(params.get("access") or "ro")
    await _validate_mount_path_for_rpc(
        manager,
        session_key,
        ctx.config,
        path=selected,
        access=access,
    )
    return {"path": str(normalize_path(selected)), "kind": kind}


@_d.method("sandbox.workspace.set", scope="operator.write")
async def _handle_sandbox_workspace_set(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.workspace.set")
    workspace_path = _require_one_string_param(
        params,
        ("workspace", "workspacePath"),
        "params.workspace is required",
    )
    workspace_path = _validate_workspace_param(workspace_path)
    manager = _require_session_manager(ctx)
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    current_workspace = await _workspace_for_session(manager, session_key, ctx.config)
    context = await set_workspace(
        manager,
        session_key,
        workspace_path=workspace_path,
        config=ctx.config,
        current_workspace=current_workspace,
    )
    _remember_context_overlay(
        ctx,
        session_key=session_key,
        workspace=context.workspace,
        context=context,
    )
    return _payload(context)
