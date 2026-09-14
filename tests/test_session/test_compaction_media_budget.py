from __future__ import annotations

import base64
import io
import json
from copy import deepcopy
from typing import Any

import pytest
from PIL import Image

from opensquilla.provider.replay_budget import project_message_replay_budget
from opensquilla.provider.request_proof import estimate_provider_media_tokens
from opensquilla.provider.types import ContentBlockImage, ContentBlockText, Message
from opensquilla.session.compaction import (
    estimate_entries_model_replay_chars,
    estimate_entry_model_replay_tokens,
)
from opensquilla.session.tokenizer import estimate_tokens


@pytest.fixture(scope="module")
def png_encodings() -> list[str]:
    encodings = []
    for compression in (0, 9):
        stream = io.BytesIO()
        with Image.new("1", (2048, 2048), 1) as image:
            image.save(stream, format="PNG", compress_level=compression)
        encodings.append(base64.b64encode(stream.getvalue()).decode("ascii"))
    return encodings


def _entry(kind: str, data: str, *, count: int = 1) -> dict[str, Any]:
    if kind == "upload":
        content = json.dumps({
            "text": "Inspect these pictures.",
            "attachments": [{"type": "image/png", "data": data} for _ in range(count)],
        })
        return {"role": "user", "content": content, "token_count": estimate_tokens(content)}
    message = Message(role="user", content=[
        ContentBlockText(text="Loaded pictures."),
        *[ContentBlockImage(media_type="image/png", data=data) for _ in range(count)],
    ])
    return {
        "role": "assistant",
        "content": "The tool loaded the pictures.",
        "assistant_replay": {"version": 1, "messages": [message.model_dump(mode="json")]},
    }


@pytest.mark.parametrize("kind", ["upload", "tool_replay"])
def test_compaction_pressure_does_not_grow_with_png_compression_size(
    png_encodings: list[str], kind: str,
) -> None:
    measurements = []
    for data in png_encodings:
        entry = _entry(kind, data)
        before = deepcopy(entry)
        measurements.append((
            estimate_entry_model_replay_tokens(entry),
            estimate_entries_model_replay_chars([entry]),
        ))
        assert entry == before
    assert measurements[0] == measurements[1]
    assert 4096 <= measurements[0][0] < 4600
    assert measurements[0][1] < 20_000


@pytest.mark.parametrize("kind", ["upload", "tool_replay"])
def test_compaction_counts_each_retained_image_once(png_encodings: list[str], kind: str) -> None:
    data = png_encodings[1]
    one = _entry(kind, data)
    two = _entry(kind, data, count=2)
    reserve = estimate_provider_media_tokens("image", 0, encoded_data=data)
    token_increment = (
        estimate_entry_model_replay_tokens(two) - estimate_entry_model_replay_tokens(one)
    )
    char_increment = (
        estimate_entries_model_replay_chars([two]) - estimate_entries_model_replay_chars([one])
    )
    assert reserve <= token_increment < reserve + 200
    assert reserve * 4 <= char_increment < reserve * 4 + 500


def test_plain_transcript_estimates_preserve_text_and_persisted_token_floor() -> None:
    entry = {
        "role": "assistant", "content": "Synthetic response", "token_count": 100,
        "tool_calls": [{"name": "lookup", "input": {"key": "value"}}],
        "reasoning_content": "Synthetic reasoning",
    }
    extras = json.dumps(entry["tool_calls"], ensure_ascii=False, sort_keys=True, default=str)
    expected = 100 + estimate_tokens(extras + "\n" + entry["reasoning_content"])
    assert estimate_entry_model_replay_tokens(entry) == expected
    payload = {key: value for key, value in entry.items() if key != "token_count"}
    expected_chars = len(json.dumps([payload], ensure_ascii=False, sort_keys=True, default=str))
    assert estimate_entries_model_replay_chars([entry]) == expected_chars


def test_plain_assistant_replay_estimates_preserve_existing_projection() -> None:
    message = Message(role="assistant", content="Synthetic response").model_dump(mode="json")
    replay = {"version": 1, "messages": [message]}
    entry = {"role": "assistant", "assistant_replay": replay}
    projected = {"version": 1, "messages": [project_message_replay_budget(message)]}
    expected = estimate_tokens(json.dumps(
        projected, ensure_ascii=False, sort_keys=True, default=str,
    ))
    assert estimate_entry_model_replay_tokens(entry) == expected
    expected_chars = len(json.dumps(
        [{"role": "assistant", "assistant_replay": projected}],
        ensure_ascii=False, sort_keys=True, default=str,
    ))
    assert estimate_entries_model_replay_chars([entry]) == expected_chars


def test_image_shape_nested_in_tool_arguments_keeps_its_full_text_cost(
    png_encodings: list[str],
) -> None:
    data = png_encodings[0]
    message = {
        "role": "assistant", "content": [{
            "type": "tool_use", "id": "synthetic-call", "name": "record",
            "input": {"type": "image", "source_type": "base64", "media_type": "image/png",
                      "data": data},
        }],
    }
    entry = {"role": "assistant", "assistant_replay": {"version": 1, "messages": [message]}}
    assert estimate_entry_model_replay_tokens(entry) > 10_000
    assert estimate_entries_model_replay_chars([entry]) > len(data)


def test_image_reference_uses_fallback_until_request_bytes_are_available() -> None:
    entry = {"role": "user", "content": json.dumps({
        "text": "Inspect.", "attachments": [{"type": "image/png", "sha256_ref": "a" * 64}],
    })}
    assert 1024 <= estimate_entry_model_replay_tokens(entry) < 1500
    assert 4096 <= estimate_entries_model_replay_chars([entry]) < 6000
