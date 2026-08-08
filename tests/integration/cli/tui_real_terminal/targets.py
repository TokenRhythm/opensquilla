from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from tui_real_terminal.driver import TerminalSize

TuiBackendId = Literal["opentui", "live-opentui"]
PACKAGED_GATE_ENV = "OPENSQUILLA_TUI_PACKAGED_GATE"
# A source-host run still pays for a cold Python import, Bun startup, the
# authenticated host handshake, and completion-catalog hydration.  Keep the
# harness budget identical to the packaged gate so machine load cannot turn a
# healthy first screen into a readiness flake.  The gate still requires the
# canonical marker; this only changes how long it waits for that proof.
TUI_READY_TIMEOUT_SECONDS = 15.0


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


def opentui_host_skip_reason(env: Mapping[str, str]) -> str | None:
    """Reason the OpenTUI host ``env`` selects cannot launch, or None when it can.

    Both backends drive the real fd bridge, so without Bun or an installed
    ``@opentui/core`` the app never prints its readiness marker and the driver
    fails on a wait timeout that names neither. These scenarios already skip
    when tmux or the terminal capabilities are missing; a checkout that has not
    run ``bun install`` is the same kind of missing precondition, and
    CONTRIBUTING asks the default path to stay fork-safe.

    The probe has to be asked about the host that will actually launch.
    ``check_opentui_host_available`` defaults ``use_source_host`` to
    ``source_host_requested()``, which reads the *pytest* process environment —
    but the source-host switch lives in the ``env`` handed to the subprocess.
    Probing without it asks about the packaged companion instead, and this
    project does not publish one, so the answer is unavailable no matter how
    the checkout is provisioned. Taking ``env`` keeps the question and the
    launch pointed at the same host.

    Kept separate from ``TuiTarget.available`` so target construction keeps
    describing the target rather than the machine it would run on.
    """
    from opensquilla.cli.tui.opentui.bridge import (  # type: ignore[import-untyped]
        DEFAULT_HOST_PACKAGE_DIR,
        check_opentui_host_available,
    )
    from opensquilla.cli.tui.opentui.host_runtime import (  # type: ignore[import-untyped]
        source_host_requested,
    )
    from opensquilla.cli.tui.renderers.selection import (  # type: ignore[import-untyped]
        RendererBackendUnavailableReason,
    )

    use_source_host = source_host_requested(env)
    runtime_bin: str | None = None
    if use_source_host:
        main_script = DEFAULT_HOST_PACKAGE_DIR / "src" / "main.mjs"
        if not main_script.is_file():
            raise AssertionError(f"OpenTUI source host entrypoint is missing: {main_script}")
        runtime_bin = shutil.which("bun", path=env.get("PATH", os.defpath))
        if runtime_bin is None:
            return "Bun is not installed or is not on PATH"

    availability = check_opentui_host_available(
        runtime_bin=runtime_bin,
        use_source_host=use_source_host,
    )
    if availability.available:
        return None
    if availability.reason_code is not RendererBackendUnavailableReason.MISSING:
        code = availability.reason_code or RendererBackendUnavailableReason.UNKNOWN
        raise AssertionError(
            f"OpenTUI host probe failed with {code.value}: "
            f"{availability.reason or 'no reason provided'}"
        )
    return availability.reason or "OpenTUI host unavailable"


def opentui_host_capability_gate(
    env: Mapping[str, str],
    *,
    require_capabilities: bool,
) -> None:
    """Skip an optional missing host, or fail when capabilities are required."""
    reason = opentui_host_skip_reason(env)
    if reason is None:
        return
    if require_capabilities:
        pytest.fail(f"required real-terminal capability is unavailable: {reason}")
    pytest.skip(reason)


def build_tui_target(backend_id: str, context: TargetContext) -> TuiTarget:
    if backend_id == "opentui":
        return _opentui_target(context)
    if backend_id == "live-opentui":
        return _live_opentui_target(context)
    raise ValueError(f"only opentui is supported; got TUI backend target: {backend_id}")


def _base_env(context: TargetContext, *, isolate_state: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    if env.get(PACKAGED_GATE_ENV) == "1":
        # The pre-rollout packaged-host gate must prove the installed core and
        # companion wheels. A
        # checkout-local PYTHONPATH or source-host override would silently turn
        # this back into a source test.
        env.pop("PYTHONPATH", None)
        env.pop("OPENSQUILLA_TUI_DEV_SOURCE_HOST", None)
        env.pop("BUN_INSTALL", None)
    else:
        src_path = str(context.project_root / "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
        env["OPENSQUILLA_TUI_DEV_SOURCE_HOST"] = "1"
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
            "OPENSQUILLA_TUI_FAKE_PHASE_ACK_DIR": str(
                context.artifact_dir / "phase-acks"
            ),
            "OPENSQUILLA_TUI_READY_MARKER": "OPEN_SQUILLA_TUI_READY",
            "OPENSQUILLA_TUI_BACKEND": "opentui",
        }
    )
    if context.scenario_id in {
        "alternate_screen_mode_loss",
        "complex_ui_state",
        "long_streaming",
        "same_size_eventless_framebuffer_recovery",
        "same_size_eventless_stream_framebuffer_recovery",
        "same_size_framebuffer_recovery",
        "same_size_stream_framebuffer_recovery",
    }:
        # Styled framebuffer assertions use the canonical surface colors as a
        # cell-level contract. Do not inherit a developer's local theme or
        # NO_COLOR setting into this deterministic pre-rollout gate.
        env.update(
            {
                "OPENSQUILLA_TUI_THEME": "opensquilla-dark",
                "OPENSQUILLA_TUI_COLOR": "truecolor",
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
    # Exercise the *default* public CLI policy, even when the parent test
    # process has a compatibility backend override from another launch-contract
    # test. Explicit ``--ui tui`` remains covered by the launch/selection suite;
    # this real-terminal target is the rollout gate for bare ``opensquilla chat``.
    env.pop("OPENSQUILLA_TUI_BACKEND", None)
    env.update(
        {
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
