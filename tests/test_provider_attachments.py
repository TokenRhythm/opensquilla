"""Tests for ContentBlockDocument and provider mapping.

The contract is:

- test_existing_image_block_unchanged is a regression — it should pass on
  day-zero (no prod code change required) because ContentBlockImage already
  works.

- The other 4 tests fail with ImportError or AttributeError until
  ContentBlockDocument is added to provider/types.py and the document branch
  is wired into provider/anthropic.py:_build_message_payload.

Image flows must not regress. PDF document blocks require Claude 3.5 Sonnet+
or newer; older SKUs and non-Anthropic providers gracefully skip with WARN +
counter + user-visible "[document attached but not consumable by this model]".
Anthropic 400s on document blocks must surface the error code into transcript
and increment counter provider.document_block.rejected{code}; never silently
swallow.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from opensquilla.provider.anthropic import AnthropicProvider, _build_message_payload
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ErrorEvent,
    Message,
)

# ---------------------------------------------------------------------------
# Test 1 — regression: existing image block round-trip unchanged.
# ---------------------------------------------------------------------------

def test_existing_image_block_unchanged() -> None:
    """ContentBlockImage round-trips through the Anthropic adapter unchanged.

    Locks the existing image-attachment shape so future additions don't
    regress the only attachment kind that works today.
    """
    msg = Message(
        role="user",
        content=[
            ContentBlockText(text="describe this"),
            ContentBlockImage(
                source_type="base64",
                media_type="image/png",
                data="aGVsbG8=",  # arbitrary base64
            ),
        ],
    )

    payload = _build_message_payload(msg)

    assert payload["role"] == "user"
    parts = payload["content"]
    assert {"type": "text", "text": "describe this"} in parts
    image_part = next(p for p in parts if p["type"] == "image")
    assert image_part == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "aGVsbG8=",
        },
    }


# ---------------------------------------------------------------------------
# Test 2 — ContentBlockDocument round-trips through the Anthropic adapter as
# a native {type:"document", source:{type:"base64", media_type, data}, title}
# block.
# ---------------------------------------------------------------------------

def test_document_block_round_trips_through_anthropic_adapter() -> None:
    """A PDF ContentBlockDocument emits Anthropic's native document shape."""
    from opensquilla.provider.types import ContentBlockDocument  # noqa: F401

    msg = Message(
        role="user",
        content=[
            ContentBlockText(text="summarise the attachment"),
            ContentBlockDocument(
                source_type="base64",
                media_type="application/pdf",
                data="JVBERi0xLjQK",  # %PDF-1.4 base64
                title="report.pdf",
            ),
        ],
    )

    payload = _build_message_payload(msg)

    parts = payload["content"]
    doc_part = next(p for p in parts if p["type"] == "document")
    assert doc_part == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": "JVBERi0xLjQK",
        },
        "title": "report.pdf",
    }


# ---------------------------------------------------------------------------
# Test 3 — ContentBlockDocument validator rejects non-PDF media types in v1.
# Locked decision: only application/pdf is a legal document block media_type
# in this release. Text/csv/json go through ContentBlockText with <file> wrap.
# ---------------------------------------------------------------------------

def test_document_block_with_unsupported_media_type_raises() -> None:
    """Constructing a ContentBlockDocument with a non-PDF media_type raises."""
    from pydantic import ValidationError

    from opensquilla.provider.types import ContentBlockDocument

    with pytest.raises(ValidationError):
        ContentBlockDocument(
            source_type="base64",
            media_type="text/plain",  # not legal in v1
            data="aGVsbG8=",
            title="notes.txt",
        )


# ---------------------------------------------------------------------------
# Test 4 — when the model SKU does not support document blocks, the adapter
# substitutes a fallback ContentBlockText so the conversation still proceeds.
# Locked decision: user-visible string is "[document attached but not
# consumable by this model]"; counter provider.document_block.unsupported
# is incremented.
# ---------------------------------------------------------------------------

def test_document_block_unsupported_provider_skips_gracefully() -> None:
    """Older SKUs replace the document block with a fallback text block."""
    from opensquilla.provider.types import ContentBlockDocument

    # The adapter's payload builder accepts an optional model parameter to
    # detect SKU support. An older Haiku SKU is below the 3.5 Sonnet floor.
    msg = Message(
        role="user",
        content=[
            ContentBlockDocument(
                source_type="base64",
                media_type="application/pdf",
                data="JVBERi0xLjQK",
                title="report.pdf",
            ),
        ],
    )

    payload = _build_message_payload(msg, model="claude-haiku-4-5-20251001")

    parts = payload["content"]
    # No document block survives.
    assert all(p.get("type") != "document" for p in parts), parts
    # A fallback text block carries the user-visible string and the filename.
    fallback = next(
        p
        for p in parts
        if p.get("type") == "text"
        and "[document attached but not consumable by this model]" in p.get("text", "")
    )
    assert "report.pdf" in fallback["text"]


# ---------------------------------------------------------------------------
# Test 5 — Anthropic 400 on a document block surfaces the HTTP code into the
# error stream and increments the rejected counter, never silently swallowed.
# ---------------------------------------------------------------------------

def test_anthropic_400_on_document_block_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 response while sending a document block surfaces code + body."""
    from opensquilla.provider import anthropic as anthropic_mod
    from opensquilla.provider.types import ContentBlockDocument

    captured: dict[str, Any] = {"counter_calls": []}

    def _record_counter(code: str) -> None:
        captured["counter_calls"].append(code)

    # The adapter must expose a hook the integration uses to bump the counter.
    # We monkeypatch it so the test can observe the increment without wiring
    # a full metrics backend.
    monkeypatch.setattr(
        anthropic_mod,
        "_increment_document_block_rejected",
        _record_counter,
        raising=False,
    )

    error_body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "document block malformed",
            },
        }
    )

    class _StubResponse:
        status_code = 400
        text = error_body

        async def aread(self) -> bytes:
            return error_body.encode()

        async def aiter_lines(self):
            if False:
                yield ""

        async def __aenter__(self) -> _StubResponse:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    class _StubClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _StubClient:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        def stream(self, *args: Any, **kwargs: Any) -> _StubResponse:
            return _StubResponse()

    monkeypatch.setattr(anthropic_mod.httpx, "AsyncClient", _StubClient)

    provider = AnthropicProvider(api_key="k", model="claude-sonnet-4-6")
    msg = Message(
        role="user",
        content=[
            ContentBlockDocument(
                source_type="base64",
                media_type="application/pdf",
                data="JVBERi0xLjQK",
                title="report.pdf",
            ),
        ],
    )

    async def _run() -> list[Any]:
        out: list[Any] = []
        async for ev in provider.chat([msg], None, ChatConfig()):
            out.append(ev)
        return out

    events = asyncio.run(_run())

    error_events = [e for e in events if isinstance(e, ErrorEvent)]
    assert error_events, f"expected ErrorEvent in {events!r}"
    err = error_events[0]
    assert len(error_events) == 1
    assert err.code == "400"
    assert err.http_status == 400
    assert err.provider_code == "invalid_request_error"
    assert err.retry_after_s is None
    assert "document block malformed" in err.message
    assert captured["counter_calls"] == ["400"]
