"""Runtime wrapper for automatic sandbox setup."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from opensquilla.sandbox.capability_service import (
    CapabilityReport,
    capability_report_from_setup,
    required_safe_capabilities,
)
from opensquilla.sandbox.setup_state import (
    SandboxSetupState,
    SetupResult,
    current_sandbox_setup_status,
    ensure_sandbox_setup,
)

_LOCK = asyncio.Lock()
_SETTING_UP = False
_LAST_RESULT: SetupResult | None = None
_CAPABILITY_CACHE: dict[str, tuple[float, CapabilityReport]] = {}
_CAPABILITY_LOCK = asyncio.Lock()
_CAPABILITY_PROBE_TIMEOUT_SECONDS = 30.0
# The fingerprint already includes the selected backend, setup marker details,
# and executable timestamp. Re-running the native canary every few seconds only
# churns Windows ACL state; one successful check per app session is sufficient,
# with the explicit refresh RPC available for diagnostics.
_CAPABILITY_CACHE_TTL_SECONDS = 3600.0
_CAPABILITY_FAILURE_CACHE_TTL_SECONDS = 10.0
_PROCESS_CANARY_MARKER = "opensquilla-safe-probe"
_DENY_WRITE_CANARY_MARKER = "opensquilla-deny-write-ok"
_DENY_READ_CANARY_MARKER = "opensquilla-deny-read-ok"
_WINDOWS_DENIAL_CANARY_SCRIPT = (
    "@echo off\r\n"
    "setlocal DisableDelayedExpansion\r\n"
    'set "operation=%~1"\r\n'
    'set "target=%~2"\r\n'
    'set "marker=%~3"\r\n'
    'if "%operation%"=="write" goto try_write\r\n'
    'if "%operation%"=="read" goto try_read\r\n'
    "exit /b 90\r\n"
    ":try_write\r\n"
    'copy /y nul "%target%" >nul 2>&1\r\n'
    "if errorlevel 1 goto denied\r\n"
    "echo unexpected-write\r\n"
    "exit /b 41\r\n"
    ":try_read\r\n"
    'type "%target%" >nul 2>&1\r\n'
    "if errorlevel 1 goto denied\r\n"
    "echo unexpected-read\r\n"
    "exit /b 42\r\n"
    ":denied\r\n"
    "echo %marker%\r\n"
    "exit /b 0\r\n"
)


def _capability_cache_ttl(report: CapabilityReport) -> float:
    return (
        _CAPABILITY_CACHE_TTL_SECONDS
        if report.available
        else _CAPABILITY_FAILURE_CACHE_TTL_SECONDS
    )


def _capability_cache_is_fresh(
    cached: tuple[float, CapabilityReport],
) -> bool:
    cached_at, report = cached
    return time.monotonic() - cached_at < _capability_cache_ttl(report)


async def current_sandbox_setup_runtime_status(config: Any) -> SetupResult:
    if _SETTING_UP:
        return SetupResult(
            state=SandboxSetupState.SETTING_UP,
            platform="auto",
            message="Sandbox setup is running.",
            requires_admin=False,
        )
    if _LAST_RESULT is not None and _LAST_RESULT.state is SandboxSetupState.FAILED:
        return _LAST_RESULT
    return await current_sandbox_setup_status(config)


async def current_sandbox_capability_report(
    config: Any,
    *,
    force_refresh: bool = False,
) -> CapabilityReport:
    setup = await current_sandbox_setup_runtime_status(config)
    from opensquilla.sandbox.integration import get_runtime

    runtime = get_runtime()
    backend_object = getattr(runtime, "backend", None)
    backend = str(
        getattr(
            backend_object,
            "name",
            getattr(getattr(config, "sandbox", None), "backend", "auto"),
        )
    )
    prerequisite = capability_report_from_setup(setup, backend=backend)
    if setup.state is not SandboxSetupState.READY:
        return prerequisite
    fingerprint = _capability_fingerprint(
        config,
        setup=setup,
        backend=backend,
        backend_object=backend_object,
    )
    if force_refresh:
        _CAPABILITY_CACHE.pop(fingerprint, None)
    cached = _CAPABILITY_CACHE.get(fingerprint)
    if cached is not None and _capability_cache_is_fresh(cached):
        return cached[1]
    async with _CAPABILITY_LOCK:
        # A force refresh invalidates once before waiting for the single-flight
        # lock. A concurrent waiter then consumes the result produced by the
        # first caller instead of invalidating it again and running a second
        # expensive native canary.
        cached = _CAPABILITY_CACHE.get(fingerprint)
        if cached is not None and _capability_cache_is_fresh(cached):
            return cached[1]
        try:
            report = await asyncio.wait_for(
                _probe_runtime_capabilities(
                    config,
                    setup=setup,
                    backend=backend,
                    backend_object=backend_object,
                ),
                timeout=_CAPABILITY_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            report = _unavailable_capability_report(
                setup,
                backend=backend,
                code="probe_timeout",
                reason="Sandbox capability verification timed out.",
            )
        except Exception as exc:  # noqa: BLE001 - capability boundary
            report = _unavailable_capability_report(
                setup,
                backend=backend,
                code="probe_failed",
                reason=f"Sandbox capability verification failed: {type(exc).__name__}",
            )
        _CAPABILITY_CACHE[fingerprint] = (time.monotonic(), report)
        return report


def _capability_fingerprint(
    config: Any,
    *,
    setup: SetupResult,
    backend: str,
    backend_object: Any,
) -> str:
    executable = Path(sys.executable)
    try:
        executable_stamp = executable.stat().st_mtime_ns
    except OSError:
        executable_stamp = 0
    sandbox = getattr(config, "sandbox", None)
    return "|".join(
        (
            str(setup.platform),
            str(setup.detail or setup.message),
            backend,
            type(backend_object).__qualname__,
            str(bool(getattr(sandbox, "sandbox", True))),
            str(executable),
            str(executable_stamp),
        )
    )


def _unavailable_capability_report(
    setup: SetupResult,
    *,
    backend: str,
    code: str,
    reason: str,
    capabilities: frozenset[str] = frozenset(),
) -> CapabilityReport:
    return CapabilityReport(
        available=False,
        backend=backend,
        platform=setup.platform,
        code=code,
        reason=reason,
        setup_supported=True,
        restart_required=False,
        probe_version=1,
        capabilities=capabilities,
    )


def _native_denial_canary_argv(
    target: Path,
    *,
    operation: str,
    marker: str,
    windows_launcher: Path | None = None,
) -> tuple[str, ...]:
    if os.name == "nt":
        if windows_launcher is None:
            raise ValueError("Windows capability canary launcher is required")
        return (
            "cmd.exe",
            "/d",
            "/c",
            "call",
            str(windows_launcher),
            operation,
            str(target),
            marker,
        )
    if operation == "write":
        # POSIX shells apply redirections left-to-right. Redirect stderr first
        # so an expected Seatbelt failure while opening the protected output
        # path does not leak a diagnostic that makes the exact canary result
        # look like a capability failure.
        attempt = 'printf changed 2>/dev/null > "$1"'
        unexpected_exit = 41
    else:
        attempt = 'cat -- "$1" >/dev/null 2>&1'
        unexpected_exit = 42
    script = (
        f"if {attempt}; then "
        f"printf unexpected-{operation}; exit {unexpected_exit}; "
        f"else printf {marker}; fi"
    )
    return ("/bin/sh", "-c", script, "opensquilla-capability-probe", str(target))


def _exact_process_result(result: Any, marker: str) -> bool:
    return (
        getattr(result, "returncode", None) == 0
        and str(getattr(result, "stdout", "")).strip() == marker
        and str(getattr(result, "stderr", "")).strip() == ""
    )


def _filesystem_probe_read_succeeded(message: object) -> bool:
    normalized = str(message).rstrip("\r\n")
    return normalized in {"worker-ok", "1\tworker-ok"}


async def _probe_runtime_capabilities(
    config: Any,
    *,
    setup: SetupResult,
    backend: str,
    backend_object: Any,
) -> CapabilityReport:
    """Run real process/filesystem/deny canaries through the selected backend."""

    from opensquilla.sandbox.file_policy import compile_safe_file_profile
    from opensquilla.sandbox.operation_runtime import SandboxOperation
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.policy_models import (
        FilePolicySettings,
    )
    from opensquilla.sandbox.policy_models import (
        SandboxPolicy as StoredSandboxPolicy,
    )
    from opensquilla.sandbox.types import NetworkMode, SandboxRequest, SecurityLevel

    if backend_object is None or backend == "noop" or not backend_object.available():
        return _unavailable_capability_report(
            setup,
            backend=backend,
            code="backend_unavailable",
            reason="The selected sandbox backend is unavailable.",
        )
    domains = backend_object.operation_domains_supported()
    if "filesystem" not in domains:
        return _unavailable_capability_report(
            setup,
            backend=backend,
            code="filesystem_worker_unavailable",
            reason="The selected sandbox backend has no filesystem worker.",
        )

    state_dir = str(getattr(config, "state_dir", "") or "").strip()
    temp_parent = Path(state_dir) / "capability-probes" if state_dir else None
    if temp_parent is not None:
        temp_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="opensquilla-safe-probe-",
        dir=str(temp_parent) if temp_parent is not None else None,
    ) as raw_root:
        root = Path(raw_root)
        workspace = root / "workspace"
        authority = root / "authority"
        protected = workspace / "protected"
        workspace.mkdir()
        authority.mkdir()
        protected.mkdir()
        authority_secret = authority / "authority.txt"
        authority_secret.write_text("authority-secret", encoding="utf-8")
        protected_target = protected / "must-remain.txt"
        protected_target.write_text("unchanged", encoding="utf-8")
        worker_target = workspace / "worker.txt"
        windows_canary_launcher = workspace / "opensquilla-capability-canary.cmd"
        if os.name == "nt":
            windows_canary_launcher.write_text(
                _WINDOWS_DENIAL_CANARY_SCRIPT,
                encoding="ascii",
                newline="",
            )
        # Capability probes run under a restricted token for the current user
        # so they cannot disturb the shared offline account's ACL journal. A
        # deny ACE added to a directory is inherited by new children, but
        # Windows does not retroactively copy it to this pre-existing canary.
        # Target the canary itself so this check measures live deny enforcement
        # instead of directory inheritance timing.
        stored_policy = StoredSandboxPolicy(
            files=FilePolicySettings(custom_deny_write_paths=[str(protected_target)])
        )
        profile = compile_safe_file_profile(
            stored_policy,
            authority_roots=(authority,),
            writable_roots=(workspace,),
            # The canary validates ACL and deny-carveout mechanics inside its
            # disposable root. Projecting the user's entire home here makes a
            # capability check both slow and invasive, and can outlive the RPC
            # timeout while holding the Windows execution lease.
            home=workspace,
            env={
                **os.environ,
                "HOME": str(workspace),
                "USERPROFILE": str(workspace),
            },
        )
        sandbox_settings = getattr(config, "sandbox", None)
        if sandbox_settings is None:
            from opensquilla.sandbox.config import SandboxSettings

            sandbox_settings = SandboxSettings()
        process_policy = replace(
            build_policy(
                SecurityLevel.STANDARD,
                "capability.probe",
                workspace,
                sandbox_settings,
            ),
            file_system=profile,
            network=NetworkMode.NONE,
        )
        process_argv = (
            ("cmd.exe", "/d", "/c", "echo", _PROCESS_CANARY_MARKER)
            if os.name == "nt"
            else ("/bin/sh", "-c", f"printf {_PROCESS_CANARY_MARKER}")
        )
        process = await backend_object.run(
            SandboxRequest(
                argv=process_argv,
                cwd=workspace,
                action_kind="capability.probe",
                policy=process_policy,
                run_mode="safe",
            )
        )
        capabilities: set[str] = set()
        if _exact_process_result(process, _PROCESS_CANARY_MARKER):
            capabilities.add("process")

        await backend_object.run_operation(
            replace(
                SandboxOperation.filesystem(
                    kind="write_text",
                    workspace=workspace,
                    run_mode="safe",
                    path=worker_target,
                    paths=(worker_target,),
                    content="worker-ok",
                    file_system_profile=profile,
                ),
                operation_id="capability-probe",
            )
        )
        read_result = await backend_object.run_operation(
            replace(
                SandboxOperation.filesystem(
                    kind="read_file",
                    workspace=workspace,
                    run_mode="safe",
                    path=worker_target,
                    paths=(worker_target,),
                    display_path=str(worker_target),
                    file_system_profile=profile,
                ),
                operation_id="capability-probe",
            )
        )
        if _filesystem_probe_read_succeeded(read_result.message):
            capabilities.add("filesystem-worker")

        deny_write = await backend_object.run(
            SandboxRequest(
                argv=_native_denial_canary_argv(
                    protected_target,
                    operation="write",
                    marker=_DENY_WRITE_CANARY_MARKER,
                    windows_launcher=windows_canary_launcher,
                ),
                cwd=workspace,
                action_kind="capability.probe",
                policy=process_policy,
                run_mode="safe",
            )
        )
        if (
            _exact_process_result(deny_write, _DENY_WRITE_CANARY_MARKER)
            and protected_target.read_text(encoding="utf-8") == "unchanged"
        ):
            capabilities.add("denyWriteCarveout")

        deny_read = await backend_object.run(
            SandboxRequest(
                argv=_native_denial_canary_argv(
                    authority_secret,
                    operation="read",
                    marker=_DENY_READ_CANARY_MARKER,
                    windows_launcher=windows_canary_launcher,
                ),
                cwd=workspace,
                action_kind="capability.probe",
                policy=process_policy,
                run_mode="safe",
            )
        )
        if _exact_process_result(deny_read, _DENY_READ_CANARY_MARKER):
            capabilities.add("authorityDenyRead")

        required_capabilities = required_safe_capabilities(setup.platform)
        if str(setup.platform).lower().startswith("win"):
            from opensquilla.sandbox.backend.windows_default_support import (
                probe_windows_default_support,
            )

            support = probe_windows_default_support()
            if support.identity_ready:
                capabilities.add("windowsIdentity")
            if support.storage_ready:
                capabilities.add("windowsStorage")
            if support.proxy_allowlist_enforced:
                capabilities.add("windowsProxyWfp")

    frozen = frozenset(capabilities)
    if not required_capabilities.issubset(frozen):
        missing = sorted(required_capabilities - frozen)
        return _unavailable_capability_report(
            setup,
            backend=backend,
            code="capability_missing",
            reason=f"Sandbox capability verification is missing: {', '.join(missing)}.",
            capabilities=frozen,
        )
    return CapabilityReport.available_for(
        backend=backend,
        platform=setup.platform,
        reason="Live sandbox capability verification passed.",
        capabilities=frozen,
    )


async def ensure_sandbox_setup_auto(config: Any) -> SetupResult:
    global _LAST_RESULT, _SETTING_UP

    async with _LOCK:
        _SETTING_UP = True
        setup_result: SetupResult | None = None
        try:
            setup_result = await ensure_sandbox_setup(config)
            if (
                setup_result.state is SandboxSetupState.READY
                and setup_result.platform == "win32"
            ):
                from opensquilla.sandbox.integration import (
                    refresh_runtime_backend_after_setup,
                )

                refresh_runtime_backend_after_setup()
            _LAST_RESULT = setup_result
            return setup_result
        except Exception as exc:  # noqa: BLE001
            result = SetupResult(
                state=SandboxSetupState.FAILED,
                platform=setup_result.platform if setup_result is not None else "auto",
                message="Sandbox setup failed.",
                requires_admin=(
                    setup_result.requires_admin if setup_result is not None else False
                ),
                detail=str(exc),
            )
            _LAST_RESULT = result
            return result
        finally:
            _SETTING_UP = False


def reset_sandbox_setup_runtime_state() -> None:
    global _CAPABILITY_LOCK, _LAST_RESULT, _LOCK, _SETTING_UP

    _LOCK = asyncio.Lock()
    _CAPABILITY_LOCK = asyncio.Lock()
    _SETTING_UP = False
    _LAST_RESULT = None
    _CAPABILITY_CACHE.clear()


__all__ = [
    "current_sandbox_capability_report",
    "current_sandbox_setup_runtime_status",
    "ensure_sandbox_setup_auto",
    "reset_sandbox_setup_runtime_state",
]
