from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from tui_real_terminal import assertions
from tui_real_terminal.driver import (
    RealTerminalSession,
    TerminalFrame,
    TerminalSize,
)
from tui_real_terminal.evidence import (
    EvidenceBundle,
    ScenarioFailure,
    ScenarioResult,
)
from tui_real_terminal.framebuffer import assert_opentui_framebuffer
from tui_real_terminal.targets import TUI_READY_TIMEOUT_SECONDS
from tui_real_terminal.visual import blocking, build_visual_verdict

ScenarioFamily = Literal[
    "launch_and_input_loop",
    "long_streaming_output",
    "complex_ui_state",
    "architecture_prompt",
    "live_prompt",
    "terminal_changes",
    "completion_menu",
]

ScenarioAction = Literal[
    "wait_text",
    "wait_any_text",
    "send_text",
    "paste",
    "key",
    "resize",
    "capture",
    "ack_phase",
]


@dataclass(frozen=True)
class ScenarioStep:
    step_id: str
    action: ScenarioAction
    value: str = ""
    checkpoint: str = ""
    timeout_s: float = 5.0
    assert_framebuffer: bool = False


@dataclass(frozen=True)
class TuiScenario:
    scenario_id: str
    family: ScenarioFamily
    initial_size: TerminalSize
    steps: tuple[ScenarioStep, ...]
    expected_text: tuple[str, ...]
    requires_tmux: bool = False
    requires_prompt_ready: bool = True
    required_backend_id: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "family": self.family,
            "initial_size": {
                "cols": self.initial_size.cols,
                "rows": self.initial_size.rows,
            },
            "steps": [step.__dict__ for step in self.steps],
            "expected_text": list(self.expected_text),
            "requires_tmux": self.requires_tmux,
            "requires_prompt_ready": self.requires_prompt_ready,
            "required_backend_id": self.required_backend_id,
        }


def all_scenarios() -> tuple[TuiScenario, ...]:
    return (
        TuiScenario(
            scenario_id="launch_input_loop",
            family="launch_and_input_loop",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep("send-message", "send_text", "hello harness", "after-input"),
                ScenarioStep(
                    "wait-response",
                    "wait_text",
                    "fake-response:hello harness",
                    "after-response",
                ),
            ),
            expected_text=("fake-response:hello harness",),
        ),
        TuiScenario(
            scenario_id="cjk_input_loop",
            family="launch_and_input_loop",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep(
                    "send-message",
                    "send_text",
                    "中文输入 CJK混合ASCII",
                    "after-input",
                ),
                ScenarioStep(
                    "wait-response",
                    "wait_text",
                    "fake-response:中文输入 CJK混合ASCII",
                    "after-response",
                ),
            ),
            expected_text=("fake-response:中文输入 CJK混合ASCII", "CJK混合ASCII"),
        ),
        TuiScenario(
            scenario_id="long_streaming",
            family="long_streaming_output",
            initial_size=TerminalSize(cols=140, rows=36),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep("send-message", "send_text", "stream please", "after-input"),
                ScenarioStep(
                    "wait-stream-wide",
                    "wait_text",
                    "stream-token-010",
                    "during-stream-wide",
                    timeout_s=10.0,
                ),
                ScenarioStep("resize-stream-narrow", "resize", "72x24", "during-stream-narrow"),
                ScenarioStep(
                    "wait-stream-narrow",
                    "wait_text",
                    "stream-token-035",
                    "after-stream-narrow",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep(
                    "resize-stream-wide",
                    "resize",
                    "140x34",
                    "stream-restored-wide",
                    assert_framebuffer=True,
                ),
                ScenarioStep(
                    "wait-stream-complete",
                    "wait_text",
                    "stream-token-079",
                    "after-stream",
                    timeout_s=10.0,
                ),
                ScenarioStep(
                    "capture-prompt-restored",
                    "capture",
                    "",
                    "after-prompt-restored",
                    timeout_s=0.2,
                ),
            ),
            expected_text=("stream-token-079",),
            requires_tmux=True,
        ),
        TuiScenario(
            scenario_id="complex_ui_state",
            family="complex_ui_state",
            initial_size=TerminalSize(cols=110, rows=34),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep(
                    "send-message",
                    "send_text",
                    "complex state please",
                    "after-input",
                ),
                ScenarioStep(
                    "wait-ensemble",
                    "wait_text",
                    "Ensemble · 0/2 complete",
                    "during-ensemble",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep("ack-ensemble", "ack_phase", "ensemble"),
                ScenarioStep(
                    "wait-reasoning",
                    "wait_text",
                    "reasoning-process-streams-live",
                    "during-reasoning",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep("ack-reasoning", "ack_phase", "reasoning"),
                ScenarioStep(
                    "wait-intermediate",
                    "wait_text",
                    "intermediate-before-tool",
                    "during-intermediate",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep("ack-intermediate", "ack_phase", "intermediate"),
                ScenarioStep(
                    "wait-tool",
                    "wait_text",
                    "fake_tool",
                    "during-tool",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep("ack-tool", "ack_phase", "tool"),
                ScenarioStep(
                    "wait-answer",
                    "wait_text",
                    "complex-state-complete",
                    "during-answer",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep("ack-answer", "ack_phase", "answer"),
                ScenarioStep(
                    "wait-usage",
                    "wait_text",
                    "think 436",
                    "after-usage",
                    timeout_s=10.0,
                    assert_framebuffer=True,
                ),
                ScenarioStep("ack-usage", "ack_phase", "usage"),
                ScenarioStep(
                    "settle-final-frame",
                    "capture",
                    "",
                    "after-complex",
                    timeout_s=0.3,
                    assert_framebuffer=True,
                ),
            ),
            expected_text=(
                "route standard",
                "Ensemble",
                "fake_tool",
                "complex-state-complete",
                "think 436",
            ),
            requires_tmux=True,
        ),
        TuiScenario(
            scenario_id="architecture_prompt",
            family="architecture_prompt",
            initial_size=TerminalSize(cols=112, rows=34),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep(
                    "send-message",
                    "send_text",
                    "帮我分析这个代码长的架构 /workspace/opensquilla",
                    "after-input",
                ),
                ScenarioStep(
                    "wait-architecture",
                    "wait_any_text",
                    "architecture-analysis-complete\n╰ in 1 / out 2",
                    "after-architecture",
                    timeout_s=10.0,
                ),
            ),
            expected_text=("架构",),
        ),
        TuiScenario(
            scenario_id="terminal_changes",
            family="terminal_changes",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep("resize-narrow", "resize", "72x24", "after-narrow"),
                ScenarioStep(
                    "paste-multiline",
                    "paste",
                    "first line\nsecond line CJK混合ASCII",
                    "after-paste",
                ),
                ScenarioStep("submit-paste", "key", "Enter", "after-submit"),
                ScenarioStep(
                    "wait-terminal-change",
                    "wait_text",
                    "terminal-change-response",
                    "after-response",
                    timeout_s=10.0,
                ),
                # The fake app echoes the submitted input line by line; waiting
                # for the second echoed line proves the pasted newline survived
                # the composer (a collapsed paste would submit a single line).
                ScenarioStep(
                    "wait-paste-echo",
                    "wait_text",
                    "echo-line-1:second line CJK混合ASCII",
                    "after-paste-echo",
                    timeout_s=10.0,
                ),
                ScenarioStep("resize-wide", "resize", "120x34", "after-wide"),
                ScenarioStep(
                    "settle-wide",
                    "capture",
                    "",
                    "after-wide-settled",
                    timeout_s=0.5,
                ),
                ScenarioStep("ctrl-c", "key", "C-c", "after-ctrl-c"),
            ),
            expected_text=(
                "terminal-change-response lines=2",
                "echo-line-0:first line",
                "echo-line-1:second line CJK混合ASCII",
            ),
        ),
        TuiScenario(
            scenario_id="completion_slash_menu_filter",
            family="completion_menu",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep("open-slash-menu", "key", "/", "slash-menu-open"),
                ScenarioStep("filter-slash-c", "key", "c", "slash-menu-c"),
                ScenarioStep("filter-slash-o", "key", "o", "slash-menu-co"),
                ScenarioStep(
                    "capture-filtered-menu",
                    "capture",
                    "",
                    "slash-menu-filtered",
                    timeout_s=0.3,
                ),
            ),
            expected_text=("/compact",),
            requires_prompt_ready=False,
        ),
        TuiScenario(
            scenario_id="completion_menu_preserves_history",
            family="completion_menu",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep(
                    "send-message",
                    "send_text",
                    "history before menu",
                    "after-input",
                ),
                ScenarioStep(
                    "wait-response",
                    "wait_text",
                    "fake-response:history before menu",
                    "after-response",
                ),
                ScenarioStep("open-slash-menu", "key", "/", "menu-open-over-history"),
                ScenarioStep(
                    "capture-menu-over-history",
                    "capture",
                    "",
                    "menu-over-history",
                    timeout_s=0.3,
                ),
            ),
            # The conversation text rendered before the menu must remain visible:
            # the overlay layer must not paint a filled rectangle over history.
            expected_text=("fake-response:history before menu", "/model"),
            requires_prompt_ready=False,
        ),
        TuiScenario(
            scenario_id="completion_menu_resize",
            family="completion_menu",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep("open-slash-menu", "key", "/", "resize-menu-open"),
                ScenarioStep(
                    "resize-narrow",
                    "resize",
                    "72x24",
                    "after-narrow-completion-menu",
                ),
                ScenarioStep(
                    "resize-wide",
                    "resize",
                    "120x34",
                    "after-wide-completion-menu",
                ),
                ScenarioStep(
                    "capture-resized-menu",
                    "capture",
                    "",
                    "after-resize-completion-menu",
                    timeout_s=0.3,
                ),
            ),
            expected_text=("/model",),
            requires_prompt_ready=False,
        ),
        TuiScenario(
            scenario_id="completion_file_menu_escape",
            family="completion_menu",
            initial_size=TerminalSize(cols=100, rows=30),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep("open-file-menu", "key", "@", "file-menu-key"),
                ScenarioStep(
                    "capture-file-menu",
                    "capture",
                    "",
                    "file-menu-open",
                    timeout_s=0.4,
                ),
                ScenarioStep("close-file-menu", "key", "Escape", "after-close-key"),
                ScenarioStep(
                    "capture-closed-file-menu",
                    "capture",
                    "",
                    "after-close-file-menu",
                    timeout_s=0.3,
                ),
            ),
            expected_text=(),
            requires_prompt_ready=False,
        ),
        TuiScenario(
            scenario_id="live_opentui_architecture_prompt",
            family="live_prompt",
            initial_size=TerminalSize(cols=112, rows=34),
            steps=(
                ScenarioStep("wait-ready", "wait_text", "OPEN_SQUILLA_TUI_READY", "ready"),
                ScenarioStep(
                    "send-message",
                    "send_text",
                    "帮我分析这个代码长的架构 /workspace/opensquilla",
                    "after-input",
                ),
                ScenarioStep(
                    "wait-turn-complete",
                    "wait_any_text",
                    "╰ in \nThe task timed out before it could finish.",
                    "after-turn-complete",
                    timeout_s=180.0,
                ),
                ScenarioStep(
                    "capture-final",
                    "capture",
                    "",
                    "after-final",
                    timeout_s=0.2,
                ),
            ),
            expected_text=(),
            requires_tmux=True,
            requires_prompt_ready=False,
            required_backend_id="live-opentui",
        ),
    )


def scenario_by_id(scenario_id: str) -> TuiScenario:
    scenarios = {scenario.scenario_id: scenario for scenario in all_scenarios()}
    try:
        return scenarios[scenario_id]
    except KeyError as exc:
        raise ValueError(f"unknown real-terminal TUI scenario: {scenario_id}") from exc


def run_scenario(
    *,
    scenario: TuiScenario,
    session: RealTerminalSession,
    evidence: EvidenceBundle,
    backend_id: str,
) -> ScenarioResult:
    started_at = time.monotonic()
    evidence.write_scenario(scenario.to_json_dict())
    last_frame = TerminalFrame("not-started", "", 0, scenario.initial_size)
    last_frame_path = evidence.frames_dir / "not-started.txt"
    current_step = "start"
    phase_ack_dir = evidence.run_dir / "phase-acks"
    if any(step.action == "ack_phase" for step in scenario.steps):
        phase_ack_dir.mkdir(parents=True, exist_ok=True)
        (phase_ack_dir / "enabled").write_text("enabled\n", encoding="utf-8")
    session.start()
    try:
        last_frame = session.capture_text("started")
        last_frame_path = evidence.record_frame(last_frame)
        for step in scenario.steps:
            current_step = step.step_id
            last_frame = _run_step(
                session,
                step,
                phase_ack_dir=phase_ack_dir,
            )
            last_frame_path = evidence.record_frame(last_frame)
            assertions.assert_no_traceback(last_frame)
            assertions.assert_no_raw_ansi_leakage(last_frame)
            assertions.assert_no_inline_prompt_chrome_collision(last_frame)
            assertions.assert_no_duplicate_fixed_chrome(last_frame)
            if _requires_styled_framebuffer_gate(scenario, last_frame.checkpoint):
                if not session.alternate_screen_active():
                    raise AssertionError(f"{last_frame.checkpoint}: TUI left alternate-screen mode")
                framebuffer = session.capture_framebuffer(last_frame.checkpoint)
                if framebuffer is None:
                    raise AssertionError(
                        f"{last_frame.checkpoint}: terminal driver cannot capture "
                        "a styled cell framebuffer"
                    )
                evidence.record_framebuffer(framebuffer)
                assert_opentui_framebuffer(
                    framebuffer,
                    cursor=session.cursor_position(),
                )
            if not session.is_alive() and step.action != "key":
                raise AssertionError(f"{step.step_id}: terminal process exited unexpectedly")
        # Keep one styled, exact-cell framebuffer for visual review in addition
        # to the structural alternate-screen gates above.
        final_framebuffer = session.capture_framebuffer("visual-final")
        if final_framebuffer is not None:
            evidence.record_framebuffer(final_framebuffer)
        scrollback = session.capture_scrollback_text("scrollback")
        for expected in scenario.expected_text:
            assertions.assert_visible_text(last_frame, expected)
        if scenario.requires_prompt_ready:
            assertions.assert_prompt_ready(last_frame)
        evidence.write_scrollback(scrollback)
        result = ScenarioResult(
            scenario_id=scenario.scenario_id,
            backend_id=backend_id,
            status="pass",
            run_dir=evidence.run_dir,
        )
        _write_visual_verdict(
            scenario=scenario,
            backend_id=backend_id,
            evidence=evidence,
            frame=last_frame,
            frame_path=last_frame_path,
        )
        evidence.write_result(result)
        return result
    except Exception as exc:
        failure = ScenarioFailure(
            step_id=current_step,
            message=str(exc),
            elapsed_s=round(time.monotonic() - started_at, 3),
            last_screen=last_frame.text,
            artifact_dir=str(evidence.run_dir),
        )
        result = ScenarioResult(
            scenario_id=scenario.scenario_id,
            backend_id=backend_id,
            status="fail",
            run_dir=evidence.run_dir,
            failure=failure,
        )
        _write_visual_verdict(
            scenario=scenario,
            backend_id=backend_id,
            evidence=evidence,
            frame=last_frame,
            frame_path=last_frame_path,
        )
        evidence.write_result(result)
        raise
    finally:
        session.terminate()


def _run_step(
    session: RealTerminalSession,
    step: ScenarioStep,
    *,
    phase_ack_dir: Path,
) -> TerminalFrame:
    checkpoint = step.checkpoint or step.step_id
    if step.action == "wait_text":
        timeout_s = step.timeout_s
        if step.step_id == "wait-ready":
            timeout_s = max(timeout_s, TUI_READY_TIMEOUT_SECONDS)
        return session.wait_for_text(step.value, timeout_s=timeout_s, checkpoint=checkpoint)
    if step.action == "wait_any_text":
        needles = tuple(item for item in step.value.splitlines() if item)
        return _wait_for_any_text(
            session, needles, timeout_s=step.timeout_s, checkpoint=checkpoint
        )
    if step.action == "send_text":
        session.send_text(step.value)
        return session.capture_text(checkpoint)
    if step.action == "paste":
        session.paste(step.value)
        return session.capture_text(checkpoint)
    if step.action == "key":
        session.send_key(step.value)
        return session.capture_text(checkpoint)
    if step.action == "resize":
        cols, rows = step.value.split("x", 1)
        session.resize(TerminalSize(cols=int(cols), rows=int(rows)))
        time.sleep(0.25)
        return _capture_stable_resize_frame(session, checkpoint)
    if step.action == "capture":
        if step.timeout_s:
            time.sleep(step.timeout_s)
        return session.capture_text(checkpoint)
    if step.action == "ack_phase":
        phase_ack_dir.mkdir(parents=True, exist_ok=True)
        (phase_ack_dir / f"{step.value}.ack").write_text("captured\n", encoding="utf-8")
        return session.capture_text(checkpoint)
    raise ValueError(f"unknown scenario step action: {step.action}")


def _wait_for_any_text(
    session: RealTerminalSession,
    needles: tuple[str, ...],
    *,
    timeout_s: float,
    checkpoint: str,
) -> TerminalFrame:
    deadline = time.monotonic() + timeout_s
    last = session.capture_text(checkpoint)
    if any(needle in last.text for needle in needles):
        return last
    while time.monotonic() < deadline:
        time.sleep(0.05)
        last = session.capture_text(checkpoint)
        if any(needle in last.text for needle in needles):
            return last
    expected = " or ".join(repr(needle) for needle in needles)
    raise TimeoutError(f"timed out waiting for {expected}; last screen: {last.text}")


def _capture_stable_resize_frame(
    session: RealTerminalSession,
    checkpoint: str,
) -> TerminalFrame:
    deadline = time.monotonic() + 1.0
    last = session.capture_text(checkpoint)
    while _has_duplicate_inline_prompt(last) and time.monotonic() < deadline:
        time.sleep(0.05)
        last = session.capture_text(checkpoint)
    return last


def _has_duplicate_inline_prompt(frame: TerminalFrame) -> bool:
    placeholders = ("send a message", "send a massage")
    return sum(frame.text.count(placeholder) for placeholder in placeholders) > 1


def _requires_styled_framebuffer_gate(
    scenario: TuiScenario,
    checkpoint: str,
) -> bool:
    return any(
        step.assert_framebuffer
        and (step.checkpoint or step.step_id) == checkpoint
        for step in scenario.steps
    )


def _write_visual_verdict(
    *,
    scenario: TuiScenario,
    backend_id: str,
    evidence: EvidenceBundle,
    frame: TerminalFrame,
    frame_path: Path,
) -> None:
    verdict = build_visual_verdict(
        scenario_id=scenario.scenario_id,
        checkpoint=frame.checkpoint,
        backend_id=backend_id,
        terminal_size={"cols": frame.size.cols, "rows": frame.size.rows},
        screenshot_path=None,
        frame_path=str(frame_path),
        expected_visible_regions=("prompt", "assistant stream", scenario.family),
    )
    verdict_path = evidence.write_visual_verdict(verdict)
    if blocking(verdict):
        raise AssertionError(f"blocking visual verdict: {verdict_path}")
