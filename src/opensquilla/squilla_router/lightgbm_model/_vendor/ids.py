"""Canonical JSON, content hashes, and deterministic identifiers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .errors import CanonicalizationError, IdentityError


SAMPLE_NAMESPACE = uuid.UUID("3d911727-5d9e-5ef0-8d71-d06b17e31bb7")
GROUP_NAMESPACE = uuid.UUID("bbc604b2-c6e7-5acc-9203-24d023ab440e")
CAPTURE_NAMESPACE = uuid.UUID("60726624-0037-51d4-9613-a295fd1dda37")
SHA256_PREFIX = "sha256:"
SAFE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def safe_path_component(value: str, *, label: str = "identifier") -> str:
    """Validate an identifier before interpolating it into a filesystem path."""

    if not isinstance(value, str) or not SAFE_COMPONENT_PATTERN.fullmatch(value):
        raise ValueError(
            f"{label} must match [A-Za-z0-9][A-Za-z0-9._:-]{{0,255}}"
        )
    return value


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


def sha256_bytes(data: bytes) -> str:
    return f"{SHA256_PREFIX}{hashlib.sha256(data).hexdigest()}"


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return f"{SHA256_PREFIX}{digest.hexdigest()}"


def canonical_json_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def validate_sha256(value: str) -> str:
    if not isinstance(value, str) or not value.startswith(SHA256_PREFIX):
        raise IdentityError(f"expected sha256:<64 lowercase hex>, got {value!r}")
    hex_value = value[len(SHA256_PREFIX) :]
    if len(hex_value) != 64 or any(character not in "0123456789abcdef" for character in hex_value):
        raise IdentityError(f"expected sha256:<64 lowercase hex>, got {value!r}")
    return value


def source_content_id(files: Iterable[Any], schema: Any) -> str:
    """Hash sorted immutable file identity plus the explicit source schema."""

    normalized_files: list[dict[str, Any]] = []
    for raw_file in files:
        payload = _json_value(raw_file)
        if not isinstance(payload, dict):
            raise IdentityError("each source file entry must be an object")
        try:
            entry = {
                "path": payload["path"],
                "version": payload.get("version"),
                "size": payload["size"],
                "sha256": validate_sha256(payload["sha256"]),
            }
        except KeyError as exc:
            raise IdentityError(f"source file entry is missing {exc.args[0]!r}") from exc
        normalized_files.append(entry)

    normalized_files.sort(
        key=lambda item: (
            str(item["path"]),
            "" if item["version"] is None else str(item["version"]),
            int(item["size"]),
            str(item["sha256"]),
        )
    )
    return canonical_json_hash({"files": normalized_files, "schema": schema})


def make_capture_id(
    *,
    source_content_id_value: str,
    captured_at: datetime,
    operator: str,
    permissions: Sequence[str],
    source_path: str,
    namespace: uuid.UUID = CAPTURE_NAMESPACE,
) -> str:
    validate_sha256(source_content_id_value)
    payload = {
        "source_content_id": source_content_id_value,
        "captured_at": captured_at,
        "operator": operator,
        "permissions": sorted(permissions),
        "source_path": source_path,
    }
    return str(uuid.uuid5(namespace, canonical_json_text(payload)))


def content_hash(input_payload: Any, masks: Mapping[str, Any] | BaseModel) -> str:
    """Hash normalized input and only presence/truncation mask semantics."""

    mask_values = masks.model_dump(mode="json") if isinstance(masks, BaseModel) else masks
    if not isinstance(mask_values, Mapping):
        raise IdentityError("masks must be a mapping or Pydantic model")
    selected_masks = {
        key: value
        for key, value in mask_values.items()
        if key.endswith("_present") or key == "truncated_fields"
    }
    if "truncated_fields" not in selected_masks:
        raise IdentityError("masks must include truncated_fields")
    return canonical_json_hash({"input": input_payload, "masks": selected_masks})


def source_partition_content_hash(
    partition: Mapping[str, Any] | BaseModel,
) -> str:
    """Hash immutable partition rows, excluding mutable control-plane policy."""

    payload = (
        partition.model_dump(mode="json")
        if isinstance(partition, BaseModel)
        else dict(partition)
    )
    try:
        immutable = {
            "partition_id": payload["partition_id"],
            "files": payload["files"],
            "row_count": payload.get("row_count"),
        }
    except KeyError as exc:
        raise IdentityError(
            f"partition content hash is missing {exc.args[0]}"
        ) from exc
    return canonical_json_hash(immutable)


def stable_sample_id(
    *,
    source_system: str,
    input_norm_version: str,
    content_hash_value: str,
    router_invocation_id: str | None = None,
    request_id: str | None = None,
    session_id: str | None = None,
    snapshot_at: datetime | None = None,
    namespace: uuid.UUID = SAMPLE_NAMESPACE,
) -> str:
    """Create the specified field-named UUID5 identity without using row paths."""

    validate_sha256(content_hash_value)
    if not source_system or not input_norm_version:
        raise IdentityError("source_system and input_norm_version are required")
    # router_invocation_id is the strongest event anchor; request_id is its fallback.
    if router_invocation_id:
        stable_anchor = {"router_invocation_id": router_invocation_id}
    elif request_id:
        stable_anchor = {"request_id": request_id}
    elif session_id and snapshot_at is not None:
        stable_anchor = {"session_id": session_id, "snapshot_at": snapshot_at}
    else:
        raise IdentityError(
            "stable sample identity requires an invocation/request ID or session_id + snapshot_at"
        )

    payload = {
        "source_system": source_system,
        **stable_anchor,
        "input_norm_version": input_norm_version,
        "content_hash": content_hash_value,
    }
    return str(uuid.uuid5(namespace, canonical_json_text(payload)))


def group_anchor_id(
    *,
    anchor_type: str,
    anchor: Any,
    group_norm_version: str,
    namespace: uuid.UUID = GROUP_NAMESPACE,
) -> str:
    if not anchor_type or not group_norm_version:
        raise IdentityError("anchor_type and group_norm_version are required")
    payload = {
        "anchor_type": anchor_type,
        "anchor": anchor,
        "group_norm_version": group_norm_version,
    }
    return str(uuid.uuid5(namespace, canonical_json_text(payload)))


def canonical_group_id(
    *,
    anchor_ids: Iterable[str],
    existing_group_ids: Iterable[str] = (),
    namespace: uuid.UUID = GROUP_NAMESPACE,
) -> str:
    """Choose the lexical-min old group, or derive a stable group from anchors."""

    existing = sorted(set(existing_group_ids))
    if existing:
        return existing[0]
    anchors = sorted(set(anchor_ids))
    if not anchors:
        raise IdentityError("at least one group anchor is required")
    return str(uuid.uuid5(namespace, canonical_json_text({"anchor_ids": anchors})))


def run_key(
    *,
    stage: str,
    upstream_manifest_hashes: Iterable[str],
    config_version: str,
    parameters: Any,
    code_digest: str,
    image_digest: str | None = None,
) -> str:
    hashes = sorted(validate_sha256(value) for value in upstream_manifest_hashes)
    if not stage or not config_version or not code_digest:
        raise IdentityError("stage, config_version, and code_digest are required")
    return canonical_json_hash(
        {
            "stage": stage,
            "upstream_manifest_hashes": hashes,
            "config_version": config_version,
            "parameters": parameters,
            "code_digest": code_digest,
            "image_digest": image_digest,
        }
    )
