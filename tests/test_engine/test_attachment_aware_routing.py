"""Offline coverage for provider-visible attachment capacity routing."""

from __future__ import annotations

import asyncio
import base64
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing.policy import large_context_min_tier
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps.squilla_router import (
    _material_estimated_tokens,
    _router_text_fallback_chain,
)
from opensquilla.engine.turn_runner.attachment_stage import (
    AttachmentStage,
    AttachmentStageInput,
    rebind_attachment_prompt,
)
from opensquilla.gateway.input_normalization import normalize_incoming_text
from opensquilla.provider.types import ContentBlockText
from opensquilla.token_estimation import estimate_attachment_text_tokens

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _RuntimeAttachmentBuilder:
    calls = 0

    def build(
        self,
        message: str,
        attachments: list[dict],
        *,
        workspace_dir: str | Path | None = None,
        session_id: str | None = None,
    ) -> list[Any] | None:
        self.calls += 1
        return TurnRunner._build_attachment_messages(
            message,
            attachments,
            workspace_dir=workspace_dir,
            session_id=session_id,
        )


def _inline_attachment(payload: bytes, mime: str, name: str) -> dict[str, str]:
    return {
        "type": mime,
        "name": name,
        "data": base64.b64encode(payload).decode("ascii"),
    }


def _pdf_bytes(text: str) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    y = 760
    for offset in range(0, len(text), 90):
        pdf.drawString(36, y, text[offset : offset + 90])
        y -= 12
        if y < 36:
            pdf.showPage()
            y = 760
    pdf.save()
    return output.getvalue()


def _docx_bytes(text: str) -> bytes:
    from docx import Document

    document = Document()
    for offset in range(0, len(text), 240):
        document.add_paragraph(text[offset : offset + 240])
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _compressed_office_payload(inflated_bytes: int) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"x" * inflated_bytes)
    return output.getvalue()


async def _materialize(*attachments: dict[str, str], prompt: str = "summarize"):
    builder = _RuntimeAttachmentBuilder()
    outcome = await AttachmentStage(builder=builder).run(
        AttachmentStageInput(
            effective_runtime_message=prompt,
            attachments=list(attachments),
        )
    )
    return outcome.require_output(), builder


def _routing_tokens(
    prompt: str,
    attachment_tokens: int,
    *,
    normalization_tokens: int | None = None,
    generated_normalization_tokens: int = 0,
) -> int:
    metadata: dict[str, Any] = {
        "attachment_material_estimated_tokens": attachment_tokens,
        "attachment_generated_normalization_estimated_tokens": (
            generated_normalization_tokens
        ),
    }
    if normalization_tokens is not None:
        metadata["material_estimated_tokens"] = normalization_tokens
    context = TurnContext(
        message=prompt,
        session_key="agent:main:attachment-routing",
        config=None,
        provider=None,
        model="dummy",
        tool_defs=[],
        system_prompt="",
        metadata=metadata,
    )
    return _material_estimated_tokens(context, prompt)


@pytest.mark.asyncio
async def test_paste_txt_pdf_docx_and_multiple_files_share_capacity_floor() -> None:
    material = "capacity-equivalent source material 0123456789\n" * 2_600
    normalized = normalize_incoming_text(
        material,
        source_hint={"caller_kind": "web"},
        attachments=None,
    )
    paste_tokens = normalized.material_estimated_tokens

    variants = [
        (_inline_attachment(material.encode(), "text/plain", "material.txt"),),
        (_inline_attachment(_pdf_bytes(material), "application/pdf", "material.pdf"),),
        (_inline_attachment(_docx_bytes(material), DOCX_MIME, "material.docx"),),
        (
            _inline_attachment(material[: len(material) // 2].encode(), "text/plain", "a.txt"),
            _inline_attachment(material[len(material) // 2 :].encode(), "text/plain", "b.txt"),
        ),
    ]

    assert large_context_min_tier(paste_tokens, 200_000) == "c2"
    for attachments in variants:
        output, builder = await _materialize(*attachments)
        routed_tokens = _routing_tokens("summarize", output.stats.estimated_tokens)
        assert builder.calls == 1
        assert large_context_min_tier(routed_tokens, 200_000) == "c2"
        assert paste_tokens * 0.9 <= routed_tokens <= paste_tokens * 1.1


@pytest.mark.parametrize("attachment_kind", ["txt", "pdf", "docx"])
@pytest.mark.asyncio
async def test_stats_measure_exact_provider_visible_text_for_rendered_formats(
    attachment_kind: str,
) -> None:
    material = "synthetic capacity content 0123456789\n" * 80
    if attachment_kind == "txt":
        attachment = _inline_attachment(material.encode(), "text/plain", "sample.txt")
    elif attachment_kind == "pdf":
        attachment = _inline_attachment(
            _pdf_bytes(material),
            "application/pdf",
            "sample.pdf",
        )
    else:
        attachment = _inline_attachment(
            _docx_bytes(material),
            DOCX_MIME,
            "sample.docx",
        )

    output, _builder = await _materialize(attachment)
    content = output.extra_messages[0].content
    visible_blocks = [block for block in content[1:] if isinstance(block, ContentBlockText)]
    visible_text = "".join(block.text for block in visible_blocks)

    assert material.splitlines()[0] in visible_text
    assert output.stats.provider_visible_text_chars == len(visible_text)
    assert output.stats.estimated_tokens == sum(
        estimate_attachment_text_tokens(block.text) for block in visible_blocks
    )


@pytest.mark.asyncio
async def test_capacity_counts_prompt_and_attachment_once_and_rebind_does_not_parse() -> None:
    material = "z" * 120_000
    output, builder = await _materialize(
        _inline_attachment(material.encode(), "text/plain", "material.txt"),
        prompt="old prompt",
    )

    routed_tokens = _routing_tokens("old prompt", output.stats.estimated_tokens)
    assert routed_tokens == len("old prompt") // 4 + output.stats.estimated_tokens

    normalized = normalize_incoming_text(
        material,
        source_hint={"caller_kind": "web"},
        attachments=None,
    )
    guarded_tokens = _routing_tokens(
        normalized.message_text,
        output.stats.estimated_tokens,
        normalization_tokens=normalized.material_estimated_tokens,
        generated_normalization_tokens=output.stats.estimated_tokens,
    )
    # The provider-visible tokenizer estimate may conservatively exceed the
    # ingress chars/4 hint (highly repetitive text is one such case). It is
    # still counted once rather than added to the original paste estimate.
    assert guarded_tokens >= normalized.material_estimated_tokens
    assert guarded_tokens < output.stats.estimated_tokens * 1.1

    rebound = rebind_attachment_prompt(output.extra_messages, "new routed prompt")
    assert builder.calls == 1
    assert rebound is not output.extra_messages
    assert isinstance(rebound[0].content, list)
    assert isinstance(rebound[0].content[0], ContentBlockText)
    assert rebound[0].content[0].text == "new routed prompt"
    assert "old prompt" not in str(rebound[0].content)
    assert str(rebound[0].content).count(material) == 1


@pytest.mark.parametrize(
    ("normalization_tokens", "ordinary_attachment_tokens", "expected_tier"),
    [
        (20_000, 5_000, "c2"),
        (50_000, 30_000, "c3"),
        (50_000, 50_000, "c3"),
    ],
)
def test_generated_paste_adds_every_ordinary_attachment_once(
    normalization_tokens: int,
    ordinary_attachment_tokens: int,
    expected_tier: str,
) -> None:
    generated_preview_tokens = 2_000
    routed_tokens = _routing_tokens(
        "generated paste preview",
        generated_preview_tokens + ordinary_attachment_tokens,
        normalization_tokens=normalization_tokens,
        generated_normalization_tokens=generated_preview_tokens,
    )

    assert routed_tokens == normalization_tokens + ordinary_attachment_tokens
    assert large_context_min_tier(routed_tokens, 200_000) == expected_tier


@pytest.mark.asyncio
async def test_generated_normalization_tokens_are_measured_separately() -> None:
    generated = _inline_attachment(b"g" * 40_000, "text/plain", "generated.txt")
    generated["_generated_by"] = "input_normalization"
    generated["source"] = "input_normalization"
    generated["_provider_inline_policy"] = "preview_only"
    ordinary = _inline_attachment(b"o" * 80_000, "text/plain", "ordinary.txt")
    builder = _RuntimeAttachmentBuilder()
    outcome = await AttachmentStage(builder=builder).run(
        AttachmentStageInput(
            effective_runtime_message="preview",
            attachments=[generated, ordinary],
            generated_normalization_attachment_count=1,
        )
    )
    stats = outcome.require_output().stats

    assert 0 < stats.generated_normalization_estimated_tokens < stats.estimated_tokens
    assert builder.calls == 1


@pytest.mark.asyncio
async def test_image_workspace_marker_does_not_shift_following_attachment_accounting(
    tmp_path: Path,
) -> None:
    image = _inline_attachment(b"\x89PNG", "image/png", "pixel.png")
    generated = _inline_attachment(b"g" * 40_000, "text/plain", "generated.txt")
    generated["_generated_by"] = "input_normalization"
    generated["source"] = "input_normalization"
    generated["_provider_inline_policy"] = "preview_only"
    builder = _RuntimeAttachmentBuilder()

    output = (
        await AttachmentStage(builder=builder).run(
            AttachmentStageInput(
                effective_runtime_message="preview",
                attachments=[image, generated],
                workspace_dir=tmp_path,
                session_id="synthetic-session",
                generated_normalization_attachment_count=1,
            )
        )
    ).require_output()
    text_blocks = [
        block
        for block in output.extra_messages[0].content[1:]
        if isinstance(block, ContentBlockText)
    ]
    generated_block = text_blocks[-1]

    assert output.stats.image_count == 1
    assert output.stats.provider_visible_text_chars == sum(
        len(block.text) for block in text_blocks
    )
    assert output.stats.generated_normalization_estimated_tokens == (
        estimate_attachment_text_tokens(generated_block.text)
    )


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_share_attachment_envelopes() -> None:
    builder = _RuntimeAttachmentBuilder()
    stage = AttachmentStage(builder=builder)
    left_material = "left-only-" * 100
    right_material = "right-only-" * 100

    left, right = await asyncio.gather(
        stage.run(
            AttachmentStageInput(
                effective_runtime_message="left prompt",
                attachments=[
                    _inline_attachment(
                        left_material.encode(),
                        "text/plain",
                        "left.txt",
                    )
                ],
                session_id="left-session",
            )
        ),
        stage.run(
            AttachmentStageInput(
                effective_runtime_message="right prompt",
                attachments=[
                    _inline_attachment(
                        right_material.encode(),
                        "text/plain",
                        "right.txt",
                    )
                ],
                session_id="right-session",
            )
        ),
    )
    left_output = left.require_output()
    right_output = right.require_output()

    assert builder.calls == 2
    assert left_material in str(left_output.extra_messages)
    assert right_material not in str(left_output.extra_messages)
    assert right_material in str(right_output.extra_messages)
    assert left_material not in str(right_output.extra_messages)
    rebind_attachment_prompt(left_output.extra_messages, "left rebound")
    rebind_attachment_prompt(right_output.extra_messages, "right rebound")
    assert builder.calls == 2


@pytest.mark.asyncio
async def test_failed_opaque_image_and_extreme_text_have_bounded_capacity() -> None:
    broken_pdf, _ = await _materialize(
        _inline_attachment(b"%PDF-1.4\nbroken", "application/pdf", "broken.pdf")
    )
    broken_docx, _ = await _materialize(
        _inline_attachment(b"not-a-zip", DOCX_MIME, "broken.docx")
    )
    opaque, _ = await _materialize(
        _inline_attachment(b"\x00\x01" * 50_000, "application/octet-stream", "blob.bin")
    )
    image, _ = await _materialize(
        _inline_attachment(b"\x89PNG", "image/png", "pixel.png")
    )
    extreme, _ = await _materialize(
        _inline_attachment(b"x" * 1_000_000, "text/plain", "huge.txt")
    )

    assert broken_pdf.stats.parse_failure_count == 1
    assert broken_pdf.stats.estimated_tokens < 1_000
    assert large_context_min_tier(broken_pdf.stats.estimated_tokens, 200_000) is None
    assert broken_docx.stats.parse_failure_count == 1
    assert broken_docx.stats.estimated_tokens < 1_000
    assert opaque.stats.parse_failure_count == 0
    assert opaque.stats.estimated_tokens < 1_000
    assert large_context_min_tier(opaque.stats.estimated_tokens, 200_000) is None
    assert image.stats.image_count == 1
    assert 1_024 <= image.stats.estimated_tokens < 2_000
    assert large_context_min_tier(image.stats.estimated_tokens, 200_000) is None
    # Routing follows the provider-visible 200k-character truncation, not the
    # compressed/raw byte size of the original file.
    assert large_context_min_tier(extreme.stats.estimated_tokens, 200_000) == "c2"
    assert extreme.stats.provider_visible_text_chars < 210_000


@pytest.mark.asyncio
async def test_maximum_compressed_office_batch_has_one_bounded_inflation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import runtime

    monkeypatch.setattr(runtime, "_OFFICE_DECOMPRESSED_LIMIT", 2 * 1024 * 1024)
    compressed = _compressed_office_payload(768 * 1024)
    attachments = tuple(
        _inline_attachment(compressed, DOCX_MIME, f"compressed-{index}.docx")
        for index in range(10)
    )

    output, builder = await _materialize(*attachments)

    assert builder.calls == 1
    assert output.stats.attachment_count == 10
    assert output.stats.parse_failure_count == 10
    assert output.stats.estimated_tokens < 10_000
    assert "attachment batch" in str(output.extra_messages)


def test_large_context_floor_filters_every_fallback_below_the_floor() -> None:
    tiers = {
        "c0": {"model": "dummy-c0"},
        "c1": {"model": "dummy-c1"},
        "c2": {"model": "dummy-c2"},
        "c3": {"model": "dummy-c3"},
    }

    assert _router_text_fallback_chain("c2", tiers, "c2") == []
    assert [item["tier"] for item in _router_text_fallback_chain("c3", tiers, "c2")] == [
        "c2"
    ]
    assert [item["tier"] for item in _router_text_fallback_chain("c3", tiers)] == [
        "c2",
        "c1",
        "c0",
    ]
