"""Format-neutral tools for a restricted annotated document turn.

The four tools intentionally describe user-level document operations rather
than HTML implementation steps.  The current adapter is HTML-only; future
Office adapters can keep this model contract while issuing semantic grants for
paragraphs, cells, or slide shapes instead of source ranges.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from opensquilla.artifact_session.errors import ArtifactConflictError
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.builtin.artifact_editing import (
    _MAX_HTML_READ_CHUNK_CHARS,
    _NO_ARGUMENTS_SCHEMA,
    _annotation_order,
    _bounded_anchor_text,
    _commit_prepared_document_mutation,
    _current_payload,
    _current_scope,
    _decode_html,
    _document_grant_binding,
    _json,
    _prepare_document_mutation,
    _prepare_document_text_patch,
    _range_binding,
    _range_error,
    _require_single_file_html,
    _take_utf8_chunk,
)
from opensquilla.tools.builtin.artifact_range_grants import (
    ArtifactRangeGrantError,
    document_grant_registry_for_context,
    registry_for_context,
)
from opensquilla.tools.builtin.document_format_adapters import (
    DOCUMENT_SEMANTIC_OPERATIONS,
    DocumentAdapterError,
    DocumentFormatAdapter,
    DocumentMutationError,
    DocumentMutationTarget,
    get_document_format_adapter,
    mutation_error_from_adapter,
)
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, SafeToolError, current_tool_context

_DEFAULT_DOCUMENT_READ_CHARS = 8 * 1024
_MAX_DOCUMENT_MUTATIONS = 100
_MAX_DOCUMENT_LOCATION_PREVIEW_BYTES = 4 * 1024
_INITIAL_OPERATIONS = ("replace_text", "set_style", "remove_node")


def _adapter_error(exc: DocumentAdapterError) -> DocumentMutationError:
    return mutation_error_from_adapter(exc)


async def _html_adapter_scope(tool_name: str, *, require_anchor: bool = False):
    scope = await _current_scope(
        tool_name,
        require_anchor=require_anchor,
        required_format="html",
    )
    store, ref, raw = await _current_payload(scope)
    await _require_single_file_html(scope, store, ref)
    try:
        adapter = get_document_format_adapter(scope.context.artifact_format)
    except DocumentAdapterError as exc:
        raise _adapter_error(exc) from None
    return scope, ref, raw, adapter


def _grant_payload(
    *,
    adapter: DocumentFormatAdapter,
    scope: Any,
    source_sha256: str,
    target: DocumentMutationTarget,
) -> dict[str, object]:
    registry = document_grant_registry_for_context(scope.ctx)
    try:
        token = registry.mint_grant(
            binding=_document_grant_binding(
                scope,
                source_sha256,
                adapter_id=adapter.format_id,
                adapter_version=adapter.adapter_version,
            ),
            operation=target.operation,
            target_fingerprint=target.target_fingerprint,
            annotation_orders=target.annotation_orders,
            adapter_locator=target.adapter_locator,
        )
    except ArtifactRangeGrantError as exc:
        raise _range_error(exc) from None
    current = target.current
    current_end = len(current)
    while (
        current_end > 0
        and len(current[:current_end].encode("utf-8"))
        > _MAX_DOCUMENT_LOCATION_PREVIEW_BYTES
    ):
        current_end -= max(1, current_end // 8)
    current_preview = current[:current_end]
    expects_input = target.operation in {
        "replace_text",
        "set_attribute",
        "set_style",
    }
    input_kind: str | None = {
        "replace_text": "text",
        "set_attribute": "attribute_value",
        "set_style": "css_declarations",
        "remove_attribute": None,
        "remove_node": None,
    }[target.operation]
    apply_template: dict[str, object] = {"grant_token": token}
    input_schema: dict[str, object] | None = None
    if expects_input:
        apply_template["input"] = ""
        input_schema = {"type": "string"}
        if target.operation in {"replace_text", "set_style"}:
            input_schema["minLength"] = 1
    if target.operation == "set_style":
        assert input_schema is not None
        input_schema.update(
            {
                "format": "css-declaration-list",
                "description": (
                    "A CSS declaration list without selectors, rule braces, or a style wrapper."
                ),
                "examples": ["color: #222; background-color: #fff;"],
            }
        )
    return {
        "grantToken": token,
        "operation": target.operation,
        "current": current_preview,
        "currentTruncated": current_end < len(current),
        "before": target.before,
        "after": target.after,
        "confidence": target.confidence,
        "detail": target.detail,
        "expectsInput": expects_input,
        "inputKind": input_kind,
        "inputSchema": input_schema,
        "applyTemplate": apply_template,
    }


def _locations_for_operation(
    *,
    adapter: DocumentFormatAdapter,
    scope: Any,
    payload: bytes,
    source_sha256: str,
    annotation_order: int,
    operation: str,
    attribute_name: str | None = None,
) -> list[dict[str, object]]:
    try:
        ranges = adapter.locate(
            payload,
            anchor_locator=scope.anchors[annotation_order].locator,
            annotation_order=annotation_order,
            operation=operation,
            attribute_name=attribute_name,
        )
    except DocumentAdapterError as exc:
        raise _adapter_error(exc) from None
    return [
        _grant_payload(
            adapter=adapter,
            scope=scope,
            source_sha256=source_sha256,
            target=value,
        )
        for value in ranges
    ]


@tool(
    name="document_inspect",
    description=(
        "Inspect the annotated document, ordered user instructions, adapter capabilities, "
        "and initial semantic mutation grants. No path, source offset, or internal identifier "
        "is returned."
    ),
    params=_NO_ARGUMENTS_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.inspect"),
)
async def document_inspect() -> str:
    scope, ref, payload, adapter = await _html_adapter_scope(
        "document_inspect",
        require_anchor=True,
    )
    from opensquilla.gateway.artifact_contexts import BoundPromptAnnotationContext
    from opensquilla.prompt_annotations import normalize_prompt_annotation_snapshots

    if not isinstance(scope.context, BoundPromptAnnotationContext):
        raise SafeToolError("No prompt annotations are bound to this document turn.")
    snapshots = normalize_prompt_annotation_snapshots(scope.context.snapshots)
    if len(snapshots) != len(scope.anchors):
        raise SafeToolError("The annotated document context is stale.")
    annotations: list[dict[str, object]] = []
    for order, (snapshot, anchor) in enumerate(zip(snapshots, scope.anchors, strict=True)):
        locations: list[dict[str, object]] = []
        for operation in _INITIAL_OPERATIONS:
            locations.extend(
                _locations_for_operation(
                    adapter=adapter,
                    scope=scope,
                    payload=payload,
                    source_sha256=ref.sha256,
                    annotation_order=order,
                    operation=operation,
                )
            )
        tag_name = anchor.locator.get("tag_name") if isinstance(anchor.locator, dict) else None
        annotations.append(
            {
                "order": snapshot["order"],
                "instruction": snapshot["body"],
                "selection": {
                    "tag": tag_name if isinstance(tag_name, str) else None,
                    "quote": _bounded_anchor_text(anchor.quote, "quote"),
                },
                "initialLocations": locations,
            }
        )
    try:
        structure = adapter.inspect(_decode_html(payload))
    except DocumentAdapterError as exc:
        raise _adapter_error(exc) from None
    return _json(
        {
            "status": "ok",
            "document": {
                "name": scope.document.name,
                "format": adapter.format_id,
                "generation": scope.document.generation,
            },
            "revision": {"sha256": ref.sha256, "bytes": ref.size},
            "adapter": adapter.capabilities(),
            "structure": structure,
            "annotations": annotations,
        }
    )


_DOCUMENT_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "enum": ["source", "structure"]},
        "cursor": {"type": "string", "pattern": "^hcur_[A-Za-z0-9_-]{43}$"},
        "max_chars": {
            "type": "integer",
            "minimum": 256,
            "maximum": _MAX_HTML_READ_CHUNK_CHARS,
        },
    },
    "required": ["view"],
    "additionalProperties": False,
}


@tool(
    name="document_read",
    description=(
        "Read a bounded canonical document view. Use view=structure for the semantic outline "
        "or view=source for a UTF-8 page. Source pages are read-only and contain no edit "
        "authority or offsets."
    ),
    params=_DOCUMENT_READ_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.read"),
)
async def document_read(
    view: str,
    cursor: str | None = None,
    max_chars: int = _DEFAULT_DOCUMENT_READ_CHARS,
) -> str:
    scope, ref, payload, adapter = await _html_adapter_scope("document_read")
    source = _decode_html(payload)
    if view == "structure":
        if cursor is not None:
            raise SafeToolError("DOCUMENT_CURSOR_UNEXPECTED: Structure view has no cursor.")
        try:
            structure = adapter.read(source, view="structure")
        except DocumentAdapterError as exc:
            raise _adapter_error(exc) from None
        return _json(
            {
                "status": "ok",
                "view": "structure",
                "format": adapter.format_id,
                "structure": structure,
            }
        )
    if view != "source":
        raise SafeToolError("DOCUMENT_VIEW_INVALID: Choose source or structure.")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < 256
        or max_chars > _MAX_HTML_READ_CHUNK_CHARS
    ):
        raise SafeToolError("DOCUMENT_PAGE_INVALID: The source page size is invalid.")
    registry = registry_for_context(scope.ctx)
    binding = _range_binding(
        scope,
        ref.sha256,
        adapter_id=adapter.format_id,
        adapter_version=adapter.adapter_version,
    )
    start = 0
    if cursor is not None:
        try:
            start = registry.consume_cursor(binding=binding, token=cursor)
        except ArtifactRangeGrantError as exc:
            raise _range_error(exc) from None
    if start >= len(source):
        raise SafeToolError("DOCUMENT_CURSOR_INVALID: The cursor is past the document.")
    canonical = adapter.read(source, view="source")
    if not isinstance(canonical, str):
        raise SafeToolError("DOCUMENT_VIEW_INVALID: The source view is unavailable.")
    text, end = _take_utf8_chunk(canonical, start, max_chars)
    next_cursor: str | None = None
    if end < len(canonical):
        try:
            next_cursor = registry.mint_cursor(binding=binding, position=end)
        except ArtifactRangeGrantError as exc:
            raise _range_error(exc) from None
    return _json(
        {
            "status": "ok",
            "view": "source",
            "format": adapter.format_id,
            "sha256": ref.sha256,
            "chunk": {"text": text, "characters": len(text), "bytes": len(text.encode("utf-8"))},
            "nextCursor": next_cursor,
            "hasMore": next_cursor is not None,
        }
    )


_DOCUMENT_LOCATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "annotation_order": {"type": "integer", "minimum": 0},
        "operation": {
            "type": "string",
            "enum": sorted(DOCUMENT_SEMANTIC_OPERATIONS),
        },
        "attribute_name": {"type": "string", "minLength": 1, "maxLength": 128},
    },
    "required": ["annotation_order", "operation"],
    "additionalProperties": False,
}


@tool(
    name="document_locate",
    description=(
        "Locate one exact selected-document target for replace_text, set_attribute, "
        "remove_attribute, set_style, or remove_node. Returns an opaque, turn-scoped grant; "
        "never returns source offsets. Reuse an existing grant instead of locating the same "
        "annotation and operation again. For set_style, apply only a CSS declaration list "
        "such as 'color: #222; background-color: #fff;' without selectors, braces, or a "
        "style= wrapper."
    ),
    params=_DOCUMENT_LOCATE_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    plan_access=PlanAccess.READ_ONLY,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.locate"),
)
async def document_locate(
    annotation_order: int,
    operation: str,
    attribute_name: str | None = None,
) -> str:
    if operation not in DOCUMENT_SEMANTIC_OPERATIONS:
        raise SafeToolError(
            "DOCUMENT_OPERATION_UNSUPPORTED: The requested semantic operation is not supported."
        )
    attribute_operations = {"set_attribute", "remove_attribute"}
    if operation in attribute_operations and not isinstance(attribute_name, str):
        raise SafeToolError(
            "DOCUMENT_ATTRIBUTE_REQUIRED: This operation requires an attribute name."
        )
    if operation not in attribute_operations and attribute_name is not None:
        raise SafeToolError(
            "DOCUMENT_ATTRIBUTE_UNEXPECTED: This operation does not accept an attribute name."
        )
    scope, ref, payload, adapter = await _html_adapter_scope(
        "document_locate",
        require_anchor=True,
    )
    order = _annotation_order(annotation_order, len(scope.anchors))
    query_key = json.dumps(
        [
            scope.document.document_id,
            scope.revision.revision_id,
            order,
            operation,
            attribute_name,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        remaining_queries = document_grant_registry_for_context(
            scope.ctx
        ).consume_query_budget(query_key=query_key)
    except ArtifactRangeGrantError as exc:
        raise _range_error(exc) from None
    locations = _locations_for_operation(
        adapter=adapter,
        scope=scope,
        payload=payload,
        source_sha256=ref.sha256,
        annotation_order=order,
        operation=operation,
        attribute_name=attribute_name,
    )
    return _json(
        {
            "status": "ok" if locations else "not_found",
            "annotationOrder": order,
            "operation": operation,
            "attributeName": attribute_name,
            "locations": locations,
            "retryAllowed": not locations,
            "remainingUniqueLocateQueries": remaining_queries,
        }
    )


_DOCUMENT_APPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mutations": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_DOCUMENT_MUTATIONS,
            "items": {
                "type": "object",
                "properties": {
                    "grant_token": {
                        "type": "string",
                        "pattern": "^hrg_[A-Za-z0-9_-]{43}$",
                    },
                    "input": {},
                },
                "required": ["grant_token"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["mutations"],
    "additionalProperties": False,
}


@tool(
    name="document_apply",
    description=(
        "Apply one atomic set of semantic mutations using opaque grants. "
        "Each used grant must remain bound to a current selection. Validation, candidate "
        "creation, CAS, revision, change set, and receipt are completed server-side."
    ),
    params=_DOCUMENT_APPLY_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.apply"),
    runtime_only_arguments={"_tool_use_id"},
)
async def document_apply(
    mutations: list[dict[str, object]],
    _tool_use_id: str | None = None,
) -> str:
    canonical_mutations: list[dict[str, object]] = []
    if not isinstance(mutations, list):
        raise DocumentMutationError(
            "DOCUMENT_MUTATIONS_INVALID",
            "Mutations must be an array.",
            retry_policy="correctable",
        )
    for mutation in mutations:
        if not isinstance(mutation, dict):
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_INVALID",
                "Every mutation must be an object.",
                retry_policy="correctable",
            )
        canonical_mutations.append(dict(mutation))
    try:
        proposal_payload = json.dumps(
            {"mutations": canonical_mutations, "tool": "document_apply"},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_INPUT_INVALID",
            "Mutation input must be valid JSON.",
            retry_policy="correctable",
        ) from None
    proposal_sha256 = hashlib.sha256(proposal_payload).hexdigest()
    tool_context = current_tool_context.get()
    controller = (
        tool_context.artifact_mutation_attempt_controller
        if tool_context is not None
        else None
    )
    if controller is None or not isinstance(_tool_use_id, str) or not _tool_use_id:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_AUTHORITY_UNAVAILABLE",
            "Commit authority is unavailable.",
            retry_policy="forbidden",
        )
    try:
        replay = await controller.replay_commit(_tool_use_id, proposal_sha256)
    except ArtifactConflictError:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_REPLAY_CONFLICT",
            "The mutation replay does not match the original proposal.",
            retry_policy="forbidden",
        ) from None
    if replay is not None:
        return _json({"status": "replayed"})
    _scope, ref, _source, _adapter = await _html_adapter_scope(
        "document_apply",
        require_anchor=True,
    )
    summary = f"Applied {len(canonical_mutations)} document mutations"
    prepared = None
    try:
        prepared = await _prepare_document_mutation(
            ref.sha256,
            [],
            summary,
            _tool_name="document_apply",
            _semantic_operations=canonical_mutations,
            _proposal_sha256=proposal_sha256,
        )
        reservation = await controller.reserve_commit(
            _tool_use_id,
            prepared.proposal_sha256,
        )
        if not reservation.created:
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_REPLAY",
                "The mutation attempt is already reserved.",
                retry_policy="refresh",
            )
    except BaseException:
        if prepared is not None:
            prepared.release_grants()
        raise
    return await _commit_prepared_document_mutation(prepared)


_DOCUMENT_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "expectedSha256": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{64}$",
        },
        "edits": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_DOCUMENT_MUTATIONS,
            "items": {
                "type": "object",
                "properties": {
                    "expectedText": {"type": "string", "minLength": 1},
                    "replacement": {"type": "string"},
                },
                "required": ["expectedText", "replacement"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["expectedSha256", "edits"],
    "additionalProperties": False,
}


@tool(
    name="document_patch",
    description=(
        "Atomically patch the current bound HTML document. First call document_read with "
        "view=source, then pass its sha256 and exact non-empty source fragments. Every "
        "expectedText must occur exactly once; all edits apply together or none do."
    ),
    params=_DOCUMENT_PATCH_SCHEMA,
    owner_only=True,
    exposed_by_default=False,
    result_budget_class="artifact",
    sandbox=SandboxToolDescriptor.artifact(kind="document.patch"),
    runtime_only_arguments={"_tool_use_id"},
)
async def document_patch(
    expectedSha256: str,  # noqa: N803 - public tool schema uses camelCase
    edits: list[dict[str, object]],
    _tool_use_id: str | None = None,
) -> str:
    if not isinstance(edits, list):
        raise DocumentMutationError(
            "DOCUMENT_PATCH_EDITS_INVALID",
            "edits must be an array.",
            retry_policy="correctable",
        )
    canonical_edits = [dict(edit) if isinstance(edit, dict) else edit for edit in edits]
    try:
        proposal_payload = json.dumps(
            {
                "edits": canonical_edits,
                "expectedSha256": expectedSha256,
                "tool": "document_patch",
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise DocumentMutationError(
            "DOCUMENT_PATCH_INPUT_INVALID",
            "Patch input must be valid JSON.",
            retry_policy="correctable",
        ) from None
    proposal_sha256 = hashlib.sha256(proposal_payload).hexdigest()
    tool_context = current_tool_context.get()
    controller = (
        tool_context.artifact_mutation_attempt_controller
        if tool_context is not None
        else None
    )
    if controller is None or not isinstance(_tool_use_id, str) or not _tool_use_id:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_AUTHORITY_UNAVAILABLE",
            "Commit authority is unavailable.",
            retry_policy="forbidden",
        )
    try:
        replay = await controller.replay_commit(_tool_use_id, proposal_sha256)
    except ArtifactConflictError:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_REPLAY_CONFLICT",
            "The mutation replay does not match the original proposal.",
            retry_policy="forbidden",
        ) from None
    if replay is not None:
        return _json({"status": "replayed"})

    prepared = None
    try:
        prepared = await _prepare_document_text_patch(
            expectedSha256,
            canonical_edits,  # type: ignore[arg-type]
            proposal_sha256=proposal_sha256,
        )
        reservation = await controller.reserve_commit(
            _tool_use_id,
            prepared.proposal_sha256,
        )
        if not reservation.created:
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_REPLAY",
                "The mutation attempt is already reserved.",
                retry_policy="refresh",
            )
    except BaseException:
        if prepared is not None:
            prepared.release_grants()
        raise
    return await _commit_prepared_document_mutation(prepared)


__all__ = [
    "document_apply",
    "document_inspect",
    "document_locate",
    "document_patch",
    "document_read",
]
