"""Bound a tool image before it becomes provider-visible replay content."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, features

from opensquilla.contracts.attachments import normalize_attachment_mime
from opensquilla.contracts.image_validation import validate_image_bytes

MAX_TOOL_IMAGE_DIMENSION = 2048
MAX_TOOL_IMAGE_BASE64_CHARS = 1536 * 1024
_LOSSY_QUALITIES = (88, 76, 64, 52, 40, 28)


@dataclass(frozen=True, slots=True)
class PreparedToolImage:
    """Provider image bytes and the facts needed to describe any transformation."""

    data: bytes
    media_type: str
    changed: bool
    lossy: bool
    original_size: int
    encoded_chars: int


def _encoded_chars(payload: bytes) -> int:
    return ((len(payload) + 2) // 3) * 4


def _encode(image: Image.Image, *, format_name: str, **options: object) -> bytes:
    output = io.BytesIO()
    image.save(output, format=format_name, **options)
    return output.getvalue()


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info


def _lossless_candidate(image: Image.Image, mime: str) -> tuple[bytes, str, bool]:
    if mime == "image/jpeg":
        return (
            _encode(
                image.convert("RGB"), format_name="JPEG", quality=92, subsampling=0, optimize=True
            ),
            mime,
            True,
        )
    if mime == "image/webp":
        mode = "RGBA" if _has_alpha(image) else "RGB"
        return (
            _encode(image.convert(mode), format_name="WEBP", lossless=True, method=6),
            mime,
            False,
        )
    # A transformed single-frame GIF is saved as PNG so palette quantization
    # does not silently discard colors or transparency.
    mode = "RGBA" if _has_alpha(image) else "RGB"
    return _encode(image.convert(mode), format_name="PNG", optimize=True), "image/png", False


def _lossy_candidates(image: Image.Image) -> tuple[str, Image.Image]:
    if _has_alpha(image):
        return "image/webp", image.convert("RGBA")
    return "image/jpeg", image.convert("RGB")


def prepare_tool_result_image(payload: bytes, mime: str) -> PreparedToolImage:
    """Return a valid image bounded for a single model tool result.

    Source bytes are never mutated. A caller with a readable source path may
    retain that path as the original while placing these prepared bytes in the
    provider replay. Animated images remain unchanged or fail explicitly; a
    first-frame conversion would misrepresent what the model received.
    """

    normalized_mime = normalize_attachment_mime(mime)
    if normalized_mime is None:
        raise ValueError("tool image media type is missing")
    validate_image_bytes(payload, normalized_mime)
    with Image.open(io.BytesIO(payload)) as source:
        dimensions = source.size
        needs_resize = max(dimensions) > MAX_TOOL_IMAGE_DIMENSION
        if not needs_resize and _encoded_chars(payload) <= MAX_TOOL_IMAGE_BASE64_CHARS:
            return PreparedToolImage(
                data=payload,
                media_type=normalized_mime,
                changed=False,
                lossy=False,
                original_size=len(payload),
                encoded_chars=_encoded_chars(payload),
            )
        if getattr(source, "n_frames", 1) > 1:
            raise ValueError("animated tool image exceeds the request image limit")
        image = ImageOps.exif_transpose(source)
        image.load()
        if needs_resize:
            image.thumbnail(
                (MAX_TOOL_IMAGE_DIMENSION, MAX_TOOL_IMAGE_DIMENSION),
                Image.Resampling.LANCZOS,
            )

    try:
        lossless, lossless_mime, lossless_is_lossy = _lossless_candidate(image, normalized_mime)
    except (KeyError, OSError, ValueError) as exc:
        raise ValueError("tool image could not be prepared within the request limit") from exc
    if _encoded_chars(lossless) <= MAX_TOOL_IMAGE_BASE64_CHARS:
        validate_image_bytes(lossless, lossless_mime)
        return PreparedToolImage(
            data=lossless,
            media_type=lossless_mime,
            changed=True,
            lossy=lossless_is_lossy,
            original_size=len(payload),
            encoded_chars=_encoded_chars(lossless),
        )

    output_mime, converted = _lossy_candidates(image)
    if output_mime == "image/webp" and not features.check("webp"):
        raise ValueError("transparent tool image exceeds the limit and WebP encoder is unavailable")
    format_name = "WEBP" if output_mime == "image/webp" else "JPEG"
    try:
        for _ in range(12):
            for quality in _LOSSY_QUALITIES:
                options: dict[str, object] = {"quality": quality}
                if format_name == "JPEG":
                    options["optimize"] = True
                else:
                    # Fast WebP encoding avoids a long event-loop stall when a
                    # tool returns several contact sheets in one batch.
                    options["method"] = 0
                candidate = _encode(converted, format_name=format_name, **options)
                if _encoded_chars(candidate) <= MAX_TOOL_IMAGE_BASE64_CHARS:
                    validate_image_bytes(candidate, output_mime)
                    return PreparedToolImage(
                        data=candidate,
                        media_type=output_mime,
                        changed=True,
                        lossy=True,
                        original_size=len(payload),
                        encoded_chars=_encoded_chars(candidate),
                    )
            width, height = converted.size
            smaller = (max(1, width * 4 // 5), max(1, height * 4 // 5))
            if smaller == converted.size:
                break
            converted = converted.resize(smaller, Image.Resampling.LANCZOS)
    except (KeyError, OSError, ValueError) as exc:
        raise ValueError("tool image could not be prepared within the request limit") from exc
    raise ValueError("tool image could not be prepared within the request limit")
