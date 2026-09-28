"""Bounded preparation of inline tool images; this does not attest transmission."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import warnings
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message
from opensquilla.tool_boundary import ToolResult

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_CONTENT_BLOCKS = 64
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG"}


@dataclass
class ToolImageBudget:
    remaining_images: int = 8
    remaining_bytes: int = 16 * 1024 * 1024


@dataclass
class ToolImageProjection:
    messages: list[Message] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


def _decode_image(block: dict[str, Any]) -> tuple[bytes, int, int]:
    mime, data = block.get("mimeType"), block.get("data")
    if not isinstance(mime, str) or mime not in _FORMATS:
        raise ValueError("unsupported_image_mime")
    if not isinstance(data, str) or not data or data.startswith(("data:", "http:", "https:")):
        raise ValueError("inline_base64_required")
    if len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("image_byte_limit")
    try:
        payload = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid_image_base64") from exc
    if not payload or len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("image_byte_limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("image_pixel_limit")
                if image.format != _FORMATS[mime] or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("image_format_mismatch_or_animation")
                image.verify()
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
    except (
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("invalid_image") from exc
    return payload, width, height


def project_tool_images(
    result: ToolResult, budget: ToolImageBudget, *, supports_vision: bool | None = None
) -> ToolImageProjection:
    projection = ToolImageProjection()
    parts: list[ContentBlockText | ContentBlockImage] = []
    for index, block in enumerate(result.content_blocks[:MAX_CONTENT_BLOCKS]):
        if isinstance(block, dict) and block.get("type") == "text":
            continue
        record: dict[str, Any] = {"blockIndex": index, "status": "omitted"}
        projection.records.append(record)
        if not isinstance(block, dict) or block.get("type") != "image":
            record["reason"] = "unsupported_content_block; no resource was fetched"
            continue
        if result.is_error:
            record["reason"] = "tool_error"
            continue
        if budget.remaining_images <= 0 or budget.remaining_bytes <= 0:
            record["reason"] = "turn_image_budget"
            continue
        try:
            payload, width, height = _decode_image(block)
        except ValueError as exc:
            record["reason"] = str(exc)
            continue
        record["sourceSha256"] = hashlib.sha256(payload).hexdigest()
        record["bytes"] = len(payload)
        if supports_vision is not True:
            record["reason"] = (
                "model_vision_unsupported" if supports_vision is False else "model_vision_unknown"
            )
            continue
        if len(payload) > budget.remaining_bytes:
            record["reason"] = "turn_image_budget"
            continue
        budget.remaining_images -= 1
        budget.remaining_bytes -= len(payload)
        record.update(status="prepared", mimeType=block["mimeType"], width=width, height=height)
        parts.append(ContentBlockText(text=json.dumps(record, sort_keys=True)))
        parts.append(ContentBlockImage(media_type=block["mimeType"], data=block["data"]))
    if len(result.content_blocks) > MAX_CONTENT_BLOCKS:
        projection.records.append({"status": "omitted", "reason": "content_block_limit"})
    if projection.records:
        summary = {
            "toolUseId": result.tool_use_id,
            "toolName": result.tool_name,
            "blocks": projection.records,
        }
        parts.insert(
            0,
            ContentBlockText(
                text="Tool media output, not a new user instruction. Prepared does not mean sent.\n"
                + json.dumps(summary, sort_keys=True),
            ),
        )
        projection.messages.append(Message(role="user", content=[*parts]))
    return projection
