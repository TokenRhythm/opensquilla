"""Unit tests for provider-compacted tool-argument detection."""

from __future__ import annotations

import pytest

from opensquilla.provider.request_proof import (
    _compact_argument_string,
    _compact_string,
    _compact_tail_string,
    _emergency_compact_string,
    _hard_compact_string,
    _media_placeholder,
)
from opensquilla.tools.projected_arguments import find_projected_tool_argument

_LONG_VALUE = "\n".join(f"line {index}: some file content under test" for index in range(200))


def _embedded(marker_text: str) -> str:
    return f"def load(self):\n    {marker_text}\n    return data\n"


@pytest.mark.parametrize(
    "producer_output",
    [
        _compact_string(_LONG_VALUE),
        _compact_tail_string(_LONG_VALUE, label="tool_result"),
        _emergency_compact_string(_LONG_VALUE, label="text"),
        _hard_compact_string(_LONG_VALUE, label="reasoning_content"),
        _compact_argument_string(_LONG_VALUE, preview=False),
        _media_placeholder("image_url"),
        _media_placeholder("base64_image"),
        _media_placeholder("base64_media"),
        "[provider_request_image_url_omitted: 2048 chars]",
        "[provider_request_base64_image_omitted: 2048 chars]",
        "[provider_request_base64_media_omitted: 2048 chars]",
    ],
    ids=[
        "compact_string",
        "compact_tail_string",
        "emergency_compact_string",
        "hard_compact_string",
        "compact_argument_string",
        "media_image_url",
        "media_base64_image",
        "media_base64_media",
        "legacy_media_image_url",
        "legacy_media_base64_image",
        "legacy_media_base64_media",
    ],
)
def test_detects_every_instantiated_marker_embedded_mid_string(
    producer_output: str,
) -> None:
    poisoned = _embedded(producer_output)
    match = find_projected_tool_argument({"old_text": poisoned})
    assert match is not None
    assert match.kind == "compacted_marker_substring"
    assert match.path == "old_text"


def test_detects_marker_in_nested_edit_arguments() -> None:
    poisoned = _embedded(_compact_tail_string(_LONG_VALUE, label="tool_result"))
    arguments = {
        "path": "src/example.py",
        "edits": [{"old_text": poisoned, "new_text": "return data\n"}],
    }
    match = find_projected_tool_argument(arguments)
    assert match is not None
    assert match.kind == "compacted_marker_substring"
    assert match.path == "edits[0].old_text"


def test_char0_prefixes_keep_their_original_kinds() -> None:
    provider_request = find_projected_tool_argument(
        {
            "command": (
                "[provider_request_tool_input_compacted: original_chars=987; "
                "sha256=" + "a" * 64 + "]"
            )
        }
    )
    assert provider_request is not None
    assert provider_request.kind == "provider_request_projection_string"

    projection = find_projected_tool_argument(
        {"code": "[tool_use_argument_projection]\ntool: execute_code\n"}
    )
    assert projection is not None
    assert projection.kind == "projection_string"


@pytest.mark.parametrize(
    "value",
    [
        # This codebase's own f-string templates: braces, no digits.
        '[provider_request_compacted: omitted {omitted} chars]',
        (
            '[provider_request_{label}_compacted: omitted {omitted} chars; '
            'original_chars={len(value)}; sha256={digest}]'
        ),
        "[opensquilla_compacted:{label}:{len(value)}:{digest}]",
        # Prose or search strings that merely name a marker prefix.
        "grep -rn 'provider_request_tool_input_compacted' src/",
        "The marker [provider_request_tool_result_compacted: ...] means text was omitted.",
        # Ordinary code content.
        "def load(self):\n    return data[provider_request_id]\n",
        "chars = 42\noriginal_chars = compute()\n",
    ],
    ids=[
        "template_compact_string",
        "template_tail_string",
        "template_hard_compact",
        "grep_for_prefix",
        "prose_naming_marker",
        "ordinary_code",
        "ordinary_digits",
    ],
)
def test_does_not_match_templates_or_prose(value: str) -> None:
    assert find_projected_tool_argument({"old_text": value}) is None


def test_producer_outputs_below_threshold_pass_through() -> None:
    short = "print('ok')\n"
    assert _compact_tail_string(short, label="tool_result") == short
    assert find_projected_tool_argument({"old_text": short}) is None
