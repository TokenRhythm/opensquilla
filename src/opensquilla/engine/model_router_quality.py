"""One proven, durable quality retry for an already classified model-policy turn."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from opensquilla.context_budget import ContextBudgetGovernor
from opensquilla.engine.routing.model_policy import (
    ModelPolicyDecision,
    quality_retry_controls,
    quality_retry_tier,
)
from opensquilla.engine.types import THINKING_BUDGETS, AgentConfig, ThinkingLevel
from opensquilla.provider.model_catalog import resolve_effective_context_window, shared_catalog
from opensquilla.provider.protocol import (
    project_provider_final_request,
    validate_provider_chat_admission,
)
from opensquilla.provider.types import ChatConfig


@dataclass(kw_only=True)
class ModelRouterQualityRetry:
    turn: Any
    selector: Any
    manager: Any
    input_message_id: str
    session_id: str | None
    epoch: int | None

    async def __call__(
        self,
        *,
        reason: str,
        scope_id: str,
        remaining_budget: int,
        messages: list[Any],
        chat_config: ChatConfig | None,
        tools: Any,
        activate_prepared: Callable[[Any, Any], None],
        prepare_request: Callable[
            [Any, Any, list[Any], Any, ChatConfig], tuple[list[Any], ChatConfig]
        ] | None = None,
        context_overflow_threshold: float | None = None,
    ) -> dict[str, Any] | None:
        metadata = self.turn.metadata
        envelope = metadata.get("router_model_decision")
        if not envelope or not envelope.get("state_committed") or chat_config is None:
            return None
        state = envelope["state"]
        target = quality_retry_tier(
            state.get("tier", ""),
            reason,
            remaining_budget,
            already_used=bool(envelope.get("quality_retry", {}).get("used")),
            held_tier=(state.get("tier") if metadata.get("router_control_hold_applied") else None),
        )
        if target is None:
            return None
        decision = ModelPolicyDecision.from_dict(envelope["decision"])
        thinking_mode, prompt_policy = quality_retry_controls(decision, target)
        router_cfg = self.turn.config.squilla_router
        explicit_thinking = str(self.turn.config.llm.thinking or "").strip()
        automatic_thinking = router_cfg.auto_thinking and not explicit_thinking
        if automatic_thinking:
            level = {"T0": "off", "T1": "low", "T2": "medium", "T3": "high"}[thinking_mode]
            thinking_level = ThinkingLevel(level) if level != "off" else None
            thinking_enabled = level != "off"
            thinking_budget = THINKING_BUDGETS[ThinkingLevel(level)]
        else:
            thinking_level = chat_config.thinking_level
            thinking_enabled = chat_config.thinking
            thinking_budget = chat_config.thinking_budget_tokens if thinking_enabled else 0
            level = (thinking_level or "medium") if thinking_enabled else "off"
        tier_cfg = router_cfg.tiers.get(target)
        if not isinstance(tier_cfg, dict) or not tier_cfg.get("model"):
            return None
        candidate_metadata = {
            key: value for key, value in metadata.items() if not key.startswith("routed_provider_")
        }
        candidate_metadata.update(
            routed_tier=target,
            routed_model=str(tier_cfg["model"]),
            routed_provider=tier_cfg.get("provider") or self.turn.config.llm.provider,
            thinking_mode=thinking_mode,
            thinking_level=level,
            router_fallback_chain=[],
            router_fallback_strict=True,
        )
        candidate_turn = replace(
            self.turn, model=str(tier_cfg["model"]), metadata=candidate_metadata
        )
        from opensquilla.engine.routing.policy import budget_gate
        from opensquilla.engine.steps import squilla_router as common
        from opensquilla.engine.steps.model_router import validate_model_route_capacity

        budget_cfg = router_cfg.budget
        if budget_cfg.limit_usd and budget_cfg.action != "off":
            # The primary attempt may have accrued spend since initial routing.
            # Retry admission uses the durable ledger for the same incarnation.
            budget_session = await self.manager.get_session(self.turn.session_key)
            if (
                budget_session is None
                or (self.session_id is not None and budget_session.session_id != self.session_id)
                or (self.epoch is not None and budget_session.epoch != self.epoch)
            ):
                raise RuntimeError("Quality retry budget session ownership changed")
            budget_session = (
                await self.manager.storage.reconcile_session_usage_totals_from_ledger(
                    session_key=self.turn.session_key,
                    expected_session_id=budget_session.session_id,
                    expected_epoch=budget_session.epoch,
                )
                or budget_session
            )
            for field in ("billed_cost_usd", "total_cost_usd", "estimated_cost_usd"):
                candidate_metadata[f"session_{field}"] = float(
                    getattr(budget_session, field) or 0.0
                )
            candidate_metadata["session_cost_source"] = str(budget_session.cost_source or "")
        budget = common._budget_gate_input_for_turn(
            candidate_turn,
            router_cfg,
            routed_model=candidate_turn.model,
            material_tokens=max(
                1,
                len(
                    json.dumps(
                        [message.model_dump(mode="json") for message in messages],
                        ensure_ascii=False,
                        default=str,
                    )
                )
                // 3,
            ),
        )
        if budget_gate(target, valid_tiers=list(router_cfg.tiers), budget=budget).tier != target:
            return None
        try:
            validate_model_route_capacity(candidate_turn, tier=target, thinking_mode=thinking_mode)
        except (RuntimeError, ValueError):
            return None
        candidate_selector = self.selector.clone()
        from opensquilla.engine.selector_override import (
            apply_model_override,
            cross_provider_tier_config,
        )

        common._flag_tier_provider_mismatch(
            candidate_turn, router_cfg.tiers, target, routing_applied=True, force_direct=True
        )
        candidate_provider = apply_model_override(
            candidate_selector,
            candidate_turn.model,
            turn_metadata=candidate_metadata,
            realign_routed_model=False,
            strict_router_fallback_chain=[],
            tier_provider_config=cross_provider_tier_config(
                self.turn.config,
                candidate_metadata,
                candidate_turn.model,
                active_provider_id=candidate_selector.active_provider_id,
                session_key=self.turn.session_key,
            ),
        )
        deployment = candidate_selector.current_config
        model = str(deployment.model)
        provider_id = str(deployment.provider)
        if model != tier_cfg["model"] or provider_id != (
            tier_cfg.get("provider") or self.turn.config.llm.provider
        ):
            # A credential/continuity refusal may leave the selector on its
            # original deployment. That is not admission of the requested tier.
            return None
        catalog = shared_catalog()
        caps = catalog.resolve_deployment_capabilities(
            model,
            provider=provider_id,
            api_key=deployment.api_key,
            base_url=deployment.base_url,
        )
        llm = self.turn.config.llm
        window, window_source = resolve_effective_context_window(
            catalog,
            model,
            provider=provider_id,
            global_override=llm.context_window_tokens,
        )
        maximum, _ = catalog.resolve_max_tokens_with_source(
            model,
            user_override=llm.max_tokens,
            provider=provider_id,
        )
        vision_support = catalog.resolve_deployment_vision_support(
            model,
            provider=provider_id,
            api_key=deployment.api_key,
            base_url=deployment.base_url,
        )
        proof_budget = ContextBudgetGovernor.from_values(
            context_window_tokens=window,
            max_output_tokens=maximum,
            thinking_budget_tokens=thinking_budget,
            context_overflow_threshold=(
                context_overflow_threshold if context_overflow_threshold is not None
                else AgentConfig().context_overflow_threshold
            ),
            provider_request_proof_max_chars=chat_config.provider_request_max_chars_explicit_cap,
        ).snapshot().provider_request_max_chars
        bound = chat_config.model_copy(
            update={
                "max_tokens": maximum,
                "provider_context_window_tokens": window if window_source != "default" else 0,
                "provider_request_max_chars": proof_budget,
                "model_capabilities": caps,
                "model_vision_support": vision_support,
                "thinking": thinking_enabled,
                "thinking_level": thinking_level,
                "thinking_budget_tokens": thinking_budget,
                "thinking_budget_explicit": (
                    automatic_thinking or chat_config.thinking_budget_explicit
                ),
                "physical_attempt_limit": 1,
            }
        )
        from opensquilla.provider.execution_identity import rebind_execution_identity

        rebound = rebind_execution_identity(bound, provider=provider_id, model=model)
        assert rebound is not None
        bound = rebound
        if prepare_request is not None:
            messages, bound = prepare_request(
                candidate_provider, candidate_selector, messages, tools, bound,
            )
        if validate_provider_chat_admission(candidate_provider, messages, bound) is not None:
            return None
        projection = project_provider_final_request(candidate_provider, messages, tools, bound)
        if projection is None or not projection.fits:
            return None
        update = {
            "tier": target,
            "model": model,
            "provider": provider_id,
            "thinking_mode": thinking_mode,
            "prompt_policy": prompt_policy,
            "thinking_level": level,
        }
        committed = await self.manager.advance_model_router_quality_retry(
            self.turn.session_key,
            self.input_message_id,
            expected_session_id=self.session_id,
            expected_session_epoch=self.epoch,
            expected_version=int(state["version"]),
            target_tier=target,
            reason=reason,
            scope_id=scope_id,
            execution_update=update,
        )
        if committed is None:
            return None
        # No request is sent before the state CAS. An activation failure remains
        # an explicit failure; it must never dispatch the previous deployment.
        activate_prepared(candidate_provider, deployment)
        self.turn.model = model
        candidate_metadata["routing_extra"] = {
            **metadata.get("routing_extra", {}),
            "final_tier": target,
            "final_route_class": common.route_class_for_tier(target),
            "thinking_mode": thinking_mode,
            "prompt_policy": prompt_policy,
        }
        for key in tuple(metadata):
            if key.startswith("routed_provider_"):
                del metadata[key]
        metadata.update(candidate_metadata)
        metadata.update(
            applied_model=model,
            prompt_policy=prompt_policy,
            thinking_source="model_tier_policy" if automatic_thinking else "config",
            router_model_decision=committed,
            requested_provider=provider_id,
            requested_model=model,
            resolved_model=model,
        )
        metadata.pop("executed_provider", None)
        metadata.pop("executed_model", None)
        for key in ("savings_pct", "savings_max_price_per_m", "savings_routed_price_per_m"):
            metadata[key] = 0.0
        return {
            "model_id": model,
            "provider_id": provider_id,
            "max_tokens": bound.max_tokens,
            "context_window_tokens": window,
            "context_window_known": window_source != "default",
            "model_capabilities": caps,
            "model_vision_support": vision_support,
            "thinking": (thinking_level or True) if thinking_enabled else False,
            "thinking_budget_tokens": (
                bound.thinking_budget_tokens if bound.thinking_budget_explicit else -1
            ),
        }
