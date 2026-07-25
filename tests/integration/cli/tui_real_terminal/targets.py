from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tui_real_terminal.driver import TerminalSize

TuiBackendId = Literal["opentui", "live-opentui"]


@dataclass(frozen=True)
class TargetContext:
    project_root: Path
    artifact_dir: Path
    scenario_id: str
    size: TerminalSize


@dataclass(frozen=True)
class TuiTarget:
    backend_id: TuiBackendId
    command: list[str]
    env: dict[str, str]
    initial_size: TerminalSize
    readiness_markers: tuple[str, ...]
    log_paths: tuple[Path, ...]
    capability_requirements: tuple[str, ...]
    available: bool = True
    skip_reason: str | None = None


def opentui_host_skip_reason() -> str | None:
    """Reason the Bun/OpenTUI host cannot launch here, or None when it can.

    Both backends drive the real fd bridge, so without Bun or an installed
    ``@opentui/core`` the app never prints its readiness marker and the driver
    fails on a wait timeout that names neither. These scenarios already skip
    when tmux or the terminal capabilities are missing; a checkout that has not
    run ``bun install`` is the same kind of missing precondition, and
    CONTRIBUTING asks the default path to stay fork-safe.

    Kept separate from ``TuiTarget.available`` so target construction keeps
    describing the target rather than the machine it would run on.
    """
    try:
        from opensquilla.cli.tui.opentui.bridge import (  # type: ignore[import-untyped]
            check_opentui_host_available,
        )

        availability = check_opentui_host_available()
    except Exception as exc:  # noqa: BLE001 - a failed probe is itself a skip reason
        return f"OpenTUI host probe failed: {exc}"
    if availability.available:
        return None
    return availability.reason or "OpenTUI host unavailable"


def build_tui_target(backend_id: str, context: TargetContext) -> TuiTarget:
    if backend_id == "opentui":
        return _opentui_target(context)
    if backend_id == "live-opentui":
        return _live_opentui_target(context)
    raise ValueError(f"only opentui is supported; got TUI backend target: {backend_id}")


def _base_env(context: TargetContext, *, isolate_state: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    src_path = str(context.project_root / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
    if isolate_state:
        env["OPENSQUILLA_STATE_DIR"] = str(context.artifact_dir / "state")
    env["OPENSQUILLA_LOG_DIR"] = str(context.artifact_dir / "logs")
    env["OPENSQUILLA_TURN_CALL_LOG"] = "0"
    env.setdefault("TERM", "xterm-256color")
    return env


def _host_gateway_config_path(project_root: Path) -> str:
    explicit = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
    if explicit:
        return explicit

    cwd_config = project_root / "opensquilla.toml"
    if cwd_config.is_file():
        return str(cwd_config)

    from opensquilla.paths import default_opensquilla_home  # type: ignore[import-untyped]

    user_config = default_opensquilla_home() / "config.toml"
    return str(user_config) if user_config.is_file() else ""


def _opentui_target(context: TargetContext) -> TuiTarget:
    app_path = Path(__file__).with_name("fake_opentui_app.py")
    app_log = context.artifact_dir / "opentui-app.log"
    env = _base_env(context)
    env.update(
        {
            "OPENSQUILLA_TUI_FAKE_SCENARIO": context.scenario_id,
            "OPENSQUILLA_TUI_FAKE_APP_LOG": str(app_log),
            "OPENSQUILLA_TUI_READY_MARKER": "OPEN_SQUILLA_TUI_READY",
            "OPENSQUILLA_TUI_BACKEND": "opentui",
        }
    )
    return TuiTarget(
        backend_id="opentui",
        command=[sys.executable, "-u", str(app_path)],
        env=env,
        initial_size=context.size,
        readiness_markers=("OPEN_SQUILLA_TUI_READY",),
        log_paths=(app_log,),
        capability_requirements=("real-terminal", "fake-provider", "opentui-footer"),
    )


def _live_opentui_target(context: TargetContext) -> TuiTarget:
    env = _base_env(context, isolate_state=False)
    env.update(
        {
            "OPENSQUILLA_TUI_BACKEND": "opentui",
            "OPENSQUILLA_TUI_READY_MARKER": "OPEN_SQUILLA_TUI_READY",
            "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
            "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
        }
    )
    config_path = _host_gateway_config_path(context.project_root)
    if config_path:
        env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] = config_path
    return TuiTarget(
        backend_id="live-opentui",
        command=[
            sys.executable,
            "-u",
            "-m",
            "opensquilla.cli.main",
            "chat",
            "--standalone",
            "--workspace",
            str(context.project_root),
            "--workspace-strict",
            "--timeout",
            "120",
        ],
        env=env,
        initial_size=context.size,
        readiness_markers=("OPEN_SQUILLA_TUI_READY",),
        log_paths=(context.artifact_dir / "logs",),
        capability_requirements=("real-terminal", "real-cli", "opentui-footer", "tmux"),
    )
