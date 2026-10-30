"""Canonical JSON and file hashes used by the frozen BGE runtime."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .errors import CanonicalizationError


SHA256_PREFIX = "sha256:"
def _normalize_text(value: str) -> str:
    # LF is the sole line separator. Other meaningful whitespace is preserved.
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _normalize_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("naive datetime is not canonical; timezone is required")
    utc_value = value.astimezone(timezone.utc)
    return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN and infinity are not valid canonical JSON")
        return value
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalizationError("canonical JSON object keys must be strings")
            key = _normalize_text(raw_key)
            if key in normalized:
                raise CanonicalizationError(
                    f"object keys collide after Unicode/newline normalization: {key!r}"
                )
            normalized[key] = _json_value(raw_value)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise CanonicalizationError("unordered sets are not canonical JSON; sort explicitly")
    raise CanonicalizationError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonical_json_text(value: Any) -> str:
    """Serialize JSON deterministically while preserving array order and nulls."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_text(value).encode("utf-8")


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return f"{SHA256_PREFIX}{digest.hexdigest()}"
