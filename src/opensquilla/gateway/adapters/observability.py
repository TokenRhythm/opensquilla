"""Gateway implementations of the observability Application Ports."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from opensquilla import __version__
from opensquilla.agent_ids import normalize_agent_id
from opensquilla.application.observability import (
    ReadinessEvaluationPort,
    ReadinessFinding,
    ReadinessFixStep,
    RuntimeStatusPort,
)
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.health.evaluator import (
    evaluate_channels,
    evaluate_image_generation,
    evaluate_llm_ensemble,
    evaluate_logs,
    evaluate_memory,
    evaluate_memory_embedding,
    evaluate_provider,
    evaluate_router,
    evaluate_sandbox,
    evaluate_search,
    evaluate_squilla_router_runtime,
)
from opensquilla.health.model import FixStep, HealthFinding, build_report
from opensquilla.health.recovery_commands import command_with_config


class GatewayRuntimeStatusPort(RuntimeStatusPort):
    def __init__(self, context: RpcContext) -> None:
        self._context = context

    async def snapshot(self) -> Mapping[str, Any]:
        from opensquilla.gateway.boot import _boot_time_ms

        now = int(time.time() * 1000)
        uptime = now - _boot_time_ms if _boot_time_ms > 0 else 0
        provider_name = None
        selector = self._context.provider_selector
        if selector is not None and getattr(selector, "is_configured", True):
            provider_name = getattr(selector, "active_provider_id", None)
            if not provider_name:
                try:
                    provider = selector.resolve()
                    provider_name = getattr(provider, "provider_name", None)
                except Exception:  # noqa: BLE001 - status is best-effort.
                    pass

        active_sessions = 0
        if self._context.session_manager is not None:
            storage = get_session_storage(self._context.session_manager)
            if storage is not None:
                try:
                    active_sessions = len(await storage.list_sessions(limit=1000))
                except Exception:  # noqa: BLE001 - status is best-effort.
                    pass

        return {
            "status": "running",
            "version": __version__,
            "uptime_ms": uptime,
            "provider": provider_name,
            "active_sessions": active_sessions,
        }


_READINESS_EVALUATORS = {
    "provider": evaluate_provider,
    "logs": evaluate_logs,
    "memory": evaluate_memory,
    "channels": evaluate_channels,
    "sandbox": evaluate_sandbox,
    "router": evaluate_router,
    "squilla_router": evaluate_squilla_router_runtime,
    "memory_embedding": evaluate_memory_embedding,
    "search": evaluate_search,
    "image_generation": evaluate_image_generation,
    "llm_ensemble": evaluate_llm_ensemble,
}


class GatewayReadinessEvaluationPort(ReadinessEvaluationPort):
    def normalize_agent_id(self, value: str) -> str:
        return normalize_agent_id(value)

    def evaluate(
        self, surface: str, payload: Mapping[str, Any]
    ) -> Sequence[ReadinessFinding]:
        return tuple(
            _to_application_finding(finding)
            for finding in _READINESS_EVALUATORS[surface](dict(payload))
        )

    def build_report(
        self,
        findings: Sequence[ReadinessFinding],
        *,
        config_path: str | None,
    ) -> dict[str, Any]:
        health_findings = [_to_health_finding(finding) for finding in findings]
        if config_path:
            health_findings = [
                replace(
                    finding,
                    fix_steps=[
                        replace(step, command=command_with_config(step.command, config_path))
                        if step.command
                        else step
                        for step in finding.fix_steps
                    ],
                )
                for finding in health_findings
            ]
        return build_report(health_findings)


def _to_application_finding(finding: HealthFinding) -> ReadinessFinding:
    return ReadinessFinding(
        id=finding.id,
        severity=finding.severity,
        surface=finding.surface,
        title=finding.title,
        detail=finding.detail,
        readiness_impact=finding.readiness_impact,
        evidence=finding.evidence,
        fix_steps=tuple(
            ReadinessFixStep(label=step.label, command=step.command, detail=step.detail)
            for step in finding.fix_steps
        ),
        restart_required=finding.restart_required,
    )


def _to_health_finding(finding: ReadinessFinding) -> HealthFinding:
    return HealthFinding(
        id=finding.id,
        severity=finding.severity,
        surface=finding.surface,
        title=finding.title,
        detail=finding.detail,
        readiness_impact=finding.readiness_impact,
        evidence=dict(finding.evidence or {}),
        fix_steps=[
            FixStep(label=step.label, command=step.command, detail=step.detail)
            for step in finding.fix_steps
        ],
        restart_required=finding.restart_required,
    )


__all__ = [
    "GatewayReadinessEvaluationPort",
    "GatewayRuntimeStatusPort",
]
