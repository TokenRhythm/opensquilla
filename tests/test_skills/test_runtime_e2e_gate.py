from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from opensquilla.engine.types import AgentConfig, DoneEvent
from opensquilla.git_runtime import (
    GitCapability,
    GitCapabilityState,
    GitRunResult,
    GitRunState,
)
from opensquilla.skills.creator import runtime_e2e as runtime_e2e_module
from opensquilla.skills.creator.runtime_e2e import (
    make_runtime_e2e_context,
    run_runtime_e2e_gate,
)
from opensquilla.tool_boundary import ToolCall

SKILL_MD = """---
name: synth-test-pipeline
description: "Sample synthetic pipeline for runtime E2E tests"
kind: meta
meta_priority: 50
triggers:
  - "synth test trigger"
provenance:
  origin: opensquilla-user
composition:
  steps:
    - id: a
      skill: summarize
      with:
        task: "{{ inputs.user_message | xml_escape | truncate(512) }}"
---
"""


def _git_result(state: GitRunState) -> GitRunResult:
    available = state is not GitRunState.UNAVAILABLE
    capability = GitCapability(
        state=(
            GitCapabilityState.AVAILABLE
            if available
            else GitCapabilityState.UNAVAILABLE
        ),
        executable=Path("/test/bin/git") if available else None,
        source="test" if available else None,
        reason=None if available else "git_not_found",
    )
    return GitRunResult(
        state=state,
        returncode=0 if state is GitRunState.OK else None,
        stdout=b"",
        stderr=b"" if available else b"git_not_found",
        capability=capability,
    )


def test_runtime_e2e_workspace_uses_safe_git_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_git(
        args: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> GitRunResult:
        assert cwd == tmp_path / "runtime-workspace"
        assert timeout == 10.0
        calls.append(args)
        return _git_result(GitRunState.OK)

    monkeypatch.setattr(runtime_e2e_module, "run_git", fake_run_git)

    workspace = runtime_e2e_module._prepare_runtime_workspace(tmp_path, None)

    assert workspace == tmp_path / "runtime-workspace"
    assert calls == [
        ("init",),
        ("config", "user.email", "runtime-e2e@example.test"),
        ("config", "user.name", "Runtime E2E"),
        ("add", "README.md"),
        ("commit", "-m", "baseline"),
    ]
    assert all(call[0] != "git" for call in calls)


@pytest.mark.asyncio
async def test_runtime_e2e_context_returns_meta_error_when_git_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = GitCapability(
        state=GitCapabilityState.UNAVAILABLE,
        reason="git_not_found",
    )

    def unavailable_capability(*args: object, **kwargs: object) -> GitCapability:
        return capability

    def forbidden_subprocess(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("unavailable Git must not launch a subprocess")

    monkeypatch.setattr(
        "opensquilla.git_runtime.resolve_git_capability",
        unavailable_capability,
    )
    monkeypatch.setattr(
        "opensquilla.git_runtime.subprocess.run",
        forbidden_subprocess,
    )
    ctx = make_runtime_e2e_context(
        provider=object(),
        base_config=AgentConfig(model_id="frontier/highest"),
        skill_loader=object(),
        tool_definitions=[],
        tool_handler=None,
        agent_factory=None,
        llm_chat=None,
        tool_invoker=None,
    )

    result = await ctx["runner"](
        route="meta",
        prompt="please use synth test trigger",
        skill_md=SKILL_MD,
        baseline_model="frontier/highest",
    )

    assert result == {
        "route": "meta",
        "text": "",
        "ok": False,
        "error": "runtime E2E workspace requires Git, but it is unavailable (git_not_found)",
    }


@pytest.mark.asyncio
async def test_runtime_e2e_gate_runs_meta_and_no_meta_baseline() -> None:
    calls: list[tuple[str, str, str]] = []

    async def runner(*, route: str, prompt: str, skill_md: str, baseline_model: str) -> dict:
        calls.append((route, prompt, baseline_model))
        return {
            "text": (
                "meta answer with concrete summary"
                if route == "meta"
                else "baseline generic answer"
            ),
            "model": baseline_model if route == "baseline" else "meta-route",
        }

    async def judge(*, prompt: str, meta: dict, baseline: dict) -> dict:
        assert "synth test trigger" in prompt
        assert meta["text"].startswith("meta answer")
        assert baseline["text"].startswith("baseline")
        return {"winner": "meta", "regression": "", "reason": "meta follows the trigger"}

    result = await run_runtime_e2e_gate(
        skill_md=SKILL_MD,
        eval_prompts=["please use synth test trigger"],
        baseline_model="frontier/highest",
        runner=runner,
        judge=judge,
    )

    assert result["status"] == "ok"
    assert result["passed"] is True
    assert result["winner"] == "meta"
    assert calls == [
        ("meta", "please use synth test trigger", "frontier/highest"),
        ("baseline", "please use synth test trigger", "frontier/highest"),
    ]


@pytest.mark.asyncio
async def test_runtime_e2e_gate_blocks_baseline_winner() -> None:
    async def runner(*, route: str, prompt: str, skill_md: str, baseline_model: str) -> dict:
        return {"text": f"{route} output", "model": baseline_model}

    async def judge(*, prompt: str, meta: dict, baseline: dict) -> dict:
        return {
            "winner": "baseline",
            "regression": "meta omits the requested evidence",
            "reason": "baseline is more complete",
        }

    result = await run_runtime_e2e_gate(
        skill_md=SKILL_MD,
        eval_prompts=["please use synth test trigger"],
        baseline_model="frontier/highest",
        runner=runner,
        judge=judge,
    )

    assert result["passed"] is False
    assert result["winner"] == "baseline"
    assert result["cases"][0]["regression"] == "meta omits the requested evidence"


@pytest.mark.asyncio
async def test_runtime_e2e_gate_blocks_invalid_baseline_refusal() -> None:
    async def runner(*, route: str, prompt: str, skill_md: str, baseline_model: str) -> dict:
        if route == "baseline":
            return {
                "text": (
                    "Runtime E2E baseline mode: meta-skill creator tools are "
                    "disabled, so I cannot complete this request."
                ),
                "model": baseline_model,
            }
        return {"text": "meta output", "model": "meta"}

    async def judge(*, prompt: str, meta: dict, baseline: dict) -> dict:
        raise AssertionError("blocked/refusal baseline should not be sent to judge")

    result = await run_runtime_e2e_gate(
        skill_md=SKILL_MD,
        eval_prompts=["create a useful meta-skill from this workflow"],
        baseline_model="frontier/highest",
        runner=runner,
        judge=judge,
    )

    assert result["passed"] is False
    assert result["winner"] == "invalid"
    assert result["cases"][0]["regression"] == "baseline_invalid_or_blocked"


@pytest.mark.asyncio
async def test_runtime_e2e_context_baseline_runs_without_meta_loader() -> None:
    seen_configs: list[AgentConfig] = []

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            seen_configs.append(kwargs["config"])

        async def run_turn(self, prompt: str):
            yield DoneEvent(text=f"baseline handled {prompt}")

    ctx = make_runtime_e2e_context(
        provider=object(),
        base_config=AgentConfig(
            model_id="frontier/highest",
            metadata={"skill_loader": object(), "meta_match": object(), "keep": "yes"},
        ),
        skill_loader=object(),
        tool_definitions=[],
        tool_handler=None,
        agent_factory=FakeAgent,
        llm_chat=None,
        tool_invoker=None,
        session_key="test",
        baseline_model="frontier/highest",
    )

    result = await ctx["runner"](
        route="baseline",
        prompt="compare this",
        skill_md=SKILL_MD,
        baseline_model="frontier/highest",
    )

    assert result["text"] == "baseline handled compare this"
    assert seen_configs[0].metadata == {"keep": "yes"}
    assert seen_configs[0].model_id == "frontier/highest"


@pytest.mark.asyncio
async def test_runtime_e2e_context_baseline_blocks_creator_side_effect_tools() -> None:
    observed: list[tuple[str, bool, str]] = []

    async def unsafe_tool_handler(tc: ToolCall):
        raise AssertionError(f"baseline leaked creator tool call: {tc.tool_name}")

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.tool_handler = kwargs["tool_handler"]

        async def run_turn(self, prompt: str):
            result = await self.tool_handler(ToolCall(
                tool_use_id="tool-1",
                tool_name="meta_skill_persist_proposal",
                arguments={},
            ))
            observed.append((result.tool_name, result.is_error, result.content))
            yield DoneEvent(text="baseline done")

    ctx = make_runtime_e2e_context(
        provider=object(),
        base_config=AgentConfig(model_id="frontier/highest"),
        skill_loader=object(),
        tool_definitions=[],
        tool_handler=unsafe_tool_handler,
        agent_factory=FakeAgent,
        llm_chat=None,
        tool_invoker=None,
        session_key="test",
        baseline_model="frontier/highest",
    )

    result = await ctx["runner"](
        route="baseline",
        prompt="compare this",
        skill_md=SKILL_MD,
        baseline_model="frontier/highest",
    )

    assert result["text"] == "baseline done"
    assert observed == [(
        "meta_skill_persist_proposal",
        False,
        "Continue without this tool and write the strongest standalone answer "
        "directly in the final response.",
    )]


@pytest.mark.asyncio
async def test_runtime_e2e_context_baseline_hides_meta_tools_and_instructs_direct_answer() -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            captured["config"] = kwargs["config"]
            captured["tool_definitions"] = kwargs["tool_definitions"]

        async def run_turn(self, prompt: str):
            yield DoneEvent(text="baseline direct answer")

    ctx = make_runtime_e2e_context(
        provider=object(),
        base_config=AgentConfig(model_id="frontier/highest"),
        skill_loader=object(),
        tool_definitions=[
            {"type": "function", "function": {"name": "meta_invoke"}},
            {"type": "function", "function": {"name": "meta_skill_persist_proposal"}},
            {"type": "function", "function": {"name": "memory_search"}},
        ],
        tool_handler=None,
        agent_factory=FakeAgent,
        llm_chat=None,
        tool_invoker=None,
        session_key="test",
        baseline_model="frontier/highest",
    )

    result = await ctx["runner"](
        route="baseline",
        prompt="create a meta-skill from my history",
        skill_md=SKILL_MD,
        baseline_model="frontier/highest",
    )

    assert result["text"] == "baseline direct answer"
    assert captured["tool_definitions"] == [
        {"type": "function", "function": {"name": "memory_search"}},
    ]
    config = captured["config"]
    assert isinstance(config, AgentConfig)
    assert "highest-tier single model" in (config.request_context_prompt or "")
    assert "standalone proposal" in (config.request_context_prompt or "")
    assert "disabled" not in (config.request_context_prompt or "").lower()
