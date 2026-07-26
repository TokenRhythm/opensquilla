"""Harness for golden request-proof tests.

Freezes the exact outbound request JSON each provider adapter builds today so
that refactors of the request builders (reasoning-toggle ladder, compat
model-id sets) are provably behavior-preserving. Adapters are driven fully
offline through ``httpx.MockTransport``; providers are constructed through
``selector._build_provider`` with a synthetic ``ProviderConfig`` so per-kind
constructor wiring matches production.

Golden files live under ``requests/<backend>/<kind-or-provider>__<case>.json``
and store method, url, a redacted auth-style marker, content type, and the
parsed JSON body. Regenerate with ``OPENSQUILLA_REGEN_GOLDENS=1``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.engine.types import ThinkingLevel
from opensquilla.provider.compat_policy import known_policy_kinds
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.registry import get_provider_spec, list_provider_specs
from opensquilla.provider.selector import ProviderConfig, _build_provider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)

GOLDEN_ROOT = Path(__file__).resolve().parent / "requests"
REGEN_ENV = "OPENSQUILLA_REGEN_GOLDENS"

# Fake credential only — must never appear in a golden file.
FAKE_API_KEY = "sk-test-000"

# For provider kinds registered without a default base URL (azure).
SYNTHETIC_BASE_URL = "http://127.0.0.1:9/v1"

_SYSTEM_PROMPT = "You are a synthetic assistant used to freeze provider request payloads."
_NEUTRAL_MODEL = "test-chat-model"

# kind -> (model id, reasoning dialect resolved when thinking is requested).
# Model ids are derived from the code paths that trigger each dialect —
# ModelCatalog.get_capabilities prefix ladders and compat_policy model-id
# sets — never invented. "none" kinds freeze the omission: a thinking
# request must serialize with no reasoning field at all.
COMPAT_THINKING_MODELS: dict[str, tuple[str, str]] = {
    # api.openai.com host + gpt-5 prefix ladder (model_catalog) -> "openai";
    # gpt-5.4 is also in max_completion_tokens and omit-temperature-when-
    # thinking prefixes (compat_policy), both frozen by these goldens.
    "openai": ("gpt-5.4", "openai"),
    # In _OPENROUTER_DISABLE_REASONING_MODELS (compat_policy); the seeded
    # catalog entry marks it reasoning-capable -> "openrouter" format, so
    # thinking-off freezes reasoning={"enabled": false} too.
    "openrouter": ("z-ai/glm-5", "openrouter"),
    "azure": (_NEUTRAL_MODEL, "none"),
    # _DEEPSEEK_V4_MODEL_IDS: thinking-toggle + require-reasoning_content
    # sets (compat_policy); deepseek reasoning_shape -> "deepseek".
    "deepseek": ("deepseek-v4-flash", "deepseek"),
    # gemini-3 prefix ladder (model_catalog) -> "gemini".
    "gemini": ("gemini-3.5-flash", "gemini"),
    # qwen3 prefix ladder (model_catalog dashscope branch) -> "dashscope".
    "dashscope": ("qwen3-coder-plus", "dashscope"),
    "bailian_coding": (_NEUTRAL_MODEL, "none"),
    # kimi-k2.7 ladder (model_catalog moonshot branch) -> "moonshot"; also in
    # fixed_sampling_model_prefixes so the non-default temperature is dropped.
    "moonshot": ("kimi-k2.7-code", "moonshot"),
    "minimax": (_NEUTRAL_MODEL, "none"),
    "mimo": (_NEUTRAL_MODEL, "none"),
    "mistral": (_NEUTRAL_MODEL, "none"),
    "groq": (_NEUTRAL_MODEL, "none"),
    # glm-5 prefix in the zai ladder (model_catalog) -> "zai".
    "zhipu": ("glm-5", "zai"),
    "qianfan": (_NEUTRAL_MODEL, "none"),
    "siliconflow": (_NEUTRAL_MODEL, "none"),
    "aihubmix": (_NEUTRAL_MODEL, "none"),
    # doubao-seed-1-6 ladder (model_catalog volcengine branch) ->
    # "volcengine"; the ark policy also strips bounded-schema keywords.
    "volcengine": ("doubao-seed-1-6", "volcengine"),
    # seed-1-6 ladder (model_catalog byteplus branch) -> shared "volcengine"
    # dialect; ark schema keyword strips apply here too.
    "byteplus": ("seed-1-6", "volcengine"),
    # hy3 ladder ([tencent_tokenhub."hy3*"] corrections rows) ->
    # "tencent_tokenhub" dialect; the policy also requires assistant
    # reasoning_content replay for the hy3 ids, frozen by the tools golden.
    "tencent_tokenhub": ("hy3", "tencent_tokenhub"),
    # [tokenrhythm."deepseek-v4-flash"] corrections row pins
    # reasoning_format="none": the relay streams reasoning_content on its
    # own but 400s on any thinking payload, so the thinking golden freezes
    # the omission. The id is also in require_reasoning_content_model_ids,
    # so the tools golden freezes assistant reasoning_content replay.
    "tokenrhythm": ("deepseek-v4-flash", "none"),
    "lm_studio": (_NEUTRAL_MODEL, "none"),
    "ovms": (_NEUTRAL_MODEL, "none"),
    "litellm_proxy": (_NEUTRAL_MODEL, "none"),
}

# Synthetic OpenRouter catalog row fed through ModelCatalog's production
# parser so the openrouter capability path resolves exactly as it does after
# a live /models fetch.
_OPENROUTER_SEED_MODEL: dict[str, Any] = {
    "id": "z-ai/glm-5",
    "name": "Synthetic reasoning model",
    "context_length": 128000,
    "top_provider": {"max_completion_tokens": 32768},
    "supported_parameters": ["reasoning", "tools"],
    "architecture": {"input_modalities": ["text"]},
    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
}

_SCRUBBED_ENV_KEYS = (
    "OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS",
    "OPENSQUILLA_LLM_PROXY",
    "OPENSQUILLA_TRACE_ROUTING",
    "OPENSQUILLA_LLM_STREAM_CONNECT_TIMEOUT_SECONDS",
    "OPENSQUILLA_LLM_STREAM_WRITE_TIMEOUT_SECONDS",
)


@dataclass(frozen=True)
class GoldenCase:
    """One frozen request scenario for one provider adapter."""

    backend: str
    slug: str
    provider_id: str
    model: str
    kind: str = ""
    base_url: str = ""
    api_key: str = FAKE_API_KEY
    with_tools: bool = False
    thinking: bool = False
    expected_dialect: str = ""
    thinking_budget_tokens: int = 5000
    temperature: float | None = 0.7
    with_cache_breakpoints: bool = False
    seed_openrouter_catalog: bool = False
    tool_history: bool = False

    @property
    def case_id(self) -> str:
        return f"{self.backend}-{self.slug}"

    @property
    def golden_path(self) -> Path:
        return GOLDEN_ROOT / self.backend / f"{self.slug}.json"


def _compat_provider_id(kind: str) -> str:
    """Pick one registered, runtime-supported provider id for a compat kind."""
    specs = [
        spec
        for spec in list_provider_specs()
        if spec.backend == "openai_compat"
        and spec.provider_kind == kind
        and spec.runtime_supported
    ]
    if not specs:
        raise AssertionError(f"No registered openai_compat provider for kind {kind!r}")
    for spec in specs:
        if spec.provider_id == kind:
            return spec.provider_id
    return specs[0].provider_id


def build_cases() -> list[GoldenCase]:
    """The full golden matrix. openai_codex is excluded (OAuth, out of scope)."""
    cases: list[GoldenCase] = []

    for kind in sorted(known_policy_kinds()):
        model, dialect = COMPAT_THINKING_MODELS[kind]
        provider_id = _compat_provider_id(kind)
        spec = get_provider_spec(provider_id)
        base_url = "" if spec.default_base_url else SYNTHETIC_BASE_URL
        common: dict[str, Any] = {
            "backend": "openai_compat",
            "provider_id": provider_id,
            "model": model,
            "kind": kind,
            "base_url": base_url,
            "expected_dialect": dialect,
            "seed_openrouter_catalog": kind == "openrouter",
        }
        cases.append(GoldenCase(slug=f"{kind}__plain", **common))
        cases.append(
            GoldenCase(slug=f"{kind}__tools", with_tools=True, tool_history=True, **common)
        )
        cases.append(GoldenCase(slug=f"{kind}__thinking", thinking=True, **common))

    cases.extend(
        [
            GoldenCase(
                backend="anthropic",
                slug="anthropic__plain",
                provider_id="anthropic",
                model="claude-sonnet-4-6",
            ),
            GoldenCase(
                backend="anthropic",
                slug="anthropic__tools",
                provider_id="anthropic",
                model="claude-sonnet-4-6",
                with_tools=True,
                tool_history=True,
            ),
            # Non-adaptive SKU: freezes the budget_tokens branch plus the
            # max_tokens bump when the budget exceeds max_tokens.
            GoldenCase(
                backend="anthropic",
                slug="anthropic__thinking_budget",
                provider_id="anthropic",
                model="claude-haiku-4-5-20251001",
                thinking=True,
                thinking_budget_tokens=20000,
            ),
            # Adaptive SKU: freezes the {"type": "adaptive"} branch.
            GoldenCase(
                backend="anthropic",
                slug="anthropic__thinking_adaptive",
                provider_id="anthropic",
                model="claude-sonnet-4-6",
                thinking=True,
            ),
            GoldenCase(
                backend="anthropic",
                slug="anthropic__cache_breakpoints",
                provider_id="anthropic",
                model="claude-sonnet-4-6",
                with_cache_breakpoints=True,
            ),
            # Anthropic-shaped MiniMax endpoint: freezes bearer-auth style and
            # the /anthropic/v1/messages URL join.
            GoldenCase(
                backend="anthropic",
                slug="minimax__plain",
                provider_id="minimax",
                model="minimax-m2.5",
            ),
            # Anthropic-shaped Tencent TokenHub endpoint: freezes x-api-key
            # auth on a non-Anthropic host and the bare-host /v1/messages
            # URL join.
            GoldenCase(
                backend="anthropic",
                slug="tencent_tokenhub_anthropic__plain",
                provider_id="tencent_tokenhub_anthropic",
                model="hy3",
            ),
            # Token Plan Anthropic endpoint: freezes bearer auth and the
            # /plan/anthropic/v1/messages URL join on the lkeap host.
            GoldenCase(
                backend="anthropic",
                slug="tencent_token_plan_anthropic__plain",
                provider_id="tencent_token_plan_anthropic",
                model="hy3",
            ),
        ]
    )

    responses_common: dict[str, Any] = {
        "backend": "openai_responses",
        "provider_id": "openai_responses",
        "model": "gpt-5.4",
    }
    cases.extend(
        [
            GoldenCase(slug="openai_responses__plain", **responses_common),
            GoldenCase(
                slug="openai_responses__tools",
                with_tools=True,
                tool_history=True,
                **responses_common,
            ),
            # The Responses adapter currently ignores thinking flags: the
            # golden freezes that omission as the behavior.
            GoldenCase(
                slug="openai_responses__reasoning_level",
                thinking=True,
                **responses_common,
            ),
        ]
    )

    ollama_common: dict[str, Any] = {
        "backend": "ollama",
        "provider_id": "ollama",
        "model": "llama3",
        "api_key": "",
    }
    cases.extend(
        [
            GoldenCase(slug="ollama__plain", **ollama_common),
            GoldenCase(
                slug="ollama__tools",
                with_tools=True,
                tool_history=True,
                **ollama_common,
            ),
            # No temperature: freezes the bare options shape with the
            # num_ctx default visible in the payload.
            GoldenCase(slug="ollama__num_ctx_default", temperature=None, **ollama_common),
        ]
    )

    return cases


def _plain_messages() -> list[Message]:
    """Synthetic multi-turn history.

    The first assistant message carries reasoning_content (frozen replay for
    dialects that echo it), the second carries none (frozen empty-string
    injection for kinds that require reasoning_content on every assistant
    message).
    """
    return [
        Message(role="user", content="What is the capital of Australia?"),
        Message(
            role="assistant",
            content="Canberra.",
            reasoning_content="The capital is Canberra, not Sydney.",
        ),
        Message(role="user", content="Repeat the answer as a single word."),
        Message(role="assistant", content="Canberra"),
        Message(role="user", content="Now write it in uppercase."),
    ]


def _tool_history_messages() -> list[Message]:
    """A tool round-trip: assistant tool_use followed by a tool_result."""
    return [
        Message(role="user", content="List the files in the workspace root."),
        Message(
            role="assistant",
            content=[
                ContentBlockText(text="I will list the files."),
                ContentBlockToolUse(
                    id="call_001",
                    name="list_files",
                    input={"path": ".", "max_entries": 3},
                ),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_001",
                    content="README.md\nsrc\ntests",
                )
            ],
        ),
    ]


def _golden_tool() -> ToolDefinition:
    """One synthetic tool whose schema carries strip-sensitive keywords.

    minLength/maxLength/minItems/maxItems are in the ark strip set, so
    volcengine/byteplus goldens diverge; additionalProperties/default are in
    no strip set today, so removing them anywhere becomes a golden diff.
    """
    return ToolDefinition(
        name="list_files",
        description="List files under a workspace-relative path.",
        input_schema=ToolInputSchema(
            properties={
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory to list.",
                    "default": ".",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "max_entries": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                },
                "filters": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 5,
                    "default": [],
                },
                "options": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "include_hidden": {"type": "boolean", "default": False},
                    },
                },
            },
            required=["path"],
        ),
    )


def _compat_capabilities(case: GoldenCase) -> ModelCapabilities:
    """Resolve capabilities the way production does: through ModelCatalog."""
    catalog = ModelCatalog()
    if case.seed_openrouter_catalog:
        catalog._populate_from_data([dict(_OPENROUTER_SEED_MODEL)])
    spec = get_provider_spec(case.provider_id)
    base_url = case.base_url or spec.default_base_url
    caps = catalog.get_capabilities(
        case.model, provider_name=case.provider_id, base_url=base_url
    )
    assert caps.reasoning_format == case.expected_dialect, (
        f"{case.kind}: model {case.model!r} resolved reasoning_format "
        f"{caps.reasoning_format!r}, expected {case.expected_dialect!r} — the "
        "capability ladder moved; update COMPAT_THINKING_MODELS deliberately."
    )
    if case.expected_dialect == "none":
        assert not caps.supports_reasoning
    return caps


def _cache_breakpoints() -> list[dict[str, str]]:
    # Same shape the runtime builds: cached base + dynamic tail.
    return [
        {"text": "Cached system base instructions.", "cache": "true"},
        {"text": "Dynamic per-session tail."},
    ]


def _chat_config(case: GoldenCase) -> ChatConfig:
    if case.backend == "openai_compat":
        return ChatConfig(
            system=_SYSTEM_PROMPT,
            temperature=case.temperature,
            thinking=case.thinking,
            thinking_level=ThinkingLevel.HIGH if case.thinking else None,
            thinking_budget_tokens=case.thinking_budget_tokens,
            model_capabilities=_compat_capabilities(case),
        )
    if case.backend == "anthropic":
        return ChatConfig(
            system=_SYSTEM_PROMPT,
            temperature=case.temperature,
            thinking=case.thinking,
            thinking_budget_tokens=case.thinking_budget_tokens,
            cache_breakpoints=_cache_breakpoints() if case.with_cache_breakpoints else None,
        )
    if case.backend == "openai_responses":
        return ChatConfig(
            system=_SYSTEM_PROMPT,
            temperature=case.temperature,
            thinking=case.thinking,
            thinking_level=ThinkingLevel.HIGH if case.thinking else None,
        )
    if case.backend == "ollama":
        return ChatConfig(system=_SYSTEM_PROMPT, temperature=case.temperature)
    raise AssertionError(f"Unknown backend {case.backend!r}")


def _compat_sse_body() -> bytes:
    chunks = [
        {
            "model": "test-model",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "test-model",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    return b"".join(
        f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _anthropic_sse_body() -> bytes:
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "model": "claude-test", "usage": {}},
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    parts: list[bytes] = []
    for event in events:
        parts.append(f"event: {event['type']}\n".encode())
        parts.append(f"data: {json.dumps(event)}\n\n".encode())
    return b"".join(parts)


def _mock_response(case: GoldenCase) -> httpx.Response:
    """Minimal valid response body so each adapter completes its stream."""
    if case.backend == "openai_compat":
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_compat_sse_body(),
        )
    if case.backend == "anthropic":
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_anthropic_sse_body(),
        )
    if case.backend == "openai_responses":
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        )
    if case.backend == "ollama":
        lines = [
            {"message": {"content": "ok"}},
            {"done": True, "prompt_eval_count": 2, "eval_count": 1},
        ]
        content = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson"},
            content=content,
        )
    raise AssertionError(f"Unknown backend {case.backend!r}")


def _provider_for_case(case: GoldenCase) -> Any:
    return _build_provider(
        ProviderConfig(
            provider=case.provider_id,
            model=case.model,
            api_key=case.api_key,
            base_url=case.base_url,
        )
    )


def _auth_style(request: httpx.Request) -> str:
    """Redacted marker for how the request authenticates — never the key."""
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return "bearer"
    if request.headers.get("x-api-key"):
        return "x-api-key"
    if authorization:
        return "other"
    return "none"


def golden_record(request: httpx.Request) -> dict[str, Any]:
    return {
        "method": request.method,
        "url": str(request.url),
        "auth_style": _auth_style(request),
        "content_type": request.headers.get("content-type", ""),
        "body": json.loads(request.content.decode("utf-8")),
    }


async def capture_case_record(
    case: GoldenCase, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Drive the adapter's chat() offline and return the captured request."""
    for env_key in _SCRUBBED_ENV_KEYS:
        monkeypatch.delenv(env_key, raising=False)

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _mock_response(case)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)

    provider = _provider_for_case(case)
    messages = _tool_history_messages() if case.tool_history else _plain_messages()
    tools = [_golden_tool()] if case.with_tools else None
    config = _chat_config(case)

    events = [event async for event in provider.chat(messages, tools=tools, config=config)]

    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert not errors, f"{case.case_id}: adapter errored: {errors}"
    assert any(isinstance(event, DoneEvent) for event in events), case.case_id
    assert len(captured) == 1, (
        f"{case.case_id}: expected exactly one outbound request, saw {len(captured)}"
    )
    return golden_record(captured[0])


def regen_mode() -> bool:
    return bool(os.environ.get(REGEN_ENV, "").strip())


def render_golden(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def assert_or_regen(path: Path, record: dict[str, Any]) -> None:
    """Byte-compare against the golden, or rewrite it in regen mode."""
    rendered = render_golden(record)
    if regen_mode():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rendered.encode("utf-8"))
        return
    assert path.exists(), (
        f"Missing golden {path}. Generate it with {REGEN_ENV}=1 "
        "uv run pytest tests/test_provider/test_request_goldens.py"
    )
    frozen = path.read_bytes()
    if frozen != rendered.encode("utf-8"):
        assert frozen.decode("utf-8") == rendered, (
            f"Request golden drift for {path.name}: the outbound request no "
            "longer matches the frozen payload byte-for-byte. If the change "
            f"is a deliberate behavior change, regenerate with {REGEN_ENV}=1 "
            "and have the diff reviewed."
        )
        raise AssertionError(
            f"Golden {path.name} differs in raw bytes but not decoded text "
            "(encoding or newline drift)."
        )
