"""Dependency installation for skills, including verified managed toolchains."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from opensquilla.skills.toolchains import (
    DownloadVerificationError,
    ToolchainError,
    ToolchainProbeError,
    UnknownComponentError,
    UnsupportedToolchainError,
    install_component,
)
from opensquilla.skills.toolchains.manager import toolchains_root
from opensquilla.skills.types import SkillInstallSpec

log = structlog.get_logger(__name__)

# Strict allowlists to prevent arbitrary shell execution
_BREW_FORMULA_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_@.-]*$")
_UV_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*(\[[a-zA-Z0-9,._-]+\])?$")
_URL_RE = re.compile(r"^https://[a-zA-Z0-9._/-]+$")
_TOOLCHAIN_INSTALL_TIMEOUT_SECONDS = 30 * 60.0
_TOOLCHAIN_INSTALL_TASKS: dict[tuple[str, str], asyncio.Task[Any]] = {}

DepProgressCallback = Callable[[SkillInstallSpec, int, int], None]


@dataclass
class DepResult:
    """Result of installing a single dependency."""

    kind: str
    identifier: str
    success: bool
    message: str = ""


async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """Run a subprocess with timeout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", "Timed out"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def install_brew(spec: SkillInstallSpec) -> DepResult:
    """Install via Homebrew."""
    formula = spec.formula or spec.package or spec.id
    if not formula or not _BREW_FORMULA_RE.match(formula):
        return DepResult(
            kind="brew", identifier=formula, success=False, message=f"Invalid formula: {formula}"
        )

    code, out, err = await _run(["brew", "install", formula])
    if code == 0:
        return DepResult(kind="brew", identifier=formula, success=True, message="Installed")
    return DepResult(kind="brew", identifier=formula, success=False, message=err.strip()[:200])


async def install_uv(spec: SkillInstallSpec) -> DepResult:
    """Install a Python package via uv."""
    package = spec.package or spec.module or spec.id
    if not package or not _UV_PACKAGE_RE.match(package):
        return DepResult(
            kind="uv", identifier=package, success=False, message=f"Invalid package: {package}"
        )

    code, out, err = await _run(["uv", "pip", "install", package])
    if code == 0:
        return DepResult(kind="uv", identifier=package, success=True, message="Installed")
    return DepResult(kind="uv", identifier=package, success=False, message=err.strip()[:200])


async def install_download(spec: SkillInstallSpec) -> DepResult:
    """Download a binary from a URL."""
    import shutil
    from pathlib import Path

    url = spec.url
    if not url or not _URL_RE.match(url):
        return DepResult(
            kind="download", identifier=url or "", success=False, message=f"Invalid URL: {url}"
        )

    bin_name = spec.bins[0] if spec.bins else url.rsplit("/", 1)[-1]
    dest = Path.home() / ".local" / "bin" / bin_name

    code, out, err = await _run(["curl", "-fsSL", "-o", str(dest), url])
    if code != 0:
        return DepResult(kind="download", identifier=url, success=False, message=err.strip()[:200])

    dest.chmod(0o755)
    # Verify it landed on PATH
    if shutil.which(bin_name):
        return DepResult(
            kind="download", identifier=bin_name, success=True, message=f"Downloaded to {dest}"
        )
    return DepResult(
        kind="download",
        identifier=bin_name,
        success=True,
        message=f"Downloaded to {dest} (may need PATH update)",
    )


async def install_toolchain(
    spec: SkillInstallSpec,
    *,
    progress_cb: DepProgressCallback | None = None,
) -> DepResult:
    """Install a code-catalogued, verified managed toolchain component."""

    component_id = spec.id.strip()
    if not component_id:
        return DepResult(
            kind="toolchain",
            identifier="",
            success=False,
            message="Managed toolchain install is missing a component id.",
        )

    def _forward_progress(current: int, total: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(spec, current, total)
        except Exception as exc:  # noqa: BLE001 - UI progress must not abort setup
            log.warning(
                "deps.install_progress_callback_failed",
                component_id=component_id,
                error=str(exc),
            )

    loop = asyncio.get_running_loop()
    install_key = (str(toolchains_root().absolute()), component_id)
    install_task = _TOOLCHAIN_INSTALL_TASKS.get(install_key)
    if install_task is not None and (
        install_task.done() or install_task.get_loop() is not loop
    ):
        _TOOLCHAIN_INSTALL_TASKS.pop(install_key, None)
        install_task = None
    if install_task is None:
        install_future = (
            asyncio.to_thread(
                install_component,
                component_id,
                progress_cb=_forward_progress,
            )
            if progress_cb is not None
            else asyncio.to_thread(install_component, component_id)
        )
        install_task = asyncio.create_task(install_future)
        _TOOLCHAIN_INSTALL_TASKS[install_key] = install_task

        def discard_finished(done: asyncio.Task[Any]) -> None:
            if _TOOLCHAIN_INSTALL_TASKS.get(install_key) is done:
                _TOOLCHAIN_INSTALL_TASKS.pop(install_key, None)
            if not done.cancelled():
                # A timed-out caller leaves the shielded worker alive. Consume
                # its eventual exception even when nobody retries and awaits it.
                done.exception()

        install_task.add_done_callback(discard_finished)

    try:
        receipt = await asyncio.wait_for(
            # A timeout applies to this caller, not to the worker thread (which
            # Python cannot terminate). Shield and retain the task so concurrent
            # or retrying requests single-flight on the same component until the
            # real install finishes.
            asyncio.shield(install_task),
            timeout=_TOOLCHAIN_INSTALL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return DepResult(
            kind="toolchain",
            identifier=component_id,
            success=False,
            message=(
                "Timed out waiting for managed toolchain setup after 30 minutes; "
                "the verified setup worker may still be finishing. Wait a few minutes, "
                "refresh status, then check network access and disk space before retrying."
            ),
        )
    except UnknownComponentError as exc:
        return DepResult(
            kind="toolchain",
            identifier=component_id,
            success=False,
            message=f"Unknown managed toolchain component: {exc}",
        )
    except UnsupportedToolchainError as exc:
        return DepResult(
            kind="toolchain",
            identifier=component_id,
            success=False,
            message=f"Managed toolchain is unavailable on this platform: {exc}",
        )
    except DownloadVerificationError as exc:
        return DepResult(
            kind="toolchain",
            identifier=component_id,
            success=False,
            message=(
                "Managed toolchain download failed integrity verification and was not "
                f"activated: {exc}"
            ),
        )
    except ToolchainProbeError as exc:
        return DepResult(
            kind="toolchain",
            identifier=component_id,
            success=False,
            message=f"Managed toolchain self-check failed and was not activated: {exc}",
        )
    except ToolchainError as exc:
        return DepResult(
            kind="toolchain",
            identifier=component_id,
            success=False,
            message=f"Managed toolchain setup failed: {exc}",
        )

    return DepResult(
        kind="toolchain",
        identifier=component_id,
        success=True,
        message=f"Installed verified {component_id} toolchain ({receipt.version}).",
    )


_INSTALLERS = {
    "brew": install_brew,
    "uv": install_uv,
    "download": install_download,
    "toolchain": install_toolchain,
}


async def install_deps(
    specs: list[SkillInstallSpec],
    progress_cb: DepProgressCallback | None = None,
) -> list[DepResult]:
    """Install all dependencies for a skill. Returns results per spec."""
    results = []
    for spec in specs:
        handler = _INSTALLERS.get(spec.kind)
        if handler is None:
            results.append(
                DepResult(
                    kind=spec.kind,
                    identifier=spec.id,
                    success=False,
                    message=f"Unsupported install kind: {spec.kind}",
                )
            )
            continue
        try:
            if spec.kind == "toolchain":
                result = await install_toolchain(spec, progress_cb=progress_cb)
            else:
                result = await handler(spec)
        except FileNotFoundError:
            result = DepResult(
                kind=spec.kind,
                identifier=spec.id,
                success=False,
                message=f"Tool not found for kind '{spec.kind}' (brew/uv/curl)",
            )
        except Exception as exc:
            result = DepResult(
                kind=spec.kind,
                identifier=spec.id,
                success=False,
                message=f"Error: {exc}",
            )
        results.append(result)
        log.info("deps.install", kind=spec.kind, id=spec.id, success=result.success)
    return results
