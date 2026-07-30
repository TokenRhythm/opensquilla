"""Mechanical parsing and validation for profile fusion model output."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from opensquilla.memory.profile_import.errors import ProfileImportInvalidOutputError
from opensquilla.memory.profile_import.models import FusionOutput

_FENCED_JSON = re.compile(r"\A\s*```(?:json)?\s*(\{.*\})\s*```\s*\Z", re.DOTALL | re.IGNORECASE)


def _decode_json_object(raw_response: str) -> object:
    text = raw_response.strip()
    candidates = [text]
    fenced = _FENCED_JSON.fullmatch(text)
    if fenced:
        candidates.append(fenced.group(1))
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])

    last_error: json.JSONDecodeError | None = None
    for candidate in dict.fromkeys(candidates):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(value, dict):
            raise ProfileImportInvalidOutputError("profile fusion output must be a JSON object")
        return value

    message = "profile fusion output is not valid JSON"
    if last_error is not None:
        message = f"{message}: {last_error.msg}"
    raise ProfileImportInvalidOutputError(message)


def parse_fusion_output(raw_response: str, *, imported_profile: str) -> FusionOutput:
    """Parse a model response and validate evidence excerpts mechanically."""

    try:
        output = FusionOutput.model_validate(_decode_json_object(raw_response))
    except ValidationError as exc:
        raise ProfileImportInvalidOutputError(
            f"profile fusion output does not match schema: {exc.errors(include_url=False)}"
        ) from exc

    missing = [
        decision.source_excerpt
        for decision in output.decisions
        if decision.source_excerpt not in imported_profile
    ]
    if missing:
        raise ProfileImportInvalidOutputError(
            "profile fusion evidence is not an exact excerpt of the imported text"
        )
    return output
