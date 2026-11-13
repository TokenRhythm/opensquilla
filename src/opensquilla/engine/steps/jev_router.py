"""Session-aware decision advice at the shared pre-turn boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

import structlog

from opensquilla.engine.pipeline import TurnContext
from opensquilla.jev_router import JevCandidate, JevRequest, evaluate_jev
from opensquilla.router_tiers import TEXT_TIERS, normalize_tier_mapping

log = structlog.get_logger(__name__)


def jev_input_fingerprint(message: str, attachments: list[dict] | None = None) -> str:
    """Match an in-memory draft assessment to its submitted semantic input."""
    material = json.dumps(
        [message.strip(), attachments or []], ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def resolve_jev_deployment(
    config: Any,
    settings: Any,
    *,
    inherited: Any = None,
    session_key: str = "",
    acquire: bool = False,
) -> Any:
    """Resolve the decision deployment through the normal credential boundary."""
    from opensquilla.engine.selector_override import (
        acquire_profile_credential,
        peek_profile_credential,
    )
    from opensquilla.provider.deployment import resolve_provider_deployment

    def setting(name: str, default: str) -> str:
        value = (
            settings.get(name, default)
            if isinstance(settings, Mapping)
            else getattr(
                settings,
                name,
                default,
            )
        )
        return str(value)

    provider = setting("provider", "tokenrhythm").strip().lower()
    llm = getattr(config, "llm", None)
    if provider == str(getattr(llm, "provider", "")).strip().lower() and (
        inherited is None or str(getattr(inherited, "provider", "")).strip().lower() != provider
    ):
        from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config
        from opensquilla.provider.selector import ProviderConfig

        primary = resolve_llm_runtime_config(config)
        inherited = ProviderConfig(
            provider=primary.provider,
            model=primary.model,
            api_key=primary.api_key,
            base_url=primary.base_url,
            proxy=primary.proxy,
            provider_routing=primary.provider_routing,
            extra_body=primary.extra_body,
            replay_provider_state=False,
        )
    return resolve_provider_deployment(
        config,
        provider,
        setting("model", "NeoHorse-Jev-4B"),
        inherited_provider_config=inherited,
        session_key=session_key,
        replay_provider_state=False,
        credential_pool_acquirer=acquire_profile_credential if acquire else peek_profile_credential,
    )


def _deployment_is_local(provider: str, base_url: str) -> bool:
    """Require a local-runtime identity and an explicitly loopback endpoint."""
    import ipaddress
    from urllib.parse import urlsplit

    from opensquilla.provider.registry import LOCAL_RUNTIME_PROVIDERS

    if provider not in LOCAL_RUNTIME_PROVIDERS:
        return False
    try:
        endpoint = urlsplit(base_url)
        if endpoint.scheme not in {"http", "https"}:
            return False
        if endpoint.hostname == "localhost":
            return True
        return ipaddress.ip_address(endpoint.hostname or "").is_loopback
    except ValueError:
        return False


def configured_jev_candidates(ctx: TurnContext, inherited: Any = None) -> tuple[JevCandidate, ...]:
    """Expose configured deployments with capability and price evidence only."""
    from opensquilla.provider.model_catalog import shared_catalog

    llm = ctx.config.llm
    current_provider = str(getattr(inherited, "provider", "") or llm.provider).strip().lower()
    current_model = str(getattr(inherited, "model", "") or ctx.model or llm.model).strip()
    roles: dict[tuple[str, str], str] = {}
    tiers = normalize_tier_mapping(ctx.config.squilla_router.tiers)
    for tier in TEXT_TIERS:
        raw = tiers.get(tier)
        if isinstance(raw, Mapping) and raw.get("model") and not raw.get("image_only"):
            identity = (
                str(raw.get("provider") or current_provider).strip().lower(),
                str(raw["model"]).strip(),
            )
            # A deployment shared by several configured roles is still one
            # physical model. Its lowest assigned role avoids inventing a
            # capability difference between identical deployments.
            roles.setdefault(identity, tier)
    deployments = [(current_provider, current_model), *roles]
    catalog = shared_catalog()
    candidates: list[JevCandidate] = []
    current_deployment = None
    seen: set[tuple[str, str]] = set()
    for provider, model in deployments:
        if (provider, model) in seen:
            continue
        seen.add((provider, model))
        resolution = resolve_jev_deployment(
            ctx.config,
            {"provider": provider, "model": model},
            inherited=inherited,
            session_key=ctx.session_key,
        )
        if not resolution.ready and (provider, model) != (current_provider, current_model):
            continue
        deployment = resolution.provider_config
        if (provider, model) == (current_provider, current_model):
            current_deployment = deployment
        api_key = str(getattr(deployment, "api_key", "") or "")
        base_url = str(getattr(deployment, "base_url", "") or "")
        proxy = str(getattr(deployment, "proxy", "") or "")
        limits = catalog.resolve_deployment_limits(
            model, provider=provider, api_key=api_key, base_url=base_url, proxy=proxy,
        )
        capabilities = catalog.resolve_deployment_capabilities(
            model, provider=provider, api_key=api_key, base_url=base_url,
        )
        tools_known = catalog.deployment_tool_capability_is_verified(
            model, provider=provider, api_key=api_key, base_url=base_url,
        )
        prices = _deployment_prices(catalog, model, provider, api_key, base_url)
        # The deployment resolver owns host identity and credential provenance.
        vision = catalog.resolve_deployment_vision_support(
            model,
            provider=provider,
            api_key=api_key, base_url=base_url, proxy=proxy,
        )
        candidates.append(
            JevCandidate(
                id=f"{provider}:{model}",
                model=model,
                provider=provider,
                tier=roles.get((provider, model), ""),
                context_window_tokens=limits.context_window if limits.context_window_known else 0,
                vision_support=vision,
                supports_tools=capabilities.supports_tools if tools_known else None,
                input_cost_per_million=prices.get("input_cost_per_mtok"),
                output_cost_per_million=prices.get("output_cost_per_mtok"),
                is_local=_deployment_is_local(
                    provider,
                    str(getattr(resolution.provider_config, "base_url", "") or ""),
                ),
            )
        )
    if (
        current_deployment is not None
        and not ctx.attachments
        and not ctx.metadata.get("image_context_has_images")
    ):
        from opensquilla.engine.jev_ensemble import (
            JEV_ENSEMBLE_CANDIDATE_ID,
            prepare_jev_ensemble,
        )

        prepared = prepare_jev_ensemble(
            ctx.config, inherited=current_deployment, catalog=catalog,
            session_key=ctx.session_key,
        )
        if prepared is not None and (
            (ctx.metadata.get("jev_requires_tools") is not True and not ctx.tool_defs)
            or prepared.supports_tools
        ):
            aggregate = prepared.descriptor["aggregator"]
            candidates.append(JevCandidate(
                id=JEV_ENSEMBLE_CANDIDATE_ID, kind="ensemble", tier="c4",
                model=aggregate["model"], provider=aggregate["provider"],
                context_window_tokens=prepared.context_window_tokens,
                vision_support="unsupported", supports_tools=prepared.supports_tools,
                ensemble=prepared.descriptor,
            ))
    return tuple(candidates)


def _deployment_prices(
    catalog: Any, model: str, provider: str, api_key: str, base_url: str,
) -> dict[str, float]:
    """Use cached price facts belonging to the actual candidate deployment."""
    overrides = catalog.user_override_price_fields(model, provider=provider)
    if provider != "tokenrhythm":
        if provider in {"custom", "custom_anthropic"}:
            return dict(overrides)
        entry = catalog.resolve_entry(model, provider=provider)
        fields = {
            name: value for name in ("input_cost_per_mtok", "output_cost_per_mtok")
            if (value := getattr(entry, name)) is not None
        }
        return {**fields, **overrides}

    from opensquilla.provider.tokenrhythm_catalog import (
        TOKENRHYTHM_API_BASE_URL,
        canonical_tokenrhythm_base_url,
        is_official_tokenrhythm_endpoint,
        tokenrhythm_authority_identity,
        tokenrhythm_pricing_fields,
    )

    canonical = canonical_tokenrhythm_base_url(base_url or TOKENRHYTHM_API_BASE_URL)
    if not canonical or not is_official_tokenrhythm_endpoint(canonical):
        return dict(overrides)
    published = catalog.tokenrhythm_published_snapshot().get(model.strip().lower())
    fields = tokenrhythm_pricing_fields(published.pricing if published else None)
    authority = tokenrhythm_authority_identity(
        provider=provider, base_url=canonical, api_key=api_key,
    )
    declared = catalog.tokenrhythm_declared_for_authority(model, authority) if authority else None
    if declared is not None:
        fields.update(tokenrhythm_pricing_fields(declared.pricing))
    return {**fields, **overrides}


def _attachment_modalities(attachments: list[dict]) -> tuple[str, ...]:
    modalities: set[str] = set()
    for attachment in attachments:
        mime = str(attachment.get("mime_type") or attachment.get("mimeType") or "")
        kind = str(attachment.get("type") or attachment.get("kind") or "")
        name = str(attachment.get("name") or attachment.get("filename") or "").lower()
        if mime.startswith("image/") or kind in {"image", "image_url"}:
            modalities.add("image")
        elif mime.startswith("audio/") or kind == "audio":
            modalities.add("audio")
        elif mime.startswith("video/") or kind == "video":
            modalities.add("video")
        elif name.endswith((".xlsx", ".xls", ".csv", ".tsv")) or "spreadsheet" in mime:
            modalities.add("spreadsheet")
        else:
            modalities.add("document")
    return tuple(sorted(modalities))


async def apply_jev_router(
    ctx: TurnContext,
    *,
    service: Any,
    input_id: str = "",
    inherited: Any = None,
    routing_revision: int | None = None,
    assessment_stage: Literal["input", "loop", "signals"] = "signals",
    check_generation: int | None = None,
    recent_tool_results: tuple[str, ...] = (),
    loop_iteration: int = 0,
) -> TurnContext:
    """Publish advice; execution waits for confirmation or the mode-specific deadline."""
    from opensquilla.gateway.config import JevRouterConfig

    settings = await service.settings(ctx.session_key, ctx.config.jev_router)
    config = JevRouterConfig.model_validate(settings)
    if not config.enabled:
        return ctx
    advice_context = (
        "An independent model-advice companion may show suggestions in the interface. "
        "Confirmed suggestions apply at a complete model-request boundary; trust mode may "
        "confirm a choice after its waiting period. Complete the requested task normally and leave "
        "model-switching explanations to that interface. Do not claim a model changed "
        "without execution evidence."
    )
    if isinstance(ctx.system_prompt, tuple):
        base, dynamic = ctx.system_prompt
        ctx.system_prompt = (base, f"{dynamic}\n\n{advice_context}")
    else:
        ctx.system_prompt = f"{ctx.system_prompt}\n\n{advice_context}"
    candidates = configured_jev_candidates(ctx, inherited)
    current_provider = str(getattr(inherited, "provider", "") or ctx.config.llm.provider)
    current_model = str(getattr(inherited, "model", "") or ctx.model)
    ctx.model = current_model
    # A confirmed choice survives the next turn, but a manual selection's newer
    # routing revision immediately supersedes it.
    applied = await service.applied_target(ctx.session_key, routing_revision)
    if applied and any(
        candidate.provider == applied["provider"] and candidate.model == applied["model"]
        for candidate in candidates
    ):
        ctx.model = current_model = applied["model"]
        current_provider = applied["provider"]
        ctx.metadata["jev_target"] = dict(applied)
    recent: list[dict[str, Any]] = [
        {"role": "user", "content": text}
        for text in ctx.metadata.get("router_history_user_texts", [])[-6:]
    ]
    previous = ctx.metadata.get("router_prev_assistant_text")
    if previous:
        recent.append({"role": "assistant", "content": previous})
    current = next(
        (c for c in candidates if c.kind == "model" and (
            c.model == current_model and c.provider == current_provider
        )),
        None,
    )
    modalities = _attachment_modalities(ctx.attachments)
    if ctx.metadata.get("image_context_has_images") and "image" not in modalities:
        modalities = (*modalities, "image")
    from opensquilla.engine.jev_context import (
        build_context_restart_assessment,
        maybe_publish_context_restart,
    )

    context_assessment = build_context_restart_assessment(
        ctx, config=config,
        context_window_tokens=current.context_window_tokens if current else 0,
    )
    if not await service.can_evaluate(
        ctx.session_key,
        input_id,
        cooldown_turns=config.cooldown_turns if assessment_stage == "signals" else 0,
        max_prompts_per_hour=config.max_prompts_per_hour,
    ):
        return ctx
    from opensquilla.engine.steps.squilla_router import _complete_request_estimated_tokens

    # Advice runs before final prompt assembly; the same request estimator
    # includes the seeded history, current material, tool schemas and system.
    context_tokens = _complete_request_estimated_tokens(ctx, ctx.semantic_message)
    ctx.metadata["jev_context_estimated_tokens"] = context_tokens
    request = JevRequest(
        assessment_stage=assessment_stage,
        recent_tool_results=recent_tool_results,
        loop_iteration=loop_iteration,
        message=ctx.semantic_message,
        current_model=current_model,
        current_provider=current_provider,
        candidates=candidates,
        context_assessment=context_assessment,
        recent_messages=recent,
        turn_count=max(1, int(ctx.metadata.get("jev_turn_count") or len(recent) + 1)),
        context_tokens=context_tokens,
        context_window_tokens=current.context_window_tokens if current else 0,
        attachment_modalities=modalities,
        requires_tools=ctx.metadata.get("jev_requires_tools") is True,
        tool_failure_count=max(0, int(ctx.metadata.get("jev_tool_failure_count") or 0)),
        current_execution_mode=(
            "ensemble" if ctx.metadata.get("jev_execution_mode") == "ensemble"
            or ("jev_execution_mode" not in ctx.metadata and (
                ctx.metadata.get("ensemble_enabled") is True
                or getattr(ctx.config.llm_ensemble, "enabled", False)
            )) else "direct"
        ),
        failure_count=sum(
            1
            for item in recent
            if item["role"] == "user"
            and any(word in item["content"] for word in ("不对", "重新来", "没解决", "还是不行"))
        ),
    )
    resolution = resolve_jev_deployment(
        ctx.config,
        config,
        inherited=inherited,
        session_key=ctx.session_key,
        acquire=True,
    )
    await service.set_status(ctx.session_key, "checking" if resolution.ready else "unavailable")
    await service.notify(ctx.session_key)
    evaluation = await evaluate_jev(
        request,
        config,
        provider_config=resolution.provider_config if resolution.ready else None,
    )
    if check_generation is not None and not service.is_check_current(
        ctx.session_key, check_generation,
    ):
        return ctx
    ctx.metadata["jev_evaluation"] = {
        "status": evaluation.status,
        "error": evaluation.error,
        "signals": list(evaluation.signals),
        "latency_ms": evaluation.latency_ms,
        "input_tokens": evaluation.input_tokens,
        "output_tokens": evaluation.output_tokens,
        "billed_cost": evaluation.billed_cost,
    }
    if evaluation.context_decision is not None:
        ctx.metadata["jev_context_evaluation"] = {
            "action": evaluation.context_decision.action,
            "confidence": evaluation.context_decision.confidence,
        }
    if evaluation.status in {"unavailable", "invalid"}:
        log.warning(
            "jev.evaluation_unavailable",
            status=evaluation.status,
            error=evaluation.error,
            latency_ms=evaluation.latency_ms,
            candidate_count=len(candidates),
        )
    await service.set_status(
        ctx.session_key,
        "unavailable" if evaluation.status in {"unavailable", "invalid"} else "ready",
    )
    decision = evaluation.decision
    if decision is not None and decision.action != "keep":
        target = next(c for c in candidates if c.id == decision.candidate_id)
        wire: dict[str, Any] = {
            "action": decision.action,
            "current": {"model": current_model, "provider": current_provider},
            "target": (
                None if target.kind == "ensemble"
                else {"model": target.model, "provider": target.provider}
            ),
            "reason": decision.reason,
            "evidence": list(decision.evidence),
            "risk": decision.risk,
            "trigger": decision.signal,
        }
        if target.kind == "ensemble":
            wire["ensemble"] = dict(target.ensemble or {})
        if assessment_stage in {"input", "loop"}:
            wire["source"] = assessment_stage
        if decision.estimated_savings_percent is not None:
            wire["costHint"] = (
                f"相同 token 用量下，目录价格估算低 {decision.estimated_savings_percent:.0f}%；"
                "实际费用以账单为准"
            )
        record = await service.publish(
            ctx.session_key,
            wire,
            input_id=input_id,
            routing_revision=routing_revision,
            max_prompts_per_hour=config.max_prompts_per_hour,
            check_generation=check_generation,
            auto_switch_seconds=config.auto_switch_seconds,
        )
        if record:
            ctx.metadata["jev_decision_id"] = record["id"]
    await maybe_publish_context_restart(
        ctx, service=service, config=config, input_id=input_id,
        current_model=current_model, current_provider=current_provider,
        context_window_tokens=current.context_window_tokens if current else 0,
        assessment_stage=assessment_stage, check_generation=check_generation,
        assessment=context_assessment, decision=evaluation.context_decision,
    )
    await service.notify(ctx.session_key)
    return ctx
