"""RPC handlers for read-only memory inspection."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.gateway.memory_repair_service import (
    import_legacy_raw_fallback_receipts,
    list_repair_queue,
    parse_raw_fallback_entries,
    repair_receipt_to_wire,
    run_memory_repair_once,
)
from opensquilla.gateway.memory_repair_service import (
    raw_fallback_rows as repair_raw_fallback_rows,
)
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.memory.types import (
    DEFAULT_MEMORY_SEARCH_MIN_SCORE,
    DEFAULT_MEMORY_SEARCH_RESULTS,
    MemorySearchOpts,
    SearchIntent,
    normalize_memory_search_min_score,
    normalize_memory_source_filter,
)
from opensquilla.observability.network_policy import (
    provider_request_correlation_disabled,
)
from opensquilla.session.keys import normalize_agent_id
from opensquilla.tools.builtin.memory_tools import _is_memory_source_path

_d = get_dispatcher()

_MAX_MEMORY_SHOW_CHARS = 8000
_MAX_MEMORY_SHOW_LINES = 500
_MAX_MEMORY_SHOW_FILE_BYTES = 1024 * 1024
_MAX_REPAIR_ENTRY_CHARS = 4000
_MAX_REPAIR_SHOW_ENTRIES = 100
_MAX_REPAIR_LIST_LIMIT = 200
_REPAIR_SCAN_LIMIT = 1000
_HEALTH_SCAN_LIMIT = 1000
_SAFETY_ERROR_STATUSES = {"checkpoint_failed", "receipt_orphaned"}
_HASH_MISMATCH_MARKERS = ("hash_mismatch", "hash mismatch")
_SEMANTIC_WARNING_AGE_MS = 24 * 60 * 60 * 1000


def _require_memory_manager(ctx: RpcContext, agent_id: str | None) -> tuple[str, Any]:
    managers = getattr(ctx, "memory_managers", None) or {}
    if not managers:
        raise RpcUnavailableError("No memory managers configured")
    resolved_agent = normalize_agent_id(agent_id or "main")
    manager = managers.get(resolved_agent)
    if manager is None:
        raise KeyError(f"Memory manager not found for agent: {resolved_agent}")
    return resolved_agent, manager


def _require_session_manager(ctx: RpcContext) -> Any:
    manager = getattr(ctx, "session_manager", None)
    if manager is None:
        raise RpcUnavailableError("Session manager is not configured")
    return manager


def _agent_session_key_prefix(agent_id: str | None) -> str | None:
    return f"agent:{normalize_agent_id(agent_id)}:" if agent_id else None


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def _is_safety_error_receipt(row: Any) -> bool:
    status = str(_row_value(row, "status", "") or "").lower()
    reason = str(_row_value(row, "reason", "") or "").lower()
    if status in _SAFETY_ERROR_STATUSES:
        return True
    return any(marker in status or marker in reason for marker in _HASH_MISMATCH_MARKERS)


async def _recent_durable_receipts(storage: Any, *, agent_id: str) -> list[Any]:
    agent_prefix = _agent_session_key_prefix(agent_id)
    list_recent = getattr(storage, "list_recent_memory_durable_receipts", None)
    if callable(list_recent):
        return list(
            await list_recent(
                limit=_HEALTH_SCAN_LIMIT,
                session_key_prefix=agent_prefix,
            )
        )

    list_receipts = getattr(storage, "list_memory_durable_receipts", None)
    if not callable(list_receipts):
        return []
    receipt_rows: list[Any] = []
    for status in (*_SAFETY_ERROR_STATUSES, "hash_mismatch"):
        receipt_rows.extend(await list_receipts(status=status, limit=_HEALTH_SCAN_LIMIT))
    if agent_prefix is not None:
        receipt_rows = [
            row
            for row in receipt_rows
            if str(_row_value(row, "session_key", "") or "").startswith(agent_prefix)
        ]
    receipt_rows.sort(
        key=lambda row: (
            int(_row_value(row, "created_at", 0) or 0),
            str(_row_value(row, "receipt_id", "") or ""),
        ),
        reverse=True,
    )
    return list(receipt_rows[:_HEALTH_SCAN_LIMIT])


def _semantic_repair_status(backlog_count: int, oldest_pending_ms: int | None) -> str:
    if backlog_count <= 0:
        return "healthy"
    if backlog_count > 10:
        return "warning"
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if oldest_pending_ms is None or now_ms - oldest_pending_ms > _SEMANTIC_WARNING_AGE_MS:
        return "warning"
    return "degraded"


async def memory_health_from_durable_ledger(
    session_manager: Any,
    *,
    agent_id: str,
) -> dict[str, Any]:
    storage = get_session_storage(session_manager)
    if storage is None:
        return {
            "memorySafety": {"status": "ok"},
            "semanticMemory": {"status": "healthy", "repairBacklogCount": 0},
        }

    recent_rows = await _recent_durable_receipts(storage, agent_id=agent_id)
    safety_status = "error" if any(_is_safety_error_receipt(row) for row in recent_rows) else "ok"
    pending_rows = await list_repair_queue(
        storage,
        limit=_HEALTH_SCAN_LIMIT,
        agent_id=agent_id,
        due_only=False,
    )
    backlog_count = len(pending_rows)
    oldest_pending_ms = min(
        (
            int(created_at)
            for row in pending_rows
            if (created_at := _row_value(row, "created_at", None)) is not None
        ),
        default=None,
    )
    return {
        "memorySafety": {"status": safety_status},
        "semanticMemory": {
            "status": _semantic_repair_status(backlog_count, oldest_pending_ms),
            "repairBacklogCount": backlog_count,
        },
    }


def _int_param(
    params: dict[str, Any],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = params.get(name, default)
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params.{name} must be an integer") from exc
    if number < minimum:
        raise ValueError(f"params.{name} must be >= {minimum}")
    if number > maximum:
        raise ValueError(f"params.{name} must be <= {maximum}")
    return number


def _bool_param(params: dict[str, Any], name: str, default: bool = False) -> bool:
    value = params.get(name, default)
    if isinstance(value, bool):
        return value
    raise ValueError(f"params.{name} must be a boolean")


def _result_to_wire(result: Any) -> dict[str, Any]:
    source = getattr(result, "source", "")
    source_value = getattr(source, "value", source)
    return {
        "chunkId": getattr(result, "chunk_id", ""),
        "path": getattr(result, "path", ""),
        "source": str(source_value),
        "startLine": getattr(result, "start_line", 0),
        "endLine": getattr(result, "end_line", 0),
        "snippet": getattr(result, "snippet", ""),
        "score": getattr(result, "score", 0.0),
        "vectorScore": getattr(result, "vector_score", None),
        "textScore": getattr(result, "text_score", None),
        "chunkHash": getattr(result, "chunk_hash", None),
        "citation": getattr(result, "citation", None),
    }


def _summary_to_repair_wire(summary: Any) -> dict[str, Any]:
    return {
        "sourceType": "compaction_preimage",
        "summaryId": getattr(summary, "id", None),
        "sessionKey": getattr(summary, "session_key", ""),
        "compactionId": getattr(summary, "compaction_id", None),
        "triggerReason": getattr(summary, "trigger_reason", None),
        "flushReceiptStatus": getattr(summary, "flush_receipt_status", "unknown"),
        "removedCount": int(getattr(summary, "removed_count", 0) or 0),
        "coveredThroughId": getattr(summary, "covered_through_id", None),
        "createdAt": getattr(summary, "created_at", None),
    }


def _entry_to_repair_wire(entry: Any) -> dict[str, Any]:
    content = str(getattr(entry, "content", "") or "")
    truncated = len(content) > _MAX_REPAIR_ENTRY_CHARS
    return {
        "id": getattr(entry, "id", None),
        "messageId": getattr(entry, "message_id", None),
        "role": getattr(entry, "role", ""),
        "content": content[:_MAX_REPAIR_ENTRY_CHARS],
        "truncated": truncated,
        "tokenCount": getattr(entry, "token_count", None),
        "createdAt": getattr(entry, "created_at", None),
    }


def _preimage_metadata(entries: list[Any]) -> dict[str, Any]:
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
        "preimageHash": digest.hexdigest() if entries else None,
        "entryIdRange": [min(ids), max(ids)] if ids else None,
        "rangePolicy": "archived_full_removed_entries",
    }


def _summary_matches(summary: Any, params: dict[str, Any]) -> bool:
    summary_id = params.get("summaryId")
    if summary_id is not None:
        try:
            return int(summary_id) == int(getattr(summary, "id", -1) or -1)
        except (TypeError, ValueError):
            raise ValueError("params.summaryId must be an integer") from None
    session_key = str(params.get("sessionKey") or "").strip()
    compaction_id = str(params.get("compactionId") or "").strip()
    if session_key and session_key != str(getattr(summary, "session_key", "")):
        return False
    if compaction_id and compaction_id != str(getattr(summary, "compaction_id", "")):
        return False
    return bool(session_key or compaction_id)


async def _repair_summaries(
    manager: Any,
    *,
    agent_id: str,
    params: dict[str, Any],
    limit: int,
) -> list[Any]:
    list_degraded = getattr(manager, "list_degraded_compactions", None)
    if not callable(list_degraded):
        raise RpcUnavailableError("Compaction repair listing is not available")
    has_selector = any(k in params for k in ("summaryId", "sessionKey", "compactionId"))
    scan_limit = _REPAIR_SCAN_LIMIT if has_selector else limit
    rows = await list_degraded(agent_id=agent_id, limit=scan_limit)
    if has_selector:
        rows = [row for row in rows if _summary_matches(row, params)]
    return list(rows)[:limit]


def _memory_source_rows(root: Path) -> list[dict[str, Any]]:
    resolved_root = root.resolve()
    candidates: list[Path] = []
    memory_md = resolved_root / "MEMORY.md"
    if memory_md.is_file():
        candidates.append(memory_md)
    memory_dir = resolved_root / "memory"
    if memory_dir.is_dir():
        candidates.extend(path for path in memory_dir.rglob("*.md") if path.is_file())

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for file_path in candidates:
        try:
            resolved_file = file_path.resolve()
            rel = resolved_file.relative_to(resolved_root).as_posix()
        except ValueError:
            continue
        if rel in seen or not _is_memory_source_path(rel):
            continue
        stat = resolved_file.stat()
        with resolved_file.open("r", encoding="utf-8", errors="replace") as handle:
            line_count = sum(1 for _ in handle)
        seen.add(rel)
        rows.append(
            {
                "path": rel,
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "lineCount": line_count,
            }
        )
    return sorted(rows, key=lambda row: str(row["path"]))


async def _manager_status_wire(manager: Any) -> dict[str, Any]:
    status_fn = getattr(manager, "status", None)
    if not callable(status_fn):
        return {}
    status = await status_fn()
    return {
        "fileCount": status.get("file_count"),
        "chunkCount": status.get("chunk_count"),
        "sourceCounts": status.get("source_counts", {}),
        "vecAvailable": bool(status.get("vec_available", False)),
        "ftsAvailable": bool(status.get("fts_available", False)),
    }


@_d.method("memory.list", scope="operator.read")
async def _handle_memory_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, (params or {}).get("agentId"))
    root = _memory_root(manager)
    rows = _memory_source_rows(root)
    return {"agentId": agent_id, "count": len(rows), "files": rows}


@_d.method("memory.search", scope="operator.read")
async def _handle_memory_search(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("params.query is required")
    limit = _int_param(params, "limit", DEFAULT_MEMORY_SEARCH_RESULTS, minimum=1, maximum=20)
    try:
        min_score = normalize_memory_search_min_score(
            params.get("minScore", DEFAULT_MEMORY_SEARCH_MIN_SCORE),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("params.minScore must be a number") from exc
    try:
        source = normalize_memory_source_filter(params.get("source") or "memory")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    opts = MemorySearchOpts(max_results=limit, min_score=min_score, source=source)
    results = await manager.search(query, opts, intent=SearchIntent.ADMIN)
    rows = [_result_to_wire(result) for result in results]
    return {"agentId": agent_id, "query": query, "count": len(rows), "results": rows}


def _memory_root(manager: Any) -> Path:
    root = getattr(manager, "workspace_dir", None) or getattr(manager, "memory_dir", None)
    if root is None:
        raise RpcUnavailableError("Memory workspace directory is not configured")
    return Path(root)


def _repair_memory_roots(ctx: RpcContext) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for agent_id, manager in (getattr(ctx, "memory_managers", None) or {}).items():
        root = getattr(manager, "workspace_dir", None) or getattr(manager, "memory_dir", None)
        if root is not None:
            roots[normalize_agent_id(str(agent_id))] = Path(root)
    return roots


@_d.method("memory.index", scope="operator.admin")
async def _handle_memory_index(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))
    force = _bool_param(params, "force", False)
    if force:
        store = getattr(manager, "store", None)
        rebuild = getattr(store, "rebuild", None)
        if not callable(rebuild):
            raise RpcUnavailableError("Memory store rebuild is not available")
        await rebuild()
    sync = getattr(manager, "sync", None)
    if not callable(sync):
        raise RpcUnavailableError("Memory manager sync is not available")
    await sync(reason="manual", force=force)
    payload: dict[str, Any] = {
        "agentId": agent_id,
        "force": force,
    }
    payload.update(await _manager_status_wire(manager))
    return payload


def _validate_memory_path(path: str) -> None:
    if not path.strip():
        raise ValueError("params.path is required")
    if not _is_memory_source_path(path):
        raise ValueError("params.path must be MEMORY.md or memory/**/*.md")


def _raw_fallback_rel_path(path: str) -> str:
    raw = path.strip()
    if not raw:
        raise ValueError("params.path is required")
    rel = Path(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError("path traversal is not allowed")
    if len(rel.parts) == 1:
        rel = Path("memory") / ".raw_fallbacks" / rel
    if len(rel.parts) != 3 or rel.parts[:2] != ("memory", ".raw_fallbacks"):
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    if rel.suffix.lower() != ".md" or rel.name.startswith("."):
        raise ValueError("params.path must be memory/.raw_fallbacks/*.md")
    return rel.as_posix()


def _raw_fallback_reason(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (IndexError, OSError):
        return None
    prefix = "# Raw flush ("
    suffix = ")"
    if first_line.startswith(prefix) and first_line.endswith(suffix):
        return first_line[len(prefix) : -len(suffix)]
    return None


def _raw_fallback_rows(root: Path) -> list[dict[str, Any]]:
    raw_root = root / "memory" / ".raw_fallbacks"
    if not raw_root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for file_path in sorted(path for path in raw_root.glob("*.md") if path.is_file()):
        stat = file_path.stat()
        rows.append(
            {
                "path": (Path("memory") / ".raw_fallbacks" / file_path.name).as_posix(),
                "sizeBytes": stat.st_size,
                "modifiedAt": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "reason": _raw_fallback_reason(file_path),
            }
        )
    return rows


def _read_memory_content(
    file_path: Path,
    *,
    from_line: int | None,
    lines: int | None,
) -> tuple[str, int, bool]:
    if from_line is None and lines is None:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return (
            content[:_MAX_MEMORY_SHOW_CHARS],
            len(content.splitlines()),
            len(content) > _MAX_MEMORY_SHOW_CHARS,
        )

    start_line = int(from_line or 1)
    max_lines = int(lines) if lines is not None else None
    parts: list[str] = []
    char_count = 0
    selected_line_count = 0
    truncated = False

    with file_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no < start_line:
                continue
            if max_lines is not None and selected_line_count >= max_lines:
                break
            if char_count >= _MAX_MEMORY_SHOW_CHARS:
                truncated = True
                break

            text = line.rstrip("\r\n")
            piece = text if selected_line_count == 0 else f"\n{text}"
            remaining = _MAX_MEMORY_SHOW_CHARS - char_count
            if len(piece) > remaining:
                if remaining > 0:
                    parts.append(piece[:remaining])
                    selected_line_count += 1
                truncated = True
                break

            parts.append(piece)
            char_count += len(piece)
            selected_line_count += 1

    return "".join(parts), selected_line_count, truncated


@_d.method("memory.show", scope="operator.read")
async def _handle_memory_show(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    raw_path = str(params.get("path") or "")
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))

    _validate_memory_path(raw_path)

    from_line = params.get("fromLine")
    if from_line is not None:
        from_line = _int_param(params, "fromLine", 1, minimum=1, maximum=1_000_000)
    lines = params.get("lines")
    if lines is not None:
        lines = _int_param(params, "lines", 1, minimum=1, maximum=_MAX_MEMORY_SHOW_LINES)

    root = _memory_root(manager).resolve()
    file_path = (root / raw_path).resolve()
    try:
        file_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("path traversal is not allowed") from exc
    if not file_path.is_file():
        raise KeyError(f"Memory source not found: {raw_path}")

    if (
        from_line is None
        and lines is None
        and file_path.stat().st_size > _MAX_MEMORY_SHOW_FILE_BYTES
    ):
        raise ValueError("memory source is too large; request a line slice")

    content, selected_line_count, truncated = _read_memory_content(
        file_path,
        from_line=from_line,
        lines=lines,
    )

    return {
        "agentId": agent_id,
        "path": raw_path,
        "fromLine": int(from_line or 1),
        "lineCount": selected_line_count,
        "truncated": truncated,
        "content": content,
    }


@_d.method("memory.raw_fallbacks.list", scope="operator.admin")
async def _handle_raw_fallbacks_list(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id, manager = _require_memory_manager(ctx, (params or {}).get("agentId"))
    rows = _raw_fallback_rows(_memory_root(manager).resolve())
    return {"agentId": agent_id, "count": len(rows), "files": rows}


@_d.method("memory.raw_fallbacks.show", scope="operator.admin")
async def _handle_raw_fallbacks_show(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    raw_path = _raw_fallback_rel_path(str(params.get("path") or ""))
    agent_id, manager = _require_memory_manager(ctx, params.get("agentId"))

    from_line = params.get("fromLine")
    if from_line is not None:
        from_line = _int_param(params, "fromLine", 1, minimum=1, maximum=1_000_000)
    lines = params.get("lines")
    if lines is not None:
        lines = _int_param(params, "lines", 1, minimum=1, maximum=_MAX_MEMORY_SHOW_LINES)

    root = _memory_root(manager).resolve()
    file_path = (root / raw_path).resolve()
    raw_root = (root / "memory" / ".raw_fallbacks").resolve()
    try:
        file_path.relative_to(raw_root)
    except ValueError as exc:
        raise ValueError("path traversal is not allowed") from exc
    if not file_path.is_file():
        raise KeyError(f"Raw fallback not found: {raw_path}")
    if (
        from_line is None
        and lines is None
        and file_path.stat().st_size > _MAX_MEMORY_SHOW_FILE_BYTES
    ):
        raise ValueError("raw fallback is too large; request a line slice")

    content, selected_line_count, truncated = _read_memory_content(
        file_path,
        from_line=from_line,
        lines=lines,
    )
    return {
        "agentId": agent_id,
        "path": raw_path,
        "fromLine": int(from_line or 1),
        "lineCount": selected_line_count,
        "truncated": truncated,
        "content": content,
        "reason": _raw_fallback_reason(file_path),
    }


@_d.method("memory.repair.list", scope="operator.admin")
async def _handle_memory_repair_list(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    limit = _int_param(params, "limit", 50, minimum=1, maximum=_MAX_REPAIR_LIST_LIMIT)
    manager = _require_session_manager(ctx)
    storage = get_session_storage(manager)
    items: list[dict[str, Any]] = []
    has_compaction_selector = any(k in params for k in ("summaryId", "sessionKey", "compactionId"))
    memory_manager = (getattr(ctx, "memory_managers", None) or {}).get(agent_id)
    if memory_manager is not None:
        root = _memory_root(memory_manager).resolve()
        await import_legacy_raw_fallback_receipts(storage, root, agent_id=agent_id)
    if storage is not None and not has_compaction_selector:
        selected = (
            _raw_fallback_rel_path(str(params.get("path") or ""))
            if "path" in params
            else None
        )
        rows = await list_repair_queue(
            storage,
            limit=limit,
            path=selected,
            agent_id=agent_id,
        )
        items = [repair_receipt_to_wire(row) for row in rows[:limit]]
    else:
        rows = await _repair_summaries(manager, agent_id=agent_id, params=params, limit=limit)
        items = [_summary_to_repair_wire(row) for row in rows]
    if storage is None and (not has_compaction_selector or "path" in params):
        memory_manager = (getattr(ctx, "memory_managers", None) or {}).get(agent_id)
        if memory_manager is not None:
            raw_rows = repair_raw_fallback_rows(_memory_root(memory_manager).resolve())
            if "path" in params:
                selected = _raw_fallback_rel_path(str(params.get("path") or ""))
                raw_rows = [row for row in raw_rows if row.get("path") == selected]
            items.extend(raw_rows[: max(0, limit - len(items))])
    return {"agentId": agent_id, "count": len(items), "items": items}


@_d.method("memory.repair.show", scope="operator.admin")
async def _handle_memory_repair_show(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    entry_limit = _int_param(
        params,
        "entryLimit",
        20,
        minimum=1,
        maximum=_MAX_REPAIR_SHOW_ENTRIES,
    )
    if "path" in params:
        _, memory_manager = _require_memory_manager(ctx, agent_id)
        root = _memory_root(memory_manager).resolve()
        rel_raw = _raw_fallback_rel_path(str(params.get("path") or ""))
        rel_path = Path(rel_raw)
        file_path = (root / rel_path).resolve()
        raw_root = (root / "memory" / ".raw_fallbacks").resolve()
        if raw_root not in file_path.parents or not file_path.is_file():
            raise KeyError("Repair record not found")
        rows = repair_raw_fallback_rows(root, include_repaired=True)
        selected = rel_raw
        row = next((item for item in rows if item.get("path") == selected), None)
        if row is None:
            raise KeyError("Repair record not found")
        content = file_path.read_text(encoding="utf-8", errors="replace")
        entries = parse_raw_fallback_entries(content)
        return {
            "agentId": agent_id,
            **row,
            "entryCount": len(entries),
            "entries": [_entry_to_repair_wire(entry) for entry in entries[:entry_limit]],
        }
    manager = _require_session_manager(ctx)
    rows = await _repair_summaries(manager, agent_id=agent_id, params=params, limit=1)
    if not rows:
        raise KeyError("Repair record not found")
    summary = rows[0]
    get_preimage = getattr(manager, "get_compaction_preimage", None)
    if not callable(get_preimage):
        raise RpcUnavailableError("Compaction preimage lookup is not available")
    entries = await get_preimage(summary)
    entry_rows = [_entry_to_repair_wire(entry) for entry in list(entries)[:entry_limit]]
    return {
        "agentId": agent_id,
        **_summary_to_repair_wire(summary),
        **_preimage_metadata(list(entries)),
        "entryCount": len(entries),
        "entries": entry_rows,
    }


@_d.method("memory.repair.run", scope="operator.admin")
async def _handle_memory_repair_run(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    limit = _int_param(params, "limit", 50, minimum=1, maximum=_MAX_REPAIR_LIST_LIMIT)
    manager = _require_session_manager(ctx)
    flush_service = getattr(ctx, "flush_service", None)
    execute = getattr(flush_service, "execute", None)
    if not callable(execute):
        raise RpcUnavailableError("Session flush service is not configured")
    results = await run_memory_repair_once(
        session_manager=manager,
        flush_service=flush_service,
        memory_roots=_repair_memory_roots(ctx),
        agent_id=agent_id,
        limit=limit,
        params=params,
        scan_limit=_REPAIR_SCAN_LIMIT,
        provider_request_correlation_enabled=(
            not provider_request_correlation_disabled(config=ctx.config)
        ),
    )
    return {"agentId": agent_id, "count": len(results), "results": results}
