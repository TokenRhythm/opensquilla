"""Background model assessments consumed only at complete request boundaries."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace
from typing import Any, Literal, cast

import structlog

from opensquilla.engine.history import HistoryReplayProjection, project_history_replay_capacity
from opensquilla.engine.steps.jev_router import (
    apply_jev_router,
    jev_input_fingerprint,
    resolve_jev_deployment,
)
from opensquilla.provider.types import ContentBlockToolResult

log = structlog.get_logger(__name__)
_TOOL_FAILURE_ASSESSMENT_THRESHOLD = 2


class JevExecution:
    """Own the lifetime of background advice for one already admitted turn."""

    def __init__(
        self, *, turn: Any, service: Any, session_manager: Any, selector: Any,
        catalog: Any, expected_session_id: str | None, expected_session_epoch: int | None,
        ensemble_catalog: Any = None,
    ) -> None:
        self.turn = turn
        self.service = service
        self.manager = session_manager
        self.selector = selector
        self.catalog = catalog
        self.ensemble_catalog = (
            ensemble_catalog if ensemble_catalog is not None
            else catalog if callable(getattr(catalog, "resolve_deployment_limits", None)) else None
        )
        self.session_id = expected_session_id
        self.session_epoch = expected_session_epoch
        self.owner = uuid.uuid4().hex
        self.fingerprint = jev_input_fingerprint(turn.semantic_message, turn.attachments)
        self.task: asyncio.Task[Any] | None = None
        self.handled: set[str] = set()
        self.last_check_iteration = 1
        self.initial_assessment_complete = False
        self._tool_result_baseline_captured = False
        self._observed_tool_results: dict[str, bool] = {}
        self._consecutive_tool_failures = 0
        self._consecutive_failure_evidence: tuple[str, ...] = ()
        self._pending_tool_failure_count = 0
        self._pending_tool_failure_evidence: tuple[str, ...] = ()
        self.closed = False
        service.claim_execution(turn.session_key, self.owner)

    async def before_request(self, agent: Any, iteration: int, messages: list[Any]) -> bool:
        """Assess before the first dispatch; later checks only gate a ready choice."""
        if self.closed:
            return False
        settings = await self.service.settings(self.turn.session_key, self.turn.config.jev_router)
        if not settings.get("enabled", False):
            return False
        self._observe_tool_results(messages)
        if self.task is None or self.task.cancelled():
            self.task = self.service.current_check(self.turn.session_key, self.fingerprint)
            if self.task is None:
                self.task = await self._start(iteration, messages, stage="input")
        if not self.initial_assessment_complete:
            # The bounded input assessment belongs before the first model HTTP
            # request. Subsequent checkpoint assessments run alongside execution.
            try:
                await self.task
            except Exception as exc:
                log.warning("jev.initial_assessment_failed", error_type=type(exc).__name__)
            self.initial_assessment_complete = True
        changed = False
        if self.task.done() and not self.task.cancelled():
            try:
                result = self.task.result()
            except Exception as exc:  # auxiliary failures never own the main turn
                log.warning("jev.background_failed", error_type=type(exc).__name__)
            else:
                decision_id = result.metadata.get("jev_decision_id") if result else None
                if decision_id and decision_id not in self.handled:
                    self.handled.add(decision_id)
                    changed = await self._consume(agent, decision_id, result)
        if changed:
            self.last_check_iteration = iteration
            self._clear_assessed_tool_failures()
        interval = max(1, int(settings.get("loop_interval", 5)))
        periodic_due = iteration - self.last_check_iteration >= interval
        failure_due = self._pending_tool_failure_count >= _TOOL_FAILURE_ASSESSMENT_THRESHOLD
        if (periodic_due or failure_due) and self.task.done():
            if periodic_due:
                self.last_check_iteration = iteration
            self.task = await self._start(
                iteration, messages, stage="loop", expedited_tool_failures=failure_due,
            )
        return changed

    def _observe_tool_results(self, messages: list[Any]) -> None:
        results = [
            block
            for message in messages
            if getattr(message, "role", None) in {"user", "tool"}
            for block in (
                message.content if isinstance(getattr(message, "content", None), list) else []
            )
            if isinstance(block, ContentBlockToolResult) and block.tool_use_id.strip()
        ]
        if not self._tool_result_baseline_captured:
            self._observed_tool_results.update(
                (block.tool_use_id, block.is_error) for block in results
            )
            self._tool_result_baseline_captured = True
            return
        for block in results:
            identity = block.tool_use_id
            if identity in self._observed_tool_results:
                # A resolved retry may break a streak, but replaying or retrying
                # an already observed call never creates another failed call.
                if self._observed_tool_results[identity] and not block.is_error:
                    self._observed_tool_results[identity] = False
                    self._consecutive_tool_failures = 0
                    self._consecutive_failure_evidence = ()
                continue
            self._observed_tool_results[identity] = block.is_error
            if not block.is_error:
                self._consecutive_tool_failures = 0
                self._consecutive_failure_evidence = ()
                continue
            self._consecutive_tool_failures += 1
            diagnostic = _tool_result_text(block) or "No textual diagnostic was returned."
            self._consecutive_failure_evidence = (
                *self._consecutive_failure_evidence,
                f"[Tool execution failed] {diagnostic}"[:1200],
            )[-2:]
            if self._consecutive_tool_failures >= _TOOL_FAILURE_ASSESSMENT_THRESHOLD:
                self._pending_tool_failure_count = self._consecutive_tool_failures
                self._pending_tool_failure_evidence = self._consecutive_failure_evidence

    def _clear_assessed_tool_failures(self) -> None:
        self._consecutive_tool_failures = 0
        self._consecutive_failure_evidence = ()
        self._pending_tool_failure_count = 0
        self._pending_tool_failure_evidence = ()

    async def _start(
        self, iteration: int, messages: list[Any], *, stage: Literal["input", "loop"],
        expedited_tool_failures: bool = False,
    ) -> asyncio.Task[Any]:
        metadata = dict(self.turn.metadata)
        metadata.pop("jev_decision_id", None)
        metadata.pop("jev_pending_application", None)
        metadata.pop("jev_target", None)
        tool_evidence: tuple[str, ...] = ()
        if stage == "loop":
            metadata["jev_tool_failure_count"] = max(
                self._pending_tool_failure_count, self._consecutive_tool_failures,
            )
            if expedited_tool_failures:
                metadata["jev_assessment_trigger"] = "tool_failures"
            capacity = project_history_replay_capacity(
                HistoryReplayProjection(messages=tuple(messages)),
            )
            metadata["routing_history_capacity_estimated_tokens"] = capacity.estimated_tokens
            metadata["routing_history_capacity_message_count"] = capacity.message_count
            metadata["routing_history_capacity_estimate_complete"] = capacity.estimate_complete
            metadata["router_prev_assistant_text"] = _recent_assistant_text(messages)
            tool_evidence = (
                self._pending_tool_failure_evidence
                if expedited_tool_failures else _recent_tool_results(messages)
            )
            metadata["jev_requires_tools"] = any(
                getattr(block, "type", "") in {"tool_use", "tool_result"}
                for message in messages
                for block in (
                    message.content if isinstance(getattr(message, "content", None), list) else []
                )
            )
        inherited = getattr(self.selector, "current_config", None)
        context = replace(
            self.turn, metadata=metadata,
            model=str(getattr(inherited, "model", "") or self.turn.model),
        )
        fingerprint = self.fingerprint if stage == "input" else f"{self.owner}:{iteration}"

        async def evaluate(generation: int) -> Any:
            return await apply_jev_router(
                context, service=self.service, input_id=fingerprint, inherited=inherited,
                routing_revision=await self._routing_revision(), assessment_stage=stage,
                check_generation=generation,
                recent_tool_results=tool_evidence,
                loop_iteration=iteration if stage == "loop" else 0,
            )

        self._clear_assessed_tool_failures()
        return cast(
            asyncio.Task[Any],
            await self.service.start_check(self.turn.session_key, fingerprint, evaluate),
        )

    async def _routing_revision(self) -> int | None:
        if self.manager is None:
            return None
        session = await self.manager.get_session(self.turn.session_key)
        return int(getattr(session, "model_routing_revision", 0) or 0) if session else None

    async def _consume(self, agent: Any, decision_id: str, result: Any) -> bool:
        key = self.turn.session_key
        decision = await self.service.get_decision(key, decision_id)
        if not decision or decision.get("action") not in {"switch", "review", "ensemble"}:
            return False
        if decision and decision.get("status") == "pending":
            await self.service.set_blocked(key, True)
            try:
                while decision.get("status") == "pending":
                    deadline = decision.get("autoSwitchAt")
                    if isinstance(deadline, (float, int)) and time.time() * 1000 >= deadline:
                        await self.service.settle_deadline(
                            key, decision_id, defaults=self.turn.config.jev_router,
                            now_ms=int(time.time() * 1000),
                        )
                    else:
                        await asyncio.sleep(0.1)
                    decision = await self.service.get_decision(key, decision_id)
                    if not decision:
                        return False
            finally:
                await self.service.set_blocked(key, False)
        if not decision or decision.get("status") not in {"accepted", "applied"}:
            return False
        settings = await self.service.settings(key, self.turn.config.jev_router)
        if not settings.get("enabled", False):
            if decision.get("status") == "accepted":
                await self.service.expire(key, decision_id)
            return False
        target = decision.get("target")
        if decision.get("action") == "ensemble":
            return await self._activate_ensemble(agent, decision_id)
        if not target or self.manager is None or self.selector is None:
            await self.service.expire(key, decision_id)
            return False
        return await self._activate(agent, decision_id, target)

    async def _activate_ensemble(self, agent: Any, decision_id: str) -> bool:
        from opensquilla.engine.jev_ensemble import prepare_jev_ensemble

        key = self.turn.session_key
        async with self.service.response_lock:
            decision = await self.service.get_decision(key, decision_id)
            settings = await self.service.settings(key, self.turn.config.jev_router)
            if (
                not decision or decision.get("status") not in {"accepted", "applied"}
                or not settings.get("enabled", False)
                or self.manager is None or self.selector is None
            ):
                return False
            descriptor = decision.get("ensemble")
            if not isinstance(descriptor, dict):
                await self.service.expire(key, decision_id)
                return False
            session = await self.manager.get_session(key)
            if session is None or (
                self.session_id is not None and session.session_id != self.session_id
                or self.session_epoch is not None and session.epoch != self.session_epoch
            ):
                await self.service.expire(key, decision_id)
                return False
            revision = await self.service.routing_revision(key, decision_id)
            if revision is not None and revision != int(session.model_routing_revision or 0):
                await self.service.expire(key, decision_id)
                return False
            candidate_selector = self.selector.clone()
            deployment = candidate_selector.current_config
            try:
                prepared = prepare_jev_ensemble(
                    self.turn.config, inherited=deployment,
                    fallback_provider=candidate_selector.resolve(), catalog=self.ensemble_catalog,
                    session_key=key, acquire=True, fallback_selector=candidate_selector,
                    expected_fingerprint=descriptor.get("configurationFingerprint"),
                )
                if prepared is None:
                    raise ValueError("Ensemble plan is no longer available")
                if getattr(agent, "tool_definitions", ()) and not prepared.supports_tools:
                    raise ValueError("Ensemble aggregator cannot preserve tool capability")
                committed = await self.manager.set_session_routing(
                    key, "ensemble", expected_revision=int(session.model_routing_revision or 0),
                )
            except Exception as exc:
                log.warning("jev.ensemble_unavailable", error_type=type(exc).__name__)
                await self.service.expire(key, decision_id)
                return False
            prepared.provider.activate_provider_state_replay_boundary()
            activate = getattr(agent.provider, "activate_prepared_deployment", None)
            if callable(activate):
                activate(prepared.provider, prepared.deployment)
            else:
                self.selector.override_provider_config(
                    prepared.deployment, preserve_existing_tail=False,
                )
                agent.provider = prepared.provider
            metadata = {
                "ensemble_enabled": True, "ensemble_activation_source": "jev",
                "ensemble_selection_mode": prepared.descriptor["strategy"],
                "jev_ensemble": dict(prepared.descriptor),
                "jev_execution_mode": "ensemble",
            }
            self.turn.metadata.update(metadata)
            agent.config.metadata.update(metadata)
            agent.config.model_capabilities = replace(
                agent.config.model_capabilities,
                supports_tools=prepared.supports_tools, supports_vision=False,
            )
            agent.config.model_vision_support = "unsupported"
            await self.service.mark_applied(
                key, decision_id, routing_revision=committed["revision"],
            )
        await self.service.notify_routing_change(key)
        await self.service.notify(key)
        return True

    async def _activate(self, agent: Any, decision_id: str, target: dict[str, str]) -> bool:
        async with self.service.response_lock:
            decision = await self.service.get_decision(self.turn.session_key, decision_id)
            settings = await self.service.settings(
                self.turn.session_key, self.turn.config.jev_router,
            )
            if (
                not decision or decision.get("status") not in {"accepted", "applied"}
                or not settings.get("enabled", False)
            ):
                return False
            return await self._activate_approved(agent, decision_id, target)

    async def _activate_approved(
        self, agent: Any, decision_id: str, target: dict[str, str],
    ) -> bool:
        from opensquilla.engine.selector_override import apply_model_override

        key = self.turn.session_key
        session = await self.manager.get_session(key)
        if session is None or (
            self.session_id is not None and session.session_id != self.session_id
            or self.session_epoch is not None and session.epoch != self.session_epoch
        ):
            await self.service.expire(key, decision_id)
            return False
        revision = await self.service.routing_revision(key, decision_id)
        if revision is not None and revision != int(session.model_routing_revision or 0):
            await self.service.expire(key, decision_id)
            return False
        resolved = resolve_jev_deployment(
            self.turn.config, target, inherited=self.selector.current_config,
            session_key=key, acquire=True,
        )
        if not resolved.ready:
            await self.service.expire(key, decision_id)
            return False
        candidate_selector = self.selector.clone()
        metadata = dict(self.turn.metadata)
        try:
            provider = apply_model_override(
                candidate_selector, target["model"], turn_metadata=metadata,
                realign_routed_model=False, tier_provider_config=resolved.provider_config,
                strict_router_fallback_chain=[],
            )
            deployment = candidate_selector.current_config
            if deployment.provider != target["provider"] or deployment.model != target["model"]:
                raise ValueError("Prepared deployment differs from the approved target")
            # Capacity declarations on the previous model do not establish the
            # physical limits of a different model. Respect its own catalog.
            catalog = self.catalog.lookup_deployment(deployment, include_global_overrides=False)
            # Resolve the complete replacement before its durable CAS. The same
            # live messages still pass normal compaction and physical request proof.
            committed = await self.manager.set_session_routing(
                key, "direct", expected_revision=int(session.model_routing_revision or 0),
                update_model=True, model=target["model"], provider=target["provider"],
            )
        except Exception as exc:  # failed preparation/CAS retains the active deployment
            log.warning("jev.switch_unavailable", error_type=type(exc).__name__)
            await self.service.expire(key, decision_id)
            return False
        activate = getattr(agent.provider, "activate_prepared_deployment", None)
        if callable(activate):
            activate(provider, deployment)
        else:
            self.selector.override_provider_config(deployment, preserve_existing_tail=False)
            agent.provider = provider
        for name, value in {
            "model_id": target["model"], "provider_id": target["provider"],
            "model_capabilities": catalog.capabilities,
            "model_vision_support": catalog.vision_support,
            "context_window_tokens": catalog.context_window,
            "context_window_known": catalog.context_window_known,
            "max_tokens": min(agent.config.max_tokens, catalog.max_tokens),
            "provider_request_proof_max_chars": catalog.provider_request_proof_max_chars,
        }.items():
            setattr(agent.config, name, value)
        self.turn.model = target["model"]
        for name in (
            "ensemble_enabled", "ensemble_activation_source", "ensemble_selection_mode",
            "jev_ensemble",
        ):
            metadata.pop(name, None)
        metadata["jev_execution_mode"] = "direct"
        self.turn.metadata.update(metadata)
        self.turn.metadata["jev_target"] = dict(target)
        self.turn.metadata.pop("ensemble_enabled", None)
        self.turn.metadata.pop("ensemble_activation_source", None)
        self.turn.metadata.pop("ensemble_selection_mode", None)
        self.turn.metadata.pop("jev_ensemble", None)
        agent.config.metadata.pop("ensemble_enabled", None)
        agent.config.metadata.pop("ensemble_activation_source", None)
        agent.config.metadata.pop("ensemble_selection_mode", None)
        agent.config.metadata.pop("jev_ensemble", None)
        agent.config.metadata.update(metadata)
        await self.service.mark_applied(key, decision_id, routing_revision=committed["revision"])
        await self.service.notify_routing_change(key)
        await self.service.notify(key)
        return True

    async def close(self) -> None:
        self.closed = True
        await self.service.cancel_checks(self.turn.session_key, preserve_context_notices=True)
        self.service.release_execution(self.turn.session_key, self.owner)
        await self.service.set_blocked(self.turn.session_key, False)


def _recent_assistant_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if getattr(message, "role", None) != "assistant":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content[-800:]
        if isinstance(content, list):
            return " ".join(
                str(getattr(block, "text", "")) for block in content
                if getattr(block, "type", "") == "text"
            )[-800:]
    return ""


def _recent_tool_results(messages: list[Any]) -> tuple[str, ...]:
    """Keep a small text-only checkpoint snapshot outside durable turn metadata."""
    results: list[str] = []
    for message in reversed(messages):
        content = getattr(message, "content", "")
        if isinstance(content, list):
            for block in reversed(content):
                if getattr(block, "type", "") != "tool_result":
                    continue
                text = _tool_result_text(block)
                if text:
                    results.append(text)
                    if len(results) == 2:
                        return tuple(results)
    return tuple(results)


def _tool_result_text(block: Any) -> str:
    result = getattr(block, "content", "")
    if isinstance(result, str):
        return result[:1200]
    if isinstance(result, list):
        return " ".join(
            str(getattr(item, "text", ""))[:1200] for item in result
            if getattr(item, "type", "") == "text"
        )[:1200]
    return ""
