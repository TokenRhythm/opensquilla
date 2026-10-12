from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import structlog

from opensquilla.asyncio_utils import create_background_task
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.session.compaction_lifecycle import (
    flush_receipt_allows_destructive_compaction,
    flush_receipt_to_dict,
)
from opensquilla.session.keys import normalize_agent_id
from opensquilla.session.models import MemoryDurableReceipt

log = structlog.get_logger(__name__)

_RAW_FALLBACK_REPAIR_DIR = ".repair_receipts"
_RAW_FALLBACK_LINE_RE = re.compile(r"^(user|assistant|system):\s?(.*)$")
_RAW_FALLBACK_HEADER_RE = re.compile(r"^# Raw flush \(([^)]+)\)")
_REPAIR_QUEUE_STATUSES = ("repair_pending", "distill_failed", "flush_failed")
_REPAIR_RUNNING_STATUS = "repair_running"
_REPAIR_CLAIM_STALE_MS = 30 * 60 * 1000
_REPAIR_BACKOFF_MS = {
    1: 5 * 60 * 1000,
    2: 30 * 60 * 1000,
    3: 6 * 60 * 60 * 1000,
}


def _build_repair_correlation(
    session_id: object,
    *,
    enabled: bool,
) -> tuple[str, ProviderRequestCorrelation | None]:
    turn_id = f"maintenance_{uuid.uuid4().hex}"
    if not enabled or not isinstance(session_id, str) or not session_id:
        return turn_id, None
    return (
        turn_id,
        ProviderRequestCorrelation(
            session_id=session_id,
            turn_id=turn_id,
            execution_id=uuid.uuid4().hex,
            call_kind="auxiliary.session_flush",
        ),
    )


def _correlatable_repair_session_id(source: Any) -> str | None:
    """Return a real durable session ID, excluding legacy repair namespaces."""

    session_id = getattr(source, "session_id", None)
    session_key = getattr(source, "session_key", None)
    if not isinstance(session_id, str) or not session_id:
        return None
    if session_id.startswith("legacy-raw:"):
        return None
    if isinstance(session_key, str) and ":memory-repair:legacy-raw" in session_key:
        return None
    return session_id


def repair_receipt_path(receipt: Any) -> str | None:
    source_path = getattr(receipt, "source_path", None)
    target_path = getattr(receipt, "target_path", None)
    for path in (source_path, target_path):
        if isinstance(path, str) and path.startswith("memory/.raw_fallbacks/"):
            return path
    if isinstance(source_path, str) and source_path:
        return source_path
    if isinstance(target_path, str) and target_path:
        return target_path
    return None


def _agent_session_key_prefix(agent_id: str | None) -> str | None:
    return f"agent:{normalize_agent_id(agent_id)}:" if agent_id else None


def _summary_to_repair_wire(summary: Any) -> dict[str, Any]:
    return {
        "sourceType": "compaction_preimage",
        "summaryId": getattr(summary, "id", None),
        "sessionId": getattr(summary, "session_id", None),
        "sessionKey": getattr(summary, "session_key", None),
        "compactionId": getattr(summary, "compaction_id", None),
        "triggerReason": getattr(summary, "trigger_reason", None),
        "flushReceiptStatus": getattr(summary, "flush_receipt_status", "unknown"),
        "removedCount": getattr(summary, "removed_count", None),
        "coveredThroughId": getattr(summary, "covered_through_id", None),
        "createdAt": getattr(summary, "created_at", None),
    }


def repair_receipt_to_wire(receipt: Any) -> dict[str, Any]:
    path = repair_receipt_path(receipt)
    source_type = (
        "raw_fallback"
        if isinstance(path, str) and path.startswith("memory/.raw_fallbacks/")
        else "durable_receipt"
    )
    return {
        "sourceType": source_type,
        "receiptId": getattr(receipt, "receipt_id", None),
        "sessionId": getattr(receipt, "session_id", None),
        "sessionKey": getattr(receipt, "session_key", None),
        "turnId": getattr(receipt, "turn_id", None),
        "scope": getattr(receipt, "scope", None),
        "path": path,
        "sourcePath": getattr(receipt, "source_path", None),
        "targetPath": getattr(receipt, "target_path", None),
        "contentHash": getattr(receipt, "content_hash", None),
        "repairStatus": getattr(receipt, "status", None),
        "reason": getattr(receipt, "reason", None),
        "attemptCount": getattr(receipt, "attempt_count", None),
        "nextRetryAtMs": getattr(receipt, "next_retry_at_ms", None),
        "createdAt": getattr(receipt, "created_at", None),
        "updatedAt": getattr(receipt, "updated_at", None),
    }


def _entry_to_repair_wire(entry: Any) -> dict[str, Any]:
    return {
        "id": getattr(entry, "id", None),
        "messageId": getattr(entry, "message_id", None),
        "role": getattr(entry, "role", None),
        "content": getattr(entry, "content", "") or "",
        "tokenCount": getattr(entry, "token_count", None),
        "createdAt": getattr(entry, "created_at", None),
    }


def _preimage_metadata(entries: Sequence[Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    ids: list[int] = []
    for entry in entries:
        entry_id = getattr(entry, "id", None)
        try:
            if entry_id is not None:
                ids.append(int(entry_id))
        except (TypeError, ValueError):
            pass
        material = "\n".join(
            (
                str(entry_id or ""),
                str(getattr(entry, "message_id", "") or ""),
                str(getattr(entry, "role", "") or ""),
                str(getattr(entry, "content", "") or ""),
            )
        )
        digest.update(material.encode("utf-8", errors="replace"))
        digest.update(b"\n---\n")
    return {
        "entryIdRange": [min(ids), max(ids)] if ids else None,
        "preimageHash": digest.hexdigest() if entries else None,
        "rangePolicy": "archived_full_removed_entries",
    }


def summary_matches(summary: Any, params: Mapping[str, Any]) -> bool:
    summary_id = params.get("summaryId")
    if summary_id is not None and str(getattr(summary, "id", "")) != str(summary_id):
        return False
    session_key = params.get("sessionKey")
    if session_key is not None and str(getattr(summary, "session_key", "")) != str(session_key):
        return False
    compaction_id = params.get("compactionId")
    if compaction_id is not None and str(getattr(summary, "compaction_id", "")) != str(
        compaction_id
    ):
        return False
    return True


def raw_fallback_rel_path(path: str) -> Path:
    rel = Path(path)
    if rel.is_absolute():
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    if len(rel.parts) == 1:
        rel = Path("memory") / ".raw_fallbacks" / rel
    if len(rel.parts) != 3 or rel.parts[:2] != ("memory", ".raw_fallbacks"):
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    if rel.name.startswith(".") or rel.suffix != ".md":
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    return rel


def raw_fallback_reason(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    match = _RAW_FALLBACK_HEADER_RE.match(first_line.strip())
    return match.group(1) if match else None


def _raw_repair_receipt_path(raw_path: Path) -> Path:
    return raw_path.parent / _RAW_FALLBACK_REPAIR_DIR / f"{raw_path.name}.json"


def _read_raw_repair_status(raw_path: Path) -> str:
    receipt_path = _raw_repair_receipt_path(raw_path)
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "pending"
    return str(payload.get("status") or "pending")


def _write_raw_repair_status(
    raw_path: Path,
    *,
    status: str,
    receipt: Any | None = None,
    reason: str | None = None,
) -> None:
    receipt_path = _raw_repair_receipt_path(raw_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "reason": reason,
        "updatedAt": int(time.time() * 1000),
        "receipt": flush_receipt_to_dict(receipt) if receipt is not None else None,
    }
    receipt_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_raw_fallback_entries(content: str) -> list[Any]:
    entries: list[Any] = []
    current_role: str | None = None
    current_lines: list[str] = []

    def flush_current() -> None:
        nonlocal current_role, current_lines
        body = "\n".join(line for line in current_lines if line).strip()
        if current_role and body:
            entries.append(SimpleNamespace(role=current_role, content=body))
        current_role = None
        current_lines = []

    for line in content.splitlines():
        text = line.strip()
        if not text:
            continue
        match = _RAW_FALLBACK_LINE_RE.match(text)
        if match is not None:
            flush_current()
            current_role, body = match.groups()
            current_lines = [body] if body else []
            continue
        if current_role is None and (text.startswith("#") or text.startswith("<!--")):
            continue
        if current_role is not None:
            current_lines.append(text)
    flush_current()
    return entries


def raw_fallback_rows(root: Path, *, include_repaired: bool = False) -> list[dict[str, Any]]:
    raw_root = root / "memory" / ".raw_fallbacks"
    if not raw_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for file_path in sorted(path for path in raw_root.glob("*.md") if path.is_file()):
        try:
            stat = file_path.stat()
        except OSError:
            continue
        status = _read_raw_repair_status(file_path)
        if status == "repaired" and not include_repaired:
            continue
        rows.append(
            {
                "sourceType": "raw_fallback",
                "path": (Path("memory") / ".raw_fallbacks" / file_path.name).as_posix(),
                "sizeBytes": stat.st_size,
                "modifiedAt": int(stat.st_mtime * 1000),
                "reason": raw_fallback_reason(file_path),
                "repairStatus": status,
            }
        )
    return rows


async def list_repair_queue(
    storage: Any,
    limit: int = 100,
    *,
    due_only: bool = False,
    now_ms: int | None = None,
    path: str | None = None,
    agent_id: str | None = None,
) -> list[MemoryDurableReceipt]:
    limit = max(0, int(limit))
    if limit == 0:
        return []
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    list_repair_receipts = getattr(storage, "list_memory_repair_receipts", None)
    if callable(list_repair_receipts):
        return list(
            await list_repair_receipts(
                statuses=_REPAIR_QUEUE_STATUSES,
                limit=limit,
                due_before_ms=now_ms if due_only else None,
                path=path,
                session_key_prefix=_agent_session_key_prefix(agent_id),
            )
        )

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return []
    receipt_rows: list[Any] = []
    scan_limit = max(limit, 1000)
    for status in _REPAIR_QUEUE_STATUSES:
        receipt_rows.extend(await list_receipts(status=status, limit=scan_limit))
    if path is not None:
        receipt_rows = [
            row
            for row in receipt_rows
            if getattr(row, "source_path", None) == path
            or getattr(row, "target_path", None) == path
        ]
    agent_prefix = _agent_session_key_prefix(agent_id)
    if agent_prefix is not None:
        receipt_rows = [
            row
            for row in receipt_rows
            if str(getattr(row, "session_key", "") or "").startswith(agent_prefix)
        ]
    if due_only:
        receipt_rows = [
            row
            for row in receipt_rows
            if getattr(row, "next_retry_at_ms", None) is None
            or int(getattr(row, "next_retry_at_ms", 0) or 0) <= now_ms
        ]
    receipt_rows.sort(
        key=lambda row: (
            getattr(row, "next_retry_at_ms", None) is not None,
            getattr(row, "next_retry_at_ms", None) or 0,
            getattr(row, "created_at", 0) or 0,
            getattr(row, "receipt_id", "") or "",
        )
    )
    return list(receipt_rows[:limit])


async def claim_repair_receipt(
    storage: Any,
    receipt: Any,
    *,
    now_ms: int | None = None,
) -> Any | None:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    receipt_id = getattr(receipt, "receipt_id", None)
    if not receipt_id:
        return None
    claim = getattr(storage, "claim_memory_repair_receipt", None)
    if callable(claim):
        return await claim(
            receipt_id,
            eligible_statuses=_REPAIR_QUEUE_STATUSES,
            claimed_status=_REPAIR_RUNNING_STATUS,
            now_ms=now_ms,
        )

    if getattr(receipt, "status", None) not in _REPAIR_QUEUE_STATUSES:
        return None
    next_retry_at_ms = getattr(receipt, "next_retry_at_ms", None)
    if next_retry_at_ms is not None and int(next_retry_at_ms) > now_ms:
        return None
    update = getattr(storage, "update_memory_durable_receipt", None)
    if not callable(update):
        return receipt
    return await update(receipt_id, status=_REPAIR_RUNNING_STATUS)


async def recover_stale_repair_claims(
    storage: Any,
    *,
    now_ms: int | None = None,
) -> int:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    recover = getattr(storage, "recover_stale_memory_repair_claims", None)
    if not callable(recover):
        return 0
    return int(
        await recover(
            running_status=_REPAIR_RUNNING_STATUS,
            pending_status="repair_pending",
            stale_before_ms=now_ms - _REPAIR_CLAIM_STALE_MS,
            next_retry_at_ms=now_ms + _REPAIR_BACKOFF_MS[1],
            updated_at_ms=now_ms,
            reason="stale_repair_claim",
        )
    )


async def _repair_receipt_exists_for_path(
    storage: Any,
    path: str,
    *,
    agent_id: str | None = None,
) -> bool:
    agent_prefix = _agent_session_key_prefix(agent_id)
    exists_for_path = getattr(storage, "memory_durable_receipt_exists_for_path", None)
    if callable(exists_for_path):
        return bool(
            await exists_for_path(path, session_key_prefix=agent_prefix)
        )

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return False
    for status in (*_REPAIR_QUEUE_STATUSES, "repair_done", "repair_abandoned"):
        rows = await list_receipts(status=status, limit=1000)
        if any(
            (
                getattr(row, "source_path", None) == path
                or getattr(row, "target_path", None) == path
            )
            and (
                agent_prefix is None
                or str(getattr(row, "session_key", "") or "").startswith(agent_prefix)
            )
            for row in rows
        ):
            return True
    return False


async def import_legacy_raw_fallback_receipts(
    storage: Any | None,
    root: Path | None,
    *,
    agent_id: str,
) -> None:
    if storage is None or root is None:
        return
    upsert = getattr(storage, "upsert_memory_durable_receipt", None)
    if not callable(upsert):
        return
    for row in raw_fallback_rows(root):
        path = str(row.get("path") or "")
        if not path:
            continue
        if await _repair_receipt_exists_for_path(storage, path, agent_id=agent_id):
            continue
        await upsert(
            MemoryDurableReceipt(
                session_key=f"agent:{agent_id}:memory-repair:legacy-raw",
                session_id=f"legacy-raw:{agent_id}",
                scope="repair",
                source_path=path,
                idempotency_key=f"repair-legacy-raw:{agent_id}:{path}",
                status="repair_pending",
                reason=str(row.get("reason") or "legacy_raw_fallback"),
                created_at=int(row.get("modifiedAt") or time.time() * 1000),
            )
        )


async def mark_repair_attempt_failed(
    storage: Any,
    receipt: Any,
    *,
    reason: str,
    now_ms: int | None = None,
    attempt_source: Any | None = None,
) -> Any:
    update = getattr(storage, "update_memory_durable_receipt", None)
    if not callable(update):
        return receipt
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    attempt_source = attempt_source or receipt
    previous_attempts = int(getattr(attempt_source, "attempt_count", 0) or 0)
    if (
        previous_attempts == 1
        and getattr(attempt_source, "status", None) == "repair_pending"
        and getattr(attempt_source, "next_retry_at_ms", None) is None
    ):
        attempt_count = 1
    else:
        attempt_count = previous_attempts + 1
    if attempt_count >= 4:
        return await update(
            getattr(receipt, "receipt_id"),
            status="repair_abandoned",
            reason=reason,
            attempt_count=attempt_count,
            next_retry_at_ms=None,
        )
    return await update(
        getattr(receipt, "receipt_id"),
        status="repair_pending",
        reason=reason,
        attempt_count=attempt_count,
        next_retry_at_ms=now_ms + _REPAIR_BACKOFF_MS[attempt_count],
    )


async def mark_repair_attempt_done(storage: Any, receipt: Any) -> Any:
    update = getattr(storage, "update_memory_durable_receipt", None)
    if not callable(update):
        return receipt
    return await update(
        getattr(receipt, "receipt_id"),
        status="repair_done",
        next_retry_at_ms=None,
    )


def _raw_path_matches(row: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    path = params.get("path")
    return path is None or str(row.get("path") or "") == raw_fallback_rel_path(str(path)).as_posix()


def _memory_root(memory_roots: Mapping[str, Path], agent_id: str) -> Path | None:
    root = memory_roots.get(normalize_agent_id(agent_id))
    return Path(root) if root is not None else None


async def list_compaction_repair_sources(
    session_manager: Any,
    *,
    agent_id: str,
    params: Mapping[str, Any],
    limit: int,
    scan_limit: int,
) -> list[Any]:
    list_degraded = getattr(session_manager, "list_degraded_compactions", None)
    if not callable(list_degraded):
        return []
    has_selector = any(k in params for k in ("summaryId", "sessionKey", "compactionId"))
    rows = await list_degraded(agent_id=agent_id, limit=scan_limit if has_selector else limit)
    if has_selector:
        rows = [row for row in rows if summary_matches(row, params)]
    return list(rows)[:limit]


async def repair_compaction_source(
    summary: Any,
    *,
    session_manager: Any,
    flush_service: Any,
    agent_id: str,
    provider_request_correlation_enabled: bool = True,
) -> dict[str, Any]:
    get_preimage = getattr(session_manager, "get_compaction_preimage", None)
    mark_status = getattr(session_manager, "mark_compaction_repair_status", None)
    base = _summary_to_repair_wire(summary)
    if not callable(get_preimage) or not callable(mark_status):
        return {**base, "status": "failed_retryable", "reason": "repair_storage_unavailable"}
    entries = list(await get_preimage(summary))
    metadata = _preimage_metadata(entries)
    if not entries:
        await mark_status(summary, "failed_retryable")
        return {**base, **metadata, "status": "failed_retryable", "reason": "missing_preimage"}
    repair_turn_id, correlation = _build_repair_correlation(
        _correlatable_repair_session_id(summary),
        enabled=provider_request_correlation_enabled,
    )
    try:
        receipt = await flush_service.execute(
            entries,
            str(getattr(summary, "session_key", "")),
            agent_id=agent_id,
            message_window=0,
            segment_mode="auto",
            raw_capture_policy="off",
            turn_id=repair_turn_id,
            provider_request_correlation=correlation,
        )
    except Exception as exc:  # noqa: BLE001
        await mark_status(summary, "failed_retryable")
        return {**base, **metadata, "status": "failed_retryable", "reason": type(exc).__name__}
    repaired = flush_receipt_allows_destructive_compaction(receipt)
    status = "repaired" if repaired else "failed_retryable"
    await mark_status(summary, status)
    return {**base, **metadata, "status": status, "receipt": flush_receipt_to_dict(receipt)}


async def repair_raw_fallback_source(
    root: Path,
    row: Mapping[str, Any],
    *,
    flush_service: Any,
    agent_id: str,
    turn_id: str | None = None,
    provider_request_correlation: ProviderRequestCorrelation | None = None,
) -> dict[str, Any]:
    rel_path = raw_fallback_rel_path(str(row.get("path") or ""))
    raw_path = (root / rel_path).resolve()
    raw_root = (root / "memory" / ".raw_fallbacks").resolve()
    if raw_root not in raw_path.parents:
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    base = dict(row)
    try:
        content = raw_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {**base, "status": "failed_retryable", "reason": type(exc).__name__}
    entries = parse_raw_fallback_entries(content)
    if not entries:
        _write_raw_repair_status(raw_path, status="failed_retryable", reason="empty_raw_excerpt")
        return {**base, "status": "failed_retryable", "reason": "empty_raw_excerpt"}
    raw_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    flush_kwargs: dict[str, Any] = {
        "agent_id": agent_id,
        "message_window": 0,
        "segment_mode": "auto",
        "raw_capture_policy": "off",
    }
    if turn_id is not None:
        flush_kwargs["turn_id"] = turn_id
    if provider_request_correlation is not None:
        flush_kwargs["provider_request_correlation"] = (
            provider_request_correlation
        )
    try:
        receipt = await flush_service.execute(
            entries,
            f"raw-fallback:{raw_hash}",
            **flush_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        _write_raw_repair_status(raw_path, status="failed_retryable", reason=type(exc).__name__)
        return {**base, "status": "failed_retryable", "reason": type(exc).__name__}
    repaired = flush_receipt_allows_destructive_compaction(receipt)
    status = "repaired" if repaired else "failed_retryable"
    _write_raw_repair_status(raw_path, status=status, receipt=receipt)
    return {**base, "status": status, "receipt": flush_receipt_to_dict(receipt)}


async def repair_durable_receipt_source(
    storage: Any,
    receipt: Any,
    *,
    root: Path,
    flush_service: Any,
    agent_id: str,
    provider_request_correlation_enabled: bool = True,
) -> dict[str, Any]:
    base = repair_receipt_to_wire(receipt)
    path = repair_receipt_path(receipt)
    if not isinstance(path, str) or not path.startswith("memory/.raw_fallbacks/"):
        return {**base, "status": "skipped", "reason": "unsupported_repair_source"}
    claimed = await claim_repair_receipt(storage, receipt)
    if claimed is None:
        return {**base, "status": "skipped", "reason": "repair_already_claimed"}
    repair_turn_id, correlation = _build_repair_correlation(
        _correlatable_repair_session_id(receipt),
        enabled=provider_request_correlation_enabled,
    )
    result = await repair_raw_fallback_source(
        root,
        {**base, "path": path},
        flush_service=flush_service,
        agent_id=agent_id,
        turn_id=repair_turn_id,
        provider_request_correlation=correlation,
    )
    if result.get("status") == "repaired":
        await mark_repair_attempt_done(storage, claimed)
    else:
        await mark_repair_attempt_failed(
            storage,
            claimed,
            reason=str(result.get("reason") or "repair_failed"),
            attempt_source=receipt,
        )
    return result


async def run_memory_repair_once(
    *,
    session_manager: Any,
    flush_service: Any,
    memory_roots: Mapping[str, Path],
    agent_id: str,
    limit: int,
    params: Mapping[str, Any] | None = None,
    scan_limit: int = 200,
    provider_request_correlation_enabled: bool = True,
) -> list[dict[str, Any]]:
    params = params or {}
    agent_id = normalize_agent_id(agent_id)
    if not callable(getattr(flush_service, "execute", None)):
        raise RuntimeError("Session flush service is not configured")
    results: list[dict[str, Any]] = []
    root = _memory_root(memory_roots, agent_id)
    storage = get_session_storage(session_manager)
    has_compaction_selector = any(k in params for k in ("summaryId", "sessionKey", "compactionId"))
    if has_compaction_selector:
        compaction_sources = await list_compaction_repair_sources(
            session_manager,
            agent_id=agent_id,
            params=params,
            limit=limit,
            scan_limit=scan_limit,
        )
        for summary in compaction_sources:
            if len(results) >= limit:
                return results
            results.append(
                await repair_compaction_source(
                    summary,
                    session_manager=session_manager,
                    flush_service=flush_service,
                    agent_id=agent_id,
                    provider_request_correlation_enabled=(
                        provider_request_correlation_enabled
                    ),
                )
            )
        return results
    if root is not None:
        await import_legacy_raw_fallback_receipts(storage, root, agent_id=agent_id)
    if storage is not None:
        await recover_stale_repair_claims(storage)
        selected_path = (
            raw_fallback_rel_path(str(params.get("path") or "")).as_posix()
            if "path" in params
            else None
        )
        queue_rows = await list_repair_queue(
            storage,
            limit=scan_limit,
            due_only=True,
            path=selected_path,
            agent_id=agent_id,
        )
        for receipt in queue_rows:
            if len(results) >= limit:
                return results
            if root is None:
                results.append(
                    {
                        **repair_receipt_to_wire(receipt),
                        "status": "failed_retryable",
                        "reason": "memory_root_unavailable",
                    }
                )
                continue
            result = await repair_durable_receipt_source(
                storage,
                receipt,
                root=root,
                flush_service=flush_service,
                agent_id=agent_id,
                provider_request_correlation_enabled=(
                    provider_request_correlation_enabled
                ),
            )
            if result.get("reason") == "repair_already_claimed":
                continue
            results.append(result)
        return results

    compaction_sources = []
    if storage is None and "path" not in params:
        compaction_sources = await list_compaction_repair_sources(
            session_manager,
            agent_id=agent_id,
            params=params,
            limit=limit,
            scan_limit=scan_limit,
        )
    for summary in compaction_sources:
        if len(results) >= limit:
            return results
        results.append(
            await repair_compaction_source(
                summary,
                session_manager=session_manager,
                flush_service=flush_service,
                agent_id=agent_id,
                provider_request_correlation_enabled=(
                    provider_request_correlation_enabled
                ),
            )
        )

    if root is None:
        return results
    include_repaired = "path" in params
    raw_rows = [
        row
        for row in raw_fallback_rows(root, include_repaired=include_repaired)
        if _raw_path_matches(row, params)
    ]
    for row in raw_rows:
        if len(results) >= limit:
            break
        results.append(
            await repair_raw_fallback_source(
                root,
                row,
                flush_service=flush_service,
                agent_id=agent_id,
            )
        )
    return results


class MemoryRepairService:
    def __init__(
        self,
        *,
        session_manager: Any,
        flush_service: Any,
        memory_roots: Mapping[str, Path],
        agent_ids: Sequence[str] = ("main",),
        interval_seconds: float = 60.0,
        max_items_per_tick: int = 5,
        enabled: bool = True,
        usage_event_sink: Any | None = None,
        config: Any | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._flush_service = flush_service
        self._memory_roots = {
            normalize_agent_id(agent_id): Path(root)
            for agent_id, root in memory_roots.items()
        }
        self._agent_ids = tuple(normalize_agent_id(agent_id) for agent_id in agent_ids)
        self._interval_seconds = max(float(interval_seconds), 0.01)
        self._max_items_per_tick = max(int(max_items_per_tick), 1)
        self._enabled = enabled
        self._usage_event_sink = usage_event_sink
        self._config = config
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if not self._enabled or self._task is not None:
            return
        self._stop_event.clear()
        self._task = create_background_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_once(
        self, *, agent_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        limit_value = limit if limit is not None else self._max_items_per_tick
        agent_ids = (agent_id,) if agent_id is not None else self._agent_ids
        results: list[dict[str, Any]] = []
        for current_agent_id in agent_ids:
            if len(results) >= limit_value:
                break
            usage_scope = None
            if self._usage_event_sink is not None:
                from opensquilla.engine.usage_accounting import (
                    UsageAccountingScope,
                    UsageExecutionContext,
                )

                execution_id = uuid.uuid4().hex
                usage_scope = UsageAccountingScope(
                    sink=self._usage_event_sink,
                    context=UsageExecutionContext(
                        execution_id=execution_id,
                        agent_run_id=execution_id,
                        turn_id=execution_id,
                        session_id=uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"opensquilla:system:memory-repair:{current_agent_id}",
                        ).hex,
                        agent_id=current_agent_id,
                        run_kind="memory_repair",
                    ),
                )
            from opensquilla.engine.usage_accounting import bind_usage_accounting_scope

            with bind_usage_accounting_scope(usage_scope):
                results.extend(
                    await run_memory_repair_once(
                        session_manager=self._session_manager,
                        flush_service=self._flush_service,
                        memory_roots=self._memory_roots,
                        agent_id=current_agent_id,
                        limit=limit_value - len(results),
                        provider_request_correlation_enabled=(
                            not provider_request_correlation_disabled(
                                config=self._config
                            )
                        ),
                    )
                )
        return results[:limit_value]

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("memory_repair.tick_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue
