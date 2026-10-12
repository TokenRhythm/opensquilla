"""Platform-neutral OpenTUI host discovery and process lifecycle helpers."""

from __future__ import annotations

import asyncio
import importlib
import os
import platform as platform_module
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from opensquilla import __version__

HOST_PROTOCOL_VERSION = 1
SOURCE_HOST_ENV = "OPENSQUILLA_TUI_DEV_SOURCE_HOST"


class HostFailureReason(StrEnum):
    """Stable failure categories exposed by the OpenTUI host boundary."""

    MISSING = "missing"
    VERSION_MISMATCH = "version_mismatch"
    SPAWN = "spawn"
    READY_TIMEOUT = "ready_timeout"
    TRANSPORT = "transport"
    TERMINAL_UNSUPPORTED = "terminal_unsupported"
    RUNTIME_CRASH = "runtime_crash"


class HostRuntimeError(RuntimeError):
    """A typed host-boundary failure with a stable machine-readable reason."""

    def __init__(self, message: str, *, reason: HostFailureReason) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class HostArtifact:
    """Resolved command and working directory for one OpenTUI host."""

    command: tuple[str, ...]
    cwd: Path | None
    main_script: Path | None = None
    product_version: str = __version__
    host_version: str = "unknown"
    protocol_version: int = HOST_PROTOCOL_VERSION
    platform: str = "unknown"
    arch: str = "unknown"
    build_id: str = "source"
    source: str = "companion"


@dataclass(frozen=True)
class HostArtifactResolver:
    """Resolve an exact companion, or an explicitly requested source host."""

    package_dir: Path
    main_script: Path
    runtime_bin: str | None = None
    use_source_host: bool = False
    product_version: str = __version__
    protocol_version: int = HOST_PROTOCOL_VERSION
    companion_module: Any | None = None

    def resolve(self) -> HostArtifact:
        if self.use_source_host or self.runtime_bin is not None:
            return self._resolve_source_host()
        return self._resolve_companion()

    def _resolve_companion(self) -> HostArtifact:
        try:
            module = self.companion_module or importlib.import_module("opensquilla_tui_host")
            metadata = module.host_metadata()
            command = tuple(module.host_command())
        except (ImportError, ModuleNotFoundError) as exc:
            raise HostRuntimeError(
                "OpenTUI companion is not installed. This release does not publish one; "
                "use '--ui plain', or run the source host from a development checkout.",
                reason=HostFailureReason.MISSING,
            ) from exc
        except Exception as exc:
            raise HostRuntimeError(
                f"OpenTUI companion is unavailable: {exc}",
                reason=HostFailureReason.MISSING,
            ) from exc

        if not command or not all(isinstance(item, str) and item for item in command):
            raise HostRuntimeError(
                "OpenTUI companion returned an invalid host command",
                reason=HostFailureReason.MISSING,
            )
        self._validate_companion_metadata(metadata)
        executable = Path(command[0])
        return HostArtifact(
            command=command,
            cwd=executable.parent,
            product_version=str(metadata.product_version),
            host_version=str(metadata.host_version),
            protocol_version=int(metadata.protocol_version),
            platform=str(metadata.platform),
            arch=str(metadata.arch),
            build_id=str(metadata.build_id),
            source="companion",
        )

    def _validate_companion_metadata(self, metadata: Any) -> None:
        product_version = str(getattr(metadata, "product_version", ""))
        host_version = str(getattr(metadata, "host_version", ""))
        protocol_version = getattr(metadata, "protocol_version", None)
        companion_platform = str(getattr(metadata, "platform", ""))
        companion_arch = str(getattr(metadata, "arch", ""))
        if product_version != self.product_version or host_version != self.product_version:
            raise HostRuntimeError(
                "OpenTUI companion version mismatch: "
                f"core={self.product_version}, product={product_version or 'unknown'}, "
                f"host={host_version or 'unknown'}",
                reason=HostFailureReason.VERSION_MISMATCH,
            )
        if protocol_version != self.protocol_version:
            raise HostRuntimeError(
                "OpenTUI companion protocol mismatch: "
                f"core={self.protocol_version}, host={protocol_version!r}",
                reason=HostFailureReason.VERSION_MISMATCH,
            )
        expected_platform = _current_platform()
        expected_arch = _current_arch()
        if companion_platform != expected_platform or companion_arch != expected_arch:
            raise HostRuntimeError(
                "OpenTUI companion platform mismatch: "
                f"current={expected_platform}/{expected_arch}, "
                f"host={companion_platform or 'unknown'}/{companion_arch or 'unknown'}",
                reason=HostFailureReason.VERSION_MISMATCH,
            )

    def _resolve_source_host(self) -> HostArtifact:
        runtime_bin = self.runtime_bin or shutil.which("bun")
        if runtime_bin is None:
            raise HostRuntimeError(
                "Bun is not installed or is not on PATH",
                reason=HostFailureReason.MISSING,
            )
        if not shutil.which(runtime_bin):
            raise HostRuntimeError(
                f"OpenTUI host runtime is not executable: {runtime_bin}",
                reason=HostFailureReason.MISSING,
            )

        opentui_core_dir = self.package_dir / "node_modules" / "@opentui" / "core"
        if not opentui_core_dir.exists():
            raise HostRuntimeError(
                "OpenTUI host dependency @opentui/core is not installed. "
                f"Run: bun install --cwd {self.package_dir}",
                reason=HostFailureReason.MISSING,
            )
        if not self.main_script.exists():
            raise HostRuntimeError(
                f"OpenTUI host entrypoint is missing: {self.main_script}",
                reason=HostFailureReason.MISSING,
            )
        return HostArtifact(
            command=(runtime_bin, str(self.main_script)),
            cwd=self.package_dir,
            main_script=self.main_script,
            product_version=self.product_version,
            host_version="0.0.0-dev",
            protocol_version=self.protocol_version,
            platform=_current_platform(),
            arch=_current_arch(),
            build_id="source",
            source="source",
        )


def source_host_requested(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the explicit source-host developer switch is enabled."""

    values = os.environ if env is None else env
    return values.get(SOURCE_HOST_ENV, "").strip().lower() in {"1", "true", "yes"}


def _current_platform() -> str:
    return {"darwin": "darwin", "linux": "linux", "win32": "win32"}.get(sys.platform, sys.platform)


def _current_arch() -> str:
    value = platform_module.machine().lower()
    return {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }.get(value, value)


class HostProcessController:
    """Spawn and stop a host without POSIX-only process APIs."""

    async def spawn(
        self,
        artifact: HostArtifact,
        *,
        env: Mapping[str, str],
    ) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *artifact.command,
                cwd=str(artifact.cwd) if artifact.cwd is not None else None,
                env=dict(env),
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise HostRuntimeError(
                f"OpenTUI host runtime is not executable: {artifact.command[0]}",
                reason=HostFailureReason.SPAWN,
            ) from exc
        except OSError as exc:
            raise HostRuntimeError(
                f"OpenTUI host could not be started: {exc}",
                reason=HostFailureReason.SPAWN,
            ) from exc

    async def stop(
        self,
        process: asyncio.subprocess.Process,
        *,
        graceful_timeout: float = 1.0,
    ) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=graceful_timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
