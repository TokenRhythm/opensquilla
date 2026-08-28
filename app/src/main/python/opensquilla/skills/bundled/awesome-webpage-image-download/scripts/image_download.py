#!/usr/bin/env python3
"""Download searched image candidates for AwesomeWebpageMetaSkill."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

IMAGE_MIME_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),
)


def _emit(label: str, payload: dict[str, Any]) -> None:
    print(f"{label}: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}")


def _loads(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"media_search": raw}
    return value if isinstance(value, dict) else {}


def _clean_slot(raw: Any, fallback: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip()).strip("-").lower()
    return (value or fallback)[:64]


def _image_slots(payload: dict[str, Any]) -> list[dict[str, Any]]:
    media_slots = _loads(payload.get("media_slots"))
    if isinstance(media_slots, dict):
        raw_slots = media_slots.get("slots")
    elif isinstance(media_slots, list):
        raw_slots = media_slots
    else:
        raw_slots = None
    slots: list[dict[str, Any]] = []
    if not isinstance(raw_slots, list):
        return slots
    for index, item in enumerate(raw_slots, start=1):
        if not isinstance(item, dict):
            continue
        if str(item.get("modality") or "").lower() != "image":
            continue
        slot_id = _clean_slot(item.get("slot_id"), f"image-{index}")
        slots.append(
            {
                "slot_id": slot_id,
                "subject": str(item.get("subject") or item.get("placement") or slot_id),
                "search_keywords": item.get("search_keywords") or [],
            }
        )
    return slots


def _urls_from_value(value: Any) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for nested in value.values():
            found.extend(_urls_from_value(nested))
        return found
    if isinstance(value, list):
        found = []
        for nested in value:
            found.extend(_urls_from_value(nested))
        return found
    text = str(value or "")
    return [
        url.rstrip(").,;]'\"")
        for url in re.findall(r"https?://[^\s<>()\"']+", text)
        if not url.startswith("data:")
    ]


def _candidate_urls(payload: dict[str, Any]) -> list[str]:
    media_search = _loads(payload.get("media_search"))
    urls = _urls_from_value(media_search)
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _score(url: str, slot: dict[str, Any]) -> int:
    haystack = urllib.parse.unquote(url).lower()
    terms = [str(slot.get("subject") or "")]
    keywords = slot.get("search_keywords") or []
    if isinstance(keywords, list):
        terms.extend(str(item) for item in keywords)
    else:
        terms.extend(re.split(r"[,，;/、\s]+", str(keywords)))
    return sum(1 for term in terms if term and term.lower() in haystack)


def _looks_like_image(data: bytes, content_type: str, url: str) -> tuple[str, str] | None:
    lowered = content_type.split(";", 1)[0].strip().lower()
    for magic, mime, ext in IMAGE_MIME_BY_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime, ext
    if lowered in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        ext = mimetypes.guess_extension(lowered) or Path(urllib.parse.urlparse(url).path).suffix
        return lowered, ext or ".jpg"
    return None


def _fetch(url: str) -> tuple[str, bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1",
            "User-Agent": "OpenSquilla-AwesomeWebpage/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        content_type = resp.headers.get("Content-Type", "")
        return content_type, resp.read(12 * 1024 * 1024), resp.geturl()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--local-path-prefix", default="project/assets/images")
    args = parser.parse_args()

    payload = _payload()
    slots = _image_slots(payload)
    urls = _candidate_urls(payload)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[dict[str, Any]] = []
    unfilled: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    used_urls: set[str] = set()

    for slot in slots:
        candidates = sorted(
            [url for url in urls if url not in used_urls],
            key=lambda url: _score(url, slot),
            reverse=True,
        )
        saved = None
        for url in candidates:
            try:
                content_type, data, final_url = _fetch(url)
                match = _looks_like_image(data, content_type, final_url)
                if match is None:
                    skipped.append({"url": url, "reason": "not_image"})
                    continue
                mime, ext = match
                filename = f"{slot['slot_id']}{ext}"
                path = (output_dir / filename).resolve()
                path.relative_to(output_dir)
                path.write_bytes(data)
                saved = {
                    "slot_id": slot["slot_id"],
                    "url": final_url,
                    "local_path": f"{args.local_path_prefix.rstrip('/')}/{filename}",
                    "mime": mime,
                    "bytes": len(data),
                    "subject": slot.get("subject", ""),
                }
                used_urls.add(url)
                break
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                skipped.append({"url": url, "reason": exc.__class__.__name__})
        if saved is None:
            unfilled.append({"slot_id": slot["slot_id"], "reason": "no_downloadable_image_url"})
        else:
            downloaded.append(saved)

    print(
        json.dumps(
            {"downloaded": downloaded, "unfilled": unfilled, "skipped": skipped},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    for item in downloaded:
        _emit(
            "IMAGE_READY",
            {
                "local_path": item["local_path"],
                "mime": item["mime"],
                "bytes": item["bytes"],
                "slot_id": item["slot_id"],
                "subject": item.get("subject", ""),
            },
        )
    if unfilled or (slots and not downloaded):
        _emit(
            "IMAGE_DOWNLOAD_INCOMPLETE",
            {
                "reason": "unfilled_image_slots",
                "unfilled_slot_ids": [item["slot_id"] for item in unfilled],
            },
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
