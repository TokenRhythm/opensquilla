"""Golden tests freezing the StreamEvent sequences provider adapters decode.

Each case pairs a committed synthetic wire transcript (the exact bytes an
upstream would send over its native format — OpenAI-compat SSE, Anthropic SSE,
Ollama JSONL, or an OpenAI Responses JSON body) with a committed golden JSON
file holding the full decoded StreamEvent sequence. The stream is served
offline through ``httpx.MockTransport``; the test iterates ``provider.chat()``
and byte-compares the serialized events against the golden.

On top of the byte-compare, ``_assert_lifecycle_invariants`` enforces the
``stream_assembly`` contract on every decoded sequence: one Start and exactly
one End per tool call, a stable ``tool_use_id`` across Start/Delta/End,
deltas only between their Start and End, joined argument fragments parsing to
the End arguments, joined reasoning deltas equalling
``DoneEvent.reasoning_content``, and a single terminal ``DoneEvent``.

Regenerate goldens with ``OPENSQUILLA_REGEN_GOLDENS=1`` (see the README in
``tests/test_provider/golden/streams/``). A golden diff is a provider-decode
behavior change and must be intentional.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ReasoningDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_STREAMS_DIR = Path(__file__).resolve().parent / "golden" / "streams"
_REGEN_ENV = "OPENSQUILLA_REGEN_GOLDENS"

# Environment knobs that could perturb the decode path on a developer machine.
_NEUTRALIZED_ENV = (
    "OPENSQUILLA_LLM_PROXY",
    "OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS",
    "OPENSQUILLA_TRACE_ROUTING",
    "OPENSQUILLA_LLM_STREAM_CONNECT_TIMEOUT_SECONDS",
    "OPENSQUILLA_LLM_STREAM_WRITE_TIMEOUT_SECONDS",
)

Collector = Callable[[pytest.MonkeyPatch, bytes], Awaitable[list[Any]]]


# ---------------------------------------------------------------------------
# Harness helpers (test-only; no production code involved)
# ---------------------------------------------------------------------------


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    body: bytes,
    content_type: str,
) -> None:
    """Serve the canned wire transcript for any request the adapter makes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", patched_async_client)


def _patch_uuid4(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Make synthesized tool_use_ids deterministic so goldens byte-compare."""
    counter = itertools.count()

    class _FakeUUID:
        def __init__(self, value: int) -> None:
            self.hex = f"{value:032x}"

    monkeypatch.setattr(f"{module}.uuid4", lambda: _FakeUUID(next(counter)))


async def _collect_events(provider: Any, tools: list[ToolDefinition] | None) -> list[Any]:
    return [
        event
        async for event in provider.chat(
            [Message(role="user", content="hi")],
            tools=tools,
            config=ChatConfig(),
        )
    ]


def _weather_tool() -> ToolDefinition:
    return ToolDefinition(
        name="get_weather",
        description="Return the weather for a city.",
        input_schema=ToolInputSchema(
            properties={
                "city": {"type": "string"},
                "unit": {"type": "string"},
            },
            required=["city"],
        ),
    )


def _lookup_tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="Look up a value.",
        input_schema=ToolInputSchema(
            properties={"q": {"type": "string"}},
            required=["q"],
        ),
    )


def _openai_collector(
    *,
    model: str = "gpt-test",
    provider_kind: str | None = None,
    tools: list[ToolDefinition] | None = None,
    deterministic_uuid: bool = False,
) -> Collector:
    async def collect(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
        if deterministic_uuid:
            _patch_uuid4(monkeypatch, "opensquilla.provider.openai")
        _patch_transport(monkeypatch, "opensquilla.provider.openai", body, "text/event-stream")
        provider_base_url = (
            "https://tokenrhythm.studio/v1"
            if provider_kind == "tokenrhythm"
            else "https://api.openai.com"
        )
        provider = OpenAIProvider(
            api_key="sk-test-000",
            model=model,
            base_url=provider_base_url,
            provider_kind=provider_kind,
        )
        return await _collect_events(provider, tools)

    return collect


def _anthropic_collector(*, tools: list[ToolDefinition] | None = None) -> Collector:
    async def collect(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
        _patch_transport(monkeypatch, "opensquilla.provider.anthropic", body, "text/event-stream")
        provider = AnthropicProvider(api_key="sk-test-000", model="claude-test")
        return await _collect_events(provider, tools)

    return collect


def _ollama_collector(*, tools: list[ToolDefinition] | None = None) -> Collector:
    async def collect(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
        _patch_transport(monkeypatch, "opensquilla.provider.ollama", body, "application/x-ndjson")
        provider = OllamaProvider(model="test-model")
        return await _collect_events(provider, tools)

    return collect


def _responses_collector(*, tools: list[ToolDefinition] | None = None) -> Collector:
    async def collect(monkeypatch: pytest.MonkeyPatch, body: bytes) -> list[Any]:
        _patch_transport(
            monkeypatch,
            "opensquilla.provider.openai_responses",
            body,
            "application/json",
        )
        provider = OpenAIResponsesProvider(api_key="sk-test-000", model="gpt-test")
        return await _collect_events(provider, tools)

    return collect


# ---------------------------------------------------------------------------
# Case registry
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GoldenCase:
    adapter: str
    fixture: str
    collect: Collector

    @property
    def case_id(self) -> str:
        return f"{self.adapter}-{self.fixture.split('.')[0]}"

    @property
    def fixture_path(self) -> Path:
        return _STREAMS_DIR / self.adapter / self.fixture

    @property
    def golden_path(self) -> Path:
        return _STREAMS_DIR / self.adapter / f"{self.fixture.split('.')[0]}.events.json"


CASES: list[GoldenCase] = [
    # openai_compat (OpenAIProvider, Chat Completions SSE)
    GoldenCase("openai_compat", "text_finish_usage.sse", _openai_collector()),
    GoldenCase(
        "openai_compat",
        "tool_call_fragmented.sse",
        _openai_collector(tools=[_weather_tool()]),
    ),
    GoldenCase("openai_compat", "reasoning_content_then_text.sse", _openai_collector()),
    GoldenCase("openai_compat", "reasoning_details_then_text.sse", _openai_collector()),
    GoldenCase("openai_compat", "usage_final_chunk_done.sse", _openai_collector()),
    GoldenCase(
        "openai_compat",
        "minimax_text_tool_synthesis.sse",
        _openai_collector(
            model="minimax-test-1",
            provider_kind="minimax",
            tools=[_lookup_tool()],
            deterministic_uuid=True,
        ),
    ),
    # TokenRhythm's live stream tail: a duplicate finish_reason chunk plus
    # TWO usage-bearing chunks (a details chunk with reasoning/cached token
    # counts, then a finish repeat with cost_cny/trace_id extras). Finish
    # and usage handling use latest-present snapshot fields, so this must stay
    # one clean turn without losing the earlier details or double-counting.
    GoldenCase(
        "openai_compat",
        "tokenrhythm_duplicate_finish_usage.sse",
        _openai_collector(model="deepseek-v4-flash", provider_kind="tokenrhythm"),
    ),
    # TokenRhythm's corrected stream tail carries finish, the complete usage
    # snapshot, and confirmed CNY billing metadata in one terminal frame. Keep
    # this case alongside the legacy double-tail case above so both deployed
    # response shapes remain covered without special-casing either one.
    GoldenCase(
        "openai_compat",
        "tokenrhythm_single_finish_usage.sse",
        _openai_collector(model="deepseek-v4-flash", provider_kind="tokenrhythm"),
    ),
    # anthropic (AnthropicProvider, Messages SSE)
    GoldenCase("anthropic", "text_content_blocks.sse", _anthropic_collector()),
    GoldenCase("anthropic", "thinking_signature_then_text.sse", _anthropic_collector()),
    GoldenCase(
        "anthropic",
        "tool_use_fragmented_input.sse",
        _anthropic_collector(tools=[_weather_tool()]),
    ),
    GoldenCase("anthropic", "stop_reason_usage_cache.sse", _anthropic_collector()),
    # ollama (OllamaProvider, JSONL)
    GoldenCase("ollama", "text_then_done.jsonl", _ollama_collector()),
    GoldenCase(
        "ollama",
        "whole_chunk_tool_call.jsonl",
        _ollama_collector(tools=[_lookup_tool()]),
    ),
    # openai_responses (OpenAIResponsesProvider, non-streaming JSON)
    GoldenCase(
        "openai_responses",
        "text_tool_call_usage.json",
        _responses_collector(tools=[_weather_tool()]),
    ),
]


# ---------------------------------------------------------------------------
# Serialization + invariants
# ---------------------------------------------------------------------------


def _event_to_dict(event: Any) -> dict[str, Any]:
    payload = dataclasses.asdict(event)
    payload.pop("kind", None)
    if payload.get("billing_receipt") is None:
        payload.pop("billing_receipt", None)
    return {"type": type(event).__name__, **payload}


def _render_events(events: list[Any]) -> str:
    return json.dumps([_event_to_dict(e) for e in events], indent=2, sort_keys=True) + "\n"


def _assert_lifecycle_invariants(events: list[Any]) -> None:
    """Enforce the stream_assembly lifecycle contract on a decoded sequence."""
    assert events, "decoded stream produced no events"
    assert not any(isinstance(e, ErrorEvent) for e in events), "golden streams must not error"
    done_events = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done_events) == 1, "exactly one DoneEvent per stream"
    assert isinstance(events[-1], DoneEvent), "DoneEvent must terminate the stream"

    started: dict[str, ToolUseStartEvent] = {}
    fragments: dict[str, list[str]] = {}
    ended: dict[str, ToolUseEndEvent] = {}
    for event in events:
        if isinstance(event, ToolUseStartEvent):
            assert event.tool_use_id, "ToolUseStart must carry a tool_use_id"
            assert event.tool_use_id not in started, "one ToolUseStart per call"
            started[event.tool_use_id] = event
            fragments[event.tool_use_id] = []
        elif isinstance(event, ToolUseDeltaEvent):
            assert event.tool_use_id in started, "ToolUseDelta before its Start"
            assert event.tool_use_id not in ended, "ToolUseDelta after its End"
            fragments[event.tool_use_id].append(event.json_fragment)
        elif isinstance(event, ToolUseEndEvent):
            assert event.tool_use_id in started, "ToolUseEnd before its Start"
            assert event.tool_use_id not in ended, "exactly one ToolUseEnd per call"
            assert event.tool_name == started[event.tool_use_id].tool_name
            ended[event.tool_use_id] = event
    assert set(started) == set(ended), "every started tool call must be closed"
    for tool_use_id, parts in fragments.items():
        joined = "".join(parts)
        if joined:
            assert json.loads(joined) == ended[tool_use_id].arguments, (
                "joined argument fragments must parse to the End arguments"
            )

    reasoning_text = "".join(e.text for e in events if isinstance(e, ReasoningDeltaEvent))
    assert (done_events[0].reasoning_content or "") == reasoning_text, (
        "joined reasoning deltas must equal DoneEvent.reasoning_content"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
async def test_stream_decode_matches_golden(
    case: GoldenCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _NEUTRALIZED_ENV:
        monkeypatch.delenv(name, raising=False)

    events = await case.collect(monkeypatch, case.fixture_path.read_bytes())
    _assert_lifecycle_invariants(events)
    rendered = _render_events(events)

    if os.environ.get(_REGEN_ENV) == "1":
        case.golden_path.write_text(rendered, encoding="utf-8")
    assert case.golden_path.exists(), (
        f"missing golden {case.golden_path}; regenerate with {_REGEN_ENV}=1"
    )
    assert case.golden_path.read_text(encoding="utf-8") == rendered, (
        f"decoded StreamEvent sequence diverged from {case.golden_path.name}; "
        f"if the change is intentional, regenerate with {_REGEN_ENV}=1 and review the diff"
    )


def test_golden_stream_dir_inventory_is_exact() -> None:
    """Every fixture/golden is owned by a case; strays and orphans fail."""
    expected = {Path("README.md")}
    for case in CASES:
        expected.add(case.fixture_path.relative_to(_STREAMS_DIR))
        expected.add(case.golden_path.relative_to(_STREAMS_DIR))
    actual = {
        path.relative_to(_STREAMS_DIR) for path in _STREAMS_DIR.rglob("*") if path.is_file()
    }
    assert actual == expected
