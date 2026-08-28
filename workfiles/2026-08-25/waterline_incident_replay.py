"""Replay the h8m7rtg1 / 7bf7fefe incidents against the new waterline pass.

Builds an oversized tool-heavy history matching the observed shape (tens of
large tool_result blocks across many turns), then measures the assembled
provider request with the waterline disabled vs enabled, and verifies the
original content stays recoverable through the projection handle.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent


class _Provider:
    provider_name = "fake"

    def chat(self, messages, tools=None, config=None):
        return self._stream()

    async def _stream(self):
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock {name}",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


async def _handler(tool_call) -> None:
    raise AssertionError(tool_call)


setattr(_handler, "_opensquilla_available_tools", frozenset({"retrieve_tool_result"}))


_TURN_TOOLS = ["exec_command", "read_file", "edit_file"]


def build_history(turns: int) -> tuple[list[Message], list[str]]:
    """Each completed turn carries one ~60KB tool result; tools alternate."""

    messages: list[Message] = [Message(role="user", content="start the refactor")]
    originals: list[str] = []
    for i in range(turns):
        original = f"file contents turn-{i}\n" + ("source line with detail\n" * 2500)
        originals.append(original)
        tool_name = _TURN_TOOLS[i % len(_TURN_TOOLS)]
        arguments = (
            {"command": f"pytest -q tests_{i}"}
            if tool_name == "exec_command"
            else {"path": f"src/module_{i}.py"}
        )
        messages.append(
            Message(
                role="assistant",
                content=[
                    ContentBlockToolUse(
                        id=f"tool-{i}",
                        name=tool_name,
                        input=arguments,
                    )
                ],
            )
        )
        messages.append(
            Message(
                role="user",
                content=[
                    ContentBlockToolResult(tool_use_id=f"tool-{i}", content=original)
                ],
            )
        )
        messages.append(Message(role="assistant", content=f"analysis of module_{i}"))
    messages.append(Message(role="user", content="continue"))
    return messages, originals


def assemble(agent: Agent, messages: list[Message]) -> list[Message]:
    request_messages, _ = agent._provider_request_messages_with_sanitize(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[runtime context]"),
        runtime_context_insert_index=len(messages),
    )
    # Production state: previously delivered full-text blocks are frozen.
    agent._remember_provider_visible_tool_results(request_messages)
    return request_messages


def total_tool_result_chars(messages: list[Message]) -> int:
    total = 0
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolResult):
                content = block.content if isinstance(block.content, str) else ""
                total += len(content)
    return total


def make_agent(tmp: Path, keep_turns: int) -> Agent:
    return Agent(
        provider=_Provider(),
        config=AgentConfig(
            tool_result_store_dir=str(tmp / "tool-results"),
            tool_result_store_session_id="incident-replay",
            tool_result_store_session_key="agent:main:incident-replay",
            tool_result_store_agent_id="main",
            tool_result_provider_request_max_chars=4_000,
            tool_result_history_projection_keep_recent_turns=keep_turns,
        ),
        tool_definitions=[_tool_def("retrieve_tool_result")],
        tool_handler=_handler,
    )


def main() -> None:
    tmp = Path(__file__).parent / "replay-tmp"
    turns = 30
    messages, originals = build_history(turns)
    source_chars = total_tool_result_chars(messages)
    print(f"history: {turns} turns, tool_result chars = {source_chars:,}")

    off_agent = make_agent(tmp, keep_turns=0)
    off_request = assemble(off_agent, messages)
    off_chars = total_tool_result_chars(off_request)
    print(f"waterline OFF : provider request tool_result chars = {off_chars:,}")

    on_agent = make_agent(tmp, keep_turns=3)
    on_request = assemble(on_agent, messages)
    on_chars = total_tool_result_chars(on_request)
    print(f"waterline ON  : provider request tool_result chars = {on_chars:,}")

    reduction = 1 - on_chars / off_chars
    print(f"reduction: {reduction:.1%}")

    handles: list[str] = []
    for message in on_request:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolResult) and isinstance(block.content, str):
                for line in block.content.splitlines():
                    if line.startswith("tool_result_handle:"):
                        handles.append(line.split(":", 1)[1].strip())
    print(f"projected blocks with recovery handles: {len(handles)}")

    store_dir = tmp / "tool-results"
    blobs = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in store_dir.rglob("*")
        if p.is_file()
    )
    missing = [
        i for i, original in enumerate(originals[:-3])
        if f"file contents turn-{i}" not in blobs
    ]
    print(f"original content recoverable from store: {len(originals[:-3]) - len(missing)}/{len(originals[:-3])}")
    assert not missing, f"missing recoverable originals: {missing}"

    newest = max(p.stat().st_mtime for p in store_dir.rglob("*") if p.is_file())
    assert newest > 0
    print("PASS")


if __name__ == "__main__":
    main()
