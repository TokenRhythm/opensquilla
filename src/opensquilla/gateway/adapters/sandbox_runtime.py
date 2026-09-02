"""Gateway Adapters for the transport-neutral sandbox application Module."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from opensquilla.application.sandbox_runtime import (
    SandboxCapability,
    SandboxCommandPolicy,
    SandboxFilePolicy,
    SandboxNetworkPolicy,
    SandboxPolicyConflictError,
    SandboxPolicyDefaults,
    SandboxPolicyDocument,
    SandboxResumeResult,
    SandboxRunMode,
    SandboxRunModePreference,
    SandboxRuntimeAvailability,
    SandboxRuntimeComponent,
    SandboxRuntimeDiscardError,
    SandboxRuntimeDiscardResult,
    SandboxRuntimeError,
    SandboxRuntimeOperation,
    SandboxRuntimeOperationConflictError,
    SandboxRuntimeOperationKind,
    SandboxRuntimeOperationResult,
    SandboxRuntimeOperationState,
    SandboxRuntimePackDocument,
    SandboxRuntimePackSnapshot,
    SandboxRuntimePolicy,
    SandboxRuntimeSource,
    SandboxRuntimeVersion,
    SandboxSetupState,
    SandboxSetupStatus,
    SandboxSystemToolsPolicy,
    SandboxUnavailableError,
)
from opensquilla.runtime_packs.models import (
    RuntimeComponentStatus,
    RuntimeError,
    RuntimeOperation,
    RuntimePackStatus,
)
from opensquilla.sandbox.capability_service import CapabilityReport
from opensquilla.sandbox.policy_models import (
    CommandPolicySettings,
    FilePolicySettings,
    NetworkPolicySettings,
    RuntimePolicySettings,
    SandboxPolicy,
)
from opensquilla.sandbox.policy_store import PolicyVersionConflict, SandboxPolicyStore
from opensquilla.sandbox.run_context import RUN_MODE_PREFERENCE_KEY, resolve_default_run_mode
from opensquilla.sandbox.run_mode_policy import coerce_run_mode_for_principal
from opensquilla.sandbox.setup_state import SetupResult
from opensquilla.session.storage import SessionStorage

SetupStatusReader = Callable[[Any], Awaitable[SetupResult]]
SetupRunner = Callable[[Any], Awaitable[SetupResult]]
CapabilityReader = Callable[..., Awaitable[CapabilityReport]]
EventPublisher = Callable[[str, dict[str, str]], Awaitable[None]]

_RUN_MODE_PREFERENCE_CHANGED_EVENT = "sandbox.run_mode.preference.changed"


def _setup_status(result: SetupResult) -> SandboxSetupStatus:
    return SandboxSetupStatus(
        state=SandboxSetupState(str(result.state.value)),
        platform=result.platform,
        message=result.message,
        requires_admin=result.requires_admin,
        detail=result.detail,
    )


def _capability(report: CapabilityReport) -> SandboxCapability:
    return SandboxCapability(
        available=report.available,
        backend=report.backend,
        platform=report.platform,
        code=report.code,
        reason=report.reason,
        setup_supported=report.setup_supported,
        restart_required=report.restart_required,
        probe_version=report.probe_version,
        capabilities=frozenset(report.capabilities),
    )


def _policy(policy: SandboxPolicy) -> SandboxPolicyDocument:
    return SandboxPolicyDocument(
        schema_version=policy.schema_version,
        policy_version=policy.policy_version,
        files=SandboxFilePolicy(
            custom_deny_write_paths=tuple(policy.files.custom_deny_write_paths),
            recursive_delete_backup_enabled=policy.files.recursive_delete_backup_enabled,
            backup_quota_bytes=policy.files.backup_quota_bytes,
        ),
        commands=SandboxCommandPolicy(
            require_approval_prefixes=tuple(
                tuple(prefix) for prefix in policy.commands.require_approval_prefixes
            ),
            auto_allow_prefixes=tuple(
                tuple(prefix) for prefix in policy.commands.auto_allow_prefixes
            ),
            system_tools=SandboxSystemToolsPolicy(policy.commands.system_tools),
        ),
        network=SandboxNetworkPolicy(
            block_all_network=policy.network.block_all_network,
            allow_domains=tuple(policy.network.allow_domains),
            deny_domains=tuple(policy.network.deny_domains),
        ),
        runtimes=SandboxRuntimePolicy(
            enabled=policy.runtimes.enabled,
            python=policy.runtimes.python,
            node=policy.runtimes.node,
            git_bash=policy.runtimes.git_bash,
        ),
    )


def _stored_policy(policy: SandboxPolicyDocument) -> SandboxPolicy:
    return SandboxPolicy(
        schema_version=policy.schema_version,
        policy_version=policy.policy_version,
        files=FilePolicySettings(
            custom_deny_write_paths=list(policy.files.custom_deny_write_paths),
            recursive_delete_backup_enabled=policy.files.recursive_delete_backup_enabled,
            backup_quota_bytes=policy.files.backup_quota_bytes,
        ),
        commands=CommandPolicySettings(
            require_approval_prefixes=[
                list(prefix) for prefix in policy.commands.require_approval_prefixes
            ],
            auto_allow_prefixes=[
                list(prefix) for prefix in policy.commands.auto_allow_prefixes
            ],
            system_tools=policy.commands.system_tools.value,
        ),
        network=NetworkPolicySettings(
            block_all_network=policy.network.block_all_network,
            allow_domains=list(policy.network.allow_domains),
            deny_domains=list(policy.network.deny_domains),
        ),
        runtimes=RuntimePolicySettings(
            enabled=policy.runtimes.enabled,
            python=policy.runtimes.python,
            node=policy.runtimes.node,
            git_bash=policy.runtimes.git_bash,
        ),
    )


def sandbox_policy_from_payload(payload: dict[str, Any]) -> SandboxPolicyDocument:
    """Decode a v4 policy payload at the Gateway seam."""

    return _policy(SandboxPolicy.model_validate(payload))


def _runtime_error(error: RuntimeError | None) -> SandboxRuntimeError | None:
    if error is None:
        return None
    return SandboxRuntimeError(
        code=error.code,
        message=error.message,
        retryable=error.retryable,
        source=SandboxRuntimeSource(error.source.value) if error.source is not None else None,
    )


def _runtime_operation(operation: RuntimeOperation) -> SandboxRuntimeOperation:
    return SandboxRuntimeOperation(
        operation_id=operation.operation_id,
        component_id=operation.component_id,
        kind=SandboxRuntimeOperationKind(operation.kind.value),
        state=SandboxRuntimeOperationState(operation.state.value),
        progress_bytes=operation.progress_bytes,
        total_bytes=operation.total_bytes,
        source=(
            SandboxRuntimeSource(operation.source.value)
            if operation.source is not None
            else None
        ),
        started_at_ms=operation.started_at_ms,
        updated_at_ms=operation.updated_at_ms,
        error=_runtime_error(operation.error),
    )


def _runtime_component(component: RuntimeComponentStatus) -> SandboxRuntimeComponent:
    return SandboxRuntimeComponent(
        component_id=component.component_id,
        availability=SandboxRuntimeAvailability(component.availability.value),
        catalog_version=component.catalog_version,
        active_version=component.active_version,
        installed_bytes=component.installed_bytes,
        removable=component.removable,
        resume_available=component.resume_available,
        resume_bytes=component.resume_bytes,
        operation=(
            _runtime_operation(component.operation)
            if component.operation is not None
            else None
        ),
        last_error=_runtime_error(component.last_error),
    )


def _runtime_status(status: RuntimePackStatus) -> SandboxRuntimePackDocument:
    return SandboxRuntimePackDocument(
        schema_version=status.schema_version,
        management_supported=status.management_supported,
        target=status.target,
        catalog_version=status.catalog_version,
        source_order=tuple(SandboxRuntimeSource(source.value) for source in status.source_order),
        components=tuple(_runtime_component(component) for component in status.components),
        next_poll_after_ms=status.next_poll_after_ms,
    )


def sandbox_capability_payload(report: SandboxCapability) -> dict[str, object]:
    return {
        "available": report.available,
        "backend": report.backend,
        "platform": report.platform,
        "code": report.code,
        "reason": report.reason,
        "setupSupported": report.setup_supported,
        "restartRequired": report.restart_required,
        "probeVersion": report.probe_version,
        "capabilities": sorted(report.capabilities),
    }


def sandbox_policy_payload(policy: SandboxPolicyDocument) -> dict[str, object]:
    return {
        "schemaVersion": policy.schema_version,
        "policyVersion": policy.policy_version,
        "files": {
            "customDenyWritePaths": list(policy.files.custom_deny_write_paths),
            "recursiveDeleteBackupEnabled": policy.files.recursive_delete_backup_enabled,
            "backupQuotaBytes": policy.files.backup_quota_bytes,
        },
        "commands": {
            "requireApprovalPrefixes": [
                list(prefix) for prefix in policy.commands.require_approval_prefixes
            ],
            "autoAllowPrefixes": [
                list(prefix) for prefix in policy.commands.auto_allow_prefixes
            ],
            "systemTools": policy.commands.system_tools.value,
        },
        "network": {
            "blockAllNetwork": policy.network.block_all_network,
            "allowDomains": list(policy.network.allow_domains),
            "denyDomains": list(policy.network.deny_domains),
        },
        "runtimes": {
            "enabled": policy.runtimes.enabled,
            "python": policy.runtimes.python,
            "node": policy.runtimes.node,
            "gitBash": policy.runtimes.git_bash,
        },
    }


def _runtime_error_payload(error: SandboxRuntimeError | None) -> dict[str, object] | None:
    if error is None:
        return None
    return {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
        "source": error.source.value if error.source is not None else None,
    }


def _runtime_operation_payload(operation: SandboxRuntimeOperation) -> dict[str, object]:
    return {
        "operationId": operation.operation_id,
        "componentId": operation.component_id,
        "kind": operation.kind.value,
        "state": operation.state.value,
        "downloadedBytes": operation.progress_bytes,
        "totalBytes": operation.total_bytes,
        "progressPercent": operation.progress_percent,
        "source": operation.source.value if operation.source is not None else None,
        "startedAtMs": operation.started_at_ms,
        "updatedAtMs": operation.updated_at_ms,
        "error": _runtime_error_payload(operation.error),
    }


def _runtime_status_payload(status: SandboxRuntimePackDocument) -> dict[str, object]:
    return {
        "schemaVersion": status.schema_version,
        "managementSupported": status.management_supported,
        "target": status.target,
        "catalogVersion": status.catalog_version,
        "sourceOrder": [source.value for source in status.source_order],
        "components": [
            {
                "componentId": component.component_id,
                "availability": component.availability.value,
                "catalogVersion": component.catalog_version,
                "activeVersion": component.active_version,
                "installedBytes": component.installed_bytes,
                "removable": component.removable,
                "resumeAvailable": component.resume_available,
                "resumeBytes": component.resume_bytes,
                "operation": (
                    _runtime_operation_payload(component.operation)
                    if component.operation is not None
                    else None
                ),
                "lastError": _runtime_error_payload(component.last_error),
            }
            for component in status.components
        ],
        "nextPollAfterMs": status.next_poll_after_ms,
    }


def sandbox_run_mode_preference_payload(
    preference: SandboxRunModePreference,
) -> dict[str, str]:
    return {"runMode": preference.mode.value, "source": preference.source}


def sandbox_application_payload(result: object) -> dict[str, Any]:
    """Project typed application results to the established v4 wire shape."""

    if isinstance(result, SandboxSetupStatus):
        payload: dict[str, Any] = {
            "state": result.state.value,
            "platform": result.platform,
            "message": result.message,
            "requiresAdmin": result.requires_admin,
        }
        if result.detail:
            payload["detail"] = result.detail
        return payload
    if isinstance(result, SandboxCapability):
        return sandbox_capability_payload(result)
    if isinstance(result, SandboxPolicyDocument):
        return sandbox_policy_payload(result)
    if isinstance(result, SandboxPolicyDefaults):
        return {
            "builtinDenyWritePaths": list(result.builtin_deny_write_paths),
            "runtimeTarget": result.runtime_target,
            "runtimeVersions": {
                runtime.component_id: {
                    "version": runtime.version,
                    "available": runtime.available,
                }
                for runtime in result.runtime_versions
            },
        }
    if isinstance(result, SandboxRunModePreference):
        return sandbox_run_mode_preference_payload(result)
    if isinstance(result, SandboxRuntimePackSnapshot):
        return _runtime_status_payload(result.status)
    if isinstance(result, SandboxRuntimeOperationResult):
        return {"operation": _runtime_operation_payload(result.operation)}
    if isinstance(result, SandboxRuntimeDiscardResult):
        return {"status": _runtime_status_payload(result.status)}
    if isinstance(result, SandboxResumeResult):
        return {
            "sessionKey": result.session_key,
            "resumed": result.resumed,
            "autonomousPaused": result.autonomous_paused,
        }
    raise TypeError(f"Unsupported sandbox application result: {type(result).__name__}")


class GatewaySandboxSetupAdapter:
    """Bind platform setup functions without retaining an RPC context."""

    def __init__(
        self,
        config: Any,
        *,
        status_reader: SetupStatusReader,
        setup_runner: SetupRunner,
        capability_reader: CapabilityReader,
    ) -> None:
        self._config = config
        self._status_reader = status_reader
        self._setup_runner = setup_runner
        self._capability_reader = capability_reader

    async def read_setup_status(self) -> SandboxSetupStatus:
        return _setup_status(await self._status_reader(self._config))

    async def ensure_setup(self) -> SandboxSetupStatus:
        return _setup_status(await self._setup_runner(self._config))

    async def read_capability(self, *, refresh: bool) -> SandboxCapability:
        if refresh:
            report = await self._capability_reader(self._config, force_refresh=True)
        else:
            report = await self._capability_reader(self._config)
        return _capability(report)


class GatewaySandboxPolicyAdapter:
    """Persist versioned policy and derive the cheap legacy defaults view."""

    def __init__(self, state_dir: str | Path | None) -> None:
        self._state_dir = state_dir

    def _store(self) -> SandboxPolicyStore:
        if not self._state_dir or not str(self._state_dir).strip():
            raise SandboxUnavailableError("Sandbox policy storage is unavailable.")
        return SandboxPolicyStore(Path(str(self._state_dir)) / "sessions.db")

    async def read_policy(self) -> SandboxPolicyDocument:
        return _policy(self._store().read())

    async def read_policy_defaults(self) -> SandboxPolicyDefaults:
        return _legacy_policy_defaults()

    async def replace_policy(
        self,
        base_policy_version: int,
        policy: SandboxPolicyDocument,
    ) -> SandboxPolicyDocument:
        try:
            saved = self._store().compare_and_swap(base_policy_version, _stored_policy(policy))
        except PolicyVersionConflict as exc:
            raise SandboxPolicyConflictError(
                expected_version=base_policy_version,
                current_policy=_policy(exc.current_policy),
            ) from exc
        return _policy(saved)


def _legacy_policy_defaults() -> SandboxPolicyDefaults:
    from opensquilla.runtime_packs import load_default_catalog
    from opensquilla.sandbox.file_policy import builtin_deny_write_paths
    from opensquilla.sandbox.runtime_launcher import bundled_runtime_resolver
    from opensquilla.sandbox.runtime_manifest import (
        BundledRuntimeResolver,
        RuntimeManifest,
        RuntimeManifestError,
        runtime_target,
    )

    runtime_versions: list[SandboxRuntimeVersion] = []
    detected_runtime_target: str | None = None

    # This projection intentionally avoids installed-state and integrity reads.
    try:
        detected_runtime_target = runtime_target()
        catalog = load_default_catalog(require_complete=True)
        for key in ("python", "node", "gitBash"):
            descriptor = catalog.descriptor(detected_runtime_target, key)
            if descriptor is None:
                continue
            runtime_versions.append(
                SandboxRuntimeVersion(
                    component_id=key,
                    version=descriptor.version,
                    available=False,
                )
            )
    except (OSError, RuntimeManifestError, ValueError):
        runtime_versions = []

    if not runtime_versions:
        try:
            resolver = bundled_runtime_resolver()
            if resolver is None:
                candidate = (
                    Path(__file__).resolve().parents[4]
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
                    runtime_versions.append(
                        SandboxRuntimeVersion(
                            component_id=key,
                            version=asset.version,
                            available=bool(executable_names)
                            and all(
                                executable_paths.get(name, Path()).is_file()
                                for name in executable_names
                            ),
                        )
                    )
        except (OSError, RuntimeManifestError, ValueError):
            detected_runtime_target = None
            runtime_versions = []

    return SandboxPolicyDefaults(
        builtin_deny_write_paths=tuple(
            str(path) for path in builtin_deny_write_paths()
        ),
        runtime_target=detected_runtime_target,
        runtime_versions=tuple(runtime_versions),
    )


class GatewaySandboxRunModeAdapter:
    """Resolve and persist run-mode preference from narrow request values."""

    def __init__(self, *, session_manager: Any, config: Any, principal: Any) -> None:
        self._session_manager = session_manager
        self._config = config
        self._principal = principal

    async def read_run_mode(self) -> SandboxRunModePreference:
        mode, source = await resolve_default_run_mode(self._session_manager, self._config)
        coerced = coerce_run_mode_for_principal(mode, self._principal)
        return SandboxRunModePreference(SandboxRunMode.parse(coerced.value), str(source))

    async def persist_run_mode(self, mode: SandboxRunMode) -> SandboxRunMode:
        storage = _session_storage(self._session_manager)
        confirmed = await storage.set_runtime_preference(
            RUN_MODE_PREFERENCE_KEY,
            mode.value,
        )
        return SandboxRunMode.parse(confirmed)


def _session_storage(session_manager: Any) -> SessionStorage:
    from opensquilla.gateway.session_services import get_session_storage

    storage = get_session_storage(session_manager)
    if storage is None:
        raise SandboxUnavailableError("Session storage is not configured")
    return cast(SessionStorage, storage)


class GatewaySandboxRunModeEventsAdapter:
    def __init__(self, publish: EventPublisher) -> None:
        self._publish = publish

    async def publish_run_mode_changed(self, preference: SandboxRunModePreference) -> None:
        await self._publish(
            _RUN_MODE_PREFERENCE_CHANGED_EVENT,
            sandbox_run_mode_preference_payload(preference),
        )


class GatewaySandboxRuntimePackAdapter:
    """Run blocking Runtime Pack operations outside the Gateway event loop."""

    def __init__(self, state_dir: str | Path | None) -> None:
        self._state_dir = state_dir

    async def read_runtime_status(self) -> SandboxRuntimePackDocument:
        from opensquilla.runtime_packs import status_snapshot

        status = await asyncio.to_thread(status_snapshot, self._state_dir)
        return _runtime_status(status)

    async def install_runtime(self, component_id: str) -> SandboxRuntimeOperation:
        from opensquilla.runtime_packs import start_install

        operation = await asyncio.to_thread(start_install, component_id, self._state_dir)
        return _runtime_operation(operation)

    async def cancel_runtime(
        self,
        component_id: str,
        operation_id: str,
    ) -> SandboxRuntimeOperation:
        from opensquilla.runtime_packs import RuntimePackError, cancel_install

        try:
            operation = await asyncio.to_thread(
                cancel_install,
                component_id,
                operation_id,
                self._state_dir,
            )
        except RuntimePackError as exc:
            raise SandboxRuntimeOperationConflictError(component_id) from exc
        return _runtime_operation(operation)

    async def remove_runtime(self, component_id: str) -> SandboxRuntimeOperation:
        from opensquilla.runtime_packs import remove_component

        operation = await asyncio.to_thread(remove_component, component_id, self._state_dir)
        return _runtime_operation(operation)

    async def discard_runtime_download(
        self,
        component_id: str,
    ) -> SandboxRuntimePackDocument:
        from opensquilla.runtime_packs import (
            RuntimePackDiscardError,
            RuntimePackError,
            RuntimePackUnavailableError,
            discard_download,
        )

        try:
            status = await asyncio.to_thread(discard_download, component_id, self._state_dir)
        except (RuntimePackDiscardError, RuntimePackUnavailableError) as exc:
            raise SandboxRuntimeDiscardError(component_id) from exc
        except RuntimePackError as exc:
            raise SandboxRuntimeOperationConflictError(component_id) from exc
        return _runtime_status(status)


class GatewaySandboxResumeAdapter:
    """Clear denial-ledger pauses without retaining request state."""

    async def clear_pause(self, session_key: str) -> bool:
        from opensquilla.sandbox.integration import get_runtime

        runtime = get_runtime()
        if runtime is None:
            raise SandboxUnavailableError(
                "Sandbox runtime is not configured.",
                retryable=True,
            )
        return bool(await runtime.ledger.clear_pause(session_key))


__all__ = [
    "GatewaySandboxPolicyAdapter",
    "GatewaySandboxResumeAdapter",
    "GatewaySandboxRunModeAdapter",
    "GatewaySandboxRunModeEventsAdapter",
    "GatewaySandboxRuntimePackAdapter",
    "GatewaySandboxSetupAdapter",
    "sandbox_application_payload",
    "sandbox_capability_payload",
    "sandbox_policy_from_payload",
    "sandbox_policy_payload",
    "sandbox_run_mode_preference_payload",
]
