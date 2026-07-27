"""OpenAIProvider — streams via OpenAI Chat Completions API using httpx."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.execution_status import compact_provider_status, derive_is_error
from opensquilla.safety.secret_redaction import redact_secret_text
from opensquilla.secrets import clean_header_secret

from .app_attribution import is_provider_app_host, provider_app_headers
from .candidate_artifact import (
    CandidateArtifactBuilder,
    CandidateArtifactLimitError,
    strip_candidate_tool_identity,
)
from .compat_policy import (
    TEXT_TOOL_DIALECT_MINIMAX_XML,
    TEXT_TOOL_DIALECT_PLAIN_JSON,
    TEXT_TOOL_DIALECT_QWEN_TAG,
    OpenAICompatPolicy,
    compat_policy_for_kind,
)
from .context_capabilities import supports_openrouter_explicit_prompt_cache
from .error_redaction import (
    redact_upstream_error_code,
    redact_upstream_error_text,
    redacted_httpx_error,
)
from .failures import retry_after_from_headers
from .fx import TOKENRHYTHM_CNY_PER_USD, TOKENRHYTHM_CNY_PER_USD_NANOS
from .protocol import ProviderConnectionConfig, ProviderMetadata
from .reasoning_dialects import (
    ReasoningDisableArgs,
    ReasoningEnableArgs,
    apply_reasoning_disable,
    apply_reasoning_enable,
)
from .request_proof import (
    ProviderRequestBudgetExceededError,
    prove_provider_payload_from_env,
)
from .stream_assembly import (
    ReasoningAccumulator,
    ToolStreamAccumulator,
    ToolStreamProtocolError,
)
from .text_tool_normalizer import (
    LiteralTextSegment,
    TextToolSegment,
    TextToolStreamNormalizer,
    classify_text_tool_segments,
    warn_for_unauthorized_plain_candidate,
)
from .tokenrhythm_correlation import tokenrhythm_correlation_headers
from .trace_recorder import LLMTraceRecorder
from .types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ModelInfo,
    ProviderBillingReceipt,
    ProviderHeartbeatEvent,
    ProviderMessageCountProjection,
    ProviderMessageLimitProof,
    ReasoningDeltaEvent,
    StreamEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_OPENAI_API_BASE = "https://api.openai.com"
log = structlog.get_logger(__name__)
_DASHSCOPE_PARAMETER_RE = re.compile(
    r"<parameter(?:\s[^>]*)?>(?P<body>[\s\S]*?)</parameter>",
    re.IGNORECASE,
)
_MARKDOWN_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>[\s\S]*?)\s*```\s*$",
    re.IGNORECASE,
)


class _InertCandidateTextPassthrough:
    """Keep textual tool syntax literal for non-executable proposer output."""

    native_lifecycle_deferred = False
    held_chars = 0
    held_event_count = 0

    def push(self, text: str) -> list[str]:
        return [text] if text else []

    def observe_native_tool_start(self, _tool_name: str) -> list[TextToolSegment]:
        return []

    def abandon_native_lifecycle_defer(self) -> list[TextToolSegment]:
        return []

    def finish(
        self,
        *,
        successful_text_tool_terminal: bool,
        native_calls: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> list[TextToolSegment]:
        del successful_text_tool_terminal, native_calls
        return []


_MAX_CANDIDATE_WIRE_ID_CHARS = 4096


def _candidate_wire_digest(value: str) -> bytes | None:
    """Bound a response-local native identity before using it as an assembly key."""

    if len(value) > _MAX_CANDIDATE_WIRE_ID_CHARS:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).digest()


def _candidate_fragment_has_content(value: object | None) -> bool:
    """Return whether a malformed native field still carries advisory content."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping | list | tuple):
        return bool(value)
    return True


def _candidate_malformed_tool_wrapper(
    tool_call: Mapping[str, Any],
) -> dict[str, object] | None:
    """Retain non-identity wrapper content when function fields are absent."""

    sanitized = strip_candidate_tool_identity(tool_call)
    if not isinstance(sanitized, Mapping):
        return None

    residual = dict(sanitized)
    residual.pop("index", None)
    residual.pop("type", None)
    function = residual.pop("function", None)
    if isinstance(function, Mapping):
        function_residual = dict(function)
        function_residual.pop("name", None)
        function_residual.pop("arguments", None)
        if function_residual:
            residual["function"] = function_residual
    elif _candidate_fragment_has_content(function):
        residual["function"] = function
    if not residual:
        return None
    return {"malformed_tool_call": sanitized}


_OPENAI_TOOL_STATUS_OUTPUT_MAX_CHARS = 10000
_OPENAI_TOOL_STATUS_OUTPUT_HEAD_CHARS = 2000
_OPENAI_TOOL_STATUS_OUTPUT_TAIL_CHARS = 8000
_OPENAI_STREAM_USAGE_ONLY_KEYS = frozenset(
    {
        "id",
        "object",
        "created",
        "model",
        "system_fingerprint",
        "service_tier",
        "choices",
        "usage",
    }
)
_OPENAI_STREAM_NOOP_CHOICE_KEYS = frozenset(
    {"index", "delta", "finish_reason", "native_finish_reason"}
)
_OPENAI_STREAM_NOOP_DELTA_KEYS = frozenset({"content", "role"})
# Some OpenAI-compatible API roots carry a non-integer version segment before
# an adapter namespace.  Gemini's documented compatibility root is
# ``/v1beta/openai``: appending our canonical ``/v1`` again produces the
# nonexistent ``/v1beta/openai/v1/chat/completions`` endpoint.  Treat these
# roots exactly like the existing ``/v1`` ... ``/vN`` forms.
_VERSIONED_BASE_URL_RE = re.compile(
    r"/v\d+(?:(?:alpha|beta)\d*)?(?:/openai)?$",
)


def _versioned_api_url(base_url: str, path: str) -> str:
    """Join a canonical ``/v1/...`` path to an API root without duplication."""

    base = base_url.rstrip("/")
    if path.startswith("/v1/") and _VERSIONED_BASE_URL_RE.search(base):
        return f"{base}{path[3:]}"
    return f"{base}{path}"


_EPHEMERAL_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}
_DASHSCOPE_MAX_CACHE_MARKERS = 4
_DASHSCOPE_CACHE_MARKER_ROLES = {"system", "user", "assistant", "tool"}
_DASHSCOPE_WORKSPACE_MUTATION_TOOLS = frozenset(
    {
        "apply_patch",
        "edit_file",
        "write_file",
    }
)
_DASHSCOPE_FAILURE_ANCHOR_MARKERS = (
    "assertionerror",
    "traceback",
    "failed",
    "failure",
    "error",
    "exception",
    "expected",
    "actual",
    "exit code:",
    "exit_code=",
)


def _is_inert_post_terminal_stream_frame(
    *,
    chunk: Mapping[str, Any],
    raw_choices: list[Any],
    terminal_finish_reason: str,
    terminal_native_finish_reason_present: bool,
    terminal_native_finish_reason: Any,
    policy: OpenAICompatPolicy,
) -> bool:
    """Accept only a provider-declared, state-free terminal epilogue.

    OpenAI's usage trailer normally has ``choices: []``.  A small number of
    compatible gateways instead repeat choice zero with a semantically empty
    delta while attaching usage/cost metadata.  (Some spell that no-op as
    ``{"content": "", "role": "assistant"}``.)  Routing the duplicate through
    the ordinary choice parser would make a second terminal look like mutable
    response state.  Keep the exception narrow and fail closed on any content,
    tool, reasoning, index, role, or finish-reason change.
    """

    allowed_chunk_keys = _OPENAI_STREAM_USAGE_ONLY_KEYS.union(
        policy.post_terminal_metadata_keys
    )
    if set(chunk).difference(allowed_chunk_keys):
        return False

    usage_present = "usage" in chunk
    usage_payload = chunk.get("usage")
    has_usage = usage_present and isinstance(usage_payload, Mapping)
    has_null_usage_noop = (
        usage_present
        and usage_payload is None
        and policy.allow_post_terminal_null_usage_noop_choice
    )
    if usage_present and not has_usage and not has_null_usage_noop:
        return False

    if not raw_choices:
        return has_usage
    if not policy.allow_post_terminal_noop_choice or len(raw_choices) != 1:
        return False

    choice = raw_choices[0]
    if not isinstance(choice, Mapping):
        return False
    if set(choice).difference(_OPENAI_STREAM_NOOP_CHOICE_KEYS):
        return False

    choice_index = choice.get("index", 0)
    if (
        not isinstance(choice_index, int)
        or isinstance(choice_index, bool)
        or choice_index != 0
    ):
        return False

    if "delta" not in choice:
        return False
    delta = choice["delta"]
    if not isinstance(delta, Mapping):
        return False
    if set(delta).difference(_OPENAI_STREAM_NOOP_DELTA_KEYS):
        return False
    if delta.get("content") not in (None, ""):
        return False
    if delta.get("role") not in (None, "assistant"):
        return False

    repeated_finish = choice.get("finish_reason")
    if repeated_finish is not None and repeated_finish != terminal_finish_reason:
        return False

    repeated_native_present = "native_finish_reason" in choice
    if repeated_native_present != terminal_native_finish_reason_present:
        return False
    if (
        repeated_native_present
        and choice["native_finish_reason"] != terminal_native_finish_reason
    ):
        return False

    # A choice with neither usage nor a repeated finish is normally not a
    # meaningful terminal epilogue. TokenRhythm explicitly opts into its
    # observed ``usage: null`` spacer, which is still subject to every no-op
    # choice and top-level key validation above.
    return (
        has_usage
        or repeated_finish == terminal_finish_reason
        or has_null_usage_noop
    )


def _truncate_tool_status_output(output: str) -> str:
    """Bound an error tool-result while preserving the failing tail.

    Test/build failures put the actionable evidence (assertion message, ``FAILED``
    summary, traceback) at the END of the output. The previous head-only slice
    dropped exactly that tail, so keep a head slice for context plus a larger tail
    slice, joined by a visible marker that names how many chars were removed.
    """
    if len(output) <= _OPENAI_TOOL_STATUS_OUTPUT_MAX_CHARS:
        return output
    head = output[:_OPENAI_TOOL_STATUS_OUTPUT_HEAD_CHARS]
    tail = output[-_OPENAI_TOOL_STATUS_OUTPUT_TAIL_CHARS:]
    dropped = len(output) - len(head) - len(tail)
    return f"{head}\n...[{dropped} chars truncated]...\n{tail}"


def _openai_tool_result_content(block: Any) -> str:
    content = block.content if isinstance(block.content, str) else json.dumps(block.content)
    status = getattr(block, "execution_status", None)
    if status is None or not derive_is_error(status):
        return content
    return json.dumps(
        {
            "execution_status": compact_provider_status(status),
            "output": _truncate_tool_status_output(content),
        },
        ensure_ascii=False,
    )


def _provider_display_name(provider_kind: str) -> str:
    return {
        "openai": "OpenAI",
        "openrouter": "OpenRouter",
        "deepseek": "DeepSeek",
        "moonshot": "Moonshot",
        "dashscope": "DashScope",
        "gemini": "Gemini",
        "zhipu": "Zhipu",
        "qianfan": "Qianfan",
        "volcengine": "Volcengine",
        "tencent_tokenhub": "Tencent TokenHub",
        "tokenrhythm": "TokenRhythm",
    }.get(provider_kind, "Provider")


def _dashscope_endpoint_family(base_url: str) -> str:
    url = base_url.strip().lower()
    if "coding-intl.dashscope.aliyuncs.com" in url:
        return "coding_global"
    if "coding.dashscope.aliyuncs.com" in url:
        return "coding_cn"
    if "dashscope-intl.aliyuncs.com" in url:
        return "standard_global"
    if "dashscope.aliyuncs.com" in url:
        return "standard_cn"
    return "custom"


def _http_error_body_text(body: bytes | str) -> str:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    text = text.strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, str) and message.strip():
        # Non-OpenAI envelopes ({"code","message","traceId"} — TokenRhythm
        # and similar gateways) carry the machine-readable kind in a
        # top-level code; keep it with the (often localized) text so
        # failure-classification substrings have something stable to match.
        code = payload.get("code") if isinstance(payload, dict) else None
        if isinstance(code, str) and code.strip():
            return f"{code.strip()}: {message.strip()}"
        return message.strip()
    return text


def _format_chat_http_error(display_name: str, status_code: int, body: bytes | str) -> str:
    body_text = _http_error_body_text(body) or "empty response body"
    return f"{display_name} chat request failed (HTTP {status_code}): {body_text}"


def _base_url_hostname(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        return (parsed.hostname or "").lower()
    except ValueError:
        return ""


def _safe_validation_message(value: object) -> str:
    """Return a bounded, single-line, secret-redacted validation detail."""
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    if not compact:
        return ""
    return redact_secret_text(compact)[:500]


def _format_tokenrhythm_message_limit_error(
    display_name: str,
    status_code: int,
    body: bytes | str,
    validation_message: str,
) -> str:
    """Format only allowlisted fields from an exact TokenRhythm rejection."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    top_message = (
        _safe_validation_message(payload.get("message"))
        if isinstance(payload, dict)
        else ""
    )
    detail = f"BAD_REQUEST: {top_message}" if top_message else "BAD_REQUEST"
    if validation_message:
        detail = f"{detail}; {validation_message}"
    return f"{display_name} chat request failed (HTTP {status_code}): {detail}"


def _tokenrhythm_message_limit_evidence(
    *,
    provider_kind: str,
    base_url: str,
    model: str,
    status_code: int,
    body: bytes | str,
    wire_messages: object,
    logical_messages: int,
) -> tuple[ProviderMessageLimitProof, str] | None:
    """Parse TokenRhythm's exact structured ``messages[]`` size rejection.

    This deliberately refuses text matching.  The observed limit is safe to
    use for recovery only when the official host, HTTP status, envelope, field
    path, numeric constraint, and locally observed wire count all agree.
    """
    if (
        provider_kind != "tokenrhythm"
        or status_code != 400
        or not is_provider_app_host(base_url, "tokenrhythm.studio")
        or not isinstance(wire_messages, list)
    ):
        return None
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("code") != "BAD_REQUEST":
        return None
    rows = payload.get("data")
    if not isinstance(rows, list):
        return None

    limits: list[int] = []
    first_validation_message = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        maximum = row.get("maximum")
        inclusive = row.get("inclusive")
        if (
            row.get("origin") != "array"
            or row.get("code") != "too_big"
            or row.get("path") != ["messages"]
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum <= 0
            or not isinstance(inclusive, bool)
        ):
            continue
        limits.append(maximum if inclusive else maximum - 1)
        if not first_validation_message:
            first_validation_message = _safe_validation_message(row.get("message"))

    if not limits:
        return None
    limit = min(limits)
    actual_wire_messages = len(wire_messages)
    if actual_wire_messages <= limit:
        return None
    proof = ProviderMessageLimitProof(
        actual_wire_messages=actual_wire_messages,
        limit=limit,
        logical_messages=max(0, logical_messages),
        system_messages=sum(
            1
            for message in wire_messages
            if isinstance(message, dict) and message.get("role") == "system"
        ),
        tool_result_messages=sum(
            1
            for message in wire_messages
            if isinstance(message, dict) and message.get("role") == "tool"
        ),
        provider_kind=provider_kind,
        model=model,
        base_host=_base_url_hostname(base_url),
    )
    return proof, first_validation_message


def _strip_tool_schema_keywords(value: Any, unsupported: frozenset[str]) -> Any:
    if not unsupported:
        return value
    if isinstance(value, dict):
        return {
            key: _strip_tool_schema_keywords(item, unsupported)
            for key, item in value.items()
            if key not in unsupported
        }
    if isinstance(value, list):
        return [_strip_tool_schema_keywords(item, unsupported) for item in value]
    return value


_DASHSCOPE_THINKING_BUDGET_ENV = "OPENSQUILLA_DASHSCOPE_THINKING_BUDGET"
_DASHSCOPE_THINKING_BUDGET_MIN = 1024
_DASHSCOPE_THINKING_BUDGET_MAX = 38_912


def _thinking_budget_tokens_from_env() -> int | None:
    """Read an explicit per-call DashScope thinking budget from the local env.

    Returns a clamped positive token count, or ``None`` when the override is
    unset, blank, or unparseable. This is a provider-local escape hatch for the
    Qwen ``dashscope`` payload branch only; it deliberately does not touch
    ``AgentConfig`` or ``resolve_thinking``, so GLM/``zai`` and the shared
    context-budget governor are unaffected.
    """
    raw = os.environ.get(_DASHSCOPE_THINKING_BUDGET_ENV)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return max(_DASHSCOPE_THINKING_BUDGET_MIN, min(value, _DASHSCOPE_THINKING_BUDGET_MAX))


def _extract_think_tags(text: str) -> str:
    """Extract content from <think> tags. Returns empty string if none found."""
    matches = re.findall(r"<think>([\s\S]*?)</think>", text)
    return "\n".join(matches) if matches else ""


def _strip_think_tags(text: str) -> str:
    """Remove <think> tags from text, including unclosed trailing tags."""
    result = re.sub(r"<think>[\s\S]*?</think>", "", text)
    result = re.sub(r"<think>[\s\S]*$", "", result)
    return result.strip()


def _model_basename(model: str) -> str:
    return model.rsplit("/", 1)[-1].strip().lower()


def _on_official_host(policy: OpenAICompatPolicy, base_url: str) -> bool:
    return bool(policy.official_host) and policy.official_host in base_url.lower()


def _uses_max_completion_tokens(
    policy: OpenAICompatPolicy,
    base_url: str,
    model: str,
) -> bool:
    if not policy.max_completion_tokens_model_prefixes:
        return False
    if not _on_official_host(policy, base_url):
        return False
    return _model_basename(model).startswith(policy.max_completion_tokens_model_prefixes)


def _should_use_max_completion_tokens(
    policy: OpenAICompatPolicy,
    provider_kind: str,
    base_url: str,
    model: str,
    cfg: ChatConfig,
    caps: Any,
) -> bool:
    if _uses_max_completion_tokens(policy, base_url, model):
        return True
    return bool(
        provider_kind == "dashscope"
        and cfg.thinking
        and caps
        and caps.supports_reasoning
        and caps.reasoning_format == "dashscope"
    )


def _should_send_tool_choice(
    provider_kind: str,
    cfg: ChatConfig,
    caps: Any,
) -> bool:
    if cfg.tool_choice is None:
        return False
    if (
        provider_kind == "dashscope"
        and cfg.thinking
        and caps
        and caps.supports_reasoning
        and caps.reasoning_format == "dashscope"
    ):
        return False
    return True


_DASHSCOPE_PRESERVE_THINKING_MODEL_IDS = frozenset(
    {
        "qwen3.6-max-preview",
    }
)


def _dashscope_supports_preserve_thinking(model: str) -> bool:
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    return model_name in _DASHSCOPE_PRESERVE_THINKING_MODEL_IDS


def _should_send_temperature(
    policy: OpenAICompatPolicy,
    base_url: str,
    model: str,
    cfg: ChatConfig,
    caps: Any,
) -> bool:
    if cfg.temperature is None:
        return False
    model_name = _model_basename(model)
    if (
        policy.fixed_sampling_model_prefixes
        and model_name.startswith(policy.fixed_sampling_model_prefixes)
        and cfg.temperature != 1.0
    ):
        return False
    if (
        policy.omit_temperature_when_thinking_model_prefixes
        and _on_official_host(policy, base_url)
        and cfg.thinking
        and bool(caps and caps.supports_reasoning)
        and model_name.startswith(policy.omit_temperature_when_thinking_model_prefixes)
    ):
        return False
    return True


def _resolve_llm_proxy(proxy: str | None) -> str | None:
    if proxy is None:
        return os.environ.get("OPENSQUILLA_LLM_PROXY", "").strip() or None
    return proxy.strip() or None


def _tool_by_name(tools: list[ToolDefinition] | None) -> dict[str, ToolDefinition]:
    if not tools:
        return {}
    return {tool.name: tool for tool in tools}


def _tool_schema_accepts_arguments(
    tool: ToolDefinition | None,
    arguments: dict[str, Any],
) -> bool:
    return not _tool_schema_validation_errors(tool, arguments)


def _tool_schema_validation_errors(
    tool: ToolDefinition | None,
    arguments: dict[str, Any],
) -> list[str]:
    from opensquilla.tools.schema_validation import validate_tool_arguments

    if not isinstance(arguments, dict):
        return ["arguments expected object"]
    if tool is None:
        return []
    schema = tool.input_schema
    return validate_tool_arguments(
        arguments,
        properties=schema.properties or {},
        required=schema.required or [],
        additional_properties=schema.additional_properties,
    )


def _tool_schema_repair_validation_errors(
    tool: ToolDefinition | None,
    arguments: dict[str, Any],
) -> list[str]:
    errors = _tool_schema_validation_errors(tool, arguments)
    if errors or tool is None:
        return errors
    properties = set((tool.input_schema.properties or {}).keys())
    if properties and arguments and not (set(arguments) & properties):
        return ["arguments did not include any known tool properties"]
    return []


def _strip_markdown_json_fence(text: str) -> str:
    match = _MARKDOWN_JSON_FENCE_RE.match(text)
    if not match:
        return text
    return match.group("body").strip()


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    return _extract_json_object_at(text, start)


def _extract_json_object_at(text: str, start: int) -> str | None:
    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _json_object_start_positions(text: str, *, limit: int = 128) -> list[int]:
    positions = [index for index, char in enumerate(text) if char == "{"]
    if len(positions) <= limit:
        return positions
    # DashScope corruption often has a valid object after a long invalid prefix.
    # Keep both ends so recovery still sees late embedded tool arguments.
    head = max(1, limit // 4)
    tail = limit - head
    return [*positions[:head], *positions[-tail:]]


def _extract_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    for start in _json_object_start_positions(text):
        candidate = _extract_json_object_at(text, start)
        if candidate is not None and candidate not in objects:
            objects.append(candidate)
    return objects


def _dashscope_tool_argument_candidates_with_source(
    raw_text: str,
) -> list[tuple[str, str]]:
    text = raw_text.strip()
    if not text:
        return []
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(candidate: str | None, source: str) -> None:
        if candidate is None:
            return
        candidate = _strip_markdown_json_fence(candidate.strip())
        if candidate and candidate not in seen:
            candidates.append((candidate, source))
            seen.add(candidate)

    add(text, "direct")
    for match in _DASHSCOPE_PARAMETER_RE.finditer(text):
        add(match.group("body"), "parameter")
    for candidate, _source in list(candidates):
        add(_extract_first_json_object(candidate), "first_json_object")
    for candidate, _source in list(candidates):
        for embedded in _extract_json_objects(candidate):
            add(embedded, "embedded_json_object")
    return candidates


def _dashscope_tool_argument_candidates(raw_text: str) -> list[str]:
    return [
        candidate
        for candidate, _source in _dashscope_tool_argument_candidates_with_source(raw_text)
    ]


def _dashscope_repair_log_name(source: str) -> str:
    if source == "malformed_json":
        return "dashscope_malformed_json"
    if source == "embedded_json_object":
        return "dashscope_embedded_json_object"
    return "dashscope_wrapper_json"


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape literal control characters that appear inside JSON strings."""

    output: list[str] = []
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
                output.append(char)
                continue
            if char == "\\":
                escaped = True
                output.append(char)
                continue
            if char == '"':
                in_string = False
                output.append(char)
                continue
            if ord(char) < 0x20:
                output.append(f"\\u{ord(char):04x}")
                continue
            output.append(char)
            continue
        if char == '"':
            in_string = True
        output.append(char)
    return "".join(output)


def _reject_nonstandard_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_loads(value: str, *, strict: bool = True) -> Any:
    return json.loads(
        value,
        strict=strict,
        parse_constant=_reject_nonstandard_json_constant,
    )


def _strict_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return value


def _repair_malformed_json_object_candidate(candidate: str) -> dict[str, Any] | None:
    text = candidate.strip()
    if not text:
        return None

    try:
        parsed = _strict_json_loads(text, strict=False)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        pass
    else:
        return _strict_json_object(parsed)

    fixed = text
    open_curly = fixed.count("{") - fixed.count("}")
    open_bracket = fixed.count("[") - fixed.count("]")
    if open_bracket > 0:
        fixed += "]" * open_bracket
    if open_curly > 0:
        fixed += "}" * open_curly
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)

    for _ in range(50):
        try:
            parsed = _strict_json_loads(fixed)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            if fixed.endswith("}") and fixed.count("}") > fixed.count("{"):
                fixed = fixed[:-1]
                continue
            if fixed.endswith("]") and fixed.count("]") > fixed.count("["):
                fixed = fixed[:-1]
                continue
            break
        else:
            return _strict_json_object(parsed)

    escaped = _escape_invalid_chars_in_json_strings(fixed)
    if escaped != fixed:
        try:
            parsed = _strict_json_loads(escaped)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return None
        return _strict_json_object(parsed)
    return None


def _parse_json_object_candidate(candidate: str) -> dict[str, Any] | None:
    try:
        parsed = _strict_json_loads(candidate)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None
    parsed_object = _strict_json_object(parsed)
    if parsed_object is not None:
        return parsed_object
    if isinstance(parsed, str):
        try:
            nested = _strict_json_loads(parsed)
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            return None
        return _strict_json_object(nested)
    return None


def _unwrap_raw_json_arguments(arguments: dict[str, Any]) -> dict[str, Any] | None:
    raw = arguments.get("_raw")
    if set(arguments) != {"_raw"} or not isinstance(raw, str):
        return None
    for candidate in _dashscope_tool_argument_candidates(raw):
        parsed = _parse_json_object_candidate(candidate)
        if parsed is not None:
            return parsed
    return None


def _repair_dashscope_tool_arguments(
    raw_text: str,
    *,
    tool_name: str,
    tools_by_name: Mapping[str, ToolDefinition],
    schema_errors: list[str] | None = None,
    alias_conflicts: list[str] | None = None,
) -> tuple[dict[str, Any], str, list[dict[str, str]]] | None:
    from opensquilla.tools.argument_normalization import (
        canonicalize_tool_arguments,
        format_alias_conflicts,
    )

    tool = tools_by_name.get(tool_name)
    for candidate, source in _dashscope_tool_argument_candidates_with_source(raw_text):
        parsed = _parse_json_object_candidate(candidate)
        repair_source = source
        if parsed is None:
            parsed = _repair_malformed_json_object_candidate(candidate)
            repair_source = "malformed_json"
        if parsed is None:
            continue
        unwrapped = _unwrap_raw_json_arguments(parsed)
        if unwrapped is not None:
            parsed = unwrapped
        normalization = canonicalize_tool_arguments(tool_name, parsed)
        if normalization.conflicts:
            conflict_messages = format_alias_conflicts(normalization.conflicts)
            if alias_conflicts is not None:
                alias_conflicts.extend(conflict_messages)
            if schema_errors is not None:
                schema_errors.extend(conflict_messages)
            continue
        parsed = normalization.arguments
        if _strict_json_object(parsed) is None:
            if schema_errors is not None:
                schema_errors.append("arguments are not strict finite JSON")
            continue
        errors = _tool_schema_repair_validation_errors(tool, parsed)
        if not errors:
            return (
                parsed,
                _dashscope_repair_log_name(repair_source),
                normalization.aliases_applied,
            )
        if schema_errors is not None:
            schema_errors.extend(errors)
    return None


def _parse_openai_tool_arguments(
    *,
    provider_kind: str,
    model: str,
    tool_name: str,
    tool_use_id: str,
    raw_text: str,
    tools_by_name: Mapping[str, ToolDefinition],
) -> tuple[dict[str, Any], bool, bool]:
    """Parse provider tool arguments.

    Returns ``(arguments, json_valid, repaired)``. ``json_valid`` describes the
    executable argument object emitted downstream, not necessarily whether the
    provider's raw bytes were valid as-is.
    """

    if not raw_text:
        return {}, True, False
    try:
        parsed = _strict_json_loads(raw_text)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        if provider_kind == "dashscope":
            schema_errors: list[str] = []
            alias_conflicts: list[str] = []
            repaired = _repair_dashscope_tool_arguments(
                raw_text,
                tool_name=tool_name,
                tools_by_name=tools_by_name,
                schema_errors=schema_errors,
                alias_conflicts=alias_conflicts,
            )
            if repaired is not None:
                repaired_arguments, repair_name, aliases_applied = repaired
                if aliases_applied:
                    log.warning(
                        "provider.tool_arguments_aliases_applied",
                        provider=provider_kind,
                        model=model,
                        tool=tool_name,
                        tool_use_id=tool_use_id,
                        aliases=aliases_applied,
                    )
                log.warning(
                    "provider.tool_arguments_json_repaired",
                    provider=provider_kind,
                    model=model,
                    tool=tool_name,
                    tool_use_id=tool_use_id,
                    raw_chars=len(raw_text),
                    repair=repair_name,
                )
                return repaired_arguments, True, True
            if alias_conflicts:
                log.warning(
                    "provider.tool_arguments_alias_conflict",
                    provider=provider_kind,
                    model=model,
                    tool=tool_name,
                    tool_use_id=tool_use_id,
                    raw_chars=len(raw_text),
                    conflicts=alias_conflicts[:5],
                )
            if schema_errors:
                log.warning(
                    "provider.tool_arguments_json_invalid",
                    provider=provider_kind,
                    model=model,
                    tool=tool_name,
                    tool_use_id=tool_use_id,
                    raw_chars=len(raw_text),
                    reason="schema_validation_failed",
                    errors=schema_errors[:5],
                )
                return {}, False, False
        log.warning(
            "provider.tool_arguments_json_invalid",
            provider=provider_kind,
            model=model,
            tool=tool_name,
            tool_use_id=tool_use_id,
            raw_chars=len(raw_text),
            error=str(exc),
        )
        return {}, False, False

    if isinstance(parsed, dict) and _strict_json_object(parsed) is None:
        log.warning(
            "provider.tool_arguments_json_invalid",
            provider=provider_kind,
            model=model,
            tool=tool_name,
            tool_use_id=tool_use_id,
            raw_chars=len(raw_text),
            reason="non_finite_or_unserializable_value",
        )
        return {}, False, False

    if isinstance(parsed, dict):
        if provider_kind == "dashscope":
            unwrapped = _unwrap_raw_json_arguments(parsed)
            if unwrapped is not None and _tool_schema_accepts_arguments(
                tools_by_name.get(tool_name),
                unwrapped,
            ):
                log.warning(
                    "provider.tool_arguments_json_repaired",
                    provider=provider_kind,
                    model=model,
                    tool=tool_name,
                    tool_use_id=tool_use_id,
                    raw_chars=len(raw_text),
                    repair="dashscope_nested_raw_json",
                )
                return unwrapped, True, True
        return parsed, True, False

    log.warning(
        "provider.tool_arguments_json_invalid",
        provider=provider_kind,
        model=model,
        tool=tool_name,
        tool_use_id=tool_use_id,
        raw_chars=len(raw_text),
        error=f"tool arguments decoded to {type(parsed).__name__}, expected object",
    )
    return {}, False, False


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _first_present_value(*sources: tuple[Mapping[str, Any], str]) -> tuple[bool, int]:
    """Return whether a semantic field was present and its integer value.

    Truthiness chains would skip an explicit zero and fall through to a stale,
    lower-priority alias. Presence checks make zero a real replacement.
    """

    for src, key in sources:
        if isinstance(src, Mapping) and key in src:
            return True, _coerce_int(src[key])
    return False, 0


@dataclass
class _UsageSnapshotAccumulator:
    """Merge cumulative usage snapshots using latest-present semantics.

    OpenAI-compatible usage trailers are cumulative snapshots, not deltas.
    Some gateways split details and billing across multiple trailers. Each
    logical field is therefore replaced only when the new snapshot actually
    contains that field; an explicit zero is a real replacement.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    raw_billed_cost: Any = None
    billed_cost_present: bool = False

    def update(self, usage: Mapping[str, Any]) -> None:
        if "prompt_tokens" in usage:
            self.input_tokens = _coerce_int(usage["prompt_tokens"])
        if "completion_tokens" in usage:
            self.output_tokens = _coerce_int(usage["completion_tokens"])

        completion_details_raw = usage.get("completion_tokens_details")
        completion_details = (
            completion_details_raw
            if isinstance(completion_details_raw, Mapping)
            else {}
        )
        if "reasoning_tokens" in completion_details:
            self.reasoning_tokens = _coerce_int(completion_details["reasoning_tokens"])

        prompt_details_raw = usage.get("prompt_tokens_details")
        prompt_details = (
            prompt_details_raw if isinstance(prompt_details_raw, Mapping) else {}
        )
        top_cache_creation_raw = usage.get("cache_creation")
        top_cache_creation = (
            top_cache_creation_raw
            if isinstance(top_cache_creation_raw, Mapping)
            else {}
        )
        prompt_cache_creation_raw = prompt_details.get("cache_creation")
        prompt_cache_creation = (
            prompt_cache_creation_raw
            if isinstance(prompt_cache_creation_raw, Mapping)
            else {}
        )

        cached_present, cached_tokens = _first_present_value(
            (prompt_details, "cached_tokens"),
            (usage, "cached_tokens"),
            (usage, "prompt_cache_hit_tokens"),
        )
        if cached_present:
            self.cached_tokens = cached_tokens

        cache_write_present, cache_write_tokens = _first_present_value(
            (usage, "cache_creation_input_tokens"),
            (prompt_details, "cache_write_tokens"),
            (usage, "cache_write_tokens"),
            (prompt_details, "cache_creation_input_tokens"),
            (top_cache_creation, "ephemeral_5m_input_tokens"),
            (prompt_cache_creation, "ephemeral_5m_input_tokens"),
            (prompt_details, "cache_creation_tokens"),
        )
        if cache_write_present:
            self.cache_write_tokens = cache_write_tokens

        if "cost" in usage:
            self.raw_billed_cost = usage["cost"]
            self.billed_cost_present = True
        elif "total_cost" in usage:
            self.raw_billed_cost = usage["total_cost"]
            self.billed_cost_present = True

    def fields(self) -> tuple[int, int, int, int, int, float]:
        raw_billed_cost = (
            _coerce_float(self.raw_billed_cost) if self.billed_cost_present else 0.0
        )
        return (
            self.input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.cached_tokens,
            self.cache_write_tokens,
            raw_billed_cost,
        )


def _usage_fields(usage: Mapping[str, Any] | None) -> tuple[int, int, int, int, int, float]:
    if not usage:
        return 0, 0, 0, 0, 0, 0.0

    accumulator = _UsageSnapshotAccumulator()
    accumulator.update(usage)
    return accumulator.fields()


_MONEY_NANO_SCALE = 1_000_000_000
_MAX_MONEY_NANOS = (1 << 63) - 1
_TOKENRHYTHM_CNY_PER_USD = TOKENRHYTHM_CNY_PER_USD
_TOKENRHYTHM_FX_NANOS = TOKENRHYTHM_CNY_PER_USD_NANOS
_USD_FX_NANOS = _MONEY_NANO_SCALE


@dataclass
class _ProviderBillingAccumulator:
    """Accumulate provider billing metadata separately from token usage."""

    tokenrhythm_cost_cny: Any = None
    tokenrhythm_cost_present: bool = False
    tokenrhythm_pending: Any = None
    tokenrhythm_pending_present: bool = False

    def update(self, provider_kind: str, chunk: Mapping[str, Any]) -> None:
        if provider_kind != "tokenrhythm":
            return
        if "cost_cny" in chunk:
            self.tokenrhythm_cost_cny = chunk["cost_cny"]
            self.tokenrhythm_cost_present = True
        if "billing_pending" in chunk:
            self.tokenrhythm_pending = chunk["billing_pending"]
            self.tokenrhythm_pending_present = True


def _exact_provider_billing_payload(
    provider_kind: str,
    fallback: Mapping[str, Any],
    raw_json: str,
) -> Mapping[str, Any]:
    """Reparse native money as Decimal without exposing it to binary float.

    The ordinary response object intentionally keeps the adapter's historical
    JSON number types. TokenRhythm's billing projection is parsed a second time
    from the same wire text so sub-nano boundary rounding remains exact without
    leaking Decimal objects into content/tool/trace parsing.
    """

    if provider_kind != "tokenrhythm" or not raw_json:
        return fallback
    try:
        parsed = json.loads(raw_json, parse_float=Decimal)
    except (json.JSONDecodeError, InvalidOperation, RecursionError, TypeError):
        return fallback
    return parsed if isinstance(parsed, Mapping) else fallback


def _decimal_json_number(value: Any) -> Decimal | None:
    """Parse a finite, non-negative JSON number without float arithmetic."""

    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _decimal_compat_number(value: Any) -> Decimal | None:
    """Parse legacy compatible usage.cost values, including numeric strings."""

    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _money_to_nanos(value: Decimal) -> int | None:
    """Convert bounded money to ledger-safe nanos without raising."""

    try:
        rounded = (value * _MONEY_NANO_SCALE).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
        nanos = int(rounded)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    if nanos < 0 or nanos > _MAX_MONEY_NANOS:
        return None
    return nanos


def _billing_result(
    *,
    provider_kind: str,
    base_url: str,
    usage: _UsageSnapshotAccumulator,
    billing: _ProviderBillingAccumulator,
    model: str,
) -> tuple[float, str, ProviderBillingReceipt | None]:
    """Resolve a trusted provider-native receipt and canonical USD cost."""

    if compat_policy_for_kind(provider_kind).trust_billed_cost:
        amount = (
            _decimal_compat_number(usage.raw_billed_cost)
            if usage.billed_cost_present
            else None
        )
        # Keep OpenRouter's historical positive-only billed-cost contract.
        if amount is not None and amount > 0:
            amount_nanos = _money_to_nanos(amount)
            if amount_nanos is None:
                return 0.0, "none", None
            receipt = ProviderBillingReceipt(
                currency="USD",
                status="confirmed",
                amount_nanos=amount_nanos,
                usd_equivalent_nanos=amount_nanos,
                fx_native_per_usd_nanos=_USD_FX_NANOS,
            )
            return float(amount), "provider_billed", receipt
        return 0.0, "none", None

    if provider_kind != "tokenrhythm":
        return 0.0, "none", None

    if not is_provider_app_host(base_url, "tokenrhythm.studio"):
        if billing.tokenrhythm_cost_present or billing.tokenrhythm_pending_present:
            log.warning(
                "provider.billing_receipt_rejected",
                provider=provider_kind,
                model=model,
                reason="unofficial_host",
            )
        return 0.0, "none", None

    if not billing.tokenrhythm_pending_present:
        log.warning(
            "provider.billing_receipt_rejected",
            provider=provider_kind,
            model=model,
            reason="billing_status_missing",
        )
        return 0.0, "none", None
    pending = billing.tokenrhythm_pending
    if type(pending) is not bool:
        log.warning(
            "provider.billing_receipt_rejected",
            provider=provider_kind,
            model=model,
            reason="billing_status_invalid",
        )
        return 0.0, "none", None

    amount = (
        _decimal_json_number(billing.tokenrhythm_cost_cny)
        if billing.tokenrhythm_cost_present
        else None
    )
    amount_nanos = _money_to_nanos(amount) if amount is not None else None
    if pending:
        if billing.tokenrhythm_cost_present and amount_nanos is None:
            log.warning(
                "provider.billing_receipt_deferred",
                provider=provider_kind,
                model=model,
                reason="pending_amount_invalid",
            )
        return (
            0.0,
            "none",
            ProviderBillingReceipt(
                currency="CNY",
                status="pending",
                amount_nanos=amount_nanos,
                usd_equivalent_nanos=None,
                fx_native_per_usd_nanos=_TOKENRHYTHM_FX_NANOS,
            ),
        )

    if amount is None or amount_nanos is None:
        log.warning(
            "provider.billing_receipt_rejected",
            provider=provider_kind,
            model=model,
            reason=(
                "billing_amount_invalid"
                if amount is None and billing.tokenrhythm_cost_present
                else "billing_amount_out_of_range"
                if billing.tokenrhythm_cost_present
                else "billing_amount_missing"
            ),
        )
        return 0.0, "none", None

    usd_equivalent_nanos = int(
        (Decimal(amount_nanos) / _TOKENRHYTHM_CNY_PER_USD).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    if usd_equivalent_nanos < 0 or usd_equivalent_nanos > _MAX_MONEY_NANOS:
        log.warning(
            "provider.billing_receipt_rejected",
            provider=provider_kind,
            model=model,
            reason="billing_usd_equivalent_out_of_range",
        )
        return 0.0, "none", None
    receipt = ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=amount_nanos,
        usd_equivalent_nanos=usd_equivalent_nanos,
        fx_native_per_usd_nanos=_TOKENRHYTHM_FX_NANOS,
    )
    return (
        float(Decimal(usd_equivalent_nanos) / _MONEY_NANO_SCALE),
        "provider_billed",
        receipt,
    )


def _provider_billed_cost(provider_kind: str, raw_billed_cost: float) -> tuple[float, str]:
    """Return trusted provider-billed cost and its source marker."""
    amount = _decimal_compat_number(raw_billed_cost)
    if (
        compat_policy_for_kind(provider_kind).trust_billed_cost
        and amount is not None
        and amount > 0
    ):
        return float(amount), "provider_billed"
    return 0.0, "none"


def _resolve_tool_call_index(
    tc: Mapping[str, Any],
    tools_acc: ToolStreamAccumulator,
) -> tuple[int, bool]:
    """Resolve the accumulator slot for a streamed tool-call delta.

    Most upstreams send an explicit ``index``, but some (Gemini's
    OpenAI-compat endpoint, assorted local gateways) omit it: fall back to
    matching the provider-supplied id against known calls, then to opening a
    new slot — a missing index must never fail the stream.
    """
    tool_call_id = tc.get("id")
    if "index" in tc:
        raw_index = tc["index"]
        if isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            return raw_index, True
        if isinstance(tool_call_id, str) and tool_call_id:
            key = tools_acc.find_key_for_tool_call_id(tool_call_id)
            if key is not None:
                return cast(int, key), False
        return tools_acc.next_int_key(), False
    if isinstance(tool_call_id, str) and tool_call_id:
        key = tools_acc.find_key_for_tool_call_id(tool_call_id)
        if key is not None:
            return cast(int, key), True
        return tools_acc.next_int_key(), True
    single = tools_acc.single_key()
    if single is not None:
        return cast(int, single), True
    return tools_acc.next_int_key(), True


def _dashscope_tool_call_chunk_is_empty(tc: Mapping[str, Any]) -> bool:
    function = tc.get("function")
    if not isinstance(function, Mapping):
        function = {}
    return not (
        tc.get("id")
        or function.get("name")
        or function.get("arguments")
    )


def _stream_timeout(timeout: float) -> httpx.Timeout:
    connect = _coerce_float(os.environ.get("OPENSQUILLA_LLM_STREAM_CONNECT_TIMEOUT_SECONDS"))
    if connect <= 0:
        connect = 12.0
    connect = min(connect, max(timeout, 1.0))
    write = _coerce_float(os.environ.get("OPENSQUILLA_LLM_STREAM_WRITE_TIMEOUT_SECONDS"))
    if write <= 0:
        write = max(60.0, timeout)
    return httpx.Timeout(timeout, connect=connect, write=write, pool=10.0)


_SUCCESSFUL_TEXT_TOOL_FINISH_REASONS = frozenset({"stop", "tool_calls"})
_MAX_DEFERRED_NATIVE_EVENTS = 256
_MAX_DEFERRED_NATIVE_ARGUMENT_CHARS = 256_000


class _DeferredDeltaParts:
    """Rope-like storage for adjacent deltas; materialized exactly once."""

    __slots__ = ("kind", "parts", "tool_use_id")

    def __init__(self, kind: str, part: str, tool_use_id: str = "") -> None:
        self.kind = kind
        self.parts = [part]
        self.tool_use_id = tool_use_id

    def accepts(self, kind: str, tool_use_id: str) -> bool:
        return self.kind == kind and self.tool_use_id == tool_use_id

    def materialize(self) -> StreamEvent:
        value = "".join(self.parts)
        if self.kind == "text":
            return TextDeltaEvent(text=value)
        if self.kind == "reasoning":
            return ReasoningDeltaEvent(text=value)
        return ToolUseDeltaEvent(
            tool_use_id=self.tool_use_id,
            json_fragment=value,
        )


class _DeferredStreamEventBuffer:
    """Ordered event holdback with O(1) fragment append and exact accounting."""

    __slots__ = ("_chars", "_entries")

    def __init__(self) -> None:
        self._entries: list[StreamEvent | _DeferredDeltaParts] = []
        self._chars = 0

    @property
    def char_count(self) -> int:
        return self._chars

    @property
    def event_count(self) -> int:
        return len(self._entries)

    def __len__(self) -> int:
        return self.event_count

    def __iter__(self) -> Iterator[StreamEvent]:
        return iter(self.materialize())

    def append(self, event: StreamEvent) -> int:
        kind = ""
        part = ""
        tool_use_id = ""
        if isinstance(event, TextDeltaEvent):
            kind = "text"
            part = event.text
        elif isinstance(event, ReasoningDeltaEvent):
            kind = "reasoning"
            part = event.text
        elif isinstance(event, ToolUseDeltaEvent):
            kind = "tool"
            part = event.json_fragment
            tool_use_id = event.tool_use_id
        if kind:
            previous = self._entries[-1] if self._entries else None
            if isinstance(previous, _DeferredDeltaParts) and previous.accepts(
                kind,
                tool_use_id,
            ):
                previous.parts.append(part)
            else:
                self._entries.append(_DeferredDeltaParts(kind, part, tool_use_id))
            self._chars += len(part)
            return len(part)
        self._entries.append(event)
        return 0

    def patch_start_tool_name(self, tool_name: str) -> None:
        for entry in self._entries:
            if isinstance(entry, ToolUseStartEvent):
                entry.tool_name = tool_name

    def materialize(self) -> list[StreamEvent]:
        return [
            entry.materialize()
            if isinstance(entry, _DeferredDeltaParts)
            else entry
            for entry in self._entries
        ]

    def clear(self) -> None:
        self._entries.clear()
        self._chars = 0

    def drain(self) -> list[StreamEvent]:
        events = self.materialize()
        self.clear()
        return events


def _append_coalesced_stream_event(
    events: _DeferredStreamEventBuffer,
    event: StreamEvent,
) -> int:
    """Append one event to a fragment-list buffer without string copying."""

    return events.append(event)


def _successful_text_tool_terminal(
    *,
    saw_done_sentinel: bool,
    finish_reasons: list[str],
) -> bool:
    """Whether a response is complete enough to authorize text execution."""

    has_terminal_evidence = saw_done_sentinel or bool(finish_reasons)
    return has_terminal_evidence and all(
        reason in _SUCCESSFUL_TEXT_TOOL_FINISH_REASONS for reason in finish_reasons
    )


def _segment_text_tool_events(
    segments: list[TextToolSegment],
    *,
    provider_kind: str,
    model: str,
) -> list[TextDeltaEvent | ToolUseStartEvent | ToolUseEndEvent]:
    events: list[TextDeltaEvent | ToolUseStartEvent | ToolUseEndEvent] = []
    for segment in segments:
        if isinstance(segment, LiteralTextSegment):
            if segment.text:
                events.append(TextDeltaEvent(text=segment.text))
            continue
        for call in segment.calls:
            id_prefix = {
                TEXT_TOOL_DIALECT_QWEN_TAG: "qwen_text",
                TEXT_TOOL_DIALECT_MINIMAX_XML: "minimax_compat",
                TEXT_TOOL_DIALECT_PLAIN_JSON: "text_compat",
            }[call.dialect]
            tool_use_id = f"{id_prefix}_{uuid4().hex[:12]}"
            event_name = (
                "provider.qwen_text_tool_call_parsed"
                if call.dialect == TEXT_TOOL_DIALECT_QWEN_TAG
                else "provider.text_tool_call_parsed"
            )
            log.warning(
                event_name,
                provider=provider_kind,
                model=model,
                tool=call.tool_name,
                tool_use_id=tool_use_id,
                dialect=call.dialect,
                parse_format=call.parse_format,
            )
            events.append(
                ToolUseStartEvent(
                    tool_use_id=tool_use_id,
                    tool_name=call.tool_name,
                    synthetic_from_text=True,
                )
            )
            events.append(
                ToolUseEndEvent(
                    tool_use_id=tool_use_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    synthetic_from_text=True,
                )
            )
    return events


def _synthesize_text_tool_events(
    full_text: str,
    tools: list[ToolDefinition] | None,
    *,
    provider_kind: str,
    model: str,
) -> list[ToolUseStartEvent | ToolUseEndEvent]:
    """Compatibility helper backed by the scoped, atomic classifier."""

    policy = compat_policy_for_kind(provider_kind)
    segments = classify_text_tool_segments(
        full_text,
        tools,
        dialects=policy.text_tool_profile.dialects_for_model(model),
        provider_kind=provider_kind,
        model=model,
    )
    return [
        event
        for event in _segment_text_tool_events(
            segments,
            provider_kind=provider_kind,
            model=model,
        )
        if isinstance(event, (ToolUseStartEvent, ToolUseEndEvent))
    ]


def _build_openai_tool(
    tool: ToolDefinition,
    *,
    unsupported_keywords: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    schema = tool.input_schema.model_dump(exclude_none=True, by_alias=True)
    schema = _strip_tool_schema_keywords(schema, unsupported_keywords)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": schema,
        },
    }


def _openrouter_model_likely_supports_explicit_prompt_cache(model: str) -> bool:
    return supports_openrouter_explicit_prompt_cache(model)


def _dashscope_model_likely_supports_explicit_prompt_cache(model: str) -> bool:
    """Return True for DashScope model families with documented context cache support."""
    model_name = model.rsplit("/", 1)[-1].strip().lower()
    exact_models = {
        "qwen3-max",
        "qwen-plus",
        "qwen-flash",
        "deepseek-v3.2",
        "kimi-k2.6",
        "kimi-k2.5",
        "glm-5.1",
    }
    if model_name in exact_models:
        return True
    return model_name.startswith(
        (
            "qwen3.7-max",
            "qwen3.6-max-preview",
            "qwen3.7-plus",
            "qwen3.6-plus",
            "qwen3.5-plus",
            "qwen3.6-flash",
            "qwen3.5-flash",
            "qwen3-coder-plus",
            "qwen3-coder-flash",
            "qwen3-vl-plus",
            "qwen3-vl-flash",
        )
    )


def _supports_explicit_prompt_cache(
    provider_kind: str,
    model: str,
    cache_mode: str,
) -> bool:
    if cache_mode == "off":
        return False
    if provider_kind == "openrouter":
        return cache_mode == "on" or _openrouter_model_likely_supports_explicit_prompt_cache(model)
    if provider_kind == "dashscope":
        return cache_mode == "on" or _dashscope_model_likely_supports_explicit_prompt_cache(model)
    return False


def _openrouter_model_is_anthropic(model: str) -> bool:
    return model.strip().lower().startswith("anthropic/")


def _openrouter_model_uses_alibaba_message_cache(model: str) -> bool:
    model_l = model.strip().lower()
    model_name = model_l.rsplit("/", 1)[-1]
    return model_l.startswith("qwen/") or model_name.startswith(
        ("qwen3.6-flash", "qwen3.5-flash", "qwen3-coder")
    )


def _openrouter_anthropic_should_use_top_level_cache(
    *,
    provider_kind: str,
    model: str,
    cfg: ChatConfig,
) -> bool:
    return (
        provider_kind == "openrouter"
        and cfg.cache_mode in {"auto", "on"}
        and _openrouter_model_is_anthropic(model)
    )


def _build_cache_breakpoint_blocks(
    cache_breakpoints: list[dict[str, str]],
    *,
    max_cache_markers: int | None = None,
) -> list[dict[str, Any]]:
    content_blocks: list[dict[str, Any]] = []
    markers_used = 0
    for bp in cache_breakpoints:
        block: dict[str, Any] = {"type": "text", "text": bp["text"]}
        if bp.get("cache") and (max_cache_markers is None or markers_used < max_cache_markers):
            block["cache_control"] = dict(_EPHEMERAL_CACHE_CONTROL)
            markers_used += 1
        content_blocks.append(block)
    return content_blocks


def _count_explicit_cache_markers(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            total += sum(
                1 for block in content if isinstance(block, dict) and block.get("cache_control")
            )
    return total


def _cache_marker_positions(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if isinstance(block, dict) and block.get("cache_control"):
                positions.append(
                    {
                        "message_index": message_index,
                        "role": message.get("role", ""),
                        "block_index": block_index,
                        "block_type": block.get("type", ""),
                        "text_chars": len(block.get("text", ""))
                        if isinstance(block.get("text"), str)
                        else 0,
                    }
                )
    return positions


def _payload_cache_shape(
    payload: Mapping[str, Any],
    *,
    tools: list[ToolDefinition] | None,
) -> dict[str, Any]:
    messages = payload.get("messages") if isinstance(payload, Mapping) else None
    openai_messages = messages if isinstance(messages, list) else []
    system_payload = (
        openai_messages[0]
        if openai_messages and openai_messages[0].get("role") == "system"
        else None
    )
    non_system_prefix_item_hashes = _openrouter_non_system_prefix_item_hashes(openai_messages)
    return {
        "top_level_cache_control": bool(payload.get("cache_control")),
        "explicit_cache_markers": _cache_marker_positions(openai_messages),
        "explicit_cache_marker_count": _count_explicit_cache_markers(openai_messages),
        "system_hash": _stable_json_hash(system_payload) if system_payload else "",
        "tools_hash": _stable_json_hash(payload.get("tools", [])) if tools else "",
        "messages_prefix_hash": _stable_json_hash(openai_messages[:-1]),
        "first_non_system_hash": (
            non_system_prefix_item_hashes[0] if non_system_prefix_item_hashes else ""
        ),
        "non_system_prefix_item_hashes": non_system_prefix_item_hashes,
        "message_count": len(openai_messages),
    }


def _log_provider_cache_usage(
    *,
    provider_kind: str,
    model: str,
    actual_model: str,
    input_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    cache_shape: Mapping[str, Any],
) -> None:
    if provider_kind != "dashscope":
        return
    log.info(
        f"{provider_kind}.prompt_cache_usage",
        model=model,
        actual_model=actual_model,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        cached_input_ratio=round(cached_tokens / input_tokens, 6) if input_tokens else 0.0,
        system_hash=cache_shape.get("system_hash", ""),
        tools_hash=cache_shape.get("tools_hash", ""),
        messages_prefix_hash=cache_shape.get("messages_prefix_hash", ""),
        explicit_cache_marker_count=cache_shape.get("explicit_cache_marker_count", 0),
        explicit_cache_markers=cache_shape.get("explicit_cache_markers", []),
        message_count=cache_shape.get("message_count", 0),
    )


def _attach_cache_control_to_latest_text_messages(
    messages: list[dict[str, Any]],
    *,
    max_cache_markers: int,
) -> None:
    def _attach_to_message(message: dict[str, Any]) -> bool:
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                return False
            message["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": dict(_EPHEMERAL_CACHE_CONTROL),
                }
            ]
            return True
        if not isinstance(content, list):
            return False
        for block in reversed(content):
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
                and block["text"].strip()
                and not block.get("cache_control")
            ):
                block["cache_control"] = dict(_EPHEMERAL_CACHE_CONTROL)
                return True
        return False

    markers_remaining = max_cache_markers - _count_explicit_cache_markers(messages)
    if markers_remaining <= 0:
        return

    # Keep the initial user task pinned. In long agentic coding loops, spending all remaining
    # markers on the moving tail can collapse DashScope hits to the system block.
    pinned_initial_user_index: int | None = None
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        pinned_initial_user_index = index
        if _attach_to_message(message):
            markers_remaining -= 1
        break
    if markers_remaining <= 0:
        return

    for index, message in reversed(list(enumerate(messages))):
        if pinned_initial_user_index is not None and index == pinned_initial_user_index:
            continue
        if message.get("role") not in _DASHSCOPE_CACHE_MARKER_ROLES:
            continue
        if _attach_to_message(message):
            markers_remaining -= 1
            if markers_remaining <= 0:
                return


def _disambiguate_repeated_tool_call_arguments_for_dashscope(
    messages: list[dict[str, Any]],
) -> None:
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, sort_keys=True)

    def _preview_tool_result(tool_call_id: str) -> str:
        result = result_messages_by_id.get(tool_call_id)
        if result is None:
            return "missing"
        content = _content_text(result.get("content", ""))
        preview = content.replace("\n", "\\n")
        if len(preview) > 160:
            preview = preview[:157] + "..."
        return preview

    def _provider_result_details(tool_call_id: str) -> dict[str, Any]:
        result = result_messages_by_id.get(tool_call_id)
        if result is None:
            return {
                "result_is_error": None,
                "exit_code": None,
                "execution_reason": "missing_tool_result",
                "result_sha256": None,
                "result_chars": 0,
                "failure_anchors": [],
            }

        content = _content_text(result.get("content", ""))
        result_text = content
        execution_status: dict[str, Any] | None = None
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            status = parsed.get("execution_status")
            if isinstance(status, dict):
                execution_status = status
            output = parsed.get("output")
            if isinstance(output, str):
                result_text = output

        lowered = result_text.lower()
        failure_anchors = [
            line.strip()
            for line in result_text.splitlines()
            if line.strip()
            and any(marker in line.lower() for marker in _DASHSCOPE_FAILURE_ANCHOR_MARKERS)
        ][:3]

        status_value = (
            str(execution_status.get("status") or "") if execution_status is not None else ""
        )
        inferred_failure = bool(failure_anchors) or bool(
            re.search(r"\bexit(?: code|_code)[:=]\s*[1-9][0-9]*\b", lowered)
        )
        result_is_error = (
            status_value in {"error", "timeout", "cancelled"}
            if execution_status is not None
            else inferred_failure
        )
        execution_reason = (
            str(execution_status.get("reason") or "") if execution_status is not None else ""
        )
        if not execution_reason:
            execution_reason = "failure_anchor" if inferred_failure else "unknown"

        return {
            "result_is_error": result_is_error,
            "exit_code": (
                execution_status.get("exit_code") if execution_status is not None else None
            ),
            "execution_reason": execution_reason,
            "result_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()[:16],
            "result_chars": len(content),
            "failure_anchors": failure_anchors,
        }

    def _summary_for_omitted_duplicate(
        *,
        name: str,
        arguments: dict[str, Any],
        repeat_index: int,
        tool_call_id: str,
        workspace_epoch: int,
        latest_workspace_epoch: int,
    ) -> str:
        result_details = _provider_result_details(tool_call_id)
        anchors = json.dumps(
            result_details["failure_anchors"],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        exit_code = result_details["exit_code"]
        exit_code_text = "null" if exit_code is None else str(exit_code)
        result_sha256 = result_details["result_sha256"] or "missing"
        return (
            "[Earlier duplicate tool interaction omitted for DashScope replay "
            f"compatibility: tool={name}, arguments_sha256={_stable_json_hash(arguments)}, "
            f"repeat_index={repeat_index}, workspace_epoch={workspace_epoch}, "
            f"latest_workspace_epoch={latest_workspace_epoch}, "
            f"result_is_error={str(result_details['result_is_error']).lower()}, "
            f"exit_code={exit_code_text}, "
            f"execution_reason={result_details['execution_reason']}, "
            f"result_sha256={result_sha256}, result_chars={result_details['result_chars']}, "
            f"failure_anchors={anchors}, result_preview="
            f"{json.dumps(_preview_tool_result(tool_call_id), ensure_ascii=False)}]"
        )

    result_messages_by_id = {
        message["tool_call_id"]: message
        for message in messages
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str)
    }
    tool_name_by_id: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            tool_call_id = tool_call.get("id")
            function = tool_call.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(tool_call_id, str) and isinstance(name, str):
                tool_name_by_id[tool_call_id] = name

    occurrences: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], int] = {}
    workspace_epoch = 0
    for message_index, message in enumerate(messages):
        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if (
                isinstance(tool_call_id, str)
                and tool_name_by_id.get(tool_call_id) in _DASHSCOPE_WORKSPACE_MUTATION_TOOLS
                and _provider_result_details(tool_call_id)["result_is_error"] is not True
            ):
                workspace_epoch += 1
            continue
        if message.get("role") != "assistant":
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
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                continue
            try:
                parsed_arguments = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed_arguments, dict):
                continue
            canonical_arguments = json.dumps(
                parsed_arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            key = (name, canonical_arguments)
            repeat_index = seen.get(key, 0)
            seen[key] = repeat_index + 1
            occurrences.append(
                {
                    "key": key,
                    "message_index": message_index,
                    "tool_call": tool_call,
                    "tool_call_id": tool_call.get("id"),
                    "tool": name,
                    "arguments": parsed_arguments,
                    "repeat_index": repeat_index,
                    "workspace_epoch": workspace_epoch,
                }
            )

    if not occurrences:
        return

    last_occurrence_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for occurrence in occurrences:
        last_occurrence_by_key[occurrence["key"]] = occurrence

    omitted_summaries_by_id: dict[str, str] = {}
    for occurrence in occurrences:
        if last_occurrence_by_key.get(occurrence["key"]) is occurrence:
            continue
        tool_call_id = occurrence.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        omitted_summaries_by_id[tool_call_id] = _summary_for_omitted_duplicate(
            name=str(occurrence["tool"]),
            arguments=cast(dict[str, Any], occurrence["arguments"]),
            repeat_index=int(occurrence["repeat_index"]),
            tool_call_id=tool_call_id,
            workspace_epoch=int(occurrence["workspace_epoch"]),
            latest_workspace_epoch=int(
                last_occurrence_by_key[occurrence["key"]].get("workspace_epoch", 0)
            ),
        )

    if not omitted_summaries_by_id:
        return

    rewritten: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") in omitted_summaries_by_id:
            continue
        tool_calls = message.get("tool_calls")
        if message.get("role") != "assistant" or not isinstance(tool_calls, list):
            rewritten.append(message)
            continue

        kept_calls: list[dict[str, Any]] = []
        summaries: list[str] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                kept_calls.append(tool_call)
                continue
            tool_call_id = tool_call.get("id")
            if isinstance(tool_call_id, str) and tool_call_id in omitted_summaries_by_id:
                summaries.append(omitted_summaries_by_id[tool_call_id])
            else:
                kept_calls.append(tool_call)
        if not summaries:
            rewritten.append(message)
            continue

        summary_text = "\n".join(summaries)
        if kept_calls:
            next_message = dict(message)
            next_message["tool_calls"] = kept_calls
            existing_content = next_message.get("content")
            next_message["content"] = (
                f"{existing_content}\n{summary_text}"
                if isinstance(existing_content, str) and existing_content
                else summary_text
            )
            rewritten.append(next_message)
        else:
            rewritten.append({"role": "assistant", "content": summary_text})
    messages[:] = rewritten


def _stable_json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _openrouter_non_system_prefix_item_hashes(
    messages: list[dict[str, Any]], *, max_items: int = 3
) -> list[str]:
    hashes: list[str] = []
    for message in messages:
        if message.get("role") == "system":
            continue
        hashes.append(_stable_json_hash(message))
        if len(hashes) >= max_items:
            break
    return hashes


def _attach_reasoning_content(
    msg: Message,
    payload: dict[str, Any],
    *,
    include_reasoning_content: bool = True,
    require_assistant_reasoning_content: bool = False,
) -> dict[str, Any]:
    if include_reasoning_content and msg.role == "assistant" and msg.reasoning_content:
        payload["reasoning_content"] = msg.reasoning_content
    elif require_assistant_reasoning_content and msg.role == "assistant":
        # Models that require the key on every assistant message get an
        # empty string whenever the actual reasoning is absent or withheld
        # (e.g. reasoning-echo truncation of older messages).
        payload["reasoning_content"] = ""
    return payload


_REASONING_ECHO_TURNS_ENV = "OPENSQUILLA_REASONING_ECHO_TURNS"


def _resolve_reasoning_echo_turns() -> int | None:
    """Resolve the opt-in reasoning-echo truncation lever.

    ``OPENSQUILLA_REASONING_ECHO_TURNS`` limits how many of the most recent
    assistant messages replay their ``reasoning_content`` when the compat
    policy replays reasoning at all: a non-negative integer keeps only the
    last N assistant messages' reasoning (0 drops every echo), and unset or
    "all" keeps the replay-all behavior byte-identical. Unrecognized values
    raise instead of being silently ignored so a run manifest cannot record
    an override the run did not actually apply.
    """
    env_value = os.environ.get(_REASONING_ECHO_TURNS_ENV, "").strip().lower()
    if not env_value or env_value == "all":
        return None
    if env_value.isdigit():
        return int(env_value)
    raise ValueError(
        f'{_REASONING_ECHO_TURNS_ENV} must be a non-negative integer or "all"'
    )


def _reasoning_echo_allowed_indexes(
    messages: list[Message],
    echo_turns: int | None,
) -> set[int] | None:
    """Indexes of assistant messages allowed to replay reasoning_content.

    Returns ``None`` when the lever is unset (no per-message gating).
    """
    if echo_turns is None:
        return None
    assistant_indexes = [
        index for index, message in enumerate(messages) if message.role == "assistant"
    ]
    if echo_turns <= 0:
        return set()
    return set(assistant_indexes[-echo_turns:])


def _requires_assistant_reasoning_content(policy: OpenAICompatPolicy, model: str) -> bool:
    return model.strip().lower() in policy.require_reasoning_content_model_ids


def _should_replay_reasoning_content(
    *,
    policy: OpenAICompatPolicy,
    model: str,
    caps: ModelCapabilities | None,
    thinking: bool = False,
) -> bool:
    if _requires_assistant_reasoning_content(policy, model):
        return True
    if not caps or not caps.supports_reasoning:
        return False
    if caps.reasoning_format == "dashscope":
        if not thinking:
            return False
        return _dashscope_supports_preserve_thinking(model)
    return bool(policy.replay_reasoning_format) and (
        caps.reasoning_format == policy.replay_reasoning_format
    )


def _build_openai_messages(
    msg: Message,
    *,
    include_reasoning_content: bool = True,
    require_assistant_reasoning_content: bool = False,
    replay_provider_state: bool = True,
) -> list[dict[str, Any]]:
    """Convert a opensquilla Message into one or more OpenAI-format message dicts.

    Returns a list because OpenAI requires one ``{"role": "tool"}`` message
    per tool result, while opensquilla packs multiple tool results into a single
    Message.

    Invariant: tool_result blocks never coexist with text/image blocks in the
    same Message (agent.py always packs tool results into a dedicated message).
    """
    if isinstance(msg.content, str):
        return [
            _attach_reasoning_content(
                msg,
                {"role": msg.role, "content": msg.content},
                include_reasoning_content=include_reasoning_content,
                require_assistant_reasoning_content=require_assistant_reasoning_content,
            )
        ]

    parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    thinking_signature: str | None = None

    for block in msg.content:
        if block.type == "text":
            parts.append({"type": "text", "text": block.text})
        elif block.type == "thinking":
            sig = getattr(block, "signature", None)
            if isinstance(sig, str) and sig:
                thinking_signature = sig
        elif block.type == "tool_use":
            tc_dict: dict[str, Any] = {
                "id": block.id,
                "type": "function",
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input),
                },
            }
            tool_calls.append(tc_dict)
        elif block.type == "image":
            if block.source_type == "url":
                parts.append({"type": "image_url", "image_url": {"url": block.data}})
            else:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
                    }
                )
        elif block.type == "tool_result":
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": block.tool_use_id,
                    "content": _openai_tool_result_content(block),
                }
            )

    # Tool results → one message per result (OpenAI requirement)
    if tool_results:
        return tool_results

    # Assistant message with tool_calls (preserve text alongside calls)
    if tool_calls:
        # Gemini requires thought_signature on the first tool_call in each
        # step of the current turn. Attach it if a ContentBlockThinking with
        # a signature preceded the tool_use blocks — but never replay a
        # signature to a provider that did not mint it.
        if thinking_signature and tool_calls and replay_provider_state:
            tool_calls[0]["extra_content"] = {
                "google": {"thought_signature": thinking_signature},
            }
        result: dict[str, Any] = {"role": msg.role, "tool_calls": tool_calls}
        text_content = " ".join(p["text"] for p in parts if p.get("type") == "text")
        if text_content:
            result["content"] = text_content
        return [
            _attach_reasoning_content(
                msg,
                result,
                include_reasoning_content=include_reasoning_content,
                require_assistant_reasoning_content=require_assistant_reasoning_content,
            )
        ]

    # If parts contain mixed content (text + images), return as list for multimodal
    has_non_text = any(p["type"] != "text" for p in parts)
    if has_non_text:
        return [
            _attach_reasoning_content(
                msg,
                {"role": msg.role, "content": parts},
                include_reasoning_content=include_reasoning_content,
                require_assistant_reasoning_content=require_assistant_reasoning_content,
            )
        ]
    content_text = " ".join(p["text"] for p in parts if p["type"] == "text")
    return [
        _attach_reasoning_content(
            msg,
            {"role": msg.role, "content": content_text},
            include_reasoning_content=include_reasoning_content,
            require_assistant_reasoning_content=require_assistant_reasoning_content,
        )
    ]


def _build_openai_wire_messages(
    messages: list[Message],
    cfg: ChatConfig,
    *,
    policy: OpenAICompatPolicy,
    provider_kind: str,
    model: str,
    replay_provider_state: bool,
    reasoning_echo_turns: int | None,
) -> list[dict[str, Any]]:
    """Build the exact OpenAI-compatible wire-message array, without I/O."""
    openai_messages: list[dict[str, Any]] = []
    caps = cfg.model_capabilities
    include_reasoning_content = replay_provider_state and (
        _should_replay_reasoning_content(
            policy=policy,
            model=model,
            caps=caps,
            thinking=cfg.thinking,
        )
    )
    explicit_cache_supported = False
    if cfg.system:
        explicit_cache_supported = policy.supports_explicit_prompt_cache and (
            _supports_explicit_prompt_cache(
                provider_kind,
                model,
                cfg.cache_mode,
            )
        )
        if cfg.cache_breakpoints and explicit_cache_supported:
            content_blocks = _build_cache_breakpoint_blocks(
                cfg.cache_breakpoints,
                max_cache_markers=(
                    _DASHSCOPE_MAX_CACHE_MARKERS if provider_kind == "dashscope" else None
                ),
            )
            openai_messages.append({"role": "system", "content": content_blocks})
        else:
            openai_messages.append({"role": "system", "content": cfg.system})
    reasoning_echo_allowed = (
        _reasoning_echo_allowed_indexes(messages, reasoning_echo_turns)
        if include_reasoning_content
        else None
    )
    for message_index, message in enumerate(messages):
        openai_messages.extend(
            _build_openai_messages(
                message,
                include_reasoning_content=(
                    include_reasoning_content
                    if reasoning_echo_allowed is None
                    else message_index in reasoning_echo_allowed
                ),
                require_assistant_reasoning_content=(
                    _requires_assistant_reasoning_content(policy, model)
                ),
                replay_provider_state=replay_provider_state,
            )
        )
    if provider_kind == "dashscope" and cfg.cache_mode == "on":
        _attach_cache_control_to_latest_text_messages(
            openai_messages,
            max_cache_markers=_DASHSCOPE_MAX_CACHE_MARKERS,
        )
    elif (
        provider_kind == "openrouter"
        and cfg.cache_mode in {"auto", "on"}
        and explicit_cache_supported
        and _openrouter_model_uses_alibaba_message_cache(model)
    ):
        _attach_cache_control_to_latest_text_messages(
            openai_messages,
            max_cache_markers=_DASHSCOPE_MAX_CACHE_MARKERS,
        )
    return openai_messages


class OpenAIProvider:
    """Streams from OpenAI-compatible Chat Completions API (SSE)."""

    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        base_url: str = _OPENAI_API_BASE,
        org_id: str | None = None,
        proxy: str | None = None,
        provider_kind: str | None = None,
        provider_routing: Mapping[str, str] | None = None,
        compat: OpenAICompatPolicy | None = None,
        replay_provider_state: bool = True,
        provider_id: str | None = None,
    ) -> None:
        self._api_key = clean_header_secret(api_key, label="LLM API key")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._proxy = _resolve_llm_proxy(proxy)
        self._org_id = org_id
        if not provider_kind:
            # Fallback for direct construction only (tests, ad-hoc
            # embedding): every production path flows through
            # selector._build_provider, which always passes the registry
            # spec's provider_kind. The base-url sniff keeps a bare
            # OpenAIProvider(base_url="https://openrouter.ai/...") resolving
            # the OpenRouter dialect instead of silently degrading.
            provider_kind = "openrouter" if "openrouter.ai" in self._base_url else "openai"
        self._provider_kind = provider_kind
        # Keep configured deployment identity separate from the adapter family
        # (``provider_name``) and wire dialect (``_provider_kind``).  A
        # DashScope or DeepSeek instance still needs OpenAI-family behavior,
        # but must never be attributed to OpenAI in telemetry.
        self.provider_id = (provider_id or self.provider_name).strip()
        self._compat = compat or compat_policy_for_kind(self._provider_kind)
        self._replay_provider_state = replay_provider_state
        self._provider_routing: Mapping[str, str] = provider_routing or {}
        # Strict routing pin: send {"only": [...], "allow_fallbacks": false}
        # instead of the default {"order": [...], "allow_fallbacks": true},
        # so requests fail rather than silently reroute when the pinned
        # upstream is unavailable. Off by default.
        self._provider_routing_strict = (
            os.environ.get("OPENSQUILLA_PROVIDER_ROUTING_STRICT", "").strip().lower()
            in {"1", "true", "yes", "on", "enabled"}
        )
        # Opt-in reasoning-echo truncation: when a compat policy replays
        # assistant reasoning_content, every historical assistant message
        # carries its full reasoning bytes on every request. Limiting the
        # echo to the last N assistant messages caps that growth. None
        # (unset) keeps the replay-all behavior.
        self._reasoning_echo_turns = _resolve_reasoning_echo_turns()

    @property
    def model(self) -> str:
        """Model id this provider was configured with.

        Public so callers (e.g. derived-cache key construction) can identify
        the underlying model without prying at private state.
        """
        return self._model

    def disable_provider_state_replay(self) -> None:
        """Prevent provider-private reasoning/signature replay for this turn."""

        self._replay_provider_state = False

    def provider_metadata(self) -> ProviderMetadata:
        """Return read-only non-secret provider metadata for consumers."""
        return ProviderMetadata(
            provider_name=self.provider_name,
            provider_kind=self._provider_kind,
            model=self._model,
            base_url=self._base_url,
            provider_id=self.provider_id,
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        """Return provider-owned connection fields for internal runtime calls."""
        return ProviderConnectionConfig(
            provider_kind=self._provider_kind,
            model=self._model,
            api_key=self._api_key,
            base_url=self._base_url,
        )

    def _api_url(self, path: str) -> str:
        """Build an API URL without duplicating the version prefix.

        A base URL already carrying a version segment (``/v1``…``/vN``, e.g.
        Qianfan's ``/v2``, Volcengine's ``/api/v3``, Zhipu's ``/paas/v4``)
        absorbs the canonical ``/v1`` path prefix.
        """
        return _versioned_api_url(self._base_url, path)

    def project_message_count(
        self,
        messages: list[Message],
        config: ChatConfig | None = None,
        *,
        additional_messages: int = 0,
    ) -> ProviderMessageCountProjection:
        """Project this adapter's exact wire-message expansion without I/O."""
        if (
            not isinstance(additional_messages, int)
            or isinstance(additional_messages, bool)
            or additional_messages < 0
        ):
            raise ValueError("additional_messages must be a non-negative integer")
        cfg = config or ChatConfig()
        wire_messages = _build_openai_wire_messages(
            messages,
            cfg,
            policy=self._compat,
            provider_kind=self._provider_kind,
            model=self._model,
            replay_provider_state=self._replay_provider_state,
            reasoning_echo_turns=self._reasoning_echo_turns,
        )
        return ProviderMessageCountProjection(
            actual_wire_messages=len(wire_messages) + additional_messages,
            logical_messages=len(messages) + additional_messages,
            system_messages=sum(
                1 for message in wire_messages if message.get("role") == "system"
            ),
            tool_result_messages=sum(
                1 for message in wire_messages if message.get("role") == "tool"
            ),
            additional_messages=additional_messages,
            provider_kind=self._provider_kind,
            model=self._model,
            base_host=_base_url_hostname(self._base_url),
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        cfg = config or ChatConfig()
        return self._stream(messages, tools, cfg)

    async def _stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        cfg: ChatConfig,
    ) -> AsyncIterator[StreamEvent]:
        caps = cfg.model_capabilities
        include_reasoning_content = _should_replay_reasoning_content(
            policy=self._compat,
            model=self._model,
            caps=caps,
            thinking=cfg.thinking,
        )
        openai_messages = _build_openai_wire_messages(
            messages,
            cfg,
            policy=self._compat,
            provider_kind=self._provider_kind,
            model=self._model,
            replay_provider_state=self._replay_provider_state,
            reasoning_echo_turns=self._reasoning_echo_turns,
        )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if cfg.output_json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": cfg.output_json_schema_strict,
                    "schema": cfg.output_json_schema,
                },
            }
        if self._provider_kind == "dashscope" and include_reasoning_content:
            payload["preserve_thinking"] = True
        if _should_use_max_completion_tokens(
            self._compat,
            self._provider_kind,
            self._base_url,
            self._model,
            cfg,
            caps,
        ):
            payload["max_completion_tokens"] = cfg.max_tokens
        else:
            payload["max_tokens"] = cfg.max_tokens
        if self._compat.sends_usage_include:
            payload["usage"] = {"include": True}
        if self._compat.sends_disable_fallbacks:
            # Gateway proxies must not silently substitute another model:
            # SquillaRouter is the single routing authority.
            payload["disable_fallbacks"] = True
        if (
            self._compat.anthropic_top_level_cache
            and cfg.cache_mode in {"auto", "on"}
            and _openrouter_model_is_anthropic(self._model)
        ):
            payload["cache_control"] = {"type": "ephemeral"}
        if _should_send_temperature(
            self._compat,
            self._base_url,
            self._model,
            cfg,
            caps,
        ):
            payload["temperature"] = cfg.temperature
        if cfg.top_p is not None:
            payload["top_p"] = cfg.top_p
        if cfg.stop_sequences:
            payload["stop"] = cfg.stop_sequences
        if tools:
            payload["tools"] = [
                _build_openai_tool(
                    t,
                    unsupported_keywords=self._compat.tool_schema_unsupported_keywords,
                )
                for t in tools
            ]
            tool_names = [tool.get("function", {}).get("name", "") for tool in payload["tools"]]
            tool_schema_hash = hashlib.sha256(
                json.dumps(payload["tools"], ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            log.info(
                "provider.request_tool_surface",
                provider=self._provider_kind,
                model=self._model,
                provider_visible_tool_names=tool_names,
                tool_schema_hash=tool_schema_hash,
                temperature=payload.get("temperature"),
                top_p=payload.get("top_p"),
            )
            if _should_send_tool_choice(self._provider_kind, cfg, caps):
                payload["tool_choice"] = cfg.tool_choice
        if self._compat.supports_provider_routing_pin:
            pinned_provider = self._provider_routing.get(self._model)
            if pinned_provider:
                if self._provider_routing_strict:
                    payload["provider"] = {
                        "only": [pinned_provider],
                        "allow_fallbacks": False,
                    }
                else:
                    payload["provider"] = {
                        "order": [pinned_provider],
                        "allow_fallbacks": True,
                    }

        # Reasoning injection (gated on thinking being enabled). Gating —
        # which model/capability profile triggers a payload at all — lives
        # here; how each dialect spells it lives in reasoning_dialects.
        thinking_toggle_model = (
            self._model.strip().lower() in self._compat.thinking_toggle_model_ids
        )
        if (caps and caps.supports_reasoning and cfg.thinking) or (
            thinking_toggle_model and cfg.thinking
        ):
            reasoning_format = (
                caps.reasoning_format
                if caps is not None
                else self._compat.default_reasoning_format
            )
            apply_reasoning_enable(
                payload,
                reasoning_format,
                ReasoningEnableArgs(
                    thinking_level=cfg.thinking_level,
                    thinking_budget_tokens=cfg.thinking_budget_tokens,
                ),
            )
            if reasoning_format == "dashscope":
                # DashScope thinking budget: the local env override wins;
                # without an explicit per-call budget the field is omitted
                # entirely so the endpoint applies its own default.
                env_thinking_budget = _thinking_budget_tokens_from_env()
                if env_thinking_budget is not None:
                    payload["thinking_budget"] = env_thinking_budget
                elif not cfg.thinking_budget_explicit:
                    payload.pop("thinking_budget", None)
        elif (
            self._compat.thinking_required_model_prefixes
            and self._model.strip().lower().startswith(
                self._compat.thinking_required_model_prefixes
            )
        ):
            # Forced-thinking endpoints reject enable_thinking=False; omit the
            # off-payload so the request still succeeds. The model will still
            # think — that is the only mode the endpoint supports.
            pass
        elif thinking_toggle_model:
            # Toggle models need an explicit off payload even without a
            # capability profile (policy gating, independent of dialect).
            payload["thinking"] = {"type": "disabled"}
        elif caps and caps.supports_reasoning:
            apply_reasoning_disable(
                payload,
                caps.reasoning_format,
                ReasoningDisableArgs(
                    model=self._model,
                    disable_reasoning_by_default_models=(
                        self._compat.disable_reasoning_by_default_models
                    ),
                ),
            )

        if self._provider_kind == "dashscope":
            log.info(
                "provider.qwen_provider_profile",
                provider=self._provider_kind,
                model=self._model,
                endpoint_family=_dashscope_endpoint_family(self._base_url),
                thinking_enabled=bool(payload.get("enable_thinking")),
                thinking_budget=payload.get("thinking_budget"),
                temperature=payload.get("temperature"),
                top_p=payload.get("top_p"),
                cache_mode=cfg.cache_mode,
                text_tool_parser="qwen_tags",
                stream_fallback="non_stream_once",
            )

        fallback_reason = (
            "native_is_error_unavailable"
            if any(message.get("role") == "tool" for message in openai_messages)
            else None
        )
        from opensquilla.engine.context_budget import coordinate_provider_context_budget

        budget_decision = coordinate_provider_context_budget(
            payload,
            projection_adapter=self._provider_kind,
            proof_budget=cfg.provider_request_max_chars,
            status_projection_mode="content_envelope",
            fallback_reason=fallback_reason,
        )
        if budget_decision.action == "budget_limited":
            proof = budget_decision.proof or {}
            log.warning("provider.request_budget_exhausted", **proof)
            yield ErrorEvent(
                message=json.dumps(proof, ensure_ascii=False, sort_keys=True),
                code="provider_request_budget_exhausted",
            )
            return
        payload = budget_decision.payload or payload
        if budget_decision.proof is not None:
            log.info("provider.request_proof", **budget_decision.proof)
        try:
            prove_provider_payload_from_env(
                payload,
                projection_adapter=self._provider_kind,
                status_projection_mode="content_envelope",
                fallback_reason=fallback_reason,
            )
        except ProviderRequestBudgetExceededError as exc:
            log.warning("provider.request_budget_exhausted", **exc.proof)
            yield ErrorEvent(
                message=json.dumps(exc.proof, ensure_ascii=False, sort_keys=True),
                code="provider_request_budget_exhausted",
            )
            return

        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        headers.update(provider_app_headers(self._base_url))
        headers.update(
            tokenrhythm_correlation_headers(
                self._provider_kind,
                self._base_url,
                cfg.provider_request_correlation,
            )
        )
        if self._org_id:
            headers["OpenAI-Organization"] = self._org_id

        inert_candidate_output = cfg.candidate_output_mode == "inert_artifact"
        candidate_artifact = (
            CandidateArtifactBuilder() if inert_candidate_output else None
        )
        candidate_artifact_open_keys: set[Any] = set()
        candidate_artifact_wire_keys: dict[bytes, Any] = {}
        tools_acc = ToolStreamAccumulator()
        # Gemini thought_signature streamed on a non-FC text delta. Kept
        # separate from the tool accumulator (whose keys MUST stay int — see
        # _resolve_tool_call_index's next_int_key) so a str key can never
        # poison the next-index computation with a TypeError.
        streamed_thought_signature: str | None = None
        reasoning = ReasoningAccumulator()
        tools_by_name = _tool_by_name(tools)
        text_tool_dialects = self._compat.text_tool_profile.dialects_for_model(self._model)
        text_tool_normalizer: (
            TextToolStreamNormalizer | _InertCandidateTextPassthrough
        )
        if inert_candidate_output:
            text_tool_normalizer = _InertCandidateTextPassthrough()
        else:
            text_tool_normalizer = TextToolStreamNormalizer(
                tools=tools,
                dialects=text_tool_dialects,
                provider_kind=self._provider_kind,
                model=self._model,
            )
        assistant_text_parts: list[str] = []
        visible_assistant_text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0
        cached_tokens = 0
        cache_write_tokens = 0
        billed_cost = 0.0
        cost_source = "none"
        billing_receipt: ProviderBillingReceipt | None = None
        usage_accumulator = _UsageSnapshotAccumulator()
        billing_accumulator = _ProviderBillingAccumulator()
        actual_model = self._model
        stop_reason = "stop"
        emitted_stream_event = False
        saw_done_sentinel = False
        finish_reasons: list[str] = []
        deferred_native_events = _DeferredStreamEventBuffer()
        deferred_post_native_events = _DeferredStreamEventBuffer()
        pending_native_identity_events: dict[Any, _DeferredStreamEventBuffer] = {}
        native_key_order: list[Any] = []
        native_flushed_keys: set[Any] = set()
        native_identity_flush_index = 0
        native_tool_names: dict[Any, str] = {}
        native_wire_ids: dict[Any, str] = {}
        invalid_native_structure = 0
        malformed_stream_frames = 0
        choice_terminal_seen = False
        terminal_finish_reason: str | None = None
        terminal_native_finish_reason_present = False
        terminal_native_finish_reason: Any = None
        active_choice_seen = False

        if os.environ.get("OPENSQUILLA_TRACE_ROUTING"):
            print(
                f"[CALLED] base={self._base_url} model={self._model} "
                f"n_messages={len(openai_messages)}",
                file=sys.stderr,
                flush=True,
            )
        cache_shape = _payload_cache_shape(payload, tools=tools)
        endpoint = self._api_url("/v1/chat/completions")
        trace = LLMTraceRecorder(
            provider=self._provider_kind,
            model=self._model,
            base_url=self._base_url,
            endpoint=endpoint,
            stream=True,
        )
        trace.record_request(
            payload=payload,
            headers=headers,
            metadata={
                "cache_shape": cache_shape,
                "timeout_seconds": cfg.timeout,
                "tools_count": len(tools or []),
                "request_proof": budget_decision.proof,
            },
        )
        if self._compat.log_payload_cache_shape:
            log.debug(
                "openrouter.payload_cache_shape",
                model=self._model,
                **cache_shape,
            )
        elif self._provider_kind == "dashscope":
            log.info(
                "dashscope.payload_cache_shape",
                model=self._model,
                **cache_shape,
            )

        def deferred_queue_is_oversized() -> bool:
            identity_event_count = sum(
                buffer.event_count
                for buffer in pending_native_identity_events.values()
            )
            identity_chars = sum(
                buffer.char_count
                for buffer in pending_native_identity_events.values()
            )
            return (
                deferred_native_events.event_count
                + deferred_post_native_events.event_count
                + identity_event_count
                + tools_acc.pending_unemitted_event_count
                + text_tool_normalizer.held_event_count
                > _MAX_DEFERRED_NATIVE_EVENTS
                or deferred_native_events.char_count
                + deferred_post_native_events.char_count
                + identity_chars
                + tools_acc.pending_unemitted_char_count
                + text_tool_normalizer.held_chars
                > _MAX_DEFERRED_NATIVE_ARGUMENT_CHARS
            )

        def release_deferred_queue() -> list[StreamEvent]:
            log.warning(
                "provider.deferred_native_queue_oversized",
                provider=self._provider_kind,
                model=self._model,
                max_events=_MAX_DEFERRED_NATIVE_EVENTS,
                max_argument_chars=_MAX_DEFERRED_NATIVE_ARGUMENT_CHARS,
            )
            released: list[StreamEvent] = list(
                _segment_text_tool_events(
                    text_tool_normalizer.abandon_native_lifecycle_defer(),
                    provider_kind=self._provider_kind,
                    model=self._model,
                )
            )
            released.extend(deferred_native_events.drain())
            released.extend(deferred_post_native_events.drain())
            return released

        try:
            async with httpx.AsyncClient(
                timeout=(
                    _stream_timeout(cfg.timeout)
                    if self._compat.stream_timeout_fallback
                    else cfg.timeout
                ),
                trust_env=_trust_env(),
                proxy=self._proxy,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=payload,
                ) as response:
                    if self._compat.attribution_response_headers:
                        attribution = {
                            name: response.headers[name]
                            for name in self._compat.attribution_response_headers
                            if name in response.headers
                        }
                        if attribution:
                            fallbacks_taken = _coerce_int(
                                attribution.get("x-litellm-attempted-fallbacks")
                            )
                            log_fn = log.warning if fallbacks_taken > 0 else log.info
                            log_fn(
                                "provider.gateway_attribution",
                                provider=self._provider_kind,
                                requested_model=self._model,
                                **{k.replace("-", "_"): v for k, v in attribution.items()},
                            )
                    if response.status_code != 200:
                        body = await response.aread()
                        body_text = (
                            body.decode("utf-8", errors="replace")
                            if isinstance(body, bytes)
                            else str(body)
                        )
                        safe_body_text = redact_upstream_error_text(
                            body_text,
                            api_key=self._api_key,
                            max_len=4000,
                        )
                        message = redact_upstream_error_text(
                            _format_chat_http_error(
                                self._compat.display_name,
                                response.status_code,
                                body,
                            ),
                            api_key=self._api_key,
                            max_len=2000,
                        )
                        message_limit_evidence = _tokenrhythm_message_limit_evidence(
                            provider_kind=self._provider_kind,
                            base_url=self._base_url,
                            model=self._model,
                            status_code=response.status_code,
                            body=body,
                            wire_messages=payload.get("messages"),
                            logical_messages=len(messages),
                        )
                        if message_limit_evidence is not None:
                            message_limit_proof, validation_message = message_limit_evidence
                            message = _format_tokenrhythm_message_limit_error(
                                self._compat.display_name,
                                response.status_code,
                                body,
                                validation_message,
                            )
                            message = redact_upstream_error_text(
                                message,
                                api_key=self._api_key,
                                max_len=2000,
                            )
                            proof_fields = asdict(message_limit_proof)
                            log.warning(
                                "provider.request_message_limit_detected",
                                **proof_fields,
                            )
                            trace.record_error(
                                code=str(response.status_code),
                                message="Provider request message limit detected",
                                status_code=response.status_code,
                                metadata={
                                    "cache_shape": cache_shape,
                                    "message_limit_proof": proof_fields,
                                },
                            )
                            yield ErrorEvent(
                                message=message,
                                code=str(response.status_code),
                                retry_after_s=retry_after_from_headers(
                                    response.status_code,
                                    getattr(response, "headers", None),
                                ),
                                message_limit_proof=message_limit_proof,
                            )
                            return
                        # Diagnostic: dump payload head (no auth headers)
                        # so 400s from picky upstreams are debuggable. Truncated
                        # to keep memory low.
                        try:
                            _payload_head = json.dumps(
                                payload,
                                ensure_ascii=False,
                            )[:4000]
                        except Exception:  # noqa: BLE001
                            _payload_head = repr(payload)[:4000]
                        log.warning(
                            "provider.chat_http_error",
                            provider=self._provider_kind,
                            model=self._model,
                            status_code=response.status_code,
                            message=message,
                            response_body=safe_body_text[:2000],
                            request_payload_head=_payload_head,
                        )
                        trace.record_error(
                            code=str(response.status_code),
                            message=message,
                            status_code=response.status_code,
                            response_body=safe_body_text,
                            metadata={"cache_shape": cache_shape},
                        )
                        yield ErrorEvent(
                            message=message,
                            code=str(response.status_code),
                            retry_after_s=retry_after_from_headers(
                                response.status_code,
                                getattr(response, "headers", None),
                            ),
                        )
                        return

                    response_ids: set[str] = set()
                    trace_tool_calls: list[dict[str, Any]] = []
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:]
                        if data_str.startswith(" "):
                            data_str = data_str[1:]
                        if data_str == "[DONE]":
                            saw_done_sentinel = True
                            break
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, RecursionError):
                            if data_str.strip():
                                malformed_stream_frames += 1
                                log.warning(
                                    "provider.invalid_stream_frame",
                                    provider=self._provider_kind,
                                    model=self._model,
                                    frame_chars=len(data_str),
                                )
                            continue
                        if not isinstance(chunk, dict):
                            malformed_stream_frames += 1
                            log.warning(
                                "provider.invalid_stream_frame",
                                provider=self._provider_kind,
                                model=self._model,
                                frame_chars=len(data_str),
                                reason="json_frame_not_object",
                            )
                            continue
                        billing_chunk = _exact_provider_billing_payload(
                            self._provider_kind,
                            chunk,
                            data_str,
                        )

                        if "error" in chunk and chunk["error"] is not None:
                            error_obj = chunk["error"]
                            err_message = (
                                str(error_obj.get("message") or "stream error frame")
                                if isinstance(error_obj, Mapping)
                                else str(error_obj).strip() or "stream error frame"
                            )
                            err_message = redact_upstream_error_text(
                                err_message,
                                api_key=self._api_key,
                                max_len=2000,
                            )
                            raw_code = (
                                error_obj.get("code")
                                if isinstance(error_obj, Mapping)
                                else None
                            )
                            err_code = (
                                str(raw_code)
                                if raw_code not in (None, "")
                                else "stream_error"
                            )
                            err_code = redact_upstream_error_code(
                                err_code,
                                api_key=self._api_key,
                            )
                            log.warning(
                                "provider.stream_error_frame",
                                provider=self._provider_kind,
                                model=self._model,
                                code=err_code,
                                message=err_message,
                            )
                            trace.record_error(
                                code=err_code,
                                message=err_message,
                                metadata={
                                    "phase": "stream",
                                    "cache_shape": cache_shape,
                                },
                            )
                            # An explicit top-level error field poisons the response,
                            # including malformed empty error envelopes.
                            # Provisional text/tool events already delivered stay
                            # diagnostic only; no deferred End or Done is released.
                            yield ErrorEvent(
                                message=(
                                    f"{self._compat.display_name} stream error: "
                                    f"{err_message}"
                                ),
                                code=err_code,
                            )
                            return
                        trace.record_chunk(chunk)
                        chunk_id = chunk.get("id")
                        if isinstance(chunk_id, str) and chunk_id:
                            response_ids.add(chunk_id)
                        chunk_model = chunk.get("model")
                        if chunk_model:
                            actual_model = chunk_model

                        raw_choices = chunk.get("choices", [])
                        if not isinstance(raw_choices, list) or len(raw_choices) > 1:
                            trace.record_error(
                                code="invalid_stream_frame",
                                message="Provider stream returned an invalid choice batch",
                                metadata={"phase": "stream", "cache_shape": cache_shape},
                            )
                            yield ErrorEvent(
                                message=(
                                    f"{self._compat.display_name} stream returned "
                                    "multiple or malformed choices"
                                ),
                                code="invalid_stream_frame",
                            )
                            return
                        if choice_terminal_seen:
                            assert terminal_finish_reason is not None
                            if not _is_inert_post_terminal_stream_frame(
                                chunk=chunk,
                                raw_choices=raw_choices,
                                terminal_finish_reason=terminal_finish_reason,
                                terminal_native_finish_reason_present=(
                                    terminal_native_finish_reason_present
                                ),
                                terminal_native_finish_reason=(
                                    terminal_native_finish_reason
                                ),
                                policy=self._compat,
                            ):
                                trace.record_error(
                                    code="invalid_stream_order",
                                    message="Provider mutated state after finish_reason",
                                    metadata={
                                        "phase": "stream",
                                        "cache_shape": cache_shape,
                                    },
                                )
                                yield ErrorEvent(
                                    message=(
                                        f"{self._compat.display_name} stream mutated "
                                        "state after finish_reason"
                                    ),
                                    code="invalid_stream_order",
                                )
                                return
                            usage_payload = chunk.get("usage")
                            billing_accumulator.update(
                                self._provider_kind,
                                billing_chunk,
                            )
                            if isinstance(usage_payload, Mapping):
                                usage_accumulator.update(usage_payload)
                                (
                                    input_tokens,
                                    output_tokens,
                                    reasoning_tokens,
                                    cached_tokens,
                                    cache_write_tokens,
                                    _,
                                ) = usage_accumulator.fields()
                                _log_provider_cache_usage(
                                    provider_kind=self._provider_kind,
                                    model=self._model,
                                    actual_model=actual_model,
                                    input_tokens=input_tokens,
                                    cached_tokens=cached_tokens,
                                    cache_write_tokens=cache_write_tokens,
                                    cache_shape=cache_shape,
                                )
                            # Usage was already accounted for above.  Do not let
                            # the duplicate choice re-enter the normal parser or
                            # append a second finish reason.
                            continue

                        # Usage is a cumulative snapshot. Apply it only after
                        # the frame's outer shape has passed validation; later
                        # snapshots replace fields they contain and preserve
                        # details they omit.
                        usage_payload = chunk.get("usage")
                        if usage_payload is not None and not isinstance(
                            usage_payload,
                            Mapping,
                        ):
                            trace.record_error(
                                code="invalid_stream_frame",
                                message="Provider stream returned malformed usage",
                                metadata={"phase": "stream", "cache_shape": cache_shape},
                            )
                            yield ErrorEvent(
                                message=(
                                    f"{self._compat.display_name} stream returned "
                                    "malformed usage"
                                ),
                                code="invalid_stream_frame",
                            )
                            return
                        # Native billing fields are independent top-level
                        # metadata. A terminal choice may carry settlement
                        # status while a later usage trailer carries the
                        # amount, so do not couple their accumulation to the
                        # presence of ``usage`` on this frame.
                        billing_accumulator.update(
                            self._provider_kind,
                            billing_chunk,
                        )
                        if isinstance(usage_payload, Mapping):
                            usage_accumulator.update(usage_payload)
                            (
                                input_tokens,
                                output_tokens,
                                reasoning_tokens,
                                cached_tokens,
                                cache_write_tokens,
                                _,
                            ) = usage_accumulator.fields()
                            _log_provider_cache_usage(
                                provider_kind=self._provider_kind,
                                model=self._model,
                                actual_model=actual_model,
                                input_tokens=input_tokens,
                                cached_tokens=cached_tokens,
                                cache_write_tokens=cache_write_tokens,
                                cache_shape=cache_shape,
                            )

                        for choice in raw_choices:
                            if not isinstance(choice, Mapping):
                                yield ErrorEvent(
                                    message=(
                                        f"{self._compat.display_name} stream returned "
                                        "a malformed choice"
                                    ),
                                    code="invalid_stream_frame",
                                )
                                return
                            choice_index = choice.get("index", 0)
                            if (
                                not isinstance(choice_index, int)
                                or isinstance(choice_index, bool)
                                or choice_index != 0
                            ):
                                yield ErrorEvent(
                                    message=(
                                        f"{self._compat.display_name} stream returned "
                                        "an unsupported choice index"
                                    ),
                                    code="invalid_stream_frame",
                                )
                                return
                            active_choice_seen = True
                            finish = choice.get("finish_reason")
                            if finish is not None and (
                                not isinstance(finish, str) or not finish.strip()
                            ):
                                yield ErrorEvent(
                                    message=(
                                        f"{self._compat.display_name} stream returned "
                                        "an invalid finish reason"
                                    ),
                                    code="invalid_stream_frame",
                                )
                                return
                            if finish:
                                stop_reason = finish
                                finish_reasons.append(str(finish))

                            delta = choice.get("delta", {})
                            if not isinstance(delta, Mapping):
                                yield ErrorEvent(
                                    message=(
                                        f"{self._compat.display_name} stream returned "
                                        "a malformed choice delta"
                                    ),
                                    code="invalid_stream_frame",
                                )
                                return

                            # Text content
                            text = delta.get("content")
                            if text:
                                emitted_stream_event = True
                                assistant_text_parts.append(text)
                                for visible_text in text_tool_normalizer.push(text):
                                    text_event = TextDeltaEvent(text=visible_text)
                                    if text_tool_normalizer.native_lifecycle_deferred:
                                        _append_coalesced_stream_event(
                                            deferred_post_native_events,
                                            text_event,
                                        )
                                        if deferred_queue_is_oversized():
                                            for release_event in release_deferred_queue():
                                                if isinstance(
                                                    release_event,
                                                    TextDeltaEvent,
                                                ):
                                                    visible_assistant_text_parts.append(
                                                        release_event.text
                                                    )
                                                yield release_event
                                    else:
                                        visible_assistant_text_parts.append(visible_text)
                                        yield text_event
                                if deferred_queue_is_oversized():
                                    for release_event in release_deferred_queue():
                                        if isinstance(release_event, TextDeltaEvent):
                                            visible_assistant_text_parts.append(
                                                release_event.text
                                            )
                                        yield release_event

                            # Reasoning content (always parsed, not gated on thinking).
                            # Streamed in real time as ReasoningDeltaEvent; the
                            # accumulator also retains the joined text for DoneEvent.
                            # Counts as an emitted stream event: once the caller
                            # has received reasoning deltas, an empty-stream or
                            # timeout fallback retry would deliver (and bill)
                            # the turn twice.
                            reasoning_details = delta.get("reasoning_details")
                            if reasoning_details:
                                for detail in reasoning_details:
                                    if isinstance(detail, dict):
                                        reasoning_event = reasoning.emit(detail.get("text", ""))
                                        if reasoning_event is not None:
                                            emitted_stream_event = True
                                            if text_tool_normalizer.native_lifecycle_deferred:
                                                _append_coalesced_stream_event(
                                                    deferred_post_native_events,
                                                    reasoning_event,
                                                )
                                                if deferred_queue_is_oversized():
                                                    for (
                                                        release_event
                                                    ) in release_deferred_queue():
                                                        if isinstance(
                                                            release_event,
                                                            TextDeltaEvent,
                                                        ):
                                                            visible_assistant_text_parts.append(
                                                                release_event.text
                                                            )
                                                        yield release_event
                                            else:
                                                yield reasoning_event
                            reasoning_event = reasoning.emit(delta.get("reasoning_content"))
                            if reasoning_event is not None:
                                emitted_stream_event = True
                                if text_tool_normalizer.native_lifecycle_deferred:
                                    _append_coalesced_stream_event(
                                        deferred_post_native_events,
                                        reasoning_event,
                                    )
                                    if deferred_queue_is_oversized():
                                        for release_event in release_deferred_queue():
                                            if isinstance(
                                                release_event,
                                                TextDeltaEvent,
                                            ):
                                                visible_assistant_text_parts.append(
                                                    release_event.text
                                                )
                                            yield release_event
                                else:
                                    yield reasoning_event

                            # Gemini thought_signature on non-FC deltas
                            # (streamed thinking path): Gemini sends it on
                            # the top-level delta instead of attaching it to
                            # a tool_call. Keep it out of the tool accumulator.
                            ts_delta = delta.get("thought_signature")
                            if isinstance(ts_delta, str) and ts_delta:
                                streamed_thought_signature = ts_delta

                            # Tool calls (may stream over multiple chunks)
                            raw_tool_calls = delta.get("tool_calls") or []
                            if not isinstance(raw_tool_calls, list):
                                if inert_candidate_output:
                                    assert candidate_artifact is not None
                                    candidate_artifact.observe_call(
                                        ("invalid_tool_calls", candidate_artifact.call_count),
                                        arguments=strip_candidate_tool_identity(
                                            raw_tool_calls
                                        ),
                                    )
                                    emitted_stream_event = True
                                else:
                                    invalid_native_structure += 1
                                    log.warning(
                                        "provider.native_tool_call_invalid",
                                        provider=self._provider_kind,
                                        model=self._model,
                                        reason="tool_calls_not_array",
                                    )
                                raw_tool_calls = []
                            for tc in raw_tool_calls:
                                if not isinstance(tc, Mapping):
                                    if inert_candidate_output:
                                        assert candidate_artifact is not None
                                        candidate_artifact.observe_call(
                                            ("invalid_tool_call", candidate_artifact.call_count),
                                            arguments=strip_candidate_tool_identity(tc),
                                        )
                                        emitted_stream_event = True
                                    else:
                                        invalid_native_structure += 1
                                        log.warning(
                                            "provider.native_tool_call_invalid",
                                            provider=self._provider_kind,
                                            model=self._model,
                                            reason="tool_call_not_object",
                                        )
                                    continue
                                if (
                                    self._provider_kind == "dashscope"
                                    and _dashscope_tool_call_chunk_is_empty(tc)
                                    and (
                                        not inert_candidate_output
                                        or _candidate_malformed_tool_wrapper(tc) is None
                                    )
                                ):
                                    log.warning(
                                        "dashscope.stream_tool_chunk_sanitized",
                                        model=self._model,
                                        reason="empty_tool_call_chunk",
                                    )
                                    continue
                                if inert_candidate_output:
                                    assert candidate_artifact is not None
                                    raw_idx = tc.get("index")
                                    raw_wire_id = tc.get("id")
                                    if (
                                        isinstance(raw_idx, int)
                                        and not isinstance(raw_idx, bool)
                                        and raw_idx >= 0
                                    ):
                                        # A valid provider index is already a
                                        # bounded stream-local identity. Do not
                                        # inspect or retain an attacker-sized ID.
                                        artifact_key: Any = ("index", raw_idx)
                                    else:
                                        wire_digest = (
                                            _candidate_wire_digest(raw_wire_id)
                                            if isinstance(raw_wire_id, str)
                                            and raw_wire_id
                                            else None
                                        )
                                        if wire_digest is not None:
                                            artifact_key = (
                                                candidate_artifact_wire_keys.get(
                                                    wire_digest,
                                                    ("wire_digest", wire_digest),
                                                )
                                            )
                                            candidate_artifact_wire_keys[wire_digest] = (
                                                artifact_key
                                            )
                                        else:
                                            if (
                                                "index" not in tc
                                                and len(candidate_artifact_open_keys) == 1
                                            ):
                                                artifact_key = next(
                                                    iter(candidate_artifact_open_keys)
                                                )
                                            else:
                                                artifact_key = (
                                                    "sequence",
                                                    candidate_artifact.call_count,
                                                )
                                    raw_function = tc.get("function")
                                    if isinstance(raw_function, Mapping):
                                        name_fragment = raw_function.get("name")
                                        arguments_fragment = raw_function.get("arguments")
                                    else:
                                        name_fragment = None
                                        arguments_fragment = (
                                            strip_candidate_tool_identity(raw_function)
                                            if _candidate_fragment_has_content(raw_function)
                                            else None
                                        )
                                    if (
                                        not _candidate_fragment_has_content(name_fragment)
                                        and not _candidate_fragment_has_content(
                                            arguments_fragment
                                        )
                                    ):
                                        malformed_wrapper = (
                                            _candidate_malformed_tool_wrapper(tc)
                                        )
                                        if malformed_wrapper is not None:
                                            arguments_fragment = malformed_wrapper
                                    candidate_artifact.append_or_start(
                                        artifact_key,
                                        name_fragment=name_fragment,
                                        arguments_fragment=arguments_fragment,
                                    )
                                    candidate_artifact_open_keys.add(artifact_key)
                                    emitted_stream_event = True
                                    continue
                                idx, index_valid = _resolve_tool_call_index(tc, tools_acc)
                                if not index_valid:
                                    invalid_native_structure += 1
                                    log.warning(
                                        "provider.native_tool_call_invalid",
                                        provider=self._provider_kind,
                                        model=self._model,
                                        reason="invalid_tool_call_index",
                                    )
                                wire_id = tc.get("id")
                                wire_id = wire_id if isinstance(wire_id, str) else ""
                                existing_wire_id = native_wire_ids.get(idx, "")
                                if (
                                    existing_wire_id
                                    and wire_id
                                    and existing_wire_id != wire_id
                                ):
                                    invalid_native_structure += 1
                                    log.warning(
                                        "provider.native_tool_call_invalid",
                                        provider=self._provider_kind,
                                        model=self._model,
                                        reason="conflicting_tool_call_id",
                                    )
                                    matching_key = tools_acc.find_key_for_tool_call_id(
                                        wire_id
                                    )
                                    idx = (
                                        cast(int, matching_key)
                                        if matching_key is not None
                                        else tools_acc.next_int_key()
                                    )
                                if wire_id and idx not in native_wire_ids:
                                    native_wire_ids[idx] = wire_id
                                is_new_native_key = not tools_acc.has_key(idx)
                                if is_new_native_key:
                                    native_key_order.append(idx)
                                raw_function = tc.get("function", {}) or {}
                                if not isinstance(raw_function, Mapping):
                                    invalid_native_structure += 1
                                    log.warning(
                                        "provider.native_tool_call_invalid",
                                        provider=self._provider_kind,
                                        model=self._model,
                                        reason="function_not_object",
                                    )
                                    raw_function = {}
                                function = raw_function
                                raw_tool_name = function.get("name")
                                tool_name = (
                                    raw_tool_name if isinstance(raw_tool_name, str) else ""
                                )
                                existing_tool_name = native_tool_names.get(idx, "")
                                if tool_name.strip():
                                    if existing_tool_name and existing_tool_name != tool_name:
                                        invalid_native_structure += 1
                                        log.warning(
                                            "provider.native_tool_call_invalid",
                                            provider=self._provider_kind,
                                            model=self._model,
                                            reason="conflicting_tool_name",
                                        )
                                    elif not existing_tool_name:
                                        native_tool_names[idx] = tool_name
                                effective_tool_name = native_tool_names.get(idx, "")
                                if is_new_native_key:
                                    pending_segments = (
                                        text_tool_normalizer.observe_native_tool_start(
                                            effective_tool_name
                                        )
                                    )
                                    for pending_event in _segment_text_tool_events(
                                        pending_segments,
                                        provider_kind=self._provider_kind,
                                        model=self._model,
                                    ):
                                        if isinstance(pending_event, TextDeltaEvent):
                                            visible_assistant_text_parts.append(
                                                pending_event.text
                                            )
                                            emitted_stream_event = True
                                            yield pending_event
                                raw_arguments_fragment = function.get("arguments", "")
                                if raw_arguments_fragment is None:
                                    arguments_fragment = ""
                                elif isinstance(raw_arguments_fragment, str):
                                    arguments_fragment = raw_arguments_fragment
                                else:
                                    invalid_native_structure += 1
                                    log.warning(
                                        "provider.native_tool_call_invalid",
                                        provider=self._provider_kind,
                                        model=self._model,
                                        reason="arguments_fragment_not_string",
                                    )
                                    arguments_fragment = ""
                                tool_events = list(
                                    tools_acc.append_or_start(
                                        idx,
                                        tool_call_id=(
                                            wire_id or None
                                        ),
                                        tool_name=effective_tool_name,
                                        fragment=arguments_fragment,
                                    )
                                )
                                routed_tool_events: list[StreamEvent] = []
                                if idx in native_flushed_keys:
                                    routed_tool_events.extend(tool_events)
                                else:
                                    identity_events = (
                                        pending_native_identity_events.setdefault(
                                            idx,
                                            _DeferredStreamEventBuffer(),
                                        )
                                    )
                                    for tool_event in tool_events:
                                        emitted_stream_event = True
                                        _append_coalesced_stream_event(
                                            identity_events,
                                            tool_event,
                                        )
                                    while native_identity_flush_index < len(
                                        native_key_order
                                    ):
                                        flush_key = native_key_order[
                                            native_identity_flush_index
                                        ]
                                        known_name = native_tool_names.get(flush_key, "")
                                        if not known_name:
                                            break
                                        flush_buffer = (
                                            pending_native_identity_events.pop(
                                                flush_key,
                                                _DeferredStreamEventBuffer(),
                                            )
                                        )
                                        flush_buffer.patch_start_tool_name(known_name)
                                        routed_tool_events.extend(flush_buffer.drain())
                                        native_flushed_keys.add(flush_key)
                                        native_identity_flush_index += 1

                                    if deferred_queue_is_oversized():
                                        log.warning(
                                            "provider.pending_native_identity_oversized",
                                            provider=self._provider_kind,
                                            model=self._model,
                                            max_events=_MAX_DEFERRED_NATIVE_EVENTS,
                                            max_argument_chars=(
                                                _MAX_DEFERRED_NATIVE_ARGUMENT_CHARS
                                            ),
                                        )
                                        for release_event in _segment_text_tool_events(
                                            text_tool_normalizer.finish(
                                                successful_text_tool_terminal=False,
                                            ),
                                            provider_kind=self._provider_kind,
                                            model=self._model,
                                        ):
                                            if isinstance(release_event, TextDeltaEvent):
                                                visible_assistant_text_parts.append(
                                                    release_event.text
                                                )
                                            yield release_event
                                        for native_event in deferred_native_events:
                                            yield native_event
                                        for post_native_event in deferred_post_native_events:
                                            if isinstance(
                                                post_native_event,
                                                TextDeltaEvent,
                                            ):
                                                visible_assistant_text_parts.append(
                                                    post_native_event.text
                                                )
                                            yield post_native_event
                                        trace.record_error(
                                            code="incomplete_tool_call",
                                            message=(
                                                "Native tool identity remained missing "
                                                "beyond the bounded queue"
                                            ),
                                            metadata={
                                                "phase": "stream",
                                                "cache_shape": cache_shape,
                                            },
                                        )
                                        yield ErrorEvent(
                                            message=(
                                                f"{self._compat.display_name} returned "
                                                "an incomplete native tool identity"
                                            ),
                                            code="incomplete_tool_call",
                                        )
                                        return
                                for tool_event in routed_tool_events:
                                    emitted_stream_event = True
                                    if text_tool_normalizer.native_lifecycle_deferred:
                                        _append_coalesced_stream_event(
                                            deferred_native_events,
                                            tool_event,
                                        )
                                        if deferred_queue_is_oversized():
                                            for release_event in release_deferred_queue():
                                                if isinstance(
                                                    release_event,
                                                    TextDeltaEvent,
                                                ):
                                                    visible_assistant_text_parts.append(
                                                        release_event.text
                                                    )
                                                yield release_event
                                    else:
                                        yield tool_event

                                # Gemini thought_signature (OpenAI compat format):
                                # tool_calls[].extra_content.google.thought_signature
                                sig = (
                                    (tc.get("extra_content") or {})
                                    .get("google", {})
                                    .get("thought_signature")
                                )
                                if isinstance(sig, str) and sig:
                                    tools_acc.set_metadata(idx, "thought_signature", sig)

                            if finish:
                                choice_terminal_seen = True
                                terminal_finish_reason = finish
                                terminal_native_finish_reason_present = (
                                    "native_finish_reason" in choice
                                )
                                terminal_native_finish_reason = choice.get(
                                    "native_finish_reason"
                                )

                    if malformed_stream_frames:
                        for pending_event in _segment_text_tool_events(
                            text_tool_normalizer.finish(
                                successful_text_tool_terminal=False,
                            ),
                            provider_kind=self._provider_kind,
                            model=self._model,
                        ):
                            if isinstance(pending_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(pending_event.text)
                            yield pending_event
                        for deferred_event in deferred_native_events:
                            yield deferred_event
                        deferred_native_events.clear()
                        for deferred_event in deferred_post_native_events:
                            if isinstance(deferred_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(deferred_event.text)
                            yield deferred_event
                        deferred_post_native_events.clear()
                        trace.record_error(
                            code="invalid_stream_frame",
                            message="Provider stream contained malformed data frames",
                            metadata={
                                "phase": "stream",
                                "cache_shape": cache_shape,
                                "malformed_frame_count": malformed_stream_frames,
                            },
                        )
                        yield ErrorEvent(
                            message=(
                                f"{self._compat.display_name} stream contained "
                                "a malformed data frame"
                            ),
                            code="invalid_stream_frame",
                        )
                        return

                    has_terminal_evidence = active_choice_seen and choice_terminal_seen
                    if not has_terminal_evidence:
                        if (
                            self._compat.empty_stream_fallback
                            and not active_choice_seen
                            and not emitted_stream_event
                            and not assistant_text_parts
                            and not tools_acc.has_calls
                            and not (
                                candidate_artifact is not None
                                and candidate_artifact.has_calls
                            )
                            and input_tokens == 0
                            and output_tokens == 0
                        ):
                            log.warning(
                                "openai.empty_stream_fallback_started",
                                provider=self._provider_kind,
                                model=self._model,
                            )
                            yield ProviderHeartbeatEvent(
                                phase="llm_fallback",
                                message=(
                                    "Provider returned an empty stream; retrying "
                                    "without streaming."
                                ),
                            )
                            empty_stream_exc = httpx.ReadTimeout("empty stream")
                            async for fallback_event in self._complete_non_stream(
                                payload=payload,
                                headers=headers,
                                cfg=cfg,
                                tools=tools,
                                timeout_exc=empty_stream_exc,
                            ):
                                yield fallback_event
                            return
                        for pending_event in _segment_text_tool_events(
                            text_tool_normalizer.finish(
                                successful_text_tool_terminal=False,
                            ),
                            provider_kind=self._provider_kind,
                            model=self._model,
                        ):
                            if isinstance(pending_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(pending_event.text)
                                yield pending_event
                        for deferred_event in deferred_native_events:
                            yield deferred_event
                        deferred_native_events.clear()
                        for deferred_event in deferred_post_native_events:
                            if isinstance(deferred_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(deferred_event.text)
                            yield deferred_event
                        deferred_post_native_events.clear()
                        trace.record_error(
                            code="incomplete_stream",
                            message="Provider stream ended without terminal evidence",
                            metadata={"phase": "stream", "cache_shape": cache_shape},
                        )
                        yield ErrorEvent(
                            message=(
                                f"{self._compat.display_name} stream ended before a "
                                "finish reason"
                            ),
                            code="incomplete_stream",
                        )
                        return

                    successful_text_tool_terminal = _successful_text_tool_terminal(
                        saw_done_sentinel=saw_done_sentinel,
                        finish_reasons=finish_reasons,
                    )
                    if not inert_candidate_output:
                        warn_for_unauthorized_plain_candidate(
                            "".join(assistant_text_parts),
                            tools,
                            dialects=text_tool_dialects,
                            provider_kind=self._provider_kind,
                            model=self._model,
                        )

                    if tools_acc.has_calls and not successful_text_tool_terminal:
                        for pending_event in _segment_text_tool_events(
                            text_tool_normalizer.finish(
                                successful_text_tool_terminal=False,
                            ),
                            provider_kind=self._provider_kind,
                            model=self._model,
                        ):
                            if isinstance(pending_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(pending_event.text)
                            yield pending_event
                        for deferred_event in deferred_native_events:
                            yield deferred_event
                        deferred_native_events.clear()
                        for deferred_event in deferred_post_native_events:
                            if isinstance(deferred_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(deferred_event.text)
                            yield deferred_event
                        deferred_post_native_events.clear()
                        trace.record_error(
                            code="incomplete_tool_call",
                            message=(
                                "Provider ended a native tool call with an "
                                f"unsuccessful finish reason: {stop_reason}"
                            ),
                            metadata={"phase": "stream", "cache_shape": cache_shape},
                        )
                        yield ErrorEvent(
                            message=(
                                f"{self._compat.display_name} ended a native tool call "
                                f"with finish reason {stop_reason!r}"
                            ),
                            code="incomplete_tool_call",
                        )
                        return

                    # Chat Completions has no per-call stop event: close every
                    # assembled call once the stream ends, running the
                    # provider-aware argument parser (including the DashScope
                    # JSON repair) over the accumulated raw fragments first.
                    native_calls: list[tuple[str, dict[str, Any]]] = []
                    pending_native_finishes: list[tuple[Any, dict[str, Any]]] = []
                    invalid_native_arguments = invalid_native_structure
                    for key, tool_use_id, tool_name, raw_arguments in (
                        tools_acc.pending_raw_arguments()
                    ):
                        args, arguments_valid, arguments_repaired = _parse_openai_tool_arguments(
                            provider_kind=self._provider_kind,
                            model=self._model,
                            tool_name=tool_name,
                            tool_use_id=tool_use_id,
                            raw_text=raw_arguments,
                            tools_by_name=tools_by_name,
                        )
                        trace_tool_calls.append(
                            {
                                "id": tool_use_id,
                                "name": tool_name,
                                "arguments_raw": raw_arguments,
                                "arguments_json_valid": arguments_valid,
                                "arguments_json_repaired": arguments_repaired,
                                "arguments": args,
                            }
                        )
                        tool_name_valid = bool(tool_name.strip())
                        if not tool_name_valid:
                            log.warning(
                                "provider.native_tool_call_invalid",
                                provider=self._provider_kind,
                                model=self._model,
                                tool_use_id=tool_use_id,
                                reason="missing_tool_name",
                            )
                        if not arguments_valid or not tool_name_valid:
                            invalid_native_arguments += 1
                            continue
                        native_calls.append((tool_name, args))
                        pending_native_finishes.append((key, args))

                    if invalid_native_arguments:
                        for event in _segment_text_tool_events(
                            text_tool_normalizer.finish(
                                successful_text_tool_terminal=False,
                            ),
                            provider_kind=self._provider_kind,
                            model=self._model,
                        ):
                            if isinstance(event, TextDeltaEvent):
                                visible_assistant_text_parts.append(event.text)
                            yield event
                        for deferred_event in deferred_native_events:
                            yield deferred_event
                        deferred_native_events.clear()
                        for deferred_event in deferred_post_native_events:
                            if isinstance(deferred_event, TextDeltaEvent):
                                visible_assistant_text_parts.append(deferred_event.text)
                            yield deferred_event
                        deferred_post_native_events.clear()
                        trace.record_error(
                            code="incomplete_tool_call",
                            message="Provider returned invalid native tool arguments",
                            metadata={
                                "phase": "stream",
                                "cache_shape": cache_shape,
                                "invalid_call_count": invalid_native_arguments,
                            },
                        )
                        yield ErrorEvent(
                            message=(
                                f"{self._compat.display_name} returned invalid "
                                "native tool arguments"
                            ),
                            code="incomplete_tool_call",
                        )
                        return

                    for key, args in pending_native_finishes:
                        for tool_event in tools_acc.finish_with_arguments(key, args):
                            emitted_stream_event = True
                            if text_tool_normalizer.native_lifecycle_deferred:
                                deferred_native_events.append(tool_event)
                            else:
                                yield tool_event

                    normalized_segments = text_tool_normalizer.finish(
                        successful_text_tool_terminal=successful_text_tool_terminal,
                        native_calls=native_calls,
                    )
                    for event in _segment_text_tool_events(
                        normalized_segments,
                        provider_kind=self._provider_kind,
                        model=self._model,
                    ):
                        emitted_stream_event = True
                        if isinstance(event, TextDeltaEvent):
                            visible_assistant_text_parts.append(event.text)
                        elif isinstance(event, ToolUseEndEvent):
                            trace_tool_calls.append(
                                {
                                    "id": event.tool_use_id,
                                    "name": event.tool_name,
                                    "arguments": event.arguments,
                                    "synthetic_from_text": True,
                                }
                            )
                        yield event

                    for deferred_event in deferred_native_events:
                        yield deferred_event
                    deferred_native_events.clear()
                    for deferred_event in deferred_post_native_events:
                        if isinstance(deferred_event, TextDeltaEvent):
                            visible_assistant_text_parts.append(deferred_event.text)
                        yield deferred_event
                    deferred_post_native_events.clear()

                    candidate_artifact_text = ""
                    if candidate_artifact is not None and candidate_artifact.has_calls:
                        if successful_text_tool_terminal:
                            for artifact_key in candidate_artifact_open_keys:
                                candidate_artifact.finish(artifact_key)
                        candidate_artifact_text = candidate_artifact.render_text()
                        if candidate_artifact_text:
                            visible_assistant_text_parts.append(candidate_artifact_text)
                        log.info(
                            "provider.candidate_artifact",
                            provider=self._provider_kind,
                            model=self._model,
                            call_count=candidate_artifact.call_count,
                            event_count=candidate_artifact.event_count,
                            char_count=candidate_artifact.char_count,
                            issue_codes=sorted(candidate_artifact.issue_codes),
                            truncated=False,
                        )

                    # Assemble reasoning from the structured fields already
                    # streamed in real time via ReasoningDeltaEvent.
                    reasoning_text = reasoning.finalize()

                    # Fallback: <think> tag extraction from accumulated text.
                    # This format embeds reasoning inside the answer text, so it
                    # can only be recovered after the full text arrives — it is
                    # inherently non-streamable and stays a turn-end assembly.
                    caps = cfg.model_capabilities
                    if not reasoning_text and caps and caps.reasoning_format == "think_tags":
                        full_text = "".join(assistant_text_parts)
                        reasoning_text = _extract_think_tags(full_text) or None

                    # Gemini thought_signature: extract from the first tool call
                    # that carries one (Gemini attaches it to the first FC only).
                    # Fallback: when Gemini streams the signature on a non-FC
                    # text delta (no tool_call carries it), use the streamed one.
                    gemini_thought_sig = cast(
                        "str | None",
                        tools_acc.first_metadata("thought_signature"),
                    )
                    if gemini_thought_sig is None:
                        gemini_thought_sig = streamed_thought_signature

                    if (
                        self._compat.empty_stream_fallback
                        and not emitted_stream_event
                        and not assistant_text_parts
                        and not tools_acc.has_calls
                        and not (
                            candidate_artifact is not None
                            and candidate_artifact.has_calls
                        )
                        and input_tokens == 0
                        and output_tokens == 0
                    ):
                        log.warning(
                            "openai.empty_stream_fallback_started",
                            provider=self._provider_kind,
                            model=self._model,
                        )
                        yield ProviderHeartbeatEvent(
                            phase="llm_fallback",
                            message=(
                                "Provider returned an empty stream; retrying "
                                "without streaming."
                            ),
                        )
                        empty_stream_exc = httpx.ReadTimeout("empty stream")
                        async for fallback_event in self._complete_non_stream(
                            payload=payload,
                            headers=headers,
                            cfg=cfg,
                            tools=tools,
                            timeout_exc=empty_stream_exc,
                        ):
                            yield fallback_event
                        return

                    billed_cost, cost_source, billing_receipt = _billing_result(
                        provider_kind=self._provider_kind,
                        base_url=self._base_url,
                        usage=usage_accumulator,
                        billing=billing_accumulator,
                        model=self._model,
                    )

                    trace.record_response(
                        usage={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "reasoning_tokens": reasoning_tokens,
                            "cached_tokens": cached_tokens,
                            "cache_write_tokens": cache_write_tokens,
                            "billed_cost": billed_cost,
                            "cost_source": cost_source,
                        },
                        stop_reason=stop_reason,
                        actual_model=actual_model,
                        assistant_text="".join(visible_assistant_text_parts),
                        reasoning_content=reasoning_text or None,
                        tool_calls=trace_tool_calls,
                        response_ids=sorted(response_ids),
                        metadata={"cache_shape": cache_shape},
                    )
                    if candidate_artifact_text:
                        yield TextDeltaEvent(text=candidate_artifact_text)
                    yield DoneEvent(
                        stop_reason=stop_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        reasoning_content=reasoning_text or None,
                        thinking_signature=gemini_thought_sig,
                        reasoning_tokens=reasoning_tokens,
                        cached_tokens=cached_tokens,
                        cache_write_tokens=cache_write_tokens,
                        billed_cost=billed_cost,
                        model=actual_model,
                        cost_source=cost_source,
                        provider=self.provider_id,
                        billing_receipt=billing_receipt,
                    )

        except httpx.TimeoutException as exc:
            safe_error = redact_upstream_error_text(
                f"Request timed out: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code="timeout",
                message=safe_error,
                metadata={"phase": "stream", "cache_shape": cache_shape},
            )
            if self._compat.stream_timeout_fallback and not emitted_stream_event:
                event_name = (
                    "openrouter.stream_timeout_fallback_started"
                    if self._provider_kind == "openrouter"
                    else "dashscope.non_stream_fallback_started"
                )
                log.warning(
                    event_name,
                    model=self._model,
                    timeout_seconds=cfg.timeout,
                    timeout_phase=type(exc).__name__,
                    error=safe_error,
                )
                yield ProviderHeartbeatEvent(
                    phase="llm_fallback",
                    message=(
                        f"{_provider_display_name(self._provider_kind)} stream timed out; "
                        "retrying without streaming."
                    ),
                )
                try:
                    async for fallback_event in self._complete_non_stream(
                        payload=payload,
                        headers=headers,
                        cfg=cfg,
                        tools=tools,
                        timeout_exc=exc,
                    ):
                        yield fallback_event
                except CandidateArtifactLimitError as fallback_exc:
                    log.warning(
                        "provider.candidate_artifact_limit",
                        provider=self._provider_kind,
                        model=self._model,
                        phase="non_stream_fallback",
                        operation=fallback_exc.operation,
                        reason=fallback_exc.reason,
                        limit=fallback_exc.limit,
                        observed=fallback_exc.observed,
                    )
                    yield ErrorEvent(
                        message="Candidate artifact exceeded bounded assembly limits",
                        code="candidate_artifact_limit_exceeded",
                    )
                except ToolStreamProtocolError as fallback_exc:
                    log.warning(
                        "provider.tool_stream_protocol_error",
                        provider=self._provider_kind,
                        model=self._model,
                        phase="non_stream_fallback",
                        operation=fallback_exc.operation,
                        reason=fallback_exc.reason,
                    )
                    yield ErrorEvent(
                        message="Provider returned an invalid tool lifecycle",
                        code="provider_protocol_error",
                    )
                except Exception as fallback_exc:  # noqa: BLE001 - see contract note below
                    fallback_error = redact_upstream_error_text(
                        f"Provider response handling failed: "
                        f"{str(fallback_exc) or repr(fallback_exc)}",
                        api_key=self._api_key,
                        max_len=2000,
                    )
                    log.error(
                        "provider.stream_internal_error",
                        provider=self._provider_kind,
                        model=self._model,
                        error=fallback_error,
                        exception_type=type(fallback_exc).__name__,
                    )
                    trace.record_error(code="provider_internal", message=fallback_error)
                    yield ErrorEvent(
                        message=fallback_error,
                        code="provider_internal",
                    )
                return
            for pending_event in _segment_text_tool_events(
                text_tool_normalizer.finish(successful_text_tool_terminal=False),
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(pending_event, TextDeltaEvent):
                    yield pending_event
            for deferred_event in deferred_native_events:
                yield deferred_event
            deferred_native_events.clear()
            for deferred_event in deferred_post_native_events:
                if isinstance(deferred_event, TextDeltaEvent):
                    visible_assistant_text_parts.append(deferred_event.text)
                yield deferred_event
            deferred_post_native_events.clear()
            yield ErrorEvent(message=safe_error, code="timeout")
        except httpx.RequestError as exc:
            safe_error = redact_upstream_error_text(
                f"Request error: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code="request_error",
                message=safe_error,
                metadata={"phase": "stream", "cache_shape": cache_shape},
            )
            for pending_event in _segment_text_tool_events(
                text_tool_normalizer.finish(successful_text_tool_terminal=False),
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(pending_event, TextDeltaEvent):
                    yield pending_event
            for deferred_event in deferred_native_events:
                yield deferred_event
            deferred_native_events.clear()
            for deferred_event in deferred_post_native_events:
                if isinstance(deferred_event, TextDeltaEvent):
                    visible_assistant_text_parts.append(deferred_event.text)
                yield deferred_event
            deferred_post_native_events.clear()
            yield ErrorEvent(message=safe_error, code="request_error")
        except CandidateArtifactLimitError as exc:
            message = "Candidate artifact exceeded bounded assembly limits"
            log.warning(
                "provider.candidate_artifact_limit",
                provider=self._provider_kind,
                model=self._model,
                phase="stream",
                operation=exc.operation,
                reason=exc.reason,
                limit=exc.limit,
                observed=exc.observed,
            )
            trace.record_error(
                code="candidate_artifact_limit_exceeded",
                message=message,
                metadata={
                    "phase": "stream",
                    "cache_shape": cache_shape,
                    "reason": exc.reason,
                    "limit": exc.limit,
                    "observed": exc.observed,
                },
            )
            deferred_native_events.clear()
            deferred_post_native_events.clear()
            yield ErrorEvent(
                message=message,
                code="candidate_artifact_limit_exceeded",
            )
        except ToolStreamProtocolError as exc:
            message = "Provider returned an invalid tool lifecycle"
            log.warning(
                "provider.tool_stream_protocol_error",
                provider=self._provider_kind,
                model=self._model,
                phase="stream",
                operation=exc.operation,
                reason=exc.reason,
            )
            trace.record_error(
                code="provider_protocol_error",
                message=message,
                metadata={
                    "phase": "stream",
                    "cache_shape": cache_shape,
                    "reason": exc.reason,
                },
            )
            for pending_event in _segment_text_tool_events(
                text_tool_normalizer.finish(successful_text_tool_terminal=False),
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(pending_event, TextDeltaEvent):
                    yield pending_event
            deferred_native_events.clear()
            deferred_post_native_events.clear()
            yield ErrorEvent(message=message, code="provider_protocol_error")
        except Exception as exc:  # noqa: BLE001 - chat() contract: ErrorEvent instead of raising
            safe_error = redact_upstream_error_text(
                f"Provider response handling failed: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            log.error(
                "provider.stream_internal_error",
                provider=self._provider_kind,
                model=self._model,
                error=safe_error,
                exception_type=type(exc).__name__,
            )
            trace.record_error(
                code="provider_internal",
                message=safe_error,
                metadata={"phase": "stream", "cache_shape": cache_shape},
            )
            for pending_event in _segment_text_tool_events(
                text_tool_normalizer.finish(successful_text_tool_terminal=False),
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(pending_event, TextDeltaEvent):
                    yield pending_event
            for deferred_event in deferred_native_events:
                yield deferred_event
            deferred_native_events.clear()
            for deferred_event in deferred_post_native_events:
                if isinstance(deferred_event, TextDeltaEvent):
                    visible_assistant_text_parts.append(deferred_event.text)
                yield deferred_event
            deferred_post_native_events.clear()
            yield ErrorEvent(
                message=safe_error,
                code="provider_internal",
            )

    async def _complete_non_stream(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        cfg: ChatConfig,
        tools: list[ToolDefinition] | None,
        timeout_exc: httpx.TimeoutException,
    ) -> AsyncIterator[StreamEvent]:
        fallback_payload = dict(payload)
        fallback_payload["stream"] = False
        fallback_payload.pop("stream_options", None)
        cache_shape = _payload_cache_shape(fallback_payload, tools=tools)
        fallback_headers = dict(headers)
        fallback_headers["Accept"] = "application/json"
        endpoint = self._api_url("/v1/chat/completions")
        trace = LLMTraceRecorder(
            provider=self._provider_kind,
            model=self._model,
            base_url=self._base_url,
            endpoint=endpoint,
            stream=False,
        )
        trace.record_request(
            payload=fallback_payload,
            headers=fallback_headers,
            metadata={
                "cache_shape": cache_shape,
                "timeout_seconds": cfg.timeout,
                "tools_count": len(tools or []),
                "fallback_from": "stream_timeout",
                "stream_error": redact_upstream_error_text(
                    str(timeout_exc) or repr(timeout_exc),
                    api_key=self._api_key,
                    max_len=2000,
                ),
            },
        )

        try:
            async with httpx.AsyncClient(
                timeout=cfg.timeout,
                trust_env=_trust_env(),
                proxy=self._proxy,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    endpoint,
                    headers=fallback_headers,
                    json=fallback_payload,
                )
        except httpx.TimeoutException:
            safe_error = redact_upstream_error_text(
                f"Request timed out: {str(timeout_exc) or repr(timeout_exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            log.warning(
                "openrouter.non_stream_fallback_timeout",
                model=self._model,
                timeout_seconds=cfg.timeout,
                stream_error=safe_error,
            )
            trace.record_error(
                code="timeout",
                message=safe_error,
                metadata={"phase": "non_stream_fallback", "cache_shape": cache_shape},
            )
            yield ErrorEvent(message=safe_error, code="timeout")
            return
        except httpx.RequestError as exc:
            safe_error = redact_upstream_error_text(
                f"Request error: {str(exc) or repr(exc)}",
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code="request_error",
                message=safe_error,
                metadata={"phase": "non_stream_fallback", "cache_shape": cache_shape},
            )
            yield ErrorEvent(message=safe_error, code="request_error")
            return

        if response.status_code != 200:
            safe_response_body = redact_upstream_error_text(
                response.text,
                api_key=self._api_key,
                max_len=4000,
            )
            safe_message = redact_upstream_error_text(
                _format_chat_http_error(
                    self._compat.display_name,
                    response.status_code,
                    response.text,
                ),
                api_key=self._api_key,
                max_len=2000,
            )
            trace.record_error(
                code=str(response.status_code),
                message=safe_message,
                status_code=response.status_code,
                response_body=safe_response_body,
                metadata={"cache_shape": cache_shape},
            )
            yield ErrorEvent(
                message=safe_message,
                code=str(response.status_code),
                retry_after_s=retry_after_from_headers(
                    response.status_code,
                    getattr(response, "headers", None),
                ),
            )
            return

        try:
            data = response.json()
        except json.JSONDecodeError:
            safe_response_body = redact_upstream_error_text(
                response.text,
                api_key=self._api_key,
                max_len=4000,
            )
            trace.record_error(
                code="invalid_json",
                message="Invalid JSON response from provider",
                response_body=safe_response_body,
                metadata={"cache_shape": cache_shape},
            )
            yield ErrorEvent(message="Invalid JSON response from provider", code="invalid_json")
            return

        if not isinstance(data, dict):
            yield ErrorEvent(
                message="Provider returned an invalid response object",
                code="invalid_response",
            )
            return
        if "error" in data and data["error"] is not None:
            top_level_error = data["error"]
            error_message = (
                str(top_level_error.get("message") or "provider error response")
                if isinstance(top_level_error, Mapping)
                else str(top_level_error).strip() or "provider error response"
            )
            error_message = redact_upstream_error_text(
                error_message,
                api_key=self._api_key,
                max_len=2000,
            )
            error_code = (
                str(top_level_error.get("code") or "response_error")
                if isinstance(top_level_error, Mapping)
                else "response_error"
            )
            error_code = redact_upstream_error_code(
                error_code,
                api_key=self._api_key,
            )
            trace.record_error(
                code=error_code,
                message=error_message,
                response_body=redact_upstream_error_text(
                    response.text,
                    api_key=self._api_key,
                    max_len=4000,
                ),
                metadata={"cache_shape": cache_shape},
            )
            yield ErrorEvent(message=error_message, code=error_code)
            return
        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            yield ErrorEvent(
                message="Provider returned an invalid choice batch",
                code="invalid_response",
            )
            return
        choice = choices[0]
        if not isinstance(choice, Mapping):
            yield ErrorEvent(
                message="Provider returned a malformed choice",
                code="invalid_response",
            )
            return
        choice_index = choice.get("index", 0)
        finish_reason = choice.get("finish_reason")
        message = choice.get("message")
        if (
            not isinstance(choice_index, int)
            or isinstance(choice_index, bool)
            or choice_index != 0
            or (
                finish_reason is not None
                and (
                    not isinstance(finish_reason, str)
                    or not finish_reason.strip()
                )
            )
            or not isinstance(message, Mapping)
        ):
            yield ErrorEvent(
                message="Provider returned an invalid choice terminal",
                code="invalid_response",
            )
            return

        actual_model = data.get("model") or self._model
        usage_accumulator = _UsageSnapshotAccumulator()
        usage_payload = data.get("usage")
        if isinstance(usage_payload, Mapping):
            usage_accumulator.update(usage_payload)
        (
            input_tokens,
            output_tokens,
            reasoning_tokens,
            cached_tokens,
            cache_write_tokens,
            _,
        ) = usage_accumulator.fields()
        billing_accumulator = _ProviderBillingAccumulator()
        billing_accumulator.update(
            self._provider_kind,
            _exact_provider_billing_payload(
                self._provider_kind,
                data,
                str(getattr(response, "text", "") or ""),
            ),
        )
        billed_cost, cost_source, billing_receipt = _billing_result(
            provider_kind=self._provider_kind,
            base_url=self._base_url,
            usage=usage_accumulator,
            billing=billing_accumulator,
            model=self._model,
        )
        _log_provider_cache_usage(
            provider_kind=self._provider_kind,
            model=self._model,
            actual_model=actual_model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_shape=cache_shape,
        )
        stop_reason = "stop"
        assistant_text_parts: list[str] = []
        visible_assistant_text_parts: list[str] = []
        reasoning = ReasoningAccumulator()
        inert_candidate_output = cfg.candidate_output_mode == "inert_artifact"
        candidate_artifact = (
            CandidateArtifactBuilder() if inert_candidate_output else None
        )
        tools_acc = ToolStreamAccumulator()
        trace_tool_calls: list[dict[str, Any]] = []
        tools_by_name = _tool_by_name(tools)
        finish_reasons: list[str] = []
        text_tool_dialects = self._compat.text_tool_profile.dialects_for_model(self._model)
        text_tool_normalizer: (
            TextToolStreamNormalizer | _InertCandidateTextPassthrough
        )
        if inert_candidate_output:
            text_tool_normalizer = _InertCandidateTextPassthrough()
        else:
            text_tool_normalizer = TextToolStreamNormalizer(
                tools=tools,
                dialects=text_tool_dialects,
                provider_kind=self._provider_kind,
                model=self._model,
            )
        native_calls: list[tuple[str, dict[str, Any]]] = []
        pending_native_finishes: list[tuple[Any, dict[str, Any]]] = []
        deferred_native_events = _DeferredStreamEventBuffer()
        invalid_native_arguments = 0

        for choice in choices:
            if choice.get("finish_reason"):
                stop_reason = choice["finish_reason"]
                finish_reasons.append(str(choice["finish_reason"]))
            message = choice.get("message") or {}

            text = message.get("content")
            if isinstance(text, str) and text:
                assistant_text_parts.append(text)
                for visible_text in text_tool_normalizer.push(text):
                    visible_assistant_text_parts.append(visible_text)
                    yield TextDeltaEvent(text=visible_text)

            reasoning_details = message.get("reasoning_details")
            if reasoning_details:
                for detail in reasoning_details:
                    if isinstance(detail, dict):
                        reasoning_event = reasoning.emit(detail.get("text", ""))
                        if reasoning_event is not None:
                            yield reasoning_event
            for key in ("reasoning_content", "reasoning"):
                reasoning_str = message.get(key)
                if isinstance(reasoning_str, str):
                    reasoning_event = reasoning.emit(reasoning_str)
                    if reasoning_event is not None:
                        yield reasoning_event

            raw_tool_calls = message.get("tool_calls") or []
            if not isinstance(raw_tool_calls, list):
                if inert_candidate_output:
                    assert candidate_artifact is not None
                    candidate_artifact.observe_call(
                        ("invalid_tool_calls", candidate_artifact.call_count),
                        arguments=strip_candidate_tool_identity(raw_tool_calls),
                    )
                else:
                    invalid_native_arguments += 1
                    log.warning(
                        "provider.native_tool_call_invalid",
                        provider=self._provider_kind,
                        model=self._model,
                        reason="tool_calls_not_array",
                    )
                raw_tool_calls = []
            for call_position, tc in enumerate(raw_tool_calls):
                if not isinstance(tc, Mapping):
                    if inert_candidate_output:
                        assert candidate_artifact is not None
                        candidate_artifact.observe_call(
                            ("invalid_tool_call", call_position),
                            arguments=strip_candidate_tool_identity(tc),
                        )
                    else:
                        invalid_native_arguments += 1
                        log.warning(
                            "provider.native_tool_call_invalid",
                            provider=self._provider_kind,
                            model=self._model,
                            reason="tool_call_not_object",
                        )
                    continue
                if inert_candidate_output:
                    assert candidate_artifact is not None
                    raw_function = tc.get("function")
                    if isinstance(raw_function, Mapping):
                        raw_name = raw_function.get("name")
                        raw_arguments = raw_function.get("arguments")
                    else:
                        raw_name = None
                        raw_arguments = (
                            strip_candidate_tool_identity(raw_function)
                            if _candidate_fragment_has_content(raw_function)
                            else None
                        )
                    if (
                        not _candidate_fragment_has_content(raw_name)
                        and not _candidate_fragment_has_content(raw_arguments)
                    ):
                        malformed_wrapper = _candidate_malformed_tool_wrapper(tc)
                        if malformed_wrapper is not None:
                            raw_arguments = malformed_wrapper
                    candidate_artifact.observe_call(
                        ("tool_call", call_position),
                        name_text=raw_name,
                        arguments=raw_arguments,
                    )
                    continue
                raw_function = tc.get("function") or {}
                if not isinstance(raw_function, Mapping):
                    invalid_native_arguments += 1
                    log.warning(
                        "provider.native_tool_call_invalid",
                        provider=self._provider_kind,
                        model=self._model,
                        reason="function_not_object",
                    )
                    raw_function = {}
                function = raw_function
                raw_tool_use_id = tc.get("id")
                tool_use_id = (
                    raw_tool_use_id
                    if isinstance(raw_tool_use_id, str) and raw_tool_use_id
                    else f"call_{uuid4().hex[:12]}"
                )
                raw_tool_name = function.get("name")
                tool_name = raw_tool_name if isinstance(raw_tool_name, str) else ""
                tool_name_valid = bool(tool_name.strip())
                call_key = tools_acc.next_int_key()
                for pending_event in _segment_text_tool_events(
                    text_tool_normalizer.observe_native_tool_start(tool_name),
                    provider_kind=self._provider_kind,
                    model=self._model,
                ):
                    if isinstance(pending_event, TextDeltaEvent):
                        visible_assistant_text_parts.append(pending_event.text)
                        yield pending_event
                for tool_event in tools_acc.start(
                    call_key,
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                ):
                    if not tool_name_valid:
                        continue
                    deferred_native_events.append(tool_event)
                raw_arguments_text = function.get("arguments")
                if raw_arguments_text is None:
                    arguments_text = ""
                elif isinstance(raw_arguments_text, str):
                    arguments_text = raw_arguments_text
                else:
                    invalid_native_arguments += 1
                    log.warning(
                        "provider.native_tool_call_invalid",
                        provider=self._provider_kind,
                        model=self._model,
                        tool_use_id=tool_use_id,
                        reason="arguments_not_string",
                    )
                    arguments_text = ""
                if arguments_text:
                    for tool_event in tools_acc.append(call_key, arguments_text):
                        if not tool_name_valid:
                            continue
                        deferred_native_events.append(tool_event)
                sig = (tc.get("extra_content") or {}).get("google", {}).get("thought_signature")
                if isinstance(sig, str) and sig:
                    tools_acc.set_metadata(call_key, "thought_signature", sig)
                arguments, arguments_valid, arguments_repaired = _parse_openai_tool_arguments(
                    provider_kind=self._provider_kind,
                    model=self._model,
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    raw_text=arguments_text,
                    tools_by_name=tools_by_name,
                )
                trace_tool_calls.append(
                    {
                        "id": tool_use_id,
                        "name": tool_name,
                        "arguments_raw": arguments_text,
                        "arguments_json_valid": arguments_valid,
                        "arguments_json_repaired": arguments_repaired,
                        "arguments": arguments,
                    }
                )
                if not tool_name_valid:
                    log.warning(
                        "provider.native_tool_call_invalid",
                        provider=self._provider_kind,
                        model=self._model,
                        tool_use_id=tool_use_id,
                        reason="missing_tool_name",
                    )
                if arguments_valid and tool_name_valid:
                    native_calls.append((tool_name, arguments))
                    pending_native_finishes.append((call_key, arguments))
                else:
                    invalid_native_arguments += 1

        if not inert_candidate_output:
            warn_for_unauthorized_plain_candidate(
                "".join(assistant_text_parts),
                tools,
                dialects=text_tool_dialects,
                provider_kind=self._provider_kind,
                model=self._model,
            )
        successful_text_tool_terminal = _successful_text_tool_terminal(
            saw_done_sentinel=False,
            finish_reasons=finish_reasons,
        )
        if not finish_reasons:
            for event in _segment_text_tool_events(
                text_tool_normalizer.finish(
                    successful_text_tool_terminal=False,
                ),
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(event, TextDeltaEvent):
                    visible_assistant_text_parts.append(event.text)
                yield event
            yield ErrorEvent(
                message=(
                    f"{self._compat.display_name} response ended without a finish reason"
                ),
                code="incomplete_stream",
            )
            return
        if (
            deferred_native_events.event_count
            + tools_acc.pending_unemitted_event_count
            + text_tool_normalizer.held_event_count
            > _MAX_DEFERRED_NATIVE_EVENTS
            or deferred_native_events.char_count
            + tools_acc.pending_unemitted_char_count
            + text_tool_normalizer.held_chars
            > _MAX_DEFERRED_NATIVE_ARGUMENT_CHARS
        ):
            invalid_native_arguments += 1
            log.warning(
                "provider.deferred_native_queue_oversized",
                provider=self._provider_kind,
                model=self._model,
                max_events=_MAX_DEFERRED_NATIVE_EVENTS,
                max_argument_chars=_MAX_DEFERRED_NATIVE_ARGUMENT_CHARS,
            )
        if tools_acc.has_calls and not successful_text_tool_terminal:
            normalized_segments = text_tool_normalizer.finish(
                successful_text_tool_terminal=False,
            )
            for event in _segment_text_tool_events(
                normalized_segments,
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(event, TextDeltaEvent):
                    visible_assistant_text_parts.append(event.text)
                yield event
            trace.record_error(
                code="incomplete_tool_call",
                message=(
                    "Provider ended a native tool call with an unsuccessful "
                    f"finish reason: {stop_reason}"
                ),
                metadata={"phase": "non_stream", "cache_shape": cache_shape},
            )
            yield ErrorEvent(
                message=(
                    f"{self._compat.display_name} ended a native tool call with "
                    f"finish reason {stop_reason!r}"
                ),
                code="incomplete_tool_call",
            )
            return

        if invalid_native_arguments:
            normalized_segments = text_tool_normalizer.finish(
                successful_text_tool_terminal=False,
            )
            for event in _segment_text_tool_events(
                normalized_segments,
                provider_kind=self._provider_kind,
                model=self._model,
            ):
                if isinstance(event, TextDeltaEvent):
                    visible_assistant_text_parts.append(event.text)
                yield event
            trace.record_error(
                code="incomplete_tool_call",
                message="Provider returned invalid native tool arguments",
                metadata={
                    "phase": "non_stream",
                    "cache_shape": cache_shape,
                    "invalid_call_count": invalid_native_arguments,
                },
            )
            yield ErrorEvent(
                message=(
                    f"{self._compat.display_name} returned invalid native tool arguments"
                ),
                code="incomplete_tool_call",
            )
            return

        for call_key, arguments in pending_native_finishes:
            for tool_event in tools_acc.finish_with_arguments(call_key, arguments):
                deferred_native_events.append(tool_event)

        normalized_segments = text_tool_normalizer.finish(
            successful_text_tool_terminal=successful_text_tool_terminal,
            native_calls=native_calls,
        )
        for event in _segment_text_tool_events(
            normalized_segments,
            provider_kind=self._provider_kind,
            model=self._model,
        ):
            if isinstance(event, TextDeltaEvent):
                visible_assistant_text_parts.append(event.text)
            elif isinstance(event, ToolUseEndEvent):
                trace_tool_calls.append(
                    {
                        "id": event.tool_use_id,
                        "name": event.tool_name,
                        "arguments": event.arguments,
                        "synthetic_from_text": True,
                    }
                )
            yield event

        for deferred_event in deferred_native_events:
            yield deferred_event

        candidate_artifact_text = ""
        if candidate_artifact is not None and candidate_artifact.has_calls:
            candidate_artifact_text = candidate_artifact.render_text()
            if candidate_artifact_text:
                visible_assistant_text_parts.append(candidate_artifact_text)
            log.info(
                "provider.candidate_artifact",
                provider=self._provider_kind,
                model=self._model,
                call_count=candidate_artifact.call_count,
                event_count=candidate_artifact.event_count,
                char_count=candidate_artifact.char_count,
                issue_codes=sorted(candidate_artifact.issue_codes),
                truncated=False,
            )

        reasoning_text = reasoning.finalize()
        if (
            not reasoning_text
            and cfg.model_capabilities
            and cfg.model_capabilities.reasoning_format == "think_tags"
        ):
            reasoning_text = _extract_think_tags("".join(assistant_text_parts)) or None

        trace.record_response(
            response=data,
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
                "billed_cost": billed_cost,
                "cost_source": cost_source,
            },
            stop_reason=stop_reason,
            actual_model=actual_model,
            assistant_text="".join(visible_assistant_text_parts),
            reasoning_content=reasoning_text or None,
            tool_calls=trace_tool_calls,
            response_ids=[str(data["id"])] if data.get("id") else [],
            metadata={"cache_shape": cache_shape},
        )
        if candidate_artifact_text:
            yield TextDeltaEvent(text=candidate_artifact_text)
        yield DoneEvent(
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_content=reasoning_text or None,
            thinking_signature=cast(
                "str | None",
                tools_acc.first_metadata("thought_signature"),
            ),
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            billed_cost=billed_cost,
            model=actual_model,
            cost_source=cost_source,
            provider=self.provider_id,
            billing_receipt=billing_receipt,
        )

    async def list_models(self, *, raise_on_error: bool = False) -> list[ModelInfo]:
        """List available models.

        By default any auth/transport failure degrades to an empty list (the
        historical contract every runtime caller relies on). Pass
        ``raise_on_error=True`` to surface the underlying exception instead,
        so callers that must distinguish a wrong key from an empty catalog
        (e.g. onboarding discovery) can classify it.
        """
        headers = {"Authorization": f"Bearer {self._api_key}"}
        headers.update(provider_app_headers(self._base_url))
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                trust_env=_trust_env(),
                proxy=self._proxy,
            ) as client:
                resp = await client.get(self._api_url("/v1/models"), headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return [
                    ModelInfo(
                        provider=self._provider_kind,
                        model_id=m["id"],
                        display_name=m.get("name", m.get("id", "")),
                        context_window=m.get("context_length", 0),
                        max_output_tokens=(m.get("top_provider") or {}).get("max_completion_tokens")
                        or 0,
                    )
                    for m in data.get("data", [])
                ]
        except httpx.HTTPError as exc:
            if raise_on_error:
                raise redacted_httpx_error(exc, api_key=self._api_key) from None
            return []
        except Exception:
            if raise_on_error:
                raise
            return []
