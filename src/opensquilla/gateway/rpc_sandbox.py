"""RPC handlers for per-session sandbox run context."""

from __future__ import annotations

import asyncio
import copy
import json
import plistlib
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.project_workspace_runtime import (
    authoritative_project_run_context,
    map_project_workspace_error,
    resolve_session_project_workspace,
)
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.token_store import TokenRecord, TokenStore
from opensquilla.project_workspaces import (
    ProjectWorkspaceGuard,
    ProjectWorkspaceStateError,
)
from opensquilla.run_mode import (
    RunMode,
    display_name,
    execution_target,
    normalize_run_mode,
)
from opensquilla.sandbox.domain_validation import validate_domain_pattern
from opensquilla.sandbox.escalation import remember_resolved_run_context
from opensquilla.sandbox.file_policy import builtin_deny_write_paths
from opensquilla.sandbox.package_bundles import expand_package_bundle
from opensquilla.sandbox.path_validation import (
    decide_path_access,
    normalize_mount_access,
    normalize_path,
)
from opensquilla.sandbox.policy_store import (
    PolicyVersionConflict,
    SandboxPolicyStore,
)
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    RUN_MODE_PREFERENCE_KEY,
    RunContext,
    get_run_context,
    normalize_workspace_path,
    resolve_default_run_mode,
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
from opensquilla.sandbox.run_mode_policy import (
    coerce_run_mode_for_principal,
    run_mode_allowed_for_principal,
)
from opensquilla.sandbox.runtime_launcher import ChildRole, internal_child_argv
from opensquilla.sandbox.setup_runtime import (
    current_sandbox_capability_report,
    current_sandbox_setup_runtime_status,
    ensure_sandbox_setup_auto,
)
from opensquilla.sandbox.status import status_payload
from opensquilla.session.keys import parse_agent_id

_d = get_dispatcher()
_RUN_MODE_PREFERENCE_CHANGED_EVENT = "sandbox.run_mode.preference.changed"
_WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _require_params(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    return params


def _sandbox_policy_store(ctx: RpcContext) -> SandboxPolicyStore:
    state_dir = getattr(ctx.config, "state_dir", None)
    if not state_dir:
        raise RpcUnavailableError("Sandbox policy storage is unavailable.")
    return SandboxPolicyStore(Path(str(state_dir)) / "sessions.db")


def _sandbox_token_store(ctx: RpcContext) -> TokenStore:
    state_dir = getattr(ctx.config, "state_dir", None)
    if not state_dir:
        raise RpcUnavailableError("Sandbox token storage is unavailable.")
    return TokenStore(Path(str(state_dir)) / "sessions.db")


def _sandbox_token_payload(record: TokenRecord) -> dict[str, Any]:
    return {
        "publicId": record.public_id,
        "name": record.name,
        "capabilities": sorted(record.capabilities),
        "createdAt": record.created_at,
        "lastUsedAt": record.last_used_at,
        "lastPeer": record.last_peer,
    }


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


def _path_entry_payload(path: Path, *, selection_kind: str) -> dict[str, Any]:
    name = str(path.name or str(path))
    entry_kind = "directory" if path.is_dir() else "file"
    payload = {
        "name": name,
        "path": str(path),
        "kind": entry_kind,
        "selectable": entry_kind == "directory" or selection_kind == "mount",
    }
    if name.startswith("."):
        payload["hidden"] = True
    return payload


_MACOS_APPKIT_RESOURCES = Path("/System/Library/Frameworks/AppKit.framework/Resources")
_MACOS_APPLET_INFO_KEYS_TO_REMOVE = (
    "CFBundleSignature",
    "LSMinimumSystemVersionByArchitecture",
    "LSRequiresCarbon",
    "NSAppleEventsUsageDescription",
    "NSAppleMusicUsageDescription",
    "NSCalendarsUsageDescription",
    "NSCameraUsageDescription",
    "NSContactsUsageDescription",
    "NSHomeKitUsageDescription",
    "NSMicrophoneUsageDescription",
    "NSPhotoLibraryUsageDescription",
    "NSRemindersUsageDescription",
    "NSSiriUsageDescription",
    "NSSystemAdministrationUsageDescription",
)
_MACOS_DIRECTORY_PICKER_SCRIPT = """
ObjC.import("AppKit");
ObjC.import("Foundation");

const initialDirectory = __INITIAL_DIRECTORY__;
const resultPath = __RESULT_PATH__;

function writeSelection(selection) {
    const data = $(selection).dataUsingEncoding($.NSUTF8StringEncoding);
    data.writeToFileAtomically(resultPath, true);
}

function run() {
    const app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyRegular);

    const panel = $.NSOpenPanel.openPanel;
    panel.canChooseFiles = false;
    panel.canChooseDirectories = true;
    panel.allowsMultipleSelection = false;
    panel.canCreateDirectories = true;
    panel.resolvesAliases = true;

    if (initialDirectory) {
        panel.directoryURL = $.NSURL.fileURLWithPath(initialDirectory);
    }

    app.activateIgnoringOtherApps(true);
    if (panel.runModal === $.NSModalResponseOK) {
        writeSelection(ObjC.unwrap(panel.URL.path));
    } else {
        writeSelection("");
    }
}
"""


def _macos_directory_picker_script(
    initial_dir: str | None,
    result_path: Path,
) -> str:
    return _MACOS_DIRECTORY_PICKER_SCRIPT.replace(
        "__INITIAL_DIRECTORY__",
        json.dumps(initial_dir),
    ).replace(
        "__RESULT_PATH__",
        json.dumps(str(result_path)),
    )


def _prepare_macos_directory_picker_bundle(app_path: Path) -> None:
    info_path = app_path / "Contents" / "Info.plist"
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)

    for key in _MACOS_APPLET_INFO_KEYS_TO_REMOVE:
        info.pop(key, None)
    info["CFBundleAllowMixedLocalizations"] = True
    info["CFBundleIdentifier"] = "ai.opensquilla.directory-picker"

    with info_path.open("wb") as handle:
        plistlib.dump(info, handle)

    resources = app_path / "Contents" / "Resources"
    localizations = list(_MACOS_APPKIT_RESOURCES.glob("*.lproj"))
    if not localizations:
        localizations = [Path("en.lproj")]
    for localization in localizations:
        (resources / localization.name).mkdir(exist_ok=True)


def _macos_picker_error(result: subprocess.CompletedProcess[str]) -> RpcUnavailableError:
    detail = result.stderr.strip()
    message = "Directory picker is not available on this host."
    if detail:
        message = f"{message} {detail}"
    return RpcUnavailableError(message)


def _pick_directory_path_macos(initial_dir: str | None = None) -> str | None:
    try:
        with tempfile.TemporaryDirectory(prefix="opensquilla-directory-picker-") as temp_dir:
            temp_path = Path(temp_dir)
            app_path = temp_path / "OpenSquilla Directory Picker.app"
            result_path = temp_path / "selection.txt"
            script = _macos_directory_picker_script(initial_dir, result_path)

            compile_result = subprocess.run(
                [
                    "osacompile",
                    "-l",
                    "JavaScript",
                    "-o",
                    str(app_path),
                    "-e",
                    script,
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            if compile_result.returncode != 0:
                raise _macos_picker_error(compile_result)

            _prepare_macos_directory_picker_bundle(app_path)
            launch_result = subprocess.run(
                ["open", "-W", "-n", str(app_path)],
                capture_output=True,
                check=False,
                text=True,
            )
            if launch_result.returncode != 0:
                raise _macos_picker_error(launch_result)
            if not result_path.is_file():
                raise RpcUnavailableError("Directory picker closed without returning a result.")

            return result_path.read_text(encoding="utf-8") or None
    except (OSError, plistlib.InvalidFileException) as exc:
        raise RpcUnavailableError("Directory picker is not available on this host.") from exc


def _pick_directory_path(initial_dir: str | None = None) -> str | None:
    if sys.platform == "darwin":
        return _pick_directory_path_macos(initial_dir)

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

    return selected or None


async def _pick_directory_path_windows(initial_dir: str | None = None) -> str | None:
    arguments: list[str] = []
    if initial_dir:
        arguments.append(initial_dir)
    command = internal_child_argv(ChildRole.DIRECTORY_PICKER, args=arguments)

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_WINDOWS_CREATE_NO_WINDOW,
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        message = "Directory picker is not available on this host."
        if detail:
            message = f"{message} {detail}"
        raise RpcUnavailableError(message)

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpcUnavailableError("Directory picker returned an invalid response.") from exc
    selected = payload.get("path")
    if selected is None:
        return None
    if not isinstance(selected, str):
        raise RpcUnavailableError("Directory picker returned an invalid response.")
    return selected


async def _pick_directory_path_async(initial_dir: str | None = None) -> str | None:
    if sys.platform == "win32":
        return await _pick_directory_path_windows(initial_dir)
    return await asyncio.to_thread(_pick_directory_path, initial_dir)


def _require_session_manager(ctx: RpcContext) -> Any:
    manager = getattr(ctx, "session_manager", None)
    if manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    return manager


def _require_owner(ctx: RpcContext, method: str) -> None:
    if not getattr(ctx.principal, "is_owner", False):
        raise RpcHandlerError("UNAUTHORIZED", f"{method} requires owner principal.")


def _run_mode_preference_registry() -> Any:
    from opensquilla.gateway.websocket import get_registry

    return get_registry()


def _runtime_preference_storage(ctx: RpcContext) -> Any:
    storage = get_session_storage(getattr(ctx, "session_manager", None))
    if storage is None:
        raise RpcUnavailableError("Session storage is not configured")
    return storage


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


def _default_workspace_for_session(
    session: Any | None,
    session_key: str,
    config: Any,
) -> str | None:
    agent_id = parse_agent_id(session_key)
    session_agent_id = getattr(session, "agent_id", None) if session is not None else None
    if isinstance(session_agent_id, str) and session_agent_id:
        agent_id = session_agent_id
    workspace = resolve_agent_workspace_dir(agent_id, config)
    return str(workspace) if workspace is not None else None


async def _path_list_start(
    params: dict[str, Any],
    ctx: RpcContext,
    session_key: str,
) -> Path:
    raw_path = params.get("path")
    if raw_path is not None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("params.path must be a non-empty string")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            base_raw = params.get("basePath")
            if (
                not isinstance(base_raw, str)
                or not base_raw.strip()
                or not Path(base_raw).expanduser().is_absolute()
            ):
                raise ValueError("relative path requires absolute basePath")
            base = Path(base_raw).expanduser().resolve(strict=True)
            if not stat.S_ISDIR(base.stat().st_mode):
                raise NotADirectoryError(str(base))
            candidate = base / candidate
        return candidate.resolve(strict=True)

    manager = _require_session_manager(ctx)
    session = await _session_for_key(manager, session_key)
    if session is not None and getattr(session, "workspace_id", None):
        storage = get_session_storage(manager)
        if storage is None:
            raise RpcUnavailableError("Session storage is not configured")
        try:
            validated = await resolve_session_project_workspace(storage, session)
        except ProjectWorkspaceStateError as exc:
            raise map_project_workspace_error(
                exc,
                owner=ctx.principal.is_owner,
            ) from exc
        assert validated is not None
        return Path(validated.canonical_path)

    workspace = _default_workspace_for_session(session, session_key, ctx.config)
    if workspace is not None:
        try:
            candidate = Path(workspace).expanduser().resolve(strict=True)
            workspace_mode = candidate.stat().st_mode
        except (FileNotFoundError, NotADirectoryError, RuntimeError):
            pass
        else:
            if stat.S_ISDIR(workspace_mode):
                return candidate
    return Path.home().resolve(strict=True)


async def _context_for_session(
    session_manager: Any,
    session_key: str,
    config: Any,
    *,
    owner: bool,
    session: Any | None = None,
) -> tuple[Any, RunContext, ProjectWorkspaceGuard | None]:
    session = session or await _session_for_key(session_manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    default_workspace = _default_workspace_for_session(session, session_key, config)
    storage = get_session_storage(session_manager)
    if storage is None:
        context = await get_run_context(
            session_manager,
            session_key,
            config=config,
            workspace=default_workspace,
            session_node=session,
        )
        return session, context, None
    try:
        context, guard = await authoritative_project_run_context(
            storage=storage,
            session_manager=session_manager,
            session=session,
            config=config,
            default_workspace=default_workspace,
        )
    except ProjectWorkspaceStateError as exc:
        raise map_project_workspace_error(exc, owner=owner) from exc
    return session, context, guard


class _AuthoritativeSessionManagerView:
    """Feed mutation helpers a validated base without a preflight write."""

    def __init__(
        self,
        session_manager: Any,
        session: Any,
        context: RunContext,
    ) -> None:
        self._session_manager = session_manager
        self._session_key = session.session_key
        self._session = copy.copy(session)
        raw_origin = getattr(session, "origin", None)
        origin = dict(raw_origin) if isinstance(raw_origin, dict) else {}
        origin[RUN_CONTEXT_ORIGIN_KEY] = context.to_origin_payload()
        self._session.origin = origin

    async def get_session(self, session_key: str) -> Any | None:
        if session_key == self._session_key:
            return self._session
        return await self._session_manager.get_session(session_key)

    async def update(self, session_key: str, **fields: Any) -> Any:
        updated = await self._session_manager.update(session_key, **fields)
        if session_key == self._session_key:
            self._session = copy.copy(updated)
        return updated

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session_manager, name)


def _mutation_manager(
    session_manager: Any,
    session: Any,
    context: RunContext,
    guard: ProjectWorkspaceGuard | None,
) -> Any:
    if guard is None:
        return session_manager
    return _AuthoritativeSessionManagerView(
        session_manager,
        session,
        context,
    )


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
    owner: bool,
) -> None:
    session = await _session_for_key(session_manager, session_key)
    if session is None:
        workspace = _default_workspace_for_session(None, session_key, config)
        context = await get_run_context(
            session_manager,
            session_key,
            config=config,
            workspace=workspace,
        )
    else:
        _session, context, _guard = await _context_for_session(
            session_manager,
            session_key,
            config,
            owner=owner,
            session=session,
        )
        workspace = context.workspace
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


@_d.method("sandbox.capability.status", scope="operator.read")
async def _handle_sandbox_capability_status(params: dict | None, ctx: RpcContext) -> dict:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    refresh = (params or {}).get("refresh", False)
    if not isinstance(refresh, bool):
        raise ValueError("params.refresh must be a boolean")
    report = await current_sandbox_capability_report(
        ctx.config,
        force_refresh=refresh,
    )
    return report.to_payload()


@_d.method("sandbox.policy.get", scope="operator.read")
async def _handle_sandbox_policy_get(params: dict | None, ctx: RpcContext) -> dict:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    return _sandbox_policy_store(ctx).read().to_public_dict()


@_d.method("sandbox.policy.defaults", scope="operator.read")
async def _handle_sandbox_policy_defaults(params: dict | None, ctx: RpcContext) -> dict:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    from opensquilla.runtime_packs import load_default_catalog
    from opensquilla.sandbox.runtime_launcher import bundled_runtime_resolver
    from opensquilla.sandbox.runtime_manifest import (
        BundledRuntimeResolver,
        RuntimeManifest,
        RuntimeManifestError,
        runtime_target,
    )

    runtime_versions: dict[str, dict[str, object]] = {}
    detected_runtime_target: str | None = None

    # This legacy projection must stay cheap: new clients load installation and
    # integrity state independently through sandbox.runtime.status. In
    # particular, policy.defaults must never hash installed Runtime Pack
    # payloads while the rest of the Settings page is waiting for its response.
    try:
        detected_runtime_target = runtime_target()
        catalog = load_default_catalog(require_complete=True)
        for key in ("python", "node", "gitBash"):
            descriptor = catalog.descriptor(detected_runtime_target, key)
            if descriptor is None:
                continue
            runtime_versions[key] = {
                "version": descriptor.version,
                "available": False,
            }
    except (OSError, RuntimeManifestError, ValueError):
        runtime_versions = {}

    resolver = None
    if not runtime_versions:
        try:
            resolver = bundled_runtime_resolver()
            if resolver is None:
                # Source checkouts intentionally omit developer/. The checked-in
                # layout remains authoritative for legacy version display.
                candidate = (
                    Path(__file__).resolve().parents[3]
                    / "desktop"
                    / "electron"
                    / "runtime"
                    / "runtime-manifest.json"
                )
                if candidate.is_file():
                    resolver = BundledRuntimeResolver(
                        RuntimeManifest.from_path(candidate),
                        resource_root=candidate.parent / "developer",
                    )
            if resolver is not None:
                detected_runtime_target = resolver.target
                assets = resolver.manifest.assets.get(resolver.target, {})
                executable_paths = resolver.executable_paths()
                for key, asset in assets.items():
                    executable_names = tuple(asset.executables)
                    runtime_versions[key] = {
                        "version": asset.version,
                        "available": bool(executable_names)
                        and all(
                            executable_paths.get(name, Path()).is_file()
                            for name in executable_names
                        ),
                    }
        except (OSError, RuntimeManifestError, ValueError):
            resolver = None
            detected_runtime_target = None
            runtime_versions = {}

    return {
        "builtinDenyWritePaths": [str(path) for path in builtin_deny_write_paths()],
        "runtimeTarget": detected_runtime_target,
        "runtimeVersions": runtime_versions,
    }


def _runtime_component_param(params: dict | None) -> str:
    values = _require_params(params)
    component_id = values.get("componentId")
    if component_id not in {"python", "node", "gitBash"}:
        raise ValueError("params.componentId must be python, node, or gitBash")
    return str(component_id)


def _runtime_state_dir(ctx: RpcContext) -> str | Path | None:
    value = getattr(ctx.config, "state_dir", None)
    return value if value and str(value).strip() else None


async def _runtime_status_payload(ctx: RpcContext) -> dict[str, object]:
    from opensquilla.runtime_packs import status_snapshot

    status = await asyncio.to_thread(status_snapshot, _runtime_state_dir(ctx))
    return status.to_public_dict()


@_d.method("sandbox.runtime.status", scope="operator.read")
async def _handle_sandbox_runtime_status(params: dict | None, ctx: RpcContext) -> dict:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    return await _runtime_status_payload(ctx)


@_d.method("sandbox.runtime.install", scope="operator.admin")
async def _handle_sandbox_runtime_install(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.runtime_packs import start_install

    _require_owner(ctx, "sandbox.runtime.install")
    component_id = _runtime_component_param(params)
    operation = await asyncio.to_thread(
        start_install,
        component_id,
        _runtime_state_dir(ctx),
    )
    return {"operation": operation.to_public_dict()}


@_d.method("sandbox.runtime.cancel", scope="operator.admin")
async def _handle_sandbox_runtime_cancel(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.runtime_packs import RuntimePackError, cancel_install

    _require_owner(ctx, "sandbox.runtime.cancel")
    component_id = _runtime_component_param(params)
    operation_id = _require_string_param(
        _require_params(params),
        "operationId",
        "params.operationId is required",
    ).strip()
    try:
        operation = await asyncio.to_thread(
            cancel_install,
            component_id,
            operation_id,
            _runtime_state_dir(ctx),
        )
    except RuntimePackError as exc:
        raise RpcHandlerError(
            "RUNTIME_JOB_CONFLICT",
            "The Runtime Pack operation changed; refresh its status and try again.",
        ) from exc
    return {"operation": operation.to_public_dict()}


@_d.method("sandbox.runtime.remove", scope="operator.admin")
async def _handle_sandbox_runtime_remove(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.runtime_packs import remove_component

    _require_owner(ctx, "sandbox.runtime.remove")
    component_id = _runtime_component_param(params)
    operation = await asyncio.to_thread(
        remove_component,
        component_id,
        _runtime_state_dir(ctx),
    )
    return {"operation": operation.to_public_dict()}


@_d.method("sandbox.policy.update", scope="operator.write")
async def _handle_sandbox_policy_update(params: dict | None, ctx: RpcContext) -> dict:
    _require_owner(ctx, "sandbox.policy.update")
    values = _require_params(params)
    base_version = values.get("basePolicyVersion")
    if not isinstance(base_version, int) or isinstance(base_version, bool):
        raise ValueError("params.basePolicyVersion must be an integer")
    policy = values.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("params.policy must be an object")
    try:
        saved = _sandbox_policy_store(ctx).compare_and_swap(base_version, policy)
    except PolicyVersionConflict as exc:
        raise RpcHandlerError(
            "POLICY_VERSION_CONFLICT",
            "The sandbox policy changed in another client.",
            details={"currentPolicy": exc.current_policy.to_public_dict()},
        ) from exc
    return saved.to_public_dict()


@_d.method("sandbox.tokens.list", scope="operator.read")
async def _handle_sandbox_token_list(params: dict | None, ctx: RpcContext) -> dict:
    _require_owner(ctx, "sandbox.tokens.list")
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    return {
        "tokens": [
            _sandbox_token_payload(record)
            for record in _sandbox_token_store(ctx).list_active()
        ]
    }


@_d.method("sandbox.tokens.create", scope="operator.write")
async def _handle_sandbox_token_create(params: dict | None, ctx: RpcContext) -> dict:
    _require_owner(ctx, "sandbox.tokens.create")
    values = _require_params(params)
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("params.name must be a non-empty string")
    host_execute = values.get("hostExecute", True)
    if not isinstance(host_execute, bool):
        raise ValueError("params.hostExecute must be a boolean")
    capabilities = {"task.read", "task.submit"}
    if host_execute:
        capabilities.add("host.execute")
    issued = _sandbox_token_store(ctx).create(
        name=name.strip(),
        roles={"operator"},
        scopes={"operator.read", "operator.write"},
        capabilities=capabilities,
    )
    return {
        "token": issued.token,
        "record": _sandbox_token_payload(issued.record),
    }


@_d.method("sandbox.tokens.revoke", scope="operator.write")
async def _handle_sandbox_token_revoke(params: dict | None, ctx: RpcContext) -> dict:
    _require_owner(ctx, "sandbox.tokens.revoke")
    values = _require_params(params)
    public_id = values.get("publicId")
    if not isinstance(public_id, str) or not public_id.strip():
        raise ValueError("params.publicId must be a non-empty string")
    return {
        "publicId": public_id.strip(),
        "revoked": _sandbox_token_store(ctx).revoke(public_id.strip()),
    }


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
        _session, context, _guard = await _context_for_session(
            manager,
            session_key,
            ctx.config,
            owner=ctx.principal.is_owner,
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
    report = await current_sandbox_capability_report(ctx.config)
    if not report.available:
        raise RpcHandlerError(
            "SANDBOX_CAPABILITY_UNAVAILABLE",
            "Safe mode cannot be enabled because live sandbox verification failed.",
            details=report.to_payload(),
        )


@_d.method("sandbox.run_mode.preference.get", scope="operator.read")
async def _handle_run_mode_preference_get(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, str]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    mode, source = await resolve_default_run_mode(ctx.session_manager, ctx.config)
    mode = coerce_run_mode_for_principal(mode, ctx.principal)
    return {"runMode": mode.value, "source": source}


@_d.method("sandbox.run_mode.preference.set", scope="operator.write")
async def _handle_run_mode_preference_set(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, str]:
    _require_owner(ctx, "sandbox.run_mode.preference.set")
    params = _require_params(params)
    mode = normalize_run_mode(params.get("runMode"))
    await _require_sandbox_setup_ready_for_mode(ctx, mode)
    storage = _runtime_preference_storage(ctx)
    confirmed = await storage.set_runtime_preference(
        RUN_MODE_PREFERENCE_KEY,
        mode.value,
    )
    payload = {"runMode": confirmed, "source": "preference"}
    await _run_mode_preference_registry().broadcast(
        _RUN_MODE_PREFERENCE_CHANGED_EVENT,
        payload,
    )
    return payload


@_d.method("sandbox.run_context.get", scope="operator.read")
async def _handle_sandbox_run_context_get(params: dict | None, ctx: RpcContext) -> dict:
    params = _require_params(params)
    session_key = _require_session_key(params)
    manager = _require_session_manager(ctx)
    _session, context, _guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
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
    manager = _require_session_manager(ctx)
    session = await _session_for_key(manager, session_key)
    base_context = None
    guard = None
    if session is not None:
        session, base_context, guard = await _context_for_session(
            manager,
            session_key,
            ctx.config,
            owner=ctx.principal.is_owner,
            session=session,
        )
    await _require_sandbox_setup_ready_for_mode(ctx, run_mode)
    if session is None:
        session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    if base_context is None:
        session, base_context, guard = await _context_for_session(
            manager,
            session_key,
            ctx.config,
            owner=ctx.principal.is_owner,
            session=session,
        )
    mutation_manager = _mutation_manager(
        manager,
        session,
        base_context,
        guard,
    )
    context = await set_run_mode(
        mutation_manager,
        session_key,
        run_mode,
        config=ctx.config,
        workspace=base_context.workspace,
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
        owner=ctx.principal.is_owner,
    )
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    session, base_context, guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    mutation_manager = _mutation_manager(manager, session, base_context, guard)
    workspace = base_context.workspace
    context = await add_mount_grant(
        mutation_manager,
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
        owner=ctx.principal.is_owner,
    )
    session = await _ensure_session_for_set(manager, session_key)
    if session is None:
        raise KeyError(f"Session not found: {session_key}")
    session, base_context, guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    mutation_manager = _mutation_manager(manager, session, base_context, guard)
    workspace = base_context.workspace
    context = await remove_mount_grant(
        mutation_manager,
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
    session, base_context, guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    mutation_manager = _mutation_manager(manager, session, base_context, guard)
    workspace = base_context.workspace
    context = await add_domain_grant(
        mutation_manager,
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
    session, base_context, guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    mutation_manager = _mutation_manager(manager, session, base_context, guard)
    workspace = base_context.workspace
    context = await remove_domain_grant(
        mutation_manager,
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
    session, base_context, guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    mutation_manager = _mutation_manager(manager, session, base_context, guard)
    workspace = base_context.workspace
    context = await enable_bundle_grant(
        mutation_manager,
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
    session, base_context, guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    mutation_manager = _mutation_manager(manager, session, base_context, guard)
    workspace = base_context.workspace
    context = await disable_bundle_grant(
        mutation_manager,
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
    session_key = _require_session_key(params)
    _require_owner(ctx, "sandbox.path.list")
    kind = str(params.get("kind") or "workspace").strip().lower()
    if kind not in {"workspace", "mount"}:
        raise ValueError("params.kind must be workspace or mount")

    listing_dir = await _path_list_start(params, ctx, session_key)
    if not stat.S_ISDIR(listing_dir.stat().st_mode):
        raise NotADirectoryError(str(listing_dir))
    entries = [
        _path_entry_payload(entry, selection_kind=kind)
        for entry in sorted(
            listing_dir.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.casefold()),
        )
    ]
    current_path = str(listing_dir)
    parent_path = str(listing_dir.parent) if listing_dir.parent != listing_dir else None

    return {
        "currentPath": current_path,
        "path": current_path,
        "parentPath": parent_path,
        "entries": entries,
        "systemPickerAvailable": sys.platform != "linux",
    }


@_d.method("sandbox.path.create-directory", scope="operator.write")
async def _handle_sandbox_path_create_directory(
    params: dict | None,
    ctx: RpcContext,
) -> dict:
    params = _require_params(params)
    _require_session_key(params)
    _require_owner(ctx, "sandbox.path.create-directory")
    kind = str(params.get("kind") or "workspace").strip().lower()
    if kind not in {"workspace", "mount"}:
        raise ValueError("params.kind must be workspace or mount")

    raw_parent = params.get("parentPath")
    if not isinstance(raw_parent, str) or not raw_parent.strip():
        raise ValueError("params.parentPath must be a non-empty absolute path")
    parent = Path(raw_parent).expanduser()
    if not parent.is_absolute():
        raise ValueError("params.parentPath must be a non-empty absolute path")
    parent = parent.resolve(strict=True)
    if not stat.S_ISDIR(parent.stat().st_mode):
        raise NotADirectoryError(str(parent))

    raw_name = params.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError("params.name must be a non-empty directory name")
    name = raw_name.strip()
    if (
        name in {".", ".."}
        or Path(name).is_absolute()
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise ValueError("params.name must be a single directory name")

    created = parent / name
    created.mkdir()
    return {
        "path": str(created.resolve(strict=True)),
        "name": name,
        "kind": "directory",
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
    selected = await _pick_directory_path_async(
        str(initial_dir) if isinstance(initial_dir, str) and initial_dir.strip() else None
    )
    if selected is None:
        return {"path": None, "kind": kind}

    if kind == "workspace":
        return {"path": _validate_workspace_param(selected), "kind": kind}

    access = str(params.get("access") or "ro")
    await _validate_mount_path_for_rpc(
        manager,
        session_key,
        ctx.config,
        path=selected,
        access=access,
        owner=ctx.principal.is_owner,
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
    if getattr(session, "workspace_id", None) is not None:
        raise RpcHandlerError(
            "PROJECT_WORKSPACE_FIXED",
            "A project-bound session cannot change its workspace.",
        )
    _session, base_context, _guard = await _context_for_session(
        manager,
        session_key,
        ctx.config,
        owner=ctx.principal.is_owner,
        session=session,
    )
    current_workspace = base_context.workspace
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
    storage = get_session_storage(manager)
    invalidate_adoption = getattr(storage, "invalidate_legacy_project_adoption", None)
    if callable(invalidate_adoption):
        invalidate_adoption()
    return _payload(context)
