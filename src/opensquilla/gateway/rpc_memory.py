"""RPC handlers for read-only memory inspection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.memory.types import (
    DEFAULT_MEMORY_SEARCH_MIN_SCORE,
    DEFAULT_MEMORY_SEARCH_RESULTS,
    MemorySearchOpts,
    SearchIntent,
    normalize_memory_search_min_score,
    normalize_memory_source_filter,
)
from opensquilla.session.keys import normalize_agent_id
from opensquilla.tools.builtin.memory_tools import _is_memory_source_path

_d = get_dispatcher()

_MAX_MEMORY_SHOW_CHARS = 8000
_MAX_MEMORY_SHOW_LINES = 500
_MAX_MEMORY_SHOW_FILE_BYTES = 1024 * 1024


def _require_memory_manager(ctx: RpcContext, agent_id: str | None) -> tuple[str, Any]:
    managers = getattr(ctx, "memory_managers", None) or {}
    if not managers:
        raise RpcUnavailableError("No memory managers configured")
    resolved_agent = normalize_agent_id(agent_id or "main")
    manager = managers.get(resolved_agent)
    if manager is None:
        raise KeyError(f"Memory manager not found for agent: {resolved_agent}")
    return resolved_agent, manager


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
