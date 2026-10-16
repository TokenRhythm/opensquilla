"""Explicit local-host app launches for authenticated workspace resources.

The browser does not choose a host path, executable or command. It selects an
installed app ID and the Gateway re-resolves the authorized file immediately
before launching a fixed argument vector on its own interactive desktop.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.artifacts import _is_reparse_point
from opensquilla.gateway.auth import resolve_auth_async
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    extract_http_token,
    request_origin_allowed,
    request_principal_is_owner_async,
)
from opensquilla.gateway.scopes import is_loopback_address
from opensquilla.gateway.workspace_files import (
    WorkspaceFileChangedError,
    WorkspaceFiles,
    WorkspaceFileUnavailableError,
    _check_revision,
    _directory,
    _readable_file,
    _Workspace,
)
from opensquilla.paths import native_io_path
from opensquilla.sandbox.run_mode_policy import principal_has_host_execute

_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@dataclass(frozen=True)
class _App:
    id: str
    label: str
    executable: Path | None = None


def _interactive_desktop() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform == "win32":
        # Services can run under a user account but have no visible desktop.
        class UserObjectFlags(ctypes.Structure):
            _fields_ = [
                ("inherit", ctypes.c_int),
                ("reserved", ctypes.c_int),
                ("flags", ctypes.c_uint),
            ]

        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetProcessWindowStation.restype = ctypes.c_void_p
            user32.GetUserObjectInformationW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint),
            ]
            flags = UserObjectFlags()
            needed = ctypes.c_uint()
            return bool(
                user32.GetUserObjectInformationW(
                    user32.GetProcessWindowStation(),
                    1,
                    ctypes.byref(flags),
                    ctypes.sizeof(flags),
                    ctypes.byref(needed),
                )
                and flags.flags & 1
            )
        except (AttributeError, OSError):
            return False
    if sys.platform == "darwin":
        return Path("/System/Library/CoreServices/Finder.app").is_dir()
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _installed_apps() -> list[_App]:
    """Probe a small fixed catalogue, without shell or subprocess discovery."""
    apps = [_App("default", "Default app"), _App("reveal", "Show in folder")]
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
        programs = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = [
            (
                "vscode",
                "Visual Studio Code",
                [
                    local / "Microsoft VS Code" / "Code.exe",
                    programs / "Microsoft VS Code" / "Code.exe",
                ],
            ),
            (
                "cursor",
                "Cursor",
                [local / "cursor" / "Cursor.exe", programs / "Cursor" / "Cursor.exe"],
            ),
            ("windsurf", "Windsurf", [local / "Windsurf" / "Windsurf.exe"]),
            (
                "sublime",
                "Sublime Text",
                [
                    programs / "Sublime Text" / "sublime_text.exe",
                    programs / "Sublime Text 3" / "sublime_text.exe",
                ],
            ),
            ("zed", "Zed", [local / "Zed" / "zed.exe"]),
        ]
        for app_id, label, paths in candidates:
            executable = next(
                (path for path in paths if path.is_absolute() and path.is_file()), None
            )
            if executable is not None:
                apps.append(_App(app_id, label, executable))
        return apps
    for app_id, label, command in (
        ("vscode", "Visual Studio Code", "code"),
        ("cursor", "Cursor", "cursor"),
        ("windsurf", "Windsurf", "windsurf"),
        ("sublime", "Sublime Text", "subl"),
        ("zed", "Zed", "zed"),
    ):
        executable = shutil.which(command)
        if executable:
            apps.append(_App(app_id, label, Path(executable)))
    if sys.platform != "darwin" and shutil.which("xdg-open") is None:
        apps = [app for app in apps if app.executable is not None]
    return apps


def _local_browser(request: Request) -> bool:
    if not is_loopback_address(request.client.host if request.client else None):
        return False
    if not is_loopback_address(request.url.hostname):
        return False
    # Reverse proxies must not turn a remote browser into a local launch user.
    if request.headers.get("forwarded"):
        return False
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for and any(
        not is_loopback_address(ip.strip()) for ip in forwarded_for.split(",")
    ):
        return False
    origin = request.headers.get("origin", "")
    if origin and origin != "opensquilla-app://desktop":
        try:
            if not is_loopback_address(urlsplit(origin).hostname):
                return False
        except ValueError:
            return False
    return True


async def _access_reason(request: Request, config: GatewayConfig) -> str | None:
    if not request_origin_allowed(request, config):
        return "forbidden-origin"
    token = extract_http_token(request)
    principal = await resolve_auth_async(
        config,
        {"token": token} if token else {},
        "operator",
        peer_ip=request.client.host if request.client else None,
    )
    if not principal or not principal.is_owner or not principal_has_host_execute(principal):
        return "host-permission-required"
    if not _local_browser(request):
        return "remote-client"
    if not await asyncio.to_thread(_interactive_desktop):
        return "desktop-unavailable"
    return None


def _windows_reveal_file(target: Path) -> None:
    """Select one Unicode file with the Shell API, without Explorer CLI parsing."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    shell32.SHParseDisplayName.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_uint),
    ]
    shell32.SHParseDisplayName.restype = ctypes.c_long
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long

    initialized = int(ole32.CoInitializeEx(None, 2))
    # RPC_E_CHANGED_MODE means this worker already has a COM apartment. Do not
    # uninitialize somebody else's apartment, but Shell calls can still use it.
    if initialized & 0x80000000 and initialized & 0xFFFFFFFF != 0x80010106:
        raise OSError(initialized, "Could not initialize the desktop shell")
    pidl = ctypes.c_void_p()
    try:
        parsed = int(shell32.SHParseDisplayName(str(target), None, ctypes.byref(pidl), 0, None))
        if parsed & 0x80000000 or not pidl.value:
            raise OSError(parsed, "Could not resolve the file in the desktop shell")
        # With cidl=0, pidl is the absolute item to select, not its parent folder.
        selected = int(shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0))
        if selected & 0x80000000:
            raise OSError(selected, "Could not select the file in its folder")
    finally:
        if pidl.value:
            ole32.CoTaskMemFree(pidl)
        if not initialized & 0x80000000:
            ole32.CoUninitialize()


def _launch(app: _App, target: Path) -> None:
    if app.id == "reveal" and sys.platform == "win32":
        _windows_reveal_file(target)
        return
    # The child should not inherit Gateway/provider secrets or Electron flags.
    allowed = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "LANG",
        "LC_ALL",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if app.id == "default" and sys.platform == "win32":
        # Explorer delegates files to their registered default app and keeps
        # association lookup out of this process. Its environment is scrubbed
        # too, unlike an in-process ShellExecute/os.startfile call.
        executable = Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
        command = [str(executable), str(target)]
    elif app.id in ("default", "reveal") and sys.platform == "darwin":
        command = ["/usr/bin/open", *(["-R"] if app.id == "reveal" else []), str(target)]
    elif app.id in ("default", "reveal"):
        executable = shutil.which("xdg-open")
        if not executable:
            raise WorkspaceFileUnavailableError()
        command = [executable, str(target.parent if app.id == "reveal" else target)]
    elif app.executable is not None and app.executable.is_file():
        command = [str(app.executable), "--", str(target)]
    else:
        raise WorkspaceFileUnavailableError()
    subprocess.Popen(
        command,
        shell=False,
        close_fds=True,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _open_verified(workspace: _Workspace, path: str, revision: str, app_id: str) -> None:
    app = next((app for app in _installed_apps() if app.id == app_id), None)
    if app is None:
        raise WorkspaceFileUnavailableError()
    target, _, actual_revision = _readable_file(workspace, path)
    _check_revision(actual_revision, revision)
    _launch(app, target)


def _root_scope(request: Request, project_id: Any = None) -> tuple[str, str]:
    session_key = request.headers.get("x-opensquilla-session-key", "")
    if project_id is not None:
        if (
            not isinstance(project_id, str)
            or not project_id
            or len(project_id) > 256
            or any(ord(char) < 32 for char in project_id)
            or session_key
        ):
            raise ValueError("Expected exactly one project or session scope")
        return "", project_id
    if not session_key:
        raise ValueError("Expected a project or session scope")
    return session_key, ""


def _query_root_scope(request: Request) -> tuple[str, str]:
    # Token-in-query remains an existing HTTP authentication compatibility path.
    if set(request.query_params) - {"projectId", "token"}:
        raise ValueError("Unexpected query parameter")
    if len(request.query_params.getlist("projectId")) > 1:
        raise ValueError("Repeated project scope")
    return _root_scope(request, request.query_params.get("projectId"))


async def _scope_workspace(service: WorkspaceFiles, scope: tuple[str, str]) -> _Workspace:
    session_key, project_id = scope
    if project_id:
        return await service.project_workspace(project_id)
    return await service.workspace(session_key)


def _root_identity(workspace: _Workspace) -> tuple[int, int]:
    root = _directory(workspace, "")
    if root != workspace.root or _is_reparse_point(root):
        raise WorkspaceFileUnavailableError()
    metadata = native_io_path(root).lstat()
    # Content changes must not invalidate an open-directory action.
    return metadata.st_dev, metadata.st_ino


def _open_verified_root(workspace: _Workspace, identity: tuple[int, int], app_id: str) -> None:
    app = next((app for app in _installed_apps() if app.id == app_id), None)
    if app is None or _root_identity(workspace) != identity:
        raise WorkspaceFileUnavailableError()
    # Root reveal enters the directory; file reveal continues selecting the file.
    if app.id == "reveal":
        app = _App("default", app.label)
    _launch(app, workspace.root)


def register_workspace_file_app_routes(
    app: Starlette, *, config: GatewayConfig, service: WorkspaceFiles
) -> None:
    async def catalogue(request: Request) -> JSONResponse:
        reason = await _access_reason(request, config)
        if reason is not None:
            status = 403 if reason == "forbidden-origin" else 200
            return JSONResponse(
                {"available": False, "host": "gateway", "reason": reason, "apps": []},
                status_code=status,
                headers=_HEADERS,
            )
        try:
            workspace = await _scope_workspace(service, _query_root_scope(request))
            await asyncio.to_thread(_root_identity, workspace)
            apps = await asyncio.to_thread(_installed_apps)
        except (OSError, ValueError):
            return JSONResponse(
                {
                    "available": False,
                    "host": "gateway",
                    "reason": "workspace-unavailable",
                    "apps": [],
                },
                headers=_HEADERS,
            )
        return JSONResponse(
            {
                "available": bool(apps),
                "host": "gateway",
                "apps": [{"id": app.id, "label": app.label} for app in apps],
            },
            headers=_HEADERS,
        )

    async def root_metadata(request: Request) -> JSONResponse:
        if not request_origin_allowed(request, config):
            return JSONResponse({"code": "FORBIDDEN_ORIGIN"}, status_code=403, headers=_HEADERS)
        if not await request_principal_is_owner_async(config, request):
            return JSONResponse({"code": "OWNER_REQUIRED"}, status_code=403, headers=_HEADERS)
        try:
            scope = _query_root_scope(request)
        except ValueError:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            workspace = await _scope_workspace(service, scope)
            await asyncio.to_thread(_root_identity, workspace)
            if await _scope_workspace(service, scope) != workspace:
                raise WorkspaceFileUnavailableError()
        except (OSError, ValueError):
            return JSONResponse(
                {"code": "WORKSPACE_FILE_UNAVAILABLE"}, status_code=404, headers=_HEADERS
            )
        return JSONResponse(
            {
                "kind": "workspace-root",
                "name": workspace.root.name or str(workspace.root),
                "path": str(workspace.root),
                "workspaceBinding": workspace.binding,
            },
            headers=_HEADERS,
        )

    async def open_root(request: Request) -> JSONResponse:
        reason = await _access_reason(request, config)
        if reason is not None:
            return JSONResponse(
                {"code": "HOST_APP_UNAVAILABLE", "reason": reason},
                status_code=403,
                headers=_HEADERS,
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            values: Any = json.loads(body)
            if (
                not isinstance(values, dict)
                or set(values)
                not in (
                    {"workspaceBinding", "appId"},
                    {"workspaceBinding", "appId", "projectId"},
                )
                or any(not isinstance(value, str) or not value for value in values.values())
                or len(values["workspaceBinding"]) != 64
                or len(values["appId"]) > 32
                or set(request.query_params) - {"token"}
            ):
                raise ValueError()
            scope = _root_scope(request, values.get("projectId"))
        except (ValueError, UnicodeError):
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            workspace = await _scope_workspace(service, scope)
            if workspace.binding != values["workspaceBinding"]:
                raise WorkspaceFileUnavailableError()
            identity = await asyncio.to_thread(_root_identity, workspace)
            if await _scope_workspace(service, scope) != workspace:
                raise WorkspaceFileUnavailableError()
            reason = await _access_reason(request, config)
            if reason is not None:
                return JSONResponse(
                    {"code": "HOST_APP_UNAVAILABLE", "reason": reason},
                    status_code=403,
                    headers=_HEADERS,
                )
            if await _scope_workspace(service, scope) != workspace:
                raise WorkspaceFileUnavailableError()
            await asyncio.to_thread(_open_verified_root, workspace, identity, values["appId"])
        except (OSError, ValueError):
            return JSONResponse(
                {"code": "WORKSPACE_FILE_UNAVAILABLE"}, status_code=404, headers=_HEADERS
            )
        return JSONResponse(
            {"ok": True, "host": "gateway", "appId": values["appId"]}, headers=_HEADERS
        )

    async def open_in_app(request: Request) -> JSONResponse:
        reason = await _access_reason(request, config)
        if reason is not None:
            return JSONResponse(
                {"code": "HOST_APP_UNAVAILABLE", "reason": reason},
                status_code=403,
                headers=_HEADERS,
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            values: Any = json.loads(body)
            if (
                not isinstance(values, dict)
                or set(values)
                != {
                    "path",
                    "workspaceBinding",
                    "expectedRevision",
                    "appId",
                }
                or any(not isinstance(value, str) or not value for value in values.values())
            ):
                raise ValueError()
            if (
                len(values["path"]) > 4096
                or Path(values["path"]).is_absolute()
                or len(values["workspaceBinding"]) != 64
                or len(values["expectedRevision"]) != 64
                or len(values["appId"]) > 32
            ):
                raise ValueError()
        except (ValueError, UnicodeError):
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            session_key = request.headers.get("x-opensquilla-session-key", "")
            workspace = await service.workspace(session_key)
            if workspace.binding != values["workspaceBinding"]:
                raise WorkspaceFileUnavailableError()
            _, _, revision = await asyncio.to_thread(_readable_file, workspace, values["path"])
            _check_revision(revision, values["expectedRevision"])
            if await service.workspace(session_key) != workspace:
                raise WorkspaceFileUnavailableError()
            # Host capabilities may have been revoked while filesystem and
            # session checks ran. A menu opened earlier never grants authority.
            reason = await _access_reason(request, config)
            if reason is not None:
                return JSONResponse(
                    {"code": "HOST_APP_UNAVAILABLE", "reason": reason},
                    status_code=403,
                    headers=_HEADERS,
                )
            if await service.workspace(session_key) != workspace:
                raise WorkspaceFileUnavailableError()
            await asyncio.to_thread(
                _open_verified,
                workspace,
                values["path"],
                values["expectedRevision"],
                values["appId"],
            )
        except WorkspaceFileChangedError:
            return JSONResponse(
                {"code": "WORKSPACE_FILE_CHANGED"}, status_code=409, headers=_HEADERS
            )
        except (OSError, ValueError):
            return JSONResponse(
                {"code": "WORKSPACE_FILE_UNAVAILABLE"}, status_code=404, headers=_HEADERS
            )
        return JSONResponse(
            {"ok": True, "host": "gateway", "appId": values["appId"]}, headers=_HEADERS
        )

    app.router.routes.extend(
        [
            Route("/api/v1/workspace-files/apps", catalogue, methods=["GET"]),
            Route("/api/v1/workspace-files/open", open_in_app, methods=["POST"]),
            Route("/api/v1/workspace-files/root", root_metadata, methods=["GET"]),
            Route("/api/v1/workspace-files/open-root", open_root, methods=["POST"]),
        ]
    )
