"""Typed chat session contexts for terminal and future chat frontends."""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from opensquilla.cli.chat.session_state import ChatSessionState

GatewayRuntimeScope = MutableMapping[str, Any]
StandaloneRuntimeScope = MutableMapping[str, Any]


@dataclass
class GatewaySessionContext:
    """Typed gateway session state with a legacy scope mirror."""

    state: ChatSessionState
    scope: GatewayRuntimeScope

    @classmethod
    def create(cls, state: ChatSessionState) -> GatewaySessionContext:
        context = cls(state=state, scope={})
        context.sync_from_state()
        return context

    @property
    def session_key(self) -> str:
        return self.state.session_key

    @property
    def model(self) -> str | None:
        return self.state.model

    def sync_from_state(self) -> None:
        self.scope["session_key"] = self.state.session_key
        self.scope["state"] = self.state
        self.scope["model"] = self.state.model


@dataclass
class StandaloneSessionContext:
    """Typed standalone session state with a legacy scope mirror."""

    state: ChatSessionState
    tool_ctx: object
    scope: StandaloneRuntimeScope

    @classmethod
    def create(
        cls,
        *,
        state: ChatSessionState,
        tool_ctx: object,
    ) -> StandaloneSessionContext:
        context = cls(state=state, tool_ctx=tool_ctx, scope={})
        context.sync_from_state()
        return context

    @property
    def session_key(self) -> str:
        return self.state.session_key

    @property
    def model(self) -> str | None:
        return self.state.model

    def replace_session(
        self,
        *,
        session_key: str,
        tool_ctx: object,
        state: ChatSessionState,
        model: str | None,
    ) -> None:
        state.session_key = session_key
        state.model = model
        self.state = state
        self.tool_ctx = tool_ctx
        self.sync_from_state()

    def sync_from_state(self) -> None:
        self.scope["session_key"] = self.state.session_key
        self.scope["tool_ctx"] = self.tool_ctx
        self.scope["state"] = self.state
        self.scope["model"] = self.state.model
