from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.tools.builtin  # noqa: F401 - registers built-in tools
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.tools.builtin.ptc_run import (
    _PTC_DEFAULT_ALLOW,
    _execute_program,
    _normalize_program_body,
    _ToolsNamespace,
)
from opensquilla.tools.registry import get_default_registry

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PTC_SKILLS_DIR = _REPO_ROOT / "src" / "opensquilla" / "skills" / "exp"


def test_ptc_default_allowlist_only_references_registered_tools() -> None:
    registered = set(get_default_registry().list_names())

    assert _PTC_DEFAULT_ALLOW <= registered
    assert "write_file" not in _PTC_DEFAULT_ALLOW
    assert "edit_file" not in _PTC_DEFAULT_ALLOW


def test_meta_ptc_runner_dependencies_match_static_allowlist(tmp_path: Path) -> None:
    loader = SkillLoader(
        bundled_dir=_REPO_ROOT / "src" / "opensquilla" / "skills" / "bundled",
        extra_dirs=[_PTC_SKILLS_DIR],
        snapshot_path=tmp_path / "skills.json",
    )
    loader.invalidate_cache()
    loader.load_all()

    spec = loader.get_by_name("meta-ptc-runner")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None

    run_program = next(step for step in plan.steps if step.id == "run_program")
    allowed = set(run_program.tool_args["allowed_tools"])
    required = set(spec.requires_tools)
    registered = set(get_default_registry().list_names())

    assert allowed <= registered
    assert required == allowed | {"ptc_run"}
    assert {"read_file", "write_file", "edit_file", "glob_search"} <= allowed
    assert {"memory_get", "memory_save"}.isdisjoint(allowed)


@pytest.mark.asyncio
async def test_ptc_run_uses_registered_default_allowlist() -> None:
    from opensquilla.tools.builtin.ptc_run import ptc_run

    payload = json.loads(
        await ptc_run(
            code="return {'allowed': tools.list()}",
            description="List default PTC tools",
        )
    )

    assert payload["status"] == "ok"
    assert set(payload["result"]["allowed"]) == _PTC_DEFAULT_ALLOW
    assert "write_file" not in payload["result"]["allowed"]


@pytest.mark.asyncio
async def test_ptc_sdk_exposes_asyncio_and_tool_call_error() -> None:
    calls: list[str] = []

    async def handler(call: object) -> SimpleNamespace:
        path = call.arguments["path"]  # type: ignore[attr-defined]
        calls.append(path)
        if path == "broken.md":
            return SimpleNamespace(is_error=True, content="file not found")
        return SimpleNamespace(is_error=False, content=f'"content for {path}"')

    namespace = _ToolsNamespace(handler=handler, allowed=frozenset({"read_file"}))
    logs, result, _elapsed, status, message = await _execute_program(
        """
async def read_one(path):
    try:
        value = await tools.read_file(path=path)
        return {"path": path, "value": value}
    except ToolCallError as exc:
        return {"path": path, "error": exc.message, "tool": exc.tool_name}

rows = await asyncio.gather(*(read_one(path) for path in ["one.md", "broken.md", "two.md"]))
return {"rows": rows, "failures": sum("error" in row for row in rows)}
""",
        namespace,
        timeout=5,
    )

    assert logs == []
    assert status == "ok"
    assert message == ""
    assert calls == ["one.md", "broken.md", "two.md"]
    assert result == {
        "rows": [
            {"path": "one.md", "value": "content for one.md"},
            {"path": "broken.md", "error": "file not found", "tool": "read_file"},
            {"path": "two.md", "value": "content for two.md"},
        ],
        "failures": 1,
    }


def test_normalize_program_body_strips_fence_and_entry_def() -> None:
    code = "```python\nasync def main(tools):\n    return 1\n```"
    assert _normalize_program_body(code) == "return 1"


def test_normalize_program_body_keeps_helper_definitions() -> None:
    code = (
        "async def read_one(path):\n"
        "    return await tools.read_file(path=path)\n"
        "return await read_one('a.md')\n"
    )
    normalized = _normalize_program_body(code)
    assert normalized.startswith("async def read_one(path):")
    assert "return await read_one('a.md')" in normalized


def test_normalize_program_body_dedents_uniform_indent() -> None:
    assert _normalize_program_body("    return 1\n    return 2") == "return 1\nreturn 2"


@pytest.mark.asyncio
async def test_ptc_run_strips_fence_and_def_line() -> None:
    from opensquilla.tools.builtin.ptc_run import ptc_run

    payload = json.loads(
        await ptc_run(
            code="```python\nasync def main(tools):\n    return 7\n```",
            description="Fenced + def line",
        )
    )
    assert payload["status"] == "ok"
    assert payload["result"] == 7


@pytest.mark.asyncio
async def test_ptc_run_injects_safe_stdlib_globals() -> None:
    from opensquilla.tools.builtin.ptc_run import ptc_run

    code = (
        "data = json.loads('{\"x\": 3}')\n"
        'return {"x": data["x"], "count": collections.Counter("aab")["a"]}'
    )
    payload = json.loads(await ptc_run(code=code, description="stdlib globals"))
    assert payload["status"] == "ok"
    assert payload["result"] == {"x": 3, "count": 2}


@pytest.mark.asyncio
async def test_ptc_run_compile_error_reports_source_line() -> None:
    from opensquilla.tools.builtin.ptc_run import ptc_run

    payload = json.loads(
        await ptc_run(
            code="return 1\n    return 2",
            description="Broken indentation",
        )
    )
    assert payload["status"] == "error"
    assert "line" in payload["error"]
    assert "return 2" in payload["error"]
