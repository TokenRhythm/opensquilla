"""Mechanical parsing and validation for profile fusion model output."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from opensquilla.memory.profile_import.errors import ProfileImportInvalidOutputError
from opensquilla.memory.profile_import.models import FusionOutput

_FENCED_JSON = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>\{.*\})\r?\n```[ \t]*\Z",
    re.DOTALL | re.IGNORECASE,
)


def _decode_json_object(raw_response: str) -> dict[str, object]:
    """Decode exactly one JSON object, optionally wrapped by one full code fence."""

    text = raw_response.strip()
    fenced = _FENCED_JSON.fullmatch(text)
    if fenced:
        text = fenced.group("body")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProfileImportInvalidOutputError(
            f"profile fusion output is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ProfileImportInvalidOutputError("profile fusion output must be a JSON object")
    return value


def parse_fusion_output(raw_response: str, *, imported_profile: str) -> FusionOutput:
    """Parse a model response and validate evidence excerpts mechanically."""

    try:
        output = FusionOutput.model_validate(_decode_json_object(raw_response))
    except ValidationError as exc:
        raise ProfileImportInvalidOutputError(
            "profile fusion output does not match schema: "
            f"{exc.errors(include_url=False)}"
        ) from exc
    if any(decision.source_excerpt not in imported_profile for decision in output.decisions):
        raise ProfileImportInvalidOutputError(
            "profile fusion evidence is not an exact excerpt of the imported text"
        )
    return output
