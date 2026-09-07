"""Gateway Adapters for narrow ancillary conversation Modules."""

from __future__ import annotations

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


class GatewayConversationAncillaryAdapter:
    """Wire projection for five independent application interfaces."""

    def __init__(
        self,
        *,
        usage: UsageReportingPort | None = None,
        commands: CommandCatalogPort | None = None,
        feedback: RouteFeedbackPort | None = None,
        prompt_cache: PromptCacheLeasePort | None = None,
        clarification: ClarificationSubmissionPort | None = None,
        prompt_cache_policy: PromptCachePolicy | None = None,
    ) -> None:
        self._usage = UsageReporting(usage) if usage is not None else None
        self._commands = CommandCatalog(commands) if commands is not None else None
        self._feedback = RouteFeedback(feedback) if feedback is not None else None
        self._clarification = (
            ClarificationSubmission(clarification)
            if clarification is not None
            else None
        )
        self._prompt_cache = (
            PromptCacheLease(prompt_cache, prompt_cache_policy)
            if prompt_cache is not None and prompt_cache_policy is not None
            else None
        )

    @staticmethod
    def _require[T](value: T | None, name: str) -> T:
        if value is None:
            raise RuntimeError(f"Ancillary runtime does not provide {name}")
        return value

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
        return dict(await self._require(self._usage, "usage status").status(query))

    async def usage_query(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(
            await self._require(self._usage, "usage query").query(
                UsageQuery(filters=self._raw(params))
            )
        )

    async def usage_cost(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(
            await self._require(self._usage, "usage cost").cost_breakdown(
                UsageQuery(filters=self._raw(params))
            )
        )

    async def list_commands(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        surface = raw.get("surface", "web")
        if not isinstance(surface, str):
            raise ValueError("params.surface must be a string")
        return dict(
            await self._require(self._commands, "command catalog").list(
                CommandCatalogQuery(surface)
            )
        )

    async def submit_feedback(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = self._raw(params)
        decision_id = raw.get("decisionId", raw.get("decision_id"))
        rating = raw.get("rating")
        if not isinstance(decision_id, str):
            raise ValueError("params.decisionId must be a string")
        if rating not in {"up", "down", "neutral"}:
            raise ValueError("params.rating must be up, down, or neutral")
        return dict(
            await self._require(self._feedback, "route feedback").submit(
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
            await self._require(self._clarification, "clarification").submit(
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
]
