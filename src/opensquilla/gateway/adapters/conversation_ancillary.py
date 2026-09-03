"""Gateway Adapters for narrow ancillary conversation Modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from opensquilla.application.conversation_ancillary import (
    ClarificationSubmission,
    ClarificationSubmissionPort,
    CommandCatalog,
    CommandCatalogPort,
    CommandCatalogQuery,
    PromptCacheLease,
    PromptCacheLeasePort,
    PromptCachePolicy,
    RouteFeedback,
    RouteFeedbackPort,
    RouteFeedbackRating,
    SetPromptCacheLease,
    SubmitClarification,
    SubmitRouteFeedback,
    UsageQuery,
    UsageReporting,
    UsageReportingPort,
)
from opensquilla.gateway.rpc import RpcContext

type AncillaryExecutor = Callable[
    [dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]
]


@dataclass(frozen=True, slots=True)
class GatewayConversationAncillaryCallbacks:
    usage_status: AncillaryExecutor | None = None
    usage_query: AncillaryExecutor | None = None
    usage_cost: AncillaryExecutor | None = None
    command_catalog: AncillaryExecutor | None = None
    route_feedback: AncillaryExecutor | None = None
    prompt_cache_status: AncillaryExecutor | None = None
    prompt_cache_set: AncillaryExecutor | None = None
    clarification: AncillaryExecutor | None = None


def _callback(value: AncillaryExecutor | None, name: str) -> AncillaryExecutor:
    if value is None:
        raise RuntimeError(f"Ancillary runtime does not provide {name}")
    return value


class GatewayUsageReportingPort(UsageReportingPort):
    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayConversationAncillaryCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    @staticmethod
    def _params(query: UsageQuery) -> dict[str, Any]:
        params = dict(query.filters or {})
        if query.session_key is not None:
            params["sessionKey"] = query.session_key
        return params

    async def status(self, query: UsageQuery) -> Mapping[str, Any]:
        return await _callback(self._callbacks.usage_status, "usage status")(
            self._params(query), self._context
        )

    async def query(self, query: UsageQuery) -> Mapping[str, Any]:
        return await _callback(self._callbacks.usage_query, "usage query")(
            self._params(query), self._context
        )

    async def cost_breakdown(self, query: UsageQuery) -> Mapping[str, Any]:
        return await _callback(self._callbacks.usage_cost, "usage cost")(
            self._params(query), self._context
        )


class GatewayCommandCatalogPort(CommandCatalogPort):
    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayConversationAncillaryCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    async def list(self, query: CommandCatalogQuery) -> Mapping[str, Any]:
        return await _callback(self._callbacks.command_catalog, "command catalog")(
            {"surface": query.surface}, self._context
        )


class GatewayRouteFeedbackPort(RouteFeedbackPort):
    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayConversationAncillaryCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    async def submit(self, command: SubmitRouteFeedback) -> Mapping[str, Any]:
        return await _callback(self._callbacks.route_feedback, "route feedback")(
            {"decisionId": command.decision_id, "rating": command.rating},
            self._context,
        )


class GatewayPromptCacheLeasePort(PromptCacheLeasePort):
    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayConversationAncillaryCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    async def status(self, session_key: str) -> Mapping[str, Any]:
        return await _callback(self._callbacks.prompt_cache_status, "prompt-cache status")(
            {"key": session_key}, self._context
        )

    async def set_policy(self, command: SetPromptCacheLease) -> Mapping[str, Any]:
        return await _callback(self._callbacks.prompt_cache_set, "prompt-cache set")(
            {
                "key": command.session_key,
                "enabled": command.enabled,
                "ttlSeconds": command.ttl_seconds,
                "idleTimeoutSeconds": command.idle_timeout_seconds,
            },
            self._context,
        )


class GatewayClarificationSubmissionPort(ClarificationSubmissionPort):
    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayConversationAncillaryCallbacks,
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    async def submit(self, command: SubmitClarification) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "sessionKey": command.session_key,
            "fields": dict(command.fields),
        }
        if command.request_id is not None:
            params["requestId"] = command.request_id
        if command.run_id is not None:
            params["run_id"] = command.run_id
        return await _callback(self._callbacks.clarification, "clarification")(
            params, self._context
        )


class GatewayConversationAncillaryAdapter:
    """Wire projection for five independent application interfaces."""

    def __init__(
        self,
        context: RpcContext,
        callbacks: GatewayConversationAncillaryCallbacks,
        *,
        prompt_cache_policy: PromptCachePolicy | None = None,
    ) -> None:
        self._usage = UsageReporting(GatewayUsageReportingPort(context, callbacks))
        self._commands = CommandCatalog(GatewayCommandCatalogPort(context, callbacks))
        self._feedback = RouteFeedback(GatewayRouteFeedbackPort(context, callbacks))
        self._clarification = ClarificationSubmission(
            GatewayClarificationSubmissionPort(context, callbacks)
        )
        self._prompt_cache = (
            PromptCacheLease(
                GatewayPromptCacheLeasePort(context, callbacks),
                prompt_cache_policy,
            )
            if prompt_cache_policy is not None
            else None
        )

    @staticmethod
    def _raw(params: dict[str, Any] | None) -> dict[str, Any]:
        return params if isinstance(params, dict) else {}

    async def usage_status(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        key = raw.get("sessionKey", raw.get("session_key", raw.get("key")))
        query = UsageQuery(
            session_key=key if isinstance(key, str) else None,
            filters=raw,
        )
        return dict(await self._usage.status(query))

    async def usage_query(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._usage.query(UsageQuery(filters=self._raw(params))))

    async def usage_cost(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._usage.cost_breakdown(UsageQuery(filters=self._raw(params))))

    async def list_commands(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        surface = raw.get("surface", "web")
        if not isinstance(surface, str):
            raise ValueError("params.surface must be a string")
        return dict(await self._commands.list(CommandCatalogQuery(surface)))

    async def submit_feedback(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        decision_id = raw.get("decisionId", raw.get("decision_id"))
        rating = raw.get("rating")
        if not isinstance(decision_id, str):
            raise ValueError("params.decisionId must be a string")
        if rating not in {"up", "down", "neutral"}:
            raise ValueError("params.rating must be up, down, or neutral")
        return dict(
            await self._feedback.submit(
                SubmitRouteFeedback(
                    decision_id=decision_id,
                    rating=cast(RouteFeedbackRating, rating),
                )
            )
        )

    def _prompt(self) -> PromptCacheLease:
        if self._prompt_cache is None:
            raise RuntimeError("Prompt-cache policy is not configured")
        return self._prompt_cache

    async def prompt_cache_status(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        key = raw.get("key")
        if not isinstance(key, str):
            raise ValueError("params.key must be a complete session key")
        return dict(await self._prompt().status(key))

    async def prompt_cache_set(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        key = raw.get("key")
        enabled = raw.get("enabled")
        ttl = raw.get("ttlSeconds")
        idle = raw.get("idleTimeoutSeconds")
        if not isinstance(key, str):
            raise ValueError("params.key must be a complete session key")
        if type(enabled) is not bool:
            raise ValueError("params.enabled must be a boolean")
        if ttl is not None and type(ttl) is not int:
            raise ValueError("params.ttlSeconds must be an integer")
        if idle is not None and type(idle) is not int:
            raise ValueError("params.idleTimeoutSeconds must be an integer")
        return dict(
            await self._prompt().set_policy(
                SetPromptCacheLease(
                    session_key=key,
                    enabled=enabled,
                    ttl_seconds=ttl,
                    idle_timeout_seconds=idle,
                )
            )
        )

    async def submit_clarification(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params required: sessionKey, fields")
        fields = params.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("params.fields must be a non-empty mapping")
        key = params.get("sessionKey", params.get("key"))
        if not isinstance(key, str):
            raise ValueError("params.sessionKey must be a string")
        request_id = params.get("request_id", params.get("requestId"))
        run_id = params.get("run_id")
        return dict(
            await self._clarification.submit(
                SubmitClarification(
                    session_key=key,
                    fields=fields,
                    request_id=str(request_id) if request_id is not None else None,
                    run_id=run_id if isinstance(run_id, str) else None,
                )
            )
        )


__all__ = [
    "GatewayConversationAncillaryAdapter",
    "GatewayConversationAncillaryCallbacks",
]
