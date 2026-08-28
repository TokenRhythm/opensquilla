from __future__ import annotations

import json
import re

REQUIRED_SOURCE_KEYS = {
    "index_html": "index.html",
    "style_css": "style.css",
    "script_js": "script.js",
}


class WebpageSourceError(ValueError):
    pass


class WebpageSourcePayloadError(WebpageSourceError):
    pass


class WebpageSourceParseError(WebpageSourceError):
    pass


def require_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    raise WebpageSourceParseError("source must be a JSON object")


def parse_json_object(text: str) -> dict[str, object]:
    text = text.strip()
    errors: list[str] = []
    if not text:
        raise WebpageSourceParseError("empty source JSON")

    candidates = [text]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    )

    for candidate in candidates:
        try:
            return require_object(json.loads(candidate))
        except (json.JSONDecodeError, WebpageSourceParseError) as exc:
            errors.append(str(exc))

    depth = 0
    start = None
    in_string = False
    escape = False
    for idx, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start : idx + 1]
                try:
                    return require_object(json.loads(candidate))
                except (json.JSONDecodeError, WebpageSourceParseError) as exc:
                    errors.append(str(exc))

    raise WebpageSourceParseError(errors[-1] if errors else "no JSON object found")


def load_source_payload(raw_payload_source: str) -> dict[str, object]:
    try:
        raw_payload = json.loads(raw_payload_source)
    except json.JSONDecodeError as exc:
        raise WebpageSourcePayloadError(f"invalid source JSON payload: {exc}") from exc

    if isinstance(raw_payload, str):
        return parse_json_object(raw_payload)
    return require_object(raw_payload)


def missing_required_keys(data: dict[str, object]) -> list[str]:
    return [key for key in REQUIRED_SOURCE_KEYS if not str(data.get(key, "")).strip()]
