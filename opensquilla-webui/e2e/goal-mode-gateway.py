"""Real Gateway fixture for the Goal-mode browser regression.

The browser test starts this process with the repository virtualenv.  The
Provider replies are deterministic and token counting uses the production
conservative offline fallback. Every lifecycle boundary is production code:
WebSocket RPC, SQLite state, TaskRuntime, GoalService, TurnRunner, tools, and
the automatic continuation lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from opensquilla import token_estimation
from opensquilla.gateway.boot import start_gateway_server
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.session_services import get_session_storage
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
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

MODEL = "qwen3:4b"
OBJECTIVE = "Produce and verify a deterministic release report"
FIRST_REPLY = "The release inputs are inspected; final verification still remains."
FINAL_REPLY = "The deterministic release report is complete and verified."
LIFECYCLE_FIRST_REPLY = "Task one completed after the lifecycle checks."
LIFECYCLE_SECOND_REPLY = "Task two completed after Goal removal."
SILENT_INITIAL_REPLY = "The initial Goal turn completed normally."
SILENT_MIXED_REPLY = "NO_REPLY\nThe deterministic silent-reply body is visible."
SILENT_VISIBLE_BODY = "The deterministic silent-reply body is visible."
SILENT_FORMATTED_REPLY = (
    "**HEARTBEAT_OK**\nThe formatted heartbeat body is visible."
)
SILENT_FORMATTED_BODY = "The formatted heartbeat body is visible."


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
            LIFECYCLE_FIRST_REPLY
            if self._scenario == "lifecycle"
            else SILENT_INITIAL_REPLY
            if self._scenario == "silent-reply"
            else FIRST_REPLY
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
                "requestHasInternalContinuation": (
                    "[GOAL CONTINUATION]" in message_text
                    or "[INTERNAL SYSTEM EVENT]" in message_text
                ),
                "historyHasSilentSentinel": (
                    "NO_REPLY" in assistant_history or "HEARTBEAT_OK" in assistant_history
                ),
                "silentVisibleBodyInAssistantHistory": (
                    SILENT_VISIBLE_BODY in assistant_history
                ),
            }
        )
        if self._scenario == "silent-reply":
            return self._stream_silent_reply(call_number)
        if self._scenario == "lifecycle":
            return self._stream_lifecycle(call_number)
        return self._stream_continuation(call_number)

    async def _stream_silent_reply(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            # The Goal-set turn is ordinary user ingress, so it must not rely
            # on internal sentinel interpretation.
            yield TextDeltaEvent(text=SILENT_INITIAL_REPLY)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=12,
                output_tokens=4,
                model=MODEL,
            )
            return
        if call_number == 2:
            # This first automatic continuation is an internal Goal context.
            # Hold Done after the raw provider delta so Playwright can prove no
            # marker escapes into either the WebSocket stream or live DOM.
            yield TextDeltaEvent(text=SILENT_MIXED_REPLY)
            await self._wait_for_release(call_number, self._first_release_file)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=11,
                output_tokens=5,
                model=MODEL,
            )
            return
        if call_number == 3:
            # A pure no-reply acknowledgement must settle without creating a
            # ghost assistant row, while Goal continuation remains active.
            yield TextDeltaEvent(text="NO_REPLY")
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=7,
                output_tokens=1,
                model=MODEL,
            )
            return
        if call_number == 4:
            # Markdown presentation around the protocol line is normalized on
            # an internal turn, while its substantive body remains visible.
            yield TextDeltaEvent(text=SILENT_FORMATTED_REPLY)
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=10,
                output_tokens=4,
                model=MODEL,
            )
            return
        if call_number == 5:
            # Pause before completion so the browser can reload the durable
            # history after mixed, pure-suppressed, and formatted turns.
            await self._wait_for_release(call_number, self._second_release_file)
            yield ToolUseStartEvent(
                tool_use_id="silent-goal-complete-5",
                tool_name="update_goal",
            )
            yield ToolUseEndEvent(
                tool_use_id="silent-goal-complete-5",
                tool_name="update_goal",
                arguments={"status": "complete"},
            )
            yield DoneEvent(
                stop_reason="tool_use",
                input_tokens=9,
                output_tokens=2,
                model=MODEL,
            )
            return
        if call_number == 6:
            # A final pure heartbeat acknowledgement exercises the second
            # suppression reason. The completed Goal uses its durable fallback
            # outcome anchor and must still expose accumulated usage.
            yield TextDeltaEvent(text="HEARTBEAT_OK")
            yield DoneEvent(
                stop_reason="stop",
                input_tokens=5,
                output_tokens=1,
                model=MODEL,
            )
            return
        raise AssertionError(f"Unexpected silent-reply provider call {call_number}")

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
            # first Task intentionally did not call update_plan.
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


async def _install_activation_orphan_fault(server: Any, provider: Any) -> asyncio.Task:
    """Opt-in test fault: no production endpoint, storage shim, or real profile.

    The exact first reservation fails before activation. A native SQLite trigger
    keeps its compensation failing even after Clear deletes the original Goal.
    Only the fixture release file removes that database fault; production recovery
    remains responsible for every task/ownership transition.
    """
    services = server._services
    assert services is not None
    runtime = services.task_runtime
    assert runtime is not None and services.goal_service is not None
    storage = get_session_storage(services.session_manager)
    assert storage is not None
    evidence_file = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_ACTIVATION_EVIDENCE"])
    release_file = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE_ACTIVATION_FAULT"])
    original_activate = runtime.activate
    failed_task_id: str | None = None
    failed_session_key: str | None = None
    fault_released = False
    activated_task_ids: list[str] = []

    def execute_fault_ddl(sql: str) -> None:
        # An independent native SQLite connection must not commit or roll back
        # a production transaction that happens to share the writer coroutine.
        with sqlite3.connect(storage._db_path, timeout=5) as connection:
            connection.execute(sql)

    def publish(payload: dict[str, Any]) -> None:
        temporary = evidence_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(evidence_file)

    async def activate(reservation: Any, **kwargs: Any) -> Any:
        nonlocal failed_task_id, failed_session_key
        if failed_task_id is None:
            assert not reservation.activated
            failed_task_id = reservation.task_id
            failed_session_key = reservation.session_key
            task_literal = failed_task_id.replace("'", "''")
            await asyncio.to_thread(execute_fault_ddl,
                f"""CREATE TRIGGER e2e_deny_activation_compensation
                BEFORE UPDATE OF status ON agent_tasks
                WHEN OLD.task_id = '{task_literal}' AND OLD.status = 'queued'
                  AND NEW.status != 'queued'
                BEGIN SELECT RAISE(FAIL, 'controlled activation compensation outage'); END"""
            )
            raise RuntimeError("controlled preactivation failure before executor ownership")
        result = await original_activate(reservation, **kwargs)
        activated_task_ids.append(reservation.task_id)
        return result

    runtime.activate = activate
    publish({"fixtureReady": True, "faultInjected": False})

    async def observe() -> None:
        nonlocal fault_released
        previous: dict[str, Any] = {}
        while True:
            if failed_task_id is not None:
                if not fault_released and release_file.exists():
                    await asyncio.to_thread(execute_fault_ddl,
                        "DROP TRIGGER e2e_deny_activation_compensation")
                    fault_released = True
                task = await storage.get_agent_task(failed_task_id)
                goal = await storage.get_goal(failed_session_key)
                assert task is not None
                owner = task.details.get("admission_owner") or {}
                payload = {
                    "fixtureReady": True,
                    "faultInjected": True,
                    "faultReleased": fault_released,
                    "failedTask": {
                        "taskId": task.task_id, "sessionKey": task.session_key,
                        "status": str(task.status), "startedAt": task.started_at,
                        "admissionOwner": {key: owner.get(key) for key in (
                            "resident_lease", "compute_lease", "settlement",
                        )},
                    },
                    "currentGoal": None if goal is None else {
                        "goalId": goal.goal_id, "status": goal.status,
                        "activeTaskId": goal.active_task_id,
                        "turnsStarted": goal.turns_started, "turnsSettled": goal.turns_settled,
                    },
                    "recoveryRecords": len(services.goal_service._settlement_recoveries),
                    "runtimeReservations": len(runtime._reservations_by_session),
                    "runtimeTasks": len(runtime._tasks),
                    "activatedTaskIds": list(activated_task_ids),
                    "providerCalls": provider.calls,
                }
                if payload != previous:
                    publish(payload)
                    previous = payload
            await asyncio.sleep(0.05)

    return asyncio.create_task(observe(), name="e2e-activation-recovery-observer")


async def main() -> None:
    # Every fixture gets a fresh temporary directory, including tiktoken's
    # default download cache. Use the production conservative fallback so a
    # chat lifecycle proof never waits for an external tokenizer download.
    token_estimation._encoding = token_estimation._ENCODING_UNAVAILABLE
    port = int(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_PORT"])
    state_dir = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_STATE"])
    event_log = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_EVENT_LOG"])
    first_release_file = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE_FIRST"])
    second_release_file = Path(os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_RELEASE"])
    scenario = os.environ.get("OPENSQUILLA_WEBUI_GOAL_E2E_SCENARIO", "continuation")
    if scenario not in {"continuation", "lifecycle", "silent-reply", "activation-orphan-clear"}:
        raise ValueError(f"Unsupported Goal E2E scenario: {scenario}")
    auth_mode = os.environ.get("OPENSQUILLA_WEBUI_GOAL_E2E_AUTH_MODE", "none")
    if auth_mode not in {"none", "token"}:
        raise ValueError(f"Unsupported Gateway fixture auth mode: {auth_mode}")
    webui_origin = os.environ["OPENSQUILLA_WEBUI_GOAL_E2E_ORIGIN"]
    state_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = state_dir / "workspace"
    missing_configured_workspace = (
        os.environ.get("OPENSQUILLA_WEBUI_GOAL_E2E_WORKSPACE_INITIALLY_MISSING") == "true"
    )
    if not missing_configured_workspace:
        workspace_dir.mkdir(parents=True, exist_ok=True)

    # Read-only browser fixtures need durable identities for the real V2 lease
    # protocol. Seed these before Gateway startup without invoking a Provider.
    initial_keys = json.loads(
        os.environ.get("OPENSQUILLA_WEBUI_GOAL_E2E_INITIAL_SESSION_KEYS", "[]"),
    )
    if initial_keys:
        storage = await SessionStorage.open(state_dir / "sessions.db")
        try:
            for key in initial_keys:
                await storage.upsert_session(SessionNode(session_key=key, agent_id="main"))
        finally:
            await storage.close()

    config = GatewayConfig(
        host="127.0.0.1",
        port=port,
        auth=AuthConfig(mode=auth_mode, token="synthetic-real-gateway-owner-token"),
        # Constructor provenance matches a loaded explicit workspace_dir. A
        # later assignment alone retains the managed-workspace default, which
        # would bypass the configured-directory failure under test.
        **({"workspace_dir": str(workspace_dir)} if missing_configured_workspace else {}),
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
    config.memory.ttl_sweep_interval_minutes = 0
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
        scenario="lifecycle" if scenario == "activation-orphan-clear" else scenario,
    )
    server = await start_gateway_server(
        config=config,
        provider_selector=DeterministicGoalSelector(provider),
        subscription_manager=SubscriptionManager(),
        run=True,
    )
    fault_observer = None
    if scenario == "activation-orphan-clear":
        fault_observer = await _install_activation_orphan_fault(server, provider)
    # Propagate observer errors so the fixture fails visibly instead of serving
    # indefinitely while the browser polls a stale observation file.
    if fault_observer is not None:
        await fault_observer
    else:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
