"""Summary and consumer admission reuse the execution path's Store proof."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import httpx
import pytest

from opensquilla import token_estimation
from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.provider import (
    ContentBlockToolResult,
    OpenAIProvider,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.session.compaction import _build_suffix_compaction_call, call_compaction_provider
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)

_CONTEXT_WINDOW_TOKENS = 131_072


@pytest.fixture(autouse=True)
def offline_token_estimates(monkeypatch):
    # Reference validation must also work without optional tokenizer data.
    # Keep this contract offline and budget for the real conservative estimate.
    monkeypatch.setattr(token_estimation, "_get_encoding", lambda: None)


def fixture(tmp_path, *, stored_session="session-1", digest_valid=True, retrieval=True):
    raw = "Original tool evidence line.\n" * 3000
    store = ToolResultStore(tmp_path / "tool-results")
    record = store.write(raw, tool_use_id="call-1", tool_name="read_log",
                         session_id=stored_session, session_key="agent:main:refs", agent_id="main")
    digest = record.sha256 if digest_valid else "0" * 64
    content = (f"[tool_result_projection]\ntool_result_handle: {record.handle}\n"
               f"sha256: {digest}\n" + raw)

    async def handler(call):
        raise AssertionError("Summary must not execute tools")
    handler._opensquilla_available_tools = frozenset({"retrieve_tool_result"} if retrieval else {})
    tools = [ToolDefinition(name="retrieve_tool_result", description="Read retained output.",
                            input_schema=ToolInputSchema(properties={}))]
    provider = OpenAIProvider(api_key="synthetic-no-network", model="synthetic")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=_CONTEXT_WINDOW_TOKENS, context_window_known=True, max_tokens=2048,
        tool_result_store_dir=str(tmp_path / "tool-results"),
        tool_result_store_session_id="session-1",
    ), tool_definitions=tools, tool_handler=handler)
    entries = [{"role": "user", "content": "Inspect this output."}, {
        "role": "assistant", "content": "", "tool_calls": [
            {"type": "tool_use", "tool_use_id": "call-1", "name": "read_log", "input": {}},
            {"type": "tool_result", "tool_use_id": "call-1", "result": content},
        ],
    }, {"role": "assistant", "content": "Inspection completed."}]
    target = CompactionExecutionTarget(provider=provider, provider_id="openai", model="synthetic",
                                       context_window_tokens=_CONTEXT_WINDOW_TOKENS,
                                       max_output_tokens=2048,
                                       max_generation_tokens=2048)
    # These cases isolate Store proof from summary-only pressure recovery.
    # Verify that the complete raw request really fits before testing references.
    raw_context = replace(
        agent.build_compaction_request_context(),
        recoverable_reference_resolver=lambda _messages: frozenset(),
    )
    raw_messages, raw_tools, raw_config = _build_suffix_compaction_call(
        raw_context, entries, "", "", None, provider=provider, deployment=target,
        context_window_tokens=_CONTEXT_WINDOW_TOKENS, summary_output_tokens=2048, timeout=30,
        provider_request_correlation=None,
    )
    assert tool_text(raw_messages) == content
    raw_request = provider.project_final_request(raw_messages, raw_tools, raw_config)
    assert raw_request.fits
    assert next(
        message["content"] for message in raw_request.payload["messages"]
        if message["role"] == "tool"
    ) == content
    return agent, provider, target, entries, store, record, raw, content


def tool_text(messages):
    return next(block.content for message in messages if isinstance(message.content, list)
                for block in message.content if isinstance(block, ContentBlockToolResult))


@pytest.mark.parametrize("mode", ["verified", "wrong_session", "wrong_digest", "unavailable"])
def test_verified_projection_matches_consumer_and_preserves_raw(tmp_path, mode):
    agent, provider, target, entries, store, record, raw, original = fixture(
        tmp_path, stored_session="other" if mode == "wrong_session" else "session-1",
        digest_valid=mode != "wrong_digest", retrieval=mode != "unavailable",
    )
    before = copy.deepcopy(entries)
    context = agent.build_compaction_request_context()
    messages, _, _ = _build_suffix_compaction_call(
        context, entries, "", "", None, provider=provider, deployment=target,
        context_window_tokens=_CONTEXT_WINDOW_TOKENS, summary_output_tokens=2048, timeout=30,
        provider_request_correlation=None,
    )
    admitted = agent._history_messages_for_compaction_admission(
        entries, active_user_in_history=False, bound_user_message_id=None, active_user_message="",
    )
    assert tool_text(messages) == tool_text(admitted)
    if mode == "verified":
        assert len(tool_text(messages)) < len(original)
        assert record.handle in tool_text(messages) and record.sha256 in tool_text(messages)
    else:
        assert tool_text(messages) == original
    assert entries == before
    assert store.read(record.handle, session_id=record.session_id).content == raw


async def test_summary_wire_uses_verified_projection_and_revalidates_disappeared_store(
    tmp_path, monkeypatch,
):
    agent, provider, target, entries, store, record, raw, original = fixture(tmp_path)
    context = agent.build_compaction_request_context()
    messages, tools, config = _build_suffix_compaction_call(
        context, entries, "", "", None, provider=provider, deployment=target,
        context_window_tokens=_CONTEXT_WINDOW_TOKENS, summary_output_tokens=2048, timeout=30,
        provider_request_correlation=None,
    )
    expected = provider.project_final_request(messages, tools, config)
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        chunks = [{"choices": [{"delta": {"content": "Inspection is complete."},
                                "finish_reason": None}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 100, "completion_tokens": 10}}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        ).encode())

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))
    result = await call_compaction_provider(
        "", "", CompactionExecutionPlan(candidates=(target,)), request_context=context,
        source_entries=entries, summary_output_tokens=2048,
    )
    assert result == "Inspection is complete."
    assert sent == [expected.payload]
    assert store.read(record.handle, session_id=record.session_id).content == raw
    monkeypatch.setattr(ToolResultStore, "read", lambda *args, **kwargs: None)
    retry_messages, _, _ = _build_suffix_compaction_call(
        context, entries, "", "", None, provider=provider, deployment=target,
        context_window_tokens=_CONTEXT_WINDOW_TOKENS, summary_output_tokens=2048, timeout=30,
        provider_request_correlation=None,
    )
    assert tool_text(retry_messages) == original
