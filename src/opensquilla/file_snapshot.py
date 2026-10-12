"""Immutable display content captured by successful file mutations."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.tools.types import ToolContext

# Match compact tool arguments so many moderate files cannot inflate a turn's
# history envelope. Larger bodies are fetched only when their details open.
_INLINE_BYTE_LIMIT = 4 * 1024
# Display capture must remain bounded independently of the file operation.
MAX_EDIT_SNAPSHOT_INPUT_BYTES = 16 * 1024 * 1024
MAX_EDIT_SNAPSHOT_LINES = 100_000
MAX_EDIT_SNAPSHOT_BYTES = 8 * 1024 * 1024
# SequenceMatcher can take quadratic time even on small, reordered files.
# Apply this budget to the differing region, keeping ordinary local edits cheap.
MAX_EDIT_SNAPSHOT_LINE_PAIRS = 1_000_000
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@(.*)$")
# JSON may escape each UTF-8 byte into six ASCII bytes; leave room for metadata.
MAX_FILESYSTEM_REPLY_BYTES = 64 * 1024 * 1024
log = logging.getLogger(__name__)


async def retain_file_snapshot(
    snapshot: dict[str, Any],
    *,
    context: ToolContext | None,
) -> dict[str, Any]:
    """Keep up to 4 KiB inline and durably reference larger display content.

    This runs before the public result event. Failure to save display history
    must not turn a successful filesystem mutation into a failed operation.
    References and unavailable content never carry a misleading partial body.
    """

    content = snapshot["content"]
    payload = await asyncio.to_thread(content.encode, "utf-8")
    if len(payload) <= _INLINE_BYTE_LIMIT:
        return dict(snapshot)

    retained = {
        "kind": snapshot["kind"],
        "path": snapshot["path"],
        "content": "",
        "bytes": len(payload),
    }
    media_root = getattr(context, "artifact_media_root", None)
    session_id = getattr(context, "artifact_session_id", None)
    if not media_root or not session_id:
        log.warning("file_snapshot.persistence_unavailable: reason=missing_context bytes=%d",
                    len(payload))
        return {**retained, "content_unavailable": True}

    try:
        from opensquilla.attachment_refs import write_transcript_material

        attachments = getattr(getattr(context, "sandbox_gateway_config", None), "attachments", None)
        budget = getattr(attachments, "transcript_disk_budget_bytes", None)
        sha, _path, _written = await asyncio.to_thread(
            write_transcript_material,
            media_root=Path(media_root),
            session_id=session_id,
            payload=payload,
            disk_budget_bytes=budget if isinstance(budget, int) else None,
        )
    except Exception as exc:  # Display persistence cannot undo a completed mutation.
        log.warning("file_snapshot.persistence_unavailable: reason=%s bytes=%d",
                    type(exc).__name__, len(payload))
        return {**retained, "content_unavailable": True}
    return {**retained, "sha256_ref": sha}


def build_file_edit_snapshot(path: str, original: str, updated: str) -> dict[str, Any]:
    """Capture a complete diff, or mark only its display content unavailable.

    Called on a worker thread/process after the mutation has succeeded. Never
    publish a partial diff as complete or let display work fail the mutation.
    """
    unavailable = {"kind": "edit", "path": path, "content": "", "content_unavailable": True}
    try:
        for text in (original, updated):
            if len(text) > MAX_EDIT_SNAPSHOT_INPUT_BYTES:
                return unavailable
            if len(text.encode("utf-8")) > MAX_EDIT_SNAPSHOT_INPUT_BYTES:
                return unavailable
        before = original.splitlines(keepends=True)
        after = updated.splitlines(keepends=True)
        if max(len(before), len(after)) > MAX_EDIT_SNAPSHOT_LINES:
            return unavailable
        prefix = 0
        while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
            prefix += 1
        before_end, after_end = len(before), len(after)
        while (
            before_end > prefix and after_end > prefix
            and before[before_end - 1] == after[after_end - 1]
        ):
            before_end -= 1
            after_end -= 1
        # Retain the same three context lines that unified_diff normally emits.
        # Cropping also prevents repeated unchanged lines inflating match work.
        offset = max(0, prefix - 3)
        before = before[offset:min(len(before), before_end + 3)]
        after = after[offset:min(len(after), after_end + 3)]
        if len(before) * len(after) > MAX_EDIT_SNAPSHOT_LINE_PAIRS:
            return unavailable
        lines: list[str] = []
        retained_bytes = 0
        for line in difflib.unified_diff(before, after, fromfile=path, tofile=path):
            if offset and (header := _HUNK_HEADER.match(line)):
                line = (
                    f"@@ -{int(header[1]) + offset}{header[2] or ''} "
                    f"+{int(header[3]) + offset}{header[4] or ''} @@{header[5]}\n"
                )
            literal = line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            retained_bytes += len(literal.encode("utf-8"))
            if retained_bytes > MAX_EDIT_SNAPSHOT_BYTES:
                return unavailable
            lines.append(literal)
        return {"kind": "edit", "path": path, "content": "".join(lines)}
    except Exception as exc:
        log.warning("file_snapshot.diff_unavailable: reason=%s", type(exc).__name__)
        return unavailable
