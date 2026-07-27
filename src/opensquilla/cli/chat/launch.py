"""Typed launch contracts shared by chat entrypoints and frontends."""

from __future__ import annotations

from dataclasses import dataclass

from opensquilla.cli.chat.frontend import ChatCommandLauncher, ChatSessionRunner

type ChatCommandRunner = ChatSessionRunner


@dataclass(frozen=True)
class ChatCommandRequest:
    model: str
    session_id: str
    standalone: bool
    workspace: str
    workspace_strict: bool | None
    timeout: float | None
    ui: str | None = None


@dataclass(frozen=True)
class ChatCommandLaunchOverrides:
    launch_chat: ChatCommandLauncher | None = None
    standalone_runner: ChatSessionRunner | None = None
    gateway_runner: ChatSessionRunner | None = None
