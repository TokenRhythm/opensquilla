from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

HARNESS_PARENT = Path(__file__).resolve().parents[1]
if str(HARNESS_PARENT) not in sys.path:
    sys.path.insert(0, str(HARNESS_PARENT))

from tui_real_terminal.driver import (  # noqa: E402
    DriverSelection,
    build_run_id,
    open_real_terminal_session,
    probe_terminal_capabilities,
)
from tui_real_terminal.evidence import EvidenceBundle, ScenarioResult  # noqa: E402
from tui_real_terminal.scenarios import TuiScenario, run_scenario  # noqa: E402
from tui_real_terminal.targets import TargetContext, build_tui_target  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--tui-backend",
        action="store",
        default="opentui",
        choices=("opentui", "live-opentui"),
    )
    parser.addoption(
        "--tui-driver",
        action="store",
        default="auto",
        choices=("auto", "tmux", "pty"),
    )
    parser.addoption(
        "--tui-artifact-root",
        action="store",
        default=".artifacts/tui-real-terminal/runs",
    )
    parser.addoption(
        "--tui-require-capabilities",
        action="store_true",
        default=False,
        help="fail instead of skipping when a requested real-terminal capability is missing",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "tui_real_terminal: real terminal TUI integration tests driven through tmux or PTY",
    )
    config.addinivalue_line(
        "markers",
        "tui_gateway_e2e: installed TUI companion with a real deterministic Gateway",
    )


@pytest.fixture
def artifact_root(pytestconfig: pytest.Config) -> Path:
    return Path(str(pytestconfig.getoption("--tui-artifact-root")))


@pytest.fixture
def tui_backend(pytestconfig: pytest.Config) -> str:
    return str(pytestconfig.getoption("--tui-backend"))


@pytest.fixture
def tui_driver(pytestconfig: pytest.Config) -> DriverSelection:
    return cast(DriverSelection, str(pytestconfig.getoption("--tui-driver")))


@pytest.fixture
def run_real_terminal_scenario(
    artifact_root: Path,
    tui_backend: str,
    tui_driver: DriverSelection,
    pytestconfig: pytest.Config,
) -> Callable[[TuiScenario], ScenarioResult]:
    require_capabilities = bool(pytestconfig.getoption("--tui-require-capabilities"))

    def capability_miss(reason: str) -> None:
        if require_capabilities:
            pytest.fail(f"required real-terminal capability is unavailable: {reason}")
        pytest.skip(reason)

    def _run(scenario: TuiScenario) -> ScenarioResult:
        if tui_backend == "live-opentui":
            if scenario.family != "live_prompt":
                pytest.skip("live-opentui backend only runs live_prompt scenarios")
            if os.environ.get("OPENSQUILLA_TUI_LIVE_REAL") != "1":
                pytest.skip("set OPENSQUILLA_TUI_LIVE_REAL=1 to run the real CLI/OpenTUI smoke")

        capabilities = probe_terminal_capabilities()
        if capabilities.preferred_driver == "none":
            capability_miss(capabilities.skip_reason or "real-terminal capabilities unavailable")

        evidence = EvidenceBundle.create(
            artifact_root,
            scenario_id=scenario.scenario_id,
            backend_id=tui_backend,
        )
        target = build_tui_target(
            tui_backend,
            TargetContext(
                project_root=Path.cwd(),
                artifact_dir=evidence.run_dir,
                scenario_id=scenario.scenario_id,
                size=scenario.initial_size,
            ),
        )
        if not target.available:
            capability_miss(target.skip_reason or f"TUI backend {tui_backend!r} unavailable")
        if (
            target.backend_id == "live-opentui"
            and os.environ.get("OPENSQUILLA_TUI_LIVE_REAL") != "1"
        ):
            pytest.skip("set OPENSQUILLA_TUI_LIVE_REAL=1 to run the real CLI/OpenTUI smoke")
        if (
            scenario.required_backend_id is not None
            and target.backend_id != scenario.required_backend_id
        ):
            pytest.skip(
                f"scenario {scenario.scenario_id!r} requires "
                f"--tui-backend={scenario.required_backend_id}"
            )
        if target.backend_id in {"opentui", "live-opentui"}:
            from opensquilla.cli.tui.opentui.bridge import check_opentui_host_available

            availability = check_opentui_host_available()
            if not availability.available:
                pytest.skip(availability.reason or "OpenTUI host unavailable")
        scenario_driver = tui_driver
        if scenario.requires_tmux:
            if not capabilities.tmux_available:
                capability_miss(f"scenario {scenario.scenario_id!r} requires tmux")
            if tui_driver == "pty":
                capability_miss(f"scenario {scenario.scenario_id!r} requires tmux, not PTY")
            scenario_driver = "tmux"

        session = open_real_terminal_session(
            command=target.command,
            cwd=Path.cwd(),
            env=target.env,
            run_id=build_run_id(scenario.scenario_id),
            size=target.initial_size,
            artifact_dir=evidence.run_dir,
            driver=scenario_driver,
        )
        return run_scenario(
            scenario=scenario,
            session=session,
            evidence=evidence,
            backend_id=target.backend_id,
        )

    return _run
