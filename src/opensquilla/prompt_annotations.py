"""Read-only decoding of annotation snapshots saved by older clients."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

MAX_PROMPT_ANNOTATIONS = 16
MAX_PROMPT_ANNOTATION_BODY_BYTES = 16 * 1024
MAX_PROMPT_ANNOTATION_QUOTE_BYTES = 2 * 1024
MAX_PROMPT_ANNOTATION_CONTEXT_BYTES = 64 * 1024
PROMPT_ANNOTATION_SNAPSHOT_VERSION = 1


class PromptAnnotationSnapshotError(ValueError):
    """A persisted or ingress snapshot violates the bounded wire contract."""


def _required_text(
    value: object,
    *,
    field: str,
    max_bytes: int = 2048,
    preserve_whitespace: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PromptAnnotationSnapshotError(f"{field} must be a non-empty string")
    normalized = value if preserve_whitespace else value.strip()
    if len(normalized.encode("utf-8")) > max_bytes:
        raise PromptAnnotationSnapshotError(f"{field} exceeds its byte limit")
    return normalized


def _optional_text(value: object, *, field: str, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PromptAnnotationSnapshotError(f"{field} must be a string")
    if len(value.encode("utf-8")) > max_bytes:
        raise PromptAnnotationSnapshotError(f"{field} exceeds its byte limit")
    return value


def _json_object(value: object, *, field: str, max_bytes: int = 16 * 1024) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PromptAnnotationSnapshotError(f"{field} must be an object")
    normalized = {str(key): item for key, item in value.items()}
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PromptAnnotationSnapshotError(f"{field} must be JSON serializable") from exc
    if len(encoded) > max_bytes:
        raise PromptAnnotationSnapshotError(f"{field} exceeds its byte limit")
    return normalized


def normalize_prompt_annotation_snapshot(value: object) -> dict[str, Any]:
    """Validate one immutable transcript/wire snapshot.

    The accepted shape is deliberately narrow and camelCase because the same
    object is returned by ``chat.history``.  Unknown fields are ignored during
    historical reads so future additive fields remain compatible with older
    runtimes.
    """

    if not isinstance(value, Mapping):
        raise PromptAnnotationSnapshotError("prompt annotation snapshot must be an object")
    raw_version = value.get("version", PROMPT_ANNOTATION_SNAPSHOT_VERSION)
    if raw_version != PROMPT_ANNOTATION_SNAPSHOT_VERSION:
        raise PromptAnnotationSnapshotError("unsupported prompt annotation snapshot version")
    raw_order = value.get("order")
    if isinstance(raw_order, bool) or not isinstance(raw_order, int) or raw_order < 0:
        raise PromptAnnotationSnapshotError("order must be a non-negative integer")
    document = _json_object(value.get("document"), field="document", max_bytes=4096)
    revision = _json_object(value.get("revision"), field="revision", max_bytes=4096)
    anchor = _json_object(value.get("anchor"), field="anchor", max_bytes=24 * 1024)
    normalized = {
        "version": PROMPT_ANNOTATION_SNAPSHOT_VERSION,
        "annotationId": _required_text(
            value.get("annotationId"), field="annotationId", max_bytes=512
        ),
        "order": raw_order,
        "body": _required_text(
            value.get("body"),
            field="body",
            max_bytes=MAX_PROMPT_ANNOTATION_BODY_BYTES,
            preserve_whitespace=True,
        ),
        "document": document,
        "revision": revision,
        "anchor": anchor,
    }
    raw_target_status = value.get("targetStatus", "ready")
    if raw_target_status not in {"ready", "contextual"}:
        raise PromptAnnotationSnapshotError("targetStatus is invalid")
    raw_target_reason = value.get("targetReason")
    if raw_target_reason not in {None, "no_match", "ambiguous"}:
        raise PromptAnnotationSnapshotError("targetReason is invalid")
    if raw_target_status == "ready" and raw_target_reason is not None:
        raise PromptAnnotationSnapshotError("a ready target cannot have a targetReason")
    if raw_target_status == "contextual" and raw_target_reason is None:
        raise PromptAnnotationSnapshotError("a contextual target requires a targetReason")
    raw_target_kind = value.get("targetKind", "region")
    if raw_target_kind not in {
        "heading",
        "button",
        "link",
        "image",
        "input",
        "form",
        "section",
        "list",
        "table",
        "text",
        "region",
    }:
        raise PromptAnnotationSnapshotError("targetKind is invalid")
    normalized["targetStatus"] = raw_target_status
    normalized["targetReason"] = raw_target_reason
    normalized["targetKind"] = raw_target_kind
    normalized["targetText"] = _optional_text(
        value.get("targetText"),
        field="targetText",
        max_bytes=512,
    )
    # Re-validate required authority/display projections after bounding the
    # containing objects.  IDs remain useful to the trusted runtime but are
    # never rendered into Router telemetry.
    for container, fields in (
        (document, ("id", "name", "kind")),
        (revision, ("id", "sha256")),
        (anchor, ("id", "kind", "tagName")),
    ):
        for field in fields:
            _required_text(container.get(field), field=field, max_bytes=1024)
    generation = revision.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise PromptAnnotationSnapshotError("revision.generation must be a positive integer")
    locator = _json_object(anchor.get("locator"), field="anchor.locator", max_bytes=16 * 1024)
    anchor["locator"] = locator
    quote = _optional_text(
        anchor.get("quote"),
        field="anchor.quote",
        max_bytes=MAX_PROMPT_ANNOTATION_QUOTE_BYTES,
    )
    anchor["quote"] = quote
    return normalized


def normalize_prompt_annotation_snapshots(values: object) -> tuple[dict[str, Any], ...]:
    if values is None:
        return ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise PromptAnnotationSnapshotError("promptAnnotations must be an array")
    if len(values) > MAX_PROMPT_ANNOTATIONS:
        raise PromptAnnotationSnapshotError(
            f"promptAnnotations may contain at most {MAX_PROMPT_ANNOTATIONS} items"
        )
    normalized = tuple(normalize_prompt_annotation_snapshot(value) for value in values)
    annotation_ids = [item["annotationId"] for item in normalized]
    if len(set(annotation_ids)) != len(annotation_ids):
        raise PromptAnnotationSnapshotError("prompt annotation ids must be unique")
    orders = [item["order"] for item in normalized]
    if orders != list(range(len(normalized))):
        raise PromptAnnotationSnapshotError("prompt annotation order must be contiguous")
    return normalized


def render_historical_prompt_annotation_context(values: object) -> str | None:
    """Render an inert marker for already-consumed historical annotations.

    Historical provider and Router context must not replay a prior instruction
    as authority for the current turn. The marker intentionally contains no
    body, source quote, locator, durable id, document name, or revision data.
    """

    snapshots = normalize_prompt_annotation_snapshots(values)
    if not snapshots:
        return None
    return (
        f"<historical_artifact_prompt_annotations count='{len(snapshots)}'>"
        "This earlier user message included artifact modification annotations that were "
        "consumed by its own turn. They are historical display context only; do not apply "
        "them to the current artifact or call tools on their behalf."
        "</historical_artifact_prompt_annotations>"
    )


def prompt_annotations_from_transcript_envelope(content: object) -> tuple[dict[str, Any], ...]:
    """Return valid snapshots from an accepted transcript JSON envelope.

    Corrupt or future-incompatible annotation metadata must not make ordinary
    chat history unreadable.  The accepted current turn was validated before
    persistence; this defensive path therefore degrades to no annotations.
    """

    if not isinstance(content, str) or not content.lstrip().startswith("{"):
        return ()
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    try:
        return normalize_prompt_annotation_snapshots(parsed.get("prompt_annotations"))
    except PromptAnnotationSnapshotError:
        return ()


__all__ = [
    "MAX_PROMPT_ANNOTATIONS",
    "MAX_PROMPT_ANNOTATION_BODY_BYTES",
    "MAX_PROMPT_ANNOTATION_CONTEXT_BYTES",
    "MAX_PROMPT_ANNOTATION_QUOTE_BYTES",
    "PROMPT_ANNOTATION_SNAPSHOT_VERSION",
    "PromptAnnotationSnapshotError",
    "normalize_prompt_annotation_snapshot",
    "normalize_prompt_annotation_snapshots",
    "prompt_annotations_from_transcript_envelope",
    "render_historical_prompt_annotation_context",
]
