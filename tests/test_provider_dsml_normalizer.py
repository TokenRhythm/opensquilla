"""Strict DeepSeek DSML normalization at the provider trust boundary."""

from __future__ import annotations

import pytest

from opensquilla.provider.compat_policy import TEXT_TOOL_DIALECT_DEEPSEEK_DSML
from opensquilla.provider.text_tool_normalizer import (
    LiteralTextSegment,
    RejectedTextToolSegment,
    SyntheticToolSegment,
    TextToolStreamNormalizer,
    classify_text_tool_segments,
    parse_dsml_candidate,
)
from opensquilla.provider.types import ToolDefinition, ToolInputSchema

_TOOL = ToolDefinition(
    name="search",
    description="Search synthetic data.",
    input_schema=ToolInputSchema(
        properties={
            "query": {"type": "string"},
            "options": {"type": "object"},
        },
        required=["query"],
        additionalProperties=False,
    ),
)

_PING_TOOL = ToolDefinition(
    name="ping",
    description="Record a bounded synthetic call.",
    input_schema=ToolInputSchema(
        properties={},
        required=[],
        additionalProperties=False,
    ),
)

_DSML_CALL = (
    "<｜DSML｜tool_calls>"
    '<｜DSML｜invoke name="search">'
    '<｜DSML｜parameter name="query" string="true">needle</｜DSML｜parameter>'
    '<｜DSML｜parameter name="options" string="false">'
    '{"limit":2,"nested":{"ok":true}}'
    "</｜DSML｜parameter>"
    "</｜DSML｜invoke>"
    "</｜DSML｜tool_calls>"
)


def _normalizer(
    *,
    tools: list[ToolDefinition] | None = None,
    max_candidate_chars: int = 256_000,
) -> TextToolStreamNormalizer:
    return TextToolStreamNormalizer(
        tools=[_TOOL] if tools is None else tools,
        dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
        provider_kind="deepseek",
        model="deepseek-v4-flash",
        max_candidate_chars=max_candidate_chars,
    )


def _synthetic_calls(segments: list[object]) -> list[object]:
    return [
        call
        for segment in segments
        if isinstance(segment, SyntheticToolSegment)
        for call in segment.calls
    ]


def _rejections(segments: list[object]) -> list[RejectedTextToolSegment]:
    return [
        segment
        for segment in segments
        if isinstance(segment, RejectedTextToolSegment)
    ]


def test_parse_dsml_candidate_is_syntax_only_and_preserves_typed_values() -> None:
    marker_text = "literal <｜DSML｜invoke bytes"
    text = (
        "I will search.\n"
        "<｜DSML｜tool_calls>\r\n"
        '<｜DSML｜invoke name="not_in_allowlist">'
        '<｜DSML｜parameter name="query" string="true">'
        f"{marker_text}"
        "</｜DSML｜parameter>"
        '<｜DSML｜parameter name="options" string="false">'
        '{"items":[1,false,null],"nested":{"x":"y"}}'
        "</｜DSML｜parameter>"
        "</｜DSML｜invoke>"
        '<｜DSML｜invoke name="search"></｜DSML｜invoke>'
        "</｜DSML｜tool_calls>\n"
    )

    result = parse_dsml_candidate(text)

    assert result.status == "complete"
    assert result.call_count == 2
    assert result.calls[0].tool_name == "not_in_allowlist"
    assert result.calls[0].arguments == {
        "query": marker_text,
        "options": {
            "items": [1, False, None],
            "nested": {"x": "y"},
        },
    }
    assert result.calls[1].arguments == {}
    assert text[result.start : result.end].endswith("</｜DSML｜tool_calls>")


def test_complete_dsml_is_validated_and_synthesized_atomically() -> None:
    text = f"Searching now.\n{_DSML_CALL}\n "

    segments = classify_text_tool_segments(
        text,
        [_TOOL],
        dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
        provider_kind="deepseek",
        model="deepseek-v4-flash",
    )

    calls = _synthetic_calls(segments)
    assert len(calls) == 1
    call = calls[0]
    assert call.tool_name == "search"
    assert call.arguments == {
        "query": "needle",
        "options": {"limit": 2, "nested": {"ok": True}},
    }
    assert call.dialect == TEXT_TOOL_DIALECT_DEEPSEEK_DSML
    assert call.parse_format == "dsml"
    assert [
        segment.text
        for segment in segments
        if isinstance(segment, LiteralTextSegment)
    ] == ["Searching now.\n", "\n "]


def test_every_character_split_holds_dsml_until_finish() -> None:
    text = f"Visible prose.\n{_DSML_CALL}"
    expected_arguments = {
        "query": "needle",
        "options": {"limit": 2, "nested": {"ok": True}},
    }

    for split in range(1, len(text)):
        normalizer = _normalizer()
        visible = "".join(normalizer.push(text[:split]))
        visible += "".join(normalizer.push(text[split:]))
        segments = normalizer.finish(successful_text_tool_terminal=True)

        assert visible == "Visible prose.\n", split
        assert [call.arguments for call in _synthetic_calls(segments)] == [
            expected_arguments
        ], split
        assert _rejections(segments) == [], split


def test_dsml_call_count_matches_native_256_call_limit() -> None:
    invoke = '<｜DSML｜invoke name="ping"></｜DSML｜invoke>'

    allowed = parse_dsml_candidate(
        f"<｜DSML｜tool_calls>{invoke * 256}</｜DSML｜tool_calls>"
    )
    rejected_text = f"<｜DSML｜tool_calls>{invoke * 257}</｜DSML｜tool_calls>"
    rejected = parse_dsml_candidate(rejected_text)

    assert allowed.status == "complete"
    assert allowed.call_count == 256
    assert rejected.status == "rejected"
    assert rejected.reason == "dsml_oversized"
    assert rejected.call_count == 257
    segments = classify_text_tool_segments(
        rejected_text,
        [_PING_TOOL],
        dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
        provider_kind="deepseek",
        model="deepseek-v4-flash",
    )
    assert _synthetic_calls(segments) == []
    assert _rejections(segments) == [
        RejectedTextToolSegment(
            dialect=TEXT_TOOL_DIALECT_DEEPSEEK_DSML,
            reason="dsml_oversized",
            call_count=257,
            recognized_tool_names=("ping",) * 256,
        )
    ]


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        ("<｜DSML｜tool_calls>", "dsml_incomplete"),
        ("<｜DSML｜tool_call", "dsml_incomplete"),
        (
            _DSML_CALL.replace(
                "</｜DSML｜invoke>",
                '<｜DSML｜parameter name="query" string="true">again'
                "</｜DSML｜parameter></｜DSML｜invoke>",
            ),
            "dsml_malformed",
        ),
        (
            _DSML_CALL.replace('string="false">{', 'string="false">{"x":1,"x":2,'),
            "dsml_malformed",
        ),
        (
            _DSML_CALL.replace('{"limit":2,"nested":{"ok":true}}', "NaN"),
            "dsml_malformed",
        ),
        (f"{_DSML_CALL} trailing", "dsml_malformed"),
        (f"{_DSML_CALL}\n{_DSML_CALL}", "dsml_malformed"),
        (
            _DSML_CALL.replace(
                '<｜DSML｜invoke name="search">',
                '<｜DSML｜invoke extra="x" name="search">',
            ),
            "dsml_malformed",
        ),
        (
            _DSML_CALL.replace(
                '<｜DSML｜parameter name="query" string="true">',
                '<｜DSML｜parameter string="true" name="query">',
            ),
            "dsml_malformed",
        ),
        (
            _DSML_CALL.replace(
                '<｜DSML｜invoke name="search">',
                '<｜DSML｜invoke name="search"><｜DSML｜invoke name="search">',
            ),
            "dsml_malformed",
        ),
        (
            _DSML_CALL.replace(
                "<｜DSML｜tool_calls><｜DSML｜invoke",
                "<｜DSML｜tool_calls>\u00a0<｜DSML｜invoke",
            ),
            "dsml_malformed",
        ),
    ],
)
def test_malformed_or_incomplete_dsml_returns_metadata_only_rejection(
    candidate: str,
    reason: str,
) -> None:
    text = f"Safe prefix.\n{candidate}"
    segments = classify_text_tool_segments(
        text,
        [_TOOL],
        dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
        provider_kind="deepseek",
        model="deepseek-v4-flash",
    )

    assert segments[0] == LiteralTextSegment("Safe prefix.\n")
    assert _synthetic_calls(segments) == []
    rejection = _rejections(segments)[0]
    assert rejection.dialect == TEXT_TOOL_DIALECT_DEEPSEEK_DSML
    assert rejection.reason == reason
    assert rejection.recognized_tool_names == (
        "search",
    ) * candidate.count('<｜DSML｜invoke name="search">')
    assert not hasattr(rejection, "source_text")
    assert candidate not in repr(rejection)


@pytest.mark.parametrize(
    "literal",
    [
        _DSML_CALL.replace("｜", "|"),
        _DSML_CALL.replace("DSML", "dsml"),
        f"Before: {_DSML_CALL}",
        f"```xml\n{_DSML_CALL}\n```",
        f"<code>{_DSML_CALL}</code>",
        f"    {_DSML_CALL}",
    ],
)
def test_noncanonical_or_documentation_dsml_remains_literal(literal: str) -> None:
    segments = classify_text_tool_segments(
        literal,
        [_TOOL],
        dialects=frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML}),
        provider_kind="deepseek",
        model="deepseek-v4-flash",
    )
    assert segments == [LiteralTextSegment(literal)]


def test_unauthorized_dialect_remains_literal() -> None:
    segments = classify_text_tool_segments(
        _DSML_CALL,
        [_TOOL],
        dialects=frozenset(),
        provider_kind="other",
        model="deepseek-v4-flash",
    )
    assert segments == [LiteralTextSegment(_DSML_CALL)]


@pytest.mark.parametrize(
    ("tools", "candidate", "reason"),
    [
        ([], _DSML_CALL, "dsml_unknown_tool"),
        (
            [_TOOL],
            _DSML_CALL.replace('name="search"', 'name="not_allowed"', 1),
            "dsml_unknown_tool",
        ),
        (
            [_TOOL],
            _DSML_CALL.replace("needle", "").replace('name="query"', 'name="extra"'),
            "dsml_schema_invalid",
        ),
    ],
)
def test_allowlist_and_schema_rejections_never_replay_dsml(
    tools: list[ToolDefinition],
    candidate: str,
    reason: str,
) -> None:
    normalizer = _normalizer(tools=tools)
    assert normalizer.push(candidate) == []
    segments = normalizer.finish(successful_text_tool_terminal=True)
    assert _synthetic_calls(segments) == []
    assert _rejections(segments) == [
        RejectedTextToolSegment(
            dialect=TEXT_TOOL_DIALECT_DEEPSEEK_DSML,
            reason=reason,
            call_count=1,
            recognized_tool_names=("search",)
            if reason == "dsml_schema_invalid"
            else (),
        )
    ]


def test_oversized_dsml_discards_all_remaining_text_and_returns_reason_only() -> None:
    normalizer = _normalizer(max_candidate_chars=32)
    sensitive = "<｜DSML｜tool_calls>" + "secret-command " * 20

    assert normalizer.push(sensitive) == []
    assert normalizer.push("more secret trailing bytes") == []
    segments = normalizer.finish(successful_text_tool_terminal=True)

    assert segments == [
        RejectedTextToolSegment(
            dialect=TEXT_TOOL_DIALECT_DEEPSEEK_DSML,
            reason="dsml_oversized",
        )
    ]
    assert "secret" not in repr(segments)


def test_unsuccessful_terminal_scrubs_even_complete_dsml() -> None:
    normalizer = _normalizer()
    assert normalizer.push(_DSML_CALL) == []

    segments = normalizer.finish(successful_text_tool_terminal=False)

    assert segments == [
        RejectedTextToolSegment(
            dialect=TEXT_TOOL_DIALECT_DEEPSEEK_DSML,
            reason="dsml_incomplete",
            call_count=1,
        )
    ]


@pytest.mark.parametrize("native_first", [False, True])
@pytest.mark.parametrize("candidate", [_DSML_CALL, "<｜DSML｜tool_calls>bad"])
def test_native_call_scrubs_dsml_before_or_after_native_without_synthesis(
    native_first: bool,
    candidate: str,
) -> None:
    normalizer = _normalizer()
    visible: list[str] = []
    observed: list[object] = []
    if native_first:
        observed.extend(normalizer.observe_native_tool_start("search"))
    visible.extend(normalizer.push(candidate))
    if not native_first:
        observed.extend(normalizer.observe_native_tool_start("search"))

    segments = normalizer.finish(
        successful_text_tool_terminal=True,
        native_calls=[("search", {"query": "different"})],
    )

    assert visible == []
    assert observed == []
    assert segments == []


@pytest.mark.parametrize("successful_terminal", [False, True])
def test_native_start_at_every_dsml_split_never_releases_protocol_suffix(
    successful_terminal: bool,
) -> None:
    for split in range(len(_DSML_CALL) + 1):
        normalizer = _normalizer()
        visible = list(normalizer.push(_DSML_CALL[:split]))
        observed = normalizer.observe_native_tool_start("search")
        visible.extend(normalizer.push(_DSML_CALL[split:]))
        segments = normalizer.finish(
            successful_text_tool_terminal=successful_terminal,
            native_calls=[("search", {"query": "native"})],
        )

        assert visible == [], split
        assert observed == [], split
        assert segments == [], split


def test_native_dsml_ownership_survives_deferred_queue_abandon() -> None:
    split = len("<｜DSML｜tool_calls>") + 5
    normalizer = _normalizer()

    assert normalizer.push(_DSML_CALL[:split]) == []
    assert normalizer.observe_native_tool_start("search") == []
    assert normalizer.abandon_native_lifecycle_defer() == []
    assert normalizer.push(_DSML_CALL[split:]) == []
    assert normalizer.finish(
        successful_text_tool_terminal=True,
        native_calls=[("search", {"query": "native"})],
    ) == []


def test_native_first_queue_abandon_does_not_disable_later_dsml_ownership() -> None:
    normalizer = _normalizer()

    assert normalizer.observe_native_tool_start("search") == []
    assert normalizer.abandon_native_lifecycle_defer() == []
    assert normalizer.push(_DSML_CALL) == []
    assert normalizer.finish(
        successful_text_tool_terminal=True,
        native_calls=[("search", {"query": "native"})],
    ) == []
