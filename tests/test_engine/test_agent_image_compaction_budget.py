from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from opensquilla import token_estimation
from opensquilla.attachment_refs import write_transcript_material
from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message
from opensquilla.session.compaction import CompactionConfig, CompactionRequest, CompactionResult
from opensquilla.tools.types import current_tool_context


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


def test_consumer_admission_proves_retained_image_reference_pixels(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with Image.new("1", (2048, 2048), 1) as image:
        image.save(stream, format="PNG")
    raw = stream.getvalue()
    media_root = tmp_path / "media"
    sha, _path, _written = write_transcript_material(
        media_root=media_root, session_id="synthetic-session", payload=raw,
    )
    entry = {
        "role": "user", "session_id": "synthetic-session", "message_id": "image-1",
        "content": json.dumps({"text": "Inspect the retained picture.", "attachments": [{
            "type": "image/png", "sha256_ref": sha, "size": len(raw),
        }]}),
    }
    provider = OpenAIProvider(api_key="synthetic-offline", model="synthetic-model")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=3_500, max_tokens=256, preserve_historical_images=True,
    ))
    agent._tool_context = SimpleNamespace(
        artifact_media_root=str(media_root), artifact_session_id="synthetic-session",
    )
    history = agent._history_messages_for_compaction_admission(
        [entry], active_user_in_history=False, bound_user_message_id=None,
        active_user_message="Continue.",
    )
    assert history is not None
    assert any(
        isinstance(block, ContentBlockImage) and block.data == base64.b64encode(raw).decode()
        for message in history
        for block in message.content if isinstance(message.content, list)
    )
    projection = agent._project_compaction_consumer_request(
        consumer_provider=provider, replay_summary="checkpoint", kept_entries=[entry],
        active_user_message="Continue.", active_user_in_history=False,
        bound_user_message_id=None, attachment_messages=None,
        runtime_context_message=Message(role="user", content="runtime"),
        context_window_tokens=3_500, max_output_tokens=256,
    )
    assert projection is not None
    assert projection.proof["estimated_tokens"] >= 4_096
    assert not projection.fits

    agent._tool_context = None
    agent._compaction_media_root = media_root
    agent._compaction_session_id = "synthetic-session"
    ambient = current_tool_context.set(SimpleNamespace(
        artifact_media_root=str(tmp_path / "wrong-media"),
        artifact_session_id="other-session",
    ))
    try:
        assert agent._history_messages_for_compaction_admission(
            [entry], active_user_in_history=False, bound_user_message_id=None,
            active_user_message="Continue.",
        ) is not None
        manual_config = agent._build_compaction_config()
        assert manual_config.attachment_media_root == media_root
        assert manual_config.attachment_path_resolver is None
        write_transcript_material(
            media_root=media_root, session_id="other-session", payload=raw,
        )
        agent._compaction_session_id = ""
        assert agent._history_messages_for_compaction_admission(
            [{**entry, "session_id": ""}], active_user_in_history=False,
            bound_user_message_id=None, active_user_message="Continue.",
        ) is None
        agent._compaction_session_id = "synthetic-session"
    finally:
        current_tool_context.reset(ambient)

    agent._compaction_media_root = None
    agent._compaction_session_id = ""
    assert agent._history_messages_for_compaction_admission(
        [entry], active_user_in_history=False, bound_user_message_id=None,
        active_user_message="Continue.",
    ) is None


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
