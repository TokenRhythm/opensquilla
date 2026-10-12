"""Settings-only, read-only OpenSquilla profile migration RPCs.

The gateway may discover and preview import sources, but it never applies an
import. Candidate paths stay server-side behind short-lived, connection-bound
opaque identifiers so a remote administrator cannot use this API as a host
path oracle.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
import secrets
import stat
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher

log = structlog.get_logger(__name__)
_d = get_dispatcher()

_SCHEMA_VERSION = 1
_CANDIDATE_TTL_SECONDS = 10 * 60
_MAX_CACHED_CANDIDATES = 128
_MAX_DISCOVERED_CANDIDATES = 12
_PUBLIC_VERSION_RE = re.compile(
    r"[vV]?\d+\.\d+(?:\.\d+)?"
    r"(?:(?:a|b|rc)\d+)?"
    r"(?:-[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?"
    r"(?:\+[0-9A-Za-z]+(?:[._-][0-9A-Za-z]+)*)?",
    re.ASCII,
)

_BLOCKER_ORDER = (
    "source_in_use",
    "source_schema_newer",
    "source_database_invalid",
    "source_config_invalid",
    "source_unreadable",
    "insufficient_disk",
    "target_recovery_required",
    "target_not_replaceable",
    "preview_unavailable",
)


@dataclass(frozen=True)
class _CandidateRecord:
    conn_id: str
    expires_at: float
    target_key: str
    target_identity: tuple[int, ...]
    source_path: Path
    source_kind: str
    source_identity: tuple[int, ...]
    public: dict[str, Any]


_candidate_cache: OrderedDict[str, _CandidateRecord] = OrderedDict()


def _capabilities(*, available: bool) -> dict[str, bool]:
    return {
        "discover": available,
        "preview": available,
        "apply": False,
        "manualSource": False,
    }


def _list_payload(
    candidates: list[dict[str, Any]],
    *,
    available: bool = True,
) -> dict[str, Any]:
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "mode": "preview_only",
        "capabilities": _capabilities(available=available),
        "candidates": candidates,
    }


def _require_empty_object(params: Any) -> None:
    if not isinstance(params, dict) or params:
        raise RpcHandlerError(
            "migration.invalid_params",
            "migration.sources.list requires an empty object",
        )


def _require_preview_params(params: Any) -> str:
    if not isinstance(params, dict) or set(params) != {"candidateId"}:
        raise RpcHandlerError(
            "migration.invalid_params",
            "migration.sources.preview requires only candidateId",
        )
    candidate_id = params.get("candidateId")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise RpcHandlerError(
            "migration.invalid_params",
            "candidateId must be a non-empty string",
        )
    return candidate_id


def _target_for_context(ctx: RpcContext) -> Path | None:
    if ctx.config is None:
        return None
    # Import lazily: gateway.rpc is imported while gateway.boot is still being
    # initialized. At request time this helper is the canonical interpretation
    # of the running config's actual profile home.
    from opensquilla.gateway.boot import _gateway_home

    return _gateway_home(ctx.config).expanduser().absolute()


def _path_key(path: Path) -> str:
    try:
        normalized = path.resolve(strict=False)
    except OSError:
        normalized = path.expanduser().absolute()
    return os.path.normcase(os.path.normpath(str(normalized)))


def _directory_identity(path: Path) -> tuple[int, ...] | None:
    try:
        result = path.lstat()
    except OSError:
        return None
    file_type = stat.S_IFMT(result.st_mode)
    if file_type != stat.S_IFDIR:
        return None
    base = (int(result.st_dev), int(result.st_ino), int(file_type))
    if os.name == "nt" and int(result.st_ino) == 0:  # pragma: no cover - Windows only
        return (*base, int(result.st_ctime_ns))
    return base


def _public_version(value: Any) -> str | None:
    """Return only a bounded version token safe for the path-free Web RPC."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) > 80 or _PUBLIC_VERSION_RE.fullmatch(candidate) is None:
        return None
    return candidate


def _discover_sync(
    target: Path,
) -> list[tuple[Path, str, tuple[int, ...], dict[str, Any]]]:
    legacy_detect = importlib.import_module("opensquilla.migration.legacy_detect")
    opensquilla_home = importlib.import_module("opensquilla.migration.opensquilla_home")

    discovered: list[tuple[Path, str, tuple[int, ...], dict[str, Any]]] = []
    for candidate in legacy_detect.detect_legacy_homes(
        target,
        limit=_MAX_DISCOVERED_CANDIDATES,
    ):
        if len(discovered) >= _MAX_DISCOVERED_CANDIDATES:
            break
        source_path = candidate.path.expanduser().absolute()
        identity = _directory_identity(source_path)
        if identity is None:
            continue
        inspected = opensquilla_home.inspect_opensquilla_home_candidate(
            source_path,
            kind=candidate.kind,
            target=target,
        )
        if inspected is None:
            continue
        public = {
            "sourceKind": candidate.kind,
            "version": _public_version(inspected.version),
            "estimatedActivityAt": inspected.estimated_activity_at,
            "sessionCount": inspected.session_count,
            "sizeBytes": inspected.size_bytes,
            "previouslyImported": inspected.previously_imported,
        }
        discovered.append((source_path, candidate.kind, identity, public))
    return discovered


def _prune_candidate_cache(now: float) -> None:
    expired = [
        candidate_id
        for candidate_id, record in _candidate_cache.items()
        if record.expires_at <= now
    ]
    for candidate_id in expired:
        _candidate_cache.pop(candidate_id, None)


def _new_candidate_id() -> str:
    while True:
        candidate_id = secrets.token_urlsafe(24)
        if candidate_id not in _candidate_cache:
            return candidate_id


def _store_candidates(
    *,
    ctx: RpcContext,
    target: Path,
    target_identity: tuple[int, ...],
    discovered: list[tuple[Path, str, tuple[int, ...], dict[str, Any]]],
) -> list[dict[str, Any]]:
    now = time.monotonic()
    _prune_candidate_cache(now)
    payloads: list[dict[str, Any]] = []
    for source_path, source_kind, source_identity, public in discovered:
        while len(_candidate_cache) >= _MAX_CACHED_CANDIDATES:
            _candidate_cache.popitem(last=False)
        candidate_id = _new_candidate_id()
        candidate_payload = {"candidateId": candidate_id, **public}
        _candidate_cache[candidate_id] = _CandidateRecord(
            conn_id=ctx.conn_id,
            expires_at=now + _CANDIDATE_TTL_SECONDS,
            target_key=_path_key(target),
            target_identity=target_identity,
            source_path=source_path,
            source_kind=source_kind,
            source_identity=source_identity,
            public=candidate_payload,
        )
        payloads.append(candidate_payload)
    return payloads


def _candidate_unavailable() -> RpcHandlerError:
    return RpcHandlerError(
        "migration.candidate_unavailable",
        "Migration candidate is unavailable; refresh the source list and try again.",
    )


def _resolve_candidate(
    candidate_id: str,
    *,
    ctx: RpcContext,
    target: Path,
) -> _CandidateRecord:
    now = time.monotonic()
    _prune_candidate_cache(now)
    record = _candidate_cache.get(candidate_id)
    if record is None:
        raise _candidate_unavailable()
    if record.conn_id != ctx.conn_id or record.target_key != _path_key(target):
        raise _candidate_unavailable()
    if (
        record.target_identity != _directory_identity(target)
        or record.source_identity != _directory_identity(record.source_path)
    ):
        _candidate_cache.pop(candidate_id, None)
        raise _candidate_unavailable()
    _candidate_cache.move_to_end(candidate_id)
    return record


def _preview_sync(
    record: _CandidateRecord,
    *,
    target: Path,
) -> tuple[dict[str, Any], bool]:
    opensquilla_home = importlib.import_module("opensquilla.migration.opensquilla_home")

    if (
        record.source_identity != _directory_identity(record.source_path)
        or record.target_identity != _directory_identity(target)
    ):
        raise _candidate_unavailable()
    migrator = opensquilla_home.OpenSquillaHomeMigrator(
        opensquilla_home.OpenSquillaMigrationOptions(
            source=record.source_path,
            kind=record.source_kind,
            target=target,
            apply=False,
            replace_target=True,
            allow_running_target_preview=True,
        )
    )
    report = migrator.migrate()
    if (
        record.source_identity != _directory_identity(record.source_path)
        or record.target_identity != _directory_identity(target)
    ):
        raise _candidate_unavailable()
    return report, migrator.target_had_real_data


def _blockers(report: dict[str, Any]) -> list[str]:
    preflight = report.get("preflight")
    preflight = preflight if isinstance(preflight, dict) else {}
    found: set[str] = set()
    if preflight.get("source_gateway_running") is True:
        found.add("source_in_use")
    if preflight.get("schema_ahead") is True:
        found.add("source_schema_newer")

    unknown_error = False
    items = report.get("items")
    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict) or item.get("status") != "error":
            continue
        kind = str(item.get("kind") or "")
        reason = str(item.get("reason") or "").lower()
        if kind == "preflight/gateway" and preflight.get("source_gateway_running"):
            found.add("source_in_use")
        elif kind == "preflight/schema":
            found.add("source_schema_newer")
        elif kind == "preflight/sqlite":
            found.add("source_database_invalid")
        elif kind == "preflight/config":
            found.add("source_config_invalid")
        elif kind == "preflight/disk":
            found.add("insufficient_disk")
        elif kind == "preflight/recovery" or "recovery profile" in reason:
            found.add("target_recovery_required")
        elif kind == "preflight/target":
            found.add("target_not_replaceable")
        elif kind in {
            "source",
            "preflight/manifest",
            "preflight/data-root",
            "data-root",
        }:
            found.add("source_unreadable")
        else:
            unknown_error = True
    if unknown_error or (not found and _error_count(report) > 0):
        found.add("preview_unavailable")
    return [code for code in _BLOCKER_ORDER if code in found]


def _error_count(report: dict[str, Any]) -> int:
    items = report.get("items")
    return sum(
        1
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and item.get("status") == "error"
    )


def _item_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = {"planned": 0, "skipped": 0, "error": 0}
    items = report.get("items")
    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _int_value(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _preview_payload(
    record: _CandidateRecord,
    report: dict[str, Any],
    *,
    target_has_data: bool,
) -> dict[str, Any]:
    blockers = _blockers(report)
    notices: list[str] = []
    if target_has_data:
        notices.append("whole_profile_replacement")
    if record.public.get("previouslyImported") is True:
        notices.append("previously_imported")
    paused_jobs = report.get("paused_jobs")
    paused_job_count = len(paused_jobs) if isinstance(paused_jobs, list) else 0
    if paused_job_count:
        notices.append("scheduled_jobs_will_be_paused")
    preflight = report.get("preflight")
    preflight = preflight if isinstance(preflight, dict) else {}
    session_count = preflight.get("session_count")
    if not isinstance(session_count, int) or isinstance(session_count, bool):
        session_count = None
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "mode": "preview_only",
        "candidate": dict(record.public),
        "previewStatus": "blocked" if blockers else "available",
        "targetAction": "replace" if target_has_data else "copy",
        "summary": {
            "sessionCount": session_count,
            "itemCounts": _item_counts(report),
            "pausedJobCount": paused_job_count,
            "diskRequiredBytes": _int_value(preflight, "disk_required_bytes"),
            "diskFreeBytes": _int_value(preflight, "disk_free_bytes"),
        },
        "blockers": blockers,
        "notices": notices,
        "execution": {
            "canApply": False,
            "supportedBy": ["desktop", "host_cli"],
        },
    }


@_d.method("migration.sources.list", scope="operator.admin")
async def _handle_migration_sources_list(
    params: Any,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_empty_object(params)
    target = _target_for_context(ctx)
    if target is None:
        return _list_payload([], available=False)
    target_identity = _directory_identity(target)
    if target_identity is None:
        return _list_payload([], available=False)
    try:
        discovered = await asyncio.to_thread(_discover_sync, target)
    except RpcHandlerError:
        raise
    except Exception:
        log.warning("migration.sources.list_failed", result_code="discovery_unavailable")
        raise RpcHandlerError(
            "migration.unavailable",
            "Migration source discovery is temporarily unavailable.",
            retryable=True,
        ) from None
    try:
        if target_identity != _directory_identity(target):
            raise _candidate_unavailable()
        candidates = _store_candidates(
            ctx=ctx,
            target=target,
            target_identity=target_identity,
            discovered=discovered,
        )
    except RpcHandlerError:
        raise
    except Exception:
        log.warning("migration.sources.list_failed", result_code="cache_unavailable")
        raise RpcHandlerError(
            "migration.unavailable",
            "Migration source discovery is temporarily unavailable.",
            retryable=True,
        ) from None
    log.info("migration.sources.list", candidate_count=len(candidates))
    return _list_payload(candidates)


@_d.method("migration.sources.preview", scope="operator.admin")
async def _handle_migration_sources_preview(
    params: Any,
    ctx: RpcContext,
) -> dict[str, Any]:
    candidate_id = _require_preview_params(params)
    target = _target_for_context(ctx)
    if target is None:
        raise RpcHandlerError(
            "migration.unavailable",
            "Migration preview is unavailable without an active gateway config.",
        )
    record = _resolve_candidate(candidate_id, ctx=ctx, target=target)
    try:
        report, target_has_data = await asyncio.to_thread(
            _preview_sync,
            record,
            target=target,
        )
    except RpcHandlerError:
        raise
    except Exception:
        log.warning(
            "migration.sources.preview_failed",
            source_kind=record.source_kind,
            result_code="preview_unavailable",
        )
        raise RpcHandlerError(
            "migration.preview_unavailable",
            "Migration preview is temporarily unavailable.",
            retryable=True,
        ) from None
    payload = _preview_payload(
        record,
        report,
        target_has_data=target_has_data,
    )
    log.info(
        "migration.sources.preview",
        source_kind=record.source_kind,
        preview_status=payload["previewStatus"],
        blockers=payload["blockers"],
    )
    return payload


__all__ = [
    "_handle_migration_sources_list",
    "_handle_migration_sources_preview",
]
