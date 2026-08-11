"""Real Gateway fixture for the Goal-mode browser regression.

The browser test starts this process with the repository virtualenv.  The
provider is deterministic, but every other boundary is production code:
WebSocket RPC, SQLite state, TaskRuntime, GoalService, TurnRunner, tools, and
the automatic continuation lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.websocket import SubscriptionManager
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    Message,
    ModelInfo,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

MODEL = "qwen3:4b"
OBJECTIVE = "Produce and verify a deterministic release report"
FIRST_REPLY = "The release inputs are inspected; final verification still remains."
FINAL_REPLY = "The deterministic release report is complete and verified."
LIFECYCLE_FIRST_REPLY = "Task one completed after the lifecycle checks."
LIFECYCLE_SECOND_REPLY = "Task two completed after Goal removal."


def _message_text(message: Message) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else repr(content)


class DeterministicGoalProvider:
    """Drive one non-terminal Task followed by one automatic Goal Task."""

    provider_name = "ollama"

    def __init__(
        self,
        event_log: Path,
        first_release_file: Path,
        second_release_file: Path,
        *,
        scenario: str,
    ) -> None:
        self._event_log = event_log
        self._first_release_file = first_release_file
        self._second_release_file = second_release_file
        self._scenario = scenario
        self.calls = 0

    def _record(self, payload: dict[str, Any]) -> None:
        self._event_log.parent.mkdir(parents=True, exist_ok=True)
        with self._event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del config
        self.calls += 1
        call_number = self.calls
        message_text = "\n".join(_message_text(message) for message in messages)
        request_context = "\n".join(
            _message_text(message)
            for message in messages
            if message.role == "user"
            and isinstance(message.content, str)
            and message.content.startswith("[Request context for this turn]")
        )
        assistant_history = "\n".join(
            _message_text(message) for message in messages if message.role == "assistant"
        )
        expected_first_reply = (
            LIFECYCLE_FIRST_REPLY if self._scenario == "lifecycle" else FIRST_REPLY
        )
        tool_names = [str(getattr(tool, "name", "")) for tool in tools or []]
        self._record(
            {
                "event": "provider.call",
                "callNumber": call_number,
                "toolNames": tool_names,
                "objectiveInRequestContext": OBJECTIVE in request_context,
                "progressIsNull": (
                    '"progress": null' in request_context
                    or "&quot;progress&quot;: null" in request_context
                ),
                "firstReplyInAssistantHistory": expected_first_reply in assistant_history,
                "requestHasInternalContinuation": "[INTERNAL SYSTEM EVENT]" in message_text,
            }
        )
        if self._scenario == "lifecycle":
            return self._stream_lifecycle(call_number)
        return self._stream_continuation(call_number)

    async def _wait_for_release(self, call_number: int, release_file: Path) -> None:
        self._record({"event": "provider.waiting", "callNumber": call_number})
        while not release_file.exists():
            await asyncio.sleep(0.025)
        self._record({"event": "provider.released", "callNumber": call_number})

    async def _stream_continuation(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield TextDeltaEvent(text=FIRST_REPLY)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=13,
                output_tokens=5,
                model=MODEL,
            )
            return
        if call_number == 2:
            # The browser releases this only after proving that Task 1 settled,
            # Goal state stayed active, and the automatic Task 2 reached the
            # provider with the original objective and transcript history. The
            # first Task intentionally did not call update_goal_progress.
            await self._wait_for_release(call_number, self._second_release_file)
            yield ToolUseStartEvent(
                tool_use_id="goal-complete-2",
                tool_name="update_goal",
            )
            yield ToolUseEndEvent(
                tool_use_id="goal-complete-2",
                tool_name="update_goal",
                arguments={"status": "complete"},
            )
            yield DoneEvent(
                stop_reason="tool_use",
                input_tokens=17,
                output_tokens=7,
                model=MODEL,
            )
            return
        if call_number == 3:
            yield TextDeltaEvent(text=FINAL_REPLY)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=19,
                output_tokens=9,
                model=MODEL,
            )
            return
        raise AssertionError(f"Unexpected provider call {call_number}")

    async def _stream_lifecycle(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            await self._wait_for_release(call_number, self._first_release_file)
            yield TextDeltaEvent(text=LIFECYCLE_FIRST_REPLY)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=11,
                output_tokens=5,
                model=MODEL,
            )
            return
        if call_number == 2:
            await self._wait_for_release(call_number, self._second_release_file)
            yield TextDeltaEvent(text=LIFECYCLE_SECOND_REPLY)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=13,
                output_tokens=6,
                model=MODEL,
            )
            return
        raise AssertionError(f"Unexpected lifecycle provider call {call_number}")

    async def list_models(self) -> list[ModelInfo]:
        return []


class DeterministicGoalSelector:
    active_provider_id = "ollama"

    def __init__(self, provider: DeterministicGoalProvider, model: str = MODEL) -> None:
        self._provider = provider
        self.current_config = SimpleNamespace(
            provider="ollama",
            model=model,
            base_url="",
            fallback_chain=[],
        )

    def clone(self) -> DeterministicGoalSelector:
        return self

    def override_model(self, model: str) -> None:
        self.current_config.model = model

    def override_model_with_fallback_chain(
        self,
        model: str,
        fallback_chain: list[object],  # noqa: ARG002
    ) -> None:
        self.override_model(model)

    def resolve(self) -> DeterministicGoalProvider:
        return self._provider

    async def list_models(self) -> list[dict[str, Any]]:
        return []


async def main() -> None:
    port = int(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_PORT"])
    state_dir = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_STATE"])
    event_log = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_EVENT_LOG"])
    first_release_file = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE_FIRST"])
    second_release_file = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE"])
    scenario = os.environ.get("OPENSQUILLA_WEBUI_GOAL_E2E_SCENARIO", "continuation")
    if scenario not in {"continuation", "lifecycle"}:
        raise ValueError(f"Unsupported Goal E2E scenario: {scenario}")
    webui_origin = os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_ORIGIN"]
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
    config.cors.allowed_origins = [webui_origin]
    config.control_ui.enabled = False
    config.squilla_router.enabled = False
    for tier in config.squilla_router.tiers.values():
        tier["provider"] = "ollama"
        tier["model"] = MODEL
    config.naming.enabled = False
    config.compaction.enabled = False
    config.memory.flush_enabled = False
    config.memory.repair_enabled = False
    config.memory.ttl_sweep_interval_minutes = 0
    config.meta_skill.enabled = False
    config.heartbeat.enabled = False
    config.goal.execution_enabled = True
    config.task_runtime.max_concurrency = 1
    config.task_runtime.max_pending_per_session = 8
    config.subagents.subagent_reserved_slots = 0
    # Use a registered, keyless deployment identity so production readiness
    # admits the turn.  The injected selector below remains the only provider
    # implementation and never contacts a local Ollama server.
    config.llm.provider = "ollama"
    config.llm.model = MODEL
    config.llm.api_key = ""
    config.llm.base_url = "http://127.0.0.1:11434"
    config.agent_max_provider_retries = 0
    config.log_file_enabled = False

    provider = DeterministicGoalProvider(
        event_log,
        first_release_file,
        second_release_file,
        scenario=scenario,
    )
    await start_gateway_server(
        config=config,
        provider_selector=DeterministicGoalSelector(provider),
        subscription_manager=SubscriptionManager(),
        run=True,
    )
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
