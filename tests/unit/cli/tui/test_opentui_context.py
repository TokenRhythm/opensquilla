from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from opensquilla.cli.tui.opentui.context import (
    context_update_from_bootstrap,
    send_context_patch,
)
from opensquilla.engine.commands import Surface


def test_context_update_uses_canonical_identity_and_hides_opaque_paths() -> None:
    update = context_update_from_bootstrap(
        {
            "agent_identity": {
                "agent_id": "main",
                "name": " Mira\x1b[31m\nOperator ",
                "emoji": "🦐",
                "avatar": "/private/mira.png",
            },
            "session": {
                "session_key": "agent:main:secret-session-id",
                "displayName": " TUI\nPolish ",
                "effective_model": "openai/gpt-5.4",
                "workspace": "/workspace/Developer/opensquilla",
            },
            "queue": {"running_count": 1, "queued_count": 2},
        },
        surface=Surface.CLI_GATEWAY,
        permission="workspace-write",
    )

    assert asdict(update) == {
        "agent": {"id": "main", "name": "Mira Operator", "emoji": "🦐"},
        "task": "TUI Polish",
        "surface": "Web + TUI",
        "gateway": "connected",
        "model": "openai/gpt-5.4",
        "permission": "workspace-write",
        "workspace": "opensquilla",
        "queue": "1 running · 2 queued",
        "context": "",
    }
    rendered = repr(asdict(update))
    assert "secret-session-id" not in rendered
    assert "/workspace/Developer" not in rendered
    assert "avatar" not in rendered


def test_context_update_has_safe_standalone_fallbacks() -> None:
    update = context_update_from_bootstrap(
        None,
        surface=Surface.CLI_STANDALONE,
        model=None,
        session_id=None,
        workspace_label=r"C:\work\squilla",
    )

    assert update.agent == {"id": "main", "name": "main", "emoji": None}
    assert update.task == "New session"
    assert update.surface == "TUI"
    assert update.gateway == "isolated"
    assert update.workspace == "squilla"
    assert update.queue == "idle"


@pytest.mark.asyncio
async def test_context_patch_is_additive_and_sanitized() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    class Output:
        async def send_message(self, kind: str, payload: dict[str, Any]) -> None:
            sent.append((kind, payload))

    await send_context_patch(
        Output(),
        model="gpt-5.4\x1b[31m",
        permission="workspace-write\n",
    )

    assert sent == [
        (
            "context.update",
            {"model": "gpt-5.4", "permission": "workspace-write"},
        )
    ]
