"""Execute explicitly granted scripts without exposing an arbitrary shell."""

from __future__ import annotations

import json
from pathlib import Path

from opensquilla.skills.script_runtime import SkillScriptError, SkillScriptRunner
from opensquilla.tools.registry import tool
from opensquilla.tools.types import SafeToolError, current_tool_context


@tool(
    name="run_skill_script",
    description=(
        "Run a host-granted installed Skill Python script with CLI arguments "
        "in an isolated workspace."
    ),
    params={
        "skill_name": {"type": "string", "description": "Installed Skill name"},
        "script": {"type": "string", "description": "Granted relative scripts/*.py entry"},
        "arguments": {"type": "array", "items": {"type": "string"}, "maxItems": 64},
    },
    required=["skill_name", "script", "arguments"],
    exposed_by_default=False,
    execution_timeout_seconds=130,
)
async def run_skill_script(skill_name: str, script: str, arguments: list[str]) -> str:
    ctx = current_tool_context.get()
    runner = getattr(ctx, "skill_script_runner", None)
    if (
        ctx is None
        or not isinstance(runner, SkillScriptRunner)
        or not ctx.workspace_dir
        or ctx.execution_id != runner.execution_id
        or Path(ctx.workspace_dir).resolve() != runner.workspace
    ):
        raise SafeToolError("No matching host Skill execution grant is active")
    try:
        result = await runner.run(skill_name, script, arguments)
    except (SkillScriptError, OSError, TimeoutError) as error:
        raise SafeToolError(f"Skill script execution failed: {error}") from error
    if result.returncode:
        raise SafeToolError(
            f"Skill script failed with exit {result.returncode}: {result.stderr[:2000]}"
        )
    return json.dumps(
        {
            "exitCode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "startedAt": result.started_at,
            "finishedAt": result.finished_at,
            "skillSha256": result.package_sha256,
        }
    )
