"""Chat transcript normalization shared by frontends."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from itertools import islice
from typing import Any, Literal

from opensquilla.artifacts import artifact_payload, strip_artifact_markers_from_text
from opensquilla.chat.flattened_tool_markers import (
    flattened_used_tool_names,
    has_flattened_used_tool_line,
    parse_flattened_tool_result_dumps,
    strip_confirmed_flattened_tool_result,
    strip_flattened_used_tool_lines,
)
from opensquilla.content_reader import MAX_DISPLAY_CONTENT_BYTES
from opensquilla.contracts.tool_presentation import (
    project_tool_arguments_payload,
    resolve_tool_presentation_fields,
)
from opensquilla.silent_reply import sanitize_historical_silent_reply
from opensquilla.turn_outcome_projection import public_turn_context

_LEGACY_PLAN_IMPLEMENTATION_PROMPT = re.compile(
    r'Implement the approved plan “.+”\. '
    r"Work through its ordered steps and record truthful checkpoints\."
)

# ``chat.history`` is a control-plane message.  Keep the compatibility
# projection available to callers that still need the historical full text,
# but make the v4 wire projection explicitly bounded.  The range endpoint is
# the authority for the remainder of a legacy body.
HISTORY_PROJECTION_MODE = Literal["legacy", "bounded"]
DEFAULT_HISTORY_PREVIEW_BYTES = 16 * 1024
# Finalizer segments can duplicate the body, and tool details can carry equally
# large nested values. Their display payloads share a budget; identity, status,
# order, presentation, usage and attachment references are never abbreviated.
_HISTORY_SEGMENT_PAYLOAD_BYTES = 128 * 1024
_SEGMENT_PAYLOAD_FIELDS = {
    "text": ("text", "raw"),
    "reasoning": ("text", "reasoning"),
    "tool_use": ("input", "arguments"),
    "tool_result": ("content", "result", "output", "stdout", "stderr", "error"),
}

# A bounded history frame may still need to show a short preview when the
# persisted row cannot be safely rehydrated through the display projection.
# Keep this vocabulary stable and machine-readable: the client must never
# infer that an omitted ``contentRef`` means the body was empty or lost.
CONTENT_UNAVAILABLE_DISPLAY_TOO_LARGE = "display_projection_too_large"
CONTENT_UNAVAILABLE_METADATA_PENDING = "content_metadata_pending"
CONTENT_UNAVAILABLE_REFERENCE_UNAVAILABLE = "content_reference_unavailable"


def _bounded_history_preview(content: str, max_bytes: int) -> tuple[str, bool, int]:
    """Return a UTF-8-safe preview and the original encoded byte length."""

    encoded = content.encode("utf-8")
    byte_length = len(encoded)
    if byte_length <= max_bytes:
        return content, False, byte_length
    # Decoding with ``ignore`` can only shorten the result, and therefore keeps
    # the advertised budget true even when the cut lands inside a codepoint.
    return encoded[:max_bytes].decode("utf-8", "ignore"), True, byte_length


def _json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bounded_detail_payload(
    value: Any, budget: int, path: tuple[Any, ...], truncated: Callable[[tuple[Any, ...]], None],
) -> Any:
    """Preview one text/detail value, never a segment's identity or control metadata."""
    if isinstance(value, str):
        preview, cut, _ = _bounded_history_preview(
            value, min(DEFAULT_HISTORY_PREVIEW_BYTES, budget - 2),
        )
        # JSON escaping can expand otherwise small UTF-8 text (e.g. control
        # characters); account for the actual encoded wire representation.
        while _json_bytes(preview) > budget:
            preview = preview[:len(preview) // 2]
            cut = True
        if cut:
            truncated(path)
        return preview
    if isinstance(value, (dict, list)):
        if len(path) >= 12:
            truncated(path)
            return {} if isinstance(value, dict) else []
        items = (
            list(islice(value.items(), 256))
            if isinstance(value, dict) else list(enumerate(value[:256]))
        )
        if len(items) != len(value):
            truncated(path)
        overhead = 2
        minimum = 0
        selected = []
        for key, item in items:
            cost = (_json_bytes(key) + 1 if isinstance(value, dict) else 0) + bool(selected)
            item_minimum = 4 if isinstance(item, (str, dict, list)) else _json_bytes(item)
            if overhead + cost + minimum + item_minimum > budget:
                truncated(path)
                break
            overhead += cost
            minimum += item_minimum
            selected.append((key, item, item_minimum))
        remaining = budget - overhead
        projected = {} if isinstance(value, dict) else []
        for index, (key, item, item_minimum) in enumerate(selected):
            minimum -= item_minimum
            child_budget = min(
                remaining - minimum, max(item_minimum, remaining // (len(selected) - index)),
            )
            child = _bounded_detail_payload(item, child_budget, (*path, key), truncated)
            remaining -= _json_bytes(child)
            if isinstance(projected, dict):
                projected[key] = child
            else:
                projected.append(child)
        return projected
    return value


def _bounded_result_payload(
    value: Any, budget: int, path: tuple[Any, ...], truncated: Callable[[tuple[Any, ...]], None],
) -> Any:
    # Tool results also carry workspace/session/clarify actions. A JSON string
    # is an envelope, not prose: never turn a valid reference into a cut prefix.
    parsed = value
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            pass
    if not isinstance(parsed, dict):
        return _bounded_detail_payload(value, budget, path, truncated)
    projected = dict(parsed)
    body_fields = [
        field for field in (
            "text", "content", "body", "output", "stdout", "stderr", "diff", "patch",
        )
        if isinstance(parsed.get(field), str)
        and len(parsed[field].encode("utf-8")) > DEFAULT_HISTORY_PREVIEW_BYTES
    ]
    # These are display bodies only. IDs, resource/session references, URLs,
    # statuses, actions and other structured result fields remain authoritative.
    # Pathological control metadata still gets the page's explicit size error.
    for field in body_fields:
        projected[field] = _bounded_detail_payload(
            parsed[field], max(4, budget // (2 * len(body_fields))), (*path, field), truncated,
        )
    if not body_fields:
        return value
    return (
        json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
        if isinstance(value, str) else projected
    )


def _bound_history_display_payloads(message: dict[str, Any]) -> None:
    segments = message.get("tool_calls", [])
    preview: dict[str, Any] = {}
    text_cut = False

    def truncated(path: tuple[Any, ...]) -> None:
        nonlocal text_cut
        if path[0] == "tool_calls":
            if (
                len(path) == 3 and path[2] in {"text", "raw"}
                and isinstance(segments[path[1]], dict)
                and segments[path[1]].get("type") == "text"
            ):
                text_cut = True
                return
        # These fields have no display-body range reader. Explicitly disclose
        # their partial details instead of presenting a shortened value as full.
        preview["detailsTruncated"] = True
        if path[0] == "reasoning_content":
            preview["reasoningUtf16Length"] = (
                len(message["reasoning_content"].encode("utf-16-le")) // 2
            )

    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        message["reasoning_content"] = _bounded_detail_payload(
            reasoning, DEFAULT_HISTORY_PREVIEW_BYTES, ("reasoning_content",), truncated,
        )
    payloads = [
        (index, field) for index, segment in enumerate(segments) if isinstance(segment, dict)
        for field in _SEGMENT_PAYLOAD_FIELDS.get(str(segment.get("type")), ())
        if field in segment
    ]
    payload_budget = max(4, _HISTORY_SEGMENT_PAYLOAD_BYTES // max(1, len(payloads)))
    projected_segments = [
        dict(segment) if isinstance(segment, dict) else segment for segment in segments
    ]
    for index, field in payloads:
        projector = (
            _bounded_result_payload if segments[index].get("type") == "tool_result"
            else _bounded_detail_payload
        )
        projected_segments[index][field] = projector(
            segments[index][field], payload_budget, ("tool_calls", index, field), truncated,
        )
    if segments:
        message["tool_calls"] = projected_segments
    if text_cut:
        # The activity snapshot refers to original UTF-16 lengths. Preserve
        # those references without relabelling its checksum or chronological
        # orders as invalid merely because the display payload is a preview.
        preview["textUtf16Lengths"] = [
            len(str(segment.get("text") or segment.get("raw") or "").encode("utf-16-le")) // 2
            for segment in segments
            if isinstance(segment, dict) and segment.get("type") == "text"
            and (segment.get("text") or segment.get("raw"))
        ]
    if preview:
        message["historyPayloadPreview"] = preview


def _legacy_tool_presentation(segment: dict[str, Any]) -> dict[str, Any] | None:
    """Classify old tool-use rows that predate persisted presentation metadata."""

    if segment.get("type") != "tool_use" or not isinstance(segment.get("input"), dict):
        return None
    name = segment.get("name") or segment.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    return resolve_tool_presentation_fields(
        name=name,
        parameter_names=tuple(str(key) for key in segment["input"]),
    ).to_payload()


def _sanitize_display_protocol_payload(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_sanitize_display_protocol_payload(item) for item in value]
    if isinstance(value, dict):
        projected = {
            key: _sanitize_display_protocol_payload(item)
            for key, item in value.items()
        }
        presentation = projected.get("tool_presentation")
        if not isinstance(presentation, dict):
            presentation = _legacy_tool_presentation(projected)
        if (
            projected.get("type") in {"tool_use", "tool_result"}
            and isinstance(presentation, dict)
        ):
            for field in ("input", "arguments"):
                if field not in projected:
                    continue
                arguments = projected[field]
                if isinstance(arguments, dict):
                    projected[field] = project_tool_arguments_payload(
                        presentation,
                        arguments,
                    )
                elif presentation.get("argumentDisplay") == "primary":
                    projected[field] = {}
        return projected
    return value


def _public_attachment_projection(value: Any) -> Any:
    """Hide occurrence ids that never identified addressable attachment material."""

    if not isinstance(value, list):
        return value
    return [
        {
            key: item
            for key, item in attachment.items()
            if key != "attachment_id" or not attachment.get("missing_reason")
        }
        if isinstance(attachment, dict)
        else attachment
        for attachment in value
    ]


def _tool_display_value(value: Any) -> str | None:
    """Extract safe visible text from a persisted tool-result payload.

    Tool rows are protocol records in some legacy stores, rather than plain
    text.  The display projection must expose the result body while never
    sending tool arguments or the protocol envelope back to the renderer.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _tool_display_value(item)
            if text is not None and text != "":
                parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        for key in (
            "display_text",
            "text",
            "content",
            "output",
            "result",
            "stdout",
            "error",
        ):
            if key not in value:
                continue
            text = _tool_display_value(value[key])
            if text is not None:
                return text
    return None


def _tool_result_display_text(content: Any) -> tuple[Any, bool]:
    """Project a role=tool JSON envelope to its result text, if recognized."""

    if not isinstance(content, str) or not content.lstrip().startswith(("{", "[")):
        return content, False
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError):
        # A truncated bounded preview can retain the protocol discriminator
        # without being valid JSON.  Fail closed for that shape; treating it
        # as ordinary tool text would leak the partial wire envelope.
        marker = content.lstrip()[:80].lower()
        if '"type"' in marker and any(
            f'"{kind}"' in marker
            for kind in ("tool_result", "tool_output", "tool_response")
        ):
            return "", True
        return content, False
    recognized_types = {"tool_result", "tool_output", "tool_response"}
    is_tool_envelope = (
        isinstance(payload, dict) and payload.get("type") in recognized_types
    ) or (
        isinstance(payload, list)
        and any(
            isinstance(item, dict) and item.get("type") in recognized_types
            for item in payload
        )
    )
    if is_tool_envelope:
        text = _tool_display_value(payload)
        return (text if text is not None else ""), True
    return content, False


def _is_legacy_generated_plan_implementation(
    content: str,
    turn_context: Any,
) -> bool:
    """Recognize the exact pre-display_text PlanRun control prompt.

    Older gateways persisted the generated provider instruction as ordinary
    user-visible text. The positive PlanRun id plus the exact server template
    makes this a protocol compatibility check, not a guess based on user prose.
    Explicit implementation messages do not use this template and remain
    visible.
    """

    if not isinstance(turn_context, dict) or not turn_context.get("plan_run_id"):
        return False
    visible = str(content or "").strip()
    if visible.startswith("[") and "]\n" in visible:
        visible = visible.split("]\n", 1)[1].strip()
    return _LEGACY_PLAN_IMPLEMENTATION_PROMPT.fullmatch(visible) is not None


def _legacy_flattened_tool_result_pairs(entries: list[object]) -> dict[int, int]:
    """Map legacy result rows to their adjacent flattened assistant call row.

    Modern rows carry ``tool_call_id`` or role ``tool``. Older compaction
    projections sometimes persisted Anthropic-style tool results as role
    ``user`` with no structured identity, so recognize only the adjacent
    assistant-marker/result pair. An isolated user message that merely quotes
    the legacy syntax must remain ordinary conversation text.
    """

    pairs: dict[int, int] = {}
    previous_flattened_call: int | None = None
    for index, entry in enumerate(entries):
        role = str(getattr(entry, "role", "unknown") or "unknown").lower()
        content = str(getattr(entry, "content", "") or "")
        if (
            previous_flattened_call is not None
            and role in {"tool", "user"}
            and _legacy_tool_activity_segments(
                entries[previous_flattened_call],
                entry,
            )
            is not None
        ):
            pairs[index] = previous_flattened_call
        previous_flattened_call = (
            index
            if (
                role == "assistant"
                and has_flattened_used_tool_line(content)
                and not getattr(entry, "tool_calls", None)
            )
            else None
        )
    return pairs


def _legacy_tool_activity_segments(
    tool_entry: object,
    result_entry: object | None = None,
) -> list[dict[str, Any]] | None:
    """Project confirmed legacy text into the existing auditable tool timeline."""

    tool_content = str(getattr(tool_entry, "content", "") or "")
    names = flattened_used_tool_names(tool_content)
    if not names:
        return None
    parsed_results = (
        parse_flattened_tool_result_dumps(str(getattr(result_entry, "content", "") or ""))
        if result_entry is not None
        else None
    )
    if parsed_results is None or len(parsed_results.results) != len(names):
        return None
    result_ids = [result.tool_use_id for result in parsed_results.results]
    if len(set(result_ids)) != len(result_ids):
        return None
    segments: list[dict[str, Any]] = []
    text_lines: list[str] = []
    tool_index = 0

    def flush_text() -> None:
        text = "".join(text_lines).strip()
        text_lines.clear()
        if text:
            segments.append({"type": "text", "text": text})

    for line in tool_content.splitlines(keepends=True):
        line_names = flattened_used_tool_names(line)
        if len(line_names) != 1:
            text_lines.append(line)
            continue
        flush_text()
        name = names[tool_index]
        tool_use_id = result_ids[tool_index]
        segments.append(
            {
                "type": "tool_use",
                "tool_use_id": tool_use_id,
                "name": name,
                "input": {},
                "legacy_projection": True,
            }
        )
        tool_index += 1
    flush_text()
    if tool_index != len(names):
        return None

    for name, result in zip(names, parsed_results.results, strict=True):
        segments.append(
            {
                "type": "tool_result",
                "tool_use_id": result.tool_use_id,
                "name": name,
                "result": result.content,
                "legacy_projection": True,
            }
        )
    return segments


def transcript_entries_to_chat_messages(
    entries: list[object],
    *,
    limit: int | None = None,
    previous_entry: object | None = None,
    next_entry: object | None = None,
    content_mode: HISTORY_PROJECTION_MODE = "legacy",
    preview_bytes: int = DEFAULT_HISTORY_PREVIEW_BYTES,
) -> list[dict[str, Any]]:
    """Project transcript rows into frontend messages.

    ``legacy`` preserves the historical in-process projection for callers that
    still consume the complete body.  ``bounded`` is the v4 control-plane
    contract: ``text`` is capped and ``contentRef.byteLength`` records the
    original body size so a client can fetch the remainder through
    ``content.read.v1``.  Keeping the mode explicit avoids silently changing
    non-Gateway callers while preventing large legacy rows from crossing the
    history wire.
    """

    if content_mode not in ("legacy", "bounded"):
        raise ValueError("content_mode must be 'legacy' or 'bounded'")
    if isinstance(preview_bytes, bool) or not isinstance(preview_bytes, int):
        raise ValueError("preview_bytes must be an integer")
    if preview_bytes <= 0:
        raise ValueError("preview_bytes must be positive")
    selected = entries[-limit:] if limit is not None else entries
    context_entries = [
        *([previous_entry] if previous_entry is not None else []),
        *selected,
        *([next_entry] if next_entry is not None else []),
    ]
    selected_offset = 1 if previous_entry is not None else 0
    legacy_tool_result_pairs = _legacy_flattened_tool_result_pairs(context_entries)
    selected_start = selected_offset
    selected_end = selected_start + len(selected)
    legacy_projection_by_owner: dict[int, tuple[object, list[dict[str, Any]]]] = {}
    legacy_cursor_bounds: dict[int, tuple[object, object]] = {}
    suppressed_legacy_indexes: set[int] = set()
    for result_index, tool_index in legacy_tool_result_pairs.items():
        tool_selected = selected_start <= tool_index < selected_end
        result_selected = selected_start <= result_index < selected_end
        if not result_selected:
            if tool_selected:
                # Defer the combined activity until the result-owning page is
                # loaded, so refresh/prepend cannot render duplicate halves.
                suppressed_legacy_indexes.add(tool_index)
            continue
        segments = _legacy_tool_activity_segments(
            context_entries[tool_index],
            context_entries[result_index],
        )
        if segments is None:
            continue
        owner_index = tool_index if tool_selected else result_index
        legacy_projection_by_owner[owner_index] = (
            context_entries[tool_index],
            segments,
        )
        legacy_cursor_bounds[owner_index] = (
            context_entries[tool_index if tool_selected else result_index],
            context_entries[result_index],
        )
        if tool_selected:
            suppressed_legacy_indexes.add(result_index)
    messages: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(selected):
        context_index = selected_offset + entry_index
        if context_index in suppressed_legacy_indexes:
            continue
        legacy_projection = legacy_projection_by_owner.get(context_index)
        projected_entry = legacy_projection[0] if legacy_projection else entry
        raw_content = getattr(projected_entry, "content", "") or ""
        raw_content_text = raw_content if isinstance(raw_content, str) else str(raw_content)
        projected_byte_length = getattr(projected_entry, "content_byte_length", None)
        content_metadata_pending = bool(
            getattr(projected_entry, "content_metadata_pending", False)
        )
        content_byte_length = (
            max(0, int(projected_byte_length))
            if (
                isinstance(projected_byte_length, int)
                and not isinstance(projected_byte_length, bool)
            )
            else len(raw_content_text.encode("utf-8"))
        )
        raw_body_truncated = (
            content_mode == "bounded"
            and bool(getattr(projected_entry, "content_truncated", (
                    isinstance(projected_byte_length, int)
                    and not isinstance(projected_byte_length, bool)
                    and projected_byte_length > len(raw_content_text.encode("utf-8"))
                )))
        )
        role = getattr(projected_entry, "role", "unknown")
        turn_context = getattr(projected_entry, "turn_context", None)
        silent_reply = sanitize_historical_silent_reply(
            getattr(projected_entry, "content", "") or "",
            getattr(projected_entry, "tool_calls", None),
            role=role,
            turn_context=turn_context if isinstance(turn_context, dict) else None,
        )
        content = "" if legacy_projection else (silent_reply.content or "")
        if role == "tool":
            content, tool_payload_projected = _tool_result_display_text(content)
        else:
            tool_payload_projected = False
        raw_display_hydration_safe = (
            not legacy_projection
            and not tool_payload_projected
            and content == raw_content_text
        )
        # A bounded SQL projection can cut a protocol JSON/tool payload before
        # it reaches the parser. Showing that prefix would leak raw transport
        # text into history (and the semantic endpoint would only repair it
        # after a click). Keep the initial preview empty; the emitted
        # ``view=display`` ref lets the server return the safe projection.
        protocol_projection_suppressed = raw_body_truncated and (
            # The tool projector has already suppressed a truncated envelope.
            # Preserve that fact so the empty preview still gets a semantic
            # read reference; inspecting only ``content`` loses its prefix.
            (tool_payload_projected and not content.strip())
            or content.lstrip().startswith("{")
            or content.lstrip().startswith("[ContentBlock")
            or content.lstrip().startswith("[Used tool:")
        )
        if protocol_projection_suppressed:
            content = ""
            raw_display_hydration_safe = False
        # A range endpoint serves the persisted body, so only expose it as a
        # renderer hydration target when that body still has the same display
        # semantics.  JSON display_text, tool-marker cleanup, and legacy
        # control prompts are projection transforms; hydrating their raw row
        # would reintroduce internal protocol text into the chat UI.
        legacy_segments = legacy_projection[1] if legacy_projection else []
        projected_role = "assistant" if legacy_projection else role
        attachments = None
        workspace_files = []
        artifacts = None
        prompt_annotations = None
        page_context = None
        selected_skills = None
        local_path_references: tuple[str, ...] = ()
        user_display = getattr(projected_entry, "user_display_envelope", None)
        if content_mode == "bounded" and role == "user" and isinstance(user_display, dict):
            display_envelope = user_display.get("envelope")
            if isinstance(display_envelope, dict):
                envelope_content = json.dumps(display_envelope, ensure_ascii=False)
                # Removing inline attachment bytes is a complete display
                # transform, not truncated message text.
                raw_body_truncated = bool(user_display.get("text_truncated"))
                protocol_projection_suppressed = False
            else:
                content = (
                    "[Attachment preview unavailable: "
                    + str(user_display.get("error") or "invalid display metadata")
                    + "]"
                )
                envelope_content = ""
        else:
            envelope_content = content
        if envelope_content and envelope_content.startswith("{"):
            try:
                parsed = json.loads(envelope_content)
                if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
                    raw_display_hydration_safe = False
                    display_text = parsed.get("display_text")
                    content = display_text if isinstance(display_text, str) else parsed["text"]
                    from opensquilla.contracts.local_path_references import (
                        normalize_local_path_references,
                    )

                    try:
                        path_validation_text = (
                            user_display.get("local_path_validation_text")
                            if isinstance(user_display, dict) else None
                        )
                        local_path_references = normalize_local_path_references(
                            parsed.get("local_path_references"),
                            message=path_validation_text
                            if isinstance(path_validation_text, str)
                            else content,
                        )
                    except ValueError:
                        local_path_references = ()
                    attachments = _public_attachment_projection(parsed.get("attachments"))
                    from opensquilla.workspace_files import normalize_workspace_files

                    workspace_files = normalize_workspace_files(parsed.get("workspace_files"))
                    if workspace_files:
                        attachments = [*(attachments or []), *[
                            {"kind": "file", "name": ref["name"], "mime": ref["mime"],
                             "size": ref.get("size"), "workspaceFile": ref}
                            for ref in workspace_files
                        ]]
                    from opensquilla.contracts.selected_skills import normalize_selected_skills

                    try:
                        selected_skills = normalize_selected_skills(parsed.get("selected_skills"))
                    except ValueError:
                        selected_skills = None
                    raw_page_context = parsed.get("page_context")
                    if isinstance(raw_page_context, dict):
                        page_context = raw_page_context
                    from opensquilla.prompt_annotations import (
                        PromptAnnotationSnapshotError,
                        normalize_prompt_annotation_snapshots,
                    )

                    try:
                        normalized_annotations = normalize_prompt_annotation_snapshots(
                            parsed.get("prompt_annotations")
                        )
                    except PromptAnnotationSnapshotError:
                        normalized_annotations = ()
                    if normalized_annotations:
                        prompt_annotations = list(normalized_annotations)
                    parsed_artifacts = parsed.get("artifacts")
                    if isinstance(parsed_artifacts, list):
                        artifacts = [
                            artifact_payload(item)
                            for item in parsed_artifacts
                            if isinstance(item, dict)
                        ]
                        if artifacts:
                            content = strip_artifact_markers_from_text(content)
            except (ValueError, KeyError):
                pass
        if content and content.lstrip().startswith("[ContentBlock"):
            raw_display_hydration_safe = False
            texts = re.findall(
                r"ContentBlockText\(type='text', text='(.*?)'\)",
                content,
            )
            content = "\n".join(t.replace("\\n", "\n") for t in texts) if texts else ""
            if not content.strip():
                continue
        if content:
            cleaned = content
            if (
                role == "assistant"
                and has_flattened_used_tool_line(cleaned)
                and (silent_reply.segments or legacy_segments)
            ):
                cleaned = strip_flattened_used_tool_lines(cleaned)
            confirmed_tool_result = (
                role == "tool"
                or bool(getattr(projected_entry, "tool_call_id", None))
                or bool(legacy_projection)
            )
            if confirmed_tool_result:
                cleaned = strip_confirmed_flattened_tool_result(cleaned)
            if cleaned != content:
                raw_display_hydration_safe = False
                # The entry carried OpenSquilla's flattened tool serialization.
                # Drop it when nothing but internal tool transcript remains and
                # there is no structured tool timeline to render instead;
                # otherwise keep the narration that surrounded the markers.
                if not cleaned.strip() and not silent_reply.segments and not legacy_segments:
                    continue
                content = cleaned
        if projected_role == "user":
            if _is_legacy_generated_plan_implementation(
                content,
                getattr(projected_entry, "turn_context", None),
            ):
                raw_display_hydration_safe = False
                content = ""
        display_truncated = False
        if content_mode == "bounded":
            display_truncated = len(content.encode("utf-8")) > preview_bytes
            content, _, _ = _bounded_history_preview(
                content,
                preview_bytes,
            )
        msg: dict[str, Any] = {
            "id": getattr(projected_entry, "message_id", None),
            "message_id": getattr(projected_entry, "message_id", None),
            "role": projected_role,
            "text": content,
            "timestamp": getattr(projected_entry, "created_at", None),
            "provenance_kind": getattr(projected_entry, "provenance_kind", None),
            "provenance_source_session_key": getattr(
                projected_entry,
                "provenance_source_session_key",
                None,
            ),
            "provenance_source_tool": getattr(projected_entry, "provenance_source_tool", None),
        }
        if content_mode == "bounded" and content_metadata_pending:
            # Keep the preview usable while making the temporary state
            # explicit.  No contentRef is emitted until the exact byte length
            # is backfilled, so the client cannot issue an unsafe range read.
            msg["contentMetadataPending"] = True
        preview_complete = not (raw_body_truncated or display_truncated)
        if content_mode == "bounded":
            msg["contentPreviewComplete"] = preview_complete
            content_revision = getattr(projected_entry, "content_revision", None)
            if isinstance(content_revision, str) and content_revision:
                msg["contentRevision"] = content_revision
            # Projection can join an assistant tool call with a later result.
            # Byte-trimmed pages must resume past all selected source rows
            # owned by the item, not its older display identity.
            cursor_entries = legacy_cursor_bounds.get(context_index, (entry, entry))
            for cursor_name, cursor_entry in zip(
                ("historyCursorBefore", "historyCursorAfter"), cursor_entries, strict=True,
            ):
                row_id = getattr(cursor_entry, "id", None)
                created_at = getattr(cursor_entry, "created_at", None)
                if isinstance(row_id, int) and isinstance(created_at, int):
                    msg[cursor_name] = f"{int(created_at)}|{int(row_id)}"
        content_session_id = getattr(projected_entry, "session_id", None)
        content_message_id = getattr(projected_entry, "message_id", None)
        content_session_key = getattr(projected_entry, "session_key", None)
        content_ref_emitted = False
        if (
            isinstance(content_session_id, str)
            and content_session_id
            and isinstance(content_message_id, str)
            and content_message_id
            and isinstance(content_session_key, str)
            and content_session_key
        ):
            # Keep the history frame metadata-only. The legacy body remains in
            # SQLite and is fetched through content.read.v1 ranges.
            # Small rows remain byte-for-byte compatible with the old v4
            # payload.  A range reference is needed only when the bounded
            # projection actually truncates the body; this keeps the new
            # control-plane metadata from increasing every ordinary history
            # message while preserving a fetch path for large rows.
            semantic_projection_available = (
                content_mode == "bounded"
                and (bool(content.strip()) or protocol_projection_suppressed)
                and (
                    user_display.get("display_byte_length", content_byte_length)
                    if isinstance(user_display, dict) else content_byte_length
                ) <= MAX_DISPLAY_CONTENT_BYTES
            )
            semantic_view_required = (
                semantic_projection_available
                and (not raw_display_hydration_safe or role not in {"user"})
            )
            if content_mode == "legacy" or (
                not preview_complete
                and not content_metadata_pending
                and (
                    raw_display_hydration_safe
                    or semantic_projection_available
                )
            ):
                msg["contentRef"] = {
                    "version": 1,
                    "sessionKey": content_session_key,
                    "sessionId": content_session_id,
                    "messageId": content_message_id,
                }
                if content_mode == "bounded":
                    msg["contentRef"]["byteLength"] = content_byte_length
                    content_revision = getattr(projected_entry, "content_revision", None)
                    if isinstance(content_revision, str) and content_revision:
                        msg["contentRef"]["revision"] = content_revision
                    if raw_display_hydration_safe and role == "user":
                        msg["contentRef"]["view"] = "raw"
                    if semantic_view_required:
                        # Raw SQLite bytes are not safe for this row.  The
                        # client must call the semantic display endpoint,
                        # which reapplies this projector before returning
                        # text.  Keeping the view in the ref prevents a
                        # generic range reader from reintroducing protocol
                        # JSON or tool markers.
                        msg["contentRef"]["view"] = "display"
                content_source = getattr(projected_entry, "content_source", None)
                if content_source in {"active", "compacted"}:
                    msg["contentRef"]["source"] = content_source
                content_ref_emitted = True

        if (
            content_mode == "bounded"
            and not preview_complete
            and not content_ref_emitted
        ):
            # A transformed assistant/tool row must not expose its raw SQLite
            # body as a fallback range: it may contain protocol JSON, tool
            # arguments, or flattened tool results.  For rows above the
            # semantic display cap there is no bounded safe view to hydrate;
            # publish the reason explicitly so clients can distinguish this
            # state from an empty or missing message.
            if content_metadata_pending:
                msg["contentUnavailableReason"] = CONTENT_UNAVAILABLE_METADATA_PENDING
            elif content_byte_length > MAX_DISPLAY_CONTENT_BYTES:
                msg["contentUnavailableReason"] = CONTENT_UNAVAILABLE_DISPLAY_TOO_LARGE
            else:
                msg["contentUnavailableReason"] = CONTENT_UNAVAILABLE_REFERENCE_UNAVAILABLE
        transcript_id = getattr(projected_entry, "id", None)
        if transcript_id is not None:
            msg["transcript_id"] = transcript_id
        reasoning = getattr(projected_entry, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning.strip():
            msg["reasoning_content"] = reasoning
        if isinstance(turn_context, dict):
            if public_context := public_turn_context(turn_context):
                msg["turn_context"] = public_context
        if workspace_files:
            msg["workspaceFiles"] = workspace_files
        if attachments:
            msg["attachments"] = attachments
        if artifacts:
            msg["artifacts"] = artifacts
        if prompt_annotations:
            msg["promptAnnotations"] = prompt_annotations
        if page_context:
            msg["pageContext"] = page_context
        if selected_skills:
            msg["selectedSkills"] = list(selected_skills)
        if local_path_references:
            msg["localPathReferences"] = list(local_path_references)
        usage = getattr(projected_entry, "turn_usage", None)
        if isinstance(usage, dict):
            msg["usage"] = usage
            model = usage.get("model") or usage.get("routed_model")
            if model:
                msg["model"] = model
            input_tokens = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("outputTokens") or 0)
            msg["input"] = input_tokens
            msg["output"] = output_tokens
            msg["input_tokens"] = input_tokens
            msg["output_tokens"] = output_tokens
            if usage.get("cost_usd") is not None:
                msg["cost_usd"] = float(usage.get("cost_usd") or 0.0)
        tool_calls = [*(silent_reply.segments or []), *legacy_segments]
        if tool_calls:
            msg["tool_calls"] = _sanitize_display_protocol_payload(tool_calls)
        if (
            silent_reply.suppressed
            and not content
            and not artifacts
            and not attachments
            and not tool_calls
        ):
            continue
        if content_mode == "bounded":
            _bound_history_display_payloads(msg)
        messages.append(msg)
    return messages
