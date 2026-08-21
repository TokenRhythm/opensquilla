"""Optional, independently managed developer Runtime Packs."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from opensquilla.run_mode import RunMode
from opensquilla.runtime_packs.catalog import (
    RuntimePackCatalog,
    RuntimePackCatalogError,
    RuntimePackDescriptor,
    component_ids,
    discover_catalog_path,
    load_default_catalog,
)
from opensquilla.runtime_packs.manager import (
    ActiveRuntime,
    RuntimePackError,
    RuntimePackService,
    RuntimePackUnavailableError,
    runtime_pack_state_scope,
    runtime_packs_root,
)
from opensquilla.runtime_packs.models import (
    RuntimeAvailability,
    RuntimeComponentStatus,
    RuntimeError,
    RuntimeOperation,
    RuntimeOperationKind,
    RuntimeOperationState,
    RuntimePackStatus,
    RuntimeSource,
)
from opensquilla.runtime_packs.resolver import RuntimePackResolver, RuntimePolicyInput

_services: dict[str, RuntimePackService] = {}
_services_lock = threading.Lock()
_LOG = logging.getLogger(__name__)


def _service_key(configured_state_dir: str | Path | None) -> str:
    return str(runtime_packs_root(configured_state_dir).absolute())


def get_runtime_pack_service(
    configured_state_dir: str | Path | None = None,
) -> RuntimePackService:
    """Return the process-local service for one configured state directory."""

    key = _service_key(configured_state_dir)
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = RuntimePackService(configured_state_dir=configured_state_dir)
            _services[key] = service
        return service


def _unavailable_status() -> RuntimePackStatus:
    error = RuntimeError(
        code="CATALOG_UNAVAILABLE",
        message="Runtime Pack management is unavailable for this application build.",
        retryable=False,
    )
    components = tuple(
        RuntimeComponentStatus(
            component_id=component_id,
            availability=RuntimeAvailability.UNSUPPORTED,
            catalog_version=None,
            active_version=None,
            installed_bytes=None,
            removable=False,
            resume_available=False,
            resume_bytes=0,
            operation=None,
            last_error=error,
        )
        for component_id in component_ids()
    )
    return RuntimePackStatus(
        schema_version=1,
        management_supported=False,
        target=None,
        catalog_version=None,
        source_order=(RuntimeSource.GITHUB, RuntimeSource.OSS),
        components=components,
        next_poll_after_ms=5_000,
    )


def status_snapshot(
    state_dir: str | Path | None = None,
) -> RuntimePackStatus:
    """Return a fail-open Runtime Pack status snapshot."""

    try:
        return get_runtime_pack_service(state_dir).status()
    except Exception:
        _LOG.debug("Runtime Pack status is unavailable", exc_info=True)
        return _unavailable_status()


def _unavailable_operation(component_id: str, kind: RuntimeOperationKind) -> RuntimeOperation:
    now = int(time.time() * 1000)
    return RuntimeOperation(
        operation_id=uuid.uuid4().hex,
        component_id=component_id,
        kind=kind,
        state=RuntimeOperationState.FAILED,
        progress_bytes=0,
        total_bytes=0,
        source=None,
        started_at_ms=now,
        updated_at_ms=now,
        error=RuntimeError(
            code="CATALOG_UNAVAILABLE",
            message="Runtime Pack management is unavailable.",
            retryable=False,
        ),
    )


def start_install(
    component_id: str,
    state_dir: str | Path | None = None,
) -> RuntimeOperation:
    try:
        return get_runtime_pack_service(state_dir).start_install(component_id)
    except Exception:
        _LOG.debug("Runtime Pack installation could not be started", exc_info=True)
        return _unavailable_operation(component_id, RuntimeOperationKind.INSTALL)


def cancel_install(
    component_id: str,
    operation_id: str,
    state_dir: str | Path | None = None,
) -> RuntimeOperation:
    try:
        return get_runtime_pack_service(state_dir).cancel(component_id, operation_id)
    except RuntimePackError:
        raise
    except Exception:
        _LOG.debug("Runtime Pack cancellation is unavailable", exc_info=True)
        return _unavailable_operation(component_id, RuntimeOperationKind.INSTALL)


def remove_component(
    component_id: str,
    state_dir: str | Path | None = None,
) -> RuntimeOperation:
    try:
        return get_runtime_pack_service(state_dir).remove(component_id)
    except Exception:
        _LOG.debug("Runtime Pack removal could not be started", exc_info=True)
        return _unavailable_operation(component_id, RuntimeOperationKind.REMOVE)


def apply_runtime_environment(
    environment: Mapping[str, str] | None,
    *,
    mode: RunMode | str,
    policy: RuntimePolicyInput = None,
    require_managed: bool = False,
    state_dir: str | Path | None = None,
) -> dict[str, str]:
    """Apply runtime precedence; strict callers receive an empty PATH on failure."""

    try:
        resolver = RuntimePackResolver(get_runtime_pack_service(state_dir))
        return resolver.apply_environment(
            environment,
            mode=mode,
            policy=policy,
            require_managed=require_managed,
        )
    except Exception:
        _LOG.debug("Runtime Pack environment resolution is unavailable", exc_info=True)
        result = dict(environment or {})
        if require_managed:
            path_key = next((key for key in result if key.casefold() == "path"), "PATH")
            result[path_key] = ""
        return result


def runtime_roots(
    policy: RuntimePolicyInput = None,
    state_dir: str | Path | None = None,
) -> tuple[Path, ...]:
    try:
        return RuntimePackResolver(get_runtime_pack_service(state_dir)).runtime_roots(policy)
    except Exception:
        _LOG.debug("Runtime Pack roots are unavailable", exc_info=True)
        return ()


def resolve_component_binary(
    component_id: str,
    name: str,
    state_dir: str | Path | None = None,
    *,
    allow_host: bool = False,
) -> Path | None:
    try:
        return RuntimePackResolver(get_runtime_pack_service(state_dir)).resolve_component_binary(
            component_id, name, allow_host=allow_host
        )
    except Exception:
        _LOG.debug("Runtime Pack executable resolution is unavailable", exc_info=True)
        return None


__all__ = [
    "ActiveRuntime",
    "RuntimeAvailability",
    "RuntimeComponentStatus",
    "RuntimeError",
    "RuntimeOperation",
    "RuntimeOperationKind",
    "RuntimeOperationState",
    "RuntimePackCatalog",
    "RuntimePackCatalogError",
    "RuntimePackDescriptor",
    "RuntimePackError",
    "RuntimePackResolver",
    "RuntimePackService",
    "RuntimePackStatus",
    "RuntimePackUnavailableError",
    "RuntimeSource",
    "apply_runtime_environment",
    "cancel_install",
    "component_ids",
    "discover_catalog_path",
    "get_runtime_pack_service",
    "load_default_catalog",
    "remove_component",
    "resolve_component_binary",
    "runtime_packs_root",
    "runtime_pack_state_scope",
    "runtime_roots",
    "start_install",
    "status_snapshot",
]
