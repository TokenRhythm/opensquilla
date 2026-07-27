from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from opensquilla.tools.builtin import git as git_tool
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import ToolContext, current_tool_context


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def _init_repo_with_generated_file(repo: Path) -> Path:
    target = repo / "src" / "parser.c"
    target.parent.mkdir(parents=True)
    target.write_text(
        "/* A Bison parser, made by GNU Bison 3.0.4.  */\nold\n",
        encoding="utf-8",
    )
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    _run(["git", "add", "src/parser.c"], repo)
    _run(["git", "commit", "-q", "-m", "initial"], repo)
    target.write_text(
        "/* A Bison parser, made by GNU Bison 3.8.2.  */\nnew\n",
        encoding="utf-8",
    )
    return target


def _init_repo_with_test_file(repo: Path) -> Path:
    target = repo / "packages" / "core" / "__tests__" / "feature.spec.ts"
    target.parent.mkdir(parents=True)
    target.write_text("expect(old).toBe(true)\n", encoding="utf-8")
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    _run(["git", "add", "packages/core/__tests__/feature.spec.ts"], repo)
    _run(["git", "commit", "-q", "-m", "initial"], repo)
    target.write_text("expect(newValue).toBe(true)\n", encoding="utf-8")
    return target


@pytest.mark.asyncio
async def test_git_diff_warns_about_generated_files_in_final_diff(tmp_path: Path) -> None:
    _init_repo_with_generated_file(tmp_path)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    git_diff = _original_async(git_tool.git_diff)
    try:
        result = await git_diff(workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)

    assert result.startswith("[patch_hygiene_warning]")
    assert "generated or derived-looking file(s) are present in the final diff" in result
    assert "remove them before final" in result
    assert "diff --git a/src/parser.c b/src/parser.c" in result


@pytest.mark.asyncio
async def test_exec_git_diff_name_only_warns_about_generated_files(
    tmp_path: Path,
) -> None:
    _init_repo_with_generated_file(tmp_path)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    try:
        result = await shell.exec_command("git diff --name-only", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)

    assert result.startswith("exit_code=0\n[patch_hygiene_warning]")
    assert "generated or derived-looking file(s) are present in the final diff" in result
    assert "src/parser.c" in result


@pytest.mark.asyncio
async def test_git_diff_warns_about_test_files_in_final_diff(tmp_path: Path) -> None:
    _init_repo_with_test_file(tmp_path)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    git_diff = _original_async(git_tool.git_diff)
    try:
        result = await git_diff(workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)

    assert result.startswith("[patch_hygiene_warning]")
    assert "test file(s) are present in the final diff" in result
    assert "revert them and put the functional change in source files" in result
    assert "diff --git a/packages/core/__tests__/feature.spec.ts" in result
