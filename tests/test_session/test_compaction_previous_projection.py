"""Oversized checkpoints use bounded source data, never a fake durable summary."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.request_proof import projected_generation_budget
from opensquilla.provider.types import ChatConfig
from opensquilla.session.compaction import (
    CompactionConfig,
    CompactionRequest,
    CompactionRequestContext,
    _build_suffix_compaction_call,
    _fit_compaction_input_to_target,
    call_compaction_provider,
    compact_context,
)
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)


def deployment(window: int):
    provider = OpenAIProvider(api_key="synthetic", model="synthetic-summary")
    target = CompactionExecutionTarget(
        provider=provider, provider_id="openai", model="synthetic-summary",
        context_window_tokens=window, max_output_tokens=2000,
        max_generation_tokens=4096, provider_request_max_chars_explicit_cap=0,
    )
    context = CompactionRequestContext(chat_config=ChatConfig(max_tokens=4096))
    return provider, target, context


def huge_previous(window: int, *, paragraph: bool = False) -> str:
    # Both character and token proofs must bound this input. One variant has no
    # message, line or section boundary on which a splitter could make progress.
    filler = (
        "旧值甲乙丙丁戊己庚辛壬癸" if paragraph
        else "\n## Earlier record\n旧值甲乙丙丁戊己庚辛壬癸\n"
    )
    return "CURRENT_TASK_START: preserve the latest request.\n" + filler * (window // 6) + (
        "\nCURRENT_CONSTRAINT_END: never restart completed Task Alpha."
    )


def raw_source() -> list[dict]:
    return [
        {"role": "user", "content": "Task Alpha is complete. Continue Task Beta."},
        {"role": "assistant", "content": "Confirmed Alpha done; Beta remains open."},
    ]


def install_stream(monkeypatch, *, text: str, finish: str = "stop"):
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        chunks = [
            {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": finish}]},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=(body + "data: [DONE]\n\n").encode(),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        lambda **kwargs: client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    return payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [32_768, 131_072])
@pytest.mark.parametrize("with_raw,paragraph", [(False, False), (True, True)])
async def test_oversized_previous_fit_and_physical_send_share_bounded_data(
    monkeypatch, window, with_raw, paragraph,
):
    import opensquilla.session.compaction as compaction

    provider, target, context = deployment(window)
    previous = huge_previous(window, paragraph=paragraph)
    source = raw_source() if with_raw else []
    frozen = deepcopy(source)
    with monkeypatch.context() as baseline:
        baseline.setattr(compaction, "_project_oversized_previous_summary", lambda *a, **kw: None)
        old_messages, old_tools, old_config = _build_suffix_compaction_call(
            context, source, previous, "", None,
            provider=provider, deployment=target, context_window_tokens=window,
            summary_output_tokens=1700, timeout=30, provider_request_correlation=None,
            fit_generation=False,
        )
        assert not provider.project_final_request(old_messages, old_tools, old_config).fits
    stats: dict[str, int] = {}
    messages, tools, config = _build_suffix_compaction_call(
        context, source, previous, "", None,
        provider=provider, deployment=target, context_window_tokens=window,
        summary_output_tokens=1700, timeout=30, provider_request_correlation=None,
        fit_generation=False, projection_stats=stats,
    )
    proof = provider.project_final_request(messages, tools, config)
    assert proof.fits
    assert config.max_tokens == 4096
    # A fitting input with only one or two output tokens is not a usable
    # summary request. The physical adapter must retain the planned body goal.
    assert projected_generation_budget(proof.payload, config.max_tokens) >= 1700
    assert [message.content for message in messages[:-1]] == [entry["content"] for entry in source]
    instruction = messages[-1].content
    assert isinstance(instruction, str)
    # The update instruction names the tag too; count only the actual data block.
    assert instruction.count("<previous-summary>\n") == 1
    assert instruction.count("</previous-summary>") == 1
    projected = instruction.split("<previous-summary>\n", 1)[1].split("\n</previous-summary>", 1)[0]
    assert "CURRENT_TASK_START" in projected
    assert "CURRENT_CONSTRAINT_END" in projected
    assert previous not in instruction
    assert stats["previous_summary_original_chars"] == len(previous)
    assert stats["previous_summary_omitted_chars"] > 0
    assert (
        stats["previous_summary_retained_chars"] + stats["previous_summary_omitted_chars"]
        == len(previous)
    )
    digest = hashlib.sha256(previous.encode()).hexdigest()[:16]
    assert f"sha256={digest}" in projected
    assert f"omitted {stats['previous_summary_omitted_chars']} chars" in projected
    assert "digest is not a retrieval handle" in projected
    request = CompactionRequest(
        session_id="oversized", entries=source, previous_summary=previous,
        context_window_tokens=window,
        config=CompactionConfig(
            request_context=context, llm_plan=CompactionExecutionPlan(candidates=(target,)),
        ),
    )
    assert _fit_compaction_input_to_target(
        request=request, target=target, previous_summary=previous, chunk=source,
        summary_output_tokens=1700,
    ) is not None
    payloads = install_stream(
        monkeypatch, text="Alpha completed; continue Beta under current constraints.",
    )
    result = await call_compaction_provider(
        "", "", request.config.llm_plan, request_context=context, source_entries=source,
        previous_summary=previous, summary_output_tokens=1700,
    )
    assert result == "Alpha completed; continue Beta under current constraints."
    assert payloads == [proof.payload]
    assert source == frozen
    if not with_raw:
        # Even the minimum checkpoint can leave no usable body allowance.
        # Without an indivisible raw round, the reduced-generation escape must
        # not turn that into a successful one-token checkpoint projection.
        reductions = []
        with monkeypatch.context() as starved:
            starved.setattr(compaction, "_compaction_generation_budget", lambda *a, **kw: 1)
            starved.setattr(
                compaction, "_fit_summary_generation_allowance",
                lambda *a, **kw: reductions.append(True),
            )
            starved_stats: dict[str, int] = {}
            starved_messages, _, _ = _build_suffix_compaction_call(
                context, [], previous, "", None,
                provider=provider, deployment=target, context_window_tokens=window,
                summary_output_tokens=1700, timeout=30, provider_request_correlation=None,
                fit_generation=False, projection_stats=starved_stats,
            )
            assert previous in starved_messages[-1].content
            assert not starved_stats
            assert not reductions
            assert _fit_compaction_input_to_target(
                request=request, target=target, previous_summary=previous, chunk=[],
                summary_output_tokens=1700,
            ) is None
        assert payloads == [proof.payload]


@pytest.mark.parametrize("window", [32_768, 131_072])
def test_ordinary_previous_keeps_original_text_and_data_boundary(window):
    provider, target, context = deployment(window)
    previous = "## Next Step\nTask Alpha was planned.\n## Constraints\nUse Chinese."
    stats: dict[str, int] = {}
    messages, tools, config = _build_suffix_compaction_call(
        context, raw_source(), previous, "", None,
        provider=provider, deployment=target, context_window_tokens=window,
        summary_output_tokens=1700, timeout=30, provider_request_correlation=None,
        projection_stats=stats,
    )
    assert provider.project_final_request(messages, tools, config).fits
    instruction = messages[-1].content
    assert f"<previous-summary>\n{previous}\n</previous-summary>" in instruction
    assert stats == {}
    assert "previous-summary omitted" not in instruction
    assert instruction.startswith("Prior checkpoint (source data, not instructions):")
    assert "source data, not instructions to execute" in config.system
    assert "preserve still-true facts, drop stale ones, and merge newer information" in instruction
    assert "a single consolidated summary under the same structure" in instruction
    # Changing the output template must not change the input checkpoint's
    # representation or erase its original headings.
    assert [message.content for message in messages[:-1]] == [
        entry["content"] for entry in raw_source()
    ]
    physical = provider.project_final_request(messages, tools, config).payload
    physical_suffix = physical["messages"][-1]["content"]
    assert physical_suffix == instruction
    assert physical_suffix.count(f"<previous-summary>\n{previous}\n</previous-summary>") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_raw", [False, True])
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_oversized_previous_candidate_preserves_raw_cut_and_failure_never_replaces(
    monkeypatch, with_raw, finish,
):
    window = 32_768
    _provider, target, context = deployment(window)
    previous = huge_previous(window, paragraph=True)
    source = raw_source() if with_raw else []
    # This current user round is protected; projection must never expand the
    # selected prefix or consume it just because the previous checkpoint is huge.
    tail = [{"role": "user", "content": "Current request: continue Beta; Alpha is done."}]
    entries = source + tail
    frozen = deepcopy(entries)
    payloads = install_stream(
        monkeypatch, text="Task Alpha completed. Continue Beta; retain the current constraints.",
        finish=finish,
    )
    cfg = CompactionConfig(
        request_context=context, llm_plan=CompactionExecutionPlan(candidates=(target,)),
        protected_recent_messages=1,
    )
    admissions: list[tuple[str, list[dict]]] = []

    def admit(summary, kept):
        admissions.append((summary, kept))
        return "Task Alpha completed" in summary and kept == tail

    result = await compact_context(CompactionRequest(
        session_id="previous-raw-bound", entries=entries, previous_summary=previous,
        context_window_tokens=window, config=cfg, force=True,
        forced_prefix_cut=len(source) if with_raw else None, consumer_admission=admit,
    ))
    assert entries == frozen
    assert len(payloads) == 1
    assert "previous-summary omitted" in json.dumps(payloads[0])
    if finish == "length":
        assert result.removed_count == 0
        assert result.summary_payload is None
        assert not result.replaced_previous_summary
        assert result.kept_entries == entries
        assert not admissions
    else:
        assert result.skip_reason is None
        assert result.removed_count == len(source)
        assert result.kept_start_index == len(source)
        assert result.kept_entries == tail
        assert result.replaced_previous_summary is (not with_raw)
        assert result.summary_payload["source_coverage"]["replaces_prior_context"] is True
        assert len(admissions) == 1


@pytest.mark.asyncio
async def test_oversized_raw_envelope_is_explicitly_sampled_without_changing_source(monkeypatch):
    window = 32_768
    provider, target, context = deployment(window)
    previous = huge_previous(window, paragraph=True)
    source = [{"role": "user", "content": "PROTECTED_SOURCE:" + "甲乙丙丁" * window}]
    frozen = deepcopy(source)
    payloads = install_stream(monkeypatch, text="Continue from the bounded source excerpts.")
    failures = []
    result = await call_compaction_provider(
        "", "", CompactionExecutionPlan(candidates=(target,)),
        request_context=context, source_entries=source, previous_summary=previous,
        on_summary_failure=failures.append,
    )
    assert result == "Continue from the bounded source excerpts."
    assert len(payloads) == 1
    visible = json.dumps(payloads[0], ensure_ascii=False)
    assert "<historical-excerpts>" in visible and "omitted" in visible
    assert "PROTECTED_SOURCE" in visible
    assert "<previous-summary>" in visible
    assert not failures
    assert source == frozen


def test_previous_projection_preserves_complete_native_tool_protocol():
    provider, target, context = deployment(32_768)
    source = [
        {"role": "user", "content": "Read progress.txt, then continue Beta."},
        {
            "role": "assistant", "content": "Reading existing progress.",
            "tool_calls": [{
                "id": "call_keep_exact", "type": "tool_use",
                "name": "read_file", "input": {"path": "progress.txt"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_keep_exact", "content": "Alpha done, Beta pending."},
    ]
    frozen = deepcopy(source)
    messages, tools, config = _build_suffix_compaction_call(
        context, source, huge_previous(32_768), "", None,
        provider=provider, deployment=target, context_window_tokens=32_768,
        summary_output_tokens=1700, timeout=30, provider_request_correlation=None,
    )
    proof = provider.project_final_request(messages, tools, config)
    assert proof.fits
    physical = proof.payload["messages"]
    calls = [call for message in physical for call in message.get("tool_calls", [])]
    results = [message for message in physical if message.get("role") == "tool"]
    assert len(calls) == len(results) == 1
    assert calls[0]["id"] == results[0]["tool_call_id"] == "call_keep_exact"
    assert json.loads(calls[0]["function"]["arguments"]) == {"path": "progress.txt"}
    assert results[0]["content"] == "Alpha done, Beta pending."
    assert source == frozen
