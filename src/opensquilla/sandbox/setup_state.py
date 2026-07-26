"""Platform-neutral sandbox setup state."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class SandboxSetupState(StrEnum):
    NOT_SETUP = "not_setup"
    SETTING_UP = "setting_up"
    READY = "ready"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SetupResult:
    state: SandboxSetupState
    platform: str
    message: str
    requires_admin: bool = False
    detail: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "state": self.state.value,
            "platform": self.platform,
            "message": self.message,
            "requiresAdmin": self.requires_admin,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class WindowsSetupSupport:
    default_backend_available: bool
    ctypes_available: bool
    token_api_available: bool
    acl_api_available: bool
    setup_ready: bool
    proxy_allowlist_enforced: bool


def _platform_name(platform: str | None = None) -> str:
    value = platform or sys.platform
    if value.startswith("win"):
        return "win32"
    if value == "darwin":
        return "darwin"
    if value.startswith("linux"):
        return "linux"
    return value


def _requires_admin(platform: str) -> bool:
    _ = platform
    return False


def setup_status_payload(
    state: SandboxSetupState,
    *,
    platform: str | None = None,
    message: str | None = None,
    detail: str | None = None,
) -> dict[str, object]:
    normalized_platform = _platform_name(platform)
    default_message = {
        SandboxSetupState.NOT_SETUP: "Sandbox setup has not been completed.",
        SandboxSetupState.SETTING_UP: "Sandbox setup is running.",
        SandboxSetupState.READY: "Sandbox setup is ready.",
        SandboxSetupState.FAILED: "Sandbox setup failed.",
        SandboxSetupState.UNAVAILABLE: "Sandbox setup is unavailable on this host.",
    }[state]
    return SetupResult(
        state=state,
        platform=normalized_platform,
        message=message or default_message,
        requires_admin=_requires_admin(normalized_platform),
        detail=detail,
    ).to_payload()


async def current_sandbox_setup_status(config: Any) -> SetupResult:
    platform = _platform_name()
    if platform == "win32":
        return await _windows_setup_status(config)
    if platform == "darwin":
        return await _macos_setup_status(config)
    return await _portable_setup_status(config, platform=platform)


async def ensure_sandbox_setup(config: Any) -> SetupResult:
    platform = _platform_name()
    if platform == "win32":
        return await _ensure_windows_setup(config)
    if platform == "darwin":
        return await _ensure_macos_setup(config)
    return await _ensure_portable_setup(config, platform=platform)


async def _macos_setup_status(config: Any) -> SetupResult:
    _ = config
    if _macos_seatbelt_available():
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="darwin",
            message="macOS Seatbelt sandbox is ready.",
            requires_admin=False,
            detail="sandbox-exec=ready",
        )
    return SetupResult(
        state=SandboxSetupState.UNAVAILABLE,
        platform="darwin",
        message="macOS Seatbelt sandbox is unavailable.",
        requires_admin=False,
        detail="sandbox-exec=missing",
    )


async def _ensure_macos_setup(config: Any) -> SetupResult:
    return await _macos_setup_status(config)


def _macos_seatbelt_available() -> bool:
    from opensquilla.sandbox.backend.seatbelt import SeatbeltBackend

    return SeatbeltBackend().available()


async def _windows_setup_status(config: Any) -> SetupResult:
    _ = config
    return _windows_default_setup_result()


async def _ensure_windows_setup(config: Any) -> SetupResult:
    # Windows setup can wait for UAC consent and for the elevated helper to
    # finish. Keep that blocking Win32 work off the gateway event loop so
    # health checks, shutdown, and setup-status RPCs stay responsive.
    return await asyncio.to_thread(_ensure_windows_setup_sync, config)


def _ensure_windows_setup_sync(config: Any) -> SetupResult:
    _ = config
    support = _probe_windows_sandbox_support()
    if support.default_backend_available and support.proxy_allowlist_enforced:
        return _windows_default_setup_result()
    if (
        support.ctypes_available
        and support.token_api_available
        and support.acl_api_available
        and (not support.setup_ready or not support.proxy_allowlist_enforced)
    ):
        marker_path = _windows_setup_marker_path()
        if not _windows_process_is_admin():
            try:
                _run_windows_setup_helper_elevated(marker_path)
            except OSError as exc:
                return SetupResult(
                    state=SandboxSetupState.FAILED,
                    platform="win32",
                    message="Windows default sandbox setup failed.",
                    requires_admin=True,
                    detail=str(exc),
                )
            result = _windows_default_setup_result()
            if result.state is SandboxSetupState.READY:
                return result
            incomplete_detail = (
                _windows_setup_helper_report_detail(marker_path)
                or result.detail
                or result.message
            )
            return SetupResult(
                state=SandboxSetupState.FAILED,
                platform="win32",
                message="Windows default sandbox setup failed.",
                requires_admin=True,
                detail=f"elevated_setup_incomplete: {incomplete_detail}",
            )
        try:
            network = _establish_windows_network_setup(marker_path)
            _write_windows_setup_marker(marker_path, network=network)
            return SetupResult(
                state=SandboxSetupState.READY,
                platform="win32",
                message="Windows default sandbox is ready.",
                requires_admin=False,
                detail="proxy_allowlist=ready",
            )
        except OSError as exc:
            return SetupResult(
                state=SandboxSetupState.FAILED,
                platform="win32",
                message="Windows default sandbox setup failed.",
                requires_admin=True,
                detail=str(exc),
            )
    return _windows_default_setup_result()


def _windows_default_setup_result() -> SetupResult:
    support = _probe_windows_sandbox_support()
    if support.default_backend_available and support.proxy_allowlist_enforced:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="Windows default sandbox is ready.",
            requires_admin=False,
            detail="proxy_allowlist=ready",
        )

    reasons: list[str] = []
    if not support.ctypes_available:
        reasons.append("ctypes=missing")
    if not support.token_api_available:
        reasons.append("token_api=not ready")
    if not support.acl_api_available:
        reasons.append("acl_api=not ready")
    if not support.setup_ready:
        reasons.append("setup=not ready")
    elif not support.proxy_allowlist_enforced:
        reasons.append("network_boundary=not ready")
    recoverable_setup = (
        support.ctypes_available
        and support.token_api_available
        and support.acl_api_available
        and (not support.setup_ready or not support.proxy_allowlist_enforced)
    )
    return SetupResult(
        state=SandboxSetupState.NOT_SETUP if recoverable_setup else SandboxSetupState.UNAVAILABLE,
        platform="win32",
        message="Windows default sandbox setup is required.",
        requires_admin=True,
        detail=", ".join(reasons),
    )


def _probe_windows_sandbox_support() -> WindowsSetupSupport:
    from opensquilla.sandbox.backend.windows_default_support import (
        probe_windows_default_support,
    )

    support = probe_windows_default_support(proxy_ports=_windows_marker_proxy_ports())
    return WindowsSetupSupport(
        default_backend_available=support.default_backend_available,
        ctypes_available=support.ctypes_available,
        token_api_available=support.token_api_available,
        acl_api_available=support.acl_api_available,
        setup_ready=support.setup_ready,
        proxy_allowlist_enforced=support.proxy_allowlist_enforced,
    )


def _windows_marker_proxy_ports() -> tuple[int, ...]:
    from opensquilla.sandbox.backend.windows_default_setup import (
        default_setup_marker_path,
        read_setup_marker,
    )

    marker = read_setup_marker(default_setup_marker_path())
    if marker is None or marker.network is None:
        return ()
    return marker.network.allowed_proxy_ports


def _windows_setup_marker_path() -> Path:
    from opensquilla.sandbox.backend.windows_default_setup import default_setup_marker_path

    return default_setup_marker_path()


def _windows_process_is_admin() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _establish_windows_network_setup(marker_path: Path):
    from opensquilla.sandbox.backend.windows_default_network import WindowsNetworkSetup
    from opensquilla.sandbox.backend.windows_default_setup import (
        establish_windows_network_setup,
    )

    network = establish_windows_network_setup(marker_path)
    if not isinstance(network, WindowsNetworkSetup):
        raise OSError("windows network setup did not return network marker")
    return network


def _write_windows_setup_marker(path: Path, *, network=None) -> None:
    from opensquilla.sandbox.backend.windows_default_setup import write_setup_marker

    write_setup_marker(path, network=network)


def _run_windows_setup_helper_elevated(path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default_setup import run_elevated_setup_helper

    run_elevated_setup_helper(path)


def _windows_setup_helper_report_detail(path: Path) -> str | None:
    from opensquilla.sandbox.backend.windows_default_setup import read_setup_helper_report

    report = read_setup_helper_report(path)
    if report is None:
        return None
    return report.get("detail") or report.get("state")


async def _portable_setup_status(config: Any, *, platform: str) -> SetupResult:
    _ = config
    return SetupResult(
        state=SandboxSetupState.READY,
        platform=platform,
        message="Sandbox setup is ready.",
        requires_admin=False,
    )


async def _ensure_portable_setup(config: Any, *, platform: str) -> SetupResult:
    _ = config
    return SetupResult(
        state=SandboxSetupState.READY,
        platform=platform,
        message="Sandbox setup is ready.",
        requires_admin=False,
    )


__all__ = [
    "SandboxSetupState",
    "SetupResult",
    "WindowsSetupSupport",
    "current_sandbox_setup_status",
    "ensure_sandbox_setup",
    "setup_status_payload",
]
