"""End-to-end guard: a keyless local (Ollama) provider works across the stack.

These cover a whole bug class where the gateway / cost / catalog assumed every
LLM provider has an API key or a provider-qualified model id, silently breaking
local Ollama (whose model ids are bare, e.g. ``qwen3:4b``):

- the gateway built the provider selector only ``if api_key:`` -> keyless local
  providers ended every turn with "No provider available";
- a bare ollama model id missed the ``ollama/`` price entry and was billed at the
  cloud default ($3/$15 per 1M tokens);
- a bare ollama model id reported the 200k cloud context window, so the turn
  budget over-estimated and never trimmed while the runtime truncated.

One regression net for all three so the local path can't silently rot again.
"""

from __future__ import annotations

from opensquilla.engine.pricing import lookup_price
from opensquilla.engine.usage import SessionUsage
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
from opensquilla.gateway.provider_runtime import sync_provider_selector as _sync_provider_selector
from opensquilla.provider.model_catalog import (
    DEFAULT_CONTEXT_WINDOW,
    ModelCatalog,
)
from opensquilla.provider.ollama import _OLLAMA_DEFAULT_NUM_CTX, OllamaProvider
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import ProviderConfig, _build_provider


def _keyless_ollama_cfg() -> GatewayConfig:
    return GatewayConfig(llm={"provider": "ollama", "model": "qwen3:4b", "api_key": ""})


def test_keyless_ollama_resolves_and_builds_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    cfg = _keyless_ollama_cfg()

    # registry is the source of truth: local providers need no key.
    assert get_provider_spec("ollama").requires_api_key() is False

    runtime = resolve_llm_runtime_config(cfg)
    assert runtime.provider == "ollama"
    assert runtime.api_key == ""

    # boot path: the selector must still build a provider for a keyless config.
    provider = _build_provider(
        ProviderConfig(
            provider=runtime.provider,
            model=runtime.model,
            api_key=runtime.api_key,
            base_url=runtime.base_url,
        )
    )
    assert isinstance(provider, OllamaProvider)
    assert provider._headers() == {}  # no Authorization header without a key


def test_keyless_ollama_estimates_as_free() -> None:
    usage = SessionUsage()
    usage.add(50_000, 2_000, "qwen3:4b", provider="ollama")
    assert usage.cost == 0.0
    # Sanity: the bare id alone (no provider) WOULD have been mispriced.
    assert lookup_price("qwen3:4b").input_per_m > 0


def test_keyless_ollama_context_window_is_local_not_cloud() -> None:
    catalog = ModelCatalog()
    window = catalog.resolve_context_window("qwen3:4b", provider="ollama")
    assert window == _OLLAMA_DEFAULT_NUM_CTX
    assert window < DEFAULT_CONTEXT_WINDOW
    # max_tokens cannot exceed the (smaller) local window.
    assert catalog.resolve_max_tokens("qwen3:4b", provider="ollama") <= window


def test_runtime_switch_to_keyless_ollama_syncs_selector() -> None:
    class _CapturingSelector:
        def __init__(self) -> None:
            self.synced: ProviderConfig | None = None

        def sync_primary(self, cfg: ProviderConfig) -> None:
            self.synced = cfg

    selector = _CapturingSelector()
    ctx = type("Ctx", (), {"config": _keyless_ollama_cfg(), "provider_selector": selector})()

    _sync_provider_selector(ctx, ctx.config)

    assert selector.synced is not None
    assert selector.synced.provider == "ollama"
    assert selector.synced.api_key == ""
    # And the synced config builds a real provider end-to-end.
    assert isinstance(_build_provider(selector.synced), OllamaProvider)
