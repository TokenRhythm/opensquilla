"""Validate image content before admitting it as a model image input."""

from __future__ import annotations

import io
import warnings

from PIL import Image

from opensquilla.contracts.attachments import IMAGE_ATTACHMENT_MIMES, normalize_attachment_mime


def validate_image_bytes(payload: bytes, media_type: str) -> None:
    """Require a supported image with matching MIME and decodable pixel data.

    Callers enforce their byte limits before decoding. Header sniffing remains
    separate so a recognized image header cannot stand in for content validation.
    """

    expected = normalize_attachment_mime(media_type)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                actual = Image.MIME.get(image.format or "")
                if actual not in IMAGE_ATTACHMENT_MIMES or actual != expected:
                    raise ValueError("image format does not match the declared media type")
                image.verify()
            # Some formats only check their header in verify(); decoding catches
            # truncated or invalid pixel data as well.
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
    except (
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError(
            f"claims {media_type} but image content is corrupt, unreadable, or mismatched "
            "(415 equivalent); please upload a valid image"
        ) from exc
