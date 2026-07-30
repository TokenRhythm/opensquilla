from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox import sensitive_paths
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.tools.builtin import patch as patch_tool
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import (
    InteractionMode,
    RetryableToolInputError,
    ToolContext,
    ToolError,
    current_tool_context,
)


def _original_async(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


@pytest.fixture(autouse=True)
def _reset_approval_queue():
    reset_approval_queue()
    yield
    reset_approval_queue()


@pytest.fixture(autouse=True)
def _run_patch_executor_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run_in_executor_inline(self, executor, func, *args):
        future = self.create_future()
        try:
            future.set_result(func(*args))
        except Exception as exc:  # pragma: no cover - exercised by awaiting callers
            future.set_exception(exc)
        return future

    monkeypatch.setattr(
        patch_tool.asyncio.BaseEventLoop,
        "run_in_executor",
        _run_in_executor_inline,
    )


def test_apply_patch_schema_exposes_optional_approval_id() -> None:
    registered = get_default_registry().get("apply_patch")

    assert registered is not None
    assert "approval_id" in registered.spec.parameters
    assert "approval_id" not in registered.spec.required


def test_apply_patch_schema_exposes_structured_elevation_fields() -> None:
    registered = get_default_registry().get("apply_patch")

    assert registered is not None
    params = registered.spec.parameters
    assert params["sandbox_permissions"]["enum"] == [
        "use_default",
        "require_escalated",
    ]
    assert "justification" in params
    assert "prefix_rule" in params


def test_apply_patch_schema_exposes_optional_patch_file_path() -> None:
    registered = get_default_registry().get("apply_patch")

    assert registered is not None
    assert "path" in registered.spec.parameters
    assert "path" not in registered.spec.required


def test_patch_request_preserves_absolute_target_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside" / "target.txt"
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    try:
        request = patch_tool._patch_request(
            {
                "patch": f"""*** Begin Patch
*** Add File: {target.as_posix()}
+created
*** End Patch"""
            }
        )
    finally:
        current_tool_context.reset(token)

    assert request.path == target
    assert request.paths == (target,)
    assert request.root == workspace


@pytest.mark.asyncio
async def test_apply_patch_blocks_sensitive_path(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: .env
+TOKEN=secret
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_blocks_sensitive_key_file_suffix(tmp_path: Path) -> None:
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: id_rsa
+secret
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["sensitive_path"] == "/id_rsa"
    assert not (tmp_path / "id_rsa").exists()


@pytest.mark.asyncio
async def test_apply_patch_blocks_workspace_write_deny_glob(tmp_path: Path) -> None:
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(tmp_path),
            workspace_write_deny_globs=["blocked/**"],
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: blocked/generated.txt
+nope
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["matched_pattern"] == "blocked/**"
    assert not (tmp_path / "blocked" / "generated.txt").exists()


@pytest.mark.asyncio
async def test_apply_patch_accepts_standard_unified_hunk(tmp_path: Path) -> None:
    target = tmp_path / "src" / "feature.py"
    target.parent.mkdir()
    target.write_text("old = 1\nkeep = True\n", encoding="utf-8")
    ctx = ToolContext(workspace_dir=str(tmp_path))
    token = current_tool_context.set(ctx)
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: src/feature.py
@@ -1,2 +1,2 @@
-old = 1
+old = 2
 keep = True
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert target.read_text(encoding="utf-8") == "old = 2\nkeep = True\n"
    assert [entry["relative_path"] for entry in ctx.workspace_file_writes] == [
        "src/feature.py"
    ]


@pytest.mark.asyncio
async def test_apply_patch_accepts_patch_text_from_configured_scratch_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "src" / "feature.py"
    target.parent.mkdir()
    target.write_text("old = 1\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    patch_file = scratch / "fix.patch"
    patch_file.write_text(
        """*** Begin Patch
*** Update File: src/feature.py
@@ -1,1 +1,1 @@
-old = 1
+old = 2
*** End Patch""",
        encoding="utf-8",
    )
    token = current_tool_context.set(
        ToolContext(workspace_dir=str(tmp_path), scratch_dir=str(scratch))
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(path=str(patch_file))
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert target.read_text(encoding="utf-8") == "old = 2\n"


@pytest.mark.asyncio
async def test_apply_patch_notes_docs_and_derived_workspace_writes(tmp_path: Path) -> None:
    docs_target = tmp_path / "docs" / "content" / "manual" / "manual.yml"
    docs_target.parent.mkdir(parents=True)
    docs_target.write_text("old docs\n", encoding="utf-8")
    derived_target = tmp_path / "jq.1.prebuilt"
    derived_target.write_text("old generated\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: docs/content/manual/manual.yml
@@ -1,1 +1,1 @@
-old docs
+new docs
*** Update File: jq.1.prebuilt
@@ -1,1 +1,1 @@
-old generated
+new generated
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result.startswith("Applied patch: 2 file(s) modified")
    assert "documentation file(s) changed" in result
    assert "verify the docs build" in result
    assert "generated or derived-looking file(s) changed" in result
    assert "regenerate/verify" in result


@pytest.mark.asyncio
async def test_apply_patch_rejects_update_without_hunks(tmp_path: Path) -> None:
    target = tmp_path / "src" / "feature.py"
    target.parent.mkdir()
    target.write_text("old = 1\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(RetryableToolInputError, match="did not contain any hunk headers"):
            await apply_patch(
                """*** Begin Patch
*** Update File: src/feature.py
--- a/src/feature.py
+++ b/src/feature.py
-old = 1
+old = 2
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)

    assert target.read_text(encoding="utf-8") == "old = 1\n"


@pytest.mark.asyncio
async def test_apply_patch_context_mismatch_is_model_retriable(tmp_path: Path) -> None:
    target = tmp_path / "src" / "feature.py"
    target.parent.mkdir()
    target.write_text("actual = 1\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(RetryableToolInputError) as exc_info:
            await apply_patch(
                """*** Begin Patch
*** Update File: src/feature.py
@@ -1,1 +1,1 @@
-expected = 1
+actual = 2
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)

    assert "context mismatch" in exc_info.value.user_message
    assert "Read the current file content" in exc_info.value.user_message
    assert target.read_text(encoding="utf-8") == "actual = 1\n"


@pytest.mark.asyncio
async def test_apply_patch_allows_workspace_under_sensitive_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sensitive_paths, "_SENSITIVE_PREFIXES", (str(tmp_path),))
    monkeypatch.setattr(
        sensitive_paths,
        "_WORKSPACE_PARENT_EXCEPTION_MARKERS",
        (str(tmp_path),),
    )
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: docs/plan.md
+hello
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result.startswith("Applied patch: 1 file(s) added")
    assert "documentation file(s) changed" in result
    assert (workspace / "docs" / "plan.md").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_apply_patch_workspace_exception_keeps_leaf_secret_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(sensitive_paths, "_SENSITIVE_PREFIXES", (str(tmp_path),))
    monkeypatch.setattr(
        sensitive_paths,
        "_WORKSPACE_PARENT_EXCEPTION_MARKERS",
        (str(tmp_path),),
    )
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: .env
+TOKEN=secret
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (workspace / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_workspace_escape_blocks_without_sandbox_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "outside_workspace"
    assert outside.read_text(encoding="utf-8") == "old\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_sensitive_path_blocks_even_with_approval_id(tmp_path: Path) -> None:
    approval_id = get_approval_queue().request(
        "exec",
        {
            "toolName": "apply_patch",
            "command": "apply_patch pretend",
            "args": {},
        },
    )
    get_approval_queue().resolve(approval_id, True)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Add File: .env
+TOKEN=secret
*** End Patch""",
            approval_id=approval_id,
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_apply_patch_rejects_foreign_posix_path_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(patch_tool, "os", SimpleNamespace(name="nt"), raising=False)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            await apply_patch(
                """*** Begin Patch
*** Add File: /Users/a1/Desktop/report.txt
+new
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_rejects_foreign_windows_path_on_posix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(patch_tool, "os", SimpleNamespace(name="posix"), raising=False)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(ToolError, match="foreign_host_path"):
            await apply_patch(
                """*** Begin Patch
*** Add File: C:\\Users\\a1\\Desktop\\report.txt
+new
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_apply_patch_elevated_full_skips_outside_workspace_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(ToolContext(workspace_dir=str(workspace), elevated="full"))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result == "Applied patch: 1 file(s) modified"
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_full_host_accepts_absolute_path_outside_patch_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(workspace),
            run_mode="full",
            elevated="full",
            session_key="agent:main:test",
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            f"""*** Begin Patch
*** Update File: {outside.as_posix()}
@@ -1,1 +1,1 @@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    assert result.startswith("Applied patch: 1 file(s) modified")
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_trusted_auto_grants_absolute_user_path_before_root_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    configure_runtime(
        SandboxSettings(run_mode="trusted", backend="noop", allow_legacy_mode=True),
        workspace=workspace,
    )

    async def run_direct(_operation, **_kwargs):
        return None

    monkeypatch.setattr(
        "opensquilla.tools.builtin.filesystem._run_sandbox_operation_if_required",
        run_direct,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(workspace),
            run_mode="trusted",
            session_key="agent:main:trusted-patch",
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            f"""*** Begin Patch
*** Update File: {outside.as_posix()}
@@ -1,1 +1,1 @@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert result.startswith("Applied patch: 1 file(s) modified")
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_run_mode_full_skips_sandbox_wrapper_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(workspace),
            run_mode="full",
            session_key="agent:main:test",
        )
    )
    try:
        result = await patch_tool.apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert result == "Applied patch: 1 file(s) modified"
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_sandbox_disabled_ignores_stale_restricted_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False),
        workspace=workspace,
    )
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            workspace_dir=str(workspace),
            workspace_lockdown=True,
            workspace_write_deny_globs=["**"],
            run_mode="standard",
            session_key="full-patch",
        )
    )
    try:
        await patch_tool.apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_unattended_bypass_blocks_outside_workspace_without_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(workspace),
            elevated="bypass",
            interaction_mode=InteractionMode.UNATTENDED,
        )
    )
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        result = await apply_patch(
            """*** Begin Patch
*** Update File: outside.txt
@@@ -1,1 +1,1 @@@
-old
+new
*** End Patch"""
        )
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "outside_workspace"
    assert outside.read_text(encoding="utf-8") == "old\n"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_apply_patch_add_file_refuses_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("old\n", encoding="utf-8")
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path)))
    apply_patch = _original_async(patch_tool.apply_patch)
    try:
        with pytest.raises(RetryableToolInputError, match="target already exists"):
            await apply_patch(
                """*** Begin Patch
*** Add File: existing.txt
+new
*** End Patch"""
            )
    finally:
        current_tool_context.reset(token)
    assert target.read_text(encoding="utf-8") == "old\n"
