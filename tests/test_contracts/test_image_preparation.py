"""Synthetic tool-image preparation checks."""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image, features

from opensquilla.contracts import image_preparation
from opensquilla.contracts.image_preparation import (
    MAX_TOOL_IMAGE_BASE64_CHARS,
    MAX_TOOL_IMAGE_DIMENSION,
    prepare_tool_result_image,
)
from opensquilla.contracts.image_validation import validate_image_bytes


def _image_bytes(image: Image.Image, format_name: str) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


def test_small_image_keeps_exact_original_bytes() -> None:
    original = _image_bytes(Image.new("RGB", (24, 16), "#365a9c"), "PNG")

    prepared = prepare_tool_result_image(original, "image/png")

    assert prepared.data == original
    assert prepared.media_type == "image/png"
    assert prepared.changed is False
    assert prepared.lossy is False
    assert prepared.encoded_chars == ((len(original) + 2) // 3) * 4


def test_large_dimension_is_resized_and_remains_valid() -> None:
    original = _image_bytes(Image.new("RGBA", (3072, 1024), "#365a9c88"), "PNG")

    prepared = prepare_tool_result_image(original, "image/png")

    assert prepared.changed is True
    assert prepared.lossy is False
    assert prepared.media_type == "image/png"
    assert prepared.encoded_chars <= MAX_TOOL_IMAGE_BASE64_CHARS
    validate_image_bytes(prepared.data, prepared.media_type)
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert max(image.size) <= MAX_TOOL_IMAGE_DIMENSION
        assert image.getpixel((0, 0))[3] == 136


def test_over_budget_static_png_uses_bounded_lossy_image() -> None:
    pixels = random.Random(47).randbytes(1024 * 1024 * 3)
    original = _image_bytes(Image.frombytes("RGB", (1024, 1024), pixels), "PNG")
    assert ((len(original) + 2) // 3) * 4 > MAX_TOOL_IMAGE_BASE64_CHARS

    prepared = prepare_tool_result_image(original, "image/png")

    assert prepared.changed is True
    assert prepared.lossy is True
    assert prepared.media_type == "image/jpeg"
    assert prepared.encoded_chars <= MAX_TOOL_IMAGE_BASE64_CHARS
    validate_image_bytes(prepared.data, prepared.media_type)
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.size == (1024, 1024)


def test_over_budget_transparent_png_preserves_alpha() -> None:
    if not features.check("webp"):
        pytest.skip("Pillow WebP encoder unavailable")
    pixels = random.Random(71).randbytes(768 * 768 * 4)
    original = _image_bytes(Image.frombytes("RGBA", (768, 768), pixels), "PNG")
    assert ((len(original) + 2) // 3) * 4 > MAX_TOOL_IMAGE_BASE64_CHARS

    prepared = prepare_tool_result_image(original, "image/png")

    assert prepared.media_type == "image/webp"
    assert prepared.lossy is True
    assert prepared.encoded_chars <= MAX_TOOL_IMAGE_BASE64_CHARS
    validate_image_bytes(prepared.data, prepared.media_type)
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.mode == "RGBA"
        assert image.getchannel("A").getextrema() == (0, 255)


def test_transparent_image_fails_explicitly_without_webp_encoder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pixels = random.Random(72).randbytes(32 * 32 * 4)
    original = _image_bytes(Image.frombytes("RGBA", (32, 32), pixels), "PNG")
    monkeypatch.setattr(image_preparation, "MAX_TOOL_IMAGE_BASE64_CHARS", 256)
    monkeypatch.setattr(image_preparation.features, "check", lambda _name: False)

    with pytest.raises(ValueError, match="WebP encoder is unavailable"):
        prepare_tool_result_image(original, "image/png")


@pytest.mark.parametrize("encoder_error", [KeyError("WEBP"), OSError("encoder unavailable")])
def test_transparent_image_wraps_webp_encoder_failure(
    monkeypatch: pytest.MonkeyPatch,
    encoder_error: Exception,
) -> None:
    pixels = random.Random(73).randbytes(32 * 32 * 4)
    original = _image_bytes(Image.frombytes("RGBA", (32, 32), pixels), "PNG")
    monkeypatch.setattr(image_preparation, "MAX_TOOL_IMAGE_BASE64_CHARS", 256)
    monkeypatch.setattr(image_preparation.features, "check", lambda _name: True)
    encode = image_preparation._encode

    def failing_webp(image: Image.Image, *, format_name: str, **options: object) -> bytes:
        if format_name == "WEBP":
            raise encoder_error
        return encode(image, format_name=format_name, **options)

    monkeypatch.setattr(image_preparation, "_encode", failing_webp)

    with pytest.raises(ValueError, match="could not be prepared"):
        prepare_tool_result_image(original, "image/png")


def test_oversized_animation_is_not_silently_flattened() -> None:
    first = Image.new("RGB", (2049, 2), "red")
    second = Image.new("RGB", (2049, 2), "blue")
    output = io.BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100)

    with pytest.raises(ValueError, match="animated"):
        prepare_tool_result_image(output.getvalue(), "image/gif")


def test_small_animation_keeps_all_frames() -> None:
    first = Image.new("RGB", (16, 16), "red")
    second = Image.new("RGB", (16, 16), "blue")
    output = io.BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=100)

    prepared = prepare_tool_result_image(output.getvalue(), "image/gif")

    assert prepared.changed is False
    assert prepared.data == output.getvalue()
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.n_frames == 2


def test_mismatched_mime_is_rejected() -> None:
    original = _image_bytes(Image.new("RGB", (8, 8), "red"), "PNG")

    with pytest.raises(ValueError, match="mismatched"):
        prepare_tool_result_image(original, "image/jpeg")
