"""Execution identity belongs to the outbound request, not the route selection."""

from types import SimpleNamespace

from opensquilla.engine.agent import Agent
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import AgentConfig
from opensquilla.provider.types import DoneEvent, TextDeltaEvent


async def test_preselected_fallback_receives_its_own_identity() -> None:
    captured = []

    class Provider:
        provider_name = "synthetic-fallback"

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
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_iterations=1,
            execution_identity_context=(
                "[Execution selected for this turn]\n"
                "execution_kind=single_model\nselected_model=synthetic/model-a"
            ),
        ),
    )

    events = [event async for event in agent.run_turn("Identify the current deployment.")]

    assert any(event.kind == "done" for event in events)
    assert len(captured) == 1
    content = str(captured[0][0][-1].content)
    assert "synthetic/model-b" in content
    assert "synthetic/model-a" not in content
