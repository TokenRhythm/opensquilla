from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tui_real_terminal.replay import replay_architecture_prompt
from tui_real_terminal.scenarios import TuiScenario, scenario_by_id

pytestmark = pytest.mark.tui_real_terminal

ARCHITECTURE_PROMPT = "帮我分析这个代码长的架构 /workspace/opensquilla"
ARCHITECTURE_REPLAY_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "tui"
    / "architecture_prompt_replay.json"
)


class _ReplayRecordingRenderer:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.finished_tools: list[dict[str, Any]] = []
        self.text_chunks: list[str] = []

    async def astatus(self, message: str, *, style: str = "dim") -> None:
        self.statuses.append(message)

    async def atool_start(
        self,
        name: str,
        args: dict[str, Any] | None,
        tool_use_id: str,
    ) -> None:
        pass

    async def atool_finished(
        self,
        tool_use_id: str,
        *,
        success: bool,
        elapsed: float | None = None,
        error: str | None = None,
        result: str | None = None,
    ) -> None:
        self.finished_tools.append(
            {
                "tool_use_id": tool_use_id,
                "success": success,
                "elapsed": elapsed,
                "error": error,
                "result": result,
            }
        )

    async def aappend_text(self, text: str) -> None:
        self.text_chunks.append(text)


class _ReplayRecordingOutput:
    def set_toolbar(self, key: str, value: object | None) -> None:
        pass

    def invalidate(self) -> None:
        pass


def _architecture_prompt_scenario_for_assertions() -> TuiScenario:
    scenario = scenario_by_id("architecture_prompt")
    steps = tuple(
        replace(step, value="架构分析")
        if step.step_id == "wait-architecture"
        else step
        for step in scenario.steps
    )
    return replace(scenario, steps=steps)


def test_architecture_prompt_replay_fixture_is_saved_for_render_only_runs() -> None:
    fixture = json.loads(ARCHITECTURE_REPLAY_FIXTURE.read_text(encoding="utf-8"))
    events = fixture["events"]
    kinds = [event["kind"] for event in events]

    assert fixture["source_prompt"] == ARCHITECTURE_PROMPT
    assert "toolbar" in kinds
    assert "tool_start" in kinds
    assert "tool_finished" in kinds
    assert "tool_output" in kinds
    assert "text_delta" in kinds
    assert events[-1]["kind"] == "done"
    tool_outputs = [event["payload"] for event in events if event["kind"] == "tool_output"]
    assert tool_outputs
    assert all("stdout" in payload for payload in tool_outputs)
    assert any(payload.get("truncated") for payload in tool_outputs)
    assert any(
        "architecture-analysis-complete" in event["payload"].get("text", "")
        for event in events
        if event["kind"] == "text_delta"
    )


def test_architecture_prompt_replay_routes_tool_output_through_finished_result() -> None:
    renderer = _ReplayRecordingRenderer()

    usage = asyncio.run(replay_architecture_prompt(renderer, _ReplayRecordingOutput()))

    finished_by_id = {
        event["tool_use_id"]: event for event in renderer.finished_tools
    }
    list_result = finished_by_id["tool-list"]["result"]
    read_result = finished_by_id["tool-read"]["result"]

    assert usage.model == "fake-opentui"
    assert isinstance(list_result, str)
    assert "top-level repository layout" in list_result
    assert "AGENTS.md" in list_result
    assert isinstance(read_result, str)
    assert "OpenTUI runtime owns the chat surface factory" in read_result
    assert "async def run_opentui_chat_runtime(" in read_result
    assert not any("tool_output" in message for message in renderer.statuses)
    assert not any("stdout:" in message for message in renderer.statuses)
    assert not any("truncated" in message for message in renderer.statuses)


def test_architecture_prompt_renders_tools_and_chinese_output(
    run_real_terminal_scenario,
) -> None:
    result = run_real_terminal_scenario(_architecture_prompt_scenario_for_assertions())

    assert result.status == "pass"
    transcript = (result.run_dir / "transcript.txt").read_text(encoding="utf-8")
    scrollback = (result.run_dir / "scrollback.txt").read_text(encoding="utf-8")
    rendered_output = f"{transcript}\n{scrollback}"
    app_log_name = "opentui-app.log"
    app_events = [
        json.loads(line)
        for line in (result.run_dir / app_log_name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    submitted = [
        event["payload"].get("input", "")
        for event in app_events
        if event["event"] == "dispatch"
    ]

    assert any(ARCHITECTURE_PROMPT in item for item in submitted)
    assert "架构" in rendered_output
    # Fullscreen alt-screen viewport: the conversation lives in a ScrollBox, so
    # assertions cover the markers visible in the final frame. The assistant
    # turn renders as ONE card with width-independent chrome (a canonical
    # Agent label, continuous left gutter, and a "╰ <usage>" footer that carries
    # the usage receipt). Each tool shows a "✓ <name> <args> · <dur>" invocation
    # row, a compact args disclosure, and a multiline result preview with an
    # explicit hidden-line receipt. The prompt uses the transcript's explicit
    # user-role contract: one brand rail, a `you` label, and the prompt text on
    # a dedicated low-contrast surface.
    assert "╭ main" in rendered_output
    assert "│ you  帮我分析这个代码长的架构" in rendered_output
    assert "╭─ prompt" not in rendered_output
    assert "✓ list_dir /workspace/opensquilla · 0.0s" in rendered_output
    assert "✓ read_file" in rendered_output
    assert rendered_output.count("args · 3 lines hidden · expand details") >= 2
    assert "├ top-level repository layout" in rendered_output
    assert "├ AGENTS.md" in rendered_output
    assert "more output lines · expand details" in rendered_output
    assert "├ OpenTUI runtime owns the chat surface factory" in rendered_output
    assert "├ structured output handle" in rendered_output
    assert "╰ in 1 / out 2 · fake-opentui" in rendered_output
    assert "tool_output" not in rendered_output
    assert "stdout:" not in rendered_output
    assert "truncated" not in rendered_output
    assert "Traceback" not in rendered_output
    assert "\x1b[" not in rendered_output
