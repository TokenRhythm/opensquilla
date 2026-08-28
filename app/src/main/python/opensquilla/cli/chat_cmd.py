"""Chat command — interactive chat mode with Rich output.

Two modes:
- Default (gateway): Connect to running gateway daemon via WebSocket. Full features.
- --standalone: TurnRunner-based direct mode, no gateway daemon needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import typer

from opensquilla.cli.chat.entrypoint import (
    LazyChatCommandAll as _LazyChatCommandAll,
)
from opensquilla.cli.chat.entrypoint import (
    legacy_chat_export as _legacy_chat_export,
)
from opensquilla.cli.chat.entrypoint import (
    legacy_chat_export_names as _legacy_chat_export_names,
)
from opensquilla.cli.chat.entrypoint import run_chat_request as _run_chat_request
from opensquilla.cli.chat.launch import (
    ChatCommandLaunchOverrides as _ChatCommandLaunchOverrides,
)
from opensquilla.cli.chat.launch import ChatCommandRequest as _ChatCommandRequest

__all__: Sequence[str] = _LazyChatCommandAll()


def __dir__() -> list[str]:
    return sorted({*globals(), *_legacy_chat_export_names()})


def __getattr__(name: str) -> Any:
    return _legacy_chat_export(name)


def run_chat(
    model: str = typer.Option("", "--model", "-m", help="Model override (provider/model)"),
    session_id: str = typer.Option("", "--session", "-s", help="Resume session ID"),
    ui: str | None = None,
    standalone: bool = typer.Option(False, "--standalone", help="Direct Agent without gateway"),
    workspace: str = typer.Option("", "--workspace", help="Workspace root for standalone tools"),
    workspace_strict: bool | None = typer.Option(
        None,
        "--workspace-strict/--no-workspace-strict",
        help="Restrict read-side file tools to --workspace in standalone mode",
    ),
    timeout: float | None = None,
) -> None:
    """Start interactive chat with the agent.

    Default: connects to the running gateway daemon for full features
    (tools, skills, session persistence). Use --standalone for direct
    TurnRunner mode without a gateway daemon.
    """
    _run_chat_request(
        _ChatCommandRequest(
            model=model,
            session_id=session_id,
            ui=ui,
            standalone=standalone,
            workspace=workspace,
            workspace_strict=workspace_strict,
            timeout=timeout,
        ),
        module_globals=globals(),
    )


type _ChatCommandLaunchOverridesType = _ChatCommandLaunchOverrides
