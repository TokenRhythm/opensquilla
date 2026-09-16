from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image

from opensquilla.contracts.image_validation import validate_image_bytes
from tests.helpers.image_bytes import image_bytes


@pytest.mark.parametrize("format", ["PNG", "JPEG", "GIF", "WEBP"])
def test_supported_image_content_is_decodable(format: str) -> None:
    validate_image_bytes(image_bytes(format), Image.MIME[format])


@pytest.mark.parametrize(
    ("payload", "mime"),
    [
        (b"\x89PNG\r\n\x1a\ninvalid image body", "image/png"),
        (b"plain text renamed to a photograph", "image/jpeg"),
        (b"\xff\xd8\xffinvalid image body", "image/jpeg"),
        (b"GIF89ainvalid image body", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPinvalid image body", "image/webp"),
        (image_bytes("JPEG")[:-30], "image/jpeg"),
        (image_bytes("BMP"), "image/png"),
        (image_bytes("PNG"), "image/jpeg"),
    ],
)
def test_invalid_image_content_is_rejected(payload: bytes, mime: str) -> None:
    with pytest.raises(ValueError, match="upload a valid image"):
        validate_image_bytes(payload, mime)


def test_png_with_valid_chunk_checksums_but_invalid_pixel_data_is_rejected() -> None:
    payload = bytearray(image_bytes())
    offset = payload.index(b"IDAT")
    length = struct.unpack(">I", payload[offset - 4 : offset])[0]
    payload[offset + 4 : offset + 4 + length] = b"x" * length
    payload[offset + 4 + length : offset + 8 + length] = struct.pack(
        ">I", zlib.crc32(payload[offset : offset + 4 + length])
    )
    # Structural verification alone accepts these checksummed chunks.
    with Image.open(io.BytesIO(payload)) as image:
        image.verify()
    with pytest.raises(ValueError, match="corrupt"):
        validate_image_bytes(bytes(payload), "image/png")


def test_image_pixel_limit_is_enforced(monkeypatch) -> None:
    payload = image_bytes()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 3)
    with pytest.raises(ValueError, match="upload a valid image"):
        validate_image_bytes(payload, "image/png")


def test_windows_jpeg_mime_alias_remains_supported() -> None:
    validate_image_bytes(image_bytes("JPEG"), "image/jpg")
