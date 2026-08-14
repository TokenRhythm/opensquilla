from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import (
    Agent,
    AgentConfig,
    AgentState,
    DoneEvent,
    ErrorEvent,
    RunHeartbeatEvent,
    SubagentSpec,
    ToolCall,
    ToolResult,
    WarningEvent,
)
from opensquilla.engine.agent import _progress_watchdog_guidance_message
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.session_sanitize import session_payload_chars
from opensquilla.engine.types import CompactionEvent
from opensquilla.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    ProviderFinalRequestProjection,
    ProviderHeartbeatEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseDeltaEvent as ProviderToolUseDelta
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.request_proof import (
    ProviderRequestBudgetExceeded,
    prove_provider_payload,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.compaction import CompactionResult
from opensquilla.session.compaction_deployment import (
    CompactionExecutionPlan,
    CompactionExecutionTarget,
)
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.mutation_receipts import (
    fingerprint_path,
    record_semantic_mutation_receipt,
)
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

RAW_CURRENT_TURN_OVERFLOW_MESSAGE = (
    "Context overflow is in the current turn's recent tool calls or "
    "reasoning tail; history compaction cannot reduce it."
)


def test_agent_compaction_uses_frozen_physical_plan() -> None:
    target_provider = _StallingProvider()
    plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=target_provider,
                provider_id="physical-provider",
                model="summary-model",
                context_window_tokens=32_000,
                provider_request_max_chars=80_000,
            ),
        )
    )
    agent = Agent(
        provider=_StallingProvider(),
        config=AgentConfig(
            model_id="turn-model",
            compaction_execution_plan=plan,
        ),
    )

    config = agent._build_compaction_config()

    assert config.llm_plan is plan
    assert config.provider == "physical-provider"
    assert config.model == "summary-model"


@pytest.mark.parametrize("refresh_mode", ["unavailable", "error"])
def test_agent_compaction_plan_refresh_never_reuses_stale_provider(
    refresh_mode: str,
) -> None:
    stale_plan = CompactionExecutionPlan(
        candidates=(
            CompactionExecutionTarget(
                provider=_StallingProvider(),
                provider_id="stale-provider",
                model="stale-model",
                context_window_tokens=32_000,
                provider_request_max_chars=80_000,
            ),
        )
    )

    def refresh() -> None:
        if refresh_mode == "error":
            raise RuntimeError("credential refresh failed")
        return None

    agent = Agent(
        provider=_StallingProvider(),
        config=AgentConfig(
            model_id="turn-model",
            compaction_execution_plan=stale_plan,
            compaction_execution_plan_factory=refresh,
        ),
    )

    config = agent._build_compaction_config()

    assert config.llm_plan is None
    assert config.api_key == ""
    assert config.provider == ""
    assert config.model == "turn-model"


class _StallingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.stream_closed = False

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        try:
            await asyncio.sleep(60.0)
            yield ProviderText(text="late")
        finally:
            self.stream_closed = True

    async def list_models(self) -> list[Any]:
        return []


class _ActiveLongToolArgumentProvider:
    provider_name = "fake"

    def __init__(
        self,
        *,
        fragment_delay: float = 0.02,
        content: str = "alpha\\nbeta\\ngamma\\n",
    ) -> None:
        self.fragment_delay = fragment_delay
        self.content = content
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > 1:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = "tool-1"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="write_file")
        fragments = [
            '{"path":"deck.py","content":"',
            self.content,
            '"}',
        ]
        for fragment in fragments:
            await asyncio.sleep(self.fragment_delay)
            yield ProviderToolUseDelta(tool_use_id=tool_use_id, json_fragment=fragment)
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="write_file",
            arguments={"path": "deck.py", "content": self.content},
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=100)

    async def list_models(self) -> list[Any]:
        return []


class _ContextOverflowProvider:
    provider_name = "fake"

    def __init__(self, *, success_after: int | None = None) -> None:
        self.success_after = success_after
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    def project_final_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
        *,
        message_limit: int | None = None,
    ) -> ProviderFinalRequestProjection:
        del tools, config
        fits_message_count = (
            None if message_limit is None else len(messages) <= message_limit
        )
        return ProviderFinalRequestProjection(
            payload={"messages": [message.model_dump() for message in messages]},
            proof={"fits": fits_message_count is not False},
            wire_message_count=len(messages),
            message_limit=message_limit,
            fits_message_count=fits_message_count,
            fits=fits_message_count is not False,
        )

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if self.success_after is not None and call_number > self.success_after:
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        yield ProviderError(message="context length exceeded", code="400")

    async def list_models(self) -> list[Any]:
        return []


class _FinalAdmissionContextOverflowProvider(_ContextOverflowProvider):
    final_request_admission_guaranteed = True


class _HangingRetryAfterOverflowProvider(_ContextOverflowProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderError(message="context length exceeded", code="400")
            return
        await asyncio.Event().wait()
        yield ProviderText(text="unreachable")


class _TextThenDelayedSuccessAfterOverflowProvider(_ContextOverflowProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderError(message="context length exceeded", code="400")
            return
        yield ProviderText(text="partial ")
        await asyncio.sleep(2.1)
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _ProviderRequestBudgetExceededProvider:
    provider_name = "openrouter"

    def __init__(
        self,
        *,
        success_after: int | None = None,
        proof: dict[str, Any] | None = None,
    ) -> None:
        self.success_after = success_after
        self.proof = proof
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if self.success_after is not None and call_number > self.success_after:
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        message = (
            '{"fallback_reason":"provider_request_budget_exhausted"}'
            if self.proof is None
            else json.dumps(self.proof)
        )
        yield ProviderError(message=message, code="provider_request_budget_exhausted")

    async def list_models(self) -> list[Any]:
        return []


class _FinalProofBudgetProvider:
    provider_name = "openrouter"
    final_request_admission_guaranteed = True

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.projected_configs: list[ChatConfig] = []

    def project_final_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
        *,
        message_limit: int | None = None,
    ) -> ProviderFinalRequestProjection:
        cfg = config or ChatConfig()
        self.projected_configs.append(cfg)
        payload = {
            "messages": [
                message.model_dump(mode="json", exclude_none=True)
                for message in messages
            ],
            "system": cfg.system,
            "tools": [
                tool.model_dump(mode="json", exclude_none=True)
                if hasattr(tool, "model_dump")
                else tool
                for tool in (tools or [])
            ],
            "max_tokens": cfg.max_tokens,
        }
        try:
            proof = prove_provider_payload(
                payload,
                projection_adapter="openrouter",
                proof_budget=cfg.provider_request_max_chars,
            )
        except ProviderRequestBudgetExceeded as exc:
            proof = exc.proof
        fits_message_count = (
            None if message_limit is None else len(messages) <= message_limit
        )
        fits = bool(proof["fits"]) and fits_message_count is not False
        return ProviderFinalRequestProjection(
            payload=payload,
            proof=proof,
            wire_message_count=len(messages),
            message_limit=message_limit,
            fits_message_count=fits_message_count,
            fits=fits,
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        projection = self.project_final_request(messages, tools, config)
        return self._stream(projection)

    async def _stream(
        self,
        projection: ProviderFinalRequestProjection,
    ) -> AsyncIterator[Any]:
        if not projection.fits:
            yield ProviderError(
                message=json.dumps(projection.proof),
                code="provider_request_budget_exhausted",
            )
            return
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def test_preflight_history_capacity_reserves_non_history_envelope() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            model_id="test-model",
            context_window_tokens=4_000,
            max_tokens=512,
            system_prompt="system policy " * 100,
            request_context_prompt="request capsule " * 50,
            flush_enabled=False,
        ),
    )
    active_prompt = "active user " * 300

    persisted_capacity, persisted_char_capacity = agent.preflight_history_capacity(
        active_user_message=active_prompt,
        active_user_in_history=True,
        context_window_tokens=4_000,
    )
    unpersisted_capacity, unpersisted_char_capacity = agent.preflight_history_capacity(
        active_user_message=active_prompt,
        active_user_in_history=False,
        context_window_tokens=4_000,
    )
    attachment_capacity, attachment_char_capacity = agent.preflight_history_capacity(
        active_user_message=active_prompt,
        active_user_in_history=True,
        attachments=[{"type": "text", "content": "attachment " * 4_000}],
        context_window_tokens=4_000,
    )

    assert 0 < persisted_capacity < 4_000
    assert unpersisted_capacity < persisted_capacity
    assert attachment_capacity < persisted_capacity
    assert 0 < persisted_char_capacity
    assert unpersisted_char_capacity < persisted_char_capacity
    assert attachment_char_capacity < persisted_char_capacity
    assert agent.preflight_history_capacity_tokens(
        active_user_message=active_prompt,
        active_user_in_history=True,
        context_window_tokens=4_000,
    ) == persisted_capacity


def test_durable_consumer_projection_uses_base_model_config() -> None:
    base_provider = OpenAIProvider(
        api_key="test",
        model="base-model",
    )
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            model_id="routed-dashscope-model",
            context_window_tokens=8_000,
            max_tokens=1_024,
            thinking=True,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                reasoning_format="dashscope",
            ),
            flush_enabled=False,
        ),
    )

    projection = agent._project_compaction_consumer_request(
        consumer_provider=base_provider,
        replay_summary="stable checkpoint",
        kept_entries=[],
        active_user_message="continue",
        active_user_in_history=False,
        bound_user_message_id=None,
        attachment_messages=None,
        runtime_context_message=Message(role="user", content="runtime"),
        context_window_tokens=128_000,
        max_output_tokens=256,
        consumer_model_id="base-model",
        consumer_model_capabilities=ModelCapabilities(
            supports_reasoning=False,
        ),
        consumer_provider_request_max_chars=12_000,
    )

    assert projection is not None
    assert projection.payload["model"] == "base-model"
    assert projection.payload["max_tokens"] == 256
    assert "enable_thinking" not in projection.payload
    assert "reasoning_effort" not in projection.payload
    assert projection.proof["raw_proof_budget"] == 12_000


class _RepeatedToolFailureThenDoneProvider:
    provider_name = "fake"

    def __init__(self, *, tool_retries: int = 3) -> None:
        self.tool_retries = tool_retries
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.tool_retries:
            yield ProviderText(text="handled")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = f"cmd-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="exec_command",
            arguments={"command": "python build_pptx.py", "timeout": 30},
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _RepeatedSuccessfulToolThenDoneProvider:
    provider_name = "fake"

    def __init__(
        self,
        *,
        tool_retries: int = 4,
        tool_name: str = "grep_search",
        arguments: dict[str, Any] | None = None,
    ) -> None:
        self.tool_retries = tool_retries
        self.tool_name = tool_name
        self.calls: list[list[Message]] = []
        self.arguments = arguments or {
            "path": "/testbed/crates/regex/src/matcher.rs",
            "pattern": 'impl.*Matcher"',
        }

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.tool_retries:
            yield ProviderText(text="handled")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = f"{self.tool_name}-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=self.tool_name)
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name=self.tool_name,
            arguments=dict(self.arguments),
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _FinalThenDoneProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text="Implemented the fix.")
        else:
            yield ProviderText(text="No code change is required.")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _FailedToolThenFinalProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_use_id = "cmd-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "cargo build 2>&1 | tail -30"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _PostWriteFailedVerificationThenSourceProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_use_id = "edit-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": "src.py", "old_text": "old", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 2:
            tool_use_id = "cmd-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "cargo build --release --bin ruff 2>&1 | tail -30"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if 3 <= call_number <= 5:
            tool_use_id = f"read-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _StableVerifiedDiffThenSourceProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.tool_lists: list[list[Any] | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        self.tool_lists.append(tools)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_use_id = "edit-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": "src.py", "old_text": "old", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 2:
            tool_use_id = "cmd-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "pytest tests/test_src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if 3 <= call_number <= 8:
            tool_use_id = f"read-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final after convergence {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _RepeatedFailedVerificationFinalProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_use_id = "edit-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": "src.py", "old_text": "old", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number in {2, 4}:
            tool_use_id = f"cmd-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "make check"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _PostWriteCleanMavenVerificationThenFinalProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_use_id = "edit-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": "src.py", "old_text": "old", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 2:
            tool_use_id = "cmd-2"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "mvn test -Dtest=ParserTest 2>&1 | tail -20"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _EditThenFinalProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            tool_use_id = "edit-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": "src.py", "old_text": "old", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=f"final attempt {call_number}")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _NoWorkspaceWriteThenPatchProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.tools_by_call: list[list[Any] | None] = []
        self.configs: list[ChatConfig | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        self.tools_by_call.append(tools)
        self.configs.append(config)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number <= 17:
            tool_use_id = f"read-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 18:
            tool_use_id = "patch-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="apply_patch")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="apply_patch",
                arguments={
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: src.py\n"
                        "@@ -1,1 +1,1 @@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    )
                },
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _ScratchReproThenPatchProvider(_NoWorkspaceWriteThenPatchProvider):
    def __init__(self, scratch_dir: Path) -> None:
        super().__init__()
        self.scratch_dir = scratch_dir

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number <= 17:
            async for event in super()._stream(call_number):
                yield event
            return
        if call_number in {18, 19, 21}:
            name_by_call = {
                18: "repro_issue.tcl",
                19: "notes.md",
                21: "notes_after_source.md",
            }
            name = name_by_call[call_number]
            tool_use_id = {
                18: "write-repro",
                19: "write-notes",
                21: "write-notes-after-source",
            }[call_number]
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="write_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="write_file",
                arguments={
                    "path": str(self.scratch_dir / name),
                    "content": (
                        "puts repro\n" if call_number == 18 else "investigation notes\n"
                    ),
                },
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 20:
            async for event in super()._stream(18):
                yield event
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _WritePatchFileThenApplyProvider:
    provider_name = "fake"

    def __init__(self, patch_path: Path, patch_text: str) -> None:
        self.patch_path = patch_path
        self.patch_text = patch_text
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="write-patch", tool_name="write_file")
            yield ProviderToolUseEnd(
                tool_use_id="write-patch",
                tool_name="write_file",
                arguments={"path": str(self.patch_path), "content": self.patch_text},
            )
            yield ProviderToolUseStart(tool_use_id="apply-path", tool_name="apply_patch")
            yield ProviderToolUseEnd(
                tool_use_id="apply-path",
                tool_name="apply_patch",
                arguments={"path": str(self.patch_path)},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _PathPatchThenFinalProvider:
    provider_name = "fake"

    def __init__(self, patch_path: Path) -> None:
        self.patch_path = patch_path
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="apply-path", tool_name="apply_patch")
            yield ProviderToolUseEnd(
                tool_use_id="apply-path",
                tool_name="apply_patch",
                arguments={"path": str(self.patch_path)},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _PatchFailureRecoveryProvider(_NoWorkspaceWriteThenPatchProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number <= 17:
            tool_use_id = f"read-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number in {18, 20}:
            tool_use_id = f"patch-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="apply_patch")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="apply_patch",
                arguments={
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: src.py\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    )
                },
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 19:
            tool_use_id = "read-recovery"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _EditFailureRecoveryProvider(_NoWorkspaceWriteThenPatchProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number <= 17:
            tool_use_id = f"read-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 18:
            tool_use_id = "edit-missing-context"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="edit_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="edit_file",
                arguments={"path": "src.py", "old_text": "missing", "new_text": "new"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 19:
            tool_use_id = "read-recovery"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="read_file")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="read_file",
                arguments={"path": "src.py"},
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        if call_number == 20:
            tool_use_id = "patch-after-read"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="apply_patch")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="apply_patch",
                arguments={
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Update File: src.py\n"
                        "@@\n"
                        "-old\n"
                        "+new\n"
                        "*** End Patch\n"
                    )
                },
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _HighUsageToolLoopProvider:
    provider_name = "fake"

    def __init__(self, *, tool_rounds: int = 3, input_tokens_per_call: int = 4000) -> None:
        self.tool_rounds = tool_rounds
        self.input_tokens_per_call = input_tokens_per_call
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.tool_rounds:
            yield ProviderText(text="done")
            yield ProviderDone(
                stop_reason="stop",
                input_tokens=self.input_tokens_per_call,
                output_tokens=0,
            )
            return
        tool_use_id = f"read-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="exec_command",
            arguments={"command": f"printf round-{call_number}"},
        )
        yield ProviderDone(
            stop_reason="tool_calls",
            input_tokens=self.input_tokens_per_call,
            output_tokens=0,
        )

    async def list_models(self) -> list[Any]:
        return []


class _ConfigCapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.configs: list[ChatConfig | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.configs.append(config)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _NoBilledCostUsageProvider:
    provider_name = "fake"

    def __init__(self, *, input_tokens: int = 1, output_tokens: int = 1) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            billed_cost=0.0,
        )

    async def list_models(self) -> list[Any]:
        return []


class _MixedBilledAndEstimatedCostProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            # First call reports a real billed cost, small enough to stay
            # under budget on its own.
            tool_use_id = "tool-1"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="exec_command")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="exec_command",
                arguments={"command": "echo hi"},
            )
            yield ProviderDone(
                stop_reason="tool_calls",
                input_tokens=1,
                output_tokens=1,
                billed_cost=0.0005,
            )
            return
        # Second call is cost-blind (billed_cost=0.0), forcing the estimator
        # to supply the remaining component that tips the turn over budget.
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=1000,
            output_tokens=1000,
            billed_cost=0.0,
        )

    async def list_models(self) -> list[Any]:
        return []


class _EnsembleUsageBreakdownProvider:
    provider_name = "ensemble"

    def __init__(self, *, billed_cost: float, rows: list[dict[str, Any]]) -> None:
        self.billed_cost = billed_cost
        self.rows = rows
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=sum(int(row.get("input_tokens") or 0) for row in self.rows),
            output_tokens=sum(int(row.get("output_tokens") or 0) for row in self.rows),
            billed_cost=self.billed_cost,
            model="aggregator",
            model_usage_breakdown=self.rows,
        )

    async def list_models(self) -> list[Any]:
        return []


class _RetryingEnsembleErrorProvider:
    provider_name = "ensemble"

    def __init__(self, *, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderError(
            message="aggregator failed after proposer usage",
            code="500",
            model_usage_breakdown=self.rows,
            usage_missing_count=1,
        )

    async def list_models(self) -> list[Any]:
        return []


class _CompactingErrorSessionManager:
    def __init__(self, *, compact_raises: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.compact_raises = compact_raises

    async def compact(self, session_key: str, budget: int, config: Any | None = None) -> str:
        self.calls.append(("compact", session_key))
        assert budget > 0
        if self.compact_raises:
            raise RuntimeError("compact failed")
        return "[summary]"

    async def append_message(self, session_key: str, **kwargs: Any) -> None:
        self.calls.append(("append", session_key))
        assert kwargs["role"] == "system"
        assert kwargs["content"].startswith("Error: ")


@pytest.mark.asyncio
async def test_turn_error_persist_records_current_turn_exhaustion_without_compacting() -> None:
    session_manager = _CompactingErrorSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=session_manager,
        config=SimpleNamespace(context_budget_tokens=96_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Context overflow is in the current turn's recent tool calls.",
            code="current_turn_context_exhausted",
        ),
    )

    assert session_manager.calls == [("append", "agent:main:webchat:test")]


@pytest.mark.asyncio
async def test_turn_error_persist_skips_error_time_compaction_for_exhaustion() -> None:
    session_manager = _CompactingErrorSessionManager(compact_raises=True)
    runner = TurnRunner(
        provider_selector=None,
        session_manager=session_manager,
        config=SimpleNamespace(context_budget_tokens=96_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Context overflow is in the current turn's recent tool calls.",
            code="current_turn_context_exhausted",
        ),
    )

    assert session_manager.calls == [("append", "agent:main:webchat:test")]


@pytest.mark.asyncio
async def test_agent_blocks_repeated_identical_tool_failures_before_tail_growth() -> None:
    calls = 0

    async def _failing_tool(call: Any) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="write failed: " + ("permission denied " * 200),
            is_error=True,
        )

    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(
            tool_failure_loop_block_threshold=3,
        ),
        tool_handler=_failing_tool,
    )
    tool_call = ToolCall(
        tool_use_id="write-1",
        tool_name="write_file",
        arguments={"path": "index.html", "content": "<html>bad</html>"},
    )

    first = await agent._execute_tool(tool_call)
    second = await agent._execute_tool(tool_call)
    third = await agent._execute_tool(tool_call)

    assert first.is_error is True
    assert second.is_error is True
    assert third.is_error is True
    assert calls == 2
    assert "tool_failure_loop_exhausted" not in third.content
    assert "Do not retry this exact call unchanged" in third.content
    assert len(third.content) < len(second.content)
    assert third.execution_status is not None
    assert third.execution_status.get("reason") == "tool_failure_loop_exhausted"


@pytest.mark.asyncio
async def test_agent_tool_failure_loop_allows_changed_arguments() -> None:
    calls = 0

    async def _failing_tool(call: Any) -> ToolResult:
        nonlocal calls
        calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="write failed",
            is_error=True,
        )

    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(tool_failure_loop_block_threshold=3),
        tool_handler=_failing_tool,
    )

    await agent._execute_tool(
        ToolCall(
            tool_use_id="write-1",
            tool_name="write_file",
            arguments={"path": "index.html", "content": "first"},
        )
    )
    await agent._execute_tool(
        ToolCall(
            tool_use_id="write-2",
            tool_name="write_file",
            arguments={"path": "index.html", "content": "first"},
        )
    )
    changed = await agent._execute_tool(
        ToolCall(
            tool_use_id="write-3",
            tool_name="write_file",
            arguments={"path": "index.html", "content": "changed"},
        )
    )

    assert calls == 3
    assert changed.content == "write failed"


@pytest.mark.asyncio
async def test_agent_tool_failure_loop_result_returns_to_model_instead_of_terminal_error() -> None:
    provider = _RepeatedToolFailureThenDoneProvider(tool_retries=3)
    handler_calls = 0

    async def _failing_tool(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="syntax error",
            is_error=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            tool_failure_loop_block_threshold=3,
            max_iterations=5,
            flush_enabled=False,
        ),
        tool_handler=_failing_tool,
    )

    events = [event async for event in agent.run_turn("build the deck")]

    assert handler_calls == 2
    assert len(provider.calls) == 4
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and getattr(event, "code", None) == "tool_failure_loop_exhausted"
        for event in events
    )
    assert any(
        getattr(event, "kind", None) == "tool_result"
        and (getattr(event, "execution_status", None) or {}).get("reason")
        == "tool_failure_loop_exhausted"
        for event in events
    )
    assert not any(
        isinstance(event, WarningEvent) and event.code == "repeated_tool_call_recovery"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_recovers_repeated_successful_identical_tool_calls(
    tmp_path,
) -> None:
    provider = _RepeatedSuccessfulToolThenDoneProvider(tool_retries=4)
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    handler_calls = 0

    async def _tool(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="No matches",
            is_error=False,
        )

    def _matching_tool_use_count(messages: list[Message]) -> int:
        count = 0
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "grep_search"
                    and getattr(block, "input", None) == provider.arguments
                ):
                    count += 1
        return count

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            repeated_tool_call_recovery_threshold=3,
            max_iterations=8,
            flush_enabled=False,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("find the matcher impl")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert handler_calls == 2
    assert len(provider.calls) == 5
    assert _matching_tool_use_count(provider.calls[-1]) == 2
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "repeated_tool_call_recovery"
        for event in events
    )
    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    recovery_events = [
        event
        for event in logged
        if event.get("mechanism") == "repeated_tool_call_recovery"
    ]
    assert len(recovery_events) == 2
    assert recovery_events[0]["evidence"]["repeat_count"] == 3
    assert recovery_events[1]["evidence"]["repeat_count"] == 4


@pytest.mark.asyncio
async def test_agent_recovers_repeated_successful_identical_exec_commands() -> None:
    provider = _RepeatedSuccessfulToolThenDoneProvider(
        tool_retries=4,
        tool_name="exec_command",
        arguments={
            "command": (
                "cd /testbed && printf 'some.domain.com/x\\n' | "
                "./target/release/rg --no-config -w domain 2>&1 || true"
            )
        },
    )
    handler_calls = 0

    async def _tool(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="",
            is_error=False,
        )

    def _matching_tool_use_count(messages: list[Message]) -> int:
        count = 0
        for message in messages:
            if not isinstance(message.content, list):
                continue
            for block in message.content:
                if (
                    getattr(block, "type", None) == "tool_use"
                    and getattr(block, "name", None) == "exec_command"
                    and getattr(block, "input", None) == provider.arguments
                ):
                    count += 1
        return count

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            repeated_tool_call_recovery_threshold=3,
            max_iterations=8,
            flush_enabled=False,
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("verify the regex behavior")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert handler_calls == 2
    assert len(provider.calls) == 5
    assert _matching_tool_use_count(provider.calls[-1]) == 2
    assert any(
        isinstance(event, WarningEvent) and event.code == "repeated_tool_call_recovery"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_repeated_git_diff_not_covered_by_default() -> None:
    provider = _RepeatedSuccessfulToolThenDoneProvider(
        tool_retries=4,
        tool_name="git_diff",
        arguments={"path": "/testbed"},
    )
    handler_calls = 0

    async def _tool(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="diff --git a/f b/f",
            is_error=False,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            repeated_tool_call_recovery_threshold=3,
            max_iterations=8,
            flush_enabled=False,
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("show the current diff")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert handler_calls == 4
    assert not any(
        isinstance(event, WarningEvent) and event.code == "repeated_tool_call_recovery"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_repeated_extra_tool_recovery_covers_git_diff() -> None:
    provider = _RepeatedSuccessfulToolThenDoneProvider(
        tool_retries=4,
        tool_name="git_diff",
        arguments={"path": "/testbed"},
    )
    handler_calls = 0

    async def _tool(call: Any) -> ToolResult:
        nonlocal handler_calls
        handler_calls += 1
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="diff --git a/f b/f",
            is_error=False,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            repeated_tool_call_recovery_threshold=3,
            repeated_tool_call_recovery_extra_tools=("git_diff",),
            max_iterations=8,
            flush_enabled=False,
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("show the current diff")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert handler_calls == 2
    assert any(
        isinstance(event, WarningEvent) and event.code == "repeated_tool_call_recovery"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_tool_failure_loop_resets_after_successful_state_change() -> None:
    calls: list[str] = []

    async def _tool(call: Any) -> ToolResult:
        calls.append(call.tool_name)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok" if call.tool_name == "edit_file" else "syntax error",
            is_error=call.tool_name != "edit_file",
        )

    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(tool_failure_loop_block_threshold=3),
        tool_handler=_tool,
    )
    command_call = ToolCall(
        tool_use_id="cmd-1",
        tool_name="exec_command",
        arguments={"command": "python build_pptx.py", "timeout": 30},
    )

    await agent._execute_tool(command_call)
    await agent._execute_tool(
        ToolCall(
            tool_use_id="cmd-2",
            tool_name="exec_command",
            arguments=command_call.arguments,
        )
    )
    await agent._execute_tool(
        ToolCall(
            tool_use_id="edit-1",
            tool_name="edit_file",
            arguments={"path": "build_pptx.py", "old_text": "bad", "new_text": "good"},
        )
    )
    retry_after_edit = await agent._execute_tool(
        ToolCall(
            tool_use_id="cmd-3",
            tool_name="exec_command",
            arguments=command_call.arguments,
        )
    )

    assert calls == ["exec_command", "exec_command", "edit_file", "exec_command"]
    assert retry_after_edit.content == "syntax error"
    assert retry_after_edit.execution_status is None


@pytest.mark.asyncio
async def test_agent_progress_watchdog_log_mode_suppresses_model_warning() -> None:
    provider = _RepeatedToolFailureThenDoneProvider(tool_retries=2)

    async def _failing_tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="syntax error",
            is_error=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=5,
            flush_enabled=False,
            progress_watchdog_mode="log",
            progress_watchdog_repeated_tool_error_threshold=2,
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_failing_tool,
    )

    events = [event async for event in agent.run_turn("build the deck")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 3
    assert not any(
        isinstance(message.content, str) and "[Runtime progress warning]" in message.content
        for message in provider.calls[2]
    )


@pytest.mark.asyncio
async def test_agent_progress_watchdog_can_warn_model_after_repeated_tool_errors() -> None:
    provider = _RepeatedToolFailureThenDoneProvider(tool_retries=2)

    async def _failing_tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="syntax error",
            is_error=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=5,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            progress_watchdog_repeated_tool_error_threshold=2,
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_failing_tool,
    )

    events = [event async for event in agent.run_turn("build the deck")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 3
    assert any(
        isinstance(message.content, str)
        and "[Runtime progress warning]" in message.content
        and "Do not repeat the same action unchanged" in message.content
        for message in provider.calls[2]
    )


@pytest.mark.asyncio
async def test_agent_warn_model_recovers_once_before_empty_workspace_diff_final(
    tmp_path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    provider = _FinalThenDoneProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
        ),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 2
    assert any(
        isinstance(message.content, str)
        and "[Runtime progress warning]" in message.content
        and "no visible workspace diff" in message.content
        for message in provider.calls[1]
    )
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "No code change is required."


def _init_git_repo_with_source(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    source = tmp_path / "src" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/parser.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    source.write_text("new\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_warns_once_for_suspicious_final_diff_contract(tmp_path) -> None:
    _init_git_repo_with_source(tmp_path)
    (tmp_path / "debug_case.py").write_text("print('repro')\n", encoding="utf-8")
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _FinalThenDoneProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            flush_enabled=False,
            progress_watchdog_mode="log",
            final_diff_contract_mode="warn_model",
            runtime_events_path=str(runtime_events_path),
        ),
        tool_context=ToolContext(
            workspace_dir=str(tmp_path),
            workspace_file_writes=[
                {"relative_path": "src/parser.py", "path": str(tmp_path / "src/parser.py")}
            ],
            workspace_mutation_receipts=[
                {"relative_path": "src/parser.py", "changed": True, "partial": False},
                {"relative_path": "src/parser.py", "changed": False, "partial": False},
                {"relative_path": "debug_case.py", "changed": True, "partial": True},
            ],
            workspace_mutation_records=[
                {
                    "tool": "exec_command",
                    "paths": [{"relative_path": "debug_case.py", "classification": "scratch"}],
                }
            ],
        ),
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 2
    assert any(
        isinstance(message.content, str)
        and "[Runtime final-diff check]" in message.content
        and "debug_case.py" in message.content
        for message in provider.calls[1]
    )
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "final_diff_contract_recovery"
        for event in events
    )
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("feature") == "final_diff_contract"
        and event.get("injected_to_model") is True
        and event.get("reason") == "scratch_artifact_in_final_diff"
        for event in logged
    )
    final_diff_events = [
        event for event in logged if event.get("feature") == "final_diff_contract"
    ]
    assert final_diff_events
    assert all(
        "runtime_events.jsonl" not in (event.get("diff_paths") or [])
        for event in final_diff_events
    )
    final_diff_event = final_diff_events[0]
    expected_receipt_summary = {
        "workspace_mutation_receipt_count": 3,
        "changed_receipt_count": 2,
        "noop_receipt_count": 1,
        "partial_receipt_count": 1,
    }
    for key, value in expected_receipt_summary.items():
        assert final_diff_event["details"][key] == value
        assert final_diff_event["evidence"][key] == value


@pytest.mark.asyncio
async def test_agent_final_diff_contract_log_mode_does_not_prompt_model(tmp_path) -> None:
    _init_git_repo_with_source(tmp_path)
    (tmp_path / "debug_case.py").write_text("print('repro')\n", encoding="utf-8")
    runtime_events_path = tmp_path.parent / f"{tmp_path.name}-runtime_events.jsonl"
    provider = _FinalThenDoneProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            flush_enabled=False,
            progress_watchdog_mode="log",
            final_diff_contract_mode="log",
            runtime_events_path=str(runtime_events_path),
        ),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 1
    assert not [
        event
        for event in events
        if isinstance(event, WarningEvent) and event.code == "final_diff_contract_recovery"
    ]
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("feature") == "final_diff_contract"
        and event.get("injected_to_model") is False
        for event in logged
    )
    final_diff_event = next(
        event for event in logged if event.get("feature") == "final_diff_contract"
    )
    expected_receipt_summary = {
        "workspace_mutation_receipt_count": 0,
        "changed_receipt_count": 0,
        "noop_receipt_count": 0,
        "partial_receipt_count": 0,
    }
    for key, value in expected_receipt_summary.items():
        assert final_diff_event["details"][key] == value
        assert final_diff_event["evidence"][key] == value


@pytest.mark.asyncio
async def test_agent_final_diff_contract_warns_for_empty_diff_after_workspace_write(
    tmp_path,
) -> None:
    _init_git_repo_with_source(tmp_path)
    (tmp_path / "src" / "parser.py").write_text("old\n", encoding="utf-8")
    runtime_events_path = tmp_path.parent / f"{tmp_path.name}-empty-diff-events.jsonl"
    provider = _FinalThenDoneProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            flush_enabled=False,
            progress_watchdog_mode="log",
            final_diff_contract_mode="warn_model",
            runtime_events_path=str(runtime_events_path),
        ),
        tool_context=ToolContext(
            workspace_dir=str(tmp_path),
            workspace_file_writes=[
                {"relative_path": "src/parser.py", "path": str(tmp_path / "src/parser.py")}
            ],
        ),
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 2
    assert any(
        isinstance(message.content, str)
        and "[Runtime final-diff check]" in message.content
        and "Current diff paths: <none>" in message.content
        and "src/parser.py" in message.content
        for message in provider.calls[1]
    )
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "final_diff_contract_recovery"
        for event in events
    )
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event.get("feature") == "final_diff_contract"
        and event.get("injected_to_model") is True
        and event.get("reason") == "workspace_writes_without_final_diff"
        and event.get("diff_paths") == []
        for event in logged
    )


@pytest.mark.asyncio
async def test_agent_records_final_diff_contract_on_finish_error_with_diff(tmp_path) -> None:
    _init_git_repo_with_source(tmp_path)
    (tmp_path / "debug_case.py").write_text("print('repro')\n", encoding="utf-8")
    runtime_events_path = tmp_path.parent / f"{tmp_path.name}-error-runtime_events.jsonl"
    provider = _ProviderRaisesTimeout()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            timeout=60.0,
            iteration_timeout=30.0,
            flush_enabled=False,
            progress_watchdog_mode="log",
            final_diff_contract_mode="warn_model",
            runtime_events_path=str(runtime_events_path),
        ),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "request_error"
        and event.failure_kind == "transport_transient"
        for event in events
    )
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout"
        for event in events
    )
    assert "provider transport timeout" not in repr(events)
    assert not [
        event
        for event in events
        if isinstance(event, WarningEvent) and event.code == "final_diff_contract_recovery"
    ]
    logged = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event.get("reason") == "finish_error_with_non_empty_diff" for event in logged)
    final_diff_events = [
        event for event in logged if event.get("feature") == "final_diff_contract"
    ]
    assert final_diff_events
    final_diff_event = final_diff_events[0]
    assert final_diff_event["mode"] == "warn_model"
    assert final_diff_event["action"] == "observe"
    assert final_diff_event["injected_to_model"] is False
    assert final_diff_event["reason"] == "scratch_artifact_in_final_diff"
    assert final_diff_event["diff_paths"] == ["debug_case.py", "src/parser.py"]
    assert final_diff_event["evidence"]["scratch_paths"] == ["debug_case.py"]
    assert final_diff_event["evidence"]["source_paths"] == ["src/parser.py"]


@pytest.mark.asyncio
async def test_agent_warn_model_recovers_before_final_after_failed_tool_with_diff(
    tmp_path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    provider = _FailedToolThenFinalProvider()
    tool_context = ToolContext(workspace_dir=str(tmp_path))

    async def _failing_after_write(call: Any) -> ToolResult:
        tool_context.workspace_file_writes.append(
            {"relative_path": "src.py", "path": str(source)}
        )
        source.write_text("new\n", encoding="utf-8")
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=(
                "[shell_warning:masked_pipeline_failure]\n"
                "error[E0308]: mismatched types"
            ),
            is_error=True,
            execution_status={
                "version": 1,
                "status": "error",
                "exit_code": 0,
                "timed_out": False,
                "truncated": False,
                "reason": "masked_pipeline_failure",
                "source": "adapter",
                "preservation_class": "diagnostic",
            },
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=4,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_failing_after_write,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 3
    assert any(
        isinstance(message.content, str)
        and "[Runtime progress warning]" in message.content
        and "masked_pipeline_failure" in message.content
        and "Do not finalize this patch yet" in message.content
        for message in provider.calls[2]
    )
    assert any(
        isinstance(event, WarningEvent)
        and event.code == "failed_tool_finalization_recovery"
        for event in events
    )
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"
    assert agent.config.metadata["failed_tool_finalization_recoveries"] == 1


@pytest.mark.asyncio
async def test_agent_rewarns_after_new_failed_focused_verification_with_diff(
    tmp_path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    verification_calls = 0

    async def _tool(call: Any) -> ToolResult:
        nonlocal verification_calls
        if call.tool_name == "edit_file":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        if call.tool_name == "exec_command":
            verification_calls += 1
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=f"error: focused validation failure {verification_calls}",
                is_error=True,
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _RepeatedFailedVerificationFinalProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=8,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    warning_events = [
        event
        for event in events
        if isinstance(event, WarningEvent)
        and event.code == "failed_tool_finalization_recovery"
    ]
    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 6
    assert len(warning_events) == 2
    assert agent.config.metadata["failed_tool_finalization_recoveries"] == 2
    assert any(
        isinstance(message.content, str)
        and "focused validation still failed" in message.content
        and "focused validation failure 1" in message.content
        for message in provider.calls[3]
    )
    assert any(
        isinstance(message.content, str)
        and "focused validation still failed" in message.content
        and "focused validation failure 2" in message.content
        for message in provider.calls[5]
    )
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 6"


@pytest.mark.asyncio
async def test_agent_does_not_warn_after_clean_maven_verification_summary(
    tmp_path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    success_log = (
        "exit_code=0\n"
        "[INFO] Results:\n"
        "[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0\n"
        "[INFO] Scanned 862 class file(s) for forbidden API invocations, 0 error(s).\n"
        "[INFO] BUILD SUCCESS\n"
    )
    assert Agent._tool_result_has_validation_success_signal(success_log)
    assert not Agent._tool_result_has_failure_signal(success_log)
    short_success_log = "test result: ok. 4 passed; 0 failed\n"
    assert Agent._tool_result_has_validation_success_signal(short_success_log)
    assert not Agent._tool_result_has_failure_signal(short_success_log)

    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "edit_file":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        if call.tool_name == "exec_command":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=success_log,
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _PostWriteCleanMavenVerificationThenFinalProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=5,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert not [
        event
        for event in events
        if isinstance(event, WarningEvent)
        and event.code == "failed_tool_finalization_recovery"
    ]
    assert len(provider.calls) == 3
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"
    assert "failed_tool_finalization_recoveries" not in agent.config.metadata


@pytest.mark.asyncio
async def test_agent_warns_before_final_without_successful_focused_verification(
    tmp_path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    tool_context = ToolContext(workspace_dir=str(tmp_path))

    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "edit_file":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _EditThenFinalProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=4,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
        ),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 3
    assert any(
        isinstance(message.content, str)
        and "before any focused validation command succeeded" in message.content
        for message in provider.calls[2]
    )
    assert agent.config.metadata["failed_tool_finalization_recoveries"] == 1
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final attempt 3"


def test_agent_focused_verification_recognizes_build_and_linter_checks() -> None:
    agent = object.__new__(Agent)

    assert agent._command_looks_like_focused_verification(
        "cd /testbed && cargo build --release --bin ruff 2>&1 | tail -30"
    )
    assert agent._command_looks_like_focused_verification(
        "cd /testbed && cargo check -p ruff_linter"
    )
    assert agent._command_looks_like_focused_verification(
        "./target/release/ruff check /tmp/repro.py --select=F523 --fix"
    )
    assert agent._command_looks_like_focused_verification("make check")
    assert agent._command_looks_like_focused_verification(
        "./run-tests.py -i basics/try_finally_return.py"
    )
    assert agent._command_looks_like_focused_verification("tests/jqtest")


def test_focused_verification_classifier_success() -> None:
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="exit_code=0\n3 passed\n",
        is_error=False,
    )

    assert Agent._classify_focused_verification_result(result) == "success"


def test_focused_verification_classifier_failure() -> None:
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="exit_code=1\nFAILED tests/test_demo.py::test_demo\n",
        is_error=True,
    )

    assert Agent._classify_focused_verification_result(result) == "failure"


def test_focused_verification_classifier_unknown_without_success_signal() -> None:
    result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="exit_code=0\nran command and wrote logs\n",
        is_error=False,
    )

    assert Agent._classify_focused_verification_result(result) == "unknown"


def test_agent_source_context_signature_includes_exec_source_reads() -> None:
    agent = object.__new__(Agent)
    source_result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="1\tfn important() {}\n",
    )

    signature = agent._source_context_signature(
        [
            ToolCall(
                tool_use_id="tool-1",
                tool_name="exec_command",
                arguments={"command": "sed -n '1,20p' src/lib.rs"},
            )
        ],
        [source_result],
    )

    assert signature is not None


def test_agent_source_context_signature_ignores_non_source_exec_commands() -> None:
    agent = object.__new__(Agent)
    test_result = ToolResult(
        tool_use_id="tool-1",
        tool_name="exec_command",
        content="test result: ok. 4 passed; 0 failed\n",
    )

    signature = agent._source_context_signature(
        [
            ToolCall(
                tool_use_id="tool-1",
                tool_name="exec_command",
                arguments={"command": "cargo test -p parser"},
            )
        ],
        [test_result],
    )

    assert signature is None


def test_agent_filters_gitlink_only_porcelain_status() -> None:
    status = (
        " m modules/oniguruma\n"
        " M src/parser.y\n"
        " M sample.json\n"
        "A  sample2.json\n"
        "?? sample.json\n"
        "?? scratch.py\n"
        "?? src/new_module.py\n"
    )

    filtered = Agent._filter_gitlink_porcelain_status(
        status,
        {"modules/oniguruma"},
    )

    assert "modules/oniguruma" not in filtered
    assert " M src/parser.y" in filtered
    assert " M sample.json" in filtered
    assert "A  sample2.json" not in filtered
    assert "?? sample.json" not in filtered
    assert "?? scratch.py" not in filtered
    assert "?? src/new_module.py" in filtered
    assert Agent._filter_gitlink_porcelain_status(
        " m modules/oniguruma\n",
        {"modules/oniguruma"},
    ) == ""


@pytest.mark.asyncio
async def test_agent_ignores_gitlink_only_workspace_diff(tmp_path) -> None:
    submodule = tmp_path / "submodule"
    repo = tmp_path / "repo"
    submodule.mkdir()
    repo.mkdir()
    for path in (submodule, repo):
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=path,
            check=True,
            capture_output=True,
        )
    (submodule / "file.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=submodule, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "submodule init"],
        cwd=submodule,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(submodule),
            "modules/oniguruma",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "commit", "-m", "repo init"], cwd=repo, check=True, capture_output=True)
    (repo / "modules/oniguruma/file.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "sample.json").write_text('{"repro": true}\n', encoding="utf-8")

    agent = Agent(
        provider=_FinalThenDoneProvider(),
        config=AgentConfig(flush_enabled=False),
        tool_context=ToolContext(workspace_dir=str(repo)),
    )

    assert await agent._workspace_git_status_porcelain() == ""
    assert agent._workspace_diff_paths_for_runtime_event() == []
    assert agent._workspace_diff_fingerprint_for_runtime_event() is None

    (repo / "src").mkdir()
    (repo / "src/new_module.py").write_text("value = 1\n", encoding="utf-8")

    status = await agent._workspace_git_status_porcelain()
    assert status == "?? src/new_module.py\n"
    assert agent._workspace_diff_paths_for_runtime_event() == ["src/new_module.py"]
    assert agent._workspace_diff_fingerprint_for_runtime_event() is not None


def test_progress_watchdog_post_write_guidance_is_diff_focused() -> None:
    message = _progress_watchdog_guidance_message(
        "verified_workspace_diff_continued_tool_activity",
        {
            "count": 3,
            "workspace_write_count": 1,
        },
    )

    assert "You already have repository edits" in message
    assert "latest verification result" in message
    assert "Stop broad source exploration" in message


def test_progress_watchdog_repeated_post_write_guidance_limits_source_tools() -> None:
    message = _progress_watchdog_guidance_message(
        "verified_workspace_diff_continued_tool_activity",
        {
            "count": 6,
            "workspace_write_count": 1,
        },
    )

    assert "have received this warning again" in message
    assert "Do not call read_file" in message
    assert "make a source edit" in message


def test_progress_watchdog_code_fix_no_write_guidance_requires_workspace_edit() -> None:
    message = _progress_watchdog_guidance_message(
        "tool_activity_without_workspace_write",
        {
            "count": 16,
            "scratch_write_count": 4,
            "workspace_change_likely_required": True,
        },
    )

    assert "appears to require a repository patch" in message
    assert "no tracked workspace source file has been changed yet" in message
    assert "targeted source reads/searches" in message
    assert "writing more scratch notes" in message
    assert "use an available source-edit tool" in message
    assert "apply_patch, edit_file, or write_file" not in message


def test_workspace_edit_gate_rejects_unconfigured_external_write_file(tmp_path) -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )
    gate_details = {
        "reason": "tool_activity_without_workspace_write",
        "count": 16,
        "threshold": 8,
    }

    scratch_result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-1",
            tool_name="write_file",
            arguments={"path": "/tmp/notes.md", "content": "notes"},
        ),
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )
    workspace_result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-2",
            tool_name="write_file",
            arguments={"path": str(tmp_path / "src.py"), "content": "patch"},
        ),
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert scratch_result is not None
    assert scratch_result.is_error is True
    assert scratch_result.execution_status["reason"] == "workspace_edit_required"
    assert workspace_result is None


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_workspace_edit_gate_allows_configured_scratch_repro_file(
    tmp_path,
    tool_name: str,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    tool_context = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
    )
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=tool_context,
    )
    gate_details = {
        "reason": "tool_activity_without_workspace_write",
        "count": 16,
        "threshold": 8,
    }
    target = scratch / "repro_issue.tcl"
    if tool_name == "edit_file":
        target.write_text("before\n", encoding="utf-8")
    arguments = (
        {"path": str(target), "content": "puts repro\n"}
        if tool_name == "write_file"
        else {"path": str(target), "old_text": "before", "new_text": "after"}
    )
    tool_call = ToolCall(
        tool_use_id=f"{tool_name}-scratch",
        tool_name=tool_name,
        arguments=arguments,
    )

    result = agent._workspace_edit_gate_tool_result(
        tool_call,
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is None
    assert agent._tool_call_targets_workspace_path(tool_call) is False
    tool_context.scratch_file_writes.append(
        {
            "path": str(target),
            "relative_path": "repro_issue.tcl",
            "name": "repro_issue.tcl",
            "suffix": ".tcl",
        }
    )
    assert agent._effective_workspace_write_records() == []


def test_workspace_edit_gate_allows_custom_external_scratch_root(tmp_path) -> None:
    # Anchored to the temp drive so the path is absolute on Windows too;
    # a bare "/opt/..." has no drive there and falls back to cwd-relative.
    scratch = Path(tmp_path.anchor) / "opensquilla-custom-scratch"
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(tmp_path),
            scratch_dir=str(scratch),
        ),
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-custom-scratch",
            tool_name="write_file",
            arguments={
                "path": str(scratch / "reproduce_issue.py"),
                "content": "print('repro')\n",
            },
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is None


@pytest.mark.parametrize("scratch_relation", ["workspace_ancestor", "same_root"])
def test_tool_context_rejects_scratch_root_containing_workspace(
    tmp_path,
    scratch_relation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = tmp_path if scratch_relation == "workspace_ancestor" else workspace

    with pytest.raises(ValueError, match="must not equal or contain workspace_dir"):
        ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        )


def test_agent_revalidates_mutated_tool_context_roots(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool_context = ToolContext(workspace_dir=str(workspace))
    tool_context.scratch_dir = str(tmp_path)

    with pytest.raises(ValueError, match="must not equal or contain workspace_dir"):
        Agent(
            provider=_ContextOverflowProvider(success_after=1),
            tool_context=tool_context,
        )


def test_configured_hidden_scratch_diff_is_not_source_progress(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = workspace / ".artifacts"
    scratch.mkdir()
    target = scratch / "repro.py"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(["git", "add", ".artifacts/repro.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True)
    target.write_text("after\n", encoding="utf-8")
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )

    assert agent._workspace_tracked_diff_paths_for_nudge() == []
    assert agent._workspace_has_source_change_evidence() is False
    observation = agent._final_diff_contract_observation()
    assert observation is not None
    assert observation.source_paths == []
    assert observation.scratch_paths == [".artifacts/repro.py"]


@pytest.mark.asyncio
async def test_workspace_edit_gate_allows_real_configured_scratch_edit_file(
    tmp_path,
    monkeypatch,
) -> None:
    # Scratch write tracking is a workspace policy layer; opt out of the
    # sandbox-disabled Full Host Access fallback so it stays active.
    monkeypatch.setenv("OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST", "off")
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = scratch / "repro_issue.py"
    target.write_text("before\n", encoding="utf-8")
    tool_context = ToolContext(
        is_owner=True,
        interaction_mode=InteractionMode.UNATTENDED,
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        file_edit_requires_fresh_read=True,
    )
    configure_runtime(
        SandboxSettings(
            sandbox=False,
            security_grading=False,
            allow_legacy_mode=True,
        ),
        workspace=workspace,
    )
    try:
        real_handler = build_tool_handler(get_default_registry(), tool_context)
        agent = Agent(
            provider=_ContextOverflowProvider(success_after=1),
            tool_handler=real_handler,
            tool_context=tool_context,
        )
        read_result = await agent._execute_tool(
            ToolCall(
                tool_use_id="read-scratch-repro",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        )
        edit_call = ToolCall(
            tool_use_id="edit-scratch-repro",
            tool_name="edit_file",
            arguments={
                "path": str(target),
                "old_text": "before",
                "new_text": "after",
            },
        )
        gate_result = agent._workspace_edit_gate_tool_result(
            edit_call,
            {
                "reason": "tool_activity_without_workspace_write",
                "count": 16,
                "threshold": 8,
            },
            recovery_read_paths=set(),
            recovery_reads_remaining=0,
        )
        edit_result = await agent._execute_tool(edit_call)
    finally:
        reset_runtime()

    assert read_result.is_error is False
    assert gate_result is None
    assert edit_result.is_error is False
    assert target.read_text(encoding="utf-8") == "after\n"
    assert [record["relative_path"] for record in tool_context.scratch_file_writes] == [
        "repro_issue.py"
    ]
    assert agent._effective_workspace_write_records() == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "notes.md",
        "notes.txt",
        "notes",
        "../outside/repro.py",
        "../workspace/source.py",
    ],
)
def test_workspace_edit_gate_rejects_non_repro_or_escaped_configured_scratch_write(
    tmp_path,
    relative_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )
    gate_details = {
        "reason": "tool_activity_without_workspace_write",
        "count": 16,
        "threshold": 8,
    }

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-scratch",
            tool_name="write_file",
            arguments={
                "path": str(scratch / relative_path),
                "content": "not source progress\n",
            },
        ),
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is not None
    assert result.is_error is True
    assert result.execution_status["reason"] == "workspace_edit_required"


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("repro.py", True),
        ("case.tcl", True),
        ("notes.md", False),
        ("../outside/repro.py", False),
    ],
)
def test_workspace_edit_gate_only_allows_write_scratch_repro_scripts(
    tmp_path,
    path: str,
    allowed: bool,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-scratch-tool",
            tool_name="write_scratch",
            arguments={"path": path, "content": "diagnostic\n"},
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert (result is None) is allowed
    if result is not None:
        assert result.is_error is True
        assert result.execution_status["reason"] == "workspace_edit_required"


def test_workspace_edit_gate_rejects_configured_scratch_prefix_collision(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    scratch_collision = tmp_path / "scratch-elsewhere"
    workspace.mkdir()
    scratch.mkdir()
    scratch_collision.mkdir()
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-prefix-collision",
            tool_name="write_file",
            arguments={
                "path": str(scratch_collision / "repro.py"),
                "content": "print('outside')\n",
            },
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is not None
    assert result.is_error is True
    assert result.execution_status["reason"] == "workspace_edit_required"


@pytest.mark.parametrize("path_form", ["absolute", "relative"])
def test_workspace_edit_gate_rejects_configured_scratch_inside_workspace(
    tmp_path,
    path_form: str,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = workspace / ".scratch"
    scratch.mkdir(parents=True)
    tool_context = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
    )
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=tool_context,
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-nested-scratch",
            tool_name="write_file",
            arguments={
                "path": (
                    str(scratch / "repro.py")
                    if path_form == "absolute"
                    else ".scratch/repro.py"
                ),
                "content": "print('repro')\n",
            },
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is not None
    assert result.is_error is True
    assert result.execution_status["reason"] == "workspace_edit_required"
    tool_context.workspace_file_writes.append(
        {
            "path": str(scratch / "repro.py"),
            "relative_path": ".scratch/repro.py",
            "created": True,
        }
    )
    tool_context.workspace_mutation_receipts.append(
        {
            "relative_path": ".scratch/repro.py",
            "classification": "scratch",
            "changed": True,
            "partial": False,
        }
    )
    assert agent._effective_workspace_write_records() == []
    assert agent._workspace_mutation_receipt_counts() == {
        "changed_receipt_count": 0,
        "noop_receipt_count": 0,
        "partial_receipt_count": 0,
    }
    assert agent._workspace_has_source_change_evidence() is False


@pytest.mark.parametrize("escape_destination", ["outside", "workspace"])
def test_workspace_edit_gate_rejects_configured_scratch_symlink_escape(
    tmp_path,
    escape_destination: str,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    outside = tmp_path / "outside"
    workspace.mkdir()
    scratch.mkdir()
    outside.mkdir()
    escape = scratch / "escape"
    try:
        escape.symlink_to(
            workspace if escape_destination == "workspace" else outside,
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-symlink-escape",
            tool_name="write_file",
            arguments={
                "path": str(escape / "repro.py"),
                "content": "print('outside')\n",
            },
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is not None
    assert result.is_error is True
    assert result.execution_status["reason"] == "workspace_edit_required"


@pytest.mark.parametrize(("filename", "allowed"), [("repro.py", True), ("notes.md", False)])
def test_workspace_edit_gate_classifies_workspace_symlink_into_scratch(
    tmp_path,
    filename: str,
    allowed: bool,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    scratch_alias = workspace / "scratch-alias"
    try:
        scratch_alias.symlink_to(scratch, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-scratch-alias",
            tool_name="write_file",
            arguments={
                "path": f"scratch-alias/{filename}",
                "content": "diagnostic\n",
            },
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert (result is None) is allowed
    if result is not None:
        assert result.execution_status["reason"] == "workspace_edit_required"


@pytest.mark.parametrize("target_kind", ["external", "nested", "nested_escape"])
def test_workspace_edit_gate_rejects_apply_patch_to_configured_scratch(
    tmp_path,
    target_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = (
        tmp_path / "scratch" if target_kind == "external" else workspace / ".scratch"
    )
    scratch.mkdir()
    patch_target = {
        "external": str(scratch / "repro.py"),
        "nested": ".scratch/repro.py",
        "nested_escape": ".scratch/../src.py",
    }[target_kind]
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )

    result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="patch-scratch",
            tool_name="apply_patch",
            arguments={
                "patch": "\n".join(
                    [
                        "*** Begin Patch",
                        f"*** Add File: {patch_target}",
                        "+print('repro')",
                        "*** End Patch",
                    ]
                )
            },
        ),
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert result is not None
    assert result.is_error is True
    assert result.execution_status["reason"] == "workspace_edit_required"


def test_finalize_evidence_classifies_each_apply_patch_target(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    scratch = workspace / ".scratch"
    scratch.mkdir(parents=True)
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )
    tool_call = ToolCall(
        tool_use_id="patch-mixed",
        tool_name="apply_patch",
        arguments={
            "patch": (
                "*** Begin Patch\n"
                "*** Add File: .scratch/repro.py\n"
                "+print('repro')\n"
                "*** Update File: src.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            )
        },
    )

    assert agent._finalize_evidence_write_targets(tool_call) == [
        (".scratch/repro.py", True),
        ("src.py", False),
    ]

    context_literal_call = ToolCall(
        tool_use_id="patch-context-literal",
        tool_name="apply_patch",
        arguments={
            "patch": (
                "  *** Begin Patch  \n"
                "*** Update File: docs/example.txt\n"
                "@@ -1,1 +1,1 @@\n"
                " *** Add File: .scratch/repro.py\n"
                "-old\n"
                "+new\n"
                "  *** End Patch  \n"
            )
        },
    )
    assert agent._workspace_edit_gate_apply_patch_raw_target_paths(
        context_literal_call
    ) == ["docs/example.txt"]
    assert agent._finalize_evidence_write_targets(
        ToolCall(
            tool_use_id="edit-source-scratch",
            tool_name="edit_source",
            arguments={"path": ".scratch/repro.py"},
        )
    ) == [(".scratch/repro.py", True)]

    source_patch_file = scratch / "source.patch"
    source_patch_file.write_text(
        "*** Begin Patch\n"
        "*** Update File: src.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n",
        encoding="utf-8",
    )
    scratch_patch_file = scratch / "scratch.patch"
    scratch_patch_file.write_text(
        "*** Begin Patch\n"
        "*** Add File: .scratch/repro.py\n"
        "+print('repro')\n"
        "*** End Patch\n",
        encoding="utf-8",
    )
    source_patch_call = ToolCall(
        tool_use_id="source-patch-file",
        tool_name="apply_patch",
        arguments={"path": str(source_patch_file)},
    )
    scratch_patch_call = ToolCall(
        tool_use_id="scratch-patch-file",
        tool_name="apply_patch",
        arguments={"path": str(scratch_patch_file)},
    )
    gate_details = {
        "reason": "tool_activity_without_workspace_write",
        "count": 16,
        "threshold": 8,
    }

    assert agent._finalize_evidence_write_targets(source_patch_call) == [("src.py", False)]
    assert agent._finalize_evidence_write_targets(scratch_patch_call) == [
        (".scratch/repro.py", True)
    ]
    assert (
        agent._workspace_edit_gate_tool_result(
            source_patch_call,
            gate_details,
            recovery_read_paths=set(),
            recovery_reads_remaining=0,
        )
        is None
    )
    blocked = agent._workspace_edit_gate_tool_result(
        scratch_patch_call,
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )
    assert blocked is not None
    assert blocked.execution_status["reason"] == "workspace_edit_required"

    frozen_source_patch_call = agent._snapshot_apply_patch_path_call(source_patch_call)
    assert frozen_source_patch_call is not source_patch_call
    assert frozen_source_patch_call.arguments["path"] == str(source_patch_file)
    source_patch_file.unlink()
    assert agent._finalize_evidence_write_targets(frozen_source_patch_call) == [
        ("src.py", False)
    ]
    assert agent._finalize_evidence_write_targets(source_patch_call) == [(None, False)]


def test_workspace_edit_gate_handles_unexpandable_paths(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )
    tool_call = ToolCall(
        tool_use_id="unexpandable-patch-path",
        tool_name="apply_patch",
        arguments={"path": "~opensquilla_user_that_does_not_exist/fix.patch"},
    )

    assert agent._snapshot_apply_patch_path_call(tool_call) is tool_call
    assert agent._workspace_edit_gate_apply_patch_raw_target_paths(tool_call) == []
    assert agent._configured_scratch_path_candidate(
        "~opensquilla_user_that_does_not_exist/repro.py",
        relative_to="workspace",
    ) == (None, False)
    blocked = agent._workspace_edit_gate_tool_result(
        tool_call,
        {
            "reason": "tool_activity_without_workspace_write",
            "count": 16,
            "threshold": 8,
        },
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )
    assert blocked is not None
    assert blocked.execution_status["reason"] == "workspace_edit_required"


def test_apply_patch_snapshot_rejects_fifo_without_blocking(tmp_path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    fifo = scratch / "fix.patch"
    os.mkfifo(fifo)
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(
            workspace_dir=str(workspace),
            scratch_dir=str(scratch),
        ),
    )
    tool_call = ToolCall(
        tool_use_id="fifo-patch-path",
        tool_name="apply_patch",
        arguments={"path": str(fifo)},
    )

    assert agent._workspace_edit_gate_apply_patch_text(tool_call) is None
    assert agent._snapshot_apply_patch_path_call(tool_call) is tool_call


def test_apply_patch_snapshot_rejects_blank_patch_file(tmp_path) -> None:
    patch_file = tmp_path / "blank.patch"
    patch_file.write_text(" \n", encoding="utf-8")
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )
    tool_call = ToolCall(
        tool_use_id="blank-patch-path",
        tool_name="apply_patch",
        arguments={"path": str(patch_file)},
    )

    assert agent._snapshot_apply_patch_path_call(tool_call) is tool_call


@pytest.mark.asyncio
async def test_failed_path_patch_snapshot_cannot_execute_later_created_file(
    tmp_path,
) -> None:
    patch_file = tmp_path / "late.patch"
    patch_text = (
        "*** Begin Patch\n"
        "*** Add File: late.py\n"
        "+created\n"
        "*** End Patch\n"
    )

    class _CreateAfterFailedSnapshotAgent(Agent):
        def _snapshot_apply_patch_path_call(self, tc: ToolCall) -> ToolCall:
            frozen = super()._snapshot_apply_patch_path_call(tc)
            if frozen is tc and tc.tool_name == "apply_patch":
                patch_file.write_text(patch_text, encoding="utf-8")
            return frozen

    handler_calls: list[ToolCall] = []

    async def _tool(call: ToolCall) -> ToolResult:
        handler_calls.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unexpected execution",
        )

    agent = _CreateAfterFailedSnapshotAgent(
        provider=_PathPatchThenFinalProvider(patch_file),
        config=AgentConfig(max_iterations=3, flush_enabled=False),
        tool_definitions=[
            ToolDefinition(
                name="apply_patch",
                description="apply_patch tool.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=_tool,
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )

    events = [event async for event in agent.run_turn("Fix the issue")]

    assert patch_file.read_text(encoding="utf-8") == patch_text
    assert handler_calls == []
    assert any(
        getattr(event, "kind", None) == "tool_result"
        and (getattr(event, "execution_status", None) or {}).get("reason")
        == "patch_snapshot_failed"
        for event in events
    )


@pytest.mark.asyncio
async def test_path_patch_snapshot_respects_mutex_order_and_survives_self_delete(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = workspace / ".scratch"
    scratch.mkdir(parents=True)
    source = workspace / "src.py"
    source.write_text("old\n", encoding="utf-8")
    patch_file = scratch / "fix.patch"
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: src.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    provider = _WritePatchFileThenApplyProvider(patch_file, patch_text)
    tool_context = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
    )
    handled_calls: list[ToolCall] = []

    async def _tool(call: ToolCall) -> ToolResult:
        handled_calls.append(call)
        if call.tool_name == "write_file":
            patch_file.write_text(str(call.arguments["content"]), encoding="utf-8")
        elif call.tool_name == "apply_patch":
            assert call.arguments["path"] == str(patch_file)
            assert call.arguments["patch"] == patch_text
            source.write_text("new\n", encoding="utf-8")
            patch_file.unlink()
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
        else:
            raise AssertionError(f"unexpected tool: {call.tool_name}")
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=4, flush_enabled=False),
        tool_definitions=[
            ToolDefinition(
                name=name,
                description=f"{name} tool.",
                input_schema=ToolInputSchema(),
            )
            for name in ["write_file", "apply_patch"]
        ],
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix src.py")]

    assert [call.tool_name for call in handled_calls] == ["write_file", "apply_patch"]
    assert source.read_text(encoding="utf-8") == "new\n"
    assert not patch_file.exists()
    assert any(isinstance(event, DoneEvent) for event in events)


def test_workspace_edit_gate_rejects_synthetic_marker_write_file(tmp_path) -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )
    gate_details = {
        "reason": "tool_activity_without_workspace_write",
        "count": 16,
        "threshold": 8,
    }

    marker_result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-marker",
            tool_name="write_file",
            arguments={
                "path": str(tmp_path / "src" / "debug_marker.h"),
                "content": "/* Placeholder for runtime guard unlock */\n",
            },
        ),
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )
    real_new_file_result = agent._workspace_edit_gate_tool_result(
        ToolCall(
            tool_use_id="write-real",
            tool_name="write_file",
            arguments={
                "path": str(tmp_path / "src" / "feature_support.h"),
                "content": "int feature_support_enabled(void);\n",
            },
        ),
        gate_details,
        recovery_read_paths=set(),
        recovery_reads_remaining=0,
    )

    assert marker_result is not None
    assert marker_result.is_error is True
    assert "temporary marker" in marker_result.content
    assert real_new_file_result is None


def test_effective_workspace_write_records_ignore_synthetic_new_files(tmp_path) -> None:
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    tool_context.workspace_file_writes.extend(
        [
            {
                "relative_path": "src/debug_marker.h",
                "path": str(tmp_path / "src" / "debug_marker.h"),
                "created": True,
            },
            {
                "relative_path": "src/parser.y",
                "path": str(tmp_path / "src" / "parser.y"),
                "created": False,
            },
        ]
    )
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=tool_context,
    )

    assert [record["relative_path"] for record in agent._effective_workspace_write_records()] == [
        "src/parser.y"
    ]


def test_filter_ignored_porcelain_status_ignores_root_scratch_artifacts() -> None:
    status = "\n".join(
        [
            "?? fix.patch",
            "?? src/parser.rs",
            "",
        ]
    )

    assert Agent._filter_ignored_porcelain_status(status, set()) == "?? src/parser.rs\n"


def test_filter_ignored_porcelain_status_can_make_scratch_only_diff_empty() -> None:
    status = "\n".join(
        [
            "?? fix.patch",
            "",
        ]
    )

    assert Agent._filter_ignored_porcelain_status(status, set()) == ""


@pytest.mark.asyncio
async def test_workspace_edit_gate_preserves_source_tools_after_repeated_no_write(
    tmp_path,
) -> None:
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    handler_calls: list[str] = []

    async def _tool(call: Any) -> ToolResult:
        handler_calls.append(call.tool_name)
        if call.tool_name == "read_file":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=source.read_text(encoding="utf-8"),
            )
        if call.tool_name == "apply_patch":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="Applied patch: 1 file(s) modified [workspace]",
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _NoWorkspaceWriteThenPatchProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=25,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
        ),
        tool_definitions=[
            ToolDefinition(
                name=name,
                description=f"{name} tool.",
                input_schema=ToolInputSchema(),
            )
            for name in [
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "edit_file",
                "write_file",
            ]
        ],
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert source.read_text(encoding="utf-8") == "new\n"
    assert handler_calls.count("read_file") == 17
    assert handler_calls.count("apply_patch") == 1
    assert agent.config.metadata["workspace_edit_gate_activations"] == 1
    filtered_tool_names = {tool.name for tool in provider.tools_by_call[16] or []}
    assert filtered_tool_names == {
        "read_file",
        "grep_search",
        "exec_command",
        "apply_patch",
        "edit_file",
        "write_file",
    }
    gated_config = provider.configs[16]
    assert gated_config is not None
    assert "Runtime Patch Progress Guidance" in (gated_config.system or "")
    assert gated_config.tool_choice is None
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(
        getattr(event, "kind", None) == "tool_result"
        and getattr(event, "tool_name", None) == "read_file"
        and (getattr(event, "execution_status", None) or {}).get("reason")
        == "workspace_edit_required"
        for event in events
    )


@pytest.mark.asyncio
async def test_workspace_edit_gate_scratch_repro_does_not_clear_gate(
    tmp_path,
    monkeypatch,
) -> None:
    # Scratch write tracking is a workspace policy layer; opt out of the
    # sandbox-disabled Full Host Access fallback so it stays active.
    monkeypatch.setenv("OPENSQUILLA_SANDBOX_DISABLED_FULL_HOST", "off")
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    source = workspace / "src.py"
    source.write_text("old\n", encoding="utf-8")
    tool_context = ToolContext(
        is_owner=True,
        interaction_mode=InteractionMode.UNATTENDED,
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
    )
    handler_call_ids: list[str] = []
    provider = _ScratchReproThenPatchProvider(scratch)
    configure_runtime(
        SandboxSettings(
            sandbox=False,
            security_grading=False,
            allow_legacy_mode=True,
        ),
        workspace=workspace,
    )
    try:
        registry = get_default_registry()
        real_handler = build_tool_handler(registry, tool_context)

        async def _recording_handler(call: Any) -> ToolResult:
            handler_call_ids.append(call.tool_use_id)
            return await real_handler(call)

        visible_names = {"apply_patch", "read_file", "write_file"}
        agent = Agent(
            provider=provider,
            config=AgentConfig(
                max_iterations=25,
                flush_enabled=False,
                progress_watchdog_mode="warn_model",
            ),
            tool_definitions=[
                tool
                for tool in registry.to_tool_definitions(tool_context)
                if tool.name in visible_names
            ],
            tool_handler=_recording_handler,
            tool_context=tool_context,
        )
        events = [event async for event in agent.run_turn("Fix the failing parser test")]
    finally:
        reset_runtime()

    assert (scratch / "repro_issue.tcl").read_text(encoding="utf-8") == "puts repro\n"
    assert not (scratch / "notes.md").exists()
    assert (scratch / "notes_after_source.md").read_text(encoding="utf-8") == (
        "investigation notes\n"
    )
    assert source.read_text(encoding="utf-8") == "new\n"
    assert handler_call_ids.count("write-repro") == 1
    assert "write-notes" not in handler_call_ids
    assert handler_call_ids.count("patch-1") == 1
    assert handler_call_ids.count("write-notes-after-source") == 1
    assert agent.config.metadata["workspace_edit_gate_activations"] == 1
    assert [record["relative_path"] for record in tool_context.scratch_file_writes] == [
        "repro_issue.tcl",
        "notes_after_source.md",
    ]
    assert [record["relative_path"] for record in agent._effective_workspace_write_records()] == [
        "src.py"
    ]
    blocked_events = [
        event
        for event in events
        if getattr(event, "kind", None) == "tool_result"
        and getattr(event, "tool_name", None) == "write_file"
        and (getattr(event, "execution_status", None) or {}).get("reason")
        == "workspace_edit_required"
    ]
    assert len(blocked_events) == 1
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_workspace_edit_gate_allows_target_read_after_patch_context_failure(
    tmp_path,
) -> None:
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    handler_calls: list[str] = []
    patch_calls = 0

    async def _tool(call: Any) -> ToolResult:
        nonlocal patch_calls
        handler_calls.append(call.tool_name)
        if call.tool_name == "read_file":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=source.read_text(encoding="utf-8"),
            )
        if call.tool_name == "apply_patch":
            patch_calls += 1
            if patch_calls == 1:
                return ToolResult(
                    tool_use_id=call.tool_use_id,
                    tool_name=call.tool_name,
                    content=(
                        "apply_patch context mismatch at line 1: expected 'old', "
                        "got 'older'. Read the current file content and retry with "
                        "exact surrounding context."
                    ),
                    is_error=True,
                )
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="Applied patch: 1 file(s) modified [workspace]",
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _PatchFailureRecoveryProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=30,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
        ),
        tool_definitions=[
            ToolDefinition(
                name=name,
                description=f"{name} tool.",
                input_schema=ToolInputSchema(),
            )
            for name in [
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "edit_file",
                "write_file",
            ]
        ],
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert source.read_text(encoding="utf-8") == "new\n"
    assert handler_calls.count("read_file") == 18
    assert handler_calls.count("apply_patch") == 2
    assert agent.config.metadata["workspace_edit_gate_activations"] == 1
    assert agent.config.metadata["workspace_edit_gate_patch_recoveries"] == 1

    first_gated_tools = {tool.name for tool in provider.tools_by_call[16] or []}
    assert first_gated_tools == {
        "read_file",
        "grep_search",
        "exec_command",
        "apply_patch",
        "edit_file",
        "write_file",
    }
    recovery_tools = {tool.name for tool in provider.tools_by_call[18] or []}
    assert recovery_tools == {
        "read_file",
        "grep_search",
        "exec_command",
        "apply_patch",
        "edit_file",
        "write_file",
    }
    post_recovery_tools = {tool.name for tool in provider.tools_by_call[19] or []}
    assert post_recovery_tools == {
        "read_file",
        "grep_search",
        "exec_command",
        "apply_patch",
        "edit_file",
        "write_file",
    }

    recovery_config = provider.configs[18]
    assert recovery_config is not None
    assert "failed edit target path" in (recovery_config.system or "")
    assert recovery_config.tool_choice is None
    post_recovery_config = provider.configs[19]
    assert post_recovery_config is not None
    assert post_recovery_config.tool_choice is None
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_workspace_edit_gate_allows_target_read_after_edit_context_failure(
    tmp_path,
) -> None:
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    handler_calls: list[str] = []

    async def _tool(call: Any) -> ToolResult:
        handler_calls.append(call.tool_name)
        if call.tool_name == "read_file":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=source.read_text(encoding="utf-8"),
            )
        if call.tool_name == "edit_file":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=(
                    "edit_file could not find old_text in src.py. Read the current "
                    "file content, then retry with exact text from that file."
                ),
                is_error=True,
            )
        if call.tool_name == "apply_patch":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="Applied patch: 1 file(s) modified [workspace]",
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _EditFailureRecoveryProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=30,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
        ),
        tool_definitions=[
            ToolDefinition(
                name=name,
                description=f"{name} tool.",
                input_schema=ToolInputSchema(),
            )
            for name in [
                "read_file",
                "grep_search",
                "exec_command",
                "apply_patch",
                "edit_file",
                "write_file",
            ]
        ],
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert source.read_text(encoding="utf-8") == "new\n"
    assert handler_calls.count("read_file") == 18
    assert handler_calls.count("edit_file") == 1
    assert handler_calls.count("apply_patch") == 1
    assert agent.config.metadata["workspace_edit_gate_activations"] == 1
    assert agent.config.metadata["workspace_edit_gate_patch_recoveries"] == 1

    recovery_tools = {tool.name for tool in provider.tools_by_call[18] or []}
    assert recovery_tools == {
        "read_file",
        "grep_search",
        "exec_command",
        "apply_patch",
        "edit_file",
        "write_file",
    }
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_agent_failed_focused_verification_counts_after_workspace_write(tmp_path) -> None:
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")

    async def _tool(call: Any) -> ToolResult:
        if call.tool_name == "edit_file":
            source.write_text("new\n", encoding="utf-8")
            tool_context.workspace_file_writes.append(
                {"relative_path": "src.py", "path": str(source)}
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        if call.tool_name == "exec_command":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="error: build failed",
                is_error=True,
            )
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="source",
        )

    provider = _PostWriteFailedVerificationThenSourceProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=8,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            tool_failure_loop_block_threshold=0,
        ),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 6
    assert any(
        isinstance(message.content, str)
        and "continued tool activity after a workspace diff and focused verification"
        in message.content
        and "Stop broad source exploration" in message.content
        for message in provider.calls[5]
    )


@pytest.mark.asyncio
async def test_agent_converges_after_stable_verified_workspace_diff(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    source = tmp_path / "src.py"
    source.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            **dict(os.environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    handler_calls: list[str] = []

    async def _tool(call: Any) -> ToolResult:
        handler_calls.append(call.tool_name)
        if call.tool_name == "edit_file":
            before = fingerprint_path(source)
            source.write_text("new\n", encoding="utf-8")
            after = fingerprint_path(source)
            record_semantic_mutation_receipt(
                tool_name="edit_file",
                path=source,
                operation="edit_file",
                before=before,
                after=after,
                partial=False,
                ctx=tool_context,
            )
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="edited",
            )
        if call.tool_name == "exec_command":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="test result: ok. 4 passed; 0 failed\n",
            )
        if call.tool_name == "read_file":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content=source.read_text(encoding="utf-8"),
            )
        raise AssertionError(f"unexpected tool: {call.tool_name}")

    provider = _StableVerifiedDiffThenSourceProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=12,
            flush_enabled=False,
            progress_watchdog_mode="warn_model",
            post_write_convergence_enabled=True,
            runtime_events_path=str(runtime_events_path),
        ),
        tool_handler=_tool,
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Fix the failing parser test")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert handler_calls == ["edit_file", "exec_command", *["read_file"] * 6]
    assert any(
        isinstance(message.content, str)
        and "[Runtime post-write convergence]" in message.content
        and "current diff has stayed unchanged" in message.content
        for call in provider.calls
        for message in call
    )
    assert any(
        isinstance(message.content, str)
        and "[Runtime post-write convergence]" in message.content
        and "Do not call tools" in message.content
        for call in provider.calls
        for message in call
    )
    assert provider.tool_lists[-1] is None
    done_events = [event for event in events if isinstance(event, DoneEvent)]
    assert done_events[-1].text == "final after convergence 9"
    runtime_events = [
        json.loads(line)
        for line in runtime_events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(row.get("name") == "post_write_convergence.warned" for row in runtime_events)
    assert any(row.get("name") == "post_write_convergence.finalized" for row in runtime_events)


@pytest.mark.asyncio
async def test_agent_blocks_repeated_missing_tool_handler_failures() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(tool_failure_loop_block_threshold=3),
    )
    tool_call = ToolCall(
        tool_use_id="missing-1",
        tool_name="missing_tool",
        arguments={"value": "same"},
    )

    await agent._execute_tool(tool_call)
    await agent._execute_tool(tool_call)
    third = await agent._execute_tool(tool_call)

    assert "tool_failure_loop_exhausted" not in third.content
    assert "Do not retry this exact call unchanged" in third.content
    assert third.execution_status is not None
    assert third.execution_status.get("reason") == "tool_failure_loop_exhausted"


@pytest.mark.asyncio
async def test_agent_provider_request_proof_budget_is_separate_from_tool_result_cap() -> None:
    provider = _ConfigCapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,
            tool_result_provider_request_max_chars=96_000,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert provider.configs
    assert provider.configs[0] is not None
    assert provider.configs[0].provider_request_max_chars > 96_000


@pytest.mark.asyncio
async def test_agent_provider_request_proof_budget_accepts_explicit_override() -> None:
    provider = _ConfigCapturingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_request_proof_max_chars=123_456,
            tool_result_provider_request_max_chars=96_000,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert provider.configs
    assert provider.configs[0] is not None
    assert provider.configs[0].provider_request_max_chars == 123_456


def test_agent_child_config_inherits_tool_failure_loop_thresholds() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(
            tool_failure_loop_block_threshold=7,
            provider_context_block_feedback=True,
            identical_request_loop_break_threshold=9,
            repeated_tool_call_recovery_threshold=11,
            progress_watchdog_mode="warn_model",
            progress_watchdog_repeated_tool_error_threshold=5,
            progress_watchdog_repeated_provider_failure_threshold=4,
            progress_watchdog_repeated_failure_anchor_threshold=6,
            tool_loop_observer_mode="log",
            runtime_recovery_mode="warn_model",
            runtime_recovery_source_loop_max_nudges=3,
            post_tool_empty_recovery_mode="warn_model",
            reasoning_prefill_recovery_mode="recover",
            runtime_events_path="/tmp/runtime-events.jsonl",
        ),
    )

    child = agent._make_child_agent(SubagentSpec(task="child task"), depth=1)

    assert child.config.tool_failure_loop_block_threshold == 7
    assert child.config.provider_context_block_feedback is True
    assert child.config.identical_request_loop_break_threshold == 9
    assert child.config.repeated_tool_call_recovery_threshold == 11
    assert child.config.progress_watchdog_mode == "warn_model"
    assert child.config.progress_watchdog_repeated_tool_error_threshold == 5
    assert child.config.progress_watchdog_repeated_provider_failure_threshold == 4
    assert child.config.progress_watchdog_repeated_failure_anchor_threshold == 6
    assert child.config.tool_loop_observer_mode == "log"
    assert child.config.runtime_recovery_mode == "warn_model"
    assert child.config.runtime_recovery_source_loop_max_nudges == 3
    assert child.config.post_tool_empty_recovery_mode == "warn_model"
    assert child.config.reasoning_prefill_recovery_mode == "recover"
    assert child.config.runtime_events_path == "/tmp/runtime-events.jsonl"


def test_agent_child_tool_context_inherits_parent_full_host_run_mode() -> None:
    parent_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        run_mode="full",
        elevated="full",
        workspace_dir="/tmp/opensquilla-workspace",
        session_key="agent:main:webchat:parent",
        sandbox_run_context=RunContext(
            run_mode=RunMode.FULL,
            workspace="/tmp/opensquilla-workspace",
        ),
    )
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        tool_context=parent_context,
        session_key="agent:main:webchat:parent",
    )

    child = agent._make_child_agent(SubagentSpec(task="child task"), depth=1)

    assert child._tool_context is not None
    assert child._tool_context.caller_kind is CallerKind.SUBAGENT
    assert child._tool_context.run_mode == "full"
    assert child._tool_context.elevated == "full"
    assert child._tool_context.sandbox_run_context is not None
    assert child._tool_context.sandbox_run_context.run_mode is RunMode.FULL


def test_agent_config_normalizes_flush_triggers_and_clamps_compaction_tail() -> None:
    config = AgentConfig(
        flush_triggers=["reset", "inline_overflow"],
        compaction_protected_recent_messages=-4,
    )

    assert config.flush_triggers == ["session_reset", "pre_compaction"]
    assert config.compaction_protected_recent_messages == 0


def test_agent_config_rejects_unknown_flush_triggers() -> None:
    with pytest.raises(ValueError, match="unknown flush trigger"):
        AgentConfig(flush_triggers=["manual", "bogus"])


def test_agent_child_config_inherits_context_and_flush_budget_policy() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,
            provider_request_proof_max_chars=123_456,
            tool_use_argument_provider_request_max_chars=12_345,
            tool_result_provider_request_max_chars=54_321,
            max_turn_llm_calls=9,
            max_turn_input_tokens=700_000,
            max_turn_output_tokens=70_000,
            max_turn_billed_cost_usd=0.75,
            max_turn_tool_errors=4,
            flush_enabled=True,
            flush_triggers=["session_reset", "manual", "idle", "pre_compaction"],
            flush_pre_compaction=True,
            flush_timeout_seconds=1.5,
            flush_background_timeout_seconds=15.0,
            flush_backoff_initial_seconds=3.0,
            flush_backoff_max_seconds=30.0,
            flush_archive_max_bytes=999_999,
            flush_compaction_requires_safe_receipt=False,
        ),
    )

    child = agent._make_child_agent(SubagentSpec(task="child task"), depth=1)

    assert child.config.context_window_tokens == 200_000
    assert child.config.provider_request_proof_max_chars == 123_456
    assert child.config.tool_use_argument_provider_request_max_chars == 12_345
    assert child.config.tool_result_provider_request_max_chars == 54_321
    assert child.config.max_turn_llm_calls == 9
    assert child.config.max_turn_input_tokens == 700_000
    assert child.config.max_turn_output_tokens == 70_000
    assert child.config.max_turn_billed_cost_usd == 0.75
    assert child.config.max_turn_tool_errors == 4
    assert child.config.flush_enabled is True
    assert child.config.flush_triggers == [
        "session_reset",
        "manual",
        "idle",
        "pre_compaction",
    ]
    assert child.config.flush_pre_compaction is True
    assert child.config.flush_timeout_seconds == 1.5
    assert child.config.flush_background_timeout_seconds == 15.0
    assert child.config.flush_backoff_initial_seconds == 3.0
    assert child.config.flush_backoff_max_seconds == 30.0
    assert child.config.flush_archive_max_bytes == 999_999
    assert child.config.flush_compaction_requires_safe_receipt is False


def test_agent_config_max_turn_cost_usd_defaults_to_disabled() -> None:
    assert AgentConfig().max_turn_cost_usd == 0.0


@pytest.mark.asyncio
async def test_agent_skips_price_resolution_per_event_when_turn_cost_budget_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # max_turn_cost_usd left at its disabled default (0.0): the turn-cost
    # accumulator in the per-event ProviderDoneEvent branch must never touch
    # the (potentially network-blocking) price resolver, even across several
    # cost-blind LLM calls in the same turn. A single call to
    # resolve_model_price still happens once, at the very end of the turn,
    # for the pre-existing DoneEvent cost-reporting computation (out of scope
    # for this gate) — so this asserts the count does *not* scale with the
    # number of LLM calls, rather than asserting zero calls overall.
    import opensquilla.engine.pricing as pricing_module

    calls: list[tuple[str, str]] = []
    real_resolve_model_price = pricing_module.resolve_model_price

    def _counting_resolve_model_price(model_id: str, provider: str = "") -> Any:
        calls.append((model_id, provider))
        return real_resolve_model_price(model_id, provider)

    monkeypatch.setattr(pricing_module, "resolve_model_price", _counting_resolve_model_price)

    provider = _HighUsageToolLoopProvider(tool_rounds=3, input_tokens_per_call=1000)

    async def _tool(call: Any) -> ToolResult:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="deepseek/deepseek-v4-pro-20260423",
            flush_enabled=False,
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 4
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" for event in events)
    # Exactly one call: the end-of-turn DoneEvent cost estimate. None of the
    # four per-event ProviderDoneEvent accumulation passes should have called
    # the resolver while the gate is disabled.
    assert len(calls) == 1


def test_agent_child_config_inherits_max_turn_cost_usd() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(success_after=1),
        config=AgentConfig(max_turn_cost_usd=0.42),
    )

    child = agent._make_child_agent(SubagentSpec(task="child task"), depth=1)

    assert child.config.max_turn_cost_usd == 0.42


@pytest.mark.asyncio
async def test_agent_stops_when_turn_estimated_cost_budget_is_exceeded() -> None:
    # No provider-billed cost at all (billed_cost=0.0 on every call), so the
    # gate must fall back to the estimator to know it is over budget.
    provider = _NoBilledCostUsageProvider(input_tokens=1000, output_tokens=1000)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="deepseek/deepseek-v4-pro-20260423",
            max_turn_cost_usd=0.001,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    error = next(
        event
        for event in events
        if event.kind == "error" and event.code == "turn_cost_budget_exceeded"
    )
    assert "estimated cost basis" in error.message


@pytest.mark.asyncio
async def test_agent_labels_turn_cost_budget_error_as_mixed_when_billed_and_estimated() -> None:
    # Turn mixes a real billed cost (call 1) with an estimated cost (call 2,
    # cost-blind) — the gate's basis label must reflect both contributions.
    provider = _MixedBilledAndEstimatedCostProvider()

    async def _tool(call: Any) -> ToolResult:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            model_id="deepseek/deepseek-v4-pro-20260423",
            max_turn_cost_usd=0.001,
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    error = next(
        event
        for event in events
        if event.kind == "error" and event.code == "turn_cost_budget_exceeded"
    )
    assert "mixed cost basis" in error.message


@pytest.mark.asyncio
async def test_agent_turn_cost_budget_prices_each_unbilled_ensemble_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pricing import PriceEntry

    calls: list[tuple[str, str]] = []

    def resolve(model_id: str, provider: str = "") -> Any:
        calls.append((model_id, provider))
        input_per_m = {("m1", "p1"): 100.0, ("m2", "p2"): 200.0}[
            (model_id, provider)
        ]
        return SimpleNamespace(
            entry=PriceEntry(input_per_m=input_per_m, output_per_m=0.0),
            source="test",
        )

    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        resolve,
    )
    provider = _EnsembleUsageBreakdownProvider(
        billed_cost=0.0,
        rows=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 1000,
                "output_tokens": 0,
                "billed_cost": 0.0,
            },
            {
                "provider": "p2",
                "model": "m2",
                "input_tokens": 1000,
                "output_tokens": 0,
                "billed_cost": 0.0,
            },
        ],
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="ensemble",
            model_id="aggregator",
            max_turn_cost_usd=0.25,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    error = next(
        event
        for event in events
        if event.kind == "error" and event.code == "turn_cost_budget_exceeded"
    )
    assert "$0.300000" in error.message
    assert "estimated cost basis" in error.message
    assert calls == [("m1", "p1"), ("m2", "p2")]


@pytest.mark.asyncio
async def test_agent_turn_cost_budget_combines_billed_and_unbilled_ensemble_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pricing import PriceEntry

    calls: list[tuple[str, str]] = []

    def resolve(model_id: str, provider: str = "") -> Any:
        calls.append((model_id, provider))
        assert (model_id, provider) == ("m2", "p2")
        return SimpleNamespace(
            entry=PriceEntry(input_per_m=400.0, output_per_m=0.0),
            source="test",
        )

    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        resolve,
    )
    provider = _EnsembleUsageBreakdownProvider(
        billed_cost=0.25,
        rows=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 1000,
                "output_tokens": 0,
                "billed_cost": 0.25,
            },
            {
                "provider": "p2",
                "model": "m2",
                "input_tokens": 1000,
                "output_tokens": 0,
                "billed_cost": 0.0,
            },
        ],
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="ensemble",
            model_id="aggregator",
            max_turn_cost_usd=0.6,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    error = next(
        event
        for event in events
        if event.kind == "error" and event.code == "turn_cost_budget_exceeded"
    )
    assert "$0.650000" in error.message
    assert "mixed cost basis" in error.message
    assert calls == [("m2", "p2")]


@pytest.mark.asyncio
async def test_agent_turn_cost_budget_stops_retry_after_ensemble_error_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pricing import PriceEntry

    calls: list[tuple[str, str]] = []

    def resolve(model_id: str, provider: str = "") -> Any:
        calls.append((model_id, provider))
        assert (model_id, provider) == ("m1", "p1")
        return SimpleNamespace(
            entry=PriceEntry(input_per_m=300.0, output_per_m=0.0),
            source="test",
        )

    monkeypatch.setattr(
        "opensquilla.engine.usage_accounting.resolve_model_price",
        resolve,
    )
    provider = _RetryingEnsembleErrorProvider(
        rows=[
            {
                "provider": "p1",
                "model": "m1",
                "input_tokens": 1000,
                "output_tokens": 0,
                "billed_cost": 0.0,
            }
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            provider_id="ensemble",
            model_id="aggregator",
            max_provider_retries=2,
            max_turn_cost_usd=0.25,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    error = next(
        event
        for event in events
        if event.kind == "error" and event.code == "turn_cost_budget_exceeded"
    )
    assert "$0.300000" in error.message
    assert "estimated cost basis" in error.message
    assert len(provider.calls) == 1
    assert calls == [("m1", "p1")]


def test_with_model_usage_cost_fields_prices_unbilled_cache_reads_cache_aware() -> None:
    from opensquilla.engine import agent as agent_module

    blind_row = {
        "model": "deepseek/deepseek-v4-pro-20260423",
        "input_tokens": 1000,
        "output_tokens": 0,
        "billed_cost": 0.0,
    }
    cached_row = dict(blind_row, cache_read_tokens=800)

    blind = agent_module._with_model_usage_cost_fields([blind_row])[0]
    cached = agent_module._with_model_usage_cost_fields([cached_row])[0]

    assert blind["estimate_basis"] == "cache_aware"
    assert cached["estimate_basis"] == "cache_aware"
    # (200 * 0.435 + 800 * 0.003625) / 1e6 == 0.0000899, rounded to 6dp by
    # model_usage_cost_fields.
    assert cached["cost_usd"] == pytest.approx(0.00009)
    assert cached["cost_usd"] < blind["cost_usd"]


class _BudgetCheckingProvider:
    provider_name = "openrouter"

    def __init__(self, *, proof_budget: int) -> None:
        self.proof_budget = proof_budget
        self.calls: list[list[Message]] = []
        self.proofs: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(messages)

    async def _stream(self, messages: list[Message]) -> AsyncIterator[Any]:
        payload = {"messages": [message.model_dump(mode="json") for message in messages]}
        try:
            proof = prove_provider_payload(
                payload,
                projection_adapter="openrouter",
                proof_budget=self.proof_budget,
            )
        except ProviderRequestBudgetExceeded as exc:
            self.proofs.append(exc.proof)
            yield ProviderError(
                message=json.dumps(exc.proof),
                code="provider_request_budget_exhausted",
            )
            return

        self.proofs.append(proof)
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _ProviderRaisesTimeout:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        raise TimeoutError("provider transport timeout")
        yield ProviderText(text="unreachable")

    async def list_models(self) -> list[Any]:
        return []


class _ProviderHeartbeatThenText:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderHeartbeatEvent(phase="llm_fallback", message="retrying")
        yield ProviderText(text="ok")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _ToolUseProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="slow")
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="slow",
            arguments={},
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_provider_heartbeat_reaches_agent_stream() -> None:
    agent = Agent(
        provider=_ProviderHeartbeatThenText(),
        config=AgentConfig(iteration_timeout=30.0, timeout=60.0, max_provider_retries=0),
    )

    events = [event async for event in agent.run_turn("hello")]

    heartbeat_index = _event_index(
        events,
        lambda event: (
            isinstance(event, RunHeartbeatEvent)
            and event.phase == "llm_fallback"
            and event.message == "retrying"
        ),
    )
    text_index = _event_index(
        events,
        lambda event: (
            getattr(event, "kind", None) == "text_delta" and getattr(event, "text", None) == "ok"
        ),
    )
    assert heartbeat_index < text_index


@pytest.mark.asyncio
async def test_iteration_timeout_interrupts_stalled_provider_stream() -> None:
    provider = _StallingProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(iteration_timeout=0.01, max_provider_retries=0),
    )

    events = await asyncio.wait_for(
        _collect_events(agent.run_turn("hello")),
        timeout=0.5,
    )

    error_index = _event_index(
        events,
        lambda event: isinstance(event, ErrorEvent) and event.code == "iteration_timeout",
    )
    state_index = _event_index(
        events,
        lambda event: (
            getattr(event, "kind", None) == "state_change"
            and getattr(event, "to_state", None) == AgentState.ERROR
        ),
    )
    assert state_index < error_index
    assert len(provider.calls) == 1
    assert provider.stream_closed is True
    assert not any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_iteration_timeout_does_not_interrupt_active_tool_argument_stream() -> None:
    async def write_file_tool(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="written",
        )

    # Each provider event stays comfortably inside the per-event watchdog
    # window, while the complete argument stream lasts longer than that
    # window.  This proves that active tool-argument streaming resets the
    # watchdog without depending on sub-10ms event-loop scheduling slack.
    provider = _ActiveLongToolArgumentProvider(fragment_delay=0.06)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            iteration_timeout=0.15,
            timeout=1.0,
            max_provider_retries=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="write_file",
                description="Write a file.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=write_file_tool,
    )

    events = await asyncio.wait_for(_collect_events(agent.run_turn("hello")), timeout=1.0)

    assert provider.calls
    assert any(
        getattr(event, "kind", None) == "tool_result"
        and getattr(event, "tool_name", None) == "write_file"
        and getattr(event, "result", None) == "written"
        for event in events
    )
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout" for event in events
    )


@pytest.mark.asyncio
async def test_large_tool_argument_stream_emits_progress_heartbeat() -> None:
    async def write_file_tool(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="written",
        )

    provider = _ActiveLongToolArgumentProvider(
        fragment_delay=0.0,
        content="x" * 5000,
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(iteration_timeout=1.0, timeout=2.0, max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="write_file",
                description="Write a file.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=write_file_tool,
    )

    events = await asyncio.wait_for(_collect_events(agent.run_turn("hello")), timeout=1.0)

    heartbeat_index = _event_index(
        events,
        lambda event: (
            isinstance(event, RunHeartbeatEvent)
            and event.phase == "llm_tool_arguments"
            # Pending tool identity stays private until a legal DoneEvent
            # commits the transactional tool timeline.
            and event.message == "Receiving tool arguments"
        ),
    )
    done_index = _event_index(events, lambda event: isinstance(event, DoneEvent))
    assert heartbeat_index < done_index


@pytest.mark.asyncio
async def test_iteration_timeout_caps_tool_execution() -> None:
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    never_complete = asyncio.Event()

    async def slow_tool(call: object) -> ToolResult:
        tool_started.set()
        try:
            await never_complete.wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="late",
        )

    agent = Agent(
        provider=_ToolUseProvider(),
        config=AgentConfig(
            iteration_timeout=0.1,
            timeout=5.0,
            tool_timeout=5.0,
            max_provider_retries=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="slow",
                description="Slow tool.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=slow_tool,
    )

    events = await asyncio.wait_for(_collect_events(agent.run_turn("hello")), timeout=2.0)

    assert tool_started.is_set()
    assert tool_cancelled.is_set()
    assert any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout" for event in events
    )


@pytest.mark.asyncio
async def test_provider_timeout_error_is_not_reclassified_as_iteration_timeout() -> None:
    provider = _ProviderRaisesTimeout()
    agent = Agent(
        provider=provider,
        config=AgentConfig(iteration_timeout=30.0, timeout=60.0, max_provider_retries=0),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "request_error"
        and event.failure_kind == "transport_transient"
        for event in events
    )
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "iteration_timeout" for event in events
    )
    assert "provider transport timeout" not in repr(events)


@pytest.mark.asyncio
async def test_context_overflow_noop_compaction_does_not_resend_unchanged_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop_compact(request: Any) -> CompactionResult:
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=0,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _noop_compact)
    provider = _ContextOverflowProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, ErrorEvent) and event.code == "compaction_not_smaller" for event in events
    )
    assert not any(getattr(event, "kind", None) == "compaction" for event in events)


@pytest.mark.asyncio
async def test_context_overflow_summary_only_larger_payload_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _summary_only_compact(request: Any) -> CompactionResult:
        return CompactionResult(
            summary="summary without reducing request payload",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _summary_only_compact)
    provider = _ContextOverflowProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(
        isinstance(event, ErrorEvent) and event.code == "compaction_not_smaller" for event in events
    )
    assert not any(getattr(event, "kind", None) == "compaction" for event in events)


@pytest.mark.asyncio
async def test_context_overflow_effective_compaction_allows_single_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _effective_compact(request: Any) -> CompactionResult:
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    provider = _ContextOverflowProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 5000)),
            Message(role="assistant", content="old answer " + ("a" * 5000)),
        ]
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert len(provider.calls) == 2
    assert _provider_payload_is_smaller(provider.calls[0], provider.calls[1])
    compaction_indexes = [
        index for index, event in enumerate(events) if isinstance(event, CompactionEvent)
    ]
    assert len(compaction_indexes) == 1
    first_provider_output = next(
        index
        for index, event in enumerate(events)
        if getattr(event, "kind", None) == "text_delta"
    )
    assert compaction_indexes[0] < first_provider_output
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)


@pytest.mark.asyncio
async def test_narrow_routed_window_never_durably_compacts_base_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_compaction(_request: Any) -> CompactionResult:
        raise AssertionError(
            "a one-turn routed window must not rewrite stable session history"
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _unexpected_compaction,
    )
    provider = _ContextOverflowProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=8_000,
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    history = [
        Message(role="user", content="old question " + ("q" * 5000)),
        Message(role="assistant", content="old answer " + ("a" * 5000)),
    ]
    agent.set_history(history)
    agent.bind_durable_consumer(
        provider=provider,
        model_id="stable-128k",
        context_window_tokens=128_000,
        max_output_tokens=8_000,
    )

    events = [event async for event in agent.run_turn("current request")]

    assert len(provider.calls) == 1
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert agent.history_snapshot() == history
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_too_large"
        for event in events
    )


@pytest.mark.asyncio
async def test_inline_compaction_candidate_gets_terminal_when_retry_never_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _effective_compact(request: Any) -> CompactionResult:
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    notifications: list[dict[str, Any]] = []

    def _record_notification(_session_key: str, **payload: Any) -> None:
        notifications.append(payload)

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    monkeypatch.setattr("opensquilla.engine.agent.notify_compaction", _record_notification)
    provider = _ContextOverflowProvider()
    agent = Agent(
        provider=provider,
        session_key="agent:main:test",
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 5000)),
            Message(role="assistant", content="old answer " + ("a" * 5000)),
        ]
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert len(provider.calls) == 2
    assert not any(isinstance(event, CompactionEvent) for event in events)
    terminal = [
        item
        for item in notifications
        if item.get("status")
        in {"completed", "skipped", "cancelled", "timed_out", "stale", "failed"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "failed"
    assert terminal[0]["reason"] == "rebuilt_request_not_admitted"


@pytest.mark.asyncio
async def test_inline_compaction_install_wait_obeys_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_wait = asyncio.wait
    deadline_limited_waits = 0

    async def _wake_deadline_limited_wait_early(
        futures: set[asyncio.Future[Any]],
        *,
        timeout: float | None = None,
        return_when: str = asyncio.ALL_COMPLETED,
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        nonlocal deadline_limited_waits
        if timeout is not None and timeout < 1.0:
            deadline_limited_waits += 1
            return set(), futures
        return await real_wait(
            futures,
            timeout=timeout,
            return_when=return_when,
        )

    async def _effective_compact(request: Any) -> CompactionResult:
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    notifications: list[dict[str, Any]] = []
    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    monkeypatch.setattr(
        "opensquilla.engine.agent.notify_compaction",
        lambda _session_key, **payload: notifications.append(payload),
    )
    monkeypatch.setattr(asyncio, "wait", _wake_deadline_limited_wait_early)
    provider = _HangingRetryAfterOverflowProvider()
    agent = Agent(
        provider=provider,
        session_key="agent:main:deadline",
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=1,
            iteration_timeout=5.0,
            timeout=5.0,
            compaction_total_timeout_seconds=0.5,
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 5000)),
            Message(role="assistant", content="old answer " + ("a" * 5000)),
        ]
    )

    started = asyncio.get_running_loop().time()
    events = [event async for event in agent.run_turn("x" * 4000)]
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert deadline_limited_waits == 1
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "compaction_deadline_exceeded"
        for event in events
    )
    terminal = [
        item
        for item in notifications
        if item.get("status")
        in {"completed", "skipped", "cancelled", "timed_out", "stale", "failed"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "timed_out"
    assert terminal[0]["reason"] == "compaction_deadline_exceeded"


@pytest.mark.asyncio
async def test_inline_compaction_install_deadline_stops_limiting_accepted_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _effective_compact(request: Any) -> CompactionResult:
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    provider = _TextThenDelayedSuccessAfterOverflowProvider()
    agent = Agent(
        provider=provider,
        session_key="agent:main:installed-deadline",
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=1,
            iteration_timeout=5.0,
            timeout=5.0,
            compaction_total_timeout_seconds=2.0,
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 5000)),
            Message(role="assistant", content="old answer " + ("a" * 5000)),
        ]
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert len(provider.calls) == 2
    assert sum(isinstance(event, CompactionEvent) for event in events) == 1
    assert any(
        isinstance(event, DoneEvent) and event.text == "partial ok"
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_native_overflow_after_final_admission_does_not_compact_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_compaction(_request: Any) -> CompactionResult:
        raise AssertionError("an admitted physical request must not mutate durable history")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _unexpected_compaction)
    provider = _FinalAdmissionContextOverflowProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert not any(isinstance(event, CompactionEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].code == "provider_request_too_large"


@pytest.mark.asyncio
async def test_inline_overflow_compaction_reduces_tool_heavy_structured_context() -> None:
    big_output = ("synthetic log line: lorem ipsum dolor sit amet 0123456789\n" * 700)[:40_000]
    # Leave room for the two most recent raw tool results protected by the
    # default semantic-tail policy while still forcing older rounds to compact.
    window_tokens = 30_000
    messages: list[Message] = [
        Message(role="user", content="Please analyze every log file in the workspace."),
        Message(role="assistant", content="Reading the logs now."),
    ]
    for i in range(6):
        messages.append(
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id=f"tool-{i}",
                        name="read_file",
                        input={"path": f"logs/part-{i}.log"},
                    )
                ],
            )
        )
        messages.append(
            Message(
                role="user",
                content=[ContentBlockToolResult(tool_use_id=f"tool-{i}", content=big_output)],
            )
        )
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(context_window_tokens=window_tokens, flush_enabled=False),
    )
    original_chars = session_payload_chars(messages)

    outcome = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=window_tokens + 1,
        compaction_window_tokens=window_tokens,
    )

    assert outcome is not None
    assert outcome.compacted
    assert session_payload_chars(outcome.messages) < original_chars


@pytest.mark.asyncio
async def test_inline_overflow_compaction_preserves_original_structured_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_recent_messages: list[int] = []

    async def _prefix_only_compact(request: Any) -> CompactionResult:
        protected_recent_messages.append(request.config.protected_recent_messages)
        return CompactionResult(
            summary="older context",
            kept_entries=request.entries[2:],
            removed_count=2,
            kept_start_index=2,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _prefix_only_compact)
    tool_use = Message(
        role="assistant",
        content=[ContentBlockToolUse(id="tool-live", name="read_file", input={"path": "x"})],
    )
    tool_result = Message(
        role="user",
        content=[ContentBlockToolResult(tool_use_id="tool-live", content="result")],
    )
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        tool_use,
        tool_result,
    ]
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(context_window_tokens=1000, flush_enabled=False),
    )

    outcome = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=1001,
        protected_turn_start_index=2,
    )

    assert outcome is not None and outcome.compacted
    assert protected_recent_messages == [2]
    assert outcome.messages[-2] is tool_use
    assert outcome.messages[-1] is tool_result
    assert isinstance(outcome.messages[-2].content, list)
    assert isinstance(outcome.messages[-1].content, list)


@pytest.mark.asyncio
async def test_inline_overflow_refuses_when_protected_current_turn_alone_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_compaction(_request: Any) -> CompactionResult:
        raise AssertionError("durable compaction must not run for an oversized current turn")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _unexpected_compaction)
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="x" * 4000),
    ]
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            context_window_tokens=1000,
            context_overflow_threshold=0.85,
            flush_enabled=False,
        ),
    )

    outcome = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=1001,
        protected_turn_start_index=2,
    )

    assert outcome is None
    assert agent._last_compaction_refusal_reason == "provider_recent_tail_too_large"


@pytest.mark.asyncio
async def test_inline_overflow_projects_completed_live_rounds_without_mutating_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []

    async def _summarize_completed_rounds(request: Any) -> CompactionResult:
        compact_requests.append(request)
        return CompactionResult(
            summary="completed work summary",
            kept_entries=[],
            removed_count=len(request.entries),
            kept_start_index=len(request.entries),
            chunks_processed=1,
            tokens_before=4000,
            tokens_after=20,
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _summarize_completed_rounds,
    )
    current_user = Message(role="user", content="finish the active task exactly")
    rounds: list[Message] = []
    for index in range(3):
        rounds.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id=f"live-{index}",
                            name="read_file",
                            input={"path": f"part-{index}.txt"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=f"live-{index}",
                            content="result " + ("x" * 4000),
                        )
                    ],
                ),
            ]
        )
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        current_user,
        *rounds,
    ]
    canonical_snapshot = list(messages)
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            context_window_tokens=1000,
            context_overflow_threshold=0.85,
            flush_enabled=False,
        ),
    )

    outcome = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=5000,
        protected_turn_start_index=2,
    )

    assert outcome is not None
    assert outcome.ephemeral_only is True
    assert outcome.messages[2] is current_user
    assert outcome.messages[-4:] == rounds[-4:]
    assert messages == canonical_snapshot
    assert compact_requests
    assert compact_requests[0].config.protect_semantic_tail is False
    assert all(
        entry["content"] != current_user.content
        for entry in compact_requests[0].entries
    )


@pytest.mark.asyncio
async def test_durable_and_live_turn_recovery_share_one_compaction_call_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_configs: list[Any] = []
    seen_operation_ids: list[str | None] = []
    seen_deadlines: list[float | None] = []

    async def _bounded_summary(request: Any) -> CompactionResult:
        seen_configs.append(request.config)
        seen_operation_ids.append(request.config.operation_id)
        seen_deadlines.append(request.config.deadline_at_monotonic)
        request.config.llm_calls_started += 1
        if request.forced_prefix_cut is not None:
            cut = int(request.forced_prefix_cut)
        else:
            protected = int(request.config.protected_recent_messages or 0)
            cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary=f"summary-{len(seen_configs)}",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _bounded_summary,
    )
    current_user = Message(role="user", content="finish the active task exactly")
    rounds: list[Message] = []
    for index in range(3):
        rounds.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id=f"shared-{index}",
                            name="read_file",
                            input={"path": f"part-{index}.txt"},
                        )
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id=f"shared-{index}",
                            content=f"result {index}",
                        )
                    ],
                ),
            ]
        )
    messages = [
        Message(role="user", content="old question " + ("q" * 40_000)),
        Message(role="assistant", content="old answer " + ("a" * 40_000)),
        current_user,
        *rounds,
    ]
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            context_window_tokens=8_000,
            context_overflow_threshold=0.85,
            flush_enabled=False,
        ),
    )

    durable = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=20_000,
        protected_turn_start_index=2,
        compaction_window_tokens=16_000,
        durable_consumer_overflow_proven=True,
    )

    assert durable is not None
    assert durable.ephemeral_only is False
    shared_config = durable.runtime_compaction_config
    assert shared_config is not None
    assert shared_config.llm_calls_started == 1
    assert "runtime_compaction_config" not in repr(durable)

    ephemeral = await agent._recover_live_turn_request_overflow(
        durable.messages,
        protected_turn_start_index=(
            durable.protected_turn_start_index or 0
        ),
        context_window_tokens=8_000,
        request_context_insert_index=durable.request_context_insert_index,
        runtime_context_insert_index=durable.runtime_context_insert_index,
        shared_compaction_config=shared_config,
    )

    assert ephemeral is not None
    assert ephemeral.ephemeral_only is True
    assert ephemeral.runtime_compaction_config is shared_config
    assert len(seen_configs) == 2
    assert seen_configs[0] is seen_configs[1]
    assert seen_operation_ids[0] == seen_operation_ids[1]
    assert seen_operation_ids[0] is not None
    assert seen_deadlines[0] == seen_deadlines[1]
    assert seen_deadlines[0] is not None
    assert shared_config.llm_calls_started == 2


@pytest.mark.asyncio
async def test_stable_consumer_retries_with_completed_live_round_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StableToolLoopBudgetProvider:
        provider_name = "fake"

        def __init__(self, max_message_chars: int) -> None:
            self.max_message_chars = max_message_chars
            self.calls: list[list[Message]] = []
            self.tool_rounds_emitted = 0

        def project_final_request(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
            *,
            message_limit: int | None = None,
        ) -> ProviderFinalRequestProjection:
            del tools, config
            estimated_chars = session_payload_chars(messages)
            fits_message_count = (
                None if message_limit is None else len(messages) <= message_limit
            )
            fits = (
                estimated_chars <= self.max_message_chars
                and fits_message_count is not False
            )
            proof = {
                "fits": fits,
                "estimated_chars": estimated_chars,
                "fallback_reason": (
                    None if fits else "provider_request_budget_exhausted"
                ),
            }
            return ProviderFinalRequestProjection(
                payload={"messages": [message.model_dump() for message in messages]},
                proof=proof,
                wire_message_count=len(messages),
                message_limit=message_limit,
                fits_message_count=fits_message_count,
                fits=fits,
            )

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            self.calls.append(messages)
            projection = self.project_final_request(messages, tools, config)
            return self._stream(projection)

        async def _stream(
            self,
            projection: ProviderFinalRequestProjection,
        ) -> AsyncIterator[Any]:
            if not projection.fits:
                yield ProviderError(
                    message=json.dumps(projection.proof),
                    code="provider_request_budget_exhausted",
                )
                return
            if self.tool_rounds_emitted < 3:
                self.tool_rounds_emitted += 1
                tool_id = f"live-round-{self.tool_rounds_emitted}"
                yield ProviderToolUseStart(
                    tool_use_id=tool_id,
                    tool_name="read_file",
                )
                yield ProviderToolUseEnd(
                    tool_use_id=tool_id,
                    tool_name="read_file",
                    arguments={"path": f"part-{self.tool_rounds_emitted}.txt"},
                )
                yield ProviderDone(
                    stop_reason="tool_calls",
                    input_tokens=1,
                    output_tokens=1,
                )
                return
            yield ProviderText(text="finished after live-turn recovery")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

        async def list_models(self) -> list[Any]:
            return []

    compact_requests: list[Any] = []

    async def _compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        if request.forced_prefix_cut is not None:
            cut = int(request.forced_prefix_cut)
            summary = "completed live tool round"
        else:
            protected = int(request.config.protected_recent_messages or 0)
            cut = max(0, len(request.entries) - protected)
            summary = "older durable history"
        return CompactionResult(
            summary=summary,
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
            tokens_before=10_001,
            tokens_after=100,
        )

    async def _tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="result " + ("x" * 6_000),
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _compact)
    provider = _StableToolLoopBudgetProvider(max_message_chars=17_000)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=10_000,
            context_overflow_threshold=0.85,
            max_overflow_retries=1,
            max_provider_retries=0,
            flush_enabled=False,
        ),
        tool_handler=_tool,
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 2_000)),
            Message(role="assistant", content="old answer " + ("a" * 2_000)),
        ]
    )

    events = [event async for event in agent.run_turn("finish the active task")]

    assert any(
        isinstance(event, DoneEvent)
        and event.text == "finished after live-turn recovery"
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, WarningEvent)
        and event.code == "context_auto_compaction_retry"
        for event in events
    )
    assert len([event for event in events if isinstance(event, CompactionEvent)]) == 1
    assert len(compact_requests) == 2
    assert compact_requests[0].forced_prefix_cut is None
    assert compact_requests[1].forced_prefix_cut is not None
    history = agent.history_snapshot()
    assert sum(
        1
        for message in history
        if isinstance(message.content, list)
        and any(isinstance(block, ContentBlockToolResult) for block in message.content)
    ) == 3


@pytest.mark.asyncio
async def test_live_turn_recovery_uses_stable_consumer_input_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExactBudgetToolLoopProvider:
        provider_name = "fake"
        final_request_admission_guaranteed = True

        def __init__(self) -> None:
            self.calls: list[list[Message]] = []
            self.tool_rounds_emitted = 0

        def project_final_request(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
            *,
            message_limit: int | None = None,
        ) -> ProviderFinalRequestProjection:
            del tools, config
            estimated_chars = session_payload_chars(messages)
            estimated_tokens = max(1, (estimated_chars + 3) // 4)
            effective_char_budget = 11_000
            effective_token_budget = effective_char_budget // 4
            fits_message_count = (
                None if message_limit is None else len(messages) <= message_limit
            )
            fits = (
                estimated_chars <= effective_char_budget
                and estimated_tokens <= effective_token_budget
                and fits_message_count is not False
            )
            proof = {
                "fits": fits,
                "estimated_chars": estimated_chars,
                "estimated_tokens": estimated_tokens,
                "effective_proof_budget": effective_char_budget,
                "effective_proof_token_budget": effective_token_budget,
                "fallback_reason": (
                    None if fits else "provider_request_budget_exhausted"
                ),
            }
            return ProviderFinalRequestProjection(
                payload={"messages": [message.model_dump() for message in messages]},
                proof=proof,
                wire_message_count=len(messages),
                message_limit=message_limit,
                fits_message_count=fits_message_count,
                fits=fits,
            )

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            self.calls.append(messages)
            projection = self.project_final_request(messages, tools, config)
            return self._stream(projection)

        async def _stream(
            self,
            projection: ProviderFinalRequestProjection,
        ) -> AsyncIterator[Any]:
            if self.tool_rounds_emitted < 3:
                # Model a provider adapter whose request-only reducer can admit
                # the first three physical tool calls even though its raw,
                # unshaped projection becomes oversized after round two. Once
                # all three protected results exist, the adapter reports the
                # hard budget failure that live-turn compaction must recover.
                self.tool_rounds_emitted += 1
                tool_id = f"exact-budget-{self.tool_rounds_emitted}"
                yield ProviderToolUseStart(
                    tool_use_id=tool_id,
                    tool_name="read_file",
                )
                yield ProviderToolUseEnd(
                    tool_use_id=tool_id,
                    tool_name="read_file",
                    arguments={"path": f"part-{self.tool_rounds_emitted}.txt"},
                )
                yield ProviderDone(
                    stop_reason="tool_calls",
                    input_tokens=1,
                    output_tokens=1,
                )
                return
            if not projection.fits:
                yield ProviderError(
                    message=json.dumps(projection.proof),
                    code="provider_request_budget_exhausted",
                )
                return
            yield ProviderText(text="finished after exact-budget recovery")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

        async def list_models(self) -> list[Any]:
            return []

    compact_requests: list[Any] = []

    async def _compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        assert request.forced_prefix_cut is not None
        cut = int(request.forced_prefix_cut)
        return CompactionResult(
            summary="first completed tool round",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
            tokens_before=5_000,
            tokens_after=100,
        )

    async def _tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="result " + ("x" * 6_000),
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _compact)
    provider = _ExactBudgetToolLoopProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=10_000,
            context_overflow_threshold=0.85,
            max_overflow_retries=1,
            max_provider_retries=0,
            flush_enabled=False,
        ),
        tool_handler=_tool,
    )

    events = [event async for event in agent.run_turn("finish all three reads")]

    assert any(
        isinstance(event, DoneEvent)
        and event.text == "finished after exact-budget recovery"
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert len(provider.calls) == 5
    assert len(compact_requests) == 2
    assert all(request.context_window_tokens == 2_750 for request in compact_requests)
    assert all(request.context_window_chars == 11_000 for request in compact_requests)
    assert all(request.forced_prefix_cut is not None for request in compact_requests)


@pytest.mark.asyncio
async def test_inline_overflow_rejects_compactor_cut_through_protected_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _violating_compaction(request: Any) -> CompactionResult:
        return CompactionResult(
            summary="unsafe summary",
            kept_entries=[],
            removed_count=len(request.entries),
            kept_start_index=len(request.entries),
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _violating_compaction)
    messages = [
        Message(role="user", content="old question"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="current question"),
    ]
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            context_window_tokens=1000,
            context_overflow_threshold=0.85,
            flush_enabled=False,
        ),
    )

    outcome = await agent._check_context_overflow(
        messages,
        estimated_context_tokens=1001,
        protected_turn_start_index=2,
    )

    assert outcome is None
    assert agent._last_compaction_refusal_reason == "provider_recent_tail_too_large"


@pytest.mark.asyncio
async def test_within_budget_skip_on_string_only_history_is_not_reported_as_compacted() -> None:
    agent = Agent(
        provider=_ContextOverflowProvider(),
        config=AgentConfig(
            context_window_tokens=1000,
            context_overflow_threshold=0.85,
            flush_enabled=False,
        ),
    )
    messages = [
        Message(role="user", content="my project is called Zephyr"),
        Message(role="assistant", content="Understood, the project is Zephyr."),
        Message(role="user", content="tell me a short story about it"),
    ]

    outcome = await agent._check_context_overflow(messages, estimated_context_tokens=900)

    assert outcome is not None
    assert outcome.removed_count == 0
    assert outcome.summary == ""
    assert outcome.compacted is False


@pytest.mark.asyncio
async def test_inline_overflow_uses_live_context_not_cumulative_provider_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.agent as agent_module

    provider = _HighUsageToolLoopProvider(tool_rounds=3, input_tokens_per_call=4000)
    flush_calls: list[int] = []
    compact_requests: list[Any] = []

    async def _tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    async def _flush(_plan: Any, flush_messages: list[Message]) -> Any:
        flush_calls.append(len(flush_messages))
        return SimpleNamespace(
            mode="llm",
            indexed_chunk_count=1,
            integrity_status="ok",
            output_coverage_status="ok",
            invalid_candidate_count=0,
            candidate_missing_ids=[],
            obligation_status="ok",
            obligation_missing_ids=[],
        )

    async def _compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=0,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=20_000,
            context_overflow_threshold=0.5,
            flush_enabled=True,
            flush_pre_compaction=True,
            flush_timeout_seconds=0.01,
            max_iterations=10,
        ),
        tool_handler=_tool,
    )
    monkeypatch.setattr(agent, "_run_flush", _flush)
    monkeypatch.setattr(agent_module, "compact_context", _compact)

    events = [event async for event in agent.run_turn("read the files one by one")]

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.text == "done"
    assert done.input_tokens == 16_000
    assert len(provider.calls) == 4
    assert flush_calls == []
    assert compact_requests == []


@pytest.mark.asyncio
async def test_successful_large_request_surface_does_not_compact_durable_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.agent as agent_module

    provider = _HighUsageToolLoopProvider(tool_rounds=0, input_tokens_per_call=1)
    large_tool = ToolDefinition(
        name="large_context_tool",
        description="large live request surface " + ("z" * 6000),
        input_schema=ToolInputSchema(),
    )
    flush_calls: list[int] = []
    compact_requests: list[Any] = []

    async def _flush(_plan: Any, flush_messages: list[Message]) -> Any:
        flush_calls.append(len(flush_messages))
        return SimpleNamespace(
            mode="llm",
            indexed_chunk_count=1,
            integrity_status="ok",
            output_coverage_status="ok",
            invalid_candidate_count=0,
            candidate_missing_ids=[],
            obligation_status="ok",
            obligation_missing_ids=[],
        )

    async def _compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=0,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=3000,
            context_overflow_threshold=0.5,
            flush_enabled=True,
            flush_pre_compaction=True,
            flush_timeout_seconds=0.01,
            system_prompt="live request system context " + ("s" * 2000),
        ),
        tool_definitions=[large_tool],
    )
    monkeypatch.setattr(agent, "_run_flush", _flush)
    monkeypatch.setattr(agent_module, "compact_context", _compact)

    events = [event async for event in agent.run_turn("hello")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(provider.calls) == 1
    assert flush_calls == []
    assert compact_requests == []


@pytest.mark.asyncio
async def test_inline_overflow_flush_enabled_without_trigger_skips_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.agent as agent_module

    provider = _HighUsageToolLoopProvider(tool_rounds=0, input_tokens_per_call=1)
    flush_calls: list[int] = []
    compact_requests: list[Any] = []

    async def _flush(_plan: Any, flush_messages: list[Message]) -> Any:
        flush_calls.append(len(flush_messages))

    async def _compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=0,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=3000,
            context_overflow_threshold=0.5,
            flush_enabled=True,
            flush_pre_compaction=False,
            system_prompt="live request system context " + ("s" * 2000),
        ),
        tool_definitions=[
            ToolDefinition(
                name="large_context_tool",
                description="large live request surface " + ("z" * 6000),
                input_schema=ToolInputSchema(),
            )
        ],
    )
    monkeypatch.setattr(agent, "_run_flush", _flush)
    monkeypatch.setattr(agent_module, "compact_context", _compact)

    events = [event async for event in agent.run_turn("hello")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert flush_calls == []
    assert compact_requests == []


@pytest.mark.asyncio
async def test_provider_request_budget_exhausted_does_not_mutate_durable_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compaction_events: list[tuple[str, dict[str, Any]]] = []

    async def _unexpected_compact(_request: Any) -> CompactionResult:
        raise AssertionError("request-envelope pressure must not compact durable history")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _unexpected_compact)
    monkeypatch.setattr(
        "opensquilla.engine.agent.notify_compaction",
        lambda session_key, **payload: compaction_events.append((session_key, payload)),
    )
    provider = _ProviderRequestBudgetExceededProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
        session_key="agent:main:budget",
    )

    events = [event async for event in agent.run_turn("x" * 4000)]
    warning_codes = [event.code for event in events if isinstance(event, WarningEvent)]

    assert len(provider.calls) == 1
    assert warning_codes == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].code == "provider_request_too_large"
    assert compaction_events == []


@pytest.mark.asyncio
async def test_provider_request_budget_does_not_become_a_history_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compaction_windows: list[int] = []

    async def _effective_compact(request: Any) -> CompactionResult:
        compaction_windows.append(request.context_window_tokens)
        return CompactionResult(
            summary="short summary",
            kept_entries=[],
            removed_count=len(request.entries),
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    provider = _ProviderRequestBudgetExceededProvider(
        success_after=1,
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "estimated_chars": 109_055,
            "estimated_tokens": 27_263,
            "proof_budget": 96_000,
            "recent_tail_too_large": True,
        },
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert compaction_windows == []
    assert len(provider.calls) == 1
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].code == "provider_request_too_large"


@pytest.mark.asyncio
async def test_provider_request_budget_failure_is_not_retried_via_history_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compaction_windows: list[int] = []

    async def _effective_compact(request: Any) -> CompactionResult:
        compaction_windows.append(request.context_window_tokens)
        return CompactionResult(
            summary="short summary",
            kept_entries=[],
            removed_count=len(request.entries),
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    provider = _BudgetCheckingProvider(proof_budget=2_500)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert [proof["fits"] for proof in provider.proofs] == [False]
    assert compaction_windows == []
    assert len(provider.calls) == 1
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].code == "provider_request_too_large"


@pytest.mark.asyncio
async def test_provider_budget_effective_cap_remains_request_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compaction_windows: list[int] = []

    async def _record_compaction_window(request: Any) -> CompactionResult:
        compaction_windows.append(request.context_window_tokens)
        return CompactionResult(
            summary="short summary",
            kept_entries=[],
            removed_count=len(request.entries),
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _record_compaction_window)
    provider = _ProviderRequestBudgetExceededProvider(
        success_after=1,
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "estimated_chars": 100_000,
            "estimated_tokens": 25_000,
            "proof_budget": 96_000,
            "raw_proof_budget": 96_000,
            "effective_proof_budget": 86_400,
            "proof_headroom_chars": 9_600,
            "recent_tail_too_large": False,
        },
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert compaction_windows == []
    assert len(provider.calls) == 1
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors[-1].code == "provider_request_too_large"


@pytest.mark.asyncio
async def test_equal_window_routed_cap_does_not_compact_when_stable_consumer_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_compact(_request: Any) -> CompactionResult:
        raise AssertionError("route-only request pressure must not compact durable history")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _unexpected_compact)
    routed = _FinalProofBudgetProvider()
    stable = _FinalProofBudgetProvider()
    agent = Agent(
        provider=routed,
        config=AgentConfig(
            context_window_tokens=8_000,
            max_tokens=4_096,
            provider_request_proof_max_chars=4_000,
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    history = [
        Message(role="user", content="old question " + ("q" * 3_000)),
        Message(role="assistant", content="old answer " + ("a" * 3_000)),
    ]
    agent.set_history(history)
    agent.bind_durable_consumer(
        provider=stable,
        model_id="stable-same-window",
        context_window_tokens=8_000,
        max_output_tokens=512,
        provider_request_proof_max_chars=20_000,
    )

    events = [event async for event in agent.run_turn("current request stays exact")]

    assert len(routed.calls) == 1
    assert stable.projected_configs
    assert stable.projected_configs[-1].max_tokens == 512
    assert stable.projected_configs[-1].provider_request_max_chars == 20_000
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert agent.history_snapshot() == history
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_too_large"
        for event in events
    )


@pytest.mark.asyncio
async def test_equal_window_stable_overflow_still_allows_durable_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []

    async def _effective_compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short stable checkpoint",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    routed = _FinalProofBudgetProvider()
    stable = _FinalProofBudgetProvider()
    agent = Agent(
        provider=routed,
        config=AgentConfig(
            context_window_tokens=8_000,
            max_tokens=4_096,
            provider_request_proof_max_chars=4_000,
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 3_000)),
            Message(role="assistant", content="old answer " + ("a" * 3_000)),
        ]
    )
    agent.bind_durable_consumer(
        provider=stable,
        model_id="stable-same-window",
        context_window_tokens=8_000,
        max_output_tokens=512,
        provider_request_proof_max_chars=4_000,
    )

    events = [event async for event in agent.run_turn("current request stays exact")]

    assert len(compact_requests) == 1
    assert len(routed.calls) == 2
    assert stable.projected_configs
    assert any(isinstance(event, CompactionEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_narrow_route_uses_stable_window_when_stable_consumer_also_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []

    async def _effective_compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short stable checkpoint",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _effective_compact)
    routed = _FinalProofBudgetProvider()
    stable = _FinalProofBudgetProvider()
    agent = Agent(
        provider=routed,
        config=AgentConfig(
            context_window_tokens=8_000,
            max_tokens=4_096,
            provider_request_proof_max_chars=4_000,
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 30_000)),
            Message(role="assistant", content="old answer " + ("a" * 30_000)),
        ]
    )
    agent.bind_durable_consumer(
        provider=stable,
        model_id="stable-16k",
        context_window_tokens=16_000,
        max_output_tokens=512,
        provider_request_proof_max_chars=40_000,
    )

    events = [event async for event in agent.run_turn("current request stays exact")]

    assert len(compact_requests) == 1
    assert compact_requests[0].context_window_tokens == 16_000
    assert len(routed.calls) == 2
    assert len(stable.projected_configs) >= 2
    assert all(config.max_tokens == 512 for config in stable.projected_configs)
    assert all(
        config.provider_request_max_chars == 40_000
        for config in stable.projected_configs
    )
    assert any(isinstance(event, CompactionEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


@pytest.mark.asyncio
async def test_narrow_route_cannot_force_stable_compaction_to_its_request_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []

    async def _stable_only_compact(request: Any) -> CompactionResult:
        compact_requests.append(request)
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="s" * 10_000,
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _stable_only_compact,
    )
    routed = _FinalProofBudgetProvider()
    stable = _FinalProofBudgetProvider()
    agent = Agent(
        provider=routed,
        config=AgentConfig(
            context_window_tokens=8_000,
            max_tokens=4_096,
            provider_request_proof_max_chars=4_000,
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    history = [
        Message(role="user", content="old question " + ("q" * 30_000)),
        Message(role="assistant", content="old answer " + ("a" * 30_000)),
    ]
    agent.set_history(history)
    agent.bind_durable_consumer(
        provider=stable,
        model_id="stable-16k",
        context_window_tokens=16_000,
        max_output_tokens=512,
        provider_request_proof_max_chars=40_000,
    )

    events = [event async for event in agent.run_turn("current request stays exact")]

    assert len(compact_requests) == 1
    assert compact_requests[0].context_window_tokens == 16_000
    assert len(stable.projected_configs) >= 2
    assert len(routed.calls) == 1
    compaction_events = [
        event for event in events if isinstance(event, CompactionEvent)
    ]
    assert len(compaction_events) == 1
    assert compaction_events[0].summary == "s" * 10_000
    assert compaction_events[0].removed_count == 2
    assert agent.history_snapshot() == history
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "provider_request_too_large"
        for event in events
    )


@pytest.mark.asyncio
async def test_mixed_pressure_does_not_install_candidate_that_stable_consumer_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_requests: list[Any] = []

    async def _still_too_large_for_stable(request: Any) -> CompactionResult:
        compact_requests.append(request)
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="s" * 50_000,
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _still_too_large_for_stable,
    )
    routed = _FinalProofBudgetProvider()
    stable = _FinalProofBudgetProvider()
    agent = Agent(
        provider=routed,
        config=AgentConfig(
            context_window_tokens=8_000,
            max_tokens=4_096,
            provider_request_proof_max_chars=4_000,
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )
    history = [
        Message(role="user", content="old question " + ("q" * 30_000)),
        Message(role="assistant", content="old answer " + ("a" * 30_000)),
    ]
    agent.set_history(history)
    agent.bind_durable_consumer(
        provider=stable,
        model_id="stable-16k",
        context_window_tokens=16_000,
        max_output_tokens=512,
        provider_request_proof_max_chars=40_000,
    )

    events = [event async for event in agent.run_turn("current request stays exact")]

    assert len(compact_requests) == 1
    assert compact_requests[0].context_window_tokens == 16_000
    assert len(stable.projected_configs) >= 2
    assert len(routed.calls) == 1
    assert not any(isinstance(event, CompactionEvent) for event in events)
    assert agent.history_snapshot() == history
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "compaction_exhausted"
        for event in events
    )


@pytest.mark.asyncio
async def test_provider_request_budget_recent_tail_reason_survives_noop_compaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_compact(_request: Any) -> CompactionResult:
        raise AssertionError("request-envelope pressure must not compact durable history")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _unexpected_compact)
    provider = _ProviderRequestBudgetExceededProvider(
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "estimated_chars": 109_055,
            "estimated_tokens": 27_263,
            "proof_budget": 96_000,
            "recent_tail_too_large": True,
        },
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=1_048_576,
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("x" * 4000)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]

    assert len(provider.calls) == 1
    assert errors[-1].code == "provider_request_too_large"
    assert RAW_CURRENT_TURN_OVERFLOW_MESSAGE not in errors[-1].message
    assert not any(
        isinstance(event, ErrorEvent) and event.code == "compaction_not_smaller" for event in events
    )


@pytest.mark.asyncio
async def test_provider_request_budget_recent_tail_exhaustion_is_reported_as_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_compact(_request: Any) -> CompactionResult:
        raise AssertionError("request-envelope pressure must not compact durable history")

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", _unexpected_compact)
    provider = _ProviderRequestBudgetExceededProvider(
        proof={
            "fallback_reason": "provider_request_budget_exhausted",
            "recent_tail_too_large": True,
            "estimated_chars": 100_000,
            "proof_budget": 96_000,
        }
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=1,
            flush_enabled=False,
        ),
    )

    events = [event async for event in agent.run_turn("x" * 4000)]
    errors = [event for event in events if isinstance(event, ErrorEvent)]

    assert len(provider.calls) == 1
    assert errors[-1].code == "provider_request_too_large"
    assert "current turn" not in errors[-1].message.lower()
    assert RAW_CURRENT_TURN_OVERFLOW_MESSAGE not in errors[-1].message


@pytest.mark.asyncio
async def test_context_overflow_degraded_flush_still_runs_live_compaction_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_called = False

    async def _compact_runs_after_degraded_flush(request: Any) -> CompactionResult:
        nonlocal compact_called
        compact_called = True
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _compact_runs_after_degraded_flush,
    )
    provider = _ContextOverflowProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0, max_overflow_retries=2),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 5000)),
            Message(role="assistant", content="old answer " + ("a" * 5000)),
        ]
    )

    events = [event async for event in agent.run_turn("x" * 4000)]

    assert compact_called is True
    assert len(provider.calls) == 2
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and event.code in {"compaction_refused_memory_flush", "compaction_refused_flush_timeout"}
        for event in events
    )


@pytest.mark.asyncio
async def test_context_overflow_flush_timeout_records_backoff_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _compact_runs_after_flush_timeout(request: Any) -> CompactionResult:
        protected = int(request.config.protected_recent_messages or 0)
        cut = max(0, len(request.entries) - protected)
        return CompactionResult(
            summary="short summary",
            kept_entries=request.entries[cut:],
            removed_count=cut,
            kept_start_index=cut,
            chunks_processed=1,
        )

    monkeypatch.setattr(
        "opensquilla.engine.agent.compact_context",
        _compact_runs_after_flush_timeout,
    )
    provider = _ContextOverflowProvider(success_after=1)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            max_overflow_retries=2,
            flush_enabled=True,
            flush_pre_compaction=True,
            flush_timeout_seconds=0.01,
            flush_backoff_initial_seconds=10.0,
        ),
    )
    agent.set_history(
        [
            Message(role="user", content="old question " + ("q" * 5000)),
            Message(role="assistant", content="old answer " + ("a" * 5000)),
        ]
    )

    async def slow_flush(_plan: Any, _messages: Any) -> None:
        await asyncio.sleep(1.0)

    monkeypatch.setattr(agent, "_run_flush", slow_flush)
    try:
        events = [event async for event in agent.run_turn("x" * 4000)]
    finally:
        task = agent._active_flush_task
        if task is not None and not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(provider.calls) == 2
    assert agent._flush_backoff_seconds == 10.0
    assert any(event.kind == "done" and getattr(event, "text", "") == "ok" for event in events)
    assert not any(
        isinstance(event, ErrorEvent)
        and event.code in {"compaction_refused_memory_flush", "compaction_refused_flush_timeout"}
        for event in events
    )


async def _collect_events(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


def _event_index(events: list[Any], predicate: Any) -> int:
    return next(index for index, event in enumerate(events) if predicate(event))


def _provider_payload_is_smaller(before: list[Message], after: list[Message]) -> bool:
    return len(after) < len(before) or session_payload_chars(after) < session_payload_chars(before)
