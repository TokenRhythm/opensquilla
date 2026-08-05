from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.types import AgentConfig
from opensquilla.gateway.rpc_memory_import import _GatewayFusionCompletion
from opensquilla.memory.dream.runner import _run_complete
from opensquilla.memory.session_flush import ProviderCompletionError, _provider_complete
from opensquilla.provider import auxiliary_budget
from opensquilla.provider.auxiliary_budget import AuxiliaryRequestTooLargeError
from opensquilla.provider.protocol import ProviderMetadata
from opensquilla.provider.types import ChatConfig, DoneEvent, Message, TextDeltaEvent
from opensquilla.skills.meta.orchestrator import make_llm_chat_from_provider
from opensquilla.tools.builtin.media import _complete_from_stream


class _Catalog:
    def __init__(self, *, context_window: int = 8192) -> None:
        self.context_window = context_window

    def resolve_context_window_with_source(
        self,
        model_id: str,
        *,
        provider: str = "",
    ) -> tuple[int, str]:
        del model_id, provider
        return self.context_window, "catalog"

    def resolve_context_window(self, model_id: str, provider: str = "") -> int:
        del model_id, provider
        return self.context_window

    def resolve_max_tokens(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> int:
        del model_id, provider
        return max(1, user_override or 256)


class _ChatProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.config: ChatConfig | None = None

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            provider_id="test",
            provider_name="test",
            model="test-model",
        )

    def chat(
        self,
        messages: list[Message],
        *,
        tools: Any = None,
        config: ChatConfig,
    ) -> AsyncIterator[Any]:
        del messages, tools
        self.calls += 1
        self.config = config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text="ok")
        yield DoneEvent(input_tokens=1, output_tokens=1)


class _CompletionProvider:
    def __init__(self) -> None:
        self.calls = 0

    def provider_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(provider_id="test", model="test-model")

    async def complete(self, *, messages: list[Message], max_tokens: int) -> Any:
        del messages, max_tokens
        self.calls += 1
        return SimpleNamespace(content="ok")


@pytest.fixture
def small_catalog(monkeypatch: pytest.MonkeyPatch) -> _Catalog:
    catalog = _Catalog()
    monkeypatch.setattr(auxiliary_budget, "shared_catalog", lambda: catalog)
    return catalog


@pytest.mark.asyncio
async def test_media_chat_receives_nonzero_resolved_request_cap(
    small_catalog: _Catalog,
) -> None:
    provider = _ChatProvider()

    assert await _complete_from_stream(
        provider,
        [Message(role="user", content="describe")],
        ChatConfig(max_tokens=64),
    ) == "ok"

    assert provider.calls == 1
    assert provider.config is not None
    assert provider.config.provider_request_max_chars > 0
    assert provider.config.max_tokens == 64


@pytest.mark.asyncio
async def test_media_chat_rejects_token_dense_input_before_call(
    small_catalog: _Catalog,
) -> None:
    small_catalog.context_window = 1024
    provider = _ChatProvider()

    with pytest.raises(AuxiliaryRequestTooLargeError, match="tokens"):
        await _complete_from_stream(
            provider,
            [Message(role="user", content="中文🙂" * 500)],
            ChatConfig(max_tokens=64),
        )

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_memory_completion_shim_rejects_oversize_before_call(
    small_catalog: _Catalog,
) -> None:
    small_catalog.context_window = 1024
    provider = _CompletionProvider()

    with pytest.raises(ProviderCompletionError) as exc_info:
        await _provider_complete(
            provider,
            messages=[Message(role="user", content="x" * 5000)],
            max_tokens=64,
        )

    assert exc_info.value.code == "provider_request_too_large"
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_dream_chat_receives_nonzero_resolved_request_cap(
    small_catalog: _Catalog,
) -> None:
    provider = _ChatProvider()

    assert await _run_complete(
        provider,
        [Message(role="user", content="consolidate")],
        64,
    ) == "ok"

    assert provider.config is not None
    assert provider.config.provider_request_max_chars > 0


@pytest.mark.asyncio
async def test_meta_chat_binds_base_deployment_budget(
    small_catalog: _Catalog,
) -> None:
    provider = _ChatProvider()
    chat = make_llm_chat_from_provider(
        provider=provider,
        base_config=AgentConfig(
            provider_id="test",
            model_id="test-model",
            context_window_tokens=small_catalog.context_window,
            context_overflow_threshold=0.8,
        ),
        max_tokens=128,
    )

    assert await chat("system", "user") == "ok"

    assert provider.config is not None
    assert provider.config.provider_request_max_chars > 0
    assert provider.config.max_tokens == 128


@pytest.mark.asyncio
async def test_profile_import_completion_binds_nonzero_request_cap(
    small_catalog: _Catalog,
) -> None:
    provider = _ChatProvider()
    selector = SimpleNamespace(resolve=lambda: provider)
    ctx = SimpleNamespace(
        config=SimpleNamespace(
            llm=SimpleNamespace(max_tokens=0),
            llm_request_timeout_seconds=1.0,
        ),
        provider_stats=None,
        usage_event_sink=None,
    )
    completion = _GatewayFusionCompletion(
        ctx=ctx,
        selector=selector,
        provider_id="test",
        model="test-model",
        agent_id="main",
        max_tokens=64,
    )

    result = await completion(
        SimpleNamespace(
            system_prompt="system",
            user_prompt="user",
            response_schema=None,
        )
    )

    assert result == "ok"
    assert provider.config is not None
    assert provider.config.provider_request_max_chars > 0
