"""Summary capacity recovery stays within one frozen operation and static authority."""

from __future__ import annotations

import asyncio
import base64
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from opensquilla.provider import ChatConfig, ModelCapabilities
from opensquilla.session.compaction import CompactionConfig, CompactionRequest, compact_context
from opensquilla.session.compaction_deployment import resolve_compaction_execution_plan
from opensquilla.session.compaction_lifecycle import CompactionTimeoutError
from tests.test_engine.test_capacity_fallback_32k import wrap
from tests.test_engine.test_native_overflow_recovery import NativeOverflowProvider


def setup(monkeypatch, *, mode="overflow", same_window=False, max_calls=None, large=False):
    primary = NativeOverflowProvider()
    wrapper, fallback, metadata, configs = wrap(
        monkeypatch, primary, same_window=same_window, output=24576 if same_window else 2048,
    )
    wrapper.configure_fallback_deployment_vision_support([
        (configs[0], "supported"), (configs[1], "unsupported"),
    ])
    capabilities = ModelCapabilities(supports_tools=True)
    source_config = ChatConfig(
        max_tokens=24576 if same_window else 2048,
        provider_context_window_tokens=32768, model_capabilities=capabilities,
        model_vision_support="supported",
        turn_deadline_at_monotonic=asyncio.get_running_loop().time() + 60,
    )
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=wrapper, active_provider_config=configs[0],
        active_chat_config=source_config, active_only=False,
    )
    assert plan is not None and len(plan.candidates) == 2
    plan = replace(plan, max_calls=max_calls)
    from opensquilla.session.compaction import CompactionRequestContext

    cfg = CompactionConfig(
        llm_plan=plan, request_context=CompactionRequestContext(chat_config=source_config),
        deadline_at_monotonic=source_config.turn_deadline_at_monotonic,
        protected_recent_messages=1,
    )
    entries = [
        {"role": "user", "content": "TASK_BETA_ONLY " + (
            "任务证据。" * 14000 if large else "Earlier record of completed work. " * 100
        )},
        {"role": "assistant", "content": "Task Alpha complete; Task Beta remains open."},
        {"role": "user", "content": "Continue Beta without restarting Alpha."},
    ]
    calls = []

    def response(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["model"] == "capacity-primary":
            if mode == "cancel":
                raise asyncio.CancelledError
            if mode == "deadline":
                cfg.deadline_at_monotonic = asyncio.get_running_loop().time() - 1
            if mode in {"auth", "policy", "quota", "missing"}:
                status, message = {
                    "auth": (401, "invalid API key"), "policy": (403, "policy refusal"),
                    "quota": (402, "insufficient credits"), "missing": (404, "model not found"),
                }[mode]
                return httpx.Response(status, json={"error": {"message": message}})
            if mode not in {"empty", "length"}:
                return httpx.Response(400, json={"error": {
                    "message": "maximum context length exceeded", "code": "context_length_exceeded",
                    "type": "invalid_request_error",
                }})
        if mode == "all_overflow":
            return httpx.Response(400, json={"error": {
                "message": "maximum context length exceeded", "code": "context_length_exceeded",
                "type": "invalid_request_error",
            }})
        text = "" if mode == "empty" else "Task Alpha is complete. Continue Task Beta only."
        body = {"choices": [{"delta": {"content": text},
                             "finish_reason": "length" if mode == "length" else "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n")

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(response)},
    ))
    request = CompactionRequest(
        session_id="summary-capacity", entries=entries, config=cfg,
        context_window_tokens=32768, forced_prefix_cut=2,
        consumer_admission=lambda summary, kept: "Beta" in summary and kept == entries[2:],
    )
    return request, wrapper, primary, fallback, metadata, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("large,same_window", [(False, False), (True, False), (False, True)])
async def test_authorized_summary_capacity_fallback_rebinds_wire_without_moving_execution(
    monkeypatch, large, same_window,
):
    request, wrapper, primary, fallback, metadata, calls = setup(
        monkeypatch, large=large, same_window=same_window,
    )
    raw = deepcopy(request.entries)
    deadline = request.config.deadline_at_monotonic
    result = await compact_context(request)
    assert result.summary and result.removed_count == 2
    assert request.entries == raw and result.kept_entries == raw[2:]
    # Even the oversized source now fits as bounded input on the primary.
    # Only its real server-side capacity rejection advances the authorized chain.
    assert [call["model"] for call in calls] == ["capacity-primary", "capacity-fallback"]
    assert request.config.llm_calls_started == len(calls)
    assert request.config.successful_target.model == "capacity-fallback"
    assert wrapper.active_model_id == "capacity-primary"
    assert len(wrapper._selector.remaining_chain()) == 2 and not metadata
    target_call = fallback.calls[-1]
    assert calls[-1] == target_call[1].payload and target_call[1].fits
    assert target_call[2].provider_context_window_tokens == (32768 if same_window else 131072)
    assert target_call[2].max_tokens == 2048
    assert "do not add detail merely to fill the available budget" in str(calls[-1])
    assert "Aim for at most" not in str(calls[-1])
    assert target_call[2].model_vision_support == "unsupported"
    assert request.config.request_context.chat_config.model_vision_support == "supported"
    assert all(config.turn_deadline_at_monotonic == deadline
               for _, _, config in primary.calls + fallback.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auth", "policy", "quota", "missing", "empty", "length"])
async def test_non_capacity_summary_failure_does_not_select_another_deployment(monkeypatch, mode):
    request, wrapper, _primary, fallback, _metadata, calls = setup(monkeypatch, mode=mode)
    result = await compact_context(request)
    assert not result.summary and result.removed_count == 0
    assert len(calls) == request.config.llm_calls_started == 1
    assert not fallback.calls and wrapper.active_model_id == "capacity-primary"
    assert result.failure_kind != "context_overflow"


@pytest.mark.asyncio
@pytest.mark.parametrize("bounded", [True, False])
async def test_summary_or_shorten_auth_failure_cannot_authorize_capacity_fallback(
    monkeypatch, bounded,
):
    if not bounded:
        # Exercise the retained shorten fallback independently of the default
        # input sampler, which now makes this source fit without shortening.
        monkeypatch.setattr(
            "opensquilla.session.compaction._bound_summary_history",
            lambda messages, *args, **kwargs: messages,
        )
    request, _wrapper, _primary, fallback, _metadata, calls = setup(
        monkeypatch, mode="auth", large=True,
    )
    request.previous_summary = "Task Alpha is complete. Task Beta is still pending."
    result = await compact_context(request)
    assert not result.summary and result.removed_count == 0
    assert len(calls) == 1 and not fallback.calls
    assert ("TASK_BETA_ONLY" in str(calls[0])) is bounded
    assert result.failure_kind != "context_overflow"


@pytest.mark.asyncio
async def test_summary_target_rebinds_media_tools_and_fresh_store_proof(tmp_path, monkeypatch):
    from opensquilla.provider import ContentBlockImage, ContentBlockText, Message
    from tests.test_engine.test_compaction_verified_references import fixture

    request, wrapper, primary, fallback, _metadata, calls = setup(monkeypatch)
    agent, _provider, _target, entries, store, record, raw, _original = fixture(tmp_path)
    entries[0]["_provider_message"] = Message(role="user", content=[
        ContentBlockText(text="Inspect the retained evidence for Task Beta."),
        ContentBlockImage(media_type="image/png", data=base64.b64encode(b"image").decode()),
    ])
    request.entries[:] = [*entries, request.entries[-1]]
    request.forced_prefix_cut = 3
    request.consumer_admission = lambda summary, kept: "Beta" in summary and len(kept) == 1
    source_context = replace(agent.build_compaction_request_context(),
                             chat_config=request.config.request_context.chat_config)
    request.config.request_context = source_context
    before = deepcopy(request.entries)
    result = await compact_context(request)
    assert result.summary and result.removed_count == 3
    assert request.entries == before
    assert [call["model"] for call in calls] == ["capacity-primary", "capacity-fallback"]
    assert "image_url" in str(calls[0]) and "image_url" not in str(calls[1])
    assert "图片未分析" in str(calls[1])
    for wire, captured in zip(calls, [primary.calls[-1], fallback.calls[-1]], strict=True):
        assert wire == captured[1].payload and captured[1].fits
        assert wire["tool_choice"] == "none"
        assert wire["tools"][0]["function"]["name"] == "retrieve_tool_result"
        assert record.handle in str(wire) and record.sha256 in str(wire)
        assert raw not in str(wire) and "call-1" in str(wire)
    assert store.read(record.handle, session_id=record.session_id).content == raw
    assert wrapper.active_model_id == "capacity-primary"


@pytest.mark.asyncio
async def test_summary_fallback_cannot_reset_explicit_call_ledger_or_restart_targets(monkeypatch):
    request, _wrapper, _primary, fallback, _metadata, calls = setup(monkeypatch, max_calls=1)
    result = await compact_context(request)
    assert not result.summary and len(calls) == request.config.llm_calls_started == 1
    assert not fallback.calls


@pytest.mark.asyncio
async def test_all_capacity_candidates_exhaust_once(monkeypatch):
    request, _wrapper, _primary, _fallback, _metadata, calls = setup(
        monkeypatch, mode="all_overflow",
    )
    result = await compact_context(request)
    assert not result.summary and result.removed_count == 0
    assert [call["model"] for call in calls] == ["capacity-primary", "capacity-fallback"]
    assert request.config.llm_calls_started == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_summary_fallback_preserves_cancel_and_original_operation_deadline(monkeypatch, mode):
    request, _wrapper, _primary, fallback, _metadata, calls = setup(monkeypatch, mode=mode)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await compact_context(request)
    else:
        with pytest.raises(CompactionTimeoutError):
            await compact_context(request)
    assert len(calls) == 1 and not fallback.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["active_only", "no_gain", "single_attempt", "exhausted"])
async def test_summary_plan_does_not_expand_existing_authority(monkeypatch, guard):
    primary = NativeOverflowProvider()
    wrapper, _fallback, _metadata, configs = wrap(
        monkeypatch, primary, same_window=guard == "no_gain",
    )
    config = ChatConfig(max_tokens=2048, provider_context_window_tokens=32768,
                        physical_attempt_limit=1 if guard == "single_attempt" else 0)
    if guard == "exhausted":
        assert wrapper.fallback_after_context_overflow(config)
    plan = resolve_compaction_execution_plan(
        app_config=None, active_provider=wrapper, active_provider_config=configs[0],
        active_chat_config=config, active_only=guard == "active_only",
    )
    assert plan is not None and len(plan.candidates) == 1
