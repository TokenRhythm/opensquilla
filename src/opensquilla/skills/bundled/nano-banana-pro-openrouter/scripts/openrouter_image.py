#!/usr/bin/env python3
"""Deterministic OpenRouter image adapter for Nano Banana Pro."""

from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from opensquilla.skills.bundled._provider_http import (
    download_public_https_bytes,
    open_authenticated_request,
    read_limited_response,
    same_http_origin,
)

SAFE_NO_SUBMIT_EXIT_CODE = 78
META_CAPABILITY_LEASE_REQUIRED_ENV = "OPENSQUILLA_META_CAPABILITY_LEASE_REQUIRED"
META_CAPABILITY_PROVIDER_ENV = "OPENSQUILLA_META_CAPABILITY_PROVIDER"
META_CAPABILITY_API_KEY_ENV = "OPENSQUILLA_META_CAPABILITY_API_KEY"
META_CAPABILITY_BASE_URL_ENV = "OPENSQUILLA_META_CAPABILITY_BASE_URL"
META_CAPABILITY_PROXY_ENV = "OPENSQUILLA_META_CAPABILITY_PROXY"
MAX_PROVIDER_JSON_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_IMAGE_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 25_000_000
MIN_IMAGE_DIMENSION = 64


def _emit(label: str, payload: dict[str, Any]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}")


def _clean_slot(raw: str) -> str:
    slot = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw.strip()).strip("-").lower()
    return slot[:64] or "image"


def _extract_field(block: str, name: str) -> str:
    match = re.search(rf"{name}\s*[:：]\s*(.+)", block, re.I)
    if not match:
        return ""
    value = match.group(1).strip().strip("`\"'")
    return value[:500]


def _is_image_modality(value: str) -> bool:
    text = value.strip().lower()
    return bool(re.search(r"\bimage\b", text)) or any(
        marker in value for marker in ("图片", "图像", "影像")
    )


def _header_index(headers: list[str], *names: str) -> int | None:
    normalized = [re.sub(r"[\s_\-/]+", "", h.strip().lower()) for h in headers]
    for needle in names:
        needle_norm = re.sub(r"[\s_\-/]+", "", needle.strip().lower())
        for index, header in enumerate(normalized):
            if needle_norm and needle_norm in header:
                return index
    return None


def _cell(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip().strip("`\"'")


def _append_slot(
    slots: list[dict[str, str]],
    seen: set[str],
    *,
    slot_id: str,
    subject: str = "",
    prompt_hint: str = "",
    keywords: str = "",
) -> None:
    cleaned = _clean_slot(slot_id)
    if cleaned in seen:
        return
    seen.add(cleaned)
    slots.append(
        {
            "slot_id": cleaned,
            "subject": (subject or cleaned.replace("-", " "))[:500],
            "prompt_hint": prompt_hint[:500],
            "keywords": keywords[:500],
        }
    )


def _extract_field_blocks(outline: str, slots: list[dict[str, str]], seen: set[str]) -> None:
    matches = list(
        re.finditer(r"slot_id\s*[:：]\s*([a-zA-Z0-9][a-zA-Z0-9_-]{0,80})", outline, re.I)
    )
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else min(
            len(outline), match.start() + 900
        )
        block = outline[match.start() : end]
        modality = _extract_field(block, "modality")
        if not _is_image_modality(modality):
            continue
        subject = (
            _extract_field(block, "subject")
            or _extract_field(block, "placement")
        )
        _append_slot(
            slots,
            seen,
            slot_id=match.group(1),
            subject=subject,
            prompt_hint=_extract_field(block, "prompt_hint"),
            keywords=_extract_field(block, "search_keywords"),
        )


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _looks_like_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells)


def _extract_table_rows(outline: str, slots: list[dict[str, str]], seen: set[str]) -> None:
    headers: list[str] | None = None
    for raw_line in outline.splitlines():
        cells = _split_table_row(raw_line)
        if not cells:
            headers = None
            continue
        if _looks_like_separator(cells):
            continue

        if headers is None:
            lower_cells = [cell.lower() for cell in cells]
            has_slot = any("slot" in cell or "槽位" in cell for cell in lower_cells + cells)
            has_modality = any(
                "modality" in cell
                or "媒体" in cell
                or "模态" in cell
                or "类型" in cell
                for cell in lower_cells + cells
            )
            if has_slot and has_modality:
                headers = cells
            continue

        slot_idx = _header_index(headers, "slot_id", "slot id", "slot", "槽位")
        modality_idx = _header_index(headers, "modality", "media", "媒体", "模态", "类型")
        if slot_idx is None or modality_idx is None:
            continue
        if not _is_image_modality(_cell(cells, modality_idx)):
            continue
        slot_id = _cell(cells, slot_idx)
        if not slot_id:
            match = re.search(r"\b([a-zA-Z0-9][a-zA-Z0-9_-]{1,80})\b", raw_line)
            slot_id = match.group(1) if match else ""
        if not slot_id:
            continue
        subject_idx = _header_index(
            headers,
            "subject",
            "placement",
            "section",
            "visual",
            "画面",
            "主题",
            "位置",
            "章节",
        )
        prompt_idx = _header_index(headers, "prompt_hint", "prompt", "hint", "描述", "构图")
        keyword_idx = _header_index(headers, "search_keywords", "keywords", "关键词")
        _append_slot(
            slots,
            seen,
            slot_id=slot_id,
            subject=_cell(cells, subject_idx),
            prompt_hint=_cell(cells, prompt_idx),
            keywords=_cell(cells, keyword_idx),
        )


def _extract_inline_rows(outline: str, slots: list[dict[str, str]], seen: set[str]) -> None:
    for line in outline.splitlines():
        if not _is_image_modality(line):
            continue
        match = re.search(
            r"slot[_\s-]*id\s*[:：=]\s*([a-zA-Z0-9][a-zA-Z0-9_-]{0,80})",
            line,
            re.I,
        )
        if not match:
            continue
        _append_slot(slots, seen, slot_id=match.group(1), subject=line[:500])


def _extract_slots(outline: str) -> list[dict[str, str]]:
    slots: list[dict[str, str]] = []
    seen: set[str] = set()
    _extract_field_blocks(outline, slots, seen)
    _extract_table_rows(outline, slots, seen)
    _extract_inline_rows(outline, slots, seen)
    return slots


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    candidates = [text]
    if text.startswith("exit_code=0\n"):
        candidates.append(text.split("\n", 1)[1].strip())
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _slot_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, list):
            text = " ".join(str(item).strip() for item in value if str(item).strip())
        else:
            text = str(value or "").strip()
        if text:
            return text[:500]
    return ""


def _coerce_slot(raw: dict[str, Any], fallback_id: str) -> dict[str, str] | None:
    modality = str(raw.get("modality") or raw.get("media") or raw.get("type") or "").strip()
    if modality and not _is_image_modality(modality):
        return None
    slot_id = _slot_text(raw, "slot_id", "slot", "id", "name") or fallback_id
    subject = _slot_text(raw, "subject", "description", "alt", "placement", "role")
    prompt_hint = _slot_text(raw, "prompt_hint", "prompt", "hint", "composition")
    keywords = _slot_text(raw, "search_keywords", "keywords", "tags")
    return {
        "slot_id": _clean_slot(slot_id),
        "subject": subject or _clean_slot(slot_id).replace("-", " "),
        "prompt_hint": prompt_hint,
        "keywords": keywords,
    }


def _coerce_slots(value: Any) -> list[dict[str, str]]:
    data = _json_value(value)
    if isinstance(data, dict):
        if isinstance(data.get("slots"), list):
            candidates = data["slots"]
        elif isinstance(data.get("image_slots"), list):
            candidates = data["image_slots"]
        else:
            candidates = []
    elif isinstance(data, list):
        candidates = data
    else:
        candidates = []

    slots: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(candidates, start=1):
        if not isinstance(item, dict):
            continue
        slot = _coerce_slot(item, f"image-{index}")
        if slot is None or slot["slot_id"] in seen:
            continue
        seen.add(slot["slot_id"])
        slots.append(slot)
    return slots


def _payload_slots(payload: dict[str, Any]) -> list[dict[str, str]]:
    for key in ("image_slots", "media_slots", "slots"):
        slots = _coerce_slots(payload.get(key))
        if slots:
            return slots
    return []


def _images_requested(payload: dict[str, Any]) -> bool:
    value = str(payload.get("include_images", "YES")).strip().lower()
    return value not in {"no", "false", "0", "n", "否", "不要", "不需要"}


def _brief_topic(payload: dict[str, Any]) -> str:
    text = "\n".join(
        str(payload.get(key) or "")
        for key in ("requirement_framing", "page_outline", "visual_style")
    )
    text = re.sub(r"[#*_`|>{}\[\]\"]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "requested webpage topic"
    for marker in ("主题", "topic", "brief", "request", "需求"):
        match = re.search(rf"{marker}\s*[:：]\s*([^。.\n|]{{6,120}})", text, re.I)
        if match:
            return match.group(1).strip()[:160]
    return text[:160]


def _fallback_slots(payload: dict[str, Any]) -> list[dict[str, str]]:
    if not _images_requested(payload):
        return []
    topic = _brief_topic(payload)
    return [
        {
            "slot_id": "hero-visual",
            "subject": f"hero image representing {topic}",
            "prompt_hint": "wide 16:9 polished webpage hero, documentary clarity, no text overlays",
            "keywords": topic,
        },
        {
            "slot_id": "supporting-visual",
            "subject": f"supporting explanatory visual for {topic}",
            "prompt_hint": (
                "clean editorial webpage illustration/photo, balanced composition, "
                "no text overlays"
            ),
            "keywords": topic,
        },
    ]


def _ready_slots(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(r"^IMAGE_READY:\s*(\{.*\})\s*$", text or "", re.M):
        try:
            payload = json.loads(match.group(1))
        except Exception:
            continue
        slot_id = _clean_slot(str(payload.get("slot_id") or ""))
        if slot_id:
            found.add(slot_id)
    return found


def _incomplete_slots(text: str) -> set[str]:
    for match in re.finditer(r"^IMAGE_DOWNLOAD_INCOMPLETE:\s*(\{.*\})\s*$", text or "", re.M):
        try:
            payload = json.loads(match.group(1))
        except Exception:
            continue
        slot_ids = payload.get("unfilled_slot_ids")
        if isinstance(slot_ids, list):
            return {_clean_slot(str(x)) for x in slot_ids if str(x).strip()}
    return set()


def _api_url(base_url: str, path: str) -> str:
    if base_url.endswith("/v1") and path.startswith("/v1/"):
        return base_url + path[3:]
    return base_url + path


def _resolve_url(url: str, *, base_url: str) -> str:
    return urllib.parse.urljoin(f"{base_url.rstrip('/')}/", url)


def _same_origin(url: str, *, base_url: str) -> bool:
    return same_http_origin(url, base_url)


def _runtime_connection(args: argparse.Namespace) -> tuple[str, str, str, bool]:
    """Resolve a MetaSkill lease or the direct-CLI compatibility inputs."""

    lease_required = os.environ.get(META_CAPABILITY_LEASE_REQUIRED_ENV) == "1"
    if lease_required:
        provider = os.environ.get(META_CAPABILITY_PROVIDER_ENV, "").strip().lower()
        if provider != "openrouter":
            return "", "", "", True
        return (
            os.environ.get(META_CAPABILITY_API_KEY_ENV, "").strip(),
            os.environ.get(META_CAPABILITY_BASE_URL_ENV, "").strip().rstrip("/"),
            os.environ.get(META_CAPABILITY_PROXY_ENV, "").strip(),
            True,
        )
    api_key_env = args.api_key_env.strip() or "OPENROUTER_API_KEY"
    return (
        args.api_key.strip() or os.environ.get(api_key_env, ""),
        args.base_url.strip().rstrip("/"),
        "",
        False,
    )


def _open_url(request: urllib.request.Request, *, timeout: float, proxy: str):
    return open_authenticated_request(
        request,
        timeout=timeout,
        proxy=proxy,
    )


def _extract_image_url(data: dict[str, Any]) -> str | None:
    for choice in data.get("choices") or []:
        message = choice.get("message") or {}
        for image in message.get("images") or []:
            image_url = image.get("image_url") or image.get("imageUrl") or {}
            url = image_url.get("url")
            if isinstance(url, str) and url:
                return url
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                image_url = item.get("image_url") or item.get("imageUrl") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if isinstance(url, str) and url:
                    return url
    return None


def _decode_image(
    url: str,
    api_key: str,
    *,
    base_url: str,
    proxy: str = "",
) -> tuple[str, bytes]:
    if url.startswith("data:"):
        prefix, sep, encoded = url.partition(",")
        if not sep or ";base64" not in prefix:
            raise RuntimeError("unsupported_data_url")
        mime = prefix.removeprefix("data:").split(";", 1)[0] or "image/png"
        if len(encoded) > (MAX_IMAGE_DOWNLOAD_BYTES * 4 // 3) + 4:
            raise RuntimeError("image_payload_too_large")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise RuntimeError("invalid_data_url") from None
        if len(decoded) > MAX_IMAGE_DOWNLOAD_BYTES:
            raise RuntimeError("image_payload_too_large")
        return mime, decoded
    resolved_url = _resolve_url(url, base_url=base_url)
    body = download_public_https_bytes(
        resolved_url,
        timeout=45,
        max_bytes=MAX_IMAGE_DOWNLOAD_BYTES,
        proxy=proxy,
        authorization="Bearer " + api_key if api_key else "",
        authorization_base_url=base_url,
    )
    # Remote response headers are deliberately not trusted to choose a parser
    # or extension.  Existing payload validation below verifies image bytes;
    # PNG remains the compatibility default for URL-based responses.
    return "image/png", body


def _extension_for_mime(mime: str) -> str:
    if mime == "image/jpeg":
        return ".jpg"
    if mime == "image/webp":
        return ".webp"
    if mime == "image/gif":
        return ".gif"
    return ".png"


def _detected_image_mime(payload: bytes) -> str | None:
    """Recognize the bounded raster formats this adapter can safely publish."""

    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validated_image_mime(payload: bytes) -> str:
    """Return the decoded raster MIME only after a complete local decode."""

    detected_mime = _detected_image_mime(payload)
    if detected_mime is None:
        raise RuntimeError("invalid_image_payload")
    expected_format = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/gif": "GIF",
        "image/webp": "WEBP",
    }[detected_mime]
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise RuntimeError("local_image_validation_unavailable") from None
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            actual_format = str(probe.format or "").upper()
            width, height = probe.size
            frame_count = int(getattr(probe, "n_frames", 1) or 1)
            if actual_format != expected_format:
                raise RuntimeError("invalid_image_payload")
            if (
                width < MIN_IMAGE_DIMENSION
                or height < MIN_IMAGE_DIMENSION
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
                or frame_count != 1
            ):
                raise RuntimeError("invalid_image_payload")
            probe.verify()
        with Image.open(io.BytesIO(payload)) as decoded:
            decoded.load()
    except RuntimeError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise RuntimeError("invalid_image_payload") from None
    return detected_mime


def _scrub_error(exc: object, api_key: str) -> str:
    text = str(exc)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    return re.sub(r"\s+", " ", text)[:220]


def _build_slot_prompt(slot: dict[str, str], *, requirement: str, visual_style: str) -> str:
    parts = [
        f"Create one high-quality webpage image for slot `{slot['slot_id']}`.",
        f"Subject: {slot['subject']}",
    ]
    if slot.get("prompt_hint"):
        parts.append(f"Composition/style hint: {slot['prompt_hint']}")
    if slot.get("keywords"):
        parts.append(f"Relevant keywords: {slot['keywords']}")
    if visual_style:
        parts.append(f"Overall webpage visual style: {visual_style}")
    parts.append(
        "No text overlays, no logos, no watermarks. "
        "Suitable for a polished public webpage."
    )
    if requirement:
        parts.append("Project brief excerpt: " + requirement[:600])
    return "\n".join(parts)


def _generate_one(
    *,
    slot_id: str,
    prompt: str,
    model: str,
    base_url: str,
    api_key: str,
    output_dir: Path,
    local_path_prefix: str,
    resolution: str,
    proxy: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
        "stream": False,
        "image_config": {"aspect_ratio": "16:9", "image_size": resolution},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _api_url(base_url, "/v1/chat/completions"),
        data=body,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "X-Title": "OpenSquilla Nano Banana Pro OpenRouter",
        },
        method="POST",
    )
    try:
        with _open_url(req, timeout=90, proxy=proxy) as resp:
            raw = read_limited_response(
                resp,
                max_bytes=MAX_PROVIDER_JSON_RESPONSE_BYTES,
                error_message="provider JSON response exceeds size limit",
            )
            data = json.loads(raw.decode("utf-8"))
        image_url = _extract_image_url(data)
        if not image_url:
            raise RuntimeError("provider_returned_no_image")
        mime, image_bytes = _decode_image(
            image_url,
            api_key,
            base_url=base_url,
            proxy=proxy,
        )
        mime = _validated_image_mime(image_bytes)
        ext = _extension_for_mime(mime)
        filename = _clean_slot(slot_id) + ext
        out_path = (output_dir / filename).resolve()
        out_path.relative_to(output_dir)
        out_path.write_bytes(image_bytes)
        return {
            "ok": True,
            "slot_id": _clean_slot(slot_id),
            "local_path": f"{local_path_prefix.rstrip('/')}/{filename}",
            "mime": mime,
            "bytes": len(image_bytes),
            "prompt_preview": prompt[:80],
        }
    except urllib.error.HTTPError as exc:
        # Provider error bodies are untrusted and can reflect prompts, signed
        # URLs, request metadata, or other user content. Persist only the
        # status code in the MetaSkill result/transcript.
        return {
            "ok": False,
            "slot_id": _clean_slot(slot_id),
            "reason": f"http_{exc.code}",
        }
    except Exception as exc:
        return {"ok": False, "slot_id": _clean_slot(slot_id), "reason": _scrub_error(exc, api_key)}


def _load_stdin() -> tuple[dict[str, Any] | None, str]:
    raw = sys.stdin.read()
    text = raw.strip()
    if not text:
        return None, ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None, text
    return value if isinstance(value, dict) else None, text


def _target_slots(payload: dict[str, Any], max_images: int) -> list[dict[str, str]]:
    outline = str(payload.get("page_outline") or "")
    all_slots = _payload_slots(payload) or _extract_slots(outline)
    if not all_slots:
        all_slots = _fallback_slots(payload)
    download_outcome = str(payload.get("image_download") or "")
    existing = _ready_slots(download_outcome)
    requested_missing = _incomplete_slots(download_outcome)
    if requested_missing:
        slots = [
            slot
            for slot in all_slots
            if slot["slot_id"] in requested_missing and slot["slot_id"] not in existing
        ]
    elif existing:
        slots = [slot for slot in all_slots if slot["slot_id"] not in existing]
    else:
        slots = all_slots
    return slots[:max_images]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--filename", default="image.png")
    parser.add_argument("--resolution", choices=["1K", "2K", "4K"], default="1K")
    parser.add_argument("--max-images", type=int, default=6)
    parser.add_argument("--local-path-prefix", default="project/assets/images")
    args = parser.parse_args()

    api_key, base_url, proxy, lease_required = _runtime_connection(args)
    replacement_slot = (
        f"{args.local_path_prefix.rstrip('/')}/replace-with-generated-image.png"
    )
    if not args.model.strip():
        _emit(
            "IMAGE_CONFIG_NEEDED",
            {
                "missing": ["awesome_webpage.openrouter.models.image_generation"],
                "reason": "missing_image_model",
                "replacement_slot": replacement_slot,
            },
        )
        return SAFE_NO_SUBMIT_EXIT_CODE if lease_required else 0
    if not api_key:
        _emit(
            "IMAGE_CONFIG_NEEDED",
            {
                "missing": [
                    "provider_connection:openrouter"
                    if lease_required
                    else (args.api_key_env.strip() or "OPENROUTER_API_KEY")
                ],
                "reason": "missing_api_key",
                "replacement_slot": replacement_slot,
            },
        )
        return SAFE_NO_SUBMIT_EXIT_CODE if lease_required else 0
    if not base_url:
        _emit(
            "IMAGE_CONFIG_NEEDED",
            {
                "missing": ["provider_endpoint:openrouter"],
                "reason": "missing_provider_endpoint",
                "replacement_slot": replacement_slot,
            },
        )
        return SAFE_NO_SUBMIT_EXIT_CODE if lease_required else 0

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload, raw_text = _load_stdin()

    if payload is not None and (
        "page_outline" in payload
        or "media_slots" in payload
        or "image_slots" in payload
        or "slots" in payload
    ):
        slots = _target_slots(payload, max(1, args.max_images))
        if not slots:
            _emit(
                "IMAGE_GENERATION_FAILED",
                {
                    "reason": "no_image_slots_to_generate",
                    "replacement_slot": replacement_slot,
                },
            )
            return 0
        requirement = str(payload.get("requirement_framing") or "")
        visual_style = str(payload.get("visual_style") or "")
        jobs = [
            {
                "slot_id": slot["slot_id"],
                "prompt": _build_slot_prompt(
                    slot,
                    requirement=requirement,
                    visual_style=visual_style,
                ),
            }
            for slot in slots
        ]
    else:
        prompt = raw_text or "Create a polished webpage image."
        slot_id = Path(args.filename).stem or "image"
        jobs = [{"slot_id": slot_id, "prompt": prompt}]

    max_workers = min(3, len(jobs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(
            pool.map(
                lambda job: _generate_one(
                    slot_id=job["slot_id"],
                    prompt=job["prompt"],
                    model=args.model.strip(),
                    base_url=base_url,
                    api_key=api_key,
                    output_dir=output_dir,
                    local_path_prefix=args.local_path_prefix,
                    resolution=args.resolution,
                    proxy=proxy,
                ),
                jobs,
            )
        )

    generated = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    print(
        "IMAGE_AIGC_ATTEMPT: "
        + json.dumps(
            {
                "requested": [job["slot_id"] for job in jobs],
                "generated": [r["slot_id"] for r in generated],
                "failed": failed,
                "model": args.model.strip(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    for record in generated:
        _emit(
            "IMAGE_READY",
            {
                "local_path": record["local_path"],
                "mime": record["mime"],
                "bytes": record["bytes"],
                "slot_id": record["slot_id"],
                "prompt_preview": record["prompt_preview"],
            },
        )
    if failed:
        _emit(
            "IMAGE_GENERATION_FAILED",
            {
                "reason": "one_or_more_images_failed",
                "missing": [{"slot_id": r["slot_id"], "reason": r["reason"]} for r in failed],
                "replacement_slot": replacement_slot,
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
