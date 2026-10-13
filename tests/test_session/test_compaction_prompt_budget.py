"""Summary instructions stay valid when native request capacity reduces generation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.request_proof import (
    project_final_request_payload,
    provider_request_character_budget,
    provider_request_token_budget,
)
from opensquilla.provider.types import ChatConfig, ModelCapabilities
from opensquilla.session.compaction import (
    CompactionRequestContext,
    _build_suffix_compaction_call,
    call_compaction_provider,
)
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)


@pytest.mark.parametrize("identifier_instruction", ["", "Keep exact identifiers."])
@pytest.mark.parametrize("custom", [None, "Focus on implementation status."])
def test_summary_preserves_fact_ownership_without_identifier_policy(
    identifier_instruction, custom,
):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-model")
    target = CompactionExecutionTarget(
        provider=provider, provider_id="openai", model="synthetic-model",
        context_window_tokens=128_000, max_output_tokens=4096,
    )
    messages, _tools, _config = _build_suffix_compaction_call(
        None, [{"role": "user", "content": "Task Alpha is complete; Beta is pending."}],
        "", identifier_instruction, custom,
        provider=provider, deployment=target, context_window_tokens=128_000,
        summary_output_tokens=4096, timeout=30, provider_request_correlation=None,
    )
    instruction = messages[-1].content
    assert isinstance(instruction, str)
    assert (
        "For each retained fact, preserve which entity or field each value belongs to, "
        "and any explicitly stated current status; do not replace facts with an unlabelled "
        "list of values."
    ) in instruction


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_chars", [0, 3300])
@pytest.mark.parametrize("reasoning", ["off", "explicit", "mandatory"])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
async def test_reduced_summary_allowance_preserves_native_request_contract(
    monkeypatch: pytest.MonkeyPatch,
    previous_chars: int,
    reasoning: str,
    finish_reason: str,
) -> None:
    provider_kind = "dashscope" if reasoning == "mandatory" else "openai"
    model = "qwen3.8-max-preview" if reasoning == "mandatory" else "synthetic-model"
    provider = OpenAIProvider(api_key="synthetic", model=model, provider_kind=provider_kind)
    ordinary = ChatConfig(
        max_tokens=4096,
        thinking=reasoning == "explicit",
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            reasoning_format="dashscope" if reasoning == "mandatory" else "deepseek",
        ),
    )
    context = CompactionRequestContext(chat_config=ordinary)
    target = CompactionExecutionTarget(
        provider=provider,
        provider_id=provider_kind,
        model=model,
        context_window_tokens=16_000,
        max_output_tokens=4096,
        max_generation_tokens=4096,
        provider_request_max_chars_explicit_cap=0,
    )
    # One intact source round is too character-heavy for the original allowance.
    # A carried checkpoint leaves still less generation capacity on the next call.
    source = [
        {"role": "user", "content": "Original instruction. " + "a" * 21_200},
        {"role": "assistant", "content": "Recorded progress. " + "b" * 21_200},
    ]
    frozen = deepcopy(source)
    previous = "c" * previous_chars
    messages, tools, config = _build_suffix_compaction_call(
        context,
        source,
        previous,
        "",
        None,
        provider=provider,
        deployment=target,
        context_window_tokens=16_000,
        summary_output_tokens=4096,
        timeout=30,
        provider_request_correlation=None,
    )
    projection = provider.project_final_request(messages, tools, config)
    assert projection.fits
    assert 0 < config.max_tokens < (1500 if previous_chars else 2400)
    assert ordinary.max_tokens == target.max_generation_tokens == 4096
    assert config.thinking == ordinary.thinking
    assert [message.content for message in messages[:-1]] == [row["content"] for row in source]
    instruction = messages[-1].content
    assert isinstance(instruction, str)
    assert "Aim for about" not in instruction
    assert "4096" not in instruction
    assert "concise and complete" in instruction
    if previous:
        assert previous in instruction

    # Compare reasoning controls to the ordinary adapter at the same allowance;
    # mandatory-reasoning policy must survive even when the caller selects off.
    ordinary_projection = provider.project_final_request(
        messages, tools, ordinary.model_copy(update={"max_tokens": config.max_tokens}),
    )
    for field in ("enable_thinking", "thinking", "reasoning_effort", "thinking_budget"):
        assert projection.payload.get(field) == ordinary_projection.payload.get(field)
    if reasoning == "explicit":
        assert projection.payload["thinking"] == {"type": "enabled"}
    elif reasoning == "mandatory":
        assert projection.payload.get("enable_thinking") is not False

    physical_payloads: list[dict[str, Any]] = []
    summary = "The recorded work is complete; continue the remaining task."

    def handler(request: httpx.Request) -> httpx.Response:
        physical_payloads.append(json.loads(request.content))
        chunks = [
            {"model": model, "choices": [{"delta": {"content": summary}, "finish_reason": None}]},
            {
                "model": model,
                "choices": [{"delta": {}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    failures: list[str] = []
    result = await call_compaction_provider(
        "",
        "",
        CompactionExecutionPlan(candidates=(target,)),
        request_context=context,
        source_entries=source,
        previous_summary=previous,
        on_summary_failure=failures.append,
    )
    assert len(physical_payloads) == 1
    physical = physical_payloads[0]
    assert physical == projection.payload
    assert physical["model"] == model
    physical_proof = project_final_request_payload(
        physical,
        projection_adapter=provider_kind,
        proof_budget=provider_request_character_budget(physical, config),
        token_budget=provider_request_token_budget(physical, config),
    )
    for field in (
        "estimated_chars", "estimated_tokens", "effective_proof_budget",
        "effective_proof_token_budget", "fits_char_budget", "fits_token_budget", "fits",
    ):
        assert physical_proof.proof[field] == projection.proof[field]
    assert result == (summary if finish_reason == "stop" else None)
    assert failures == ([] if finish_reason == "stop" else ["incomplete_summary"])
    assert source == frozen
