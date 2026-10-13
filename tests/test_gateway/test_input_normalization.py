from __future__ import annotations

import base64

import pytest

from opensquilla.attachment_refs import make_attachment_ref, write_transcript_material
from opensquilla.gateway.input_normalization import (
    INLINE_TEXT_ATTACHMENT_MAX_BYTES,
    LARGE_PASTE_CHARS,
    LARGE_PASTE_PLACEHOLDER,
    PAGE_DUMP_CHARS,
    PAGE_DUMP_PLACEHOLDER,
    estimate_text_tokens,
    infer_normalized_input_from_attachments,
    infer_pasted_text_input_from_attachments,
    materialize_generated_text_attachments,
    normalize_incoming_text,
    page_dump_marker_score,
)


def test_large_paste_becomes_generated_text_attachment_for_web() -> None:
    raw = "a" * LARGE_PASTE_CHARS

    normalized = normalize_incoming_text(
        raw,
        source_hint={"caller_kind": "web", "channel_kind": "webchat"},
        attachments=[],
    )

    assert normalized.kind == "large_paste"
    assert normalized.semantic_message == "Please process the attached pasted text."
    assert normalized.message_text == "Please process the attached pasted text."
    assert normalized.material_chars == len(raw)
    assert normalized.material_estimated_tokens == estimate_text_tokens(raw)
    assert normalized.generated_attachments
    attachment = normalized.generated_attachments[0]
    assert attachment["type"] == "text/plain"
    assert attachment["mime"] == "text/plain"
    assert attachment["name"].startswith("webchat-paste-")
    assert base64.b64decode(attachment["data"]).decode("utf-8") == raw
    assert normalized.metadata["guard_action"] == "generated_text_attachment"


def test_page_dump_marker_score_requires_multiple_markers() -> None:
    raw = "\n".join(
        [
            "Chat session agent:main:webchat:gp85g1kj",
            "Running",
            "Still waiting for agent response...",
            "AI MODEL ROUTER",
            "The provider returned an empty response; retrying once.",
        ]
    )

    assert page_dump_marker_score(raw) >= 3


def test_page_dump_is_guarded_below_large_paste_threshold() -> None:
    raw = (
        "Chat session agent:main:webchat:gp85g1kj\n"
        "Running\n"
        "Still waiting for agent response...\n"
        "AI MODEL ROUTER\n"
        + ("x" * PAGE_DUMP_CHARS)
    )

    normalized = normalize_incoming_text(
        raw,
        source_hint={"caller_kind": "web", "channel_kind": "webchat"},
        attachments=[],
    )

    assert normalized.kind == "page_dump"
    assert normalized.semantic_message == "Please process the attached WebChat page dump."
    assert normalized.generated_attachments[0]["name"].startswith("webchat-page-dump-")
    assert normalized.metadata["marker_score"] >= 3


def test_existing_attachments_keep_short_message_unchanged() -> None:
    normalized = normalize_incoming_text(
        "summarize this",
        source_hint={"caller_kind": "web", "channel_kind": "webchat"},
        attachments=[{"type": "text/plain", "data": "YWJj", "name": "note.txt"}],
    )

    assert normalized.kind == "plain"
    assert normalized.message_text == "summarize this"
    assert normalized.semantic_message == "summarize this"
    assert normalized.generated_attachments == []


def test_byte_limit_blocks_generated_attachment_without_losing_material_metadata() -> None:
    raw = "界" * ((INLINE_TEXT_ATTACHMENT_MAX_BYTES // len("界".encode())) + 1)

    normalized = normalize_incoming_text(
        raw,
        source_hint={"caller_kind": "web", "channel_kind": "webchat"},
        attachments=[],
    )

    assert normalized.kind == "too_large"
    assert normalized.generated_attachments == []
    assert normalized.material_chars == len(raw)
    assert normalized.material_estimated_tokens == estimate_text_tokens(raw)
    assert normalized.metadata["guard_action"] == "blocked_text_too_large"


@pytest.mark.parametrize(
    "source_hint",
    [
        {"caller_kind": "web"},
        {"channel_kind": "webchat"},
        {"channel_kind": "web"},
        {"source_kind": "webui"},
        {"sourceKind": "webui"},
        {"callerKind": "web"},
        {"channelKind": "webchat"},
    ],
)
def test_supported_web_source_hint_shapes_generate_attachment(
    source_hint: dict[str, str],
) -> None:
    normalized = normalize_incoming_text(
        "a" * LARGE_PASTE_CHARS,
        source_hint=source_hint,
        attachments=[],
    )

    assert normalized.kind == "large_paste"
    assert normalized.generated_attachments
    assert normalized.metadata["guard_action"] == "generated_text_attachment"


def test_ascii_token_estimate_preserves_len_div_4_behavior() -> None:
    assert estimate_text_tokens("a" * 20_000) == 5_000


def test_cjk_token_estimate_is_not_len_div_4() -> None:
    text = "家庭日程" * 100

    assert estimate_text_tokens(text) == len(text)
    assert estimate_text_tokens(text) > len(text) // 4


def test_infers_page_dump_normalization_from_placeholder_attachment() -> None:
    raw = (
        "Chat session agent:main:webchat:gp85g1kj\n"
        "Running\n"
        "Still waiting for agent response...\n"
        "AI MODEL ROUTER\n"
        + ("界" * PAGE_DUMP_CHARS)
    )
    attachment = {
        "type": "text/plain",
        "mime": "text/plain",
        "name": "webchat-page-dump-20260531-000000.txt",
        "data": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
    }

    normalized = infer_normalized_input_from_attachments(
        PAGE_DUMP_PLACEHOLDER,
        [attachment],
    )

    assert normalized is not None
    assert normalized.kind == "page_dump"
    assert normalized.semantic_message == PAGE_DUMP_PLACEHOLDER
    assert normalized.metadata["guard_action"] == "generated_text_attachment"
    assert normalized.metadata["original_chars"] == len(raw)
    assert normalized.metadata["material_estimated_tokens"] == estimate_text_tokens(raw)
    assert normalized.metadata["marker_score"] >= 3


def test_regular_text_attachment_does_not_infer_normalization() -> None:
    attachment = {
        "type": "text/plain",
        "mime": "text/plain",
        "name": "notes.txt",
        "data": base64.b64encode(b"hello").decode("ascii"),
    }

    assert (
        infer_normalized_input_from_attachments(LARGE_PASTE_PLACEHOLDER, [attachment])
        is None
    )


def test_pasted_text_attachment_preserves_user_message() -> None:
    raw = "界" * 20_001
    attachment = {
        "type": "text/plain",
        "mime": "text/plain",
        "name": "pasted-text-1.txt",
        "origin": "paste",
        "data": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
    }

    normalized = infer_pasted_text_input_from_attachments("请总结", [attachment])

    assert normalized is not None
    assert normalized.message_text == "请总结"
    assert normalized.semantic_message == "请总结"
    assert normalized.metadata["origin"] == "paste"
    assert normalized.metadata["guard_action"] == "generated_text_attachment"


def test_staged_pasted_text_uses_server_material_metadata(tmp_path) -> None:
    raw = "界" * 20_001
    sha, path, _ = write_transcript_material(
        media_root=tmp_path,
        session_id="session-1",
        payload=raw.encode("utf-8"),
    )
    attachment = make_attachment_ref(
        sha256=sha,
        name="pasted-text-1.txt",
        mime="text/plain",
        size=len(raw.encode("utf-8")),
        session_id="session-1",
        source="upload",
    )
    attachment.update(
        {
            "origin": "paste",
            "_material_chars": len(raw),
            "_material_estimated_tokens": estimate_text_tokens(raw),
            "_material_path": str(path),
        }
    )

    normalized = infer_pasted_text_input_from_attachments("请总结", [attachment])

    assert normalized is not None
    assert normalized.material_chars == len(raw)
    assert normalized.material_estimated_tokens == estimate_text_tokens(raw)
    assert normalized.metadata["original_chars"] == len(raw)


def test_pasted_text_materializes_as_preview_only_attachment(tmp_path) -> None:
    raw = "line 1\n" + ("x" * 20_000)
    attachment = {
        "type": "text/plain",
        "mime": "text/plain",
        "name": "pasted-text-1.txt",
        "origin": "paste",
        "data": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
    }

    refs = materialize_generated_text_attachments(
        [attachment],
        media_root=tmp_path,
        session_id="session-1",
        normalization_metadata={"guard_action": "generated_text_attachment"},
    )

    assert refs[0]["kind"] == "attachment_ref"
    assert refs[0]["origin"] == "paste"
    assert refs[0]["source"] == "input_normalization"
    assert refs[0]["_provider_inline_policy"] == "preview_only"
