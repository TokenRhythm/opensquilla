from __future__ import annotations

import base64
import io
from copy import deepcopy
from typing import Any

import pytest
from PIL import Image

from opensquilla.engine.history import HistoryReplayProjection, project_history_replay_capacity
from opensquilla.engine.turn_runner.attachment_stage import _materialization_stats
from opensquilla.provider.request_proof import (
    CHAT_REQUEST_ENVELOPE,
    RESPONSES_REQUEST_ENVELOPE,
    estimate_provider_media_tokens,
    project_provider_payload,
)
from opensquilla.provider.types import (
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolUse,
    Message,
)


@pytest.fixture(scope="module")
def png_encodings() -> dict[str, str]:
    encodings = {}
    for name, size, compression in (
        ("small", (32, 32), 9),
        ("uhd", (3840, 2160), 9),
        ("uhd_uncompressed", (3840, 2160), 0),
    ):
        stream = io.BytesIO()
        with Image.new("1", size, color=1) as image:
            image.save(stream, format="PNG", compress_level=compression)
        encodings[name] = base64.b64encode(stream.getvalue()).decode("ascii")
    return encodings


def _payload(adapter: str, data: str, *, count: int = 1) -> dict[str, Any]:
    if adapter == "ollama":
        return {"messages": [{"role": "user", "content": "Inspect.", "images": [data] * count}]}
    if adapter == "canonical":
        block = ContentBlockImage(media_type="image/png", data=data).model_dump(exclude_none=True)
    elif adapter == "anthropic":
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": data},
        }
    elif adapter == "responses":
        block = {"type": "input_image", "image_url": f"data:image/png;base64,{data}"}
    else:
        block = {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{data}"},
        }
    return {
        "input" if adapter == "responses" else "messages": [
            {"role": "user", "content": [deepcopy(block) for _ in range(count)]}
        ],
    }


@pytest.mark.parametrize("adapter", ["openai", "responses", "anthropic", "ollama", "canonical"])
def test_image_reserve_uses_pixels_independently_of_png_compression(
    png_encodings: dict[str, str], adapter: str,
) -> None:
    compressed = png_encodings["uhd"]
    uncompressed = png_encodings["uhd_uncompressed"]
    assert len(uncompressed) > 100 * len(compressed)
    proofs = []
    for data in (compressed, uncompressed):
        payload = _payload(adapter, data)
        before = deepcopy(payload)
        proof = project_provider_payload(
            payload,
            projection_adapter=adapter,
            proof_budget=50_000,
            envelope_shape=(
                RESPONSES_REQUEST_ENVELOPE if adapter == "responses" else CHAT_REQUEST_ENVELOPE
            ),
        )
        assert payload == before
        assert proof["media_image_blocks"] == 1
        assert proof["media_reserve_tokens"] == 8160
        assert proof["usage_confidence"] == "approximate_estimate"
        assert proof["image_token_estimate_method"] == "pixel_grid_32_with_fixed_fallback"
        assert proof["fits"]
        proofs.append(proof)
    assert abs(proofs[0]["estimated_tokens"] - proofs[1]["estimated_tokens"]) < 10
    assert proofs[1]["wire_json_bytes"] > 100 * proofs[0]["wire_json_bytes"]


@pytest.mark.parametrize("adapter", ["openai", "responses", "anthropic", "ollama", "canonical"])
def test_small_file_with_large_dimensions_cannot_bypass_multi_image_capacity(
    png_encodings: dict[str, str], adapter: str,
) -> None:
    assert len(base64.b64decode(png_encodings["uhd"])) < 10_000
    proof = project_provider_payload(
        _payload(adapter, png_encodings["uhd"], count=2),
        projection_adapter=adapter,
        proof_budget=50_000,
        envelope_shape=(
            RESPONSES_REQUEST_ENVELOPE if adapter == "responses" else CHAT_REQUEST_ENVELOPE
        ),
    )
    assert proof["media_image_blocks"] == 2
    assert proof["media_reserve_tokens"] == 2 * 8160
    assert not proof["fits"]
    assert proof["fallback_reason"] == "provider_request_budget_exhausted"


@pytest.mark.parametrize("adapter", ["openai", "responses"])
@pytest.mark.parametrize("detail", ["low", "high", "auto", "original"])
def test_image_reserve_honors_explicit_request_detail(
    png_encodings: dict[str, str], adapter: str, detail: str,
) -> None:
    payload = _payload(adapter, png_encodings["uhd"])
    sequence_key = "input" if adapter == "responses" else "messages"
    block = payload[sequence_key][0]["content"][0]
    if adapter == "responses":
        block["detail"] = detail
    else:
        block["image_url"]["detail"] = detail
    proof = project_provider_payload(
        payload,
        projection_adapter=adapter,
        proof_budget=50_000,
        envelope_shape=(
            RESPONSES_REQUEST_ENVELOPE if adapter == "responses" else CHAT_REQUEST_ENVELOPE
        ),
    )
    assert proof["media_reserve_tokens"] == (1024 if detail == "low" else 8160)


def test_new_history_and_wire_images_share_the_same_capacity_estimate(
    png_encodings: dict[str, str],
) -> None:
    data = png_encodings["uhd"]
    message = Message(role="user", content=[
        ContentBlockText(text="Inspect."),
        ContentBlockImage(media_type="image/png", data=data),
    ])
    new_stats = _materialization_stats(
        [message],
        attachments=[{"type": "image/png", "data": data}],
        generated_normalization_attachment_count=0,
    )
    history = project_history_replay_capacity(HistoryReplayProjection(messages=(message,)))
    wire = project_provider_payload(
        _payload("openai", data), projection_adapter="openai", proof_budget=50_000,
    )
    assert new_stats.estimated_tokens == history.media_reserve_tokens
    assert history.media_reserve_tokens == wire["media_reserve_tokens"]
    assert new_stats.estimated_tokens == 8160
    assert history.estimate_complete


@pytest.mark.parametrize(
    "data",
    [None, "invalid base64!", "a" * 400_000],
    ids=["missing", "invalid-base64", "unrecognized-image"],
)
def test_unavailable_image_dimensions_use_a_fixed_fallback(data: str | None) -> None:
    assert estimate_provider_media_tokens("image", 5 * 1024 * 1024, encoded_data=data) == 1024


def test_remote_image_uses_a_fixed_fallback_without_opening_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_open(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Remote image estimates must not open image bytes")

    monkeypatch.setattr(Image, "open", unexpected_open)
    proof = project_provider_payload(
        {"messages": [{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": "https://example.test/synthetic.png"},
        }]}]},
        projection_adapter="openai",
        proof_budget=10_000,
    )
    assert proof["media_remote_blocks"] == 1
    assert proof["media_reserve_tokens"] == 1024


def test_small_image_keeps_nonzero_reserve_and_pdf_keeps_byte_reserve(
    png_encodings: dict[str, str],
) -> None:
    assert estimate_provider_media_tokens("image", 0, encoded_data=png_encodings["small"]) == 1024
    assert estimate_provider_media_tokens("pdf", 5 * 1024 * 1024) == 45_056


def test_canonical_document_uses_the_same_reserve_as_anthropic_document() -> None:
    data = base64.b64encode(b"synthetic document" * 10).decode("ascii")
    canonical = ContentBlockDocument(media_type="application/pdf", data=data)
    reserves = []
    for block in (
        canonical.model_dump(exclude_none=True),
        {"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf", "data": data,
        }},
    ):
        proof = project_provider_payload(
            {"messages": [{"role": "user", "content": [block]}]},
            projection_adapter="synthetic",
            proof_budget=50_000,
        )
        reserves.append(proof["media_reserve_tokens"])
    assert reserves[0] == reserves[1]


def test_canonical_image_shape_in_tool_arguments_remains_text(
    png_encodings: dict[str, str],
) -> None:
    block = ContentBlockToolUse(
        id="synthetic-call",
        name="record",
        input=ContentBlockImage(
            media_type="image/png", data=png_encodings["uhd_uncompressed"],
        ).model_dump(),
    )
    proof = project_provider_payload(
        {"messages": [{"role": "assistant", "content": [block.model_dump()]}]},
        projection_adapter="synthetic",
        proof_budget=50_000,
    )
    assert "media_blocks_reserved" not in proof
    assert not proof["fits"]
