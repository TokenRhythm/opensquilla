"""Deterministic Gateway process used by the packaged-host TUI release gate.

This file is launched with the gate virtualenv's Python executable.  It must
therefore use only the installed OpenSquilla wheel and stdlib dependencies;
the release workflow deliberately removes the checkout's ``PYTHONPATH``.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.websocket import SubscriptionManager
from opensquilla.provider import ChatConfig, DoneEvent, Message, ModelInfo, TextDeltaEvent

_MODEL = "e2e/deterministic"


def _message_text(message: Message) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return repr(content)


class DeterministicProvider:
    """Small provider with one cancellable slow response for queue coverage."""

    provider_name = "e2e"

    def __init__(self, event_log: Path) -> None:
        self._event_log = event_log

    def _record(self, event: str, **payload: Any) -> None:
        self._event_log.parent.mkdir(parents=True, exist_ok=True)
        with self._event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **payload}, sort_keys=True) + "\n")

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,  # noqa: ARG002
        config: ChatConfig | None = None,  # noqa: ARG002
    ) -> AsyncIterator[Any]:
        text = "\n".join(_message_text(message) for message in messages)
        marker = next(
            (
                value
                for value in (
                    "E2E_HOLD_QUEUE",
                    "E2E_QUEUED_CANCEL",
                    "E2E_WEB_EXTERNAL",
                    "E2E_SEED_ATTACHMENT",
                )
                if value in text
            ),
            "E2E_GENERIC",
        )
        self._record("provider.start", marker=marker)
        try:
            if marker == "E2E_HOLD_QUEUE":
                # The test cancels this turn after observing a second queued
                # task.  A long wait makes that state deterministic on slower
                # Intel runners without adding wall time on the success path.
                await asyncio.sleep(120)
            replies = {
                "E2E_SEED_ATTACHMENT": "E2E_SEED_REPLY",
                "E2E_WEB_EXTERNAL": "E2E_WEB_REPLY",
                "E2E_QUEUED_CANCEL": "E2E_QUEUED_REPLY",
                "E2E_HOLD_QUEUE": "E2E_HOLD_REPLY",
                "E2E_GENERIC": "E2E_OK",
            }
            reply = replies[marker]
            yield TextDeltaEvent(text=reply)
            yield DoneEvent(
                stop_reason="end_turn",
                input_tokens=3,
                output_tokens=1,
                model=_MODEL,
            )
            self._record("provider.done", marker=marker, reply=reply)
        except asyncio.CancelledError:
            self._record("provider.cancelled", marker=marker)
            raise

    async def list_models(self) -> list[ModelInfo]:
        return []


class DeterministicSelector:
    active_provider_id = "e2e"

    def __init__(self, provider: DeterministicProvider, model: str = _MODEL) -> None:
        self._provider = provider
        self.model = model

    def clone(self) -> DeterministicSelector:
        return DeterministicSelector(self._provider, self.model)

    def override_model(self, model: str) -> None:
        self.model = model

    def override_model_with_fallback_chain(
        self,
        model: str,
        fallback_chain: list[object],  # noqa: ARG002
    ) -> None:
        self.override_model(model)

    def resolve(self) -> DeterministicProvider:
        return self._provider

    async def list_models(self) -> list[dict[str, Any]]:
        return []


async def main() -> None:
    port = int(os.environ["OPENSQUILLA_TUI_GATEWAY_E2E_PORT"])
    state_dir = Path(os.environ["OPENSQUILLA_TUI_GATEWAY_E2E_STATE"])
    event_log = Path(os.environ["OPENSQUILLA_TUI_GATEWAY_E2E_EVENT_LOG"])
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = state_dir / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    config = GatewayConfig(
        host="127.0.0.1",
        port=port,
        auth=AuthConfig(mode="none"),
    )
    config.state_dir = str(state_dir)
    config.workspace_dir = str(workspace_dir)
    config.attachments.media_root = str(state_dir / "media")
    config.control_ui.enabled = False
    config.squilla_router.enabled = False
    config.naming.enabled = False
    config.compaction.enabled = False
    config.memory.repair_enabled = False
    config.memory.ttl_sweep_interval_minutes = 0
    config.meta_skill.enabled = False
    config.heartbeat.enabled = False
    config.task_runtime.max_concurrency = 1
    config.task_runtime.max_pending_per_session = 8
    config.subagents.subagent_reserved_slots = 0
    config.llm.provider = "e2e"
    config.llm.model = _MODEL
    config.llm.api_key = ""

    provider = DeterministicProvider(event_log)
    selector = DeterministicSelector(provider)
    await start_gateway_server(
        config=config,
        provider_selector=selector,
        subscription_manager=SubscriptionManager(),
        run=True,
    )
    # ``start_gateway_server`` returns a handle after scheduling uvicorn.  Keep
    # this owning event loop alive until the parent release test terminates the
    # process; otherwise asyncio.run() would cancel the server during startup.
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
