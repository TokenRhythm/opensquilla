from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.attachment_refs import write_transcript_material
from opensquilla.engine.types import ToolCall
from opensquilla.sandbox import filesystem_worker, sensitive_paths
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.tools.builtin import filesystem as fs
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


@contextmanager
def tool_context(
    workspace: Path,
    *,
    strict: bool = True,
    artifact_media_root: Path | None = None,
    artifact_session_id: str | None = None,
    run_mode: str | None = None,
) -> Iterator[None]:
    token = current_tool_context.set(
        ToolContext(
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            workspace_dir=str(workspace),
            workspace_strict=strict,
            artifact_media_root=str(artifact_media_root) if artifact_media_root else None,
            artifact_session_id=artifact_session_id,
            run_mode=run_mode,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_read_file_offset_limit_does_not_call_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "big.log"
    target.write_text("".join(f"line {i}\n" for i in range(1, 1001)), encoding="utf-8")

    def fail_read_bytes(self: Path) -> bytes:  # pragma: no cover - must not be called
        raise AssertionError("read_bytes should not be used for bounded read_file")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    output = await fs.read_file(str(target), offset=500, limit=2)

    assert "500\tline 500" in output
    assert "501\tline 501" in output
    assert "499\tline 499" not in output
    assert "502\tline 502" not in output


@pytest.mark.asyncio
async def test_read_file_binary_detection_samples_first_8192_bytes(tmp_path: Path) -> None:
    first_sample = tmp_path / "first.txt"
    first_sample.write_bytes(b"abc\x00def")
    with pytest.raises(ToolError, match="NUL"):
        await fs.read_file(str(first_sample), limit=1)

    later_nul = tmp_path / "later.txt"
    later_nul.write_bytes(("ok\n" * 4100).encode("utf-8") + b"\x00tail\n")
    output = await fs.read_file(str(later_nul), offset=1, limit=1)
    assert output == "1\tok\n"


@pytest.mark.asyncio
async def test_read_file_invalid_utf8_before_selected_window_errors(tmp_path: Path) -> None:
    target = tmp_path / "invalid.txt"
    target.write_bytes(b"ok\n\xff\nlater\n")
    with pytest.raises(ToolError, match="not valid UTF-8"):
        await fs.read_file(str(target), offset=3, limit=1)


@pytest.mark.asyncio
async def test_filesystem_sandbox_boundary_attaches_runtime_profile_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = FileSystemPermissionProfile.read_only(readable_roots=(tmp_path,))
    operation = fs.SandboxOperation.filesystem(
        kind="list_dir",
        workspace=tmp_path,
        run_mode="trusted",
        path=Path("/etc"),
    )
    runtime = object()
    result_sentinel = object()
    profile_lookups: list[Path | None] = []
    seen_operations: list[fs.SandboxOperation] = []
    seen_runtime_args: list[tuple[object, bool]] = []
    active_profile = fs.active_file_system_profile

    def lookup_profile(workspace: Path | None) -> FileSystemPermissionProfile | None:
        profile_lookups.append(workspace)
        return active_profile(workspace)

    class _RecordingRuntime:
        def __init__(
            self,
            actual_runtime: object,
            *,
            host_execution_active: bool,
        ) -> None:
            seen_runtime_args.append((actual_runtime, host_execution_active))

        async def run(self, actual_operation: fs.SandboxOperation) -> object:
            seen_operations.append(actual_operation)
            return result_sentinel

    monkeypatch.setattr(fs, "active_file_system_profile", lookup_profile)
    monkeypatch.setattr(fs, "get_runtime", lambda: runtime)
    monkeypatch.setattr(fs, "full_host_access_active", lambda: False)
    monkeypatch.setattr(fs, "SandboxOperationRuntime", _RecordingRuntime)

    with tool_context(tmp_path):
        context = current_tool_context.get()
        assert context is not None
        context.sandbox_file_system_profile = profile
        result = await fs._run_sandbox_operation_if_required(operation)

    assert result is result_sentinel
    assert profile_lookups == [tmp_path]
    assert operation.file_system_profile is None
    assert len(seen_operations) == 1
    assert seen_operations[0] is not operation
    assert seen_operations[0].file_system_profile is profile
    assert seen_runtime_args == [(runtime, False)]


@pytest.mark.asyncio
async def test_filesystem_sandbox_boundary_preserves_existing_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = FileSystemPermissionProfile.workspace(workspace=tmp_path)
    operation = fs.SandboxOperation.filesystem(
        kind="list_dir",
        workspace=tmp_path,
        run_mode="trusted",
        path=tmp_path,
        file_system_profile=profile,
    )
    seen_operations: list[fs.SandboxOperation] = []

    class _RecordingRuntime:
        def __init__(self, _runtime: object, *, host_execution_active: bool) -> None:
            assert host_execution_active is False

        async def run(self, actual_operation: fs.SandboxOperation) -> None:
            seen_operations.append(actual_operation)

    def unexpected_lookup(_workspace: Path | None) -> FileSystemPermissionProfile | None:
        raise AssertionError("existing filesystem profile must not be replaced")

    monkeypatch.setattr(fs, "active_file_system_profile", unexpected_lookup)
    monkeypatch.setattr(fs, "get_runtime", object)
    monkeypatch.setattr(fs, "full_host_access_active", lambda: False)
    monkeypatch.setattr(fs, "SandboxOperationRuntime", _RecordingRuntime)

    await fs._run_sandbox_operation_if_required(operation)

    assert seen_operations == [operation]
    assert seen_operations[0] is operation
    assert seen_operations[0].file_system_profile is profile


@pytest.mark.parametrize(
    ("full_host_access", "explicit_host_execution", "expected"),
    (
        (False, False, False),
        (False, True, True),
        (True, False, True),
    ),
)
@pytest.mark.asyncio
async def test_filesystem_sandbox_boundary_preserves_host_execution_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_host_access: bool,
    explicit_host_execution: bool,
    expected: bool,
) -> None:
    profile = FileSystemPermissionProfile.workspace(workspace=tmp_path)
    operation = fs.SandboxOperation.filesystem(
        kind="list_dir",
        workspace=tmp_path,
        run_mode="trusted",
        path=tmp_path,
        file_system_profile=profile,
    )
    seen_flags: list[bool] = []

    class _RecordingRuntime:
        def __init__(self, _runtime: object, *, host_execution_active: bool) -> None:
            seen_flags.append(host_execution_active)

        async def run(self, _operation: fs.SandboxOperation) -> None:
            return None

    monkeypatch.setattr(fs, "get_runtime", object)
    monkeypatch.setattr(fs, "full_host_access_active", lambda: full_host_access)
    monkeypatch.setattr(fs, "SandboxOperationRuntime", _RecordingRuntime)

    await fs._run_sandbox_operation_if_required(
        operation,
        host_execution_active=explicit_host_execution,
    )

    assert seen_flags == [expected]


@pytest.mark.asyncio
async def test_workspace_strict_allows_inside_workspace(tmp_path: Path) -> None:
    text_file = tmp_path / "inside.txt"
    text_file.write_text("needle\n", encoding="utf-8")
    csv_file = tmp_path / "inside.csv"
    csv_file.write_text("a,b\n1,2\n", encoding="utf-8")

    with tool_context(tmp_path):
        assert "1\tneedle" in await fs.read_file(str(text_file))
        assert "inside.csv" in await fs.read_spreadsheet(str(csv_file))
        assert "inside.txt" in await fs.list_dir(str(tmp_path))
        assert "inside.txt" in await fs.glob_search("*.txt", path=str(tmp_path))
        assert "needle" in await fs.grep_search("needle", path=str(tmp_path))


@pytest.mark.asyncio
async def test_grep_search_skips_vcs_metadata_but_keeps_github(
    tmp_path: Path,
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_text("needle hidden metadata\n", encoding="utf-8")
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    (github_dir / "workflow.yml").write_text("needle workflow\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("needle source\n", encoding="utf-8")

    with tool_context(tmp_path):
        grepped = await fs.grep_search("needle", path=str(tmp_path))
        globbed = await fs.glob_search("**/*", path=str(tmp_path))

    assert ".git/index" not in grepped
    assert ".git/index" not in globbed
    assert "workflow.yml:1: needle workflow" in grepped
    assert "src.py:1: needle source" in grepped


@pytest.mark.asyncio
async def test_grep_search_skips_binary_and_invalid_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("needle source\n", encoding="utf-8")
    (tmp_path / "archive.zip").write_bytes(b"needle binary payload")
    (tmp_path / "invalid.txt").write_bytes(b"needle \xff invalid utf8")

    with tool_context(tmp_path):
        grepped = await fs.grep_search("needle", path=str(tmp_path))

    assert "source.txt:1: needle source" in grepped
    assert "archive.zip" not in grepped
    assert "invalid.txt" not in grepped


@pytest.mark.asyncio
async def test_grep_search_truncates_long_matching_lines(tmp_path: Path) -> None:
    long_tail = "x" * 3000
    (tmp_path / "source.txt").write_text(f"needle {long_tail}\n", encoding="utf-8")

    with tool_context(tmp_path):
        grepped = await fs.grep_search("needle", path=str(tmp_path))

    assert "source.txt:1: needle " in grepped
    assert "line truncated" in grepped
    assert "omitted_chars=" in grepped
    assert long_tail not in grepped


@pytest.mark.asyncio
async def test_grep_search_clamps_large_max_results(tmp_path: Path) -> None:
    lines = [f"needle {i}" for i in range(1100)]
    (tmp_path / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with tool_context(tmp_path):
        grepped = await fs.grep_search("needle", path=str(tmp_path), max_results=5000)

    assert "limit: 1000" in grepped
    assert "has_more: true" in grepped
    result_lines = grepped.split("---\n", 1)[1].splitlines()
    assert len(result_lines) == 1000
    assert "needle 999" in result_lines[-1]
    assert "needle 1000" not in grepped


@pytest.mark.asyncio
async def test_grep_search_supports_offset_and_has_more(tmp_path: Path) -> None:
    lines = [f"needle {i}" for i in range(5)]
    (tmp_path / "source.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with tool_context(tmp_path):
        grepped = await fs.grep_search("needle", path=str(tmp_path), max_results=2, offset=2)

    assert "returned: 2" in grepped
    assert "offset: 2" in grepped
    assert "limit: 2" in grepped
    assert "has_more: true" in grepped
    body = grepped.split("---\n", 1)[1]
    assert "needle 2" in body
    assert "needle 3" in body
    assert "needle 1" not in body
    assert "needle 4" not in body


@pytest.mark.asyncio
async def test_grep_search_allows_explicit_unlimited_and_hidden_line_numbers(
    tmp_path: Path,
) -> None:
    (tmp_path / "source.txt").write_text("needle a\nneedle b\n", encoding="utf-8")

    with tool_context(tmp_path):
        grepped = await fs.grep_search(
            "needle",
            path=str(tmp_path),
            max_results=0,
            include_line_numbers=False,
        )

    assert "limit: unlimited" in grepped
    assert "has_more: false" in grepped
    body = grepped.split("---\n", 1)[1]
    assert "source.txt: needle a" in body
    assert "source.txt:1:" not in body


@pytest.mark.asyncio
async def test_workspace_strict_allows_current_session_material_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    sha, material_path, _wrote = write_transcript_material(
        media_root=media_root,
        session_id="s1",
        payload=b"material body\n",
    )
    assert sha

    with tool_context(
        workspace,
        artifact_media_root=media_root,
        artifact_session_id="s1",
    ):
        output = await fs.read_file(str(material_path))

    assert "1\tmaterial body" in output


@pytest.mark.asyncio
async def test_workspace_strict_blocks_other_session_material_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    _sha, other_material_path, _wrote = write_transcript_material(
        media_root=media_root,
        session_id="s2",
        payload=b"other material\n",
    )

    with tool_context(
        workspace,
        artifact_media_root=media_root,
        artifact_session_id="s1",
    ):
        with pytest.raises(ToolError, match="outside active read roots"):
            await fs.read_file(str(other_material_path))


@pytest.mark.asyncio
async def test_workspace_inside_sensitive_parent_allows_normal_files(
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
    target = workspace / "notes" / "plan.md"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")

    with tool_context(workspace):
        write_gate, elevated = await fs._gate_out_of_workspace_write(
            "write_file",
            target.resolve(),
            "notes/plan.md",
            None,
        )
        read_result = await fs.read_file("notes/plan.md")
        listed = await fs.list_dir("notes")
        globbed = await fs.glob_search("*.md", path="notes")
        grepped = await fs.grep_search("hello", path="notes")

    assert write_gate is None
    assert elevated is False
    assert "1\thello" in read_result
    assert "plan.md" in listed
    assert "plan.md" in globbed
    assert "plan.md:1: hello" in grepped


@pytest.mark.asyncio
async def test_workspace_inside_sensitive_parent_keeps_leaf_secret_blocks(
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

    with tool_context(workspace):
        payload, elevated = await fs._gate_out_of_workspace_write(
            "write_file",
            (workspace / ".env").resolve(),
            ".env",
            None,
        )

    assert payload is not None
    assert elevated is False
    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert not (workspace / ".env").exists()


def test_resolve_path_rejects_foreign_posix_absolute_path_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fs, "os", SimpleNamespace(name="nt"), raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with tool_context(workspace):
        with pytest.raises(ToolError) as exc_info:
            fs._resolve_path("/Users/a1/Desktop/report.pptx")

    message = str(exc_info.value)
    assert "foreign_host_path" in message
    assert "/Users/a1/Desktop/report.pptx" in message
    assert "workspace-relative" in message
    assert "D:\\Users" not in message


@pytest.mark.asyncio
async def test_workspace_strict_blocks_outside_base_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "outside.txt"
    outside_file.write_text("secret\n", encoding="utf-8")
    outside_csv = outside / "outside.csv"
    outside_csv.write_text("a,b\n1,2\n", encoding="utf-8")

    with tool_context(workspace):
        for call in (
            lambda: fs.read_file(str(outside_file)),
            lambda: fs.read_spreadsheet(str(outside_csv)),
            lambda: fs.list_dir(str(outside)),
            lambda: fs.glob_search("*.txt", path=str(outside)),
            lambda: fs.grep_search("secret", path=str(outside)),
        ):
            with pytest.raises(ToolError, match="outside active read roots"):
                await call()


@pytest.mark.asyncio
async def test_full_host_access_allows_reading_outside_active_read_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "outside.txt"
    outside_file.write_text("needle\n", encoding="utf-8")
    outside_csv = outside / "outside.csv"
    outside_csv.write_text("a,b\n1,2\n", encoding="utf-8")

    with tool_context(workspace, run_mode="full"):
        assert "1\tneedle" in await fs.read_file(str(outside_file))
        assert "Workbook: outside.csv" in await fs.read_spreadsheet(str(outside_csv))
        assert "outside.txt" in await fs.list_dir(str(outside))
        assert str(outside_file) in await fs.glob_search("*.txt", path=str(outside))
        assert "outside.txt:1: needle" in await fs.grep_search("needle", path=str(outside))


@pytest.mark.asyncio
async def test_workspace_strict_block_is_actionable_in_tool_failure_envelope(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    handler = build_tool_handler(get_default_registry())

    with tool_context(workspace):
        result = await handler(
            ToolCall(
                tool_use_id="tc-glob-outside",
                tool_name="glob_search",
                arguments={"pattern": "*.txt", "path": str(outside)},
            )
        )

    envelope = json.loads(result.content)

    assert result.is_error is True
    assert envelope["status"] == "error"
    assert envelope["tool"] == "glob_search"
    assert "outside active read roots" in envelope["user_message"]
    assert "internal error" not in envelope["user_message"]
    assert envelope["retry_allowed"] is False


@pytest.mark.asyncio
async def test_workspace_write_deny_block_is_actionable_in_tool_failure_envelope(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=workspace,
    )
    handler = build_tool_handler(get_default_registry())
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            channel_kind="cli",
            channel_id="cli:test",
            elevated="bypass",
            workspace_dir=str(workspace),
            workspace_write_deny_globs=["**/test_*.py"],
        )
    )
    try:
        result = await handler(
            ToolCall(
                tool_use_id="tc-write-deny",
                tool_name="write_file",
                arguments={
                    "path": str(workspace / "test_bug.py"),
                    "content": "print('nope')\n",
                },
            )
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    envelope = json.loads(result.content)

    assert result.is_error is True
    assert envelope["status"] == "error"
    assert envelope["tool"] == "write_file"
    assert "workspace write deny policy" in envelope["user_message"]
    assert "internal error" not in envelope["user_message"]
    assert envelope["retry_allowed"] is False
    assert not (workspace / "test_bug.py").exists()


@pytest.mark.asyncio
async def test_sandbox_disabled_write_ignores_stale_restricted_tool_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside" / "probe.txt"
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False),
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            workspace_strict=True,
            workspace_lockdown=True,
            workspace_write_deny_globs=["**"],
            run_mode="standard",
            session_key="full-write",
        )
    )
    try:
        await fs.write_file(str(outside), "ok\n")
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert outside.read_text(encoding="utf-8") == "ok\n"


@pytest.mark.asyncio
async def test_workspace_strict_blocks_nonexistent_outside_before_not_found(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_missing = tmp_path / "outside" / "missing.txt"
    outside_missing_dir = tmp_path / "outside" / "missing-dir"

    with tool_context(workspace):
        for call in (
            lambda: fs.read_file(str(outside_missing)),
            lambda: fs.list_dir(str(outside_missing_dir)),
            lambda: fs.glob_search("*.txt", path=str(outside_missing_dir)),
            lambda: fs.grep_search("needle", path=str(outside_missing_dir)),
        ):
            with pytest.raises(ToolError, match="outside active read roots"):
                await call()


@pytest.mark.asyncio
async def test_workspace_strict_disabled_allows_outside_read_when_not_sensitive(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    with tool_context(workspace, strict=False):
        assert "outside" in await fs.read_file(str(outside))


def _make_symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unsupported/unavailable: {exc}")


@pytest.mark.asyncio
async def test_workspace_strict_blocks_read_file_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    link = workspace / "link.txt"
    _make_symlink(link, outside)

    with tool_context(workspace):
        with pytest.raises(ToolError, match="outside active read roots"):
            await fs.read_file(str(link))


@pytest.mark.asyncio
async def test_workspace_strict_surfaces_list_dir_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    link = workspace / "link.txt"
    _make_symlink(link, outside)

    with tool_context(workspace):
        output = await fs.list_dir(str(workspace))

    assert "[blocked]" in output
    assert "outside active read roots" in output


@pytest.mark.asyncio
async def test_list_dir_survives_broken_symlink(tmp_path: Path) -> None:
    ordinary = tmp_path / "ok.txt"
    ordinary.write_text("hello\n", encoding="utf-8")
    _make_symlink(tmp_path / "dangling", tmp_path / "missing-target")

    with tool_context(tmp_path):
        output = await fs.list_dir(str(tmp_path))

    assert f"[file] ok.txt ({ordinary.stat().st_size} bytes)" in output
    assert "[link] dangling (broken symlink)" in output


@pytest.mark.asyncio
async def test_list_dir_host_fallback_marks_child_lstat_error_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("secret", encoding="utf-8")
    original_lstat = Path.lstat

    def selective_lstat(path: Path):
        if path == blocked:
            raise PermissionError("child denied for test")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", selective_lstat)

    with tool_context(tmp_path):
        output = await fs.list_dir(str(tmp_path))

    assert "[file] ok.txt (5 bytes)" in output
    assert "[file] blocked.txt (metadata unavailable)" in output


@pytest.mark.asyncio
async def test_list_dir_host_fallback_does_not_follow_symlink_after_policy_resolve_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    escape = workspace / "escape"
    _make_symlink(escape, outside)
    original_resolve = Path.resolve
    original_stat = Path.stat

    def selective_resolve(path: Path, strict: bool = False) -> Path:
        if path == escape:
            raise PermissionError("policy resolution denied for test")
        return original_resolve(path, strict=strict)

    def reject_target_stat(path: Path, *args: object, **kwargs: object):
        if path == escape and kwargs.get("follow_symlinks", True):
            raise AssertionError("failed policy resolution must not follow target")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", selective_resolve)
    monkeypatch.setattr(Path, "stat", reject_target_stat)

    with tool_context(workspace):
        output = await fs.list_dir(str(workspace))

    assert "[file] ok.txt (5 bytes)" in output
    assert "[link] escape (target metadata unavailable)" in output
    assert "[link] escape (6 bytes target)" not in output


@pytest.mark.asyncio
async def test_list_dir_host_fallback_propagates_policy_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "child.txt").write_text("hello", encoding="utf-8")

    def fail_policy(*_args: object, **_kwargs: object) -> str | None:
        raise RuntimeError("policy bug for test")

    monkeypatch.setattr(fs, "_workspace_strict_candidate_marker", fail_policy)

    with tool_context(tmp_path):
        with pytest.raises(RuntimeError, match="policy bug for test"):
            await fs.list_dir(str(tmp_path))


@pytest.mark.asyncio
async def test_list_dir_host_fallback_preserves_directory_iterdir_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_iterdir = Path.iterdir

    def selective_iterdir(path: Path):
        if path == tmp_path:
            raise PermissionError("directory denied for test")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", selective_iterdir)

    with tool_context(tmp_path):
        with pytest.raises(PermissionError, match="directory denied for test"):
            await fs.list_dir(str(tmp_path))


@pytest.mark.asyncio
async def test_list_dir_valid_symlink_matches_worker_output(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = tmp_path / "valid-link"
    _make_symlink(link, target)

    worker_result = filesystem_worker._list_dir({"path": str(tmp_path)})
    with tool_context(tmp_path):
        host_output = await fs.list_dir(str(tmp_path))

    expected = "[link] valid-link (6 bytes target)"
    assert expected in worker_result["message"]
    assert expected in host_output


@pytest.mark.asyncio
async def test_list_dir_symlink_loop_matches_worker_output(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    _make_symlink(loop, loop)

    worker_result = filesystem_worker._list_dir({"path": str(tmp_path)})
    with tool_context(tmp_path):
        host_output = await fs.list_dir(str(tmp_path))

    expected = "[link] loop (broken symlink)"
    assert expected in worker_result["message"]
    assert expected in host_output


@pytest.mark.asyncio
async def test_list_dir_host_fallback_distinguishes_unreadable_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "protected-link"
    _make_symlink(link, target)
    original_stat = Path.stat

    def selective_stat(path: Path, *args: object, **kwargs: object):
        if path == link and kwargs.get("follow_symlinks", True):
            raise PermissionError("target blocked for test")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", selective_stat)

    with tool_context(tmp_path):
        output = await fs.list_dir(str(tmp_path))

    assert "[link] protected-link (target metadata unavailable)" in output
    assert "[link] protected-link (broken symlink)" not in output


@pytest.mark.asyncio
async def test_list_dir_full_host_tolerates_broken_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    listing = tmp_path / "listing"
    listing.mkdir()
    _make_symlink(listing / ".VolumeIcon.icns", listing / "missing.icns")

    with tool_context(workspace, run_mode="full"):
        output = await fs.list_dir(str(listing))

    assert "[link] .VolumeIcon.icns (broken symlink)" in output


@pytest.mark.asyncio
async def test_workspace_strict_surfaces_glob_and_grep_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("needle\n", encoding="utf-8")
    link = workspace / "link.txt"
    _make_symlink(link, outside)

    with tool_context(workspace):
        globbed = await fs.glob_search("*.txt", path=str(workspace))
        grepped = await fs.grep_search("needle", path=str(workspace))

    assert "[blocked]" in globbed
    assert "outside active read roots" in globbed
    assert "[blocked]" in grepped
    assert "outside active read roots" in grepped


@pytest.mark.asyncio
async def test_sensitive_path_priority_over_workspace_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("secret\n", encoding="utf-8")

    monkeypatch.setattr(
        "opensquilla.sandbox.sensitive_paths.is_sensitive_path",
        lambda path: "/secret" if "secret" in path else None,
    )

    with tool_context(workspace):
        file_result = json.loads(await fs.read_file(str(outside_file)))
        dir_result = json.loads(await fs.list_dir(str(outside)))

    assert file_result["reason"] == "sensitive_path"
    assert dir_result["reason"] == "sensitive_path"
    assert "workspace_strict" not in file_result.get("message", "")
    assert "workspace_strict" not in dir_result.get("message", "")
