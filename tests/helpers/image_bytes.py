"""Small valid images generated entirely from synthetic test pixels."""

from __future__ import annotations

import io

from PIL import Image


def image_bytes(format: str = "PNG", *, color: str = "blue") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(buffer, format=format)
    return buffer.getvalue()
