"""Inert ensemble-proposer handling for DeepSeek DSML text."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.provider.candidate_artifact import (
    CandidateArtifactBuilder,
    InertCandidateTextNormalizer,
)
from opensquilla.provider.compat_policy import TEXT_TOOL_DIALECT_DEEPSEEK_DSML
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.text_tool_normalizer import LiteralTextSegment
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

_DSML = """<｜DSML｜tool_calls>
<｜DSML｜invoke name="search">
<｜DSML｜parameter name="query" string="true">Shanghai</｜DSML｜parameter>
<｜DSML｜parameter name="limit" string="false">2</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>"""
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _normalizer(
    artifact: CandidateArtifactBuilder | None = None,
    *,
    max_candidate_chars: int = 256_000,
    enabled: bool = True,
) -> tuple[CandidateArtifactBuilder, InertCandidateTextNormalizer]:
    artifact = artifact or CandidateArtifactBuilder()
    dialects = (
        frozenset({TEXT_TOOL_DIALECT_DEEPSEEK_DSML})
        if enabled
        else frozenset()
    )
    return artifact, InertCandidateTextNormalizer(
        artifact=artifact,
        dialects=dialects,
        max_candidate_chars=max_candidate_chars,
    )


def _literal_text(segments: list[object]) -> str:
    assert all(isinstance(segment, LiteralTextSegment) for segment in segments)
    return "".join(segment.text for segment in segments)  # type: ignore[union-attr]


def _collect_provider_events(
    provider: OpenAIProvider,
) -> list[object]:
    async def run() -> list[object]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                tools=[],
                config=ChatConfig(candidate_output_mode="inert_artifact"),
            )
        ]

    return asyncio.run(run())


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.MockTransport,
) -> None:
    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = handler
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )


def _assert_only_inert_text(events: list[object]) -> dict[str, object]:
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    text = "".join(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    assert _DSML not in text
    assert any(isinstance(event, DoneEvent) for event in events)
    return json.loads(text)


def test_valid_chunked_dsml_becomes_one_non_executable_artifact() -> None:
    artifact, normalizer = _normalizer()
    text = f"Candidate rationale.\n{_DSML}"

    visible: list[str] = []
    for char in text:
        visible.extend(normalizer.push(char))
    segments = normalizer.finish(successful_text_tool_terminal=True)

    assert "".join(visible) + _literal_text(segments) == "Candidate rationale.\n"
    assert json.loads(artifact.render_text()) == {
        "actions": [
            {
                "arguments_text": '{"limit":2,"query":"Shanghai"}',
                "issues": [],
                "name_text": "search",
            }
        ],
        "executable": False,
        "kind": "inert_proposer_tool_output",
    }
    assert artifact.call_count == 1
    assert artifact.diagnostics == ()


def test_multiple_dsml_invokes_preserve_order_and_duplicates() -> None:
    duplicate_calls = """<｜DSML｜tool_calls>
<｜DSML｜invoke name="ping"></｜DSML｜invoke>
<｜DSML｜invoke name="ping"></｜DSML｜invoke>
</｜DSML｜tool_calls>"""
    artifact, normalizer = _normalizer()

    normalizer.push(duplicate_calls)
    assert normalizer.finish(successful_text_tool_terminal=True) == []

    actions = json.loads(artifact.render_text())["actions"]
    assert [action["name_text"] for action in actions] == ["ping", "ping"]
    assert [action["arguments_text"] for action in actions] == ["{}", "{}"]


@pytest.mark.parametrize(
    ("candidate", "diagnostic"),
    [
        (
            """<｜DSML｜tool_calls>
<｜DSML｜invoke name="search">
<｜DSML｜parameter name="query" string="true">a</｜DSML｜parameter>
<｜DSML｜parameter name="query" string="true">b</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>""",
            "dsml_malformed",
        ),
        ("<｜DSML｜tool_", "dsml_incomplete"),
        (
            """<｜DSML｜tool_calls>
<｜DSML｜invoke name="search">
<｜DSML｜parameter name="limit" string="false">NaN</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>""",
            "dsml_malformed",
        ),
    ],
)
def test_rejected_dsml_is_scrubbed_into_a_diagnostic_artifact(
    candidate: str,
    diagnostic: str,
) -> None:
    artifact, normalizer = _normalizer()
    raw = f"Safe prelude.\n{candidate}"

    visible = normalizer.push(raw)
    segments = normalizer.finish(successful_text_tool_terminal=True)

    assert "".join(visible) + _literal_text(segments) == "Safe prelude.\n"
    rendered = artifact.render_text()
    assert candidate not in rendered
    assert json.loads(rendered) == {
        "actions": [],
        "diagnostics": [diagnostic],
        "executable": False,
        "kind": "inert_proposer_tool_output",
    }


def test_recognized_candidate_over_limit_is_scrubbed_and_diagnosed() -> None:
    artifact, normalizer = _normalizer(max_candidate_chars=len(_DSML) - 1)

    assert normalizer.push(_DSML) == []
    assert normalizer.held_chars == 0
    assert normalizer.held_event_count == 1
    assert normalizer.finish(successful_text_tool_terminal=True) == []

    payload = json.loads(artifact.render_text())
    assert payload["actions"] == []
    assert payload["diagnostics"] == ["dsml_oversized"]
    assert _DSML not in artifact.render_text()


def test_candidate_limit_excludes_already_safe_prose_prefix() -> None:
    artifact, normalizer = _normalizer(max_candidate_chars=len(_DSML))
    prefix = "p" * 32 + "\n"

    assert normalizer.push(prefix + _DSML) == [prefix]
    assert normalizer.held_chars == len(_DSML)
    assert normalizer.finish(successful_text_tool_terminal=True) == []
    assert json.loads(artifact.render_text())["actions"][0]["name_text"] == "search"


def test_candidate_limit_preserves_every_split_dsml_prefix_after_long_prose() -> None:
    opener = "<｜DSML｜tool_calls>"
    max_candidate_chars = len(_DSML)
    safe_prefix = "p" * max_candidate_chars + "\n"

    for split in range(len(opener) + 1):
        artifact, normalizer = _normalizer(
            max_candidate_chars=max_candidate_chars
        )
        visible = normalizer.push(safe_prefix + opener[:split])
        visible.extend(normalizer.push(_DSML[split:]))
        segments = normalizer.finish(successful_text_tool_terminal=True)

        assert "".join(visible) + _literal_text(segments) == safe_prefix, split
        payload = json.loads(artifact.render_text())
        assert [action["name_text"] for action in payload["actions"]] == [
            "search"
        ], split
        assert "diagnostics" not in payload, split


def test_inert_dsml_call_count_matches_native_256_call_limit() -> None:
    invoke = '<｜DSML｜invoke name="ping"></｜DSML｜invoke>'
    allowed_text = f"<｜DSML｜tool_calls>{invoke * 256}</｜DSML｜tool_calls>"
    rejected_text = f"<｜DSML｜tool_calls>{invoke * 257}</｜DSML｜tool_calls>"

    allowed_artifact, allowed_normalizer = _normalizer()
    allowed_normalizer.push(allowed_text)
    assert allowed_normalizer.finish(successful_text_tool_terminal=True) == []
    assert len(json.loads(allowed_artifact.render_text())["actions"]) == 256

    rejected_artifact, rejected_normalizer = _normalizer()
    rejected_normalizer.push(rejected_text)
    assert rejected_normalizer.finish(successful_text_tool_terminal=True) == []
    assert json.loads(rejected_artifact.render_text()) == {
        "actions": [],
        "diagnostics": ["dsml_oversized"],
        "executable": False,
        "kind": "inert_proposer_tool_output",
    }


def test_ordinary_prose_and_documentation_remain_literal() -> None:
    prose = "Explain DSML without a call."
    documentation = f"```xml\n{_DSML}\n```\n"
    artifact, normalizer = _normalizer()

    visible = normalizer.push(prose)
    visible.extend(normalizer.push(documentation))
    segments = normalizer.finish(successful_text_tool_terminal=True)

    assert "".join(visible) + _literal_text(segments) == prose + documentation
    assert artifact.render_text() == ""


def test_unauthorized_dialect_is_immediate_literal_passthrough() -> None:
    artifact, normalizer = _normalizer(enabled=False)

    assert normalizer.push(_DSML) == [_DSML]
    assert normalizer.finish(successful_text_tool_terminal=True) == []
    assert artifact.render_text() == ""


def test_ordinary_prose_over_candidate_bound_is_released_without_diagnostic() -> None:
    artifact, normalizer = _normalizer(max_candidate_chars=8)

    assert normalizer.push("ordinary prose") == ["ordinary prose"]
    assert normalizer.push(" continues") == [" continues"]
    assert normalizer.finish(successful_text_tool_terminal=True) == []
    assert artifact.render_text() == ""


@pytest.mark.parametrize(
    "chunks",
    [
        ("```xml\n" + "x" * 300, f"\n{_DSML}\n```\n"),
        ("<pre>" + "x" * 300, f"\n{_DSML}\n</pre>"),
        ("ordinary prose " + "x" * 300, _DSML),
    ],
    ids=["fence", "raw_html", "same_line"],
)
def test_released_prose_preserves_context_for_later_dsml_documentation(
    chunks: tuple[str, str],
) -> None:
    artifact, normalizer = _normalizer(max_candidate_chars=len(_DSML) + 10)

    visible = normalizer.push(chunks[0])
    visible.extend(normalizer.push(chunks[1]))
    segments = normalizer.finish(successful_text_tool_terminal=True)

    assert "".join(visible) + _literal_text(segments) == "".join(chunks)
    assert artifact.render_text() == ""


def test_native_candidate_call_is_authoritative_over_dsml() -> None:
    artifact, normalizer = _normalizer()
    visible = normalizer.push(f"Prelude.\n{_DSML}")
    assert normalizer.observe_native_tool_start("native_search") == []
    artifact.observe_call(
        "private-native-id",
        name_text="native_search",
        arguments={"query": "native"},
    )

    segments = normalizer.finish(
        successful_text_tool_terminal=True,
        native_calls=[("native_search", {"query": "native"})],
    )

    assert "".join(visible) + _literal_text(segments) == "Prelude.\n"
    payload = json.loads(artifact.render_text())
    assert [action["name_text"] for action in payload["actions"]] == [
        "native_search"
    ]
    assert "diagnostics" not in payload
    assert _DSML not in artifact.render_text()


def test_unsuccessful_terminal_never_promotes_complete_dsml() -> None:
    artifact, normalizer = _normalizer()
    normalizer.push(_DSML)

    assert normalizer.finish(successful_text_tool_terminal=False) == []

    assert json.loads(artifact.render_text())["diagnostics"] == [
        "dsml_incomplete"
    ]
    assert artifact.call_count == 0


def test_render_limit_failure_is_atomic_and_becomes_oversized_diagnostic() -> None:
    artifact = CandidateArtifactBuilder(max_total_chars=160)
    _, normalizer = _normalizer(artifact)
    candidate = f"""<｜DSML｜tool_calls>
<｜DSML｜invoke name="run">
<｜DSML｜parameter name="payload" string="true">{'\0' * 20}</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>"""

    normalizer.push(candidate)
    normalizer.finish(successful_text_tool_terminal=True)

    payload = json.loads(artifact.render_text())
    assert payload["actions"] == []
    assert payload["diagnostics"] == ["dsml_oversized"]
    assert artifact.call_count == 0


@pytest.mark.parametrize(
    ("content", "expected_actions", "expected_diagnostics"),
    [
        (_DSML, ["search"], None),
        ("<｜DSML｜tool_", [], ["dsml_incomplete"]),
    ],
)
def test_openai_stream_candidate_mode_never_emits_tool_events(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected_actions: list[str],
    expected_diagnostics: list[str] | None,
) -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"content": content}}]},
        {
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}
            ]
        },
    ]
    body = b"".join(
        f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode()
        for frame in frames
    ) + b"data: [DONE]\n\n"
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )
    )
    _patch_transport(monkeypatch, transport)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        provider_kind="openrouter",
    )

    payload = _assert_only_inert_text(_collect_provider_events(provider))

    assert [action["name_text"] for action in payload["actions"]] == expected_actions
    assert payload.get("diagnostics") == expected_diagnostics
    assert payload["executable"] is False


def test_openai_non_stream_candidate_mode_uses_same_inert_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if json.loads(request.content).get("stream"):
            raise httpx.ReadTimeout("force non-stream fallback")
        return httpx.Response(
            200,
            json={
                "model": "deepseek/deepseek-v4-flash",
                "choices": [
                    {
                        "message": {"content": _DSML},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            },
        )

    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    payload = _assert_only_inert_text(_collect_provider_events(provider))

    assert calls == 2
    assert [action["name_text"] for action in payload["actions"]] == ["search"]
    assert payload["executable"] is False


def test_diagnostics_are_optional_sorted_and_deduplicated() -> None:
    artifact = CandidateArtifactBuilder()
    artifact.add_diagnostic("dsml_oversized")
    artifact.add_diagnostic("dsml_malformed")
    artifact.add_diagnostic("dsml_incomplete")
    artifact.add_diagnostic("dsml_malformed")

    payload = json.loads(artifact.render_text())
    assert payload["diagnostics"] == [
        "dsml_incomplete",
        "dsml_malformed",
        "dsml_oversized",
    ]
    assert artifact.diagnostics == (
        "dsml_incomplete",
        "dsml_malformed",
        "dsml_oversized",
    )

    with pytest.raises(ValueError, match="unsupported"):
        artifact.add_diagnostic("raw_protocol")  # type: ignore[arg-type]
