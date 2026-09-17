"""Executor for ``tool_call`` meta-steps.

Direct tool invocation — bypasses the LLM entirely. ``step.tool_args``
are Jinja-rendered against ``inputs`` + ``outputs`` then handed to the
injected ``tool_invoker``. When the tool_invoker isn't wired (degraded
mode) the call falls back to a one-shot sub-Agent prompt that imitates
the tool-call contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from opensquilla.engine.types import AgentEvent
from opensquilla.skills.meta.executors.llm_classify import _drain_agent_runner
from opensquilla.skills.meta.templating import render_with_args
from opensquilla.skills.meta.types import MetaStep


class ToolInvocationResult(str):
    """String-compatible direct-tool output carrying canonical artifacts."""

    artifacts: tuple[dict[str, Any], ...]

    def __new__(
        cls,
        text: str,
        artifacts: tuple[dict[str, Any], ...] = (),
    ) -> ToolInvocationResult:
        instance = super().__new__(cls, text)
        instance.artifacts = artifacts
        return instance

    @property
    def text(self) -> str:
        return str(self)


async def run_tool_call_step(
    step: MetaStep,
    inputs: dict[str, Any],
    outputs: dict[str, str],
    *,
    tool_invoker: Callable[[str, dict[str, Any]], Awaitable[str]] | None,
    agent_runner: Callable[[str, str], AsyncIterator[AgentEvent]],
) -> ToolInvocationResult:
    """Direct tool invocation — bypasses the LLM entirely.

    ``step.tool_args`` are Jinja-rendered against ``inputs`` + ``outputs``
    then passed to ``tool_invoker``. Falls back to the agent runner with
    a one-shot tool-call instruction when ``tool_invoker`` is None.
    """

    # Defence in depth: the parser already cross-validates this, but
    # repeat the check at runtime so a programmatically constructed
    # ``MetaStep`` cannot bypass per-step tool gating.
    if step.tool_allowlist and step.tool not in step.tool_allowlist:
        raise RuntimeError(
            f"step {step.id!r}: tool {step.tool!r} not in "
            f"step.tool_allowlist {list(step.tool_allowlist)!r}",
        )

    rendered_args = render_with_args(step.tool_args, inputs=inputs, outputs=outputs)

    if tool_invoker is None:
        import json as _json

        args_blob = _json.dumps(rendered_args, ensure_ascii=False, default=str)
        system_prompt = (
            f"Invoke the {step.tool!r} tool exactly once with the JSON "
            "arguments provided. Do not call any other tools. After the tool "
            "returns, reply with its result as plain text."
        )
        user_message = f"Tool: {step.tool}\nArguments: {args_blob}"
        return ToolInvocationResult(
            await _drain_agent_runner(
                system_prompt,
                user_message,
                agent_runner=agent_runner,
            ),
        )

    result = await tool_invoker(step.tool, rendered_args)
    if isinstance(result, ToolInvocationResult):
        return result
    return ToolInvocationResult(str(result))


__all__ = ["ToolInvocationResult", "run_tool_call_step"]
