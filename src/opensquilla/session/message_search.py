"""Bounded, resumable literal search of one session's visible message bodies.

No search index or transcript writes are involved. A continuation freezes the
upper entry boundary and retains an offset within a message when its matches
fill a page. Empty pages still advance through nonmatching history.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import nullcontext
from typing import Any, Protocol

from opensquilla.chat.flattened_tool_markers import (
    has_flattened_used_tool_line,
    is_flattened_tool_result_dump,
)
from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.content_reader import ContentExportLimitError
from opensquilla.history_cursor import HistoryCursorInvalidatedError
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.recovery_reads import current_read_budget, recovery_read_scope
from opensquilla.session.storage import CANONICAL_FORK_PROOF_SCHEMA_VERSION, StorageBusyError

MAX_SCAN_ENTRIES = 32
TARGET_SCAN_BYTES = 512 * 1024
MAX_COVERAGE_ROWS = 256
MAX_COVERAGE_QUERIES = 16
TARGET_PAGE_SECONDS = 0.1
MATCH_SCAN_CHARS = 8 * 1024
# Search is optional bulk work. One process-wide lease leaves the other bulk
# readers available for history recovery/hydration, including across sockets.
_ACTIVE_SEARCHES: set[object] = set()
_RETIRING_SEARCHES: set[asyncio.Task[None]] = set()


class SearchStore(Protocol):
    async def read_message_search_rows(
        self, session_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]: ...
    async def read_message_search_body(
        self, session_id: str, row: Mapping[str, Any]
    ) -> TranscriptEntry: ...
    async def read_message_search_coverage_identity(self, session_id: str) -> dict[str, Any]: ...
    async def read_message_search_coverage_summaries(
        self, session_id: str, *, after: int, through: int, limit: int,
    ) -> list[dict[str, Any]]: ...
    async def read_message_search_coverage_archives(
        self, session_id: str, *, after: int, through: int, limit: int,
        compaction_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


def _position(row: Mapping[str, Any]) -> tuple[int, int]:
    return int(row["created_at"]), int(row["entry_id"])


def _decode_position(value: object, *, optional: bool = False) -> tuple[int, int] | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(part) is not int or part < 0 or part > 2**63 - 1 for part in value)
    ):
        raise ValueError("Invalid search continuation position")
    return value[0], value[1]


def _encode(state: Mapping[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode(cursor: str, identity: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if len(cursor) > 4096:
            raise ValueError("Search continuation exceeds the limit")
        state = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(state, dict) or any(
            state.get(key) != value for key, value in identity.items()
        ):
            raise ValueError("Search continuation belongs to a different session or query")
        _decode_position(state.get("upper"), optional=True)
        _decode_position(state.get("after"), optional=True)
        if state.get("resume") is not None:
            resume = state["resume"]
            if not isinstance(resume, dict):
                raise ValueError("Invalid message continuation")
            _decode_position(resume.get("position"))
            for field in ("offset", "revision"):
                if type(resume.get(field)) is not int or resume[field] < 0:
                    raise ValueError("Invalid message continuation")
        if type(state.get("unavailable")) is not int or state["unavailable"] < 0:
            raise ValueError("Invalid search coverage")
        if type(state.get("coverage")) is not bool:
            raise ValueError("Invalid search coverage")
        proof = state.get("proof")
        if not isinstance(proof, dict) or proof.get("phase") not in {
            "summaries", "summary_archive", "archives", "done",
        }:
            raise ValueError("Invalid search coverage continuation")
        if not isinstance(proof.get("identity"), dict) or type(proof.get("valid")) is not bool:
            raise ValueError("Invalid search coverage continuation")
        proof_identity = proof["identity"]
        if proof_identity:
            for field in ("compaction_count", "forked_from_parent", "schema_version",
                          "summary_upper", "archive_upper"):
                if (type(proof_identity.get(field)) is not int
                        or not 0 <= proof_identity[field] <= 2**63 - 1):
                    raise ValueError("Invalid search coverage identity")
        elif proof["phase"] != "done":
            raise ValueError("Missing search coverage identity")
        for field in ("summary_after", "archive_after", "summary_count", "removed", "archived"):
            if type(proof.get(field)) is not int or not 0 <= proof[field] <= 2**63 - 1:
                raise ValueError("Invalid search coverage continuation")
        if type(state.get("body_complete")) is not bool:
            raise ValueError("Invalid search continuation")
        if state["upper"] is None and not state["body_complete"]:
            raise ValueError("Invalid search continuation boundary")
        if proof["phase"] == "summary_archive":
            pending = proof.get("pending")
            if (not isinstance(pending, dict) or not isinstance(pending.get("cid"), str)
                    or not 1 <= len(pending["cid"]) <= 256
                    or any(type(pending.get(field)) is not int
                           or not 0 <= pending[field] <= 2**63 - 1
                           for field in ("expected", "count"))):
                raise ValueError("Invalid archive coverage continuation")
        return state
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("Invalid search continuation") from exc


def _project_text(
    entry: TranscriptEntry,
    previous: TranscriptEntry | None = None,
    following: TranscriptEntry | None = None,
) -> str:
    messages = transcript_entries_to_chat_messages(
        [entry],
        previous_entry=previous,
        next_entry=following,
    )
    if not messages or messages[0].get("role") not in {"user", "assistant"}:
        return ""
    return str(messages[0].get("text") or "")


def _entry_bytes(entry: TranscriptEntry) -> int:
    # Charge loaded prose/metadata, not the possibly empty display projection.
    return len((entry.content or "").encode("utf-8")) + (
        len(json.dumps(entry.tool_calls).encode("utf-8")) if entry.tool_calls else 0
    )


async def _worker(function: Any, *args: Any) -> Any:
    budget = current_read_budget()
    if budget is not None:
        budget.check()
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    # Cancellation ends the caller's wait; the budget retains ownership until
    # the bounded CPU job actually settles, as it does for SQLite reads.
    return await budget.wait_for(task) if budget is not None else await task


async def _display_text(
    storage: SearchStore,
    session_id: str,
    row: Mapping[str, Any],
    entry: TranscriptEntry,
) -> tuple[str, int]:
    previous = following = None
    content = entry.content or ""
    if (entry.role == "assistant" and not entry.tool_calls and "[Used tool:" in content
            and await _worker(has_flattened_used_tool_line, content)):
        neighbors = await storage.read_message_search_rows(
            session_id,
            after=_position(row),
            limit=1,
            visible_only=False,
        )
        if neighbors:
            following = await storage.read_message_search_body(session_id, neighbors[0])
    elif (entry.role == "user" and not entry.tool_call_id and "[Tool result (" in content
            and await _worker(is_flattened_tool_result_dump, content)):
        neighbors = await storage.read_message_search_rows(
            session_id,
            before=_position(row),
            descending=True,
            limit=1,
            visible_only=False,
        )
        if neighbors:
            previous = await storage.read_message_search_body(session_id, neighbors[0])
    return await _worker(_project_and_measure, entry, previous, following)


def _project_and_measure(
    entry: TranscriptEntry, previous: TranscriptEntry | None, following: TranscriptEntry | None,
) -> tuple[str, int]:
    return _project_text(entry, previous, following), sum(
        _entry_bytes(item) for item in (entry, previous, following) if item is not None
    )


async def _advance_coverage(
    storage: SearchStore, session_id: str, proof: dict[str, Any],
) -> bool:
    """Prove raw archive coverage incrementally, with bounded metadata reads.

    Unknown coverage is false until the final page. Missing proof remains false.
    No COUNT, transcript body reads, or transaction survives across requests.
    """
    identity = proof["identity"]
    remaining = MAX_COVERAGE_ROWS
    for _ in range(MAX_COVERAGE_QUERIES):
        if proof["phase"] == "done":
            return bool(proof["valid"])
        if remaining <= 0:
            return False
        if proof["phase"] == "summaries":
            rows = await storage.read_message_search_coverage_summaries(
                session_id, after=proof["summary_after"],
                through=identity["summary_upper"], limit=1,
            )
            if not rows:
                proof["phase"] = "archives"
                proof["archive_after"] = 0
                continue
            remaining -= 1
            row = rows[0]
            proof["summary_after"] = row["id"]
            proof["summary_count"] += 1
            proof["removed"] += max(0, row["removed_count"])
            cid = row["compaction_id"]
            if (not isinstance(cid, str) or not cid or len(cid) > 256
                    or row["removed_count"] < 0
                    or (row["removed_count"] == 0 and row["covered_through_id"] > 0)):
                proof["valid"] = False
                proof["phase"] = "done"
                return False
            proof["pending"] = {"cid": cid, "expected": row["removed_count"], "count": 0}
            proof["archive_after"] = 0
            proof["phase"] = "summary_archive"
        elif proof["phase"] in {"summary_archive", "archives"}:
            pending = proof.get("pending") if proof["phase"] == "summary_archive" else None
            rows = await storage.read_message_search_coverage_archives(
                session_id, after=proof["archive_after"],
                through=identity["archive_upper"], limit=remaining,
                compaction_id=pending["cid"] if pending else None,
            )
            finished = len(rows) < remaining
            remaining -= len(rows)
            if rows:
                proof["archive_after"] = rows[-1]["id"]
            if pending:
                pending["count"] += len(rows)
                if pending["count"] > pending["expected"] or (
                    finished and pending["count"] != pending["expected"]
                ):
                    proof["valid"] = False
                    proof["phase"] = "done"
                    return False
                if finished:
                    proof.pop("pending")
                    proof["phase"] = "summaries"
            else:
                proof["archived"] += len(rows)
                if (proof["archived"] > proof["removed"]
                        or any(row["original_entry_id"] is None for row in rows)):
                    proof["valid"] = False
                    proof["phase"] = "done"
                    return False
                if finished:
                    expected = max(0, identity["compaction_count"])
                    enough = (proof["summary_count"] >= expected if identity["forked_from_parent"]
                              else proof["summary_count"] == expected)
                    unchanged = (
                        await storage.read_message_search_coverage_identity(session_id) == identity
                    )
                    proof["valid"] = bool(
                        proof["valid"] and enough and proof["removed"] == proof["archived"]
                        and unchanged
                    )
                    proof["phase"] = "done"
                    return proof["valid"]
    return False


async def _retire_search(lease: object, budget: Any) -> None:
    try:
        await budget.drain()
    finally:
        _ACTIVE_SEARCHES.discard(lease)


async def search_session_messages(
    storage: SearchStore, *, key: str, session_id: str, session_epoch: int,
    query: str, cursor: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    if _ACTIVE_SEARCHES:
        raise StorageBusyError(
            "message_search", waited_ms=0, retry_after_ms=150,
            stage="permit", resource="message_search",
        )
    lease = object()
    _ACTIVE_SEARCHES.add(lease)
    outer_budget = current_read_budget()
    scope = (nullcontext(outer_budget) if outer_budget is not None else recovery_read_scope(
        f"message-search:{uuid.uuid4().hex}", deadline=time.monotonic() + 7,
    ))
    with scope as budget:
        try:
            return await _search_session_messages(
                storage, key=key, session_id=session_id, session_epoch=session_epoch,
                query=query, cursor=cursor, limit=limit,
            )
        finally:
            if budget.has_pending_work:
                # Cancelling an await cannot stop a Python worker. Retain the
                # global permit until SQLite cleanup and CPU work really end.
                budget.cancel_token.cancel()
                task = asyncio.create_task(_retire_search(lease, budget))
                _RETIRING_SEARCHES.add(task)
                task.add_done_callback(_RETIRING_SEARCHES.discard)
            else:
                _ACTIVE_SEARCHES.discard(lease)


def _literal_spans(text: str, query: str, offset: int) -> Iterator[tuple[int, int]]:
    """Keep Python IGNORECASE semantics without one unbounded regex C call."""
    budget = current_read_budget()
    ascii_query = query.lower() if query.isascii() else None
    pattern = None
    cursor = offset
    while cursor < len(text):
        if budget is not None:
            budget.check()
        candidate_end = min(len(text), cursor + MATCH_SCAN_CHARS)
        window_end = min(len(text), candidate_end + len(query) - 1)
        window = text[cursor:window_end]
        consumed = candidate_end
        if ascii_query is not None and window.isascii():
            # Both must be ASCII: Unicode IGNORECASE also equates i/s/k with
            # non-ASCII characters which a simple lower() would miss.
            lowered = window.lower()
            start = lowered.find(ascii_query)
            while start >= 0 and cursor + start < candidate_end:
                end = cursor + start + len(query)
                consumed = max(consumed, end)
                yield cursor + start, end
                start = lowered.find(ascii_query, start + len(query))
        else:
            if pattern is None:
                pattern = re.compile(re.escape(query), re.IGNORECASE)
            for match in pattern.finditer(window):
                if cursor + match.start() >= candidate_end:
                    break
                end = cursor + match.end()
                consumed = max(consumed, end)
                yield cursor + match.start(), end
        # A match that crossed the boundary owns its suffix. Do not emit
        # overlapping occurrences when the next window resumes.
        cursor = consumed
        if budget is not None:
            budget.check()
        # to_thread alone cannot make _sre release the GIL. Yield after each
        # bounded C operation so chat controls and cancellation can execute.
        time.sleep(0)


def _matches(
    text: str, query: str, offset: int, maximum: int
) -> tuple[list[dict[str, Any]], int | None]:
    out: list[dict[str, Any]] = []
    utf16_cursor, codepoint_cursor = len(text[:offset].encode("utf-16-le")) // 2, offset
    for match_start, match_end in _literal_spans(text, query, offset):
        if len(out) >= maximum:
            # Resume at the next not-yet-emitted occurrence, not after it.
            return out, match_start
        utf16_cursor += len(text[codepoint_cursor:match_start].encode("utf-16-le")) // 2
        length = len(text[match_start:match_end].encode("utf-16-le")) // 2
        start, end = max(0, match_start - 60), min(len(text), match_end + 90)
        out.append(
            {
                "offset_utf16": utf16_cursor,
                "length_utf16": length,
                "snippet": ("…" if start else "")
                + text[start:end]
                + ("…" if end < len(text) else ""),
            }
        )
        utf16_cursor += length
        codepoint_cursor = match_end
    return out, None


async def _search_session_messages(
    storage: SearchStore,
    *,
    key: str,
    session_id: str,
    session_epoch: int,
    query: str,
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if not isinstance(query, str) or not query or len(query) > 256:
        raise ValueError("query must contain 1 to 256 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    identity = {
        "v": 3,
        # Contract-valid Unicode keys/IDs can expand beyond the 4 KiB cursor
        # ceiling when JSON escaped. Fixed-size digests retain the binding
        # without making a valid session's next page impossible to request.
        "key": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "sid": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
        "epoch": session_epoch,
        "query": hashlib.sha256(query.encode("utf-8")).hexdigest(),
    }
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor:
            raise ValueError("cursor must be a nonempty opaque search continuation")
        state = _decode(cursor, identity)
    else:
        newest = await storage.read_message_search_rows(session_id, descending=True, limit=1)
        proof_identity = await storage.read_message_search_coverage_identity(session_id)
        fork_proven = bool(proof_identity) and (
            not proof_identity["forked_from_parent"]
            or proof_identity["schema_version"] >= CANONICAL_FORK_PROOF_SCHEMA_VERSION
        )
        proof = {
            "identity": proof_identity, "phase": "summaries" if fork_proven else "done",
            "valid": fork_proven, "summary_after": 0, "archive_after": 0,
            "summary_count": 0, "removed": 0, "archived": 0,
        }
        if (fork_proven and not proof_identity["summary_upper"]
                and not proof_identity["archive_upper"]):
            proof["phase"] = "done"
            proof["valid"] = proof_identity["compaction_count"] == 0
        state = {
            **identity,
            "upper": list(_position(newest[0])) if newest else None,
            "after": None,
            "resume": None,
            "coverage": False,
            "proof": proof,
            "body_complete": newest == [],
            "unavailable": 0,
        }
    page_started = time.monotonic()
    state["coverage"] = await _advance_coverage(storage, session_id, state["proof"])
    base: dict[str, Any] = {
        "session_id": session_id,
        "session_epoch": session_epoch,
        "query": query,
        "matches": [],
        "next_cursor": None,
        "complete": True,
        "coverage_complete": bool(state["coverage"]) and not state["unavailable"],
        "scanned_entries": 0,
        "unavailable_entries": 0,
    }
    if state["body_complete"]:
        base["complete"] = state["proof"]["phase"] == "done"
        if not base["complete"]:
            base["next_cursor"] = _encode(state)
        return base
    upper = _decode_position(state["upper"])
    after = _decode_position(state.get("after"), optional=True)
    rows = await storage.read_message_search_rows(
        session_id,
        after=after,
        through=upper,
        limit=MAX_SCAN_ENTRIES + 1,
    )
    if state.get("resume"):
        resume = state["resume"]
        if (
            not rows
            or list(_position(rows[0])) != resume["position"]
            or int(rows[0]["content_revision"]) != resume["revision"]
        ):
            raise HistoryCursorInvalidatedError("The search message changed; restart search")
    scanned_bytes = 0
    has_more = False
    for index, row in enumerate(rows):
        if index >= MAX_SCAN_ENTRIES or (index and (
            scanned_bytes >= TARGET_SCAN_BYTES
            or time.monotonic() - page_started >= TARGET_PAGE_SECONDS
        )):
            has_more = True
            break
        budget = current_read_budget()
        if budget is not None:
            budget.check()
        position = _position(row)
        try:
            entry = await storage.read_message_search_body(session_id, row)
            text, loaded_bytes = await _display_text(storage, session_id, row, entry)
        except ContentExportLimitError:
            base["unavailable_entries"] += 1
            state["unavailable"] += 1
            text = ""
            # Oversized unavailable messages still consume this scan's byte
            # allowance instead of probing 64 large bodies in one request.
            loaded_bytes = TARGET_SCAN_BYTES
        base["scanned_entries"] += 1
        scanned_bytes += loaded_bytes
        resume = state.get("resume")
        offset = resume["offset"] if resume else 0
        if offset > len(text):
            raise HistoryCursorInvalidatedError("The search message changed; restart search")
        matches, next_offset = await _worker(
            _matches,
            text,
            query,
            offset,
            limit - len(base["matches"]),
        )
        for match in matches:
            base["matches"].append(
                {
                    "message_id": row["message_id"],
                    "entry_cursor": f"{position[0]}|{position[1]}",
                    "content_revision": int(row["content_revision"]),
                    **match,
                }
            )
        if next_offset is not None:
            state["resume"] = {
                "position": list(position),
                "offset": next_offset,
                "revision": int(row["content_revision"]),
            }
            has_more = True
            break
        state["resume"] = None
        state["after"] = list(position)
        if len(base["matches"]) >= limit:
            has_more = index + 1 < len(rows) or position < upper
            break
        await asyncio.sleep(0)
    state["body_complete"] = not has_more
    base["complete"] = not has_more and state["proof"]["phase"] == "done"
    base["coverage_complete"] = bool(state["coverage"]) and not state["unavailable"]
    if not base["complete"]:
        base["next_cursor"] = _encode(state)
    return base
