# ruff: noqa: E402
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HARNESS_PARENT = Path(__file__).resolve().parents[1] / "tests" / "integration" / "cli"
if str(HARNESS_PARENT) not in sys.path:
    sys.path.insert(0, str(HARNESS_PARENT))

from tui_real_terminal.driver import (  # type: ignore[import-not-found]
    build_run_id,
    open_real_terminal_session,
)
from tui_real_terminal.evidence import EvidenceBundle  # type: ignore[import-not-found]
from tui_real_terminal.scenarios import (  # type: ignore[import-not-found]
    all_scenarios,
    run_scenario,
    scenario_by_id,
)
from tui_real_terminal.targets import (  # type: ignore[import-not-found]
    TargetContext,
    build_tui_target,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an OpenSquilla TUI real-terminal scenario."
    )
    parser.add_argument(
        "--scenario",
        choices=[scenario.scenario_id for scenario in all_scenarios()],
        required=True,
    )
    parser.add_argument(
        "--backend",
        choices=("opentui", "live-opentui"),
        default="opentui",
    )
    parser.add_argument("--driver", choices=("auto", "tmux", "pty"), default="auto")
    parser.add_argument(
        "--artifact-root",
        default=".artifacts/tui-real-terminal/runs",
    )
    return parser


def _assert_live_backend_enabled(backend: str) -> None:
    if backend != "live-opentui":
        return
    if os.environ.get("OPENSQUILLA_TUI_LIVE_REAL") == "1":
        return
    raise SystemExit(
        "set OPENSQUILLA_TUI_LIVE_REAL=1 to run the real CLI/OpenTUI smoke"
    )


def _select_driver(
    requested_driver: str,
    *,
    scenario_requires_tmux: bool,
) -> str:
    if scenario_requires_tmux:
        return "tmux"
    return requested_driver


def _run_lab_scenario(args: argparse.Namespace) -> str:
    scenario = scenario_by_id(args.scenario)
    evidence = EvidenceBundle.create(
        Path(args.artifact_root),
        scenario_id=scenario.scenario_id,
        backend_id=args.backend,
    )
    target = build_tui_target(
        args.backend,
        TargetContext(
            project_root=Path.cwd(),
            artifact_dir=evidence.run_dir,
            scenario_id=scenario.scenario_id,
            size=scenario.initial_size,
        ),
    )
    if not target.available:
        raise SystemExit(target.skip_reason or f"backend {args.backend!r} unavailable")
    if (
        scenario.required_backend_id is not None
        and target.backend_id != scenario.required_backend_id
    ):
        raise SystemExit(
            f"scenario {scenario.scenario_id!r} requires "
            f"--backend {scenario.required_backend_id}"
        )
    session = open_real_terminal_session(
        command=target.command,
        cwd=Path.cwd(),
        env=target.env,
        run_id=build_run_id(scenario.scenario_id),
        size=target.initial_size,
        artifact_dir=evidence.run_dir,
        driver=_select_driver(
            args.driver,
            scenario_requires_tmux=scenario.requires_tmux,
        ),
    )
    result = run_scenario(
        scenario=scenario,
        session=session,
        evidence=evidence,
        backend_id=target.backend_id,
    )
    return f"{result.status}: {result.run_dir}"


def main() -> None:
    args = _parser().parse_args()
    _assert_live_backend_enabled(args.backend)
    print(_run_lab_scenario(args))


if __name__ == "__main__":
    main()
