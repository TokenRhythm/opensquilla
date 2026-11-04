"""Intent-led routing using the pinned pair from the model-policy experiment.

The state machine selects a semantic tier. Image admission may bind a later
configured deployment without changing that semantic decision or its state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing.policy import RoutingDecision
from opensquilla.router_control import RouterControlHoldStore
from opensquilla.router_tiers import TEXT_TIERS


class ModelRouteError(RuntimeError):
    """The selected model-policy route cannot execute faithfully."""


async def apply_model_router(ctx: TurnContext) -> TurnContext:
    from opensquilla.engine.routing.model_policy import (
        ModelPolicyDecision,
        ModelPolicyState,
        decide_model_tier,
    )
    from opensquilla.engine.steps import squilla_router as common
    from opensquilla.engine.steps.router_decision_record import stage_router_decision

    config = ctx.config.squilla_router
    tiers = config.tiers
    phase = config.rollout_phase
    state_raw = ctx.metadata.get("router_model_state")
    state = ModelPolicyState.from_dict(state_raw) if isinstance(state_raw, Mapping) else None
    raw_envelope = ctx.metadata.get("router_model_decision")
    envelope = raw_envelope if isinstance(raw_envelope, Mapping) else {}
    replay = isinstance(envelope.get("decision"), Mapping)
    if replay and envelope.get("status") == "staged":
        # The source retains rejected/incomplete admission for idempotency;
        # it never reruns classification or treats that proposed tier as live.
        raise ModelRouteError("This input has a staged model decision that was not admitted")
    held_tier = None
    hold = None
    if not replay:
        store = ctx.metadata.get("router_control_hold_store")
        if isinstance(store, RouterControlHoldStore):
            hold = store.get_valid(
                ctx.session_key,
                decrement=phase != "observe",
                routing_revision=ctx.metadata.get("router_control_routing_revision"),
            )
            held_tier = hold.tier if hold else None
    if replay:
        decision = ModelPolicyDecision.from_dict(envelope["decision"])
    else:
        router_input = ctx.metadata.get("router_model_input")
        if not isinstance(router_input, Mapping):
            from opensquilla.session.model_router_state import prepare_model_router_metadata

            ctx.metadata.update(
                prepare_model_router_metadata(
                    ctx.metadata,
                    current_request=ctx.semantic_message,
                    input_message_id=ctx.metadata.get("router_model_input_message_id"),
                    history_user=ctx.metadata.get("router_history_user_texts"),
                    previous_answer=ctx.metadata.get("router_prev_assistant_text"),
                    previous_usage=ctx.metadata.get("router_prev_assistant_usage"),
                    tool_defs=ctx.tool_defs,
                    attachments=ctx.attachments,
                )
            )
            router_input = ctx.metadata["router_model_input"]
            router_input["active_route_tier"] = state.tier.upper() if state else None
        strategy = common._get_strategy(config)
        decision = decide_model_tier(
            router_input,
            state,
            strategy,
            explicit_control=ctx.metadata.get("router_model_control_event"),
            held_tier=held_tier,
            default_tier=config.default_tier,
            input_message_id=ctx.metadata.get("router_model_input_message_id"),
            control_context=ctx.metadata.get("router_model_control_context"),
        )
    selected_tier = decision.tier
    effective_state = decision.next_state
    thinking_mode = decision.thinking_mode
    prompt_policy = decision.prompt_policy
    prompt_hint = decision.metadata.get("prompt_hint")
    if replay and isinstance(envelope.get("execution_update"), Mapping):
        update = envelope["execution_update"]
        effective_state = ModelPolicyState.from_dict(envelope["state"])
        if update.get("tier") != effective_state.tier:
            raise ModelRouteError("Replayed execution tier does not match committed state")
        if update.get("thinking_mode") not in {"T0", "T1", "T2", "T3"} or update.get(
            "prompt_policy"
        ) not in {"P0", "P1", "P2"}:
            raise ModelRouteError("Replayed execution control is invalid")
        selected_tier = update.get("execution_tier", effective_state.tier)
        if (
            selected_tier not in TEXT_TIERS
            or TEXT_TIERS.index(selected_tier) < TEXT_TIERS.index(effective_state.tier)
        ):
            raise ModelRouteError("Replayed image execution tier is invalid")
        thinking_mode = update["thinking_mode"]
        prompt_policy = update["prompt_policy"]
        prompt_hint = None
    if selected_tier not in TEXT_TIERS:
        raise ModelRouteError("Model policy selected an invalid tier")
    tier_config = tiers.get(selected_tier)
    if not isinstance(tier_config, Mapping) or not str(tier_config.get("model") or "").strip():
        raise ModelRouteError(f"Model policy selected unconfigured tier {selected_tier}")
    if replay and isinstance(envelope.get("execution_update"), Mapping):
        update = envelope["execution_update"]
        provider = str(tier_config.get("provider") or ctx.config.llm.provider)
        if update.get("model") != tier_config["model"] or update.get("provider") != provider:
            raise ModelRouteError("Replayed model deployment changed after execution")
    baseline = ctx.model
    routed_model = str(tier_config["model"])
    applied = phase != "observe"
    if applied:
        ctx.model = routed_model
    source = (
        "router_control_hold"
        if held_tier or decision.metadata.get("router_control_hold_applied")
        else "model_tier_policy"
    )
    confidence = decision.tier_audit.confidence
    if confidence is None:
        confidence = decision.intent_audit.confidence or 0.0
    extra = dict(decision.metadata)
    extra.update(
        tier_policy="model",
        base_tier=decision.raw_tier,
        final_tier=selected_tier,
        route_class=common.route_class_for_tier(decision.raw_tier),
        final_route_class=common.route_class_for_tier(selected_tier),
        thinking_mode=thinking_mode,
        prompt_policy=prompt_policy,
        intent=decision.intent,
    )
    ctx.metadata.update(
        router_tier_policy="model",
        routed_tier=selected_tier,
        routed_model=routed_model,
        routed_provider=str(tier_config.get("provider") or ctx.config.llm.provider),
        routing_source=source,
        routing_confidence=confidence,
        baseline_model=baseline,
        applied_model=ctx.model,
        routing_applied=applied,
        rollout_phase=phase,
        routing_extra=extra,
    )
    if applied:
        # Execution isolation is inactive in observation mode.
        ctx.metadata.update(
            router_fallback_chain=[],
            router_fallback_strict=True,
            quality_escalation_satisfied_by_prior_policy=True,
            quality_escalation_prior_policy_reason="model_tier_policy_resolved_explicit_correction",
        )
        if not replay:
            ctx.metadata["router_model_decision"] = {
                "schema_version": 1,
                "input_message_id": ctx.metadata.get("router_model_input_message_id"),
                "state": effective_state.to_dict(),
                "decision": decision.to_dict(),
            }
        if decision.context_reset:
            ctx.metadata["provider_state_replay_disabled"] = "router_explicit_new_task"
    if applied and decision.metadata.get("router_control_hold_applied") is True:
        ctx.metadata["router_control_hold_applied"] = True
    if applied and hold is not None:
        ctx.metadata.update(
            router_control_hold_applied=True,
            router_control_action="set_hold",
            router_control_target_tier=hold.tier,
            router_control_target_model=hold.model,
            router_control_target_provider=hold.provider,
            router_control_evidence=hold.evidence,
        )
    if applied:
        common._apply_controller(
            ctx,
            config,
            dict(tier_config),
            thinking_mode,
            prompt_policy,
            prompt_hint,
            phase,
        )
    else:
        ctx.metadata.update(thinking_mode=thinking_mode, prompt_policy=prompt_policy)
    if applied and phase == "full" and config.auto_thinking:
        # Preserve the policy's explicit T0 disable when automatic thinking is
        # enabled. Runtime still gives an explicit llm.thinking setting priority.
        ctx.metadata.update(
            thinking_requested=True,
            thinking_level={"T0": "off", "T1": "low", "T2": "medium", "T3": "high"}[thinking_mode],
            thinking_source="model_tier_policy",
        )
    ctx.metadata.update(common._compute_savings(routed_model, tiers))
    common._flag_tier_provider_mismatch(
        ctx, tiers, selected_tier, routing_applied=applied, force_direct=True
    )
    stage_router_decision(
        ctx,
        decision=RoutingDecision(
            tier=selected_tier,
            model=routed_model,
            confidence=confidence,
            source=source,
        ),
    )
    return ctx


def validate_model_route_capabilities(
    ctx: TurnContext,
    *,
    tier: str | None = None,
    thinking_mode: str | None = None,
) -> TurnContext:
    """Check deployment capabilities independently of historical size estimates."""
    if ctx.metadata.get("routing_applied") is not True:
        return ctx
    from opensquilla.engine.steps import squilla_router as common
    from opensquilla.provider.model_catalog import shared_catalog

    config = ctx.config.squilla_router
    selected = tier or ctx.metadata.get("routed_tier")
    if not isinstance(selected, str):
        raise ModelRouteError("Selected model-policy tier is missing")
    raw = config.tiers.get(selected)
    if not isinstance(raw, dict):
        raise ModelRouteError("Selected model-policy deployment is missing")
    llm = ctx.config.llm
    provider = str(raw.get("provider") or llm.provider)
    if provider != str(llm.provider) and not config.cross_provider_tiers:
        raise ModelRouteError(
            f"Selected tier {selected} requires cross-provider routing for {provider}"
        )
    model = str(raw.get("model") or "")
    catalog = shared_catalog()
    capabilities = catalog.get_capabilities(model, provider_name=provider)
    # A text deployment can execute the existing marker projection. Image
    # admission is resolved separately, before proving the final request.
    # Relay adapters can intentionally leave the control dialect unset while
    # the catalog still verifies that the underlying model reasons. Admission
    # checks that model fact; request controls remain provider-owned.
    thinking_budget = common._route_thinking_budget_tokens(
        ctx, config, raw,
        thinking_mode=thinking_mode or ctx.metadata.get("thinking_mode"),
        rollout_phase=config.rollout_phase,
    )
    if thinking_budget > 0 and catalog.resolve_entry(
        model, provider=provider
    ).supports_reasoning is not True:
        raise ModelRouteError(f"Selected tier {selected} does not support reasoning")
    if ctx.tool_defs and not capabilities.supports_tools:
        raise ModelRouteError(f"Selected tier {selected} does not support tool calls")
    return ctx


def resolve_model_image_route(
    ctx: TurnContext,
    *,
    tier: str | None = None,
    thinking_mode: str | None = None,
    check_capacity: bool = True,
) -> TurnContext:
    """Bind an image-capable forward tier, or retain the target's text projection.

    The durable model-policy envelope remains the semantic decision. Only
    physical route metadata changes, and the configured ladder is never edited.
    Unknown capability keeps the existing bounded provider probe rather than
    being treated as evidence that the model cannot read images.
    """
    if ctx.metadata.get("routing_applied") is not True:
        return ctx
    from opensquilla.engine.routing.policy import budget_gate
    from opensquilla.engine.selector_override import cross_provider_tier_config
    from opensquilla.engine.steps import squilla_router as common

    selected = tier or ctx.metadata.get("routed_tier")
    if selected not in TEXT_TIERS:
        raise ModelRouteError("Selected model-policy tier is missing")
    has_current_image = common._attachments_include_image(ctx.attachments)
    if not (
        has_current_image
        or ctx.metadata.get("image_context_has_images") is True
        or ctx.metadata.get("router_vision_followup_needs_image") is True
    ):
        return ctx
    config = ctx.config.squilla_router
    original = config.tiers.get(selected)
    if not isinstance(original, dict) or not original.get("model"):
        raise ModelRouteError("Selected model-policy deployment is missing")
    target = selected
    original_support = common._tier_deployment_vision_support(ctx, original)
    material_tokens = common._complete_request_estimated_tokens(ctx, ctx.semantic_message)
    active_provider = str(
        ctx.metadata.get("executed_provider")
        or ctx.metadata.get("requested_provider")
        or ctx.config.llm.provider
    )
    for candidate in TEXT_TIERS[TEXT_TIERS.index(selected):]:
        raw = config.tiers.get(candidate)
        if not isinstance(raw, dict) or not raw.get("model"):
            continue
        provider = str(raw.get("provider") or ctx.config.llm.provider)
        if provider != str(ctx.config.llm.provider) and not config.cross_provider_tiers:
            continue
        if common._tier_deployment_vision_support(ctx, raw) != "supported":
            continue
        candidate_metadata = {
            key: value for key, value in ctx.metadata.items()
            if not key.startswith("routed_provider_")
        }
        candidate_metadata.update(
            routed_tier=candidate, routed_model=str(raw["model"]), routed_provider=provider,
        )
        candidate_turn = replace(ctx, model=str(raw["model"]), metadata=candidate_metadata)
        if candidate != selected:
            budget = common._budget_gate_input_for_turn(
                candidate_turn, config, routed_model=candidate_turn.model,
                material_tokens=material_tokens,
            )
            allowed = budget_gate(candidate, valid_tiers=list(TEXT_TIERS), budget=budget)
            if allowed.tier != candidate:
                continue
        try:
            validate_model_route_capabilities(
                candidate_turn, tier=candidate, thinking_mode=thinking_mode,
            )
            if check_capacity:
                validate_model_route_capacity(
                    candidate_turn, tier=candidate, thinking_mode=thinking_mode,
                )
        except ModelRouteError:
            continue
        # Catalog support is not permission to borrow the active provider's
        # credentials. Reuse the execution boundary's local profile and state
        # continuity checks, keeping rejected candidates out of live metadata.
        cross_provider_tier_config(
            ctx.config, candidate_metadata, candidate_turn.model,
            active_provider_id=active_provider, session_key=ctx.session_key,
        )
        if candidate_metadata.get("routed_provider_blocked"):
            continue
        target = candidate
        break
    raw = config.tiers[target]
    support = (
        common._tier_deployment_vision_support(ctx, raw)
        if target != selected else original_support
    )
    ctx.metadata.setdefault("router_model_image_target_tier", selected)
    ctx.metadata.update(
        image_context_has_images=True,
        image_route_reason="current_turn" if has_current_image else "history_context",
        image_input_mode="marker" if support == "unsupported" else "native",
        image_input_reason=(
            "model_vision_unsupported" if support == "unsupported"
            else "capability_probe" if support == "unknown"
            else "configured_image_capable_tier"
        ),
        routed_model_vision_support=support,
    )
    if support == "unsupported":
        ctx.metadata["image_input_projection_required"] = True
    else:
        ctx.metadata.pop("image_input_projection_required", None)
    if target != selected:
        ctx.model = str(raw["model"])
        ctx.metadata.update(
            routed_tier=target,
            routed_model=ctx.model,
            routed_provider=str(raw.get("provider") or ctx.config.llm.provider),
            applied_model=ctx.model,
            router_model_image_execution_tier=target,
        )
        ctx.metadata["routing_extra"] = {
            **ctx.metadata.get("routing_extra", {}),
            "final_tier": target,
            "final_route_class": common.route_class_for_tier(target),
        }
        common._flag_tier_provider_mismatch(
            ctx, config.tiers, target, routing_applied=True, force_direct=True,
        )
        ctx.metadata.update(common._compute_savings(ctx.model, config.tiers))
    return ctx


def validate_model_route_capacity(
    ctx: TurnContext,
    *,
    tier: str | None = None,
    thinking_mode: str | None = None,
    allow_compaction_retry: bool = False,
) -> TurnContext:
    """Check the selected deployment without any semantic tier substitution."""
    validate_model_route_capabilities(ctx, tier=tier, thinking_mode=thinking_mode)
    if ctx.metadata.get("routing_applied") is not True:
        return ctx
    from opensquilla.engine.steps import squilla_router as common

    config = ctx.config.squilla_router
    selected = tier or ctx.metadata.get("routed_tier")
    assert isinstance(selected, str)  # Validated by the capability gate above.

    def assess(turn: TurnContext):
        assessments = common._capacity_tier_assessments(
            turn,
            config,
            config.tiers,
            [selected],
            minimum_tier=None,
            material_tokens=common._final_material_estimated_tokens(turn, turn.semantic_message),
            request_input_tokens=common._complete_request_estimated_tokens(
                turn, turn.semantic_message
            ),
            # Deployment capabilities above are authoritative, not the old
            # preset supports_image shortcut-routing hint.
            requires_image=False,
            thinking_mode=thinking_mode or ctx.metadata.get("thinking_mode"),
            rollout_phase=config.rollout_phase,
        )
        return assessments[0].assessment if assessments else None

    assessment = assess(ctx)
    if assessment and assessment.fits:
        ctx.metadata.pop("router_model_capacity_retry_pending", None)
        return ctx
    # Compaction may reduce known historical pressure, never current material,
    # and never changes the classified tier. Runtime must re-admit the actual
    # compacted request before provider execution.
    if (
        allow_compaction_retry
        and assessment
        and assessment.status == "known_capacity_request_too_large"
        and ctx.metadata.get("routing_history_capacity_estimate_complete") is True
        and int(ctx.metadata.get("routing_history_capacity_estimated_tokens") or 0) > 0
    ):
        current_only = replace(
            ctx,
            metadata={
                **ctx.metadata,
                "routing_history_capacity_estimated_tokens": 0,
                "routing_history_capacity_message_count": 0,
            },
        )
        reduced = assess(current_only)
        if reduced and reduced.fits:
            ctx.metadata["router_model_capacity_retry_pending"] = True
            return ctx
    raise ModelRouteError(f"Selected tier {selected} cannot fit the complete request")
