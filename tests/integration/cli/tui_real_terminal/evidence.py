from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from tui_real_terminal.driver import TerminalFrame
from tui_real_terminal.framebuffer import StyledFramebuffer

ScenarioStatus = Literal["pass", "fail"]

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ScenarioFailure:
    step_id: str
    message: str
    elapsed_s: float
    last_screen: str
    artifact_dir: str


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    backend_id: str
    status: ScenarioStatus
    run_dir: Path
    failure: ScenarioFailure | None = None


class EvidenceBundle:
    def __init__(self, run_dir: Path, *, scenario_id: str, backend_id: str) -> None:
        self.run_dir = run_dir
        self.scenario_id = scenario_id
        self.backend_id = backend_id
        self.frames_dir = run_dir / "frames"
        self.framebuffers_dir = run_dir / "framebuffers"
        self.screenshots_dir = run_dir / "screenshots"
        self.transcript_path = run_dir / "transcript.txt"
        self.scrollback_path = run_dir / "scrollback.txt"
        self.terminal_log_path = run_dir / "terminal.log"
        self.app_log_path = run_dir / "app.log"

    @classmethod
    def create(cls, root: Path, *, scenario_id: str, backend_id: str) -> EvidenceBundle:
        run_dir = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}-{scenario_id}"
        frames_dir = run_dir / "frames"
        framebuffers_dir = run_dir / "framebuffers"
        screenshots_dir = run_dir / "screenshots"
        frames_dir.mkdir(parents=True, exist_ok=False)
        framebuffers_dir.mkdir(parents=True, exist_ok=False)
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        bundle = cls(run_dir, scenario_id=scenario_id, backend_id=backend_id)
        for file_path in (
            bundle.terminal_log_path,
            bundle.app_log_path,
            bundle.transcript_path,
            bundle.scrollback_path,
        ):
            file_path.touch()
        return bundle

    def write_scenario(self, payload: dict[str, Any]) -> None:
        self._write_json("scenario.json", payload)

    def record_frame(self, frame: TerminalFrame) -> Path:
        index = len(tuple(self.frames_dir.glob("*.txt")))
        filename = f"{index:03d}-{_safe_name(frame.checkpoint)}.txt"
        frame_path = self.frames_dir / filename
        frame_path.write_text(frame.text, encoding="utf-8")
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {frame.checkpoint} ---\n{frame.text}\n")
        return frame_path

    def write_scrollback(self, frame: TerminalFrame) -> Path:
        self.scrollback_path.write_text(frame.text, encoding="utf-8")
        return self.scrollback_path

    def record_framebuffer(self, frame: StyledFramebuffer) -> tuple[Path, Path]:
        index = len(tuple(self.framebuffers_dir.glob("*.json")))
        stem = f"{index:03d}-{_safe_name(frame.checkpoint)}"
        raw_path = self.framebuffers_dir / f"{stem}.ansi"
        json_path = self.framebuffers_dir / f"{stem}.json"
        raw_path.write_text(frame.raw, encoding="utf-8")
        json_path.write_text(
            json.dumps(frame.to_json_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return raw_path, json_path

    def write_visual_verdict(self, payload: dict[str, Any]) -> Path:
        return self._write_json("visual-verdict.json", payload)

    def write_result(self, result: ScenarioResult) -> Path:
        payload: dict[str, Any] = {
            "scenario_id": result.scenario_id,
            "backend_id": result.backend_id,
            "status": result.status,
            "artifact_dir": str(result.run_dir),
        }
        if result.failure is not None:
            payload["failure"] = asdict(result.failure)
        return self._write_json("result.json", payload)

    def _write_json(self, filename: str, payload: dict[str, Any]) -> Path:
        path = self.run_dir / filename
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path


def _safe_name(value: str) -> str:
    return _SAFE_NAME_RE.sub("-", value.strip()).strip("-") or "frame"
