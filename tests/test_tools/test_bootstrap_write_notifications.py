from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from opensquilla.tools.builtin import filesystem
from opensquilla.tools.builtin import patch as patch_tool
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import (
    RetryableToolInputError,
    SafeToolError,
    ToolContext,
    current_tool_context,
)


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


@pytest.mark.asyncio
async def test_filesystem_write_notifies_bootstrap_or_memory_sources(tmp_path) -> None:
    memory_calls: list[tuple[str, str]] = []
    bootstrap_calls: list[tuple[str, str]] = []
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            agent_id="main",
            workspace_dir=str(tmp_path),
            memory_source_dir=str(tmp_path),
            on_memory_source_write=lambda agent_id, path: memory_calls.append((agent_id, path)),
            on_bootstrap_source_write=lambda agent_id, path: bootstrap_calls.append(
                (agent_id, path)
            ),
        )
    )
    write_file = _original_async(filesystem.write_file)
    try:
        await write_file("USER.md", "Name: Alice\n")
        await write_file("MEMORY.md", "# MEMORY\n")
        await write_file("memory/USER.md", "not a bootstrap file\n")
    finally:
        current_tool_context.reset(token)

    assert bootstrap_calls == [("main", "USER.md")]
    assert ("main", "MEMORY.md") in memory_calls
    assert ("main", "memory/USER.md") in memory_calls


@pytest.mark.asyncio
async def test_filesystem_write_tools_record_workspace_writes(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        file_edit_requires_fresh_read=True,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    write_file = _original_async(filesystem.write_file)
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        write_result = await write_file(str(target), "new\n")
        edit_result = await edit_file(str(target), "new\n", "newer\n")
    finally:
        current_tool_context.reset(token)

    assert "workspace file" in write_result
    assert "inspect git_diff" in write_result
    assert "workspace file" in edit_result
    assert [entry["relative_path"] for entry in ctx.workspace_file_writes] == [
        "src/app.py",
        "src/app.py",
    ]


@pytest.mark.asyncio
async def test_edit_file_requires_fresh_full_workspace_read(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        file_edit_requires_fresh_read=True,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        with pytest.raises(RetryableToolInputError) as unread:
            await edit_file(str(target), "value = 1\n", "value = 2\n")

        await read_file(str(target))
        result = await edit_file(str(target), "value = 1\n", "value = 2\n")
    finally:
        current_tool_context.reset(token)

    assert "must read" in unread.value.user_message
    assert "Edited" in result
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_fresh_read_guard_records_runtime_event_for_unread_workspace_edit(
    tmp_path,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        agent_id="agent-1",
        session_key="session-1",
        workspace_dir=str(tmp_path),
        file_edit_requires_fresh_read=True,
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    edit_file = _original_async(filesystem.edit_file)
    try:
        with pytest.raises(RetryableToolInputError):
            await edit_file(str(target), "value = 1\n", "value = 2\n")
    finally:
        current_tool_context.reset(token)

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert events == [
        {
            "feature": "fresh_read_guard",
            "name": "fresh_read_guard.blocked",
            "tool": "edit_file",
            "tool_name": "edit_file",
            "path": str(target),
            "resolved_path": str(target.resolve()),
            "relative_path": "src/app.py",
            "reason": "missing_fresh_read",
            "outcome": "blocked",
            "agent_id": "agent-1",
            "session_key": "session-1",
        }
    ]


@pytest.mark.asyncio
async def test_edit_file_rejects_stale_workspace_read(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        file_edit_requires_fresh_read=True,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        target.write_text("value = 2\n", encoding="utf-8")
        with pytest.raises(RetryableToolInputError) as stale:
            await edit_file(str(target), "value = 2\n", "value = 3\n")
    finally:
        current_tool_context.reset(token)

    assert "changed since it was read" in stale.value.user_message
    assert "read_file" in stale.value.user_message
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_fresh_read_guard_records_runtime_event_for_stale_workspace_edit(
    tmp_path,
) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        agent_id="agent-1",
        session_key="session-1",
        workspace_dir=str(tmp_path),
        file_edit_requires_fresh_read=True,
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        target.write_text("value = 2\n", encoding="utf-8")
        with pytest.raises(RetryableToolInputError):
            await edit_file(str(target), "value = 2\n", "value = 3\n")
    finally:
        current_tool_context.reset(token)

    assert len(events) == 1
    assert events[0]["feature"] == "fresh_read_guard"
    assert events[0]["name"] == "fresh_read_guard.blocked"
    assert events[0]["tool"] == "edit_file"
    assert events[0]["relative_path"] == "src/app.py"
    assert events[0]["reason"] == "stale_fresh_read"
    assert events[0]["outcome"] == "blocked"
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_write_file_existing_workspace_requires_fresh_full_read(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        file_edit_requires_fresh_read=True,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    write_file = _original_async(filesystem.write_file)
    try:
        with pytest.raises(RetryableToolInputError) as unread:
            await write_file(str(target), "value = 2\n")

        await read_file(str(target))
        result = await write_file(str(target), "value = 2\n")
    finally:
        current_tool_context.reset(token)

    assert "must read" in unread.value.user_message
    assert result.startswith("Written ")
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_write_file_allows_new_and_scratch_files_without_workspace_read(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    new_target = workspace / "src" / "new.py"
    scratch_target = scratch / "debug.py"
    scratch_target.write_text("old\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        file_edit_requires_fresh_read=True,
    )
    token = current_tool_context.set(ctx)
    write_file = _original_async(filesystem.write_file)
    try:
        new_result = await write_file(str(new_target), "created = True\n")
        scratch_result = await write_file(str(scratch_target), "debug = True\n")
    finally:
        current_tool_context.reset(token)

    assert new_result.startswith("Written ")
    assert scratch_result.startswith("Written ")
    assert new_target.read_text(encoding="utf-8") == "created = True\n"
    assert scratch_target.read_text(encoding="utf-8") == "debug = True\n"


@pytest.mark.asyncio
async def test_existing_workspace_edit_does_not_require_read_by_default(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    edit_file = _original_async(filesystem.edit_file)
    try:
        result = await edit_file(str(target), "value = 1\n", "value = 2\n")
    finally:
        current_tool_context.reset(token)

    assert "Edited" in result
    assert not any(
        event.get("name") == "fresh_read_guard.blocked"
        or (
            event.get("feature") == "fresh_read_guard"
            and event.get("outcome") == "blocked"
        )
        for event in events
    )
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_filesystem_write_tools_note_docs_and_derived_workspace_writes(
    tmp_path,
) -> None:
    docs_target = tmp_path / "docs" / "content" / "manual" / "manual.yml"
    derived_target = tmp_path / "jq.1.prebuilt"
    docs_target.parent.mkdir(parents=True)
    derived_target.write_text("old\n", encoding="utf-8")
    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    write_file = _original_async(filesystem.write_file)
    edit_file = _original_async(filesystem.edit_file)
    try:
        docs_result = await write_file(str(docs_target), "docs\n")
        await read_file(str(derived_target))
        derived_result = await edit_file(str(derived_target), "old\n", "new\n")
    finally:
        current_tool_context.reset(token)

    assert "documentation file" in docs_result
    assert "verify the docs build" in docs_result
    assert "generated or derived-looking file" in derived_result
    assert "regenerate/verify" in derived_result


@pytest.mark.asyncio
async def test_filesystem_write_tools_note_test_workspace_writes(tmp_path) -> None:
    test_target = tmp_path / "packages" / "core" / "__tests__" / "feature.spec.ts"
    test_target.parent.mkdir(parents=True)
    test_target.write_text("old\n", encoding="utf-8")
    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(test_target))
        result = await edit_file(str(test_target), "old\n", "new\n")
    finally:
        current_tool_context.reset(token)

    assert "test file" in result
    assert "explicitly requested test updates" in result
    assert "revert it before final" in result


@pytest.mark.asyncio
async def test_edit_file_missing_old_text_is_model_retriable(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("actual = 1\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        with pytest.raises(RetryableToolInputError) as exc_info:
            await edit_file(str(target), "expected = 1\n", "expected = 2\n")
    finally:
        current_tool_context.reset(token)

    assert "could not find old_text" in exc_info.value.user_message
    assert "retry with exact text" in exc_info.value.user_message


@pytest.mark.asyncio
async def test_edit_file_ambiguous_old_text_is_model_retriable(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("flag = True\nflag = True\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        with pytest.raises(RetryableToolInputError) as exc_info:
            await edit_file(str(target), "flag = True\n", "flag = False\n")
    finally:
        current_tool_context.reset(token)

    assert "matches 2 locations" in exc_info.value.user_message
    assert "unique surrounding context" in exc_info.value.user_message


@pytest.mark.asyncio
async def test_edit_file_uses_unique_flexible_match_after_exact_miss(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text(
        "def run():\n"
        "    if enabled:\n"
        "        return 1\n",
        encoding="utf-8",
    )
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        result = await edit_file(
            str(target),
            "if enabled:\n    return 1\n",
            "if enabled:\n    return 2\n",
        )
    finally:
        current_tool_context.reset(token)

    assert "Edited" in result
    assert target.read_text(encoding="utf-8") == (
        "def run():\n"
        "    if enabled:\n"
        "        return 2\n"
    )
    assert any(event["name"] == "edit_file.flexible_match_used" for event in events)


@pytest.mark.asyncio
async def test_edit_file_can_disable_flexible_match_recovery(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text(
        "def run():\n"
        "    if enabled:\n"
        "        return 1\n",
        encoding="utf-8",
    )
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        file_edit_flexible_recovery=False,
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        with pytest.raises(RetryableToolInputError) as exc_info:
            await edit_file(
                str(target),
                "if enabled:\n    return 1\n",
                "if enabled:\n    return 2\n",
            )
    finally:
        current_tool_context.reset(token)

    assert "could not find old_text" in exc_info.value.user_message
    assert target.read_text(encoding="utf-8") == (
        "def run():\n"
        "    if enabled:\n"
        "        return 1\n"
    )
    assert any(
        event["name"] == "edit_file.flexible_match_rejected"
        and event["reason"] == "disabled"
        for event in events
    )


@pytest.mark.asyncio
async def test_edit_file_unescapes_old_text_before_flexible_match(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("def run():\n    return 'old'\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        await edit_file(
            str(target),
            "def run():\\n    return 'old'\\n",
            "def run():\n    return 'new'\n",
        )
    finally:
        current_tool_context.reset(token)

    assert target.read_text(encoding="utf-8") == "def run():\n    return 'new'\n"
    event_names = [event["name"] for event in events]
    assert "edit_file.unescape_repair_used" in event_names
    assert "edit_file.flexible_match_used" not in event_names


@pytest.mark.asyncio
async def test_edit_file_rejects_non_unique_flexible_match(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text(
        "if enabled:\n"
        "    return 1\n"
        "if enabled:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    events: list[dict[str, object]] = []
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        on_runtime_event=events.append,
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        with pytest.raises(RetryableToolInputError) as exc_info:
            await edit_file(
                str(target),
                "if enabled:\nreturn 1\n",
                "if enabled:\nreturn 2\n",
            )
    finally:
        current_tool_context.reset(token)

    assert "could not find old_text" in exc_info.value.user_message
    assert any(event["name"] == "edit_file.flexible_match_rejected" for event in events)


@pytest.mark.asyncio
async def test_edit_file_accepts_multiple_precise_replacements(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text(
        "alpha = 1\n"
        "keep = True\n"
        "beta = 2\n",
        encoding="utf-8",
    )
    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        result = await edit_file(
            str(target),
            edits=[
                {"old_text": "alpha = 1\n", "new_text": "alpha = 10\n"},
                {"oldText": "beta = 2\n", "newText": "beta = 20\n"},
            ],
        )
    finally:
        current_tool_context.reset(token)

    assert target.read_text(encoding="utf-8") == (
        "alpha = 10\n"
        "keep = True\n"
        "beta = 20\n"
    )
    assert "applied 2 replacement" in result
    assert [entry["relative_path"] for entry in ctx.workspace_file_writes] == ["src/app.py"]


@pytest.mark.asyncio
async def test_edit_file_recovery_guidance_omits_hidden_apply_patch(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        allowed_tools={
            "exec_command",
            "read_file",
            "edit_file",
            "write_file",
            "glob_search",
            "grep_search",
            "list_dir",
            "git_status",
            "git_diff",
            "retrieve_tool_result",
        },
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    edit_file = _original_async(filesystem.edit_file)
    try:
        await read_file(str(target))
        with pytest.raises(RetryableToolInputError) as exc_info:
            await edit_file(str(target), "missing = True\n", "missing = False\n")
    finally:
        current_tool_context.reset(token)

    assert "could not find old_text" in exc_info.value.user_message
    assert "apply_patch" not in exc_info.value.user_message
    assert "smaller exact" in exc_info.value.user_message


@pytest.mark.asyncio
async def test_write_file_shrink_guard_omits_hidden_apply_patch(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("value = 1\n" * 700, encoding="utf-8")
    ctx = ToolContext(
        is_owner=True,
        workspace_dir=str(tmp_path),
        allowed_tools={
            "exec_command",
            "read_file",
            "edit_file",
            "write_file",
            "glob_search",
            "grep_search",
            "list_dir",
            "git_status",
            "git_diff",
            "retrieve_tool_result",
        },
    )
    token = current_tool_context.set(ctx)
    read_file = filesystem.read_file
    write_file = _original_async(filesystem.write_file)
    try:
        await read_file(str(target))
        with pytest.raises(SafeToolError) as exc_info:
            await write_file(str(target), "value = 2\n")
    finally:
        current_tool_context.reset(token)

    assert "write_file refused to overwrite" in exc_info.value.user_message
    assert "edit_file" in exc_info.value.user_message
    assert "apply_patch" not in exc_info.value.user_message


def test_edit_file_schema_guides_multi_edit_and_numbered_read_output() -> None:
    registered = get_default_registry().get("edit_file")
    assert registered is not None
    spec = registered.spec
    description = spec.description
    edits = spec.parameters["edits"]

    assert spec.required == ["path"]
    assert "edits[]" in description
    assert "read_file without offset or limit" in description
    assert "line-number" in description
    assert "prefer apply_patch" in description
    assert edits["type"] == "array"
    assert "old_text" in edits["items"]["properties"]
    assert "oldText" in edits["items"]["properties"]


def test_coding_tool_descriptions_explain_file_edit_workflow() -> None:
    registry = get_default_registry()

    read_file = registry.get("read_file")
    write_file = registry.get("write_file")
    apply_patch = registry.get("apply_patch")
    exec_command = registry.get("exec_command")
    execute_code = registry.get("execute_code")

    assert read_file is not None
    assert write_file is not None
    assert apply_patch is not None
    assert exec_command is not None
    assert execute_code is not None
    assert "establish fresh edit context" in read_file.spec.description
    assert "Best for new files and scratch files" in write_file.spec.description
    assert "Complete file content" in write_file.spec.parameters["content"]["description"]
    assert "multi-line or larger source edits" in apply_patch.spec.description
    assert "builds, tests" in exec_command.spec.description
    assert "Prefer the file editing tools" in execute_code.spec.description


@pytest.mark.asyncio
async def test_apply_patch_reports_workspace_write_progress(tmp_path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    ctx = ToolContext(is_owner=True, workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: src/app.py
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert "Applied patch" in result
    assert "inspect git_diff" in result


@pytest.mark.asyncio
async def test_patch_notifies_bootstrap_and_memory_sources(tmp_path) -> None:
    memory_calls: list[tuple[str, str]] = []
    bootstrap_calls: list[tuple[str, str]] = []
    (tmp_path / "USER.md").write_text("Name:\n", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "2026-05-01.md").write_text("old\n", encoding="utf-8")
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            agent_id="main",
            workspace_dir=str(tmp_path),
            memory_source_dir=str(tmp_path),
            on_memory_source_write=lambda agent_id, path: memory_calls.append((agent_id, path)),
            on_bootstrap_source_write=lambda agent_id, path: bootstrap_calls.append(
                (agent_id, path)
            ),
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        await apply_patch(
            """*** Begin Patch
*** Update File: USER.md
@@@ -1,1 +1,1 @@@
-Name:
+Name: Alice
*** Update File: memory/2026-05-01.md
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert bootstrap_calls == [("main", "USER.md")]
    assert memory_calls == [("main", "memory/2026-05-01.md")]
