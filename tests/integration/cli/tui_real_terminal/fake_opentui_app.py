from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tui_real_terminal.replay import replay_architecture_prompt
else:
    from replay import replay_architecture_prompt

from opensquilla.cli.chat.turn import UsageSummary  # type: ignore[import-untyped]
from opensquilla.cli.tui.opentui.history import (  # type: ignore[import-untyped]
    history_replace_from_bootstrap,
)
from opensquilla.cli.tui.opentui.renderer import (
    OpenTuiStreamRenderer,  # type: ignore[import-untyped]
)
from opensquilla.cli.tui.opentui.runtime import (  # type: ignore[import-untyped]
    get_tui_output,
    run_opentui_chat_runtime,
)
from opensquilla.engine.commands import Surface  # type: ignore[import-untyped]


def _app_log_path() -> Path:
    return Path(os.environ["OPENSQUILLA_TUI_FAKE_APP_LOG"])


def _write_log(event: str, payload: dict[str, Any] | None = None) -> None:
    path = _app_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": event, "payload": payload or {}}, sort_keys=True) + "\n")


async def _wait_phase_ack(phase: str, *, timeout_s: float = 20.0) -> None:
    """Hold a transient visual state until the terminal harness captures it."""

    raw_dir = os.environ.get("OPENSQUILLA_TUI_FAKE_PHASE_ACK_DIR", "").strip()
    if not raw_dir:
        await asyncio.sleep(0.25)
        return
    ack_dir = Path(raw_dir)
    # Only scenario runs that explicitly create this sentinel own the phase
    # clock. Other real-terminal tests reuse the same fake complex turn and
    # must keep streaming without waiting for visual capture acknowledgements.
    if not (ack_dir / "enabled").is_file():
        await asyncio.sleep(0.25)
        return
    ack_dir.mkdir(parents=True, exist_ok=True)
    ack_path = ack_dir / f"{phase}.ack"
    _write_log("phase_wait", {"phase": phase, "ack": str(ack_path)})
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not ack_path.is_file():
        if loop.time() >= deadline:
            raise TimeoutError(f"timed out waiting for visual phase ack: {phase}")
        await asyncio.sleep(0.01)
    _write_log("phase_ack", {"phase": phase})


async def _render_response(
    scope: dict[str, Any],
    user_input: str,
    scenario_id: str,
) -> bool:
    if user_input.strip() in {"/exit", "exit"}:
        _write_log("exit")
        return False

    output = get_tui_output(scope)
    if output is None:
        raise RuntimeError("opentui output handle was not exposed")

    renderer = OpenTuiStreamRenderer(title="squilla", output_handle=output)
    usage = UsageSummary(model="fake-terminal", input_tokens=1, output_tokens=2)
    _write_log("dispatch", {"input": user_input, "scenario_id": scenario_id})
    if scenario_id == "alternate_screen_mode_loss":
        # Test-only fault injection: simulate an embedded terminal remount that
        # forgets DECSET 1049 while both Python and the Bun Host still believe
        # they own the alternate screen. os.write bypasses notice capture and
        # reaches the shared PTY exactly like an external terminal reset.
        os.write(
            1,
            (b"\x1b[?1049l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1004l\x1b[?1006l\x1b[?2004l"),
        )
        _write_log("surface_mode_reset")
        await asyncio.sleep(3.0)
        return True
    if scenario_id == "long_streaming":
        stream_delay_s = max(
            0.001,
            min(
                0.25,
                float(os.environ.get("OPENSQUILLA_TUI_FAKE_STREAM_DELAY_S", "0.015")),
            ),
        )
        for index in range(80):
            await renderer.aappend_text(f"stream-token-{index:03d} ")
            # Keep the turn live long enough for the real-terminal scenario to
            # resize narrow and wide while the same answer block is mutating.
            await asyncio.sleep(stream_delay_s)
    elif scenario_id in {
        "same_size_stream_framebuffer_recovery",
        "same_size_eventless_stream_framebuffer_recovery",
    }:
        stream_delay_s = max(
            0.001,
            min(
                0.25,
                float(os.environ.get("OPENSQUILLA_TUI_FAKE_STREAM_DELAY_S", "0.015")),
            ),
        )
        for index in range(80):
            cjk_probe = "推理中 " if index % 8 == 0 else ""
            await renderer.aappend_reasoning(f"stream-token-{index:03d} {cjk_probe}")
            # Keep real provider-style reasoning deltas live while the terminal
            # surface disappears and remounts at the same geometry.
            await asyncio.sleep(stream_delay_s)
    elif scenario_id == "complex_ui_state":
        usage = UsageSummary(
            model="fake-terminal",
            input_tokens=36_060,
            output_tokens=1_047,
            reasoning_tokens=436,
            model_usage_breakdown=[
                {
                    "role": "proposer",
                    "label": "fast",
                    "provider": "openrouter",
                    "model": "candidate-fast",
                    "input_tokens": 120,
                    "output_tokens": 40,
                    "request_count": 1,
                },
                {
                    "role": "proposer",
                    "label": "critic",
                    "provider": "openrouter",
                    "model": "candidate-critic",
                    "input_tokens": 118,
                    "output_tokens": 35,
                    "request_count": 1,
                },
                {
                    "role": "aggregator",
                    "label": "judge",
                    "provider": "openrouter",
                    "model": "fake-terminal",
                    "input_tokens": 200,
                    "output_tokens": 80,
                    "request_count": 1,
                },
            ],
            ensemble_trace={
                "total_candidates": 2,
                "successful_proposers": 2,
                "fallback_used": False,
            },
        )
        await renderer.aturn_started()
        _set_toolbar(output, "router_hud", "route standard -> fake-terminal 99% save 42%")
        _set_toolbar(output, "router_hud_style", "normal")
        _invalidate(output)
        await renderer.astatus("router route standard -> fake-terminal 99% save 42%")
        await renderer.aensemble_progress(
            {
                "event_type": "proposer_start",
                "proposer_index": 0,
                "proposer_label": "fast",
                "proposer_provider": "openrouter",
                "proposer_model": "candidate-fast",
            }
        )
        await renderer.aensemble_progress(
            {
                "event_type": "proposer_start",
                "proposer_index": 1,
                "proposer_label": "critic",
                "proposer_provider": "openrouter",
                "proposer_model": "candidate-critic",
            }
        )
        # Hold one real framebuffer with both candidates visibly running. The
        # harness releases this barrier only after it records styled cells.
        await _wait_phase_ack("ensemble")
        await renderer.aensemble_progress(
            {
                "event_type": "proposer_finish",
                "proposer_index": 0,
                "proposer_label": "fast",
                "proposer_provider": "openrouter",
                "proposer_model": "candidate-fast",
                "elapsed_ms": 180,
                "input_tokens": 120,
                "output_tokens": 40,
            }
        )
        await renderer.aensemble_progress(
            {
                "event_type": "proposer_finish",
                "proposer_index": 1,
                "proposer_label": "critic",
                "proposer_provider": "openrouter",
                "proposer_model": "candidate-critic",
                "elapsed_ms": 210,
                "input_tokens": 118,
                "output_tokens": 35,
            }
        )
        # Mirror the real turn shape so the harness exercises the live
        # ensemble plus all three existing process/output block kinds:
        #   1. reasoning — the model's extended-thinking PROCESS. Exact safe
        #      deltas stream live, then retain a bounded tail when the model moves
        #      on to assistant narration.
        await renderer.aappend_reasoning(
            "\n".join(
                (
                    "reasoning-opening-context",
                    "reasoning-step-02",
                    "reasoning-step-03",
                    "reasoning-step-04",
                    "reasoning-step-05",
                    "reasoning-step-06",
                    "reasoning-step-07",
                    "reasoning-step-08",
                    "reasoning-step-09",
                    "reasoning-step-10",
                    "reasoning-step-11",
                    "reasoning-process-streams-live",
                )
            )
        )
        # Give the real-terminal driver a deterministic live frame before the
        # next assistant-text block closes and folds the reasoning block.
        await _wait_phase_ack("reasoning")
        #   2. assistant text the model speaks before a tool call — streams into
        #      a purple intermediate narration block.
        await renderer.aappend_text(
            "intermediate-before-tool narration "
            + "0123456789" * 10
            + "\nsecond-intermediate-line tail",
            presentation="intermediate",
        )
        await _wait_phase_ack("intermediate")
        await renderer.atool_start(
            "fake_tool",
            {
                "path": "fixture.txt",
                "include": ["thinking", "tool arguments", "tool results"],
            },
            "tool-1",
        )
        await renderer.atool_finished(
            "tool-1",
            success=True,
            elapsed=0.01,
            result=(
                "inspected fixture.txt\n"
                "thinking retained\n"
                "tool arguments retained\n"
                "tool results retained"
            ),
        )
        await renderer.astatus("approval requested: allow fake_tool fixture.txt")
        await _wait_phase_ack("tool")
        #   3. final answer — the cyan answer card.
        await renderer.aappend_text(
            "complex-state-complete tool-card history projection",
            presentation="answer",
        )
        await _wait_phase_ack("answer")
    elif scenario_id == "architecture_prompt":
        usage = await replay_architecture_prompt(renderer, output)
    elif scenario_id == "terminal_changes":
        # Echo the submitted input back so the harness can prove a multi-line
        # paste survived the composer round-trip. The line count and each line
        # render as their own short markdown paragraphs, so narrow-terminal
        # word wrap can never split an asserted needle across rows.
        lines = user_input.split("\n")
        await renderer.aappend_text(
            f"terminal-change-response lines={len(lines)}\n\n"
            "CJK混合ASCII multiline-paste ctrl-c-recovery wide-and-narrow-layout"
        )
        for index, line in enumerate(lines):
            await renderer.aappend_text(f"\n\necho-line-{index}:{line}")
    else:
        await renderer.aappend_text(f"fake-response:{user_input}")
    await renderer.afinalize(usage)
    if scenario_id == "complex_ui_state":
        await _wait_phase_ack("usage")
    _write_log("turn_complete", {"input": user_input})
    return True


def _set_toolbar(output: Any, key: str, value: object | None) -> None:
    setter = getattr(output, "set_toolbar", None)
    if callable(setter):
        setter(key, value)


def _invalidate(output: Any) -> None:
    invalidate = getattr(output, "invalidate", None)
    if callable(invalidate):
        invalidate()


async def _run() -> None:
    scenario_id = os.environ.get("OPENSQUILLA_TUI_FAKE_SCENARIO", "launch_input_loop")
    scope: dict[str, Any] = {
        "model": "fake-terminal",
        "session_key": f"fake:{scenario_id}",
    }
    if scenario_id in {
        "gateway_empty_bootstrap_startup",
        "gateway_resumed_bootstrap_startup",
    }:
        # Mirror run_gateway_chat's first-screen contract exactly: a real
        # Gateway always supplies a canonical history.replace frame, even for a
        # newly-created session whose durable history is empty.  The ordinary
        # fake-provider scenarios omit this frame, so they cannot catch startup
        # regressions caused by history replacement immediately before the
        # initial context.update.
        scope["workspace_label"] = str(Path.cwd())
        resumed = scenario_id == "gateway_resumed_bootstrap_startup"
        messages = (
            [
                {
                    "message_id": "bootstrap-user-1",
                    "role": "user",
                    "text": "BOOTSTRAP_USER_HISTORY",
                },
                {
                    "message_id": "bootstrap-assistant-1",
                    "role": "assistant",
                    "text": "BOOTSTRAP_ASSISTANT_HISTORY",
                },
            ]
            if resumed
            else []
        )
        scope["bootstrap"] = {
            "agent_identity": {
                "agent_id": "main",
                "name": "Mira",
                "emoji": "🦐",
                "theme": "ember",
            },
            "session": {
                "session_key": scope["session_key"],
                "display_name": ("Resumed Gateway session" if resumed else "Fresh Gateway session"),
                "effective_model": "fake-terminal",
                "workspace": str(Path.cwd()),
            },
            "history": {
                "history_scope": "complete",
                "has_more": False,
                "loaded_count": len(messages),
                "canonical_available": True,
                "messages": messages,
                "compaction_summaries": [],
            },
            "queue": {"running_count": 0, "queued_count": 0},
            "stream_cursor": 0,
        }
        scope["history_replace"] = history_replace_from_bootstrap(
            scope["bootstrap"],
            fallback_session_key=str(scope["session_key"]),
        )
    if scenario_id == "complex_ui_state":
        scope["workspace_label"] = str(Path.cwd())
        scope["bootstrap"] = {
            "agent_identity": {
                "agent_id": "main",
                "name": "Mira",
                "emoji": "🦐",
                "theme": "ember",
            },
            "session": {
                "session_key": scope["session_key"],
                "display_name": "TUI output fidelity",
                "effective_model": "fake-terminal",
                "workspace": str(Path.cwd()),
            },
            "queue": {"running_count": 0, "queued_count": 0},
        }
    _write_log("ready", {"scenario_id": scenario_id})
    await run_opentui_chat_runtime(
        surface=Surface.CLI_GATEWAY,
        scope=scope,
        dispatch=lambda user_input: _render_response(scope, user_input, scenario_id),
        queue_max_size=4,
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
