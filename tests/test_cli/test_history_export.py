from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.cli.gateway_client import GatewayRPCError
from opensquilla.cli.history_export import (
    export_session_history_json,
    export_session_history_markdown,
)


class _FakeHistoryClient:
    def __init__(
        self,
        pages: dict[str | None, dict[str, Any]],
        *,
        detail_message: bytes | None = None,
        detail_text: bytes | None = None,
        chunk_width: int = 7,
        corrupt: str | None = None,
    ) -> None:
        self.pages = pages
        self.detail_message = detail_message
        self.detail_text = detail_text
        self.chunk_width = chunk_width
        self.corrupt = corrupt
        self.history_calls: list[dict[str, Any]] = []
        self.detail_calls: list[dict[str, Any]] = []

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]:
        self.history_calls.append(
            {
                "session_key": session_key,
                "limit": limit,
                "before": before,
                "after": after,
                "include_canonical": include_canonical,
                "include_summaries": include_summaries,
            }
        )
        return dict(self.pages[before])

    async def call(self, method: str, params: dict | None = None) -> Any:
        assert method == "chat.history.entry.v1"
        assert params is not None
        self.detail_calls.append(dict(params))
        field = params["field"]
        source = self.detail_message if field == "message" else self.detail_text
        assert source is not None
        offset = int(params["offset"])
        end = min(len(source), offset + self.chunk_width)
        chunk = source[offset:end]
        reported_offset = offset
        if self.corrupt == "offset" and offset > 0:
            reported_offset += 1
        digest = hashlib.sha256(source).hexdigest()
        if self.corrupt == "sha256":
            digest = "0" * 64
        return {
            "session_key": params["sessionKey"],
            "cursor": params["cursor"],
            "encoding": "base64",
            "field": field,
            "content_type": (
                "application/json" if field == "message" else "text/plain; charset=utf-8"
            ),
            "chunk_base64": base64.b64encode(chunk).decode("ascii"),
            "offset": reported_offset,
            "next": end if end < len(source) else None,
            "total": len(source),
            "sha256": digest,
        }


def _message(index: int, text: str) -> dict[str, Any]:
    return {
        "message_id": f"message-{index}",
        "transcript_id": index,
        "timestamp": index,
        "role": "user" if index % 2 else "assistant",
        "text": text,
    }


def _detail_preview(index: int, *, original_bytes: int) -> dict[str, Any]:
    return {
        "message_id": f"message-{index}",
        "transcript_id": index,
        "timestamp": index,
        "role": "assistant",
        "preview": "分块预览",
        "original_bytes": original_bytes,
        "detail_ref": {
            "method": "chat.history.entry.v1",
            "sessionKey": "agent:main:export",
            "cursor": f"{index}|{index}",
        },
        "truncated_by_bytes": True,
    }


def _two_page_history(newest_messages: list[dict[str, Any]]) -> dict[str | None, dict[str, Any]]:
    return {
        None: {
            "messages": newest_messages,
            "has_more": True,
            "oldest_cursor": "3|3",
            "newest_cursor": "4|4",
            "canonical_available": True,
            "canonical_complete": True,
            "truncated_by_bytes": True,
            "wire_bytes": 1234,
            "byte_budget": 64 * 1024,
        },
        "3|3": {
            "messages": [_message(1, "第一条"), _message(2, "second 🦐")],
            "has_more": False,
            "oldest_cursor": "1|1",
            "newest_cursor": "2|2",
            "canonical_available": True,
            "canonical_complete": True,
            "truncated_by_bytes": False,
            "wire_bytes": 987,
            "byte_budget": 64 * 1024,
        },
    }


@pytest.mark.asyncio
async def test_json_export_spools_newest_first_pages_and_detail_chunks(tmp_path: Path) -> None:
    full_detail = _message(4, "尾页中文 🦐 \\\"quoted\\\"")
    detail_bytes = json.dumps(
        full_detail,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    pages = _two_page_history(
        [_message(3, "third"), _detail_preview(4, original_bytes=len(detail_bytes))]
    )
    client = _FakeHistoryClient(
        pages,
        detail_message=detail_bytes,
        chunk_width=3,
    )
    target = tmp_path / "session.json"

    receipt = await export_session_history_json(
        client,
        "agent:main:export",
        target,
        resolved={"session_key": "agent:main:export", "status": "done"},
        preview={"previews": [{"lastMessage": "尾页中文"}]},
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert [message["message_id"] for message in payload["history"]["messages"]] == [
        "message-1",
        "message-2",
        "message-3",
        "message-4",
    ]
    assert payload["history"]["messages"][-1] == full_detail
    assert payload["history"]["loaded_count"] == 4
    assert payload["history"]["has_more"] is False
    assert payload["history"]["truncated_by_bytes"] is False
    assert "wire_bytes" not in payload["history"]
    assert "byte_budget" not in payload["history"]
    assert receipt.message_count == 4
    assert receipt.page_count == 2
    assert [call["before"] for call in client.history_calls] == [None, "3|3"]
    assert all(call["include_canonical"] is True for call in client.history_calls)
    assert all(call["include_summaries"] is False for call in client.history_calls)
    assert [call["offset"] for call in client.detail_calls] == list(
        range(0, len(detail_bytes), 3)
    )


@pytest.mark.asyncio
async def test_markdown_export_streams_utf8_text_detail_across_chunk_boundaries(
    tmp_path: Path,
) -> None:
    text = "中文🦐跨块\\尾部"
    encoded = text.encode("utf-8")
    preview = _detail_preview(4, original_bytes=999_999)
    pages = {
        None: {
            "messages": [preview],
            "has_more": False,
            "oldest_cursor": "4|4",
            "newest_cursor": "4|4",
            "canonical_available": True,
            "canonical_complete": True,
        }
    }
    client = _FakeHistoryClient(pages, detail_text=encoded, chunk_width=2)
    target = tmp_path / "session.md"

    await export_session_history_markdown(
        client,
        "agent:main:export",
        target,
        header="# Export\n\n",
    )

    assert target.read_text(encoding="utf-8") == f"# Export\n\n## Assistant\n\n{text}\n"
    assert [call["offset"] for call in client.detail_calls] == list(range(0, len(encoded), 2))
    assert all(call["field"] == "text" for call in client.detail_calls)


@pytest.mark.parametrize(
    ("corrupt", "expected_code"),
    [
        ("offset", "HISTORY_DETAIL_OFFSET_MISMATCH"),
        ("sha256", "HISTORY_DETAIL_CHECKSUM_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_detail_validation_failure_does_not_replace_existing_target(
    tmp_path: Path,
    corrupt: str,
    expected_code: str,
) -> None:
    full_detail = _message(4, "中文🦐detail")
    detail_bytes = json.dumps(
        full_detail,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    pages = {
        None: {
            "messages": [_detail_preview(4, original_bytes=len(detail_bytes))],
            "has_more": False,
            "oldest_cursor": "4|4",
            "newest_cursor": "4|4",
            "canonical_available": True,
            "canonical_complete": True,
        }
    }
    client = _FakeHistoryClient(
        pages,
        detail_message=detail_bytes,
        chunk_width=3,
        corrupt=corrupt,
    )
    target = tmp_path / "existing.json"
    target.write_text("existing export", encoding="utf-8")

    with pytest.raises(GatewayRPCError) as exc_info:
        await export_session_history_json(
            client,
            "agent:main:export",
            target,
            resolved={},
            preview={},
        )

    assert exc_info.value.code == expected_code
    assert target.read_text(encoding="utf-8") == "existing export"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.asyncio
async def test_detail_original_bytes_must_match_first_chunk_total(tmp_path: Path) -> None:
    full_detail = _message(4, "detail")
    detail_bytes = json.dumps(full_detail, separators=(",", ":")).encode()
    pages = {
        None: {
            "messages": [_detail_preview(4, original_bytes=len(detail_bytes) + 1)],
            "has_more": False,
            "oldest_cursor": "4|4",
            "newest_cursor": "4|4",
        }
    }
    client = _FakeHistoryClient(pages, detail_message=detail_bytes)

    with pytest.raises(GatewayRPCError) as exc_info:
        await export_session_history_json(
            client,
            "agent:main:export",
            tmp_path / "mismatch.json",
            resolved={},
            preview={},
        )

    assert exc_info.value.code == "HISTORY_DETAIL_LENGTH_MISMATCH"


@pytest.mark.parametrize("preview_field", ["preview", "text"])
@pytest.mark.asyncio
async def test_truncated_preview_without_detail_ref_fails_without_replacing_target(
    tmp_path: Path,
    preview_field: str,
) -> None:
    pages = {
        None: {
            "messages": [
                {
                    **_message(4, ""),
                    preview_field: "incomplete preview",
                    "truncated_by_bytes": True,
                    "original_bytes": 99_999,
                }
            ],
            "has_more": False,
            "oldest_cursor": "4|4",
            "newest_cursor": "4|4",
            "canonical_available": True,
            "canonical_complete": True,
        }
    }
    client = _FakeHistoryClient(pages)
    target = tmp_path / "existing.json"
    target.write_text("previous export", encoding="utf-8")

    with pytest.raises(GatewayRPCError) as exc_info:
        await export_session_history_json(
            client,
            "agent:main:export",
            target,
            resolved={},
            preview={},
        )

    assert exc_info.value.code == "HISTORY_DETAIL_REFERENCE_MISSING"
    assert target.read_text(encoding="utf-8") == "previous export"


@pytest.mark.parametrize(
    ("pages", "expected_code"),
    [
        (
            {
                None: {
                    "messages": [],
                    "has_more": False,
                    "canonical_available": False,
                }
            },
            "CANONICAL_HISTORY_UNAVAILABLE",
        ),
        (
            {
                None: {
                    "messages": [],
                    "has_more": False,
                    "canonical_complete": False,
                }
            },
            "CANONICAL_HISTORY_INCOMPLETE",
        ),
        (
            {
                None: {
                    "messages": [_message(4, "new")],
                    "has_more": True,
                    "oldest_cursor": "4|4",
                    "newest_cursor": "4|4",
                },
                "4|4": {
                    "messages": [_message(3, "old")],
                    "has_more": True,
                    "oldest_cursor": "4|4",
                    "newest_cursor": "3|3",
                },
            },
            "HISTORY_PAGINATION_STALLED",
        ),
        (
            {
                None: {
                    "messages": [_message(4, "new")],
                    "has_more": True,
                    "oldest_cursor": "4|4",
                    "newest_cursor": "4|4",
                },
                "4|4": {
                    "messages": [_message(5, "changed")],
                    "has_more": False,
                    "oldest_cursor": "5|5",
                    "newest_cursor": "5|5",
                },
            },
            "HISTORY_CURSOR_INVALIDATED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_export_preserves_canonical_and_cursor_error_boundaries(
    tmp_path: Path,
    pages: dict[str | None, dict[str, Any]],
    expected_code: str,
) -> None:
    client = _FakeHistoryClient(pages)
    target = tmp_path / "boundary.json"
    target.write_text("previous", encoding="utf-8")

    with pytest.raises(GatewayRPCError) as exc_info:
        await export_session_history_json(
            client,
            "agent:main:export",
            target,
            resolved={},
            preview={},
        )

    assert exc_info.value.code == expected_code
    assert target.read_text(encoding="utf-8") == "previous"
