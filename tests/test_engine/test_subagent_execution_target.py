"""Subagent physical deployment binding and bounded task handoff."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.subagent import (
    SubagentExecutionTarget,
    SubagentManager,
    SubagentSpec,
    render_subagent_task_reference,
    subagent_task_inline_limit_bytes,
    subagent_task_reference_slice_limit_chars,
)
from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.engine.types import ToolResult
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider.model_catalog import shared_catalog
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig


class _ModelProvider:
    provider_name = "fake"
    provider_id = "fake"
    provider_kind = "openai_compat"

    def __init__(self, model: str) -> None:
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield DoneEvent(stop_reason="stop", model=self._model)

    async def list_models(self) -> list[Any]:
        return []


class _OpaqueProvider:
    provider_name = "fake"
    provider_id = "fake"

    def chat(self, messages, tools=None, config=None):
        del messages, tools, config
        return self._stream()

    async def _stream(self):
        yield DoneEvent(stop_reason="stop")

    async def list_models(self) -> list[Any]:
        return []


def test_subagent_model_override_binds_child_provider_window_and_compaction_plan() -> None:
    parent_provider = _ModelProvider("parent-model")
    parent = Agent(
        provider=parent_provider,
        config=AgentConfig(
            provider_id="fake",
            model_id="parent-model",
            context_window_tokens=100_000,
            max_tokens=4096,
            provider_request_proof_max_chars=200_000,
        ),
    )

    child = parent._make_child_agent(
        SubagentSpec(task="inspect this", model_id="child-model"),
        depth=1,
    )

    assert child.provider is not parent_provider
    assert child.provider.model == "child-model"
    assert child.config.provider_id == "fake"
    assert child.config.model_id == "child-model"
    assert child.config.context_window_tokens == 32_768
    assert child.config.max_tokens == shared_catalog().resolve_max_tokens(
        "child-model",
        provider="fake",
    )
    assert child.config.provider_request_proof_max_chars > 0
    plan = child.config.compaction_execution_plan
    assert plan is not None
    assert plan.primary.provider is child.provider
    assert plan.primary.provider_id == "fake"
    assert plan.primary.model == "child-model"
    assert plan.primary.context_window_tokens == child.config.context_window_tokens


def test_subagent_inherits_current_physical_model_vision_support() -> None:
    class _ActiveVisionProvider(_ModelProvider):
        def active_model_vision_support(self, config: Any) -> str:
            assert config.model_vision_support == "supported"
            return "supported"

    parent = Agent(
        provider=_ActiveVisionProvider("vision-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="vision-model",
            context_window_tokens=32_768,
            max_tokens=4096,
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )

    child = parent._make_child_agent(SubagentSpec(task="inspect this"), depth=1)

    assert child.config.model_vision_support == "supported"
    assert child._image_analysis_target() is not None


@pytest.mark.asyncio
async def test_subagent_image_tool_uses_bound_physical_provider(
    tmp_path: Path,
) -> None:
    from PIL import Image

    import opensquilla.tools  # noqa: F401
    from opensquilla.provider import (
        ProviderRequestCorrelation,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
    )
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import CallerKind, ToolContext

    Image.new("RGB", (2, 2)).save(tmp_path / "sample.png")
    main_calls: list[list[Message]] = []
    auxiliary_calls: list[tuple[list[Message], ChatConfig]] = []

    class _VisionToolProvider(_ModelProvider):
        def active_model_vision_support(self, config: Any) -> str:
            assert config.model_vision_support == "supported"
            return "supported"

        async def _chat(
            self,
            messages: list[Message],
            tools: list[Any] | None,
            config: ChatConfig,
        ) -> AsyncIterator[Any]:
            correlation = config.provider_request_correlation
            if correlation is not None and correlation.call_kind == "auxiliary.media":
                assert tools is None
                auxiliary_calls.append((messages, config))
                yield TextDeltaEvent(text="Child image description")
                yield DoneEvent(
                    model=self.model,
                    input_tokens=7,
                    output_tokens=3,
                    billed_cost=0.25,
                    cost_source="provider_billed",
                )
                return

            main_calls.append(messages)
            if len(main_calls) == 1:
                yield ToolUseStartEvent(tool_use_id="child-image", tool_name="image")
                yield ToolUseEndEvent(
                    tool_use_id="child-image",
                    tool_name="image",
                    arguments={
                        "path": "sample.png",
                        "prompt": "Describe the image",
                    },
                )
                yield DoneEvent(
                    stop_reason="tool_use",
                    model=self.model,
                    input_tokens=5,
                    output_tokens=1,
                )
                return

            yield TextDeltaEvent(text="Finished")
            yield DoneEvent(
                model=self.model,
                input_tokens=5,
                output_tokens=1,
            )

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            assert config is not None
            return self._chat(messages, tools, config)

    registry = get_default_registry()
    parent_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        workspace_dir=str(tmp_path),
        allowed_tools={"image"},
        surfaced_tools={"image"},
    )
    authorized = registry.to_tool_definitions(parent_context)
    model_surface = registry.to_model_tool_definitions(authorized, parent_context)
    parent = Agent(
        provider=_VisionToolProvider("vision-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="vision-model",
            context_window_tokens=32_768,
            max_tokens=4096,
            workspace_dir=str(tmp_path),
            model_capabilities=ModelCapabilities(
                supports_tools=True,
                supports_vision=True,
            ),
            model_vision_support="supported",
        ),
        tool_definitions=model_surface,
        tool_handler=build_tool_handler(registry, parent_context),
        tool_registry=registry,
        tool_context=parent_context,
        provider_request_correlation=ProviderRequestCorrelation(
            session_id="test-session",
            turn_id="test-turn",
            execution_id="parent-execution",
            call_kind="primary",
        ),
    )

    child = parent._make_child_agent(SubagentSpec(task="inspect this"), depth=1)
    events = [event async for event in child.run_turn("Inspect sample.png")]

    assert len(main_calls) == 2
    assert len(auxiliary_calls) == 1
    assert auxiliary_calls[0][1].physical_attempt_limit == 1
    assert "Child image description" in str(main_calls[1])
    done = next(event for event in reversed(events) if event.kind == "done")
    assert done.text == "Finished"
    assert done.input_tokens == 17
    assert done.output_tokens == 5
    assert done.billed_cost == 0.25
    assert child._tool_context is not None
    assert child._tool_context.image_analysis_target is None


def test_subagent_active_vision_resolver_overrides_stale_parent_config() -> None:
    class _ActiveTextProvider(_ModelProvider):
        def active_model_vision_support(self, config: Any) -> str:
            assert config.model_vision_support == "supported"
            return "unsupported"

    parent = Agent(
        provider=_ActiveTextProvider("shared-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="shared-model",
            context_window_tokens=32_768,
            max_tokens=4096,
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )

    child = parent._make_child_agent(SubagentSpec(task="inspect this"), depth=1)

    assert child.config.model_vision_support == "unsupported"
    assert child._image_analysis_target() is None


def test_subagent_active_vision_resolver_failure_stays_unknown() -> None:
    class _FailingVisionProvider(_ModelProvider):
        def active_model_vision_support(self, config: Any) -> str:
            del config
            raise RuntimeError("active deployment unavailable")

    parent = Agent(
        provider=_FailingVisionProvider("shared-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="shared-model",
            context_window_tokens=32_768,
            max_tokens=4096,
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )

    assert parent._image_analysis_target() is None
    child = parent._make_child_agent(SubagentSpec(task="inspect this"), depth=1)

    assert child.config.model_vision_support == "unknown"
    assert child._image_analysis_target() is None


def test_subagent_model_override_uses_exact_child_vision_evidence(monkeypatch) -> None:
    class _VisionCatalog:
        def resolve_entry(self, model: str, *, provider: str):
            assert (model, provider) == ("child-model", "fake")
            return SimpleNamespace(context_window=32_768)

        def resolve_max_tokens(
            self,
            model: str,
            user_override: int = 0,
            provider: str = "",
        ) -> int:
            assert (model, user_override, provider) == ("child-model", 0, "fake")
            return 4096

        def get_capabilities(
            self,
            model: str,
            *,
            provider_name: str,
            base_url: str,
        ) -> ModelCapabilities:
            assert (model, provider_name, base_url) == ("child-model", "fake", "")
            return ModelCapabilities(supports_vision=False)

        def resolve_deployment_vision_support(
            self,
            model: str,
            *,
            provider: str,
            api_key: str = "",
            base_url: str = "",
        ) -> str:
            assert (model, provider, api_key, base_url) == (
                "child-model",
                "fake",
                "",
                "",
            )
            return "unknown"

    monkeypatch.setattr(
        "opensquilla.provider.model_catalog.shared_catalog",
        lambda: _VisionCatalog(),
    )
    parent = Agent(
        provider=_ModelProvider("parent-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="parent-model",
            context_window_tokens=100_000,
            max_tokens=4096,
            model_vision_support="supported",
        ),
    )

    child = parent._make_child_agent(
        SubagentSpec(task="inspect this", model_id="child-model"),
        depth=1,
    )

    assert child.config.model_vision_support == "unknown"
    assert child.config.model_capabilities.supports_vision is False
    assert child._image_analysis_target() is None


@pytest.mark.asyncio
async def test_subagent_inherits_a_working_progressive_tool_index() -> None:
    import opensquilla.tools  # noqa: F401
    from opensquilla.engine.types import ToolCall
    from opensquilla.tools.dispatch import build_tool_handler
    from opensquilla.tools.registry import get_default_registry
    from opensquilla.tools.types import CallerKind, ToolContext

    registry = get_default_registry()
    parent_context = ToolContext(is_owner=True, caller_kind=CallerKind.AGENT)
    authorized = registry.to_tool_definitions(parent_context)
    model_surface = registry.to_model_tool_definitions(authorized, parent_context)
    parent = Agent(
        provider=_ModelProvider("parent-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="parent-model",
            context_window_tokens=100_000,
            max_tokens=4096,
        ),
        tool_definitions=model_surface,
        tool_handler=build_tool_handler(registry, parent_context),
        tool_registry=registry,
        tool_context=parent_context,
    )

    child = parent._make_child_agent(SubagentSpec(task="inspect this"), depth=1)
    result = await child._execute_tool(
        ToolCall(
            tool_use_id="child-search",
            tool_name="tool_search",
            arguments={"query": "list directory contents"},
        )
    )

    assert child._tool_context is not None
    assert child._tool_context.tool_search_index is not None
    assert result.is_error is False
    assert "list_dir" in child._tool_context.disclosed_tool_names
    assert "list_dir" in {tool.name for tool in child.tool_definitions}


def test_selector_fallback_subagent_freezes_active_chain_and_model_override() -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="ollama",
                model="configured-model",
                base_url="http://127.0.0.1:11434",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="ollama",
                    model="active-model",
                    base_url="http://127.0.0.1:11434",
                ),
                ProviderConfig(
                    provider="ollama",
                    model="remaining-model",
                    base_url="http://127.0.0.1:11434",
                ),
            ],
        )
    )
    selector.next_fallback()
    parent_provider = _SelectorFallbackProvider(selector.resolve(), selector)
    parent = Agent(
        provider=parent_provider,
        config=AgentConfig(
            provider_id="ollama",
            model_id="configured-model",
            context_window_tokens=100_000,
            max_tokens=4096,
        ),
    )

    child = parent._make_child_agent(
        SubagentSpec(task="inspect this", model_id="child-model"),
        depth=1,
    )

    assert isinstance(child.provider, _SelectorFallbackProvider)
    assert child.provider is not parent_provider
    assert child.provider._selector is not selector
    assert selector.current_config.model == "active-model"
    assert child.provider._selector.current_config.model == "child-model"
    assert [
        config.model for config in child.provider._selector.remaining_chain()
    ] == ["child-model", "active-model", "remaining-model"]

    child.provider._selector.next_fallback()
    assert child.provider._selector.current_config.model == "active-model"
    assert selector.current_config.model == "active-model"
    assert child.config.model_id == "child-model"


def test_selector_fallback_subagent_without_override_still_owns_selector() -> None:
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="ollama",
                model="configured-model",
                base_url="http://127.0.0.1:11434",
            ),
            fallbacks=[
                ProviderConfig(
                    provider="ollama",
                    model="active-model",
                    base_url="http://127.0.0.1:11434",
                ),
                ProviderConfig(
                    provider="ollama",
                    model="remaining-model",
                    base_url="http://127.0.0.1:11434",
                )
            ],
        )
    )
    selector.next_fallback()
    parent_provider = _SelectorFallbackProvider(selector.resolve(), selector)
    parent = Agent(
        provider=parent_provider,
        config=AgentConfig(
            provider_id="ollama",
            model_id="configured-model",
            context_window_tokens=100_000,
            max_tokens=4096,
        ),
    )

    child = parent._make_child_agent(
        SubagentSpec(task="inspect this"),
        depth=1,
    )

    assert isinstance(child.provider, _SelectorFallbackProvider)
    assert child.provider is not parent_provider
    assert child.provider._selector is not selector
    assert child.config.model_id == "active-model"
    plan = child.config.compaction_execution_plan
    assert plan is not None
    assert plan.primary.provider is child.provider
    child.provider._selector.next_fallback()
    assert child.provider._selector.current_config.model == "remaining-model"
    assert selector.current_config.model == "active-model"


def test_selector_fallback_subagent_preserves_parent_request_caps() -> None:
    primary = ProviderConfig(
        provider="ollama",
        model="configured-model",
        base_url="http://127.0.0.1:11434",
    )
    fallback = ProviderConfig(
        provider="ollama",
        model="fallback-model",
        base_url="http://127.0.0.1:11434",
    )
    selector = ModelSelector(
        SelectorConfig(primary=primary, fallbacks=[fallback])
    )
    parent_provider = _SelectorFallbackProvider(selector.resolve(), selector)
    parent = Agent(
        provider=parent_provider,
        config=AgentConfig(
            provider_id="ollama",
            model_id="configured-model",
            context_window_tokens=1000,
            context_window_tokens_global_override=1000,
            max_tokens=64,
            provider_request_proof_max_chars=64,
            provider_request_proof_max_chars_explicit=True,
            model_capabilities=ModelCapabilities(supports_vision=True),
            model_vision_support="supported",
        ),
    )

    child = parent._make_child_agent(SubagentSpec(task="inspect this"), depth=1)

    assert isinstance(child.provider, _SelectorFallbackProvider)
    assert child.config.context_window_tokens_global_override == 1000
    assert child.config.provider_request_proof_max_chars == 64
    assert child.config.provider_request_proof_max_chars_explicit is True
    child_fallback = child.provider._selector.remaining_chain()[1]
    child.provider.configure_fallback_deployment_limits(
        [
            (
                child_fallback,
                1000,
                64,
                ModelCapabilities(supports_vision=True),
            )
        ]
    )
    child.provider.configure_fallback_deployment_vision_support(
        [(child_fallback, "supported")]
    )
    child.provider._provider = child.provider._selector.next_fallback()
    child.provider._note_fallback_hop()

    target = child._image_analysis_target()

    assert target is not None
    assert target[1].context_window_tokens_global_override == 1000
    assert target[1].provider_request_max_chars == 64
    assert target[1].provider_request_max_chars_explicit_cap == 64


def test_subagent_output_budget_uses_catalog_safety_clamp(monkeypatch) -> None:
    class _SafetyCatalog:
        def resolve_entry(self, model: str, *, provider: str):
            del model, provider
            return SimpleNamespace(
                context_window=100_000,
                max_output_tokens=95_000,
            )

        def resolve_max_tokens(
            self,
            model: str,
            user_override: int = 0,
            provider: str = "",
        ) -> int:
            assert model == "large-output-model"
            assert user_override == 0
            assert provider == "fake"
            return 8192

        def get_capabilities(
            self,
            model: str,
            *,
            provider_name: str,
            base_url: str,
        ) -> None:
            del model, provider_name, base_url

    monkeypatch.setattr(
        "opensquilla.provider.model_catalog.shared_catalog",
        lambda: _SafetyCatalog(),
    )
    parent = Agent(
        provider=_ModelProvider("parent-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="parent-model",
            context_window_tokens=100_000,
            max_tokens=4096,
        ),
    )

    child = parent._make_child_agent(
        SubagentSpec(task="inspect this", model_id="large-output-model"),
        depth=1,
    )

    assert child.config.context_window_tokens == 100_000
    assert child.config.max_tokens == 8192
    assert child.config.max_tokens != 95_000


def test_subagent_inline_limit_never_invents_capacity_for_tiny_child() -> None:
    no_token_capacity = SubagentExecutionTarget(
        provider=None,
        provider_id="fake",
        model_id="tiny",
        context_window_tokens=32,
        max_output_tokens=32,
        provider_request_max_chars=10_000,
    )
    no_character_capacity = SubagentExecutionTarget(
        provider=None,
        provider_id="fake",
        model_id="tiny",
        context_window_tokens=4096,
        max_output_tokens=1,
        provider_request_max_chars=1,
    )

    assert subagent_task_inline_limit_bytes(no_token_capacity) == 0
    assert subagent_task_inline_limit_bytes(no_character_capacity) == 0
    assert subagent_task_reference_slice_limit_chars(no_token_capacity) == 0


def test_subagent_reference_retrieval_slice_comes_from_child_budget() -> None:
    target = SubagentExecutionTarget(
        provider=None,
        provider_id="fake",
        model_id="bounded",
        context_window_tokens=100_000,
        max_output_tokens=4096,
        provider_request_max_chars=200_000,
    )
    slice_limit = subagent_task_reference_slice_limit_chars(target)
    record = SimpleNamespace(
        handle="tr-" + ("a" * 32),
        sha256="b" * 64,
        chars=70_000,
    )

    prompt = render_subagent_task_reference(
        record,
        slice_limit_chars=slice_limit,
    )

    assert 0 < slice_limit < 60_000
    assert f"limit={slice_limit}" in prompt
    assert "limit=60000" not in prompt


def test_subagent_model_override_fails_closed_when_provider_cannot_bind_model() -> None:
    parent = Agent(
        provider=_OpaqueProvider(),
        config=AgentConfig(provider_id="fake", model_id="parent-model"),
    )

    with pytest.raises(ValueError, match="unsupported by the active provider"):
        parent._make_child_agent(
            SubagentSpec(task="inspect this", model_id="child-model"),
            depth=1,
        )


def test_oversized_subagent_task_uses_content_addressed_reference(tmp_path) -> None:
    task = "delegated exact task\n" + ("x" * 70_000)
    spec = SubagentSpec(task=task)

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unused",
        )

    setattr(
        tool_handler,
        "_opensquilla_available_tools",
        frozenset({"retrieve_tool_result"}),
    )
    parent = Agent(
        provider=_ModelProvider("parent-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="parent-model",
            context_window_tokens=200_000,
            max_tokens=16_384,
            tool_result_store_dir=str(tmp_path / "tool-results"),
            tool_result_store_session_id="session-1",
            tool_result_store_session_key="agent:main:session-1",
            tool_result_store_agent_id="main",
        ),
        tool_definitions=[
            ToolDefinition(
                name="retrieve_tool_result",
                description="Retrieve a stored payload.",
                input_schema=ToolInputSchema(
                    properties={"handle": {"type": "string"}},
                    required=["handle"],
                ),
            )
        ],
        tool_handler=tool_handler,
        session_key="agent:main:session-1",
    )

    child = parent._make_child_agent(spec, depth=1, execution_id="execution-1")

    assert spec.task == task
    assert spec.execution_task is not None
    assert len(spec.execution_task) < 1000
    assert task not in spec.execution_task
    handle_match = re.search(r"tool_result_handle: (tr-[a-f0-9]+)", spec.execution_task)
    assert handle_match is not None
    stored = ToolResultStore(tmp_path / "tool-results").read(
        handle_match.group(1),
        session_id="session-1",
    )
    assert stored.content == task
    assert any(tool.name == "retrieve_tool_result" for tool in child.tool_definitions)
    assert child._tool_context is not None
    assert getattr(child._raw_tool_handler, "_opensquilla_available_tools") == frozenset(
        {"retrieve_tool_result"}
    )
    assert child.config.tool_result_store_dir == child._tool_context.tool_result_store_dir
    assert (
        child.config.tool_result_store_session_id
        == child._tool_context.tool_result_store_session_id
    )
    assert child.config.tool_result_store_session_key == child._tool_context.session_key
    assert child.config.tool_result_store_agent_id == child._tool_context.agent_id
    assert child._tool_result_store_scope() == (
        child._tool_context.tool_result_store_session_id,
        child._tool_context.session_key,
        child._tool_context.agent_id,
    )
    assert child._tool_context.tool_result_retrieval_available is True
    assert child._tool_result_recovery_available() is True


def test_oversized_subagent_task_rejects_without_reference_path() -> None:
    parent = Agent(
        provider=_ModelProvider("parent-model"),
        config=AgentConfig(
            provider_id="fake",
            model_id="parent-model",
            context_window_tokens=200_000,
            max_tokens=16_384,
        ),
    )

    with pytest.raises(ValueError, match="artifact/workspace reference"):
        parent._make_child_agent(
            SubagentSpec(task="x" * 70_000),
            depth=1,
        )


@pytest.mark.asyncio
async def test_subagent_manager_runs_runtime_execution_task() -> None:
    prompts: list[str] = []

    class _Child:
        async def run_turn(self, prompt: str):
            prompts.append(prompt)
            yield SimpleNamespace(kind="done", text="done")

    def factory(spec: SubagentSpec, depth: int, execution_id: str) -> _Child:
        del depth, execution_id
        spec.execution_task = "bounded-reference-prompt"
        return _Child()

    manager = SubagentManager()
    handle = await manager.spawn(SubagentSpec(task="original task"), factory)
    await handle.task

    assert prompts == ["bounded-reference-prompt"]
