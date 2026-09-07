"""Profile-import startup recovery independent of RPC method registration."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

_DERIVED_REFRESH_TIMEOUT_SECONDS = 30.0


class ProfileImportUnavailableError(RuntimeError):
    """The selected agent has no configured memory source."""


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def bounded_await(value: Any, *, timeout_seconds: float) -> Any:
    if not inspect.isawaitable(value):
        return value
    async with asyncio.timeout(timeout_seconds):
        return await value


def shared_profile_state_dir(config: Any) -> Path:
    from opensquilla.agents.scope import default_state_dir

    configured = getattr(config, "state_dir", None)
    return Path(configured).expanduser() if configured else default_state_dir()


def profile_import_paths(
    *,
    config: Any,
    memory_managers: dict[str, Any],
    agent_id: str,
) -> Any:
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.memory.profile_import import ProfileImportPaths

    manager = memory_managers.get(agent_id)
    if manager is None:
        raise ProfileImportUnavailableError(
            f"Memory is not configured for agent {agent_id!r}."
        )
    memory_root = getattr(manager, "workspace_dir", None) or getattr(
        manager,
        "memory_dir",
        None,
    )
    if memory_root is None:
        raise ProfileImportUnavailableError(
            f"Memory source is not configured for agent {agent_id!r}."
        )
    state_dir = shared_profile_state_dir(config).expanduser().resolve(strict=False)
    return ProfileImportPaths(
        agent_id=agent_id,
        agent_workspace_dir=Path(resolve_agent_workspace_dir(agent_id, config)),
        memory_workspace_dir=Path(memory_root),
        state_dir=state_dir,
        profile_home_dir=state_dir.parent,
    )


async def index_receipt_sources(
    service: Any,
    manager: Any,
    receipt_id: str,
    *,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> bool | None:
    """Index committed MEMORY/IMPORT sources; return None for injected old seams."""

    domain_store = getattr(service, "store", None)
    load_receipt = getattr(domain_store, "load_receipt", None)
    index_store = getattr(manager, "store", None)
    index_file = getattr(index_store, "index_file", None)
    remove_file = getattr(index_store, "remove_file", None)
    if not callable(load_receipt) or not callable(index_file) or not callable(remove_file):
        return None

    from opensquilla.memory.profile_import.files import read_text_image, target_path
    from opensquilla.memory.types import MemorySource

    try:
        async with asyncio.timeout(timeout_seconds):
            receipt = load_receipt(receipt_id)
            for plan in receipt.files:
                target = str(
                    getattr(getattr(plan, "target", None), "value", plan.target)
                )
                if target not in {"MEMORY", "IMPORT"}:
                    continue
                root, path = target_path(service.paths, plan)
                exists, content, _mode = read_text_image(root, path)
                if exists:
                    await maybe_await(
                        index_file(
                            path=plan.relative_path,
                            content=content,
                            source=MemorySource.memory,
                        )
                    )
                else:
                    await maybe_await(remove_file(plan.relative_path))
        return True
    except Exception:  # noqa: BLE001 - index is derived; sources remain authoritative
        return False


async def persist_index_status(
    service: Any,
    receipt_id: str,
    status: str,
    *,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> None:
    update = getattr(service, "set_index_status", None)
    if not callable(update):
        return
    try:
        await bounded_await(
            update(receipt_id, status),
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        # Diagnostic metadata may lag; canonical files and retry markers do not.
        return


async def refresh_recovered_runtime(
    *,
    memory_managers: dict[str, Any],
    turn_runner: Any,
    service: Any,
    agent_id: str,
    batch_ids: list[str],
    force_full_sync: bool = False,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> None:
    """Refresh derived state after journal recovery changed canonical files."""

    if not batch_ids and not force_full_sync:
        return
    derived_ok = True
    for name in ("invalidate_profile_snapshot", "refresh_memory_snapshot"):
        callback = getattr(turn_runner, name, None)
        if not callable(callback):
            continue
        try:
            await bounded_await(
                callback(agent_id),
                timeout_seconds=timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - canonical files remain authoritative
            derived_ok = False

    manager = memory_managers.get(agent_id)
    receipts: list[Any] = []
    domain_store = getattr(service, "store", None)
    load_by_batch = getattr(domain_store, "load_receipt_by_batch", None)
    for batch_id in batch_ids:
        if not callable(load_by_batch):
            break
        try:
            receipt = load_by_batch(batch_id)
        except Exception:  # noqa: BLE001 - recovery already completed safely
            derived_ok = False
            continue
        if receipt is not None:
            receipts.append(receipt)

    if manager is None:
        for receipt in receipts:
            await persist_index_status(service, str(receipt.receipt_id), "pending")
        return

    mark_dirty = getattr(getattr(manager, "sync_manager", None), "mark_dirty", None)
    if callable(mark_dirty):
        try:
            mark_dirty()
        except Exception:  # noqa: BLE001 - derived index remains retryable
            derived_ok = False

    requires_full_sync = force_full_sync or len(receipts) != len(batch_ids)
    receipt_indexed: dict[str, bool] = {}
    for receipt in receipts:
        receipt_id = str(receipt.receipt_id)
        indexed = await index_receipt_sources(
            service,
            manager,
            receipt_id,
            timeout_seconds=timeout_seconds,
        )
        if indexed is None:
            requires_full_sync = True
        else:
            receipt_indexed[receipt_id] = indexed

    full_sync_ok = True
    if requires_full_sync:
        sync = getattr(manager, "sync", None)
        if callable(sync):
            try:
                await bounded_await(
                    sync(reason="profile_import_recovery", force=True),
                    timeout_seconds=timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - source files stay authoritative
                full_sync_ok = False
        else:
            full_sync_ok = False

    for receipt in receipts:
        receipt_id = str(receipt.receipt_id)
        indexed = receipt_indexed.get(receipt_id, full_sync_ok)
        status = "ready" if indexed and derived_ok else "pending"
        await persist_index_status(
            service,
            receipt_id,
            status,
            timeout_seconds=timeout_seconds,
        )


def startup_profile_import_service(
    *,
    config: Any,
    memory_managers: dict[str, Any],
    agent_id: str,
) -> Any:
    from opensquilla.memory.profile_import import ModelIdentity, ProfileImportService

    return ProfileImportService(
        profile_import_paths(
            config=config,
            memory_managers=memory_managers,
            agent_id=agent_id,
        ),
        ModelIdentity(
            provider="startup-maintenance",
            model="startup-maintenance",
        ),
        None,
    )


async def run_profile_import_startup_recovery(
    *,
    config: Any,
    memory_managers: dict[str, Any],
) -> dict[str, list[str]]:
    """Recover canonical profile files serially before Gateway readiness."""

    recovered: dict[str, list[str]] = {}
    for agent_id in sorted(memory_managers):
        service = startup_profile_import_service(
            config=config,
            memory_managers=memory_managers,
            agent_id=agent_id,
        )
        try:
            recovered[agent_id] = await service.recover()
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeError(
                f"profile import startup recovery failed for agent {agent_id!r}: {exc}"
            ) from exc
    return recovered


async def run_profile_import_startup_maintenance(
    *,
    config: Any,
    memory_managers: dict[str, Any],
    turn_runner: Any = None,
    recovered_batches: dict[str, list[str]] | None = None,
    timeout_seconds: float = _DERIVED_REFRESH_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Run non-canonical cleanup and derived refresh after Gateway readiness."""

    from datetime import UTC, datetime

    from opensquilla.memory.profile_import.store import (
        cleanup_expired_profile_import_raw,
        harden_profile_import_private_state,
    )

    failures: dict[str, str] = {}
    state_dir = shared_profile_state_dir(config)
    try:
        await asyncio.to_thread(
            lambda: (
                harden_profile_import_private_state(state_dir),
                cleanup_expired_profile_import_raw(state_dir, datetime.now(UTC)),
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort after readiness
        failures["_raw"] = str(exc)

    async def maintain_agent(agent_id: str) -> tuple[str, str | None]:
        service = startup_profile_import_service(
            config=config,
            memory_managers=memory_managers,
            agent_id=agent_id,
        )
        try:
            await refresh_recovered_runtime(
                memory_managers=memory_managers,
                turn_runner=turn_runner,
                service=service,
                agent_id=agent_id,
                batch_ids=(recovered_batches or {}).get(agent_id, []),
                timeout_seconds=timeout_seconds,
            )
            # Preview metadata is private-state maintenance, not canonical recovery.
            await service.info()
            return agent_id, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one agent must not block boot
            return agent_id, str(exc)

    # ProfileOperationLock is shared by every agent under one profile home.
    results = []
    for agent_id in sorted(memory_managers):
        results.append(await maintain_agent(agent_id))
    failures.update(
        {
            agent_id: error
            for agent_id, error in results
            if error is not None
        }
    )
    return failures


__all__ = [
    "ProfileImportUnavailableError",
    "bounded_await",
    "index_receipt_sources",
    "maybe_await",
    "persist_index_status",
    "profile_import_paths",
    "refresh_recovered_runtime",
    "run_profile_import_startup_maintenance",
    "run_profile_import_startup_recovery",
    "shared_profile_state_dir",
    "startup_profile_import_service",
]
