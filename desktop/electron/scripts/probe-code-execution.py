"""Offline packaged smoke: call the real Full-mode execute_code tool twice."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_TITLE = "Packaged Python tool smoke"
_PPTX_CODE = '''import json
import sys
from pptx import Presentation

presentation = Presentation()
slide = presentation.slides.add_slide(presentation.slide_layouts[0])
slide.shapes.title.text = "Packaged Python tool smoke"
presentation.save("python-tool-smoke.pptx")
reopened = Presentation("python-tool-smoke.pptx")
assert len(reopened.slides) == 1
assert reopened.slides[0].shapes.title.text == "Packaged Python tool smoke"
print(json.dumps({"executable": sys.executable, "frozen": bool(getattr(sys, "frozen", False))}))
'''


def _execution_result(raw: str, expected_exit: int) -> dict[str, Any]:
    result = json.loads(raw)
    if (
        not isinstance(result, dict)
        or result.get("exit_code") != expected_exit
        or result.get("timed_out") is not False
    ):
        raise RuntimeError(f"execute_code expected exit {expected_exit}, received {result!r}")
    return result


async def _probe() -> dict[str, Any]:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, reset_runtime
    from opensquilla.tools.builtin.code_exec import execute_code
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    workspace = Path(os.environ["OPENSQUILLA_STATE_DIR"]) / "workspace" / "code-execution"
    workspace.mkdir(parents=True, exist_ok=True)
    # Full grants host access; network_default is not an enforced Full-mode
    # network boundary. Both fixed snippets are local and make no network calls.
    configure_runtime(
        SandboxSettings(run_mode="full", network_default="none"),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="packaged-code-execution-smoke",
            run_mode="full",
            workspace_dir=str(workspace),
        )
    )
    try:
        success = _execution_result(await execute_code(_PPTX_CODE, timeout=15), 0)
        child = json.loads(success["stdout"])
        if (
            Path(child["executable"]).resolve() != Path(sys.executable).resolve()
            or child["frozen"] is not bool(getattr(sys, "frozen", False))
        ):
            raise RuntimeError("execute_code selected a different Python runtime")
        failed = _execution_result(await execute_code("raise SystemExit(7)", timeout=15), 7)
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    from pptx import Presentation

    presentation = Presentation(workspace / "python-tool-smoke.pptx")
    pages = len(presentation.slides)
    title = presentation.slides[0].shapes.title.text if pages == 1 else None
    if pages != 1 or title != _TITLE:
        raise RuntimeError("execute_code did not produce the expected one-slide presentation")
    return {
        "probe": "opensquilla-desktop-code-execution",
        "frozen": bool(getattr(sys, "frozen", False)),
        "pythonExit": success["exit_code"],
        "errorExit": failed["exit_code"],
        "pages": pages,
        "title": title,
    }


async def main() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        result = await _probe()
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
