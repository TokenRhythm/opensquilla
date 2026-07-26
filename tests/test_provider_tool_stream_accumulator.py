"""Unit contract for ToolStreamAccumulator's grammar operations.

Each operation maps to provider stream grammar while requiring the adapter to
validate and supply canonical arguments before any call can close.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from opensquilla.provider.stream_assembly import (
    ToolStreamAccumulator,
    ToolStreamProtocolError,
)
from opensquilla.provider.types import (
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


def test_accumulator_has_no_implicit_parse_and_close_shortcut() -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="t1", tool_name="search")
    acc.append(0, '{"broken')

    assert acc.pending_raw_arguments() == [(0, "t1", "search", '{"broken')]
    assert not hasattr(acc, "finish")
    assert not hasattr(acc, "finish_all")


def test_identity_first_grammar() -> None:
    acc = ToolStreamAccumulator()
    events = acc.start(0, tool_use_id="toolu_1", tool_name="search")
    assert [type(e) for e in events] == [ToolUseStartEvent]
    events = acc.append(0, '{"q":')
    events += acc.append(0, ' "x"}')
    assert all(isinstance(e, ToolUseDeltaEvent) for e in events)
    assert all(e.tool_use_id == "toolu_1" for e in events)
    (end,) = acc.finish_with_arguments(0, {"q": "x"})
    assert isinstance(end, ToolUseEndEvent)
    assert end.arguments == {"q": "x"}
    assert end.tool_name == "search"


def test_append_or_start_freezes_public_id() -> None:
    acc = ToolStreamAccumulator()
    first = acc.append_or_start(0, tool_call_id=None, tool_name="search", fragment='{"a"')
    start = first[0]
    assert isinstance(start, ToolUseStartEvent)
    synthesized = start.tool_use_id
    assert synthesized.startswith("call_")
    # Late real id: wire only, public id unchanged.
    later = acc.append_or_start(0, tool_call_id="call_real", fragment=": 1}")
    assert all(e.tool_use_id == synthesized for e in later)
    (end,) = acc.finish_with_arguments(0, {"a": 1})
    assert end.tool_use_id == synthesized
    assert end.arguments == {"a": 1}
    # The wire id is still matchable for index resolution.
    assert acc.find_key_for_tool_call_id("call_real") == 0


def test_late_name_holds_start_and_deltas_until_identity_is_complete() -> None:
    acc = ToolStreamAccumulator()

    assert acc.start("item", tool_use_id="call_1", tool_name="") == []
    assert acc.append("item", '{"query":"x"}') == []
    assert acc.pending_unemitted_event_count == 2
    assert acc.pending_unemitted_char_count == len("call_1") + len('{"query":"x"}')
    released = acc.start("item", tool_use_id="call_1", tool_name="search")

    assert [type(event) for event in released] == [
        ToolUseStartEvent,
        ToolUseDeltaEvent,
    ]
    assert {event.tool_use_id for event in released} == {"call_1"}
    assert released[0].tool_name == "search"
    assert acc.pending_unemitted_event_count == 0
    assert acc.pending_unemitted_char_count == 0
    (end,) = acc.finish_with_arguments("item", {"query": "x"})
    assert end.tool_use_id == "call_1"
    assert end.tool_name == "search"


@pytest.mark.parametrize(
    ("second_id", "second_name", "reason"),
    [
        ("call_2", "search", "conflicting_tool_use_id"),
        ("call_1", "replace", "conflicting_tool_name"),
    ],
)
def test_repeated_start_rejects_conflicting_nonempty_identity(
    second_id: str,
    second_name: str,
    reason: str,
) -> None:
    acc = ToolStreamAccumulator()
    acc.start("item", tool_use_id="call_1", tool_name="search")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.start("item", tool_use_id=second_id, tool_name=second_name)

    assert caught.value.reason == reason
    assert caught.value.tool_use_id == "call_1"


@pytest.mark.parametrize(
    ("second_id", "second_name", "reason"),
    [
        ("call_2", "search", "conflicting_tool_use_id"),
        ("call_1", "replace", "conflicting_tool_name"),
    ],
)
def test_append_or_start_rejects_conflicting_nonempty_identity(
    second_id: str,
    second_name: str,
    reason: str,
) -> None:
    acc = ToolStreamAccumulator()
    acc.append_or_start(0, tool_call_id="call_1", tool_name="search")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.append_or_start(
            0,
            tool_call_id=second_id,
            tool_name=second_name,
        )

    assert caught.value.reason == reason


def test_parallel_calls_never_mix_fragments() -> None:
    acc = ToolStreamAccumulator()
    acc.append_or_start(0, tool_call_id="call_a", tool_name="alpha", fragment='{"a"')
    acc.append_or_start(1, tool_call_id="call_b", tool_name="beta", fragment='{"b"')
    acc.append_or_start(0, fragment=": 1}")
    acc.append_or_start(1, fragment=": 2}")
    ends = {
        event.tool_use_id: event
        for event in [
            *acc.finish_with_arguments(0, {"a": 1}),
            *acc.finish_with_arguments(1, {"b": 2}),
        ]
    }
    assert ends["call_a"].arguments == {"a": 1}
    assert ends["call_b"].arguments == {"b": 2}


def test_finish_with_arguments_is_idempotent() -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="t1", tool_name="a")
    acc.start(1, tool_use_id="t2", tool_name="b")
    assert len(acc.finish_with_arguments(0, {})) == 1
    assert acc.finish_with_arguments(0, {}) == []
    remaining = acc.finish_with_arguments(1, {})
    assert [e.tool_use_id for e in remaining] == ["t2"]
    assert acc.finish_with_arguments(1, {}) == []


def test_append_on_unknown_key_is_protocol_error() -> None:
    acc = ToolStreamAccumulator()

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.append(7, '{"x": 1}')

    assert caught.value.reason == "unknown_tool_call"
    assert not acc.has_calls


def test_finish_with_arguments_is_authoritative() -> None:
    acc = ToolStreamAccumulator()
    acc.start("item_1", tool_use_id="call_1", tool_name="run")
    acc.append("item_1", "not json at all")
    (end,) = acc.finish_with_arguments("item_1", {"cmd": "ls"})
    assert end.arguments == {"cmd": "ls"}


def test_closed_call_rejects_every_late_mutation() -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="call_1", tool_name="run")
    acc.finish_with_arguments(0, {"cmd": "ls"})

    operations = [
        lambda: acc.start(0, tool_use_id="call_1", tool_name="renamed"),
        lambda: acc.append_or_start(0, fragment='{"cmd":"late"}'),
        lambda: acc.append(0, '{"cmd":"late"}'),
        lambda: acc.set_metadata(0, "thought_signature", "late"),
    ]
    for operation in operations:
        with pytest.raises(ToolStreamProtocolError) as caught:
            operation()
        assert caught.value.tool_use_id == "call_1"


def test_zero_argument_call_yields_empty_dict() -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="t1", tool_name="ping")
    (end,) = acc.finish_with_arguments(0, {})
    assert end.arguments == {}


def test_metadata_and_key_queries() -> None:
    acc = ToolStreamAccumulator()
    assert acc.next_int_key() == 0
    acc.append_or_start(0, tool_call_id="call_a", tool_name="a")
    acc.set_metadata(0, "thought_signature", "sig-1")
    assert acc.first_metadata("thought_signature") == "sig-1"
    assert acc.first_metadata("missing") is None
    assert acc.single_key() == 0
    assert acc.next_int_key() == 1
    acc.append_or_start(3, tool_call_id="call_b", tool_name="b")
    assert acc.single_key() is None
    assert acc.next_int_key() == 4
    assert acc.find_key_for_tool_call_id("call_b") == 3
    assert acc.find_key_for_tool_call_id("nope") is None


def test_different_keys_cannot_reuse_one_public_tool_use_id() -> None:
    acc = ToolStreamAccumulator()
    acc.start("item-a", tool_use_id="call-1", tool_name="alpha")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.start("item-b", tool_use_id="call-1", tool_name="beta")

    assert caught.value.reason == "duplicate_tool_use_id"
    assert caught.value.key == "item-b"
    assert acc.pending_raw_arguments() == [("item-a", "call-1", "alpha", "")]


def test_different_keys_cannot_reuse_one_late_wire_id() -> None:
    acc = ToolStreamAccumulator()
    acc.append_or_start(0, tool_name="alpha")
    acc.append_or_start(1, tool_name="beta")
    acc.append_or_start(0, tool_call_id="wire-1")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.append_or_start(1, tool_call_id="wire-1")

    assert caught.value.reason == "duplicate_tool_use_id"
    assert caught.value.key == 1


def test_public_and_wire_ids_share_one_unambiguous_namespace() -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="shared", tool_name="alpha")
    acc.append_or_start(1, tool_name="beta")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.append_or_start(1, tool_call_id="shared")

    assert caught.value.reason == "duplicate_tool_use_id"


def test_indexless_key_allocation_is_monotonic_without_scanning_calls() -> None:
    acc = ToolStreamAccumulator()

    assert [acc.next_int_key() for _ in range(4)] == [0, 1, 2, 3]
    acc.start(20, tool_use_id="call-20", tool_name="run")
    assert acc.next_int_key() == 21


@pytest.mark.parametrize(
    ("limit_name", "expected_reason"),
    [
        ("max_argument_chars", "tool_arguments_too_large"),
        ("max_total_argument_chars", "total_tool_arguments_too_large"),
    ],
)
def test_argument_limits_reject_before_retaining_overflow(
    limit_name: str,
    expected_reason: str,
) -> None:
    limits = {
        "max_argument_chars": 100,
        "max_total_argument_chars": 100,
    }
    limits[limit_name] = 3
    acc = ToolStreamAccumulator(**limits)
    acc.start(0, tool_use_id="call-1", tool_name="run")
    acc.append(0, "abc")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.append(0, "d")

    assert caught.value.reason == expected_reason
    assert caught.value.limit == 3
    assert caught.value.observed == 4
    assert acc.pending_raw_arguments() == [(0, "call-1", "run", "abc")]


def test_total_argument_limit_spans_calls() -> None:
    acc = ToolStreamAccumulator(
        max_argument_chars=10,
        max_total_argument_chars=5,
    )
    acc.start(0, tool_use_id="call-1", tool_name="run")
    acc.append(0, "abc")
    acc.start(1, tool_use_id="call-2", tool_name="run")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.append(1, "xyz")

    assert caught.value.reason == "total_tool_arguments_too_large"
    assert caught.value.observed == 6
    assert acc.pending_raw_arguments()[1] == (1, "call-2", "run", "")


def test_call_limit_rejects_before_registering_extra_call() -> None:
    acc = ToolStreamAccumulator(max_calls=1)
    acc.start(0, tool_use_id="call-1", tool_name="run")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.start(1, tool_use_id="call-2", tool_name="run")

    assert caught.value.reason == "too_many_tool_calls"
    assert caught.value.observed == 2
    assert acc.find_key_for_tool_call_id("call-2") is None


def test_event_limit_counts_start_delta_and_end() -> None:
    acc = ToolStreamAccumulator(max_events=2)
    acc.start(0, tool_use_id="call-1", tool_name="run")
    acc.append(0, "{}")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.finish_with_arguments(0, {})

    assert caught.value.reason == "too_many_tool_events"
    assert caught.value.observed == 3
    assert acc.pending_raw_arguments() == [(0, "call-1", "run", "{}")]


def test_default_event_limit_accepts_many_small_argument_deltas() -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="call-1", tool_name="run")

    for _ in range(8_192):
        acc.append(0, "x")

    events = acc.finish_with_arguments(0, {"value": "x" * 8_192})

    assert isinstance(events[-1], ToolUseEndEvent)


def test_authoritative_arguments_cannot_bypass_limits_without_deltas() -> None:
    acc = ToolStreamAccumulator(max_argument_chars=8)
    acc.start(0, tool_use_id="call-1", tool_name="run")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.finish_with_arguments(0, {"value": "too large"})

    assert caught.value.reason == "tool_arguments_too_large"
    assert acc.pending_raw_arguments() == [(0, "call-1", "run", "")]


@pytest.mark.parametrize("arguments", [["not-object"], {"value": float("nan")}])
def test_finish_rejects_non_object_or_nonfinite_arguments(arguments: object) -> None:
    acc = ToolStreamAccumulator()
    acc.start(0, tool_use_id="call-1", tool_name="run")

    with pytest.raises(ToolStreamProtocolError) as caught:
        acc.finish_with_arguments(0, arguments)  # type: ignore[arg-type]

    assert caught.value.reason == "invalid_tool_arguments"


def test_finish_on_unknown_or_identity_incomplete_call_is_protocol_error() -> None:
    acc = ToolStreamAccumulator()
    with pytest.raises(ToolStreamProtocolError) as unknown:
        acc.finish_with_arguments("missing", {})
    assert unknown.value.reason == "unknown_tool_call"

    acc.start("held", tool_use_id="call-1", tool_name="")
    with pytest.raises(ToolStreamProtocolError) as incomplete:
        acc.finish_with_arguments("held", {})
    assert incomplete.value.reason == "incomplete_tool_identity"


@pytest.mark.parametrize(
    ("kwargs", "call", "reason"),
    [
        (
            {"max_tool_use_id_chars": 3},
            lambda acc: acc.start(0, tool_use_id="four", tool_name="run"),
            "tool_use_id_too_large",
        ),
        (
            {"max_tool_name_chars": 3},
            lambda acc: acc.start(0, tool_use_id="id", tool_name="four"),
            "tool_name_too_large",
        ),
        (
            {"max_tool_use_id_chars": 3},
            lambda acc: (
                acc.append_or_start(0, tool_name="run"),
                acc.append_or_start(0, tool_call_id="late"),
            ),
            "tool_use_id_too_large",
        ),
    ],
)
def test_identity_limits_cover_public_name_and_late_wire_values(
    kwargs: dict[str, int],
    call: Callable[[ToolStreamAccumulator], object],
    reason: str,
) -> None:
    acc = ToolStreamAccumulator(**kwargs)

    with pytest.raises(ToolStreamProtocolError) as caught:
        call(acc)

    assert caught.value.reason == reason
