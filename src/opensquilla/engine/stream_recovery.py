"""One-deployment request snapshots for a bounded interrupted-stream recovery."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from opensquilla.provider.types import ChatConfig, Message, ToolDefinition


@dataclass(frozen=True, slots=True)
class StreamRecoveryRequest:
    provider: Any
    messages: list[Message]
    tools: list[ToolDefinition] | None
    config: ChatConfig
    account_usage: bool = False
    provider_id: str = ""
    model: str = ""

    @classmethod
    def capture(
        cls,
        provider: Any,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        config: ChatConfig,
        *,
        account_usage: bool = False,
        provider_id: str = "",
        model: str = "",
    ) -> StreamRecoveryRequest | None:
        if getattr(provider, "retry_failed_call_safe", True) is False:
            return None
        return cls(
            provider=provider,
            messages=copy.deepcopy(messages),
            tools=copy.deepcopy(tools),
            config=config.model_copy(deep=True, update={
                "physical_attempt_limit": 1,
                # Credential refresh precedes the original model request. A
                # response replay uses those credentials, not another refresh.
                "compatibility_recovery": (
                    None if config.compatibility_recovery == "refresh_credentials"
                    else config.compatibility_recovery
                ),
            }),
            account_usage=account_usage,
            provider_id=provider_id,
            model=model,
        )

    def open(self, execution_context: Any) -> AsyncIterator[Any]:
        def dispatch() -> AsyncIterator[Any]:
            kwargs: dict[str, Any] = {
                "tools": copy.deepcopy(self.tools),
                "config": self.config.model_copy(deep=True),
            }
            if getattr(self.provider, "execution_context_aware", False):
                kwargs["execution_context"] = execution_context
            return cast(
                AsyncIterator[Any], self.provider.chat(copy.deepcopy(self.messages), **kwargs),
            )

        if not self.account_usage:
            return dispatch()
        from opensquilla.engine.usage_accounting import account_provider_stream

        return account_provider_stream(dispatch, provider=self.provider_id, model=self.model)
