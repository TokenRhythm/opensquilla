"""Tools for retrieving stored raw tool-result evidence."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from opensquilla.engine.tool_result_query import character_page, log_grep, log_ranges
from opensquilla.engine.tool_result_store import (
    ToolOutputNotReadyError,
    ToolResultRecord,
    ToolResultStore,
)
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, SafeToolError, current_tool_context

_DEFAULT_MAX_CHARS = 12_000
_ABSOLUTE_MAX_CHARS = 500_000
_DEFAULT_CONTEXT_LINES = 2
_MAX_CONTEXT_LINES = 20
_DEFAULT_HEAD_TAIL_LINES = 80
_VALID_MODES = {"metadata", "slice", "grep", "head_tail", "query", "raw_slice"}
_HANDLE_RE = re.compile(r"\btr-[0-9a-f]{32}\b")
_LINE_QUERY_RE = re.compile(
    r"^\s*(?:L|line\s*)?(\d+)"
    r"(?:\s*(?:-|to|\.\.)\s*(?:L|line\s*)?(\d+))?\s*$",
    re.IGNORECASE,
)
_LINE_RANGE_TOKEN_RE = re.compile(
    r"(?:\bL|\bline\s+)(\d+)\s*(?:-|to|\.\.)\s*"
    r"(?:(?:L|line\s+))?(\d+)\b",
    re.IGNORECASE,
)
_LINE_REF_TOKEN_RE = re.compile(r"(?:\bL|\bline\s+)(\d+)\b", re.IGNORECASE)
_MAX_QUERY_LINE_REFS = 200


def _safe_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _store_location() -> tuple[str, str]:
    ctx = current_tool_context.get()
    if ctx is None:
        raise SafeToolError("Tool result retrieval requires an active tool context.")

    store_dir = ctx.tool_result_store_dir
    if not store_dir and ctx.artifact_media_root:
        store_dir = str(Path(ctx.artifact_media_root) / "tool-results")

    session_id = ctx.tool_result_store_session_id or ctx.artifact_session_id
    if not session_id:
        session_id = ctx.session_key

    if not store_dir or not session_id:
        raise SafeToolError(
            "No stored raw tool results are available in this session."
        )
    return store_dir, session_id


def _normalize_handle(handle: str) -> str:
    match = _HANDLE_RE.search(str(handle or ""))
    if match:
        return match.group(0)
    return str(handle or "").strip()


def _continuation_text(continuation: dict[str, Any] | None) -> str:
    if not continuation:
        return ""
    strategy = str(continuation.get("next_call_strategy") or "")
    next_call = continuation.get("next_call")
    return (
        f"continuation.next_call_strategy: {strategy}\n"
        f"continuation.next_call: "
        f"{json.dumps(next_call, ensure_ascii=False, sort_keys=True)}\n"
    )


def _clip(
    text: str,
    *,
    max_chars: int,
    continuation: dict[str, Any] | None = None,
) -> str:
    if len(text) <= max_chars:
        return text
    marker = (
        "\n[retrieve_tool_result truncated: "
        f"returned_chars={max_chars}, original_chars={len(text)}]\n"
        f"{_continuation_text(continuation)}"
    )
    if len(marker) >= max_chars:
        return marker[:max_chars]
    budget = max(0, max_chars - len(marker))
    return text[:budget].rstrip() + marker


def _larger_max_chars(current: int) -> int | None:
    if current >= _ABSOLUTE_MAX_CHARS:
        return None
    return min(_ABSOLUTE_MAX_CHARS, max(current + 1, current * 4))


def _normalize_mode(
    mode: Any,
    *,
    query: str | None,
    pattern: str | None,
    start_line: int | None,
    end_line: int | None,
    offset: int | None,
    limit: int | None,
) -> str:
    selected = str(mode or "metadata").strip().lower()
    if selected not in _VALID_MODES:
        selected = "metadata"
    if selected == "metadata":
        if start_line is not None or end_line is not None:
            return "slice"
        if query:
            return "query"
        if pattern:
            return "grep"
        if offset is not None or limit is not None:
            return "raw_slice"
    if selected == "grep" and not pattern and query:
        return "query"
    if selected == "query" and not query and pattern:
        return "grep"
    return selected


def _same_mode_continuation(
    *,
    handle: str,
    mode: str,
    max_chars: int,
    query: str | None = None,
    pattern: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    context_lines: int | None = None,
) -> dict[str, Any] | None:
    next_max_chars = _larger_max_chars(max_chars)
    if next_max_chars is None:
        return None
    args: dict[str, Any] = {
        "handle": handle,
        "mode": mode,
        "max_chars": next_max_chars,
    }
    optional_args = {
        "query": query,
        "pattern": pattern,
        "start_line": start_line,
        "end_line": end_line,
        "context_lines": context_lines,
    }
    args.update(
        {key: value for key, value in optional_args.items() if value is not None}
    )
    return {
        "available": True,
        "next_call_strategy": "same_query_larger_max_chars",
        "next_call": {
            "name": "retrieve_tool_result",
            "arguments": args,
        },
    }


def _format_lines(lines: list[str], start: int, end: int) -> str:
    width = max(1, len(str(end)))
    return "\n".join(
        f"{line_no:>{width}}| {lines[line_no - 1]}"
        for line_no in range(start, end + 1)
    )


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _metadata(record: ToolResultRecord) -> str:
    line_count = len(record.content.splitlines())
    return json.dumps(
        {
            "type": "tool_result_metadata",
            "handle": record.handle,
            "tool_name": record.tool_name,
            "tool_use_id": record.tool_use_id,
            "chars": record.chars,
            "size_bytes": record.size_bytes,
            "stored_size_bytes": record.stored_size_bytes,
            "storage_encoding": record.storage_encoding,
            "line_count": line_count,
            "sha256": record.sha256,
            "created_at": record.created_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _slice(record: ToolResultRecord, *, start_line: int | None, end_line: int | None) -> str:
    lines = record.content.splitlines()
    if not lines:
        return ""
    start = _safe_int(start_line, default=1, minimum=1, maximum=len(lines))
    end = _safe_int(
        end_line,
        default=min(len(lines), start + 199),
        minimum=start,
        maximum=len(lines),
    )
    return _format_lines(lines, start, end)


def _grep(record: ToolResultRecord, *, pattern: str | None, context_lines: int) -> str:
    if not pattern or not pattern.strip():
        raise SafeToolError("grep mode requires a non-empty pattern.")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise SafeToolError(f"Invalid grep pattern: {exc}") from exc

    lines = record.content.splitlines()
    ranges: list[tuple[int, int]] = []
    for idx, line in enumerate(lines, start=1):
        if regex.search(line):
            ranges.append(
                (
                    max(1, idx - context_lines),
                    min(len(lines), idx + context_lines),
                )
            )
    merged = _merge_ranges(ranges)
    if not merged:
        return f"No matches for pattern: {pattern}"

    chunks: list[str] = []
    for start, end in merged:
        chunks.append(f"[lines {start}-{end}]")
        chunks.append(_format_lines(lines, start, end))
    return "\n".join(chunks)


def _head_tail(record: ToolResultRecord) -> str:
    lines = record.content.splitlines()
    if len(lines) <= _DEFAULT_HEAD_TAIL_LINES * 2:
        return _format_lines(lines, 1, len(lines)) if lines else ""
    head = _format_lines(lines, 1, _DEFAULT_HEAD_TAIL_LINES)
    tail_start = len(lines) - _DEFAULT_HEAD_TAIL_LINES + 1
    tail = _format_lines(lines, tail_start, len(lines))
    omitted = len(lines) - (_DEFAULT_HEAD_TAIL_LINES * 2)
    return f"{head}\n[... omitted {omitted} lines ...]\n{tail}"


def _raw_slice(
    record: ToolResultRecord,
    *,
    offset: int | None,
    limit: int,
) -> str:
    start = _safe_int(
        offset,
        default=0,
        minimum=0,
        maximum=max(0, len(record.content)),
    )
    end = min(len(record.content), start + limit)
    body = record.content[start:end]
    next_offset = end if end < len(record.content) else None
    continuation = None
    if next_offset is not None:
        continuation = {
            "available": True,
            "next_call_strategy": "raw_slice_offset",
            "next_call": {
                "name": "retrieve_tool_result",
                "arguments": {
                    "handle": record.handle,
                    "mode": "raw_slice",
                    "offset": next_offset,
                    "limit": limit,
                },
            },
        }
    preamble = (
        f"offset: {start}\n"
        f"returned_chars: {len(body)}\n"
        f"next_offset: {next_offset if next_offset is not None else ''}\n"
    )
    return preamble + body + ("\n" + _continuation_text(continuation) if continuation else "")


def _query(record: ToolResultRecord, *, query: str | None, context_lines: int) -> str:
    text = (query or "").strip()
    if not text:
        raise SafeToolError("query mode requires a non-empty query.")
    line_match = _LINE_QUERY_RE.match(text)
    if line_match:
        start = int(line_match.group(1))
        end = int(line_match.group(2) or start)
        if end < start:
            start, end = end, start
        return _slice(
            record,
            start_line=max(1, start - context_lines),
            end_line=end + context_lines,
        )
    line_ref_result = _line_ref_query(record, text, context_lines=context_lines)
    if line_ref_result is not None:
        return line_ref_result
    try:
        return _grep(record, pattern=text, context_lines=context_lines)
    except SafeToolError as exc:
        if "Invalid grep pattern" not in exc.user_message:
            raise
        return _grep(record, pattern=re.escape(text), context_lines=context_lines)


def _query_line_refs(query: str) -> list[int]:
    refs: list[int] = []
    seen: set[int] = set()

    def add_ref(number: int) -> None:
        if number <= 0 or number in seen or len(refs) >= _MAX_QUERY_LINE_REFS:
            return
        seen.add(number)
        refs.append(number)

    for raw_start, raw_end in _LINE_RANGE_TOKEN_RE.findall(query):
        try:
            start = int(raw_start)
            end = int(raw_end)
        except ValueError:
            continue
        if end < start:
            start, end = end, start
        for number in range(start, end + 1):
            add_ref(number)
            if len(refs) >= _MAX_QUERY_LINE_REFS:
                break

    for raw_number in _LINE_REF_TOKEN_RE.findall(query):
        try:
            add_ref(int(raw_number))
        except ValueError:
            continue

    return refs


def _line_ref_query(
    record: ToolResultRecord,
    query: str,
    *,
    context_lines: int,
) -> str | None:
    lines = record.content.splitlines()
    if not lines:
        return ""
    refs = _query_line_refs(query)

    if not refs:
        return None

    ranges = _merge_ranges(
        [
            (
                max(1, number - context_lines),
                min(len(lines), number + context_lines),
            )
            for number in refs
            if number <= len(lines)
        ]
    )
    if not ranges:
        return f"No matching in-range line references for query: {query}"

    chunks: list[str] = []
    for start, end in ranges:
        chunks.append(f"[lines {start}-{end}]")
        chunks.append(_format_lines(lines, start, end))
    return "\n".join(chunks)


@tool(
    name="retrieve_tool_result",
    description=(
        "Retrieve omitted raw output from a tool_result_projection or "
        "aggregate_tool_result_compacted handle, or an execution_log_handle. "
        "Use this before acting on a "
        "projected result when exact diagnostics, source snippets, line ranges, "
        "or validation output may be needed."
    ),
    params={
        "handle": {
            "type": "string",
            "description": (
                "Stored result handle from tool_result_projection or execution_log_handle."
            ),
        },
        "mode": {
            "type": "string",
            "enum": sorted(_VALID_MODES),
            "description": (
                "Retrieval mode. Prefer query for search_hints, failing test names, "
                "paths, error phrases, L<num>, or line ranges. Use raw_slice with "
                "offset/limit only when focused query retrieval is insufficient."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Focused query such as L12, L12-L30, a failing test name, path, "
                "error phrase, or grep-style text."
            ),
        },
        "start_line": {
            "type": "integer",
            "description": "1-based inclusive start line for slice mode.",
        },
        "end_line": {
            "type": "integer",
            "description": "1-based inclusive end line for slice mode.",
        },
        "pattern": {
            "type": "string",
            "description": "Regex pattern for grep mode.",
        },
        "context_lines": {
            "type": "integer",
            "description": "Context lines around grep matches.",
        },
        "max_chars": {
            "type": "integer",
            "description": "Maximum characters to return.",
        },
        "offset": {
            "type": "integer",
            "description": "Character offset for raw_slice mode.",
        },
        "limit": {
            "type": "integer",
            "description": (
                "Character count for raw_slice mode; also accepted as a max_chars alias."
            ),
        },
    },
    required=["handle"],
    default_access="deny",
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="code",
)
async def retrieve_tool_result(
    handle: str,
    mode: str = "metadata",
    query: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    pattern: str | None = None,
    context_lines: int | None = None,
    max_chars: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    store_dir, session_id = _store_location()
    return await asyncio.to_thread(
        query_stored_tool_result, store_dir, session_id, handle,
        mode=mode, query=query, start_line=start_line, end_line=end_line,
        pattern=pattern, context_lines=context_lines, max_chars=max_chars,
        offset=offset, limit=limit,
    )


def query_stored_tool_result(
    store_dir: str | Path,
    session_id: str,
    handle: str,
    *,
    mode: str = "metadata",
    query: str | None = None,
    start_line: int | None = None,
    end_line: int | None = None,
    pattern: str | None = None,
    context_lines: int | None = None,
    max_chars: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Query a session-owned stored result without depending on tool context."""
    selected_mode = _normalize_mode(
        mode,
        query=query,
        pattern=pattern,
        start_line=start_line,
        end_line=end_line,
        offset=offset,
        limit=limit,
    )
    normalized_query = query
    normalized_pattern = pattern
    if selected_mode == "query" and normalized_query is None:
        normalized_query = pattern
    if selected_mode == "grep" and normalized_pattern is None:
        normalized_pattern = query
    raw_slice_limit = _safe_int(
        limit,
        default=_DEFAULT_MAX_CHARS,
        minimum=1,
        maximum=_ABSOLUTE_MAX_CHARS,
    )
    char_limit_input = max_chars
    if selected_mode != "raw_slice" and char_limit_input is None:
        char_limit_input = limit
    char_limit = _safe_int(
        char_limit_input,
        default=_DEFAULT_MAX_CHARS,
        minimum=1,
        maximum=_ABSOLUTE_MAX_CHARS,
    )
    if selected_mode == "raw_slice" and max_chars is None:
        char_limit = min(_ABSOLUTE_MAX_CHARS, max(char_limit, raw_slice_limit + 2000))
    ctx_lines = _safe_int(
        context_lines,
        default=_DEFAULT_CONTEXT_LINES,
        minimum=0,
        maximum=_MAX_CONTEXT_LINES,
    )
    store = ToolResultStore(store_dir)
    handle = _normalize_handle(handle)
    try:
        output_metadata = store.read_output_metadata(handle, session_id=session_id)
        if output_metadata is None:
            record = store.read(handle, session_id=session_id)
        else:
            return _query_execution_log(
                store, session_id, output_metadata, mode=selected_mode,
                query=normalized_query, pattern=normalized_pattern,
                start_line=start_line, end_line=end_line, context_lines=ctx_lines,
                char_limit=char_limit, offset=offset, raw_slice_limit=raw_slice_limit,
            )
    except ToolOutputNotReadyError as exc:
        raise SafeToolError(
            "Execution log is still being saved. Retry after the command finishes."
        ) from exc
    except (OSError, ValueError, KeyError) as exc:
        raise SafeToolError(
            "Stored tool result was not found for the current session."
        ) from exc

    if selected_mode == "metadata":
        body = _metadata(record)
    elif selected_mode == "slice":
        body = _slice(record, start_line=start_line, end_line=end_line)
    elif selected_mode == "grep":
        body = _grep(record, pattern=normalized_pattern, context_lines=ctx_lines)
    elif selected_mode == "query":
        body = _query(record, query=normalized_query, context_lines=ctx_lines)
    elif selected_mode == "raw_slice":
        body = _raw_slice(record, offset=offset, limit=raw_slice_limit)
    else:
        body = _head_tail(record)

    continuation = (
        None
        if selected_mode == "raw_slice"
        else _same_mode_continuation(
            handle=record.handle,
            mode=selected_mode,
            max_chars=char_limit,
            query=normalized_query,
            pattern=normalized_pattern,
            start_line=start_line,
            end_line=end_line,
            context_lines=context_lines,
        )
    )
    header_prefix = (
        "[tool_result_retrieval]\n"
        f"handle: {record.handle}\n"
        f"tool_name: {record.tool_name}\n"
        f"mode: {selected_mode}\n"
        f"original_chars: {record.chars}\n"
    )
    complete_probe = header_prefix + "returned_content_is_complete: true\n---\n" + body
    header = header_prefix + (
        f"returned_content_is_complete: {str(len(complete_probe) <= char_limit).lower()}\n"
        "---\n"
    )
    return _clip(header + body, max_chars=char_limit, continuation=continuation)


def read_stored_tool_result_page(
    store_dir: str | Path,
    session_id: str,
    handle: str,
    *,
    offset: int = 0,
    limit: int = _DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    """Read one character page of an execution log without loading the full log."""
    store = ToolResultStore(store_dir)
    handle = _normalize_handle(handle)
    try:
        metadata = store.read_output_metadata(handle, session_id=session_id)
        if metadata is None:
            raise ValueError("not an execution log")
        total = int(metadata["chars"])
        start = _safe_int(offset, default=0, minimum=0, maximum=total)
        count = _safe_int(limit, default=_DEFAULT_MAX_CHARS, minimum=1,
                          maximum=_ABSOLUTE_MAX_CHARS)
        body = character_page(
            store.iter_text_chunks(handle, session_id=session_id), start, count,
        )
    except ToolOutputNotReadyError:
        raise
    except (OSError, ValueError, KeyError) as exc:
        raise SafeToolError("Stored execution log was not found for the current session.") from exc
    end = start + len(body)
    return {
        "storage_kind": "execution_log",
        "handle": handle,
        "offset": start,
        "content": body,
        "returned_chars": len(body),
        "next_offset": end if end < total else None,
        "chars": total,
        "complete": bool(metadata.get("complete", False)),
    }


def _query_execution_log(
    store: ToolResultStore, session_id: str, metadata: dict[str, Any], *,
    mode: str, query: str | None, pattern: str | None, start_line: int | None,
    end_line: int | None, context_lines: int, char_limit: int, offset: int | None,
    raw_slice_limit: int,
) -> str:
    handle = str(metadata["handle"])
    line_count = int(metadata["line_count"])
    prefix = (
        "[tool_result_retrieval]\n"
        f"handle: {handle}\ntool_name: {metadata.get('tool_name', '')}\nmode: {mode}\n"
        f"original_chars: {metadata['chars']}\n"
        f"stored_log_is_complete: {str(bool(metadata.get('complete', False))).lower()}\n"
    )
    continuation = None
    incomplete = False
    if mode == "metadata":
        body = json.dumps({"type": "tool_result_metadata", **metadata},
                          ensure_ascii=False, sort_keys=True, indent=2)
    elif mode == "raw_slice":
        return _execution_log_raw_slice(
            store, session_id, handle, prefix=prefix, total=int(metadata["chars"]),
            offset=offset, limit=raw_slice_limit, max_chars=char_limit,
        )
    else:
        ranges: list[tuple[int, int]] | None = None
        labels = False
        if mode == "slice":
            start = _safe_int(start_line, default=1, minimum=1, maximum=max(1, line_count))
            end = _safe_int(end_line, default=start + 199, minimum=start,
                            maximum=max(start, line_count))
            ranges = [(start, end)] if line_count else []
        elif mode == "head_tail":
            if line_count <= _DEFAULT_HEAD_TAIL_LINES * 2:
                ranges = [(1, line_count)] if line_count else []
            else:
                ranges = [(1, _DEFAULT_HEAD_TAIL_LINES),
                          (line_count - _DEFAULT_HEAD_TAIL_LINES + 1, line_count)]
        elif mode == "query":
            text = (query or "").strip()
            if not text:
                raise SafeToolError("query mode requires a non-empty query.")
            match = _LINE_QUERY_RE.match(text)
            if match:
                first, last = sorted((int(match.group(1)), int(match.group(2) or match.group(1))))
                start = min(max(1, line_count), max(1, first - context_lines))
                end = min(max(1, line_count), max(start, last + context_lines))
                ranges = [(start, end)] if line_count else []
            else:
                refs = _query_line_refs(text)
                if refs:
                    ranges = _merge_ranges([
                        (max(1, ref - context_lines), min(line_count, ref + context_lines))
                        for ref in refs if ref <= line_count
                    ])
                    labels = True
                else:
                    pattern = text
                    try:
                        re.compile(pattern)
                    except re.error:
                        pattern = re.escape(text)
        if ranges is not None:
            body, incomplete = log_ranges(
                store, session_id, handle, ranges, max_chars=char_limit, labels=labels,
                omission_notice=mode == "head_tail",
            )
            if labels and not ranges:
                body = f"No matching in-range line references for query: {query}"
        else:
            if not pattern or not pattern.strip():
                raise SafeToolError("grep mode requires a non-empty pattern.")
            try:
                body, incomplete = log_grep(
                    store, session_id, handle, pattern=pattern, context_lines=context_lines,
                    max_chars=char_limit, line_count=line_count,
                )
            except re.error as exc:
                raise SafeToolError(f"Invalid grep pattern: {exc}") from exc
        continuation = _same_mode_continuation(
            handle=handle, mode=mode, max_chars=char_limit, query=query, pattern=pattern,
            start_line=start_line, end_line=end_line, context_lines=context_lines,
        )
    probe = prefix + "returned_content_is_complete: true\n---\n" + body
    complete = not incomplete and len(probe) <= char_limit
    result = prefix + f"returned_content_is_complete: {str(complete).lower()}\n---\n" + body
    return _clip(result, max_chars=char_limit, continuation=continuation)


def _execution_log_raw_slice(
    store: ToolResultStore, session_id: str, handle: str, *, prefix: str,
    total: int, offset: int | None, limit: int, max_chars: int,
) -> str:
    start = _safe_int(offset, default=0, minimum=0, maximum=total)
    requested = min(limit, total - start)
    content = character_page(
        store.iter_text_chunks(handle, session_id=session_id), start,
        min(requested, max_chars),
    ) if requested else ""
    # Include the header and continuation in the response budget. Clipping
    # after computing next_offset would permanently skip undisplayed text.
    while True:
        end = start + len(content)
        next_offset = end if end < total else None
        continuation = None
        if next_offset is not None:
            continuation = {
                "available": True,
                "next_call_strategy": "raw_slice_offset",
                "next_call": {"name": "retrieve_tool_result", "arguments": {
                    "handle": handle, "mode": "raw_slice", "offset": next_offset,
                    "limit": limit, "max_chars": max_chars,
                }},
            }
        complete = len(content) == requested
        response = (
            prefix + f"returned_content_is_complete: {str(complete).lower()}\n---\n"
            f"offset: {start}\nreturned_chars: {len(content)}\n"
            f"next_offset: {next_offset if next_offset is not None else ''}\n"
            + content
            + ("\n" + _continuation_text(continuation) if continuation else "")
        )
        excess = len(response) - max_chars
        if excess <= 0 and (content or not requested):
            return response
        if excess >= len(content) or not content:
            raise SafeToolError(
                "max_chars is too small to return the execution-log page and its "
                "continuation. Increase max_chars and retry the same offset."
            )
        content = content[:-excess]
