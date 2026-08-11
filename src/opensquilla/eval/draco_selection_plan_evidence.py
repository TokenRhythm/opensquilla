"""Content-addressed selection-plan evidence primitives for DRACO artifacts.

This module deliberately has no runner, finalizer, or manifest integration.  It
defines the versioned object/ref/pack protocol and a bounded, offset-based
reader that later integrations can bind to their existing durable transaction.
Object payloads are never retained in a process-wide cache.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

SELECTION_PLAN_OBJECT_SCHEMA = "opensquilla.draco-selection-plan-object/v1"
SELECTION_PLAN_REF_SCHEMA = "opensquilla.draco-selection-plan-ref/v1"
SELECTION_PLAN_PACK_SCHEMA = "opensquilla.draco-selection-plan-pack/v1"
SELECTION_PLAN_PACK_RECORD_SCHEMA = (
    "opensquilla.draco-selection-plan-pack-record/v1"
)

SELECTION_PLAN_ROOT_KIND = "selection_plan"
SELECTION_PLAN_LEAF_KINDS: Final[dict[str, str]] = {
    "registry_snapshot": "registry_snapshot",
    "ranking_parameters": "ranking_parameters",
    "request_context": "request_context",
}
SELECTION_PLAN_OBJECT_KINDS: Final[frozenset[str]] = frozenset(
    {SELECTION_PLAN_ROOT_KIND, *SELECTION_PLAN_LEAF_KINDS.values()}
)

_CANONICAL_JSON_CONTRACT = "utf8-sort-keys-compact-no-nan"
_EXPANDED_HASH_DOMAIN = b"opensquilla.draco-selection-plan-expanded/v1\0"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PACK_READ_CHUNK_BYTES = 256 * 1024

_PACK_HEADER = {
    "canonical_json": _CANONICAL_JSON_CONTRACT,
    "compression": "zlib",
    "hash": "sha256",
    "object_schema": SELECTION_PLAN_OBJECT_SCHEMA,
    "record_schema": SELECTION_PLAN_PACK_RECORD_SCHEMA,
    "ref_schema": SELECTION_PLAN_REF_SCHEMA,
    "schema": SELECTION_PLAN_PACK_SCHEMA,
}

_RECORD_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "sha256",
        "uncompressed_bytes",
        "encoding",
        "compressed_bytes",
        "compressed_sha256",
        "payload_base64",
    }
)
_LEAF_REF_FIELDS = frozenset(
    {"schema", "kind", "sha256", "uncompressed_bytes"}
)
_ROOT_REF_FIELDS = frozenset(
    {
        *_LEAF_REF_FIELDS,
        "expanded_sha256",
        "expanded_bytes",
        "summary",
    }
)

_SUMMARY_STRING_FIELDS = (
    "strategy",
    "selection_mode",
    "decision_id",
    "ranking_version",
    "ranking_config_schema_version",
    "ranking_config_version",
    "ranking_config_hash",
    "registry_snapshot_version",
    "registry_snapshot_hash",
    "request_context_hash",
    "selected_A",
    "stop_reason",
)
_SUMMARY_LIST_FIELDS = (
    "selected_P",
    "backup_P",
    "aggregator_candidates",
)
_SUMMARY_INTEGER_FIELDS = (
    "proposer_count",
    "N_min",
    "N_max",
)
_SUMMARY_BOOLEAN_FIELDS = (
    "analyzer_failure_fallback",
    "ranking_thinking_assignment_enabled",
    "capture_failed",
)
_SUMMARY_FIELDS = frozenset(
    {
        *_SUMMARY_STRING_FIELDS,
        *_SUMMARY_LIST_FIELDS,
        *_SUMMARY_INTEGER_FIELDS,
        *_SUMMARY_BOOLEAN_FIELDS,
    }
)


class SelectionPlanEvidenceError(ValueError):
    """A selection-plan object, ref, or pack violates its protocol."""


class SelectionPlanEvidenceLimitError(SelectionPlanEvidenceError):
    """Selection-plan evidence exceeds a fixed resource bound."""


@dataclass(frozen=True)
class SelectionPlanEvidenceLimits:
    """Fixed caps applied before a pack or expanded plan is trusted."""

    object_uncompressed_bytes: int = 4 * 1024 * 1024
    object_compressed_bytes: int = 4 * 1024 * 1024
    expanded_plan_bytes: int = 8 * 1024 * 1024
    ref_summary_bytes: int = 16 * 1024
    summary_text_bytes: int = 256
    summary_identity_count: int = 16
    pack_object_count: int = 100_000
    pack_bytes: int = 1024 * 1024 * 1024
    record_line_bytes: int = 6 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS = SelectionPlanEvidenceLimits()


def _validate_native_json(value: Any, *, active: set[int]) -> None:
    """Reject values whose JSON encoding would silently change their type."""

    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise SelectionPlanEvidenceError("JSON numbers must be finite")
    if type(value) not in {dict, list}:
        raise SelectionPlanEvidenceError(
            "selection-plan evidence must use native JSON containers and scalars"
        )
    identity = id(value)
    if identity in active:
        raise SelectionPlanEvidenceError("selection-plan evidence is not acyclic JSON")
    active.add(identity)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise SelectionPlanEvidenceError(
                        "selection-plan evidence object keys must be strings"
                    )
                _validate_native_json(item, active=active)
        else:
            for item in value:
                _validate_native_json(item, active=active)
    finally:
        active.remove(identity)


def _canonical_json_bytes(value: Any, *, label: str) -> bytes:
    try:
        _validate_native_json(value, active=set())
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except SelectionPlanEvidenceError:
        raise
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise SelectionPlanEvidenceError(f"{label} is not finite acyclic JSON") from exc


def canonical_selection_plan_json_bytes(value: Any) -> bytes:
    """Return the canonical JSON representation used by expanded-plan hashes."""

    return _canonical_json_bytes(value, label="selection plan")


def _sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _strict_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SelectionPlanEvidenceError(f"{label} must be a canonical SHA256 ref")
    return value


def _strict_nonnegative_int(value: Any, *, label: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        raise SelectionPlanEvidenceError(f"{label} is outside its integer bound")
    return value


def _object_envelope_bytes(
    kind: str,
    payload: Mapping[str, Any],
    *,
    limits: SelectionPlanEvidenceLimits,
) -> bytes:
    if kind not in SELECTION_PLAN_OBJECT_KINDS:
        raise SelectionPlanEvidenceError(f"unsupported selection-plan object kind: {kind!r}")
    if not isinstance(payload, Mapping):
        raise SelectionPlanEvidenceError(f"{kind} payload must be a JSON object")
    raw = _canonical_json_bytes(
        {
            "schema": SELECTION_PLAN_OBJECT_SCHEMA,
            "kind": kind,
            "payload": dict(payload),
        },
        label=f"{kind} object",
    )
    if len(raw) > limits.object_uncompressed_bytes:
        raise SelectionPlanEvidenceLimitError(
            f"{kind} object exceeds the uncompressed byte cap"
        )
    return raw


def _expanded_plan_hash(payload: bytes) -> str:
    return _sha256(_EXPANDED_HASH_DOMAIN + payload)


def _bounded_summary_text(
    value: Any,
    *,
    field_name: str,
    limits: SelectionPlanEvidenceLimits,
) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SelectionPlanEvidenceError(
            f"selection-plan summary field {field_name} must be a string"
        )
    if len(value.encode("utf-8")) > limits.summary_text_bytes:
        raise SelectionPlanEvidenceLimitError(
            f"selection-plan summary field {field_name} exceeds its byte cap"
        )
    return value


def _bounded_summary_identities(
    value: Any,
    *,
    field_name: str,
    limits: SelectionPlanEvidenceLimits,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SelectionPlanEvidenceError(
            f"selection-plan summary field {field_name} must be a list"
        )
    if len(value) > limits.summary_identity_count:
        raise SelectionPlanEvidenceLimitError(
            f"selection-plan summary field {field_name} exceeds its count cap"
        )
    return [
        _bounded_summary_text(
            item,
            field_name=f"{field_name}[{index}]",
            limits=limits,
        )
        for index, item in enumerate(value)
    ]


def _summary_integer(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _strict_nonnegative_int(
        value,
        label=f"selection-plan summary field {field_name}",
        maximum=(1 << 31) - 1,
    )


def selection_plan_summary(
    plan: Mapping[str, Any],
    *,
    limits: SelectionPlanEvidenceLimits = DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
) -> dict[str, Any]:
    """Project a fixed, content-free audit summary from an expanded plan."""

    if not isinstance(plan, Mapping):
        raise SelectionPlanEvidenceError("selection plan must be a JSON object")
    summary: dict[str, Any] = {
        field_name: _bounded_summary_text(
            plan.get(field_name),
            field_name=field_name,
            limits=limits,
        )
        for field_name in _SUMMARY_STRING_FIELDS
    }
    summary.update(
        {
            field_name: _bounded_summary_identities(
                plan.get(field_name),
                field_name=field_name,
                limits=limits,
            )
            for field_name in _SUMMARY_LIST_FIELDS
        }
    )
    summary.update(
        {
            field_name: _summary_integer(
                plan.get(field_name),
                field_name=field_name,
            )
            for field_name in _SUMMARY_INTEGER_FIELDS
        }
    )
    summary.update(
        {
            field_name: plan.get(field_name) is True
            for field_name in _SUMMARY_BOOLEAN_FIELDS
        }
    )
    summary_bytes = _canonical_json_bytes(summary, label="selection-plan summary")
    if len(summary_bytes) > limits.ref_summary_bytes:
        raise SelectionPlanEvidenceLimitError(
            "selection-plan summary exceeds its serialized byte cap"
        )
    return summary


def _validate_summary(
    value: Any,
    *,
    limits: SelectionPlanEvidenceLimits,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SUMMARY_FIELDS:
        raise SelectionPlanEvidenceError("selection-plan ref has a non-canonical summary")
    summary = dict(value)
    for field_name in _SUMMARY_STRING_FIELDS:
        _bounded_summary_text(
            summary[field_name],
            field_name=field_name,
            limits=limits,
        )
    for field_name in _SUMMARY_LIST_FIELDS:
        _bounded_summary_identities(
            summary[field_name],
            field_name=field_name,
            limits=limits,
        )
    for field_name in _SUMMARY_INTEGER_FIELDS:
        _summary_integer(summary[field_name], field_name=field_name)
    for field_name in _SUMMARY_BOOLEAN_FIELDS:
        if not isinstance(summary[field_name], bool):
            raise SelectionPlanEvidenceError(
                f"selection-plan summary field {field_name} must be boolean"
            )
    if (
        len(_canonical_json_bytes(summary, label="selection-plan summary"))
        > limits.ref_summary_bytes
    ):
        raise SelectionPlanEvidenceLimitError(
            "selection-plan summary exceeds its serialized byte cap"
        )
    return summary


@dataclass(frozen=True)
class SelectionPlanReference:
    """Validated JSON reference to one pack object."""

    kind: str
    sha256: str
    uncompressed_bytes: int
    expanded_sha256: str | None = None
    expanded_bytes: int | None = None
    summary: Mapping[str, Any] | None = None

    @property
    def is_root(self) -> bool:
        return self.kind == SELECTION_PLAN_ROOT_KIND

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema": SELECTION_PLAN_REF_SCHEMA,
            "kind": self.kind,
            "sha256": self.sha256,
            "uncompressed_bytes": self.uncompressed_bytes,
        }
        if self.is_root:
            value.update(
                {
                    "expanded_sha256": self.expanded_sha256,
                    "expanded_bytes": self.expanded_bytes,
                    "summary": dict(self.summary or {}),
                }
            )
        return value


def parse_selection_plan_reference(
    value: Any,
    *,
    expected_kind: str | None = None,
    limits: SelectionPlanEvidenceLimits = DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
) -> SelectionPlanReference:
    """Validate a serialized root or leaf reference without resolving it."""

    if not isinstance(value, Mapping):
        raise SelectionPlanEvidenceError("selection-plan ref must be a JSON object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in SELECTION_PLAN_OBJECT_KINDS:
        raise SelectionPlanEvidenceError("selection-plan ref has an unsupported kind")
    if expected_kind is not None and kind != expected_kind:
        raise SelectionPlanEvidenceError(
            f"selection-plan ref kind {kind!r} differs from {expected_kind!r}"
        )
    expected_fields = _ROOT_REF_FIELDS if kind == SELECTION_PLAN_ROOT_KIND else _LEAF_REF_FIELDS
    if set(value) != expected_fields or value.get("schema") != SELECTION_PLAN_REF_SCHEMA:
        raise SelectionPlanEvidenceError("selection-plan ref shape is not canonical")
    sha256 = _strict_sha256(value.get("sha256"), label="selection-plan object hash")
    uncompressed_bytes = _strict_nonnegative_int(
        value.get("uncompressed_bytes"),
        label="selection-plan object byte count",
        maximum=limits.object_uncompressed_bytes,
    )
    if uncompressed_bytes == 0:
        raise SelectionPlanEvidenceError("selection-plan object cannot be empty")
    if kind != SELECTION_PLAN_ROOT_KIND:
        return SelectionPlanReference(kind, sha256, uncompressed_bytes)
    expanded_sha256 = _strict_sha256(
        value.get("expanded_sha256"),
        label="expanded selection-plan hash",
    )
    expanded_bytes = _strict_nonnegative_int(
        value.get("expanded_bytes"),
        label="expanded selection-plan byte count",
        maximum=limits.expanded_plan_bytes,
    )
    if expanded_bytes == 0:
        raise SelectionPlanEvidenceError("expanded selection plan cannot be empty")
    summary = _validate_summary(value.get("summary"), limits=limits)
    return SelectionPlanReference(
        kind,
        sha256,
        uncompressed_bytes,
        expanded_sha256,
        expanded_bytes,
        MappingProxyType(summary),
    )


def is_selection_plan_reference(value: Any) -> bool:
    """Return whether ``value`` explicitly declares the ref schema."""

    return isinstance(value, Mapping) and value.get("schema") == SELECTION_PLAN_REF_SCHEMA


@dataclass(frozen=True)
class SelectionPlanObjectLocation:
    """Compact offset metadata retained after a one-pass pack scan."""

    kind: str
    sha256: str
    uncompressed_bytes: int
    compressed_bytes: int
    start_offset: int
    end_offset: int
    line_sha256: str
    dependencies: tuple[SelectionPlanReference, ...]


@dataclass(frozen=True)
class SelectionPlanPackIndex:
    """Immutable compact index; it intentionally contains no decoded payloads."""

    schema: str
    pack_sha256: str
    pack_bytes: int
    object_count: int
    device: int
    inode: int
    mtime_ns: int
    objects: Mapping[str, SelectionPlanObjectLocation]

    def location(self, sha256: str) -> SelectionPlanObjectLocation:
        try:
            return self.objects[sha256]
        except KeyError as exc:
            raise SelectionPlanEvidenceError(
                "selection-plan ref points outside its bound pack"
            ) from exc


@dataclass(frozen=True)
class _DecodedRecord:
    kind: str
    sha256: str
    uncompressed_bytes: int
    compressed_bytes: int
    payload: dict[str, Any]
    dependencies: tuple[SelectionPlanReference, ...]


@dataclass(frozen=True)
class _ScannedPack:
    index: SelectionPlanPackIndex
    digest: Any


def _bounded_zlib_decompress(
    payload: bytes,
    *,
    expected_bytes: int,
    limits: SelectionPlanEvidenceLimits,
) -> bytes:
    if not payload or len(payload) > limits.object_compressed_bytes:
        raise SelectionPlanEvidenceLimitError(
            "compressed selection-plan object is outside its byte cap"
        )
    decoder = zlib.decompressobj()
    output_limit = min(expected_bytes, limits.object_uncompressed_bytes) + 1
    try:
        raw = decoder.decompress(payload, output_limit)
        if len(raw) <= expected_bytes:
            raw += decoder.flush(expected_bytes + 1 - len(raw))
    except zlib.error as exc:
        raise SelectionPlanEvidenceError(
            "selection-plan object has invalid zlib payload"
        ) from exc
    if (
        len(raw) != expected_bytes
        or decoder.unconsumed_tail
        or decoder.unused_data
        or not decoder.eof
    ):
        raise SelectionPlanEvidenceError(
            "selection-plan object decompression boundary differs"
        )
    return raw


def _decode_object_envelope(
    raw: bytes,
    *,
    expected_kind: str,
) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SelectionPlanEvidenceError(
            "selection-plan object envelope is not valid JSON"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "kind", "payload"}
        or value.get("schema") != SELECTION_PLAN_OBJECT_SCHEMA
        or value.get("kind") != expected_kind
        or not isinstance(value.get("payload"), dict)
    ):
        raise SelectionPlanEvidenceError(
            "selection-plan object envelope shape differs"
        )
    if _canonical_json_bytes(value, label="selection-plan object envelope") != raw:
        raise SelectionPlanEvidenceError(
            "selection-plan object envelope is not canonical JSON"
        )
    return value["payload"]


def _object_dependencies(
    kind: str,
    payload: Mapping[str, Any],
    *,
    limits: SelectionPlanEvidenceLimits,
) -> tuple[SelectionPlanReference, ...]:
    if kind != SELECTION_PLAN_ROOT_KIND:
        return ()
    dependencies: list[SelectionPlanReference] = []
    for field_name, leaf_kind in SELECTION_PLAN_LEAF_KINDS.items():
        if field_name not in payload:
            continue
        dependencies.append(
            parse_selection_plan_reference(
                payload[field_name],
                expected_kind=leaf_kind,
                limits=limits,
            )
        )
    return tuple(dependencies)


def _decode_record_line(
    line: bytes,
    *,
    limits: SelectionPlanEvidenceLimits,
) -> _DecodedRecord:
    if not line.endswith(b"\n") or line == b"\n":
        raise SelectionPlanEvidenceError("selection-plan pack record is not one JSON line")
    if len(line) > limits.record_line_bytes:
        raise SelectionPlanEvidenceLimitError(
            "selection-plan pack record exceeds its line cap"
        )
    try:
        record = json.loads(line)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SelectionPlanEvidenceError(
            "selection-plan pack record is not valid JSON"
        ) from exc
    if (
        not isinstance(record, dict)
        or set(record) != _RECORD_FIELDS
        or record.get("schema") != SELECTION_PLAN_PACK_RECORD_SCHEMA
        or record.get("encoding") != "zlib"
    ):
        raise SelectionPlanEvidenceError("selection-plan pack record shape differs")
    if (
        _canonical_json_bytes(record, label="selection-plan pack record") + b"\n"
        != line
    ):
        raise SelectionPlanEvidenceError(
            "selection-plan pack record is not canonical JSON"
        )
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in SELECTION_PLAN_OBJECT_KINDS:
        raise SelectionPlanEvidenceError("selection-plan pack record kind is invalid")
    sha256 = _strict_sha256(record.get("sha256"), label="selection-plan object hash")
    compressed_sha256 = _strict_sha256(
        record.get("compressed_sha256"),
        label="compressed selection-plan object hash",
    )
    uncompressed_bytes = _strict_nonnegative_int(
        record.get("uncompressed_bytes"),
        label="selection-plan object byte count",
        maximum=limits.object_uncompressed_bytes,
    )
    compressed_bytes = _strict_nonnegative_int(
        record.get("compressed_bytes"),
        label="compressed selection-plan object byte count",
        maximum=limits.object_compressed_bytes,
    )
    if uncompressed_bytes == 0 or compressed_bytes == 0:
        raise SelectionPlanEvidenceError("selection-plan object payload cannot be empty")
    encoded = record.get("payload_base64")
    if not isinstance(encoded, str):
        raise SelectionPlanEvidenceError("selection-plan object payload is not base64 text")
    try:
        compressed = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise SelectionPlanEvidenceError(
            "selection-plan object payload is not canonical base64"
        ) from exc
    if base64.b64encode(compressed).decode("ascii") != encoded:
        raise SelectionPlanEvidenceError(
            "selection-plan object payload is not canonical base64"
        )
    if len(compressed) != compressed_bytes or _sha256(compressed) != compressed_sha256:
        raise SelectionPlanEvidenceError(
            "compressed selection-plan object bytes differ from their record"
        )
    raw = _bounded_zlib_decompress(
        compressed,
        expected_bytes=uncompressed_bytes,
        limits=limits,
    )
    if _sha256(raw) != sha256:
        raise SelectionPlanEvidenceError(
            "selection-plan object hash differs from its canonical envelope"
        )
    payload = _decode_object_envelope(raw, expected_kind=kind)
    dependencies = _object_dependencies(kind, payload, limits=limits)
    return _DecodedRecord(
        kind,
        sha256,
        uncompressed_bytes,
        compressed_bytes,
        payload,
        dependencies,
    )


def _record_line(
    kind: str,
    payload: Mapping[str, Any],
    *,
    limits: SelectionPlanEvidenceLimits,
) -> tuple[bytes, _DecodedRecord]:
    raw = _object_envelope_bytes(kind, payload, limits=limits)
    compressed = zlib.compress(raw, level=9)
    if len(compressed) > limits.object_compressed_bytes:
        raise SelectionPlanEvidenceLimitError(
            f"{kind} object exceeds the compressed byte cap"
        )
    record = {
        "schema": SELECTION_PLAN_PACK_RECORD_SCHEMA,
        "kind": kind,
        "sha256": _sha256(raw),
        "uncompressed_bytes": len(raw),
        "encoding": "zlib",
        "compressed_bytes": len(compressed),
        "compressed_sha256": _sha256(compressed),
        "payload_base64": base64.b64encode(compressed).decode("ascii"),
    }
    line = _canonical_json_bytes(record, label="selection-plan pack record") + b"\n"
    if len(line) > limits.record_line_bytes:
        raise SelectionPlanEvidenceLimitError(
            "selection-plan pack record exceeds its line cap"
        )
    decoded = _DecodedRecord(
        kind,
        record["sha256"],
        len(raw),
        len(compressed),
        dict(payload),
        _object_dependencies(kind, payload, limits=limits),
    )
    return line, decoded


_PACK_HEADER_LINE = _canonical_json_bytes(
    _PACK_HEADER,
    label="selection-plan pack header",
) + b"\n"


def _file_signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _validate_index_dependencies(
    locations: Mapping[str, SelectionPlanObjectLocation],
) -> None:
    for location in locations.values():
        for dependency in location.dependencies:
            target = locations.get(dependency.sha256)
            if target is None:
                raise SelectionPlanEvidenceError(
                    "selection-plan pack contains a dangling object dependency"
                )
            if (
                target.kind != dependency.kind
                or target.uncompressed_bytes != dependency.uncompressed_bytes
            ):
                raise SelectionPlanEvidenceError(
                    "selection-plan object dependency conflicts with its target"
                )


def _scan_pack_fd(
    fd: int,
    *,
    limits: SelectionPlanEvidenceLimits,
) -> _ScannedPack:
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        raise SelectionPlanEvidenceError("selection-plan pack is not a regular file")
    if file_stat.st_size <= 0 or file_stat.st_size > limits.pack_bytes:
        raise SelectionPlanEvidenceLimitError(
            "selection-plan pack is outside its total byte cap"
        )

    digest = hashlib.sha256()
    locations: dict[str, SelectionPlanObjectLocation] = {}
    buffer = bytearray()
    buffer_start = 0
    read_offset = 0
    line_number = 0
    while read_offset < file_stat.st_size:
        chunk = os.pread(
            fd,
            min(_PACK_READ_CHUNK_BYTES, file_stat.st_size - read_offset),
            read_offset,
        )
        if not chunk:
            raise SelectionPlanEvidenceError(
                "selection-plan pack scan made no forward progress"
            )
        read_offset += len(chunk)
        digest.update(chunk)
        buffer.extend(chunk)
        cursor = 0
        while True:
            newline = buffer.find(b"\n", cursor)
            if newline < 0:
                break
            line = bytes(buffer[cursor : newline + 1])
            start_offset = buffer_start + cursor
            end_offset = buffer_start + newline + 1
            line_number += 1
            if line_number == 1:
                if line != _PACK_HEADER_LINE:
                    raise SelectionPlanEvidenceError(
                        "selection-plan pack header differs from its schema"
                    )
            else:
                if len(locations) >= limits.pack_object_count:
                    raise SelectionPlanEvidenceLimitError(
                        "selection-plan pack exceeds its object-count cap"
                    )
                decoded = _decode_record_line(line, limits=limits)
                if decoded.sha256 in locations:
                    raise SelectionPlanEvidenceError(
                        "selection-plan pack contains a duplicate content address"
                    )
                locations[decoded.sha256] = SelectionPlanObjectLocation(
                    kind=decoded.kind,
                    sha256=decoded.sha256,
                    uncompressed_bytes=decoded.uncompressed_bytes,
                    compressed_bytes=decoded.compressed_bytes,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    line_sha256=_sha256(line),
                    dependencies=decoded.dependencies,
                )
            cursor = newline + 1
        if cursor:
            del buffer[:cursor]
            buffer_start += cursor
        if len(buffer) > limits.record_line_bytes:
            raise SelectionPlanEvidenceLimitError(
                "selection-plan pack has a line beyond its cap"
            )
    if buffer:
        raise SelectionPlanEvidenceError("selection-plan pack ends with a partial line")
    if line_number == 0:
        raise SelectionPlanEvidenceError("selection-plan pack has no header")
    end_stat = os.fstat(fd)
    if _file_signature(end_stat) != _file_signature(file_stat):
        raise SelectionPlanEvidenceError(
            "selection-plan pack changed while it was being indexed"
        )
    _validate_index_dependencies(locations)

    index = SelectionPlanPackIndex(
        schema=SELECTION_PLAN_PACK_SCHEMA,
        pack_sha256=f"sha256:{digest.hexdigest()}",
        pack_bytes=file_stat.st_size,
        object_count=len(locations),
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        mtime_ns=end_stat.st_mtime_ns,
        objects=MappingProxyType(locations),
    )
    return _ScannedPack(index=index, digest=digest)


def _read_exact(fd: int, *, start: int, length: int) -> bytes:
    chunks: list[bytes] = []
    offset = start
    remaining = length
    while remaining:
        chunk = os.pread(fd, remaining, offset)
        if not chunk:
            raise SelectionPlanEvidenceError(
                "selection-plan pack changed below an indexed object"
            )
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _load_indexed_object(
    fd: int,
    index: SelectionPlanPackIndex,
    ref: SelectionPlanReference,
    *,
    limits: SelectionPlanEvidenceLimits,
) -> dict[str, Any]:
    location = index.location(ref.sha256)
    if (
        location.kind != ref.kind
        or location.uncompressed_bytes != ref.uncompressed_bytes
    ):
        raise SelectionPlanEvidenceError(
            "selection-plan ref header differs from its indexed object"
        )
    line = _read_exact(
        fd,
        start=location.start_offset,
        length=location.end_offset - location.start_offset,
    )
    if _sha256(line) != location.line_sha256:
        raise SelectionPlanEvidenceError(
            "selection-plan pack object changed after indexing"
        )
    decoded = _decode_record_line(line, limits=limits)
    if (
        decoded.kind != location.kind
        or decoded.sha256 != location.sha256
        or decoded.uncompressed_bytes != location.uncompressed_bytes
        or decoded.compressed_bytes != location.compressed_bytes
        or decoded.dependencies != location.dependencies
    ):
        raise SelectionPlanEvidenceError(
            "selection-plan pack object conflicts with its offset index"
        )
    return decoded.payload


def _resolve_selection_plan_ref(
    fd: int,
    index: SelectionPlanPackIndex,
    value: Mapping[str, Any],
    *,
    limits: SelectionPlanEvidenceLimits,
) -> dict[str, Any]:
    root_ref = parse_selection_plan_reference(
        value,
        expected_kind=SELECTION_PLAN_ROOT_KIND,
        limits=limits,
    )
    root = _load_indexed_object(fd, index, root_ref, limits=limits)
    expanded = dict(root)
    visited = {root_ref.sha256}
    for field_name, leaf_kind in SELECTION_PLAN_LEAF_KINDS.items():
        if field_name not in expanded:
            continue
        leaf_ref = parse_selection_plan_reference(
            expanded[field_name],
            expected_kind=leaf_kind,
            limits=limits,
        )
        if leaf_ref.sha256 in visited:
            raise SelectionPlanEvidenceError("selection-plan object graph is cyclic")
        visited.add(leaf_ref.sha256)
        expanded[field_name] = _load_indexed_object(
            fd,
            index,
            leaf_ref,
            limits=limits,
        )

    expanded_payload = canonical_selection_plan_json_bytes(expanded)
    if len(expanded_payload) > limits.expanded_plan_bytes:
        raise SelectionPlanEvidenceLimitError(
            "expanded selection plan exceeds its byte cap"
        )
    if (
        len(expanded_payload) != root_ref.expanded_bytes
        or _expanded_plan_hash(expanded_payload) != root_ref.expanded_sha256
    ):
        raise SelectionPlanEvidenceError(
            "expanded selection plan differs from its root ref"
        )
    if selection_plan_summary(expanded, limits=limits) != dict(root_ref.summary or {}):
        raise SelectionPlanEvidenceError(
            "selection-plan ref summary differs from its expanded payload"
        )
    return expanded


def _open_regular(path: Path, flags: int, mode: int | None = None) -> int:
    effective_flags = flags | getattr(os, "O_CLOEXEC", 0) | getattr(
        os,
        "O_NOFOLLOW",
        0,
    )
    fd = (
        os.open(path, effective_flags, mode)
        if mode is not None
        else os.open(path, effective_flags)
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SelectionPlanEvidenceError("selection-plan pack is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("selection-plan pack write made no forward progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os,
        "O_CLOEXEC",
        0,
    )
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SelectionPlanPackReader:
    """Bound-fd, offset-based reader with no decoded-object cache."""

    def __init__(
        self,
        path: Path,
        *,
        limits: SelectionPlanEvidenceLimits = DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
    ) -> None:
        self.path = Path(path)
        self._limits = limits
        self._fd = _open_regular(self.path, os.O_RDONLY)
        self._closed = False
        try:
            self._index = _scan_pack_fd(self._fd, limits=limits).index
        except BaseException:
            self.close()
            raise

    @classmethod
    def from_fd(
        cls,
        fd: int,
        *,
        limits: SelectionPlanEvidenceLimits = DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
    ) -> SelectionPlanPackReader:
        """Bind to the same inode through a private descriptor duplicate."""

        instance = cls.__new__(cls)
        instance.path = None
        instance._limits = limits
        instance._fd = os.dup(fd)
        instance._closed = False
        try:
            instance._index = _scan_pack_fd(instance._fd, limits=limits).index
        except BaseException:
            instance.close()
            raise
        return instance

    @property
    def index(self) -> SelectionPlanPackIndex:
        return self._index

    def _require_open(self) -> int:
        if self._closed:
            raise SelectionPlanEvidenceError("selection-plan pack reader is closed")
        return self._fd

    def read_record_bytes(self, sha256: str) -> bytes:
        """Read one verified record by offset for later streaming pack merges."""

        fd = self._require_open()
        location = self._index.location(sha256)
        line = _read_exact(
            fd,
            start=location.start_offset,
            length=location.end_offset - location.start_offset,
        )
        if _sha256(line) != location.line_sha256:
            raise SelectionPlanEvidenceError(
                "selection-plan pack object changed after indexing"
            )
        return line

    def expand_selection_plan(self, value: Any) -> Any:
        """Resolve a root ref, or return a legacy inline Mapping by identity."""

        if not is_selection_plan_reference(value):
            if not isinstance(value, Mapping):
                raise SelectionPlanEvidenceError(
                    "selection plan must be an inline object or a versioned ref"
                )
            return value
        return _resolve_selection_plan_ref(
            self._require_open(),
            self._index,
            value,
            limits=self._limits,
        )

    def verify_snapshot(self) -> None:
        """Re-scan the bound inode and require the original exact pack bytes."""

        scanned = _scan_pack_fd(self._require_open(), limits=self._limits).index
        if (
            scanned.pack_sha256 != self._index.pack_sha256
            or scanned.pack_bytes != self._index.pack_bytes
            or scanned.device != self._index.device
            or scanned.inode != self._index.inode
        ):
            raise SelectionPlanEvidenceError(
                "selection-plan pack changed after its source snapshot"
            )

    def close(self) -> None:
        if self._closed:
            return
        os.close(self._fd)
        self._closed = True

    def __enter__(self) -> SelectionPlanPackReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class SelectionPlanPackAppender:
    """Append unique objects while retaining only compact offset metadata."""

    def __init__(
        self,
        path: Path,
        *,
        create: bool = True,
        limits: SelectionPlanEvidenceLimits = DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS,
    ) -> None:
        self.path = Path(path)
        self._limits = limits
        self._closed = False
        self._fd: int | None = None
        self._snapshot_signature: tuple[int, int, int, int, int] | None = None
        if create and len(_PACK_HEADER_LINE) > limits.pack_bytes:
            raise SelectionPlanEvidenceLimitError(
                "selection-plan pack header exceeds the total pack cap"
            )
        flags = os.O_RDWR | os.O_APPEND | (
            os.O_CREAT | os.O_EXCL if create else 0
        )
        try:
            self._fd = _open_regular(
                self.path,
                flags,
                0o600 if create else None,
            )
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SelectionPlanEvidenceError(
                    "selection-plan pack is locked by another appender"
                ) from exc
            if create:
                if hasattr(os, "fchmod"):
                    os.fchmod(self._fd, 0o600)
                _write_all(self._fd, _PACK_HEADER_LINE)
                os.fsync(self._fd)
                _fsync_directory(self.path.parent)
            scanned = _scan_pack_fd(self._fd, limits=limits)
            self._locations = dict(scanned.index.objects)
            self._digest = scanned.digest.copy()
            self._pack_bytes = scanned.index.pack_bytes
            self._snapshot_signature = _file_signature(os.fstat(self._fd))
        except BaseException:
            self.close()
            raise

    def _require_open(self) -> int:
        if self._closed or self._fd is None:
            raise SelectionPlanEvidenceError("selection-plan pack appender is closed")
        return self._fd

    def _assert_bound_snapshot(self) -> os.stat_result:
        file_stat = os.fstat(self._require_open())
        if (
            self._snapshot_signature is None
            or _file_signature(file_stat) != self._snapshot_signature
        ):
            try:
                self.close()
            except BaseException:
                pass
            raise SelectionPlanEvidenceError(
                "selection-plan pack changed outside its bound appender"
            )
        return file_stat

    def _index_value(self, *, immutable: bool) -> SelectionPlanPackIndex:
        file_stat = self._assert_bound_snapshot()
        objects: Mapping[str, SelectionPlanObjectLocation] = self._locations
        if immutable:
            objects = MappingProxyType(dict(self._locations))
        return SelectionPlanPackIndex(
            schema=SELECTION_PLAN_PACK_SCHEMA,
            pack_sha256=f"sha256:{self._digest.hexdigest()}",
            pack_bytes=self._pack_bytes,
            object_count=len(self._locations),
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            mtime_ns=file_stat.st_mtime_ns,
            objects=objects,
        )

    def _rollback_to(
        self,
        start_offset: int,
        prior_digest: Any,
    ) -> None:
        fd = self._require_open()
        try:
            os.ftruncate(fd, start_offset)
            os.fsync(fd)
            file_stat = os.fstat(fd)
        except BaseException as exc:
            try:
                self.close()
            except BaseException:
                pass
            raise SelectionPlanEvidenceError(
                "selection-plan pack rollback failed; appender was closed"
            ) from exc
        self._locations = {
            sha256: location
            for sha256, location in self._locations.items()
            if location.start_offset < start_offset
        }
        self._digest = prior_digest
        self._pack_bytes = start_offset
        self._snapshot_signature = _file_signature(file_stat)

    @property
    def index(self) -> SelectionPlanPackIndex:
        return self._index_value(immutable=True)

    def append_object(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append one leaf object and return its strict reference."""

        if kind == SELECTION_PLAN_ROOT_KIND:
            raise SelectionPlanEvidenceError(
                "selection-plan roots must be stored with store_selection_plan"
            )
        decoded = self._append_object(kind, payload)
        return SelectionPlanReference(
            decoded.kind,
            decoded.sha256,
            decoded.uncompressed_bytes,
        ).as_dict()

    def _append_object(self, kind: str, payload: Mapping[str, Any]) -> _DecodedRecord:
        """Append one validated object for the fixed root/leaf DAG."""

        fd = self._require_open()
        self._assert_bound_snapshot()
        line, decoded = _record_line(kind, payload, limits=self._limits)
        for dependency in decoded.dependencies:
            target = self._locations.get(dependency.sha256)
            if target is None:
                raise SelectionPlanEvidenceError(
                    "selection-plan root has a dangling object dependency"
                )
            if (
                target.kind != dependency.kind
                or target.uncompressed_bytes != dependency.uncompressed_bytes
            ):
                raise SelectionPlanEvidenceError(
                    "selection-plan root dependency conflicts with its target"
                )
        existing = self._locations.get(decoded.sha256)
        if existing is not None:
            if (
                existing.kind != decoded.kind
                or existing.uncompressed_bytes != decoded.uncompressed_bytes
                or existing.dependencies != decoded.dependencies
            ):
                raise SelectionPlanEvidenceError(
                    "selection-plan content address conflicts with an indexed object"
                )
            return decoded
        if len(self._locations) >= self._limits.pack_object_count:
            raise SelectionPlanEvidenceLimitError(
                "selection-plan pack exceeds its object-count cap"
            )
        if self._pack_bytes + len(line) > self._limits.pack_bytes:
            raise SelectionPlanEvidenceLimitError(
                "selection-plan pack exceeds its total byte cap"
            )

        start_offset = self._pack_bytes
        prior_digest = self._digest.copy()
        write_started = False
        try:
            if os.lseek(fd, 0, os.SEEK_END) != start_offset:
                try:
                    self.close()
                except BaseException:
                    pass
                raise SelectionPlanEvidenceError(
                    "selection-plan pack append offset differs from its index"
                )
            write_started = True
            _write_all(fd, line)
            os.fsync(fd)
            end_offset = start_offset + len(line)
            if os.fstat(fd).st_size != end_offset:
                raise SelectionPlanEvidenceError(
                    "selection-plan pack append length differs from its index"
                )
            self._locations[decoded.sha256] = SelectionPlanObjectLocation(
                kind=decoded.kind,
                sha256=decoded.sha256,
                uncompressed_bytes=decoded.uncompressed_bytes,
                compressed_bytes=decoded.compressed_bytes,
                start_offset=start_offset,
                end_offset=end_offset,
                line_sha256=_sha256(line),
                dependencies=decoded.dependencies,
            )
            self._digest.update(line)
            self._pack_bytes = end_offset
            self._snapshot_signature = _file_signature(os.fstat(fd))
        except BaseException:
            if write_started and not self._closed:
                self._rollback_to(
                    start_offset,
                    prior_digest,
                )
            raise
        return decoded

    def store_selection_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Store one root/leaf DAG and return a fixed root reference."""

        if not isinstance(plan, Mapping) or is_selection_plan_reference(plan):
            raise SelectionPlanEvidenceError(
                "store_selection_plan requires an expanded inline plan"
            )
        self._assert_bound_snapshot()
        expanded_payload = canonical_selection_plan_json_bytes(plan)
        if len(expanded_payload) > self._limits.expanded_plan_bytes:
            raise SelectionPlanEvidenceLimitError(
                "expanded selection plan exceeds its byte cap"
            )
        summary = selection_plan_summary(plan, limits=self._limits)
        skeleton = dict(plan)
        start_offset = self._pack_bytes
        prior_digest = self._digest.copy()
        try:
            for field_name, leaf_kind in SELECTION_PLAN_LEAF_KINDS.items():
                if field_name not in skeleton:
                    continue
                leaf = skeleton[field_name]
                if not isinstance(leaf, Mapping) or is_selection_plan_reference(leaf):
                    raise SelectionPlanEvidenceError(
                        f"selection-plan leaf {field_name} must be an expanded JSON object"
                    )
                leaf_record = self._append_object(leaf_kind, leaf)
                skeleton[field_name] = SelectionPlanReference(
                    leaf_record.kind,
                    leaf_record.sha256,
                    leaf_record.uncompressed_bytes,
                ).as_dict()
            root_record = self._append_object(SELECTION_PLAN_ROOT_KIND, skeleton)
        except BaseException:
            if not self._closed and self._pack_bytes != start_offset:
                self._rollback_to(start_offset, prior_digest)
            raise
        return SelectionPlanReference(
            kind=SELECTION_PLAN_ROOT_KIND,
            sha256=root_record.sha256,
            uncompressed_bytes=root_record.uncompressed_bytes,
            expanded_sha256=_expanded_plan_hash(expanded_payload),
            expanded_bytes=len(expanded_payload),
            summary=MappingProxyType(summary),
        ).as_dict()

    def expand_selection_plan(self, value: Any) -> Any:
        if not is_selection_plan_reference(value):
            if not isinstance(value, Mapping):
                raise SelectionPlanEvidenceError(
                    "selection plan must be an inline object or a versioned ref"
                )
            return value
        return _resolve_selection_plan_ref(
            self._require_open(),
            self._index_value(immutable=False),
            value,
            limits=self._limits,
        )

    def close(self) -> None:
        if self._closed:
            return
        fd = self._fd
        self._fd = None
        self._closed = True
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> SelectionPlanPackAppender:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


def expand_selection_plan(
    value: Any,
    *,
    reader: SelectionPlanPackReader | SelectionPlanPackAppender | None = None,
) -> Any:
    """Compatibility boundary: old inline plans are returned by identity."""

    if not is_selection_plan_reference(value):
        if not isinstance(value, Mapping):
            raise SelectionPlanEvidenceError(
                "selection plan must be an inline object or a versioned ref"
            )
        return value
    if reader is None:
        raise SelectionPlanEvidenceError(
            "content-addressed selection-plan ref requires its bound pack"
        )
    return reader.expand_selection_plan(value)


__all__ = [
    "DEFAULT_SELECTION_PLAN_EVIDENCE_LIMITS",
    "SELECTION_PLAN_LEAF_KINDS",
    "SELECTION_PLAN_OBJECT_SCHEMA",
    "SELECTION_PLAN_PACK_RECORD_SCHEMA",
    "SELECTION_PLAN_PACK_SCHEMA",
    "SELECTION_PLAN_REF_SCHEMA",
    "SELECTION_PLAN_ROOT_KIND",
    "SelectionPlanEvidenceError",
    "SelectionPlanEvidenceLimitError",
    "SelectionPlanEvidenceLimits",
    "SelectionPlanObjectLocation",
    "SelectionPlanPackAppender",
    "SelectionPlanPackIndex",
    "SelectionPlanPackReader",
    "SelectionPlanReference",
    "canonical_selection_plan_json_bytes",
    "expand_selection_plan",
    "is_selection_plan_reference",
    "parse_selection_plan_reference",
    "selection_plan_summary",
]
