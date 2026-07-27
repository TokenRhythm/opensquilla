"""Provider-adapter final payload budget proof helpers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from copy import deepcopy
from typing import Any

_COMPACTED_STRING_MAX_CHARS = 1200
_COMPACTED_TAIL_STRING_MAX_CHARS = 640
_COMPACTED_ARGUMENT_PREVIEW_CHARS = 360
_COMPACTED_ARGUMENT_TAIL_CHARS = 120
_PROOF_BUDGET_HEADROOM_RATIO = 0.10
_PROOF_BUDGET_HEADROOM_MAX_CHARS = 16_384
_PROOF_BUDGET_HEADROOM_MIN_CHARS = 512
_TOOL_ARGUMENT_PROJECTION_PREFIX = "[tool_use_argument_projection]\n"
_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY = "_invalid_provider_context_arguments"
_COMPACTED_TOOL_ARGUMENT_MARKERS = frozenset(
    {
        "_opensquilla_compacted_tool_arguments",
        "_opensquilla_compacted_tool_input",
    }
)
# Opt-in compaction safety levers (all off by default). The tiny guard stops
# every marker producer from replacing a string that is shorter than the
# marker it would emit; recent-assistant protection keeps the model's
# just-emitted turn out of tier 2+ compaction so fresh work is never
# destroyed in the same request cycle that produced it. Recent-result,
# error-result, and projected-result protection exempt those tool results
# from tier-1 rewriting; stub previews attach head/tail excerpts of the
# original value to argument stubs; never-worse abandons any replacement that
# is not strictly smaller than the text it replaces.
_TINY_COMPACTION_GUARD_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_TINY_GUARD_CHARS"
_PROTECT_RECENT_ASSISTANT_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT"
_PROTECT_RECENT_RESULTS_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS"
_PROTECT_ERROR_RESULTS_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_ERROR_RESULTS"
_SKIP_PROJECTED_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_SKIP_PROJECTED"
_STUB_PREVIEW_CHARS_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_STUB_PREVIEW_CHARS"
_NEVER_WORSE_ENV = "OPENSQUILLA_PROVIDER_COMPACTION_NEVER_WORSE"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
# Prefixes stamped on tool-result content by the delivery-time boundary
# projection layer; must stay in sync with the agent-side exemption predicate.
_BOUNDARY_PROJECTED_RESULT_PREFIXES = (
    "[tool_result_projection]\n",
    "[aggregate_tool_result_compacted]\n",
    "[duplicate_tool_result_elided]\n",
)


def _tiny_compaction_guard_chars() -> int:
    raw = os.environ.get(_TINY_COMPACTION_GUARD_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _protect_recent_assistant_enabled() -> bool:
    raw = os.environ.get(_PROTECT_RECENT_ASSISTANT_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def _protected_recent_assistant_index(messages: Any) -> int | None:
    if not _protect_recent_assistant_enabled() or not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return index
    return None


def _protect_recent_results_count() -> int:
    raw = os.environ.get(_PROTECT_RECENT_RESULTS_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _protect_error_results_enabled() -> bool:
    raw = os.environ.get(_PROTECT_ERROR_RESULTS_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def _skip_projected_results_enabled() -> bool:
    raw = os.environ.get(_SKIP_PROJECTED_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def _stub_preview_chars() -> int:
    raw = os.environ.get(_STUB_PREVIEW_CHARS_ENV, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _never_worse_enabled() -> bool:
    raw = os.environ.get(_NEVER_WORSE_ENV, "").strip().lower()
    return raw in _TRUE_ENV_VALUES


def _keep_original_for_never_worse(original: Any, replacement: Any) -> bool:
    return _never_worse_enabled() and _payload_chars(replacement) >= _payload_chars(original)


def _tool_result_content_is_provider_projection(content: str) -> bool:
    return content.startswith(_BOUNDARY_PROJECTED_RESULT_PREFIXES)


class ProviderRequestBudgetExceededError(RuntimeError):
    def __init__(self, proof: dict[str, Any]) -> None:
        self.proof = proof
        super().__init__("provider_request_budget_exhausted")


ProviderRequestBudgetExceeded = ProviderRequestBudgetExceededError


def _payload_chars(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _effective_proof_budget(proof_budget: int) -> tuple[int, int]:
    if proof_budget <= 0:
        return proof_budget, 0
    ratio_headroom = int(proof_budget * _PROOF_BUDGET_HEADROOM_RATIO)
    headroom = max(_PROOF_BUDGET_HEADROOM_MIN_CHARS, ratio_headroom)
    headroom = min(_PROOF_BUDGET_HEADROOM_MAX_CHARS, headroom)
    if proof_budget <= headroom:
        headroom = max(0, proof_budget // 4)
    return max(1, proof_budget - headroom), headroom


def _is_data_url(value: str) -> bool:
    return value.startswith("data:") and ";base64," in value[:128]


def _media_placeholder(kind: str, value: str) -> str:
    return f"[provider_request_{kind}_omitted: {len(value)} chars]"


def _budget_projection(payload: Any) -> tuple[Any, int, int]:
    media_chars = 0
    media_blocks = 0

    def visit(value: Any) -> Any:
        nonlocal media_chars, media_blocks
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value

        if value.get("type") == "image_url":
            image_url = value.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url")
                if isinstance(url, str) and _is_data_url(url):
                    media_chars += len(url)
                    media_blocks += 1
                    replaced = dict(value)
                    replaced["image_url"] = {
                        **image_url,
                        "url": _media_placeholder("image_url", url),
                    }
                    return replaced

        source = value.get("source")
        if isinstance(source, dict) and source.get("type") == "base64":
            data = source.get("data")
            media_type = source.get("media_type")
            if (
                isinstance(data, str)
                and isinstance(media_type, str)
                and (media_type.startswith("image/") or media_type == "application/pdf")
            ):
                media_chars += len(data)
                media_blocks += 1
                replaced = dict(value)
                replaced["source"] = {
                    **source,
                    "data": _media_placeholder("base64_media", data),
                }
                return replaced

        return {key: visit(item) for key, item in value.items()}

    return visit(payload), media_chars, media_blocks


def _top_contributors(payload: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    contributors: list[dict[str, Any]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str):
            contributors.append({"path": path, "chars": len(value)})
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}")

    visit(payload, "$")
    contributors.sort(key=lambda item: int(item["chars"]), reverse=True)
    return contributors[:limit]


def _compact_string(value: str) -> str:
    if len(value) <= _COMPACTED_STRING_MAX_CHARS:
        return value
    head = value[:900]
    tail = value[-200:]
    omitted = len(value) - len(head) - len(tail)
    compacted = f"{head}\n\n[provider_request_compacted: omitted {omitted} chars]\n\n{tail}"
    if _keep_original_for_never_worse(value, compacted):
        return value
    return compacted


def _compact_tail_string(value: str, *, label: str) -> str:
    if len(value) <= _COMPACTED_TAIL_STRING_MAX_CHARS:
        return value
    if len(value) <= _tiny_compaction_guard_chars():
        return value
    head = value[:420]
    tail = value[-120:]
    omitted = len(value) - len(head) - len(tail)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    compacted = (
        f"{head}\n\n"
        f"[provider_request_{label}_compacted: omitted {omitted} chars; "
        f"original_chars={len(value)}; sha256={digest}]\n\n"
        f"{tail}"
    )
    if _keep_original_for_never_worse(value, compacted):
        return value
    return compacted


def _emergency_compact_string(value: str, *, label: str) -> str:
    if len(value) <= 320:
        return value
    if len(value) <= _tiny_compaction_guard_chars():
        return value
    head = value[:180]
    tail = value[-40:]
    omitted = len(value) - len(head) - len(tail)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    compacted = (
        f"{head}\n\n"
        f"[provider_request_{label}_emergency_compacted: omitted {omitted} chars; "
        f"original_chars={len(value)}; sha256={digest}]\n\n"
        f"{tail}"
    )
    if _keep_original_for_never_worse(value, compacted):
        return value
    return compacted


def _hard_compact_string(value: str, *, label: str) -> str:
    if len(value) <= 96:
        return value
    if len(value) <= _tiny_compaction_guard_chars():
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    compacted = f"[opensquilla_compacted:{label}:{len(value)}:{digest}]"
    if _keep_original_for_never_worse(value, compacted):
        return value
    return compacted


def _compact_argument_string(value: str, *, preview: bool = True) -> str:
    if preview:
        return _compact_tail_string(value, label="tool_input")
    if len(value) <= _tiny_compaction_guard_chars():
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    compacted = (
        "[provider_request_tool_input_compacted: "
        f"original_chars={len(value)}; sha256={digest}]"
    )
    preview_chars = _stub_preview_chars()
    if preview_chars and len(value) > preview_chars * 2:
        with_previews = f"{value[:preview_chars]}\n\n{compacted}\n\n{value[-preview_chars:]}"
        # Previews may never turn compaction into growth: attach them only
        # while the preview-carrying stub stays smaller than the original.
        if _payload_chars(with_previews) < _payload_chars(value):
            compacted = with_previews
    if _keep_original_for_never_worse(value, compacted):
        return value
    return compacted


def _compact_tool_arguments(value: str, *, preview: bool = True) -> str:
    if preview and len(value) <= _COMPACTED_TAIL_STRING_MAX_CHARS:
        return value
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            compacted: dict[str, Any] = {}
            changed = False
            force_string_compaction = not preview
            for key, item in parsed.items():
                if isinstance(item, str):
                    if key == "path" and not preview:
                        compacted[key] = item
                        continue
                    next_item = _compact_argument_string(
                        item,
                        preview=preview and not force_string_compaction,
                    )
                    compacted[key] = next_item
                    changed = changed or next_item != item
                else:
                    compacted[key] = item
            # Never-worse prefers the intact original over the note stub
            # when per-item rewriting produced no smaller replacement.
            if preview and not changed and _never_worse_enabled():
                return value
            if changed or not preview:
                compacted_json = json.dumps(
                    compacted, ensure_ascii=False, separators=(",", ":")
                )
                if _keep_original_for_never_worse(value, compacted_json):
                    return value
                return compacted_json
    if len(value) <= _tiny_compaction_guard_chars():
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    stub: dict[str, Any] = {
        "note": "historical tool arguments omitted for provider context budget",
        "original_chars": len(value),
        "sha256": digest,
    }
    stub_json = json.dumps(stub, ensure_ascii=False, separators=(",", ":"))
    preview_chars = _stub_preview_chars()
    if preview_chars and len(value) > preview_chars * 2:
        stub["preview_head"] = value[:preview_chars]
        stub["preview_tail"] = value[-preview_chars:]
        with_previews_json = json.dumps(stub, ensure_ascii=False, separators=(",", ":"))
        # Previews may never turn compaction into growth.
        if _payload_chars(with_previews_json) < _payload_chars(value):
            stub_json = with_previews_json
    if _keep_original_for_never_worse(value, stub_json):
        return value
    return stub_json


def _first_cache_control(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        cache_control = block.get("cache_control")
        if isinstance(cache_control, dict):
            return dict(cache_control)
    return None


def _text_content(text: str, *, cache_control: dict[str, Any] | None = None) -> Any:
    if cache_control:
        return [
            {
                "type": "text",
                "text": text,
                "cache_control": dict(cache_control),
            }
        ]
    return text


def _summary_value(value: str, *, max_chars: int = 160) -> str:
    value = value.replace("\n", "\\n")
    if len(value) <= max_chars:
        return value
    return f"{value[: max_chars - 3]}..."


def _tool_call_context_summary(tool_calls: list[dict[str, Any]]) -> str:
    lines = [
        "Historical tool call omitted for provider context budget.",
        f"omitted_tool_calls: {len(tool_calls)}",
    ]
    for tool_call in tool_calls[:8]:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        name_text = str(name) if name else "unknown"
        arguments = function.get("arguments")
        details: list[str] = []
        if isinstance(arguments, str):
            parsed = _parsed_tool_arguments(arguments)
            if isinstance(parsed, dict):
                path = parsed.get("path")
                if isinstance(path, str) and path:
                    details.append(f"path={_summary_value(path)}")
                workdir = parsed.get("workdir")
                if isinstance(workdir, str) and workdir:
                    details.append(f"workdir={_summary_value(workdir)}")
                command = parsed.get("command")
                if isinstance(command, str) and command:
                    details.append("command=omitted")
        suffix = f" ({', '.join(details)})" if details else ""
        lines.append(f"- {name_text}{suffix}")
    if len(tool_calls) > 8:
        lines.append(f"- ... {len(tool_calls) - 8} more omitted tool calls")
    return "\n".join(lines)


def _tool_result_summary_content(tool_name: str, content: Any) -> str:
    if isinstance(content, str):
        result_text = _compact_string(content)
    else:
        result_text = json.dumps(
            _hard_compact_content_for_provider(content, label="tool_result"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return f"Historical tool result for omitted {tool_name} call:\n{result_text}"


def _summarize_tool_call_arguments_for_provider(
    payload: dict[str, Any],
    *,
    aggregate_tool_arguments: bool,
) -> tuple[dict[str, Any], bool]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, False

    omitted_tool_names_by_id: dict[str, str] = {}
    changed = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        should_summarize = aggregate_tool_arguments
        if not should_summarize:
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str) and len(arguments) > _COMPACTED_TAIL_STRING_MAX_CHARS:
                    should_summarize = True
                    break
        if not should_summarize:
            continue

        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_id = tool_call.get("id")
            function = tool_call.get("function")
            tool_name = (
                str(function.get("name"))
                if isinstance(function, dict) and function.get("name")
                else "tool"
            )
            if isinstance(tool_id, str) and tool_id:
                omitted_tool_names_by_id[tool_id] = tool_name

        cache_control = _first_cache_control(message.get("content"))
        summary = _tool_call_context_summary(tool_calls)
        existing_content = message.get("content")
        if isinstance(existing_content, str) and existing_content.strip():
            summary = f"{existing_content.rstrip()}\n\n{summary}"
        message["content"] = _text_content(summary, cache_control=cache_control)
        message.pop("tool_calls", None)
        changed = True

    if not omitted_tool_names_by_id:
        return payload, changed

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str) or tool_call_id not in omitted_tool_names_by_id:
            continue
        tool_name = omitted_tool_names_by_id[tool_call_id]
        cache_control = _first_cache_control(message.get("content"))
        content = _tool_result_summary_content(tool_name, message.get("content"))
        message.clear()
        message["role"] = "user"
        message["content"] = _text_content(content, cache_control=cache_control)
        changed = True

    return payload, changed


def _invalid_provider_context_arguments(value: str | dict[str, Any]) -> dict[str, Any]:
    return {
        _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True,
        "reason": "provider_context_omitted",
    }


def _is_provider_context_marker_value(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return False


def _has_provider_context_argument_marker(value: dict[str, Any]) -> bool:
    return (
        _is_provider_context_marker_value(value.get(_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY))
        or any(
            _is_provider_context_marker_value(value.get(marker))
            for marker in _COMPACTED_TOOL_ARGUMENT_MARKERS
        )
    )


def _parsed_tool_arguments(arguments: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_arguments_are_invalid_provider_context(arguments: str) -> bool:
    parsed = _parsed_tool_arguments(arguments)
    return (
        isinstance(parsed, dict)
        and _is_provider_context_marker_value(
            parsed.get(_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY)
        )
    )


def _tool_arguments_have_compacted_marker(arguments: str) -> bool:
    parsed = _parsed_tool_arguments(arguments)
    return (
        isinstance(parsed, dict)
        and any(
            _is_provider_context_marker_value(parsed.get(marker))
            for marker in _COMPACTED_TOOL_ARGUMENT_MARKERS
        )
    )


def _compact_tool_input(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if _has_provider_context_argument_marker(value):
        return _invalid_provider_context_arguments(value)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(
        isinstance(item, str) and item.startswith(_TOOL_ARGUMENT_PROJECTION_PREFIX)
        for item in value.values()
    ):
        return _invalid_provider_context_arguments(value)
    if len(raw) <= _COMPACTED_TAIL_STRING_MAX_CHARS:
        return value
    if len(raw) <= _tiny_compaction_guard_chars():
        return value
    compacted = dict(value)
    changed = False
    for key, item in value.items():
        if not isinstance(item, str):
            continue
        next_item = _compact_tail_string(item, label="tool_input")
        if next_item != item:
            compacted[key] = next_item
            changed = True
    if changed:
        return compacted
    stub: dict[str, Any] = {
        "_opensquilla_compacted_tool_input": True,
        "original_chars": len(raw),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "head": raw[:_COMPACTED_ARGUMENT_PREVIEW_CHARS],
        "tail": raw[-_COMPACTED_ARGUMENT_TAIL_CHARS:],
    }
    preview_chars = _stub_preview_chars()
    if preview_chars > _COMPACTED_ARGUMENT_TAIL_CHARS:
        # The stub's head/tail fields already carry fixed-size previews;
        # separate preview keys would duplicate those bytes, so the lever
        # extends the fields in place — and only while the stub stays
        # smaller than the original.
        extended = dict(stub)
        extended["head"] = raw[: max(preview_chars, _COMPACTED_ARGUMENT_PREVIEW_CHARS)]
        extended["tail"] = raw[-preview_chars:]
        if _payload_chars(extended) < _payload_chars(value):
            stub = extended
    if _keep_original_for_never_worse(value, stub):
        return value
    return stub


def _tool_arguments_contain_projection(arguments: str) -> bool:
    parsed = _parsed_tool_arguments(arguments)
    if parsed is None:
        return arguments.startswith(_TOOL_ARGUMENT_PROJECTION_PREFIX)
    return any(
        isinstance(value, str) and value.startswith(_TOOL_ARGUMENT_PROJECTION_PREFIX)
        for value in parsed.values()
    )


def _provider_context_arguments_json(
    arguments: str,
    *,
    include_compacted_markers: bool = False,
) -> str | None:
    if (
        not _tool_arguments_are_invalid_provider_context(arguments)
        and not _tool_arguments_contain_projection(arguments)
        and not (
            include_compacted_markers
            and _tool_arguments_have_compacted_marker(arguments)
        )
    ):
        return None
    return json.dumps(
        _invalid_provider_context_arguments(arguments),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _scrub_leaked_tool_argument_projections_once(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    compacted = deepcopy(payload)
    changed = False
    for message in compacted.get("messages", []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                arguments = function.get("arguments")
                normalized = (
                    _provider_context_arguments_json(
                        arguments,
                        include_compacted_markers=True,
                    )
                    if isinstance(arguments, str)
                    else None
                )
                if normalized is not None:
                    function["arguments"] = normalized
                    changed = True
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            compacted_input = _compact_tool_input(tool_input)
            if compacted_input != tool_input:
                block["input"] = compacted_input
                changed = True
    return (compacted, changed) if changed else (payload, False)


def _compact_text_block(block: dict[str, Any], *, emergency: bool = False) -> None:
    text = block.get("text")
    if not isinstance(text, str):
        return
    if emergency:
        block["text"] = _emergency_compact_string(text, label="text_block")
    else:
        block["text"] = _compact_tail_string(text, label="text_block")


def _compact_user_content_for_provider(content: Any) -> Any:
    if isinstance(content, str):
        return _emergency_compact_string(content, label="user_context")
    if not isinstance(content, list):
        return content
    compacted: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            compacted.append(block)
            continue
        next_block = dict(block)
        if next_block.get("type") == "text" and isinstance(next_block.get("text"), str):
            next_block["text"] = _emergency_compact_string(
                next_block["text"],
                label="user_text",
            )
        compacted.append(next_block)
    return compacted


def _hard_compact_content_for_provider(content: Any, *, label: str) -> Any:
    if isinstance(content, str):
        return _hard_compact_string(content, label=label)
    if not isinstance(content, list):
        return content
    compacted: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            compacted.append(block)
            continue
        next_block = dict(block)
        if isinstance(next_block.get("text"), str):
            next_block["text"] = _hard_compact_string(
                next_block["text"],
                label=f"{label}_text",
            )
        if isinstance(next_block.get("content"), str):
            next_block["content"] = _hard_compact_string(
                next_block["content"],
                label=f"{label}_content",
            )
        if isinstance(next_block.get("thinking"), str):
            next_block["thinking"] = _hard_compact_string(
                next_block["thinking"],
                label=f"{label}_thinking",
            )
        compacted.append(next_block)
    return compacted


def _execution_status_is_failure(status: Any) -> bool:
    if not isinstance(status, dict):
        return False
    return str(status.get("status") or "").lower() in {
        "error",
        "timeout",
        "cancelled",
    }


def _tool_content_is_critical(content: Any) -> bool:
    if isinstance(content, str):
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if _execution_status_is_failure(parsed.get("execution_status")):
                    return True
                if parsed.get("is_error") is True:
                    return True
        lowered = content.lower()
        return "execution_status" in lowered and any(
            marker in lowered
            for marker in (
                '"status":"error"',
                '"status": "error"',
                '"status":"timeout"',
                '"status": "timeout"',
                '"status":"cancelled"',
                '"status": "cancelled"',
            )
        )
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("is_error") is True:
            return True
        if _tool_content_is_critical(block.get("content")):
            return True
    return False


def _tool_result_entry_is_error(entry: dict[str, Any]) -> bool:
    if entry.get("is_error") is True:
        return True
    return _tool_result_content_is_error(entry.get("content"))


def _tool_result_content_is_error(content: Any) -> bool:
    # Stricter than _tool_content_is_critical on purpose: the substring
    # fallback would exempt results that merely quote error fragments
    # (grep/read output) from tier 1, forcing escalation to harsher tiers.
    # Only structurally-parsed error envelopes qualify for the exemption.
    if isinstance(content, str):
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if _execution_status_is_failure(parsed.get("execution_status")):
                    return True
                if parsed.get("is_error") is True:
                    return True
        return False
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("is_error") is True:
            return True
        if _tool_result_content_is_error(block.get("content")):
            return True
    return False


def _critical_tool_content_for_provider(content: Any) -> Any:
    if isinstance(content, str):
        return _emergency_compact_string(content, label="tool_result")
    if not isinstance(content, list):
        return content
    compacted: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            compacted.append(block)
            continue
        next_block = dict(block)
        if isinstance(next_block.get("content"), str):
            next_block["content"] = _emergency_compact_string(
                next_block["content"],
                label="tool_result",
            )
        compacted.append(next_block)
    return compacted


def _compact_tool_arguments_for_final_cap(arguments: str) -> str:
    stub: dict[str, Any] = {_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True}
    stub_json = json.dumps(stub, ensure_ascii=False, separators=(",", ":"))
    preview_chars = _stub_preview_chars()
    if preview_chars and len(arguments) > preview_chars * 2:
        # Never preview argument text the projection scrubber would redact.
        sanitized = _provider_context_arguments_json(
            arguments,
            include_compacted_markers=True,
        )
        if sanitized is None:
            stub["preview_head"] = arguments[:preview_chars]
            stub["preview_tail"] = arguments[-preview_chars:]
            with_previews_json = json.dumps(stub, ensure_ascii=False, separators=(",", ":"))
            # Previews may never turn compaction into growth.
            if _payload_chars(with_previews_json) < _payload_chars(arguments):
                stub_json = with_previews_json
    if _keep_original_for_never_worse(arguments, stub_json):
        return arguments
    return stub_json


def _compact_tool_payload_once(payload: dict[str, Any]) -> dict[str, Any]:
    compacted = deepcopy(payload)
    entries: list[dict[str, Any]] = []
    for message in compacted.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str):
            entries.append(message)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                entries.append(block)
    protect_recent = _protect_recent_results_count()
    protect_errors = _protect_error_results_enabled()
    skip_projected = _skip_projected_results_enabled()
    first_protected = len(entries) - protect_recent
    for index, entry in enumerate(entries):
        if protect_recent and index >= first_protected:
            continue
        if protect_errors and _tool_result_entry_is_error(entry):
            continue
        content = entry.get("content")
        if isinstance(content, str):
            if skip_projected and _tool_result_content_is_provider_projection(content):
                continue
            entry["content"] = _compact_string(content)
        elif isinstance(content, list):
            # Projection is skipped per item so one projected item never
            # shields unprojected siblings from the rewrite.
            for item in content:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                if skip_projected and _tool_result_content_is_provider_projection(
                    item["text"]
                ):
                    continue
                item["text"] = _compact_string(item["text"])
    return compacted


def _compact_recent_tail_payload_once(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    compacted = deepcopy(payload)
    protected_index = _protected_recent_assistant_index(compacted.get("messages"))
    tool_argument_refs: list[tuple[dict[str, Any], str]] = []
    total_tool_argument_chars = 0
    for index, message in enumerate(compacted.get("messages", [])):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if index == protected_index:
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                tool_argument_refs.append((function, arguments))
                total_tool_argument_chars += len(arguments)
    aggregate_tool_arguments = (
        len(tool_argument_refs) > 1
        and total_tool_argument_chars > _COMPACTED_TAIL_STRING_MAX_CHARS * 4
    )
    tool_call_arguments_summarized = False
    for index, message in enumerate(compacted.get("messages", [])):
        if not isinstance(message, dict):
            continue
        if index == protected_index:
            continue
        if message.get("role") == "assistant":
            reasoning_content = message.get("reasoning_content")
            if isinstance(reasoning_content, str):
                message["reasoning_content"] = _compact_tail_string(
                    reasoning_content,
                    label="reasoning_content",
                )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        normalized = _provider_context_arguments_json(arguments)
                        function["arguments"] = (
                            normalized
                            if normalized is not None
                            else _compact_tool_arguments(
                                arguments,
                                preview=not aggregate_tool_arguments,
                            )
                        )
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                block["input"] = _compact_tool_input(block.get("input"))
            elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                block["thinking"] = _compact_tail_string(
                    block["thinking"],
                    label="thinking_block",
                )
            elif message.get("role") == "assistant" and block.get("type") == "text":
                _compact_text_block(block)
    return compacted, {
        "aggregate_tool_arguments_compacted": aggregate_tool_arguments,
        "tool_call_arguments_summarized": tool_call_arguments_summarized,
    }


def _emergency_compact_assistant_message(message: dict[str, Any]) -> None:
    """Apply tier-3 emergency compaction to a single assistant message."""
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = _emergency_compact_string(
            content,
            label="assistant_content",
        )
    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str):
        message["reasoning_content"] = _emergency_compact_string(
            reasoning_content,
            label="reasoning_content",
        )
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                normalized = _provider_context_arguments_json(arguments)
                function["arguments"] = (
                    normalized
                    if normalized is not None
                    else _emergency_compact_string(
                        arguments,
                        label="tool_arguments",
                    )
                )
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
            block["thinking"] = _emergency_compact_string(
                block["thinking"],
                label="thinking_block",
            )
        elif block.get("type") == "text":
            _compact_text_block(block, emergency=True)


def _emergency_compact_current_turn_payload_once(payload: dict[str, Any]) -> dict[str, Any]:
    compacted = deepcopy(payload)
    messages = compacted.get("messages", [])
    protected_index = _protected_recent_assistant_index(messages)
    last_user_index = None
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                last_user_index = index
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if index == protected_index:
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "user" and index != last_user_index:
            message["content"] = _compact_user_content_for_provider(content)
            content = message.get("content")
        if isinstance(content, str) and role in {"assistant", "tool"}:
            message["content"] = _emergency_compact_string(
                content,
                label=f"{role}_content",
            )
        if role == "assistant":
            reasoning_content = message.get("reasoning_content")
            if isinstance(reasoning_content, str):
                message["reasoning_content"] = _emergency_compact_string(
                    reasoning_content,
                    label="reasoning_content",
                )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        normalized = _provider_context_arguments_json(arguments)
                        function["arguments"] = (
                            normalized
                            if normalized is not None
                            else _emergency_compact_string(
                                arguments,
                                label="tool_arguments",
                            )
                        )
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                block_content = block.get("content")
                if isinstance(block_content, str):
                    block["content"] = _emergency_compact_string(
                        block_content,
                        label="tool_result",
                    )
                elif isinstance(block_content, list):
                    for item in block_content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            item["text"] = _emergency_compact_string(
                                item["text"],
                                label="tool_result_text",
                            )
            elif block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                block["thinking"] = _emergency_compact_string(
                    block["thinking"],
                    label="thinking_block",
                )
            elif role == "assistant" and block.get("type") == "text":
                _compact_text_block(block, emergency=True)
    return compacted


def _final_hard_cap_payload_once(payload: dict[str, Any]) -> dict[str, Any]:
    compacted = deepcopy(payload)
    messages = compacted.get("messages", [])
    protected_index = _protected_recent_assistant_index(messages)
    latest_user_index = None
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                latest_user_index = index
    for index, message in enumerate(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict):
            continue
        if index == protected_index:
            # The most recent assistant turn is protected from tier 2+ so the
            # model's just-emitted work is never destroyed. At the hard cap it
            # still receives bounded emergency compaction rather than the
            # sha-only placeholder used for older turns.
            _emergency_compact_assistant_message(message)
            continue
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            if index == latest_user_index:
                message["content"] = _compact_user_content_for_provider(content)
            else:
                message["content"] = _hard_compact_content_for_provider(
                    content,
                    label="user_context",
                )
            continue
        if role == "tool":
            if _tool_content_is_critical(content):
                message["content"] = _critical_tool_content_for_provider(content)
            else:
                message["content"] = _hard_compact_content_for_provider(
                    content,
                    label="tool_result",
                )
            continue
        if role != "assistant":
            continue
        message["content"] = _hard_compact_content_for_provider(
            content,
            label="assistant_content",
        )
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str):
            message["reasoning_content"] = _hard_compact_string(
                reasoning_content,
                label="reasoning_content",
            )
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                function["arguments"] = _compact_tool_arguments_for_final_cap(arguments)
    return compacted


def _component_chars(payload: dict[str, Any], key: str) -> int:
    if key not in payload:
        return 0
    return _payload_chars(payload[key])


def _message_role_chars(payload: dict[str, Any], role: str) -> int:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0
    role_messages = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == role
    ]
    return _payload_chars(role_messages) if role_messages else 0


def _top_level_chars(payload: dict[str, Any]) -> int:
    top_level_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"messages", "tools", "system"}
    }
    return _payload_chars(top_level_payload) if top_level_payload else 0


def _payload_component_chars(payload: dict[str, Any], proof_budget: int) -> dict[str, Any]:
    messages_chars = _component_chars(payload, "messages")
    tools_chars = _component_chars(payload, "tools")
    system_chars = _component_chars(payload, "system") + _message_role_chars(
        payload,
        "system",
    )
    tool_schema_too_large = False
    if proof_budget > 0 and tools_chars > 0:
        tool_schema_too_large = tools_chars >= max(16_000, proof_budget // 4)
    return {
        "messages_chars": messages_chars,
        "tools_chars": tools_chars,
        "system_chars": system_chars,
        "top_level_chars": _top_level_chars(payload),
        "tool_schema_too_large": tool_schema_too_large,
    }

def prove_provider_payload(
    payload: dict[str, Any],
    *,
    projection_adapter: str,
    proof_budget: int,
    status_projection_mode: str = "native_or_none",
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    budget_payload, media_chars, media_blocks = _budget_projection(payload)
    estimated_chars = _payload_chars(budget_payload)
    estimated_tokens = max(1, estimated_chars // 4)
    effective_budget, headroom_chars = _effective_proof_budget(proof_budget)
    fits = proof_budget <= 0 or estimated_chars <= effective_budget
    proof: dict[str, Any] = {
        "projection_adapter": projection_adapter,
        "execution_status_version": 1,
        "status_projection_mode": status_projection_mode,
        "estimated_chars": estimated_chars,
        "estimated_tokens": estimated_tokens,
        "proof_budget": proof_budget,
        "raw_proof_budget": proof_budget,
        "effective_proof_budget": effective_budget,
        "proof_headroom_chars": headroom_chars,
        "fits": fits,
        "compact_needed": not fits,
        "compaction_tier": 0,
        "compaction_tiny_guard_chars": _tiny_compaction_guard_chars(),
        "compaction_protect_recent_assistant": _protect_recent_assistant_enabled(),
        "recent_tail_too_large": False,
        "compaction_not_smaller": False,
        "provider_window_mismatch": False,
        "fallback_reason": fallback_reason,
        "top_contributors": _top_contributors(budget_payload),
        "retry_count": 0,
        **_payload_component_chars(budget_payload, effective_budget),
    }
    # Stamped only when enabled so default-off proofs stay byte-identical.
    protect_recent_results = _protect_recent_results_count()
    if protect_recent_results:
        proof["compaction_protect_recent_results"] = protect_recent_results
    if _protect_error_results_enabled():
        proof["compaction_protect_error_results"] = True
    if _skip_projected_results_enabled():
        proof["compaction_skip_projected"] = True
    stub_preview_chars = _stub_preview_chars()
    if stub_preview_chars:
        proof["compaction_stub_preview_chars"] = stub_preview_chars
    if _never_worse_enabled():
        proof["compaction_never_worse"] = True
    if media_blocks:
        proof["media_chars_excluded"] = media_chars
        proof["media_blocks_excluded"] = media_blocks
    if not fits:
        proof["fallback_reason"] = "provider_request_budget_exhausted"
        raise ProviderRequestBudgetExceededError(proof)
    return proof


def prove_or_compact_provider_payload(
    payload: dict[str, Any],
    *,
    projection_adapter: str,
    proof_budget: int,
    status_projection_mode: str = "native_or_none",
    fallback_reason: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if proof_budget <= 0:
        return payload, None
    payload, scrubbed_projection = _scrub_leaked_tool_argument_projections_once(payload)
    try:
        proof = prove_provider_payload(
            payload,
            projection_adapter=projection_adapter,
            proof_budget=proof_budget,
            status_projection_mode=status_projection_mode,
            fallback_reason=fallback_reason,
        )
    except ProviderRequestBudgetExceededError as first_error:
        first_chars = int(first_error.proof["estimated_chars"])
    else:
        if scrubbed_projection:
            proof["compact_needed"] = True
            proof["tool_argument_projection_scrubbed"] = True
        return payload, proof

    tool_compacted = _compact_tool_payload_once(payload)
    tool_compacted_chars = _payload_chars(tool_compacted)
    try:
        proof = prove_provider_payload(
            tool_compacted,
            projection_adapter=projection_adapter,
            proof_budget=proof_budget,
            status_projection_mode=status_projection_mode,
            fallback_reason=fallback_reason,
        )
    except ProviderRequestBudgetExceededError:
        pass
    else:
        proof["retry_count"] = 1
        proof["compact_needed"] = True
        proof["compaction_tier"] = 1
        proof["compaction_not_smaller"] = tool_compacted_chars >= first_chars
        proof["recent_tail_too_large"] = False
        return tool_compacted, proof

    tail_compacted, tail_metadata = _compact_recent_tail_payload_once(tool_compacted)
    tail_compacted_chars = _payload_chars(tail_compacted)
    try:
        proof = prove_provider_payload(
            tail_compacted,
            projection_adapter=projection_adapter,
            proof_budget=proof_budget,
            status_projection_mode=status_projection_mode,
            fallback_reason=fallback_reason,
        )
    except ProviderRequestBudgetExceededError as tail_error:
        emergency_compacted = _emergency_compact_current_turn_payload_once(tail_compacted)
        emergency_compacted_chars = _payload_chars(emergency_compacted)
        try:
            proof = prove_provider_payload(
                emergency_compacted,
                projection_adapter=projection_adapter,
                proof_budget=proof_budget,
                status_projection_mode=status_projection_mode,
                fallback_reason=fallback_reason,
            )
        except ProviderRequestBudgetExceededError as exc:
            hard_compacted = _final_hard_cap_payload_once(emergency_compacted)
            hard_compacted_chars = _payload_chars(hard_compacted)
            try:
                proof = prove_provider_payload(
                    hard_compacted,
                    projection_adapter=projection_adapter,
                    proof_budget=proof_budget,
                    status_projection_mode=status_projection_mode,
                    fallback_reason=fallback_reason,
                )
            except ProviderRequestBudgetExceededError:
                pass
            else:
                proof["retry_count"] = 4
                proof["compact_needed"] = True
                proof["compaction_tier"] = 4
                proof["tool_payload_compaction_not_smaller"] = (
                    tool_compacted_chars >= first_chars
                )
                proof["tail_compaction_not_smaller"] = (
                    tail_compacted_chars >= tool_compacted_chars
                )
                proof["emergency_current_turn_compacted"] = True
                proof["emergency_compaction_not_smaller"] = (
                    emergency_compacted_chars >= tail_compacted_chars
                )
                proof["final_hard_cap_compacted"] = True
                proof["final_hard_cap_not_smaller"] = (
                    hard_compacted_chars >= emergency_compacted_chars
                )
                proof["compaction_not_smaller"] = hard_compacted_chars >= first_chars
                proof["recent_tail_too_large"] = False
                proof.update(tail_metadata)
                return hard_compacted, proof
            exc.proof["retry_count"] = 2
            exc.proof["compact_needed"] = True
            exc.proof["compaction_tier"] = 4
            exc.proof["tool_payload_compaction_not_smaller"] = (
                tool_compacted_chars >= first_chars
            )
            exc.proof["tail_compaction_not_smaller"] = (
                tail_compacted_chars >= tool_compacted_chars
            )
            exc.proof["emergency_current_turn_compacted"] = True
            exc.proof["emergency_compaction_not_smaller"] = (
                emergency_compacted_chars >= tail_compacted_chars
            )
            exc.proof["final_hard_cap_compacted"] = True
            exc.proof["final_hard_cap_not_smaller"] = (
                hard_compacted_chars >= emergency_compacted_chars
            )
            exc.proof["compaction_not_smaller"] = emergency_compacted_chars >= first_chars
            exc.proof["recent_tail_too_large"] = bool(tail_error.proof.get("top_contributors"))
            raise
        proof["retry_count"] = 3
        proof["compact_needed"] = True
        proof["compaction_tier"] = 3
        proof["tool_payload_compaction_not_smaller"] = tool_compacted_chars >= first_chars
        proof["tail_compaction_not_smaller"] = tail_compacted_chars >= tool_compacted_chars
        proof["emergency_current_turn_compacted"] = True
        proof["emergency_compaction_not_smaller"] = (
            emergency_compacted_chars >= tail_compacted_chars
        )
        proof["compaction_not_smaller"] = emergency_compacted_chars >= first_chars
        proof["recent_tail_too_large"] = False
        proof.update(tail_metadata)
        return emergency_compacted, proof
    proof["retry_count"] = 2
    proof["compact_needed"] = True
    proof["compaction_tier"] = 2
    proof["tool_payload_compaction_not_smaller"] = tool_compacted_chars >= first_chars
    proof["tail_compaction_not_smaller"] = tail_compacted_chars >= tool_compacted_chars
    proof["compaction_not_smaller"] = tail_compacted_chars >= first_chars
    proof["recent_tail_too_large"] = False
    proof.update(tail_metadata)
    return tail_compacted, proof


def prove_provider_payload_from_env(
    payload: dict[str, Any],
    *,
    projection_adapter: str,
    status_projection_mode: str = "native_or_none",
    fallback_reason: str | None = None,
) -> dict[str, Any] | None:
    raw = os.environ.get("OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS")
    if not raw:
        return None
    try:
        proof_budget = int(raw)
    except ValueError:
        return None
    return prove_provider_payload(
        payload,
        projection_adapter=projection_adapter,
        proof_budget=proof_budget,
        status_projection_mode=status_projection_mode,
        fallback_reason=fallback_reason,
    )
