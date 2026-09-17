"""Shared ModelCatalog injection (set_shared_catalog / shared_catalog).

The gateway boots ONE warmed catalog and publishes it through
``set_shared_catalog``; module-level consumers (router decision events,
usage RPC context windows, ensemble member wiring) resolve through
``shared_catalog()`` so they see live data. Without an injected instance the
getter falls back to a stable, lazily-built cold catalog — exactly the old
standalone-CLI semantics.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.provider.model_catalog import (
    ModelCatalog,
    set_shared_catalog,
    shared_catalog,
)
from opensquilla.provider.types import ModelCapabilities


class _SentinelCatalog(ModelCatalog):
    """Catalog whose resolve results are unmistakable in assertions."""

    CONTEXT_WINDOW = 777_216
    MAX_TOKENS = 4_321

    def __init__(self) -> None:
        super().__init__()
        self.context_window_calls: list[tuple[str, str]] = []
        self.max_tokens_calls: list[tuple[str, str]] = []
        self.capabilities_calls: list[tuple[str, str]] = []

    def resolve_context_window(self, model_id: str, provider: str = "") -> int:
        self.context_window_calls.append((model_id, provider))
        return self.CONTEXT_WINDOW

    def resolve_max_tokens(
        self, model_id: str, user_override: int = 0, provider: str = ""
    ) -> int:
        self.max_tokens_calls.append((model_id, provider))
        return self.MAX_TOKENS

    def resolve_max_tokens_with_source(
        self, model_id: str, user_override: int = 0, provider: str = ""
    ) -> tuple[int, str]:
        # Deployment limits resolve through the same injected instance while
        # carrying source reliability alongside the sentinel capacity.
        return self.resolve_max_tokens(model_id, user_override, provider), "catalog"

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = "openrouter",
        base_url: str = "",
    ) -> ModelCapabilities:
        self.capabilities_calls.append((model_id, provider_name))
        return ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        )


@pytest.fixture(autouse=True)
def _clear_shared_catalog():
    set_shared_catalog(None)
    yield
    set_shared_catalog(None)


# ---------------------------------------------------------------------------
# Getter / setter semantics.
# ---------------------------------------------------------------------------


def test_getter_returns_stable_cold_instance_when_unset() -> None:
    first = shared_catalog()
    second = shared_catalog()
    assert isinstance(first, ModelCatalog)
    assert first is second


def test_set_then_get_round_trips_the_injected_instance() -> None:
    sentinel = _SentinelCatalog()
    set_shared_catalog(sentinel)
    assert shared_catalog() is sentinel


def test_setting_none_restores_the_cold_fallback() -> None:
    cold = shared_catalog()
    sentinel = _SentinelCatalog()
    set_shared_catalog(sentinel)
    assert shared_catalog() is sentinel

    set_shared_catalog(None)
    restored = shared_catalog()
    assert restored is not sentinel
    assert restored is cold  # the SAME cold instance as before injection


# ---------------------------------------------------------------------------
# Consumer sites resolve through the injected instance.
# ---------------------------------------------------------------------------


def test_router_decision_event_uses_injected_catalog() -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.router_decision import build_router_decision_event

    sentinel = _SentinelCatalog()
    set_shared_catalog(sentinel)

    turn = TurnContext(
        message="hi",
        session_key="agent:main:webchat:shared-catalog",
        config=SimpleNamespace(),
        provider=None,
        model="",
        tool_defs=[],
        system_prompt="",
        metadata={"routed_tier": "c1", "routed_model": "model-under-test"},
    )
    event = build_router_decision_event(turn)

    assert event is not None
    assert event.context_window == _SentinelCatalog.CONTEXT_WINDOW
    assert sentinel.context_window_calls == [("model-under-test", "")]


def test_rpc_usage_context_window_uses_injected_catalog() -> None:
    # Driven via the module-level resolver directly: the full usage.status
    # handler needs a session manager + persisted rows just to reach this
    # line, while every catalog decision lives in _resolve_context_window.
    from opensquilla.gateway.rpc.registry import RpcContext
    from opensquilla.gateway.rpc_usage import _resolve_context_window

    sentinel = _SentinelCatalog()
    set_shared_catalog(sentinel)

    ctx = RpcContext(
        conn_id="test",
        config=SimpleNamespace(llm=SimpleNamespace(provider="openrouter")),
    )
    window, source = _resolve_context_window("model-under-test", ctx)

    assert window == _SentinelCatalog.CONTEXT_WINDOW
    assert source == "static_model_catalog"
    assert sentinel.context_window_calls == [("model-under-test", "openrouter")]


def test_rpc_usage_context_window_model_override_beats_config() -> None:
    # A [models.*] per-model override on the injected catalog outranks the
    # global config window and reports the additive "model_override" label.
    from opensquilla.gateway.rpc.registry import RpcContext
    from opensquilla.gateway.rpc_usage import _resolve_context_window

    catalog = ModelCatalog()
    catalog.set_user_overrides({"openrouter/model-under-test": {"context_window": 131_072}})
    set_shared_catalog(catalog)

    ctx = RpcContext(
        conn_id="test",
        config=SimpleNamespace(
            llm=SimpleNamespace(provider="openrouter", context_window_tokens=999_000)
        ),
    )
    window, source = _resolve_context_window("model-under-test", ctx)

    assert (window, source) == (131_072, "model_override")

    # Without an override the global config window still wins over the catalog.
    window, source = _resolve_context_window("other-model", ctx)
    assert (window, source) == (999_000, "config")


def test_ensemble_member_max_tokens_uses_injected_catalog() -> None:
    from opensquilla.provider.ensemble import EnsembleMemberConfig, _member_max_tokens
    from opensquilla.provider.selector import ProviderConfig

    sentinel = _SentinelCatalog()
    set_shared_catalog(sentinel)

    member = EnsembleMemberConfig(
        provider_config=ProviderConfig(provider="openai", model="model-under-test"),
    )
    assert _member_max_tokens(member) == _SentinelCatalog.MAX_TOKENS
    assert sentinel.max_tokens_calls == [("model-under-test", "openai")]


def test_ensemble_member_capabilities_use_injected_catalog() -> None:
    from opensquilla.provider.ensemble import (
        EnsembleMemberConfig,
        _member_model_capabilities,
    )
    from opensquilla.provider.selector import ProviderConfig

    sentinel = _SentinelCatalog()
    set_shared_catalog(sentinel)

    # Non-openrouter provider so the openrouter static-capabilities
    # short-circuit does not preempt the catalog lookup.
    member = EnsembleMemberConfig(
        provider_config=ProviderConfig(provider="openai", model="model-under-test"),
    )
    caps = _member_model_capabilities(member)

    assert caps.supports_reasoning is True
    assert caps.reasoning_format == "openrouter"
    assert sentinel.capabilities_calls == [("model-under-test", "openai")]
