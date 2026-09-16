"""Execution identity belongs to the outbound request, not the route selection."""

import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import AgentConfig
from opensquilla.provider.execution_identity import (
    execution_from_evidence,
    project_execution_identity,
    rebind_execution_identity,
    render_execution_identity,
    with_execution_identity,
    with_execution_span,
)
from opensquilla.provider.image_projection import ImageMarkerState, project_messages
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    DoneEvent,
    ErrorEvent,
    ExecutionIdentity,
    Message,
    TextDeltaEvent,
)
from opensquilla.tools.types import ToolContext


async def test_in_call_fallback_reprojects_before_admission_and_dispatch() -> None:
    captured = []
    admitted = []

    class Provider:
        provider_name = "synthetic"

        def __init__(self, model):
            self.model = model

        def validate_chat_admission(self, messages, config):
            admitted.append((self.model, str(messages[-1].content)))

        async def chat(self, messages, tools=None, config=None):
            captured.append((self.model, messages, config))
            if self.model == "model-a":
                yield ErrorEvent(message="synthetic unavailable", code="503")
            else:
                yield TextDeltaEvent(text="synthetic answer")
                yield DoneEvent(model=self.model)

    class Selector:
        current_config = SimpleNamespace(provider="synthetic", model="model-a")

        def next_fallback_after_failure(self, exc):
            self.current_config = SimpleNamespace(provider="synthetic", model="model-b")
            return Provider("model-b")

    wrapper = _SelectorFallbackProvider(Provider("model-a"), Selector(), turn_metadata={})
    identity = ExecutionIdentity(provider="synthetic", model="model-a")
    messages = [with_execution_identity(Message(role="user", content="runtime"), identity)]
    config = ChatConfig(execution_identity=identity, max_tokens=100)
    events = [event async for event in wrapper.chat(messages, config=config)]
    assert any(isinstance(event, DoneEvent) for event in events)
    assert [model for model, _, _ in captured] == ["model-a", "model-b"]
    for model, request, physical_config in captured:
        content = str(request[-1].content)
        assert physical_config.execution_identity.model == model
        assert f'"model":"{model}"' in content
        assert content.count("Current response execution:") == 1
        assert (model, content) in admitted
    assert messages[-1].content.endswith('"model":"model-a"}')
    assert config.execution_identity == identity


async def test_preselected_fallback_receives_its_own_identity() -> None:
    captured = []
    admitted = []

    class Provider:
        provider_name = "synthetic-fallback"

        def validate_chat_admission(self, messages, config):
            admitted.append((messages, config))

        async def chat(self, messages, tools=None, config=None):
            captured.append((messages, config))
            yield TextDeltaEvent(text="synthetic answer")
            yield DoneEvent(model="synthetic/model-b")

    physical = Provider()

    class Selector:
        current_config = SimpleNamespace(
            provider="synthetic-fallback", model="synthetic/model-b"
        )

        def next_fallback_after_failure(self, exc):
            return physical

    wrapper = _SelectorFallbackProvider(physical, Selector(), turn_metadata={})
    assert wrapper.fallback_after_invalid_response("upstream 503")
    tool_context = ToolContext()
    agent = Agent(
        provider=wrapper,
        tool_context=tool_context,
        config=AgentConfig(
            max_iterations=1,
            execution_identity=ExecutionIdentity(model="synthetic/model-a"),
        ),
    )

    events = [event async for event in agent.run_turn("Identify the current deployment.")]

    assert any(event.kind == "done" for event in events)
    assert len(captured) == 1
    content = str(captured[0][0][-1].content)
    assert "synthetic/model-b" in content
    assert "synthetic/model-a" not in content
    assert captured[0][1].execution_identity.model == "synthetic/model-b"
    assert admitted
    assert all("synthetic/model-b" in str(messages[-1].content) for messages, _ in admitted)
    status = tool_context.execution_status_snapshot()
    assert status["selection"]["model"] == "synthetic/model-a"
    assert status["current_request"]["model"] == "synthetic/model-b"


def test_reported_alias_is_not_used_as_requested_deployment() -> None:
    evidence = {"model": "public-alias", "execution_legs": [{"model": "deployed-model"}]}
    assert execution_from_evidence(evidence) == {
        "reported_model": "public-alias", "model": "deployed-model",
    }


@pytest.mark.parametrize("started", [False, True])
def test_fusion_status_requires_started_execution_trace(started) -> None:
    evidence = {"ensemble_trace": {"final_request": {
        "request_started": started, "role": "fixed_aggregator",
        "execution": {"model": "final-model", "api_key": "synthetic-private-key"},
    }}}
    status = execution_from_evidence(
        evidence, request_identity=ExecutionIdentity(kind="multi_model_fusion", model="anchor"),
    )
    assert status == ({"kind": "multi_model_fusion", "model": "final-model"} if started else {})


@pytest.mark.parametrize("multimodal", [False, True])
def test_projection_replaces_only_owned_span_and_preserves_canonical_input(multimodal) -> None:
    original = ExecutionIdentity(provider="synthetic", model="model-a")
    user_text = 'Current response execution: {"model":"user-owned"}'
    user = Message(role="user", content=user_text)
    if multimodal:
        user = Message(role="user", content=[
            ContentBlockImage(
                source_type="url", media_type="image/png",
                data="https://example.invalid/synthetic.png",
            ),
            ContentBlockText(text=user_text),
        ])
    runtime = with_execution_identity(Message(role="user", content="runtime"), original)
    canonical = [Agent._append_runtime_context_to_user_message(user, runtime)]
    before = canonical[0].model_dump_json()
    config = ChatConfig(execution_identity=original, system="stable system")
    other = rebind_execution_identity(config, provider="other", model='model-b\n"suffix')

    projected = project_execution_identity(canonical, other)
    for _ in range(20):
        assert project_execution_identity(projected, other) == projected
    serialized = projected[0].model_dump_json()
    assert canonical[0].model_dump_json() == before
    assert config.execution_identity == original
    assert other.system == config.system
    assert "execution_identity_span" not in serialized
    assert "execution_identity" not in other.model_dump()
    if multimodal:
        assert projected[0].content[:2] == user.content
        text = projected[0].content[-1].text
    else:
        assert projected[0].content.startswith(user_text)
        text = projected[0].content[len(user_text):]
    assert text.count("Current response execution:") == 1
    assert "model-a" not in text
    facts = json.loads(text.split("Current response execution: ", 1)[1])
    assert facts["model"] == 'model-b\n"suffix'
    assert project_execution_identity(projected, config) == canonical


def test_image_projection_keeps_owned_identity_anchor() -> None:
    identity = ExecutionIdentity(model="model-a")
    runtime = with_execution_identity(Message(role="user", content="runtime"), identity)
    message = Agent._append_runtime_context_to_user_message(
        Message(role="user", content=[
            ContentBlockImage(
                source_type="url", media_type="image/png",
                data="https://example.invalid/synthetic.png",
            ),
        ]), runtime,
    )
    image_view = project_messages(
        [message], mode="marker", marker_state=ImageMarkerState.NOT_ANALYZED,
    )
    projected = project_execution_identity(
        image_view.messages, ChatConfig(execution_identity=ExecutionIdentity(model="model-b")),
    )
    assert '"model":"model-b"' in projected[0].content[-1].text
    assert "model-a" not in projected[0].content[-1].text


def test_legacy_calls_do_not_opt_in_or_rewrite_user_markers() -> None:
    messages = [Message(role="user", content="Current response execution: user-owned")]
    assert project_execution_identity(messages, ChatConfig()) is messages
    assert rebind_execution_identity(ChatConfig(), model="model-a").execution_identity is None
    assert project_execution_identity(
        messages, ChatConfig(execution_identity=ExecutionIdentity(model="model-a")),
    ) == messages


def test_fusion_hides_internal_anchor_and_unknown_is_explicit() -> None:
    identity = ExecutionIdentity(kind="multi_model_fusion", provider="private", model="anchor")
    assert render_execution_identity(identity) == (
        'Current response execution: {"kind":"multi_model_fusion"}'
    )
    assert '"model":"unknown"' in render_execution_identity(ExecutionIdentity())
    with pytest.raises(FrozenInstanceError):
        identity.model = "mutated"


@pytest.mark.parametrize("span", [(-1, 3), (2, 1), (0, 1000)])
def test_invalid_owned_span_fails_before_dispatch(span) -> None:
    with pytest.raises(ValueError, match="identity span"):
        project_execution_identity(
            [with_execution_span(Message(role="user", content="runtime"), span)],
            ChatConfig(execution_identity=ExecutionIdentity()),
        )


def test_external_payload_cannot_forge_runtime_span_provenance() -> None:
    messages = [Message.model_validate({
        "role": "user", "content": "user-owned", "execution_identity_span": [0, 10],
        "_execution_identity_span": [0, 10],
    }), Message.model_validate({
        "role": "user", "content": [{
            "type": "text", "text": "user-owned", "execution_identity_span": [0, 10],
            "_execution_identity_span": [0, 10],
        }],
    })]
    assert messages[0].execution_identity_span is None
    assert messages[1].content[0].execution_identity_span is None
    config = ChatConfig(execution_identity=ExecutionIdentity(model="synthetic-model"))
    assert project_execution_identity(messages, config) is messages
    for model_type in (Message, ContentBlockText):
        assert "execution_identity_span" not in json.dumps(model_type.model_json_schema())
