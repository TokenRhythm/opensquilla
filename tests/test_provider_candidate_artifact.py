"""Unit contract for inert proposer tool-output assembly."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from opensquilla.provider.candidate_artifact import (
    CandidateArtifactBuilder,
    CandidateArtifactLimitError,
    strip_candidate_tool_identity,
)
from opensquilla.provider.types import ChatConfig


def test_candidate_output_mode_is_internal_and_literal_typed() -> None:
    config = ChatConfig(candidate_output_mode="inert_artifact")

    assert config.candidate_output_mode == "inert_artifact"
    assert "candidate_output_mode" not in config.model_dump()
    assert "candidate_output_mode" not in repr(config)

    with pytest.raises(ValidationError):
        ChatConfig(candidate_output_mode="execute")  # type: ignore[arg-type]


def test_streamed_action_renders_canonical_inert_json_without_id() -> None:
    builder = CandidateArtifactBuilder()

    assert builder.start("private-call-id", name_text="web.") is None
    assert builder.append_name("private-call-id", "search") is None
    assert builder.append_arguments("private-call-id", '{"query":') is None
    assert builder.append_arguments("private-call-id", '"Shanghai"}') is None
    assert builder.finish("private-call-id") is None

    rendered = builder.render_text()
    assert rendered == builder.render()
    assert rendered == (
        '\n{"actions":[{"arguments_text":"{\\\"query\\\":\\\"Shanghai\\\"}",'
        '"issues":[],"name_text":"web.search"}],"executable":false,'
        '"kind":"inert_proposer_tool_output"}'
    )
    assert "private-call-id" not in rendered
    assert builder.has_calls
    assert builder.has_content
    assert builder.call_count == 1
    assert builder.event_count == 5
    assert builder.char_count == len('web.search{"query":"Shanghai"}')
    assert builder.issue_codes == ()


def test_whole_call_observation_canonicalizes_objects_without_executable_args() -> None:
    builder = CandidateArtifactBuilder()
    builder.observe_call(
        0,
        name_text="weather",
        arguments={"units": "c", "city": "上海"},
    )

    artifact = json.loads(builder.render_text())

    assert artifact == {
        "actions": [
            {
                "arguments_text": '{"city":"上海","units":"c"}',
                "issues": [],
                "name_text": "weather",
            }
        ],
        "executable": False,
        "kind": "inert_proposer_tool_output",
    }
    assert not isinstance(artifact["actions"][0]["arguments_text"], dict)


def test_empty_arguments_are_a_valid_no_argument_advisory_call() -> None:
    builder = CandidateArtifactBuilder()
    builder.observe_call("no-args", name_text="get_status")

    (action,) = json.loads(builder.render_text())["actions"]

    assert action == {
        "arguments_text": "",
        "issues": [],
        "name_text": "get_status",
    }

    unfinished = CandidateArtifactBuilder()
    unfinished.start("no-args", name_text="get_status")

    (unfinished_action,) = json.loads(unfinished.render_text())["actions"]
    assert unfinished_action["issues"] == ["incomplete_call"]


def test_semantic_tool_problems_are_issues_not_failures() -> None:
    builder = CandidateArtifactBuilder()
    builder.observe_call("long", name_text="x" * 257, arguments={})
    builder.observe_call("missing", arguments={"query": "x"})
    builder.observe_call("invalid", name_text="broken", arguments="{no")
    builder.observe_call("non-object", name_text="listy", arguments=[1, 2])

    actions = json.loads(builder.render_text())["actions"]

    assert actions[0]["name_text"] == "x" * 257
    assert actions[0]["issues"] == ["name_over_execution_limit"]
    assert actions[1]["issues"] == ["missing_name"]
    assert actions[2]["arguments_text"] == "{no"
    assert actions[2]["issues"] == ["invalid_arguments_json"]
    assert actions[3]["arguments_text"] == "[1,2]"
    assert actions[3]["issues"] == ["non_object_arguments"]
    assert builder.issue_codes == (
        "invalid_arguments_json",
        "missing_name",
        "name_over_execution_limit",
        "non_object_arguments",
    )


def test_deep_or_recursive_arguments_degrade_without_escaping_provider_contract() -> None:
    builder = CandidateArtifactBuilder()
    builder.observe_call(
        "deep",
        name_text="deep",
        arguments=("[" * 10_000) + ("]" * 10_000),
    )
    recursive: list[object] = []
    recursive.append(recursive)
    builder.observe_call("recursive", name_text="recursive", arguments=recursive)

    actions = json.loads(builder.render_text())["actions"]

    assert actions[0]["issues"] == ["invalid_arguments_json"]
    assert actions[1]["arguments_text"] == "<unserializable:list>"
    assert actions[1]["issues"] == ["invalid_arguments_json"]


def test_object_serialization_stops_at_builder_character_limit() -> None:
    builder = CandidateArtifactBuilder(max_chars_per_call=32)

    with pytest.raises(CandidateArtifactLimitError) as caught:
        builder.observe_call(
            "wide",
            arguments={"payload": "x" * 100_000},
        )

    assert caught.value.reason == "call_chars_exceeded"
    assert caught.value.observed == 33
    assert builder.call_count == 0
    assert builder.char_count == 0


def test_malformed_wrapper_identity_stripping_is_recursive_and_bounded() -> None:
    recursive: dict[str, object] = {
        "id": "private-root-id",
        "value": "keep",
    }
    recursive["nested"] = {
        "tool_use_id": "private-nested-id",
        "cycle": recursive,
    }

    sanitized = strip_candidate_tool_identity(recursive)
    rendered = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)

    assert "private-root-id" not in rendered
    assert "private-nested-id" not in rendered
    assert '"value": "keep"' in rendered
    assert "<truncated:recursive_value>" in rendered

    deep: object = {"call_id": "private-deep-id", "value": "leaf"}
    for _ in range(100):
        deep = {"next": deep}
    deep_rendered = json.dumps(strip_candidate_tool_identity(deep))
    assert "private-deep-id" not in deep_rendered
    assert "<truncated:depth_limit>" in deep_rendered


def test_empty_calls_are_not_rendered() -> None:
    builder = CandidateArtifactBuilder()
    builder.start("empty")
    builder.finish("empty")
    builder.observe_call("also-empty", name_text=" ", arguments=" ")
    builder.observe_call("empty-object", arguments={})
    builder.observe_call("empty-list", arguments=[])
    builder.observe_call("empty-null", arguments=None)

    assert builder.has_calls
    assert not builder.has_content
    assert builder.render_text() == ""
    assert builder.issue_codes == ()


def test_rendered_artifact_obeys_total_limit_after_json_escaping() -> None:
    builder = CandidateArtifactBuilder(max_total_chars=180)
    builder.observe_call("escaped", name_text="\0" * 20)

    assert builder.char_count == 20
    with pytest.raises(CandidateArtifactLimitError) as caught:
        builder.render_text()

    assert caught.value.operation == "render"
    assert caught.value.reason == "total_chars_exceeded"
    assert caught.value.limit == 180
    assert caught.value.observed > 180


def test_append_or_start_groups_fragments_and_tolerates_late_content() -> None:
    builder = CandidateArtifactBuilder()
    builder.append_or_start(3, name_fragment="run", arguments_fragment='{"x":')
    builder.append_or_start(3, arguments_fragment="1}")
    builder.finish(3)
    builder.append_arguments(3, " ")

    (action,) = json.loads(builder.render_text())["actions"]

    assert action["name_text"] == "run"
    assert action["arguments_text"] == '{"x":1} '
    assert action["issues"] == ["late_mutation"]


@pytest.mark.parametrize(
    ("builder", "operations", "reason", "limit", "observed"),
    [
        (
            CandidateArtifactBuilder(max_calls=1),
            [
                lambda builder: builder.observe_call(0, name_text="a", arguments={}),
                lambda builder: builder.observe_call(1, name_text="b", arguments={}),
            ],
            "too_many_calls",
            1,
            2,
        ),
        (
            CandidateArtifactBuilder(max_events=2),
            [
                lambda builder: builder.start(0, name_text="a"),
                lambda builder: builder.append_arguments(0, "{}"),
                lambda builder: builder.finish(0),
            ],
            "too_many_events",
            2,
            3,
        ),
        (
            CandidateArtifactBuilder(max_chars_per_call=3),
            [
                lambda builder: builder.start(0, name_text="abc"),
                lambda builder: builder.append_arguments(0, "d"),
            ],
            "call_chars_exceeded",
            3,
            4,
        ),
        (
            CandidateArtifactBuilder(max_total_chars=3),
            [
                lambda builder: builder.start(0, name_text="ab"),
                lambda builder: builder.start(1, name_text="c"),
                lambda builder: builder.append_name(1, "d"),
            ],
            "total_chars_exceeded",
            3,
            4,
        ),
    ],
)
def test_hard_limits_raise_structured_errors_before_retaining_overflow(
    builder: CandidateArtifactBuilder,
    operations: list[object],
    reason: str,
    limit: int,
    observed: int,
) -> None:
    *setup, overflowing = operations
    for operation in setup:
        operation(builder)  # type: ignore[operator]

    before = (builder.call_count, builder.event_count, builder.char_count)
    with pytest.raises(CandidateArtifactLimitError) as caught:
        overflowing(builder)  # type: ignore[operator]

    assert caught.value.reason == reason
    assert caught.value.limit == limit
    assert caught.value.observed == observed
    assert (builder.call_count, builder.event_count, builder.char_count) == before


def test_constructor_rejects_nonpositive_limits() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        CandidateArtifactBuilder(max_events=0)
