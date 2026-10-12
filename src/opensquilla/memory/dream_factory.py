"""Factory helpers for constructing Dream runners from gateway config."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.memory.dream import Dream
from opensquilla.router_tiers import DEFAULT_TEXT_TIER, normalize_text_tier


def _router_model_routing_enabled(router_cfg: Any | None) -> bool:
    if router_cfg is None or not getattr(router_cfg, "enabled", False):
        return False
    return str(getattr(router_cfg, "rollout_phase", "full") or "full") != "observe"


def _dream_provider_target(config: Any) -> tuple[str, str]:
    llm_cfg = getattr(config, "llm", None)
    if llm_cfg is None:
        return "", ""

    router_cfg = getattr(config, "squilla_router", None)
    if _router_model_routing_enabled(router_cfg):
        tiers = getattr(router_cfg, "tiers", {}) or {}
        default_tier = normalize_text_tier(getattr(router_cfg, "default_tier", None))
        default_tier = default_tier or DEFAULT_TEXT_TIER
        tier_cfg = tiers.get(default_tier) if isinstance(tiers, dict) else None
        if not isinstance(tier_cfg, dict) or not str(tier_cfg.get("model") or "").strip():
            raise RuntimeError(
                f"squilla_router.tiers.{default_tier} model is required for Dream"
            )
        provider = str(tier_cfg.get("provider") or getattr(llm_cfg, "provider", "openrouter"))
        model = str(tier_cfg["model"])
        return provider, model

    return str(getattr(llm_cfg, "provider", "openrouter")), str(getattr(llm_cfg, "model", ""))


def build_dream_provider_selector(config: Any) -> Any | None:
    """Build Dream's own selector from config-derived model policy."""
    llm_cfg = getattr(config, "llm", None)
    if llm_cfg is None:
        return None

    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
    from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig

    # Resolve credentials through the shared explicit-config-first resolver
    # (config > derived provider env > spec default) instead of a local
    # openrouter-env-first chain, so a shell holding OPENROUTER_* variables
    # cannot hijack a non-openrouter Dream provider or an operator-chosen
    # endpoint. Resolved on a throwaway deep copy: the resolver materializes
    # values into the model in place, and Dream must not mutate the live
    # gateway config.
    runtime = resolve_llm_runtime_config(config.model_copy(deep=True))
    api_key = runtime.api_key
    if not api_key:
        return None

    provider, model = _dream_provider_target(config)
    base_url = runtime.base_url
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    proxy = runtime.proxy

    return ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                proxy=proxy,
                provider_routing=getattr(llm_cfg, "provider_routing", {}),
            )
        )
    )


def _resolve_provider(
    *,
    config: Any,
    need_provider: bool,
) -> Any | None:
    if not need_provider:
        return None

    selector = build_dream_provider_selector(config)
    if selector is None:
        raise RuntimeError("no provider configured for Dream")

    resolve = getattr(selector, "resolve", None)
    if not callable(resolve):
        raise RuntimeError("provider selector cannot resolve a provider")
    return resolve()


def _session_lock_for(turn_runner: Any | None, agent_id: str) -> Any | None:
    if turn_runner is None:
        return None
    get_lock = getattr(turn_runner, "get_session_lock", None)
    if not callable(get_lock):
        get_lock = getattr(turn_runner, "_get_session_lock", None)
    if not callable(get_lock):
        return None
    return get_lock(f"memory_dream:{agent_id}")


def build_dream_factory(
    *,
    config: Any,
    turn_runner: Any | None = None,
    workspace_for_agent: Callable[[str], Path] | None = None,
    need_provider: bool = True,
) -> Callable[[str], Dream]:
    """Return ``build_dream(agent_id)`` wired to gateway/CLI dependencies."""
    dream_cfg = getattr(getattr(config, "memory", None), "dream", None)
    if dream_cfg is None:
        raise RuntimeError("memory.dream config is missing")

    def build_dream(agent_id: str) -> Dream:
        workspace = (
            workspace_for_agent(agent_id)
            if workspace_for_agent is not None
            else resolve_agent_workspace_dir(agent_id, config)
        )
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "memory").mkdir(parents=True, exist_ok=True)
        provider = _resolve_provider(
            config=config,
            need_provider=need_provider,
        )
        return Dream(
            workspace=workspace,
            provider=provider,
            session_lock=_session_lock_for(turn_runner, agent_id),
            config=dream_cfg,
            agent_id=agent_id,
        )

    return build_dream
