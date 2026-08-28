"""Per-agent memory tier facade.

Owns the lifecycle of LongTermMemoryStore + MemorySyncManager +
MemoryRetriever + TurnCaptureService for one agent. The facade methods stay
narrow and observational so callers can migrate off direct attribute access
without changing retrieval, capture, or prompt behavior.

`build_memory_managers()` owns the construction logic that used to live
inline in ``gateway/boot.py`` between the per-agent stores comment and
the ``create_memory_tools`` call. It preserves that lifecycle while
centralizing later memory-source wiring in one place.

Imports of the heavy construction classes (``LongTermMemoryStore`` etc.)
are intentionally **function-local** inside ``build_memory_managers``.
This mirrors the original boot.py pattern so that tests doing
``monkeypatch.setattr("opensquilla.memory.store.LongTermMemoryStore", FakeStore)``
take effect on every fresh build_services() call without needing any
extra patches at this module's path.
"""

from __future__ import annotations

import ntpath
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

    from .retrieval import MemoryRetriever
    from .store import LongTermMemoryStore
    from .sync_manager import MemorySyncManager
    from .turn_capture import TurnCaptureService

log = structlog.get_logger(__name__)


def _native_io_path(path: str | Path) -> Path:
    """Return an internal OS spelling while public memory paths stay logical."""

    candidate = Path(path).expanduser()
    if os.name != "nt":
        return candidate
    value = ntpath.abspath(str(candidate))
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{value[2:]}")
    return Path(f"\\\\?\\{value}")


@dataclass(frozen=True)
class MemoryDegradation:
    component: str
    operation: str
    error: str

    def as_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "operation": self.operation,
            "error": self.error,
        }


# Filenames written by ``_raw_dump_fallback`` historically lived in the canonical
# memory root and matched ``YYYY-MM-DD-(reset|compact)-<unix_ts>.md``. They are
# moved into ``.raw_fallbacks/`` (dot-prefix sidecar excluded by sync_manager)
# so they don't keep contaminating retrieval after the fix.
_LEGACY_RAW_FALLBACK_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-(reset|compact)-\d+\.md$")
_RAW_FALLBACK_HEADER_PREFIX = "# Raw flush ("
_RAW_FALLBACK_SIDECAR = ".raw_fallbacks"
_RAW_FALLBACK_MIGRATION_MARKER = ".migrated"
_LEGACY_TURN_CAPTURE_REQUIRED_MARKERS = (
    "# Turn Capture",
    "- source_kind: turn_capture",
    "- schema: turn-capture-v1",
)


def _migrate_legacy_raw_fallbacks(memory_dir: str | Path) -> int:
    """Move legacy raw-dump fallback files to the dot-prefix sidecar.

    Best-effort: any failure logs a warning and returns the running count.
    Idempotent via a ``.migrated`` marker file inside the sidecar; subsequent
    boots skip the scan entirely. Files are identified by the conjunction of
    a name pattern AND the ``# Raw flush (`` content header so unrelated
    notes are never moved.

    Returns the number of files actually moved this call.
    """
    root = Path(memory_dir)
    if not root.is_dir():
        return 0
    sidecar = root / _RAW_FALLBACK_SIDECAR
    marker = sidecar / _RAW_FALLBACK_MIGRATION_MARKER
    if marker.is_file():
        return 0

    moved = 0
    try:
        sidecar.mkdir(parents=True, exist_ok=True)
        for entry in root.iterdir():
            if not entry.is_file():
                continue
            if not _LEGACY_RAW_FALLBACK_NAME_RE.match(entry.name):
                continue
            try:
                with entry.open("r", encoding="utf-8", errors="replace") as fh:
                    first = fh.readline()
            except OSError as exc:
                log.warning(
                    "memory_manager.raw_fallback_migration_read_failed",
                    path=str(entry),
                    error=str(exc),
                )
                continue
            if not first.startswith(_RAW_FALLBACK_HEADER_PREFIX):
                continue
            try:
                shutil.move(str(entry), str(sidecar / entry.name))
                moved += 1
            except OSError as exc:
                log.warning(
                    "memory_manager.raw_fallback_migration_move_failed",
                    path=str(entry),
                    error=str(exc),
                )
        try:
            marker.write_text("", encoding="utf-8")
        except OSError as exc:
            log.warning(
                "memory_manager.raw_fallback_migration_marker_failed",
                path=str(marker),
                error=str(exc),
            )
    except Exception as exc:  # noqa: BLE001 — startup must remain resilient
        log.warning(
            "memory_manager.raw_fallback_migration_failed",
            memory_dir=str(root),
            moved=moved,
            error=str(exc),
        )
    if moved:
        log.info(
            "memory_manager.raw_fallback_migrated",
            memory_dir=str(root),
            moved=moved,
        )
    return moved


def _is_legacy_turn_capture_file(path: Path) -> bool:
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:1000]
    except OSError as exc:
        log.warning(
            "memory_manager.turn_archive_migration_read_failed",
            path=str(path),
            error=str(exc),
        )
        return False
    return all(marker in sample for marker in _LEGACY_TURN_CAPTURE_REQUIRED_MARKERS)


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-legacy{index:03d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"unable to find unique migration target for {path}")


def _migrate_legacy_turn_archives(
    memory_dir: str | Path,
    turns_dir: str | Path,
) -> tuple[str, ...]:
    """Move old system-generated ``memory/archive/**`` turn captures to state.

    User-authored files under ``memory/archive`` remain curated memory. Only
    files carrying the old turn-capture header markers are moved.
    """
    root = Path(memory_dir)
    archive_root = root / "archive"
    if not archive_root.is_dir():
        return ()

    target_root = Path(turns_dir)
    moved_rel_paths: list[str] = []
    for path in sorted(archive_root.rglob("*.md")):
        if not path.is_file() or not _is_legacy_turn_capture_file(path):
            continue
        try:
            rel_to_archive = path.relative_to(archive_root)
            target = _unique_destination(target_root / rel_to_archive)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved_rel_paths.append(path.relative_to(root.parent).as_posix())
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "memory_manager.turn_archive_migration_move_failed",
                path=str(path),
                turns_dir=str(target_root),
                error=str(exc),
            )

    for directory in sorted(
        (p for p in archive_root.rglob("*") if p.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        archive_root.rmdir()
    except OSError:
        pass

    if moved_rel_paths:
        log.info(
            "memory_manager.turn_archives_migrated",
            memory_dir=str(root),
            turns_dir=str(target_root),
            moved=len(moved_rel_paths),
        )
    return tuple(moved_rel_paths)


def _nested_value(obj: Any, name: str, default: Any = "") -> Any:
    return getattr(obj, name, default) if obj is not None else default


def memory_config_diagnostics(
    memory_config: Any | None,
    *,
    memory_source: str | None = None,
) -> dict[str, Any]:
    cost_cfg = getattr(memory_config, "cost", None)
    dream_cfg = getattr(memory_config, "dream", None)
    return {
        "memory_source": memory_source
        if memory_source is not None
        else _nested_value(memory_config, "source"),
        "retrieval_mode": _nested_value(memory_config, "retrieval_mode"),
        "query_embedding_cache": _nested_value(cost_cfg, "query_embedding_cache", "off"),
        "dream_enabled": bool(_nested_value(dream_cfg, "enabled", False)),
        "dream_input_slimming": _nested_value(dream_cfg, "input_slimming", "off"),
    }


def _metadata_value(value: Any) -> str:
    return "" if value is None else str(value)


def effective_retrieval_metadata(
    memory_config: Any | None,
    embedding_decision: Any | None,
    *,
    vector_weight: float,
    text_weight: float,
) -> dict[str, str]:
    configured_mode = _metadata_value(_nested_value(memory_config, "retrieval_mode", "hybrid"))
    effective_provider = _metadata_value(getattr(embedding_decision, "effective_provider", "none"))
    effective_mode = "fts_only" if effective_provider == "none" else configured_mode
    return {
        "configured_retrieval_mode": configured_mode,
        "retrieval_mode": effective_mode,
        "embedding_requested_provider": _metadata_value(
            getattr(embedding_decision, "requested_provider", "")
        ),
        "embedding_effective_provider": effective_provider,
        "embedding_model": _metadata_value(getattr(embedding_decision, "model", "")),
        "vector_weight": str(vector_weight),
        "text_weight": str(text_weight),
    }


@dataclass
class MemoryManager:
    """Per-agent memory tier facade."""

    agent_id: str
    db_path: Path
    store: LongTermMemoryStore
    sync_manager: MemorySyncManager
    retriever: MemoryRetriever
    turn_capture: TurnCaptureService
    memory_config: Any | None = None
    workspace_dir: Path | None = None
    memory_dir: Path | None = None
    embedding_decision: Any | None = None
    vector_weight: float = 0.7
    text_weight: float = 0.3
    degraded: list[MemoryDegradation] = field(default_factory=list, init=False)

    def _record_degradation(
        self,
        *,
        component: str,
        operation: str,
        error: Exception,
    ) -> None:
        degradation = MemoryDegradation(
            component=component,
            operation=operation,
            error=str(error),
        )
        self.degraded.append(degradation)
        log.warning(
            "memory_manager.degraded",
            agent_id=self.agent_id,
            component=component,
            operation=operation,
            error=str(error),
        )

    async def search(
        self,
        query: str,
        opts: Any | None = None,
        *,
        intent: Any | None = None,
    ) -> list[Any]:
        if intent is None:
            from .types import SearchIntent

            intent = SearchIntent.TOOL
        return await self.retriever.search(query, opts, intent=intent)

    async def sync(self, *, reason: str = "manual", force: bool = False) -> None:
        await self.sync_manager.sync(reason=reason, force=force)

    async def capture_turn(self, **kwargs: Any) -> str | None:
        return await self.turn_capture.capture_turn(**kwargs)

    async def status(self) -> dict[str, Any]:
        async def metric(component: str, operation: str, fn: Any, default: Any) -> Any:
            try:
                return await fn()
            except Exception as exc:  # noqa: BLE001
                self._record_degradation(
                    component=component,
                    operation=operation,
                    error=exc,
                )
                return default

        workspace_dir = self.workspace_dir
        if workspace_dir is None:
            workspace_dir = getattr(self.turn_capture, "_workspace_dir", None)

        status: dict[str, Any] = {
            "agent_id": self.agent_id,
            "db_path": str(self.db_path),
            "workspace_dir": str(workspace_dir) if workspace_dir is not None else "",
            "memory_dir": str(self.memory_dir) if self.memory_dir is not None else "",
            "file_count": await metric("store", "file_count", self.store.file_count, 0),
            "chunk_count": await metric("store", "chunk_count", self.store.chunk_count, 0),
            "total_size_bytes": await metric(
                "store",
                "total_size",
                self.store.total_size,
                0,
            ),
            "source_counts": await metric(
                "store",
                "source_counts",
                self.store.source_counts,
                {},
            ),
            "vec_available": bool(getattr(self.store, "vec_available", False)),
            "fts_available": bool(getattr(self.store, "fts_available", False)),
        }
        status.update(memory_config_diagnostics(self.memory_config))
        status.update(self.effective_retrieval_metadata())
        status["degraded"] = [d.as_dict() for d in self.degraded]
        return status

    def effective_retrieval_metadata(self) -> dict[str, str]:
        return effective_retrieval_metadata(
            self.memory_config,
            self.embedding_decision,
            vector_weight=self.vector_weight,
            text_weight=self.text_weight,
        )

    async def _best_effort_call(self, component: str, operation: str, obj: Any) -> None:
        call = getattr(obj, operation, None)
        if call is None:
            return
        try:
            await call()
        except Exception as exc:  # noqa: BLE001
            self._record_degradation(
                component=component,
                operation=operation,
                error=exc,
            )

    async def close(self) -> None:
        """Tear down in safe order: sync_manager first (background tasks),
        then retriever, then store (aiosqlite main connection).
        Idempotent — calling twice is safe.
        """
        await self._best_effort_call("sync_manager", "stop", self.sync_manager)
        await self._best_effort_call("retriever", "close", self.retriever)
        await self._best_effort_call("store", "close", self.store)


async def build_memory_managers(
    config: GatewayConfig,
    agent_ids: list[str],
    *,
    session_storage: Any | None = None,
) -> dict[str, MemoryManager]:
    """Construct per-agent ``MemoryManager`` instances from gateway config.

    Preserves the legacy inline boot lifecycle from ``gateway/boot.py``:

    1. Resolve embedding provider (FTS-only fallback when no API key).
    2. Run one-time legacy data/memory.db migration.
    3. For each ``agent_id``:
       - Resolve db_path (env override for ``main``, else per-agent path)
       - Build + initialize ``LongTermMemoryStore``
       - Resolve memory_dir + agent_workspace
       - Build optional derived session source indexer when enabled
       - Build + start ``MemorySyncManager``
       - Build ``MemoryRetriever`` (wired to sync_manager for search-time sync)
       - Build ``TurnCaptureService``

    Caller is expected to wrap in ``try/except`` and ensure the gateway
    can degrade gracefully when memory init fails.
    """
    # Function-local imports — mirror the original boot.py pattern so that
    # tests can monkey-patch the source-module symbols (e.g.
    # ``opensquilla.memory.store.LongTermMemoryStore``,
    # ``opensquilla.agents.scope.maybe_migrate_legacy_memory``) and have those
    # patches honoured on every fresh call.
    from opensquilla.agents.scope import (
        maybe_migrate_legacy_memory,
        resolve_agent_data_dir,
        resolve_agent_memory_db,
        resolve_agent_memory_dir,
        resolve_agent_workspace_dir,
    )

    from .embedding import (
        EmbeddingProvider,
    )
    from .embedding_resolver import create_embedding_provider, resolve_memory_embedding
    from .retrieval import MemoryRetriever
    from .session_source import SessionSourceIndexer
    from .store import LongTermMemoryStore
    from .sync_manager import MemorySyncManager
    from .turn_capture import TurnCaptureService

    cfg = config.memory

    # ── Embedding provider setup ─────────────────────────────────────
    embedding_decision = resolve_memory_embedding(cfg)
    embed_provider: EmbeddingProvider = create_embedding_provider(embedding_decision)
    _force_fts_only = embedding_decision.effective_provider == "none"
    log.info(
        "build_services.embedding_provider",
        requested=embedding_decision.requested_provider,
        provider=embedding_decision.effective_provider,
        model=embedding_decision.model,
        reason=embedding_decision.reason,
    )

    # One-time legacy data migration (no-op if already migrated)
    maybe_migrate_legacy_memory("data")

    # ── Atomic per-agent build, with cleanup on failure ───────────────
    # Unlike the original boot.py which populated module-scope dicts that
    # the gateway could later tear down on partial failure, this factory
    # owns its intermediate state. Any exception teardowns previously
    # committed managers AND the in-flight store/sync_manager that hadn't
    # yet been wrapped, so a half-finished build never leaks aiosqlite
    # connections or background poll/timer tasks.
    managers: dict[str, MemoryManager] = {}
    in_flight_store: LongTermMemoryStore | None = None
    in_flight_sync: MemorySyncManager | None = None
    try:
        for agent_id in agent_ids:
            # Resolve paths
            if agent_id == "main" and os.environ.get("OPENSQUILLA_MEMORY_DB"):
                db_path = Path(os.environ["OPENSQUILLA_MEMORY_DB"])
            else:
                db_path = resolve_agent_memory_db(agent_id, config.state_dir)
            native_db_path = _native_io_path(db_path)
            native_db_path.parent.mkdir(parents=True, exist_ok=True)

            # Build + initialize store
            in_flight_store = LongTermMemoryStore(
                db_path=str(native_db_path),
                embedding_provider=embed_provider,
                query_embedding_cache_mode=getattr(
                    getattr(cfg, "cost", None), "query_embedding_cache", "on"
                ),
            )
            await in_flight_store.initialize()

            # Resolve workspace + memory dirs
            memory_source = getattr(config.memory, "source", "state")
            if memory_source == "workspace":
                agent_workspace = resolve_agent_workspace_dir(agent_id, config)
                mem_dir = str(agent_workspace / "memory")
            elif agent_id == "main" and os.environ.get("OPENSQUILLA_MEMORY_DIR"):
                mem_dir = os.environ["OPENSQUILLA_MEMORY_DIR"]
                agent_workspace = resolve_agent_data_dir(agent_id)
            else:
                mem_dir = str(resolve_agent_memory_dir(agent_id, config.state_dir))
                agent_workspace = resolve_agent_data_dir(agent_id, config.state_dir)
            logical_memory_dir = Path(mem_dir)
            native_memory_dir = _native_io_path(logical_memory_dir)
            native_agent_workspace = _native_io_path(agent_workspace)
            native_turns_dir = _native_io_path(db_path.parent / "turns")
            native_memory_dir.mkdir(parents=True, exist_ok=True)
            native_agent_workspace.mkdir(parents=True, exist_ok=True)

            # One-shot migration: legacy raw-dump fallback files used to be
            # written under canonical ``memory/`` and got picked up by the
            # sync scanner, polluting retrieval. Move them into the
            # ``.raw_fallbacks/`` sidecar BEFORE sync_manager starts so the
            # next scan drops their indexed rows naturally.
            _migrate_legacy_raw_fallbacks(native_memory_dir)
            migrated_turn_archives = _migrate_legacy_turn_archives(
                native_memory_dir,
                native_turns_dir,
            )
            for rel_path in migrated_turn_archives:
                try:
                    await in_flight_store.remove_file(rel_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "memory_manager.turn_archive_index_remove_failed",
                        path=rel_path,
                        error=str(exc),
                    )

            session_indexer = (
                SessionSourceIndexer(
                    storage=session_storage,
                    store=in_flight_store,
                    agent_id=agent_id,
                )
                if session_storage is not None and getattr(cfg, "session_source_enabled", False)
                else None
            )

            # Build + start sync_manager
            in_flight_sync = MemorySyncManager(
                store=in_flight_store,
                workspace_dir=native_agent_workspace,
                memory_dir=native_memory_dir,
                interval_minutes=getattr(cfg, "sync_interval_minutes", 0.0),
                ttl_days=int(getattr(cfg, "entry_ttl_days", 0) or 0),
                ttl_sweep_interval_minutes=getattr(cfg, "ttl_sweep_interval_minutes", 0.0),
                session_indexer=session_indexer,
            )
            await in_flight_sync.start()

            effective_vector_weight = 0.0 if _force_fts_only else getattr(cfg, "vector_weight", 0.7)
            effective_text_weight = 1.0 if _force_fts_only else getattr(cfg, "text_weight", 0.3)
            retrieval_metadata = effective_retrieval_metadata(
                cfg,
                embedding_decision,
                vector_weight=effective_vector_weight,
                text_weight=effective_text_weight,
            )
            retriever = MemoryRetriever(
                in_flight_store,
                temporal_decay_enabled=getattr(cfg, "temporal_decay_enabled", False),
                temporal_decay_half_life_days=getattr(cfg, "temporal_decay_half_life_days", 30.0),
                mmr_enabled=getattr(cfg, "mmr_enabled", False),
                mmr_lambda=getattr(cfg, "mmr_lambda", 0.7),
                vector_weight=effective_vector_weight,
                text_weight=effective_text_weight,
                sync_manager=in_flight_sync,
                effective_metadata=retrieval_metadata,
            )

            turn_capture = TurnCaptureService(
                workspace_dir=native_agent_workspace,
                turns_dir=native_turns_dir,
                # Keep the stable root identity so live config patches that
                # replace ``config.memory`` reach the next captured turn.
                memory_config=config,
            )

            managers[agent_id] = MemoryManager(
                agent_id=agent_id,
                db_path=db_path,
                store=in_flight_store,
                sync_manager=in_flight_sync,
                retriever=retriever,
                turn_capture=turn_capture,
                memory_config=cfg,
                workspace_dir=agent_workspace,
                memory_dir=logical_memory_dir,
                embedding_decision=embedding_decision,
                vector_weight=effective_vector_weight,
                text_weight=effective_text_weight,
            )
            # Resources have been transferred to a MemoryManager that is now
            # tracked by `managers`; clear in-flight handles so a later
            # exception doesn't double-close them in the cleanup path.
            in_flight_store = None
            in_flight_sync = None

            log.info(
                "build_services.memory_agent_ready",
                agent_id=agent_id,
                db=str(db_path),
            )

        return managers
    except Exception:
        # Tear down in reverse order of acquisition.
        if in_flight_sync is not None:
            try:
                await in_flight_sync.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "memory_manager.build_cleanup_failed",
                    component="sync_manager",
                    operation="stop",
                    error=str(exc),
                )
        if in_flight_store is not None:
            try:
                await in_flight_store.close()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "memory_manager.build_cleanup_failed",
                    component="store",
                    operation="close",
                    error=str(exc),
                )
        for committed in managers.values():
            try:
                await committed.close()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "memory_manager.build_cleanup_failed",
                    component="manager",
                    operation="close",
                    agent_id=committed.agent_id,
                    error=str(exc),
                )
        raise
