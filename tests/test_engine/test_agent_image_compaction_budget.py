from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from opensquilla import token_estimation
from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message
from opensquilla.session.compaction import CompactionConfig, CompactionRequest, CompactionResult


def _image_message(compression: int) -> Message:
    stream = io.BytesIO()
    with Image.new("RGB", (512, 512), "#2468ac") as image:
        image.save(stream, format="PNG", compress_level=compression)
    return Message(role="user", content=[
        ContentBlockText(text="Inspect this image."),
        ContentBlockImage(
            media_type="image/png", data=base64.b64encode(stream.getvalue()).decode("ascii"),
        ),
    ])


def test_live_compaction_entry_budget_is_independent_of_png_compression() -> None:
    messages = [_image_message(compression) for compression in (0, 9)]
    entries = Agent._message_count_compaction_entries(messages)

    assert entries[0]["token_count"] == entries[1]["token_count"]
    assert 1024 <= entries[0]["token_count"] < 1500


@pytest.mark.parametrize("compression", [0, 9])
@pytest.mark.parametrize("force_tokenizer_fallback", [False, True], ids=["default", "fallback"])
async def test_inline_compaction_reduces_old_text_while_preserving_current_image(
    monkeypatch: pytest.MonkeyPatch, compression: int, force_tokenizer_fallback: bool,
) -> None:
    import opensquilla.engine.agent as agent_module

    if force_tokenizer_fallback:
        monkeypatch.setattr(
            token_estimation, "_encoding", token_estimation._ENCODING_UNAVAILABLE,
        )
    requests: list[CompactionRequest] = []
    compact_context = agent_module.compact_context

    async def record_compaction(request: CompactionRequest) -> CompactionResult:
        requests.append(request)
        return await compact_context(request)

    async def synthetic_summary(**_kwargs: object) -> str:
        return "The archived batches are complete. Continue with the current image request."

    monkeypatch.setattr(agent_module, "compact_context", record_compaction)
    monkeypatch.setattr(
        "opensquilla.session.compaction.call_compaction_llm", synthetic_summary,
    )
    agent = Agent(
        provider=OpenAIProvider(api_key="synthetic-offline", model="synthetic-model"),
        config=AgentConfig(
            # Leave input space for the current image after reserving the
            # complete output cap; the default 8k output fills this window.
            context_window_tokens=8192, max_tokens=1024,
            context_overflow_threshold=0.9,
        ),
    )
    monkeypatch.setattr(agent, "_build_compaction_config", lambda: CompactionConfig(
        model="synthetic-summary", api_key="synthetic-test-key",
    ))
    messages: list[Message] = []
    for index in range(20):
        messages.extend([
            # Short words overflow the history window with either estimator
            # while keeping the prefix within the two-call summary budget.
            Message(role="user", content=f"Archived batch {index}. " + "a b c d e f g h " * 50),
            Message(role="assistant", content=f"Batch {index} is complete."),
        ])
    current = _image_message(compression)
    messages.append(current)
    before = agent._project_live_request_budget(messages, tools=None, config=None)
    assert before["estimated_tokens"] > 8192 * 0.9

    outcome = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=before["estimated_tokens"],
        estimated_context_chars=before["estimated_chars"],
        protected_turn_start_index=len(messages) - 1,
        request_window_chars=8192 * 4,
        durable_consumer_overflow_proven=True,
    )

    assert requests
    assert requests[0].entries[-1]["token_count"] < 1500
    assert outcome is not None and outcome.compacted
    assert outcome.messages[-1] is current
    assert messages[0] not in outcome.messages
    assert len(outcome.messages) < len(messages)
    assert agent._estimate_live_request_tokens(outcome.messages) < before["estimated_tokens"]
    assert agent._last_compaction_refusal_reason is None
