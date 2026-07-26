from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, get_runtime, reset_runtime
from opensquilla.sandbox.operation_runtime import SandboxOperation, SandboxOperationResult
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import SandboxRequest
from opensquilla.tools.builtin import filesystem as fs
from opensquilla.tools.builtin import patch as patch_tool
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


class _InlineExecutorLoop:
    async def run_in_executor(self, executor: object, func: object, *args: object) -> object:
        return func(*args)  # type: ignore[operator]


class _FilesystemBackend:
    name = "filesystem_backend"

    def operation_domains_supported(self) -> frozenset[str]:
        return frozenset({"filesystem"})

    async def run_operation(self, operation: SandboxOperation) -> SandboxOperationResult:
        request = getattr(operation, "request", None)
        path = getattr(request, "path", None)
        if path is None:
            raise AssertionError("filesystem operation missing path")
        if operation.kind == "read_file":
            if not path.exists():
                raise FileNotFoundError(f"File not found: {path}")
            return SandboxOperationResult(message=path.read_text(encoding="utf-8"))
        if operation.kind == "list_dir":
            if not path.exists():
                raise FileNotFoundError(f"Path not found: {path}")
            entries = []
            for entry in sorted(path.iterdir(), key=lambda item: item.name):
                if entry.is_dir():
                    entries.append(f"[dir]  {entry.name}/")
                else:
                    entries.append(f"[file] {entry.name} ({entry.stat().st_size} bytes)")
            return SandboxOperationResult(
                message="\n".join(entries) if entries else f"{path}: (empty directory)"
            )
        if operation.kind == "write_text":
            created = not path.exists()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(request.content, encoding="utf-8")
            return SandboxOperationResult(
                message=f"Written {len(request.content)} bytes to {path}",
                created=created,
            )
        if operation.kind == "edit_text":
            original = path.read_text(encoding="utf-8")
            updated = original.replace(request.old_text, request.new_text, 1)
            path.write_text(updated, encoding="utf-8")
            return SandboxOperationResult(
                message=(
                    f"Edited {path}: replaced {len(request.old_text)} chars "
                    f"with {len(request.new_text)} chars"
                )
            )
        if operation.kind == "grep_search":
            matches = []
            for entry in sorted(path.rglob("*")):
                if entry.is_symlink() or not entry.is_file():
                    continue
                try:
                    text = entry.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if request.pattern in line:
                        matches.append(f"{entry}:{line_no}:{line}")
            return SandboxOperationResult(
                message="\n".join(matches) if matches else "No matches"
            )
        raise AssertionError(f"unsupported filesystem operation: {operation.kind}")


def _install_filesystem_read_backend() -> None:
    runtime = get_runtime()
    assert runtime is not None
    runtime.backend = _FilesystemBackend()


@contextmanager
def tool_context(
    workspace: Path,
    *,
    run_mode: str | None = "standard",
    sandbox_mounts: list[dict[str, object]] | None = None,
    workspace_strict: bool = False,
) -> Iterator[ToolContext]:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(workspace),
        workspace_strict=workspace_strict,
        run_mode=run_mode,
        session_key="s1",
        sandbox_mounts=sandbox_mounts or [],
    )
    token = current_tool_context.set(ctx)
    try:
        yield ctx
    finally:
        current_tool_context.reset(token)


@pytest.fixture(autouse=True)
def sandbox_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from opensquilla.application import approval_queue as approval_queue_mod

    monkeypatch.setattr(
        approval_queue_mod,
        "_DEFAULT_APPROVAL_QUEUE_PATH",
        tmp_path / "approval_queue.sqlite",
    )
    reset_approval_queue()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    runtime = configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            # Most tests in this module exercise the workspace boundary.  Keep
            # Codex's writable /tmp behavior covered explicitly below.
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
        workspace=workspace,
    )
    runtime.backend = _FilesystemBackend()
    try:
        yield
    finally:
        reset_approval_queue()
        reset_runtime()


def _disable_global_root_readonly() -> None:
    runtime = get_runtime()
    assert runtime is not None
    runtime.settings.host_root_readonly = False


def test_normal_sibling_path_requests_ro_mount(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling" / "notes.txt"

    decision = decide_path_access(sibling, workspace=workspace)

    assert decision.status == "request"
    assert decision.access == "ro"
    assert decision.normalized_path == str(sibling.resolve(strict=False))


def test_readonly_root_allows_ssh_path_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = Path.home() / ".ssh" / "id_rsa"
    root = Path(target.anchor)

    decision = decide_path_access(
        target,
        workspace=workspace,
        mounts=({"path": str(root), "access": "ro"},),
    )

    assert decision.status == "allowed"


def test_readonly_root_allows_ordinary_etc_reads_but_not_writes(tmp_path: Path) -> None:
    root = Path(tmp_path.anchor)
    target = root / "etc" / "hosts"
    shadow_target = root / "etc" / "shadow"
    mounts = ({"path": str(root), "access": "ro"},)

    read = decide_path_access(
        target,
        workspace=tmp_path / "workspace",
        mounts=mounts,
    )
    write = decide_path_access(
        target,
        workspace=tmp_path / "workspace",
        mounts=mounts,
        write=True,
    )
    shadow = decide_path_access(
        shadow_target,
        workspace=tmp_path / "workspace",
        mounts=mounts,
    )

    assert read.status == "allowed"
    assert read.access == "ro"
    assert write.status == "request"
    assert write.access == "rw"
    assert shadow.status == "allowed"


def test_readonly_root_mount_allows_root_directory_read_but_blocks_write(
    tmp_path: Path,
) -> None:
    root = Path(tmp_path.anchor)
    mounts = ({"path": str(root), "access": "ro"},)

    read = decide_path_access(
        root,
        workspace=tmp_path / "workspace",
        mounts=mounts,
    )
    write = decide_path_access(
        root,
        workspace=tmp_path / "workspace",
        mounts=mounts,
        write=True,
    )

    assert read.status == "allowed"
    assert read.access == "ro"
    assert write.status == "request"
    assert write.reason == "mount_requires_write_access"


def test_workspace_child_is_allowed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "src" / "app.py"

    decision = decide_path_access(target, workspace=workspace)

    assert decision.status == "allowed"
    assert decision.access == "ro"


def test_default_container_workspace_child_is_allowed_before_root_block() -> None:
    workspace = "/root/.opensquilla/workspace"
    target = "/root/.opensquilla/workspace/project/src/app.py"

    decision = decide_path_access(target, workspace=workspace)

    assert decision.status == "allowed"
    assert decision.access == "ro"


def test_dotenv_inside_default_container_workspace_is_profile_readable() -> None:
    workspace = "/root/.opensquilla/workspace"
    target = "/root/.opensquilla/workspace/project/.env.local"

    decision = decide_path_access(target, workspace=workspace)

    assert decision.status == "allowed"


def test_explicit_denied_read_profile_still_blocks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "secret" / "token"
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        denied_read_roots=(tmp_path / "secret",),
    )

    decision = decide_path_access(
        target,
        workspace=workspace,
        profile=profile,
    )

    assert decision.status == "blocked"
    assert decision.reason == "denied_read"


def test_write_request_asks_for_rw_mount(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling" / "notes.txt"

    decision = decide_path_access(sibling, workspace=workspace, write=True)

    assert decision.status == "request"
    assert decision.access == "rw"


def test_request_path_builds_structured_mount_escalation_choices(tmp_path: Path) -> None:
    from opensquilla.sandbox.escalation import build_path_approval_params

    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling" / "notes.txt"
    decision = decide_path_access(sibling, workspace=workspace, write=True)

    proposal = build_path_approval_params(
        decision,
        session_key="agent:main:webchat:abc",
        workspace=str(workspace),
    )

    assert proposal is not None
    assert proposal["approvalKind"] == "sandbox_path"
    assert proposal["path"] == str(sibling.resolve(strict=False))
    assert proposal["access"] == "rw"
    assert [choice["id"] for choice in proposal["choices"]] == [
        "allow_once",
        "allow_same_type",
        "deny",
    ]
    assert [choice["label"] for choice in proposal["choices"]] == [
        "Allow once",
        "Allow same type",
        "Deny",
    ]
    assert proposal["choices"][0]["style"] == "primary"


def test_unmounted_root_read_can_request_a_mount_grant(tmp_path: Path) -> None:
    from opensquilla.sandbox.escalation import build_path_approval_params

    workspace = tmp_path / "workspace"
    decision = decide_path_access(
        Path(tmp_path.anchor), workspace=workspace, write=False
    )

    assert decision.status == "request"
    assert build_path_approval_params(
        decision,
        session_key="agent:main:webchat:abc",
        workspace=str(workspace),
    ) is not None


def test_most_specific_rw_mount_allows_write_under_ro_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    parent = tmp_path / "parent"
    child = parent / "child"
    target = child / "out.txt"

    decision = decide_path_access(
        target,
        workspace=workspace,
        mounts=[
            {"path": str(parent), "access": "ro"},
            {"path": str(child), "access": "rw"},
        ],
        write=True,
    )

    assert decision.status == "allowed"
    assert decision.access == "rw"


def test_most_specific_ro_mount_requests_write_under_rw_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    parent = tmp_path / "parent"
    child = parent / "child"
    target = child / "out.txt"

    decision = decide_path_access(
        target,
        workspace=workspace,
        mounts=[
            {"path": str(parent), "access": "rw"},
            {"path": str(child), "access": "ro"},
        ],
        write=True,
    )

    assert decision.status == "request"
    assert decision.access == "rw"


@pytest.mark.asyncio
async def test_existing_ro_mount_allows_filesystem_read_and_list(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    missing_file = mounted / "missing.txt"
    missing_dir = mounted / "missing-dir"

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(mounted), "access": "ro"}],
    ):
        with pytest.raises(FileNotFoundError):
            await fs.read_file(str(missing_file))
        with pytest.raises(FileNotFoundError):
            await fs.list_dir(str(missing_dir))


@pytest.mark.asyncio
async def test_existing_ro_mount_allows_list_dir_when_workspace_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(mounted), "access": "ro"}],
        workspace_strict=True,
    ):
        result = await fs.list_dir(str(mounted))

    assert "notes.txt" in result


@pytest.mark.asyncio
async def test_filesystem_read_outside_workspace_uses_global_readonly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"
    outside.parent.mkdir()
    outside.write_text("outside body\n", encoding="utf-8")

    _install_filesystem_read_backend()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace) as ctx:
        result = await fs.read_file(str(outside))

    assert "outside body" in result
    assert ctx.sandbox_mounts == []
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_filesystem_list_root_uses_global_readonly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    _install_filesystem_read_backend()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace, workspace_strict=True):
        result = await fs.list_dir(str(tmp_path))

    assert '"status": "blocked"' not in result
    assert "[dir]" in result


@pytest.mark.asyncio
async def test_filesystem_reads_dot_credential_names_through_readonly_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    credential = tmp_path / "outside" / ".ssh" / "id_rsa"
    credential.parent.mkdir(parents=True)
    credential.write_text("test fixture body\n", encoding="utf-8")

    _install_filesystem_read_backend()
    _disable_global_root_readonly()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace, workspace_strict=True) as ctx:
        ctx.sandbox_file_system_profile = FileSystemPermissionProfile.read_only(
            readable_roots=(tmp_path,),
            host_root_readonly=False,
        )
        result = await fs.read_file(str(credential))

    assert "test fixture body" in result


@pytest.mark.asyncio
async def test_shell_reads_dot_credential_names_inside_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    credential = tmp_path / "outside" / ".ssh" / "id_rsa"
    credential.parent.mkdir(parents=True)
    credential.write_text("test fixture body\n", encoding="utf-8")
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(
            stdout="test fixture body\n",
            stderr="",
            returncode=0,
            backend_notes=[],
        )

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, workspace_strict=True):
        result = await shell.exec_command(f"cat {credential}")

    assert "test fixture body" in result
    assert len(backend_calls) == 1


@pytest.mark.asyncio
async def test_denied_sandbox_path_request_does_not_create_repeated_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    from types import SimpleNamespace

    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace):
        first = json.loads(await fs.list_dir(str(outside)))

    assert first["status"] == "approval_required"
    assert first["approvalKind"] == "sandbox_path"
    approval_id = first["approval_id"]
    assert len(get_approval_queue().list_pending("exec")) == 1

    await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": False, "choice": "deny"},
        SimpleNamespace(session_manager=None, config=None),
    )

    with tool_context(workspace):
        second = json.loads(await fs.list_dir(str(outside)))

    assert second["status"] == "approval_denied"
    assert second["approval_id"] == approval_id
    assert "user denied" in second["message"].lower()
    assert "do not ask" in second["message"].lower()
    assert "Add the requested path" not in second["message"]
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_denied_sandbox_path_request_can_be_requested_again_next_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    from types import SimpleNamespace

    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import clear_sandbox_approval_denials

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace):
        first = json.loads(await fs.list_dir(str(outside)))

    assert first["status"] == "approval_required"
    approval_id = first["approval_id"]

    await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": False, "choice": "deny"},
        SimpleNamespace(session_manager=None, config=None),
    )

    with tool_context(workspace):
        same_turn = json.loads(await fs.list_dir(str(outside)))

    assert same_turn["status"] == "approval_denied"
    assert same_turn["approval_id"] == approval_id

    clear_sandbox_approval_denials("s1")

    with tool_context(workspace):
        next_turn = json.loads(await fs.list_dir(str(outside)))

    assert next_turn["status"] == "approval_required"
    assert next_turn["approval_id"] != approval_id


@pytest.mark.asyncio
async def test_denied_sandbox_path_request_clears_duplicate_pending_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    from types import SimpleNamespace

    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace):
        first = json.loads(await fs.list_dir(str(outside)))
        second = json.loads(await fs.list_dir(str(outside)))

    assert first["status"] == "approval_required"
    assert second["status"] == "approval_pending"
    assert second["approval_id"] == first["approval_id"]
    assert len(get_approval_queue().list_pending("exec")) == 1

    await _handle_exec_approval_resolve(
        {"id": first["approval_id"], "approved": False, "choice": "deny"},
        SimpleNamespace(session_manager=None, config=None),
    )

    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_filesystem_write_outside_workspace_requires_explicit_elevation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"

    with tool_context(workspace):
        payload = json.loads(await fs.write_file(str(outside), "outside body\n"))

    assert payload["status"] == "elevation_required"
    assert payload["path"] == str(outside.resolve(strict=False))
    assert get_approval_queue().list_pending("exec") == []
    assert not outside.exists()


@pytest.mark.asyncio
async def test_direct_and_shell_external_writes_share_elevation_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("external writes must stop before backend execution")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace):
        direct = json.loads(await fs.write_file(str(outside), "outside body\n"))
        command = json.loads(await shell.exec_command(f"printf test > {outside}"))

    assert direct["status"] == "elevation_required"
    assert command["status"] == "elevation_required"
    assert direct["path"] == str(outside.resolve(strict=False))
    assert command["target"] == str(outside.resolve(strict=False))
    assert backend_calls == []
    assert not outside.exists()


@pytest.mark.asyncio
async def test_direct_and_shell_explicit_denies_share_blocked_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    denied = tmp_path / "denied"
    denied.mkdir()
    sentinel = denied / "sentinel.txt"
    sentinel.write_text("must-not-appear", encoding="utf-8")
    runtime = get_runtime()
    assert runtime is not None
    runtime.settings.denied_read_roots = [str(denied)]
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("explicit denies must stop before backend execution")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace):
        direct = json.loads(await fs.read_file(str(sentinel)))
        command = json.loads(await shell.exec_command(f"cat {sentinel}"))

    assert direct["status"] == "blocked"
    assert command["status"] == "blocked"
    assert direct["reason"] == "denied_read"
    assert command["reason"] == "denied_read"
    assert backend_calls == []


@pytest.mark.asyncio
async def test_trusted_direct_read_still_honors_explicit_denied_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    denied = tmp_path / "denied"
    denied.mkdir()
    sentinel = denied / "sentinel.txt"
    sentinel.write_text("must-not-appear", encoding="utf-8")
    runtime = get_runtime()
    assert runtime is not None
    runtime.settings.denied_read_roots = [str(denied)]

    with tool_context(workspace, run_mode="trusted"):
        direct = json.loads(await fs.read_file(str(sentinel)))

    assert direct["status"] == "blocked"
    assert direct["reason"] == "denied_read"


@pytest.mark.asyncio
async def test_default_profile_allows_direct_filesystem_write_under_slash_tmp(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    target = tmp_path / "codex-default-tmp" / "notes.txt"
    runtime = get_runtime()
    assert runtime is not None
    runtime.settings.exclude_slash_tmp = False
    runtime.settings.exclude_tmpdir_env_var = False

    with tool_context(workspace):
        result = await fs.write_file(str(target), "tmp body\n")

    assert "Written 9 bytes" in result
    assert target.read_text(encoding="utf-8") == "tmp body\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_dir", [".git", ".agents", ".codex"])
async def test_direct_workspace_metadata_write_requires_elevation(
    tmp_path: Path,
    metadata_dir: str,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / metadata_dir).mkdir(parents=True)
    target = workspace / metadata_dir / "config-probe"

    with tool_context(workspace):
        payload = json.loads(await fs.write_file(str(target), "blocked\n"))

    assert payload["status"] == "elevation_required"
    assert payload["reason"] == "protected_metadata"
    assert not target.exists()


@pytest.mark.asyncio
async def test_approved_direct_workspace_metadata_write_executes_once(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".codex").mkdir(parents=True)
    target = workspace / ".codex" / "config-probe"
    kwargs = {
        "sandbox_permissions": "require_escalated",
        "justification": "Write the exact protected metadata file requested by the user.",
    }

    with tool_context(workspace):
        requested = json.loads(await fs.write_file(str(target), "approved\n", **kwargs))
        approval_id = requested["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        result = await fs.write_file(
            str(target),
            "approved\n",
            approval_id=approval_id,
            **kwargs,
        )

    assert "Written 9 bytes" in result
    assert target.read_text(encoding="utf-8") == "approved\n"
    assert get_approval_queue().get(approval_id).consumed is True


@pytest.mark.asyncio
async def test_trusted_sandbox_write_outside_workspace_does_not_auto_grant_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace, run_mode="trusted") as ctx:
        payload = json.loads(await fs.write_file(str(outside), "outside body\n"))

    assert payload["status"] == "elevation_required"
    assert not outside.exists()
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


def test_filesystem_mutation_tools_publish_structured_elevation_fields() -> None:
    from opensquilla.tools.registry import get_default_registry

    for tool_name in ("write_file", "edit_file", "edit_source"):
        registered = get_default_registry().get(tool_name)
        assert registered is not None
        params = registered.spec.parameters
        assert params["sandbox_permissions"]["enum"] == [
            "use_default",
            "require_escalated",
        ]
        assert "justification" in params
        assert "prefix_rule" in params


@pytest.mark.asyncio
async def test_write_file_exact_elevation_grant_is_consumed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace):
        requested = json.loads(
            await fs.write_file(
                str(outside),
                "outside body\n",
                sandbox_permissions="require_escalated",
                justification="Write the one fixed file requested by the user.",
            )
        )
        approval_id = requested["approval_id"]
        pending = get_approval_queue().get(approval_id)
        assert pending.params["humanActionable"] is False
        assert pending.params["action"]["content_digest"]
        assert "outside body" not in json.dumps(pending.params)

        get_approval_queue().resolve(approval_id, True)
        result = await fs.write_file(
            str(outside),
            "outside body\n",
            sandbox_permissions="require_escalated",
            justification="Write the one fixed file requested by the user.",
            approval_id=approval_id,
        )

    assert "Written 13 bytes" in result
    assert outside.read_text(encoding="utf-8") == "outside body\n"
    assert get_approval_queue().get(approval_id).consumed is True


@pytest.mark.asyncio
async def test_write_file_changed_content_cannot_consume_elevation_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"

    with tool_context(workspace):
        requested = json.loads(
            await fs.write_file(
                str(outside),
                "approved content\n",
                sandbox_permissions="require_escalated",
                justification="Write the one fixed file requested by the user.",
            )
        )
        approval_id = requested["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        changed = json.loads(
            await fs.write_file(
                str(outside),
                "changed content\n",
                sandbox_permissions="require_escalated",
                justification="Write the one fixed file requested by the user.",
                approval_id=approval_id,
            )
        )

    assert changed["status"] == "approval_action_mismatch"
    assert not outside.exists()


@pytest.mark.asyncio
async def test_apply_patch_exact_elevation_uses_digest_and_bypasses_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(patch_tool, "_default_patch_root", lambda: tmp_path.resolve())
    patch_text = """*** Begin Patch
*** Update File: outside.txt
@@ -1 +1 @@
-old
+new
*** End Patch"""

    with tool_context(workspace):
        default = json.loads(await patch_tool.apply_patch(patch=patch_text))
        assert default["status"] == "elevation_required"
        assert get_approval_queue().list_pending("exec") == []

        requested = json.loads(
            await patch_tool.apply_patch(
                patch=patch_text,
                sandbox_permissions="require_escalated",
                justification="Apply the exact one-file patch requested by the user.",
            )
        )
        approval_id = requested["approval_id"]
        pending = get_approval_queue().get(approval_id)
        assert pending.params["action"]["content_digest"]
        assert "-old" not in json.dumps(pending.params)
        get_approval_queue().resolve(approval_id, True)

        result = await patch_tool.apply_patch(
            patch=patch_text,
            sandbox_permissions="require_escalated",
            justification="Apply the exact one-file patch requested by the user.",
            approval_id=approval_id,
        )

    assert "1 file(s) modified" in result
    assert outside.read_text(encoding="utf-8") == "new\n"
    assert get_approval_queue().get(approval_id).consumed is True


@pytest.mark.asyncio
async def test_edit_file_exact_elevation_edits_one_outside_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("old value\n", encoding="utf-8")
    _install_filesystem_read_backend()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace):
        await fs.read_file(str(outside))
        requested = json.loads(
            await fs.edit_file(
                str(outside),
                "old value",
                "new value",
                sandbox_permissions="require_escalated",
                justification="Edit the exact outside file requested by the user.",
            )
        )
        approval_id = requested["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        result = await fs.edit_file(
            str(outside),
            "old value",
            "new value",
            sandbox_permissions="require_escalated",
            justification="Edit the exact outside file requested by the user.",
            approval_id=approval_id,
        )

    assert "Edited" in result
    assert outside.read_text(encoding="utf-8") == "new value\n"


@pytest.mark.asyncio
async def test_edit_source_exact_elevation_preserves_revision_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    _install_filesystem_read_backend()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())
    edits = [{"start_line": 1, "end_line": 1, "replacement": "value = 2\n"}]

    with tool_context(workspace):
        read_payload = json.loads(await fs.read_source(str(outside)))
        revision = read_payload["revision"]
        requested = json.loads(
            await fs.edit_source(
                str(outside),
                revision,
                edits,
                sandbox_permissions="require_escalated",
                justification="Apply the exact revision-gated edit requested by the user.",
            )
        )
        approval_id = requested["approval_id"]
        get_approval_queue().resolve(approval_id, True)
        result = json.loads(
            await fs.edit_source(
                str(outside),
                revision,
                edits,
                sandbox_permissions="require_escalated",
                justification="Apply the exact revision-gated edit requested by the user.",
                approval_id=approval_id,
            )
        )

    assert result["status"] == "applied"
    assert outside.read_text(encoding="utf-8") == "value = 2\n"


def test_trusted_sandbox_system_write_path_does_not_auto_grant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    target = Path("/usr/local/bin/opensquilla-system-write-probe")

    with tool_context(workspace, run_mode="trusted") as ctx:
        payload = fs._sandbox_path_access_envelope(target, write=True)

    assert payload is not None
    assert payload["status"] == "elevation_required"
    assert payload["path"] == str(target.resolve(strict=False))
    assert payload["access"] == "rw"
    assert ctx.sandbox_mounts == []
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_existing_rw_mount_allows_write_file_without_legacy_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    target = mounted / "out.txt"
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(mounted), "access": "rw"}],
    ):
        result = await fs.write_file(str(target), "x")

    assert "Written 1 bytes" in result
    assert target.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_existing_rw_mount_allows_edit_file_without_legacy_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    target = mounted / "out.txt"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(mounted), "access": "rw"}],
    ):
        result = await fs.edit_file(str(target), "old", "new")

    assert "Edited" in result
    assert target.read_text(encoding="utf-8") == "new\n"


@pytest.mark.asyncio
async def test_existing_ro_mount_write_requires_structured_elevation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    target = mounted / "out.txt"

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(mounted), "access": "ro"}],
    ):
        payload = json.loads(await fs.write_file(str(target), "x"))

    assert payload["status"] == "elevation_required"
    assert payload["path"] == str(target.resolve(strict=False))
    assert payload["access"] == "rw"
    assert get_approval_queue().list_pending("exec") == []
    assert not target.exists()


@pytest.mark.asyncio
async def test_list_dir_retry_accepts_path_approval_id_after_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    (mounted / "notes.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(mounted), "access": "ro"}],
    ):
        result = await fs.list_dir(str(mounted), approval_id="approved-path")

    assert "notes.txt" in result


@pytest.mark.asyncio
async def test_grep_search_does_not_follow_workspace_symlink_to_unmounted_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("needle secret-token\n", encoding="utf-8")
    link = workspace / "linked-secret.txt"
    try:
        link.symlink_to(outside_file)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("creating symlinks requires Windows developer mode or elevation")
        raise
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace):
        result = await fs.grep_search("needle", path=str(workspace))

    assert "secret-token" not in result
    assert "outside current sandbox view" in result or "No matches" in result


def test_shell_windows_null_redirection_does_not_request_write_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.operation_profile import OperationProfile

    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda: True)
    profile = OperationProfile("unknown_shell")

    assert shell._shell_write_access_targets("chcp 65001 >nul && echo ok", profile) == ()
    assert shell._shell_write_access_targets("where winget 2>NUL || echo missing", profile) == ()
    assert shell._shell_write_access_targets("echo ok > output.txt", profile) == (
        "output.txt",
    )


@pytest.mark.asyncio
async def test_shell_read_only_workdir_outside_workspace_requests_ro_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before path access is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace):
        payload = json.loads(await shell.exec_command("pwd", workdir=str(outside)))

    assert payload["status"] == "approval_required"
    approval_id = str(payload["approval_id"])
    assert payload["path"] == str(outside.resolve(strict=False))
    assert payload["access"] == "ro"
    assert payload["approvalKind"] == "sandbox_path"
    assert backend_calls == []

    with tool_context(workspace):
        pending = json.loads(
            await shell.exec_command("pwd", workdir=str(outside), approval_id=approval_id)
        )

    assert pending["status"] == "approval_pending"
    assert pending["approval_id"] == approval_id
    assert backend_calls == []


@pytest.mark.asyncio
async def test_shell_ro_workdir_mount_stays_read_only_in_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(
        workspace,
        sandbox_mounts=[{"path": str(outside), "access": "ro"}],
    ):
        await shell.exec_command(
            "python -c 'open(\"x\", \"w\").write(\"1\")'",
            workdir=str(outside),
        )

    assert len(backend_calls) == 1
    request = backend_calls[0]
    workspace_mount = next(
        mount
        for mount in request.policy.mounts
        if str(mount.sandbox_path) == "/workspace"
    )
    outside_mount = next(
        mount
        for mount in request.policy.mounts
        if mount.host_path == outside.resolve(strict=False)
    )
    assert request.cwd == outside.resolve(strict=False)
    assert workspace_mount.host_path == workspace.resolve(strict=False)
    assert workspace_mount.mode == "rw"
    assert outside_mount.mode == "ro"


@pytest.mark.asyncio
async def test_shell_workdir_relative_write_requires_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before path access is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace):
        payload = json.loads(await shell.exec_command("echo ok > out.txt", workdir=str(outside)))

    assert payload["status"] == "elevation_required"
    assert payload["target"] == str(outside.resolve(strict=False))
    assert backend_calls == []


@pytest.mark.asyncio
async def test_standard_shell_simple_read_path_outside_workspace_requests_ro_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before path access is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="standard"):
        payload = json.loads(await shell.exec_command(f"ls {outside}"))

    assert payload["status"] == "approval_required"
    assert payload["path"] == str(outside.resolve(strict=False))
    assert payload["access"] == "ro"
    assert payload["approvalKind"] == "sandbox_path"
    assert backend_calls == []


@pytest.mark.asyncio
async def test_standard_shell_powershell_read_path_outside_workspace_requests_ro_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before path access is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    command = f'powershell -NoProfile -Command "Get-ChildItem -LiteralPath \'{outside}\'"'
    with tool_context(workspace, run_mode="standard"):
        payload = json.loads(await shell.exec_command(command))

    assert payload["status"] == "approval_required"
    assert payload["path"] == str(outside.resolve(strict=False))
    assert payload["access"] == "ro"
    assert payload["approvalKind"] == "sandbox_path"
    assert backend_calls == []


@pytest.mark.asyncio
async def test_trusted_filesystem_read_path_outside_workspace_needs_no_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "notes.txt"
    target.write_text("trusted read\n", encoding="utf-8")
    _install_filesystem_read_backend()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace, run_mode="trusted") as ctx:
        result = await fs.read_file(str(target))

    assert "trusted read" in result
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_trusted_run_context_read_path_outside_workspace_needs_no_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "notes.txt"
    target.write_text("trusted context read\n", encoding="utf-8")
    _install_filesystem_read_backend()
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace, run_mode=None) as ctx:
        ctx.sandbox_run_context = RunContext(
            run_mode=RunMode.TRUSTED,
            workspace=str(workspace),
        )
        result = await fs.read_file(str(target))

    assert "trusted context read" in result
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_trusted_shell_simple_read_path_outside_workspace_needs_no_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []

    async def fake_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="listed\n", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="trusted") as ctx:
        result = await shell.exec_command(f"ls {outside}")

    assert "exit_code=0" in result
    assert "listed" in result
    assert backend_calls
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_trusted_shell_powershell_read_path_outside_workspace_needs_no_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []

    async def fake_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="listed\n", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    command = f'powershell -NoProfile -Command "Get-ChildItem -LiteralPath \'{outside}\'"'
    with tool_context(workspace, run_mode="trusted") as ctx:
        result = await shell.exec_command(command)

    assert "exit_code=0" in result
    assert "listed" in result
    assert backend_calls
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_trusted_shell_delete_existing_file_requires_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "outside-sandbox-smoke.txt"
    outside.parent.mkdir()
    outside.write_text("hello\n", encoding="utf-8")
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=True, reason=""),
    )

    with tool_context(workspace, run_mode="trusted") as ctx:
        payload = json.loads(await shell.exec_command(f'del "{outside}"'))

    assert payload["status"] == "elevation_required"
    assert payload["target"] == str(outside.resolve(strict=False))
    assert backend_calls == []
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_shell_write_to_protected_metadata_requires_elevation_before_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    repo = workspace
    (repo / ".git").mkdir(parents=True)
    (repo / ".codex").mkdir()
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell, "_windows_sandbox_backend_active", lambda runtime=None: True
    )
    monkeypatch.setattr(
        shell, "_windows_translate_posix_tmp_references", lambda command: command
    )
    monkeypatch.setattr(shell, "_windows_translate_posix_tmp_path", lambda path: path)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="trusted"):
        git_target = repo / ".git/_sandbox_should_not_write.txt"
        codex_target = repo / ".codex/_sandbox_should_not_write.txt"
        git_result = await shell.exec_command(
            f"touch {shlex.quote(str(git_target))}"
        )
        codex_result = await shell.exec_command(
            f"touch {shlex.quote(str(codex_target))}"
        )

    git_payload = json.loads(git_result)
    codex_payload = json.loads(codex_result)
    assert git_payload["status"] == "elevation_required"
    assert git_payload["reason"] == "protected_metadata"
    assert codex_payload["status"] == "elevation_required"
    assert codex_payload["reason"] == "protected_metadata"
    assert backend_calls == []
    assert not (repo / ".git/_sandbox_should_not_write.txt").exists()
    assert not (repo / ".codex/_sandbox_should_not_write.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_dir", [".git", ".codex"])
async def test_full_host_access_shell_write_to_protected_metadata_uses_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_dir: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    (repo / metadata_dir).mkdir(parents=True)
    target = repo / metadata_dir / "_full_host_write_probe.txt"
    host_calls: list[str] = []
    backend_calls: list[SandboxRequest] = []

    async def fail_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("full host access should not use the sandbox backend")

    async def fake_host(
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str],
        stdin_bytes: bytes | None,
        effective_timeout: float,
    ) -> str:
        host_calls.append(command)
        return "host-ran"

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda runtime=None: True)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    command = (
        "powershell -NoProfile -Command "
        f"\"Set-Content -LiteralPath '{target}' -Value full-host\""
    )
    with tool_context(workspace, run_mode="full"):
        result = await shell.exec_command(command)

    assert result == "host-ran"
    assert host_calls == [command]
    assert backend_calls == []


@pytest.mark.asyncio
async def test_trusted_shell_delete_existing_file_under_rw_mount_uses_existing_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    mounted = tmp_path / "outside"
    mounted.mkdir()
    outside = mounted / "outside-sandbox-smoke.txt"
    outside.write_text("hello\n", encoding="utf-8")
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda runtime=None: True)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=True, reason=""),
    )

    with tool_context(
        workspace,
        run_mode="trusted",
        sandbox_mounts=[{"path": str(mounted.resolve(strict=False)), "access": "rw"}],
    ) as ctx:
        result = await shell.exec_command(f'del "{outside}"')

    assert "exit_code=0" in result
    assert backend_calls
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == [
        {"path": str(mounted.resolve(strict=False)), "access": "rw"},
    ]
    request = backend_calls[0]
    assert any(mount.host_path == mounted for mount in request.policy.mounts)


def test_windows_shell_policy_ignores_deleted_active_file_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import (
        MountSpec,
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SecurityLevel,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    stale = workspace / "sandbox_probe_workspace.txt"
    stale.write_text("workspace-ok", encoding="utf-8")
    stale.unlink()
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(workspace, workspace, mode="rw"),
            MountSpec(stale, stale, mode="rw", required=False),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=(),
        require_approval=False,
    )
    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda runtime=None: True)

    with tool_context(
        workspace,
        run_mode="trusted",
        sandbox_mounts=[{"path": str(stale.resolve(strict=False)), "access": "rw"}],
    ):
        updated = shell._policy_with_active_tool_mounts(policy)

    assert stale not in {mount.host_path for mount in updated.mounts}
    assert workspace in {mount.host_path for mount in updated.mounts}


def test_shell_policy_preserves_workspace_rw_absolute_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox.types import (
        SANDBOX_WORKSPACE_PATH,
        MountSpec,
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SecurityLevel,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=workspace,
                sandbox_path=SANDBOX_WORKSPACE_PATH,
                mode="rw",
                required=True,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=(),
        require_approval=False,
    )
    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda runtime=None: False)

    with tool_context(
        workspace,
        run_mode="trusted",
        sandbox_mounts=[{"path": str(workspace), "access": "ro"}],
    ):
        updated = shell._policy_with_active_tool_mounts(policy)

    mounts_by_sandbox = {str(mount.sandbox_path): mount for mount in updated.mounts}
    assert mounts_by_sandbox["/workspace"].mode == "rw"
    assert mounts_by_sandbox[str(workspace)].mode == "rw"


@pytest.mark.asyncio
async def test_shell_copy_from_outside_workspace_requests_ro_mount_before_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_global_root_readonly()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "notes.txt"
    target = workspace / "notes.txt"
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before source path access is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="standard"):
        payload = json.loads(await shell.exec_command(f"cp {source} {target}"))

    assert payload["status"] == "approval_required"
    assert payload["path"] == str(source.resolve(strict=False))
    assert payload["access"] == "ro"
    assert payload["approvalKind"] == "sandbox_path"
    assert backend_calls == []


@pytest.mark.asyncio
async def test_standard_shell_copy_to_outside_workspace_requires_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    source = workspace / "notes.txt"
    target = tmp_path / "outside" / "notes.txt"
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before destination path access is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="standard"):
        payload = json.loads(await shell.exec_command(f"cp {source} {target}"))

    assert payload["status"] == "elevation_required"
    assert payload["target"] == str(target.resolve(strict=False))
    assert backend_calls == []


@pytest.mark.asyncio
async def test_trusted_shell_copy_to_outside_workspace_requires_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    source = workspace / "notes.txt"
    source.write_text("hello\n", encoding="utf-8")
    target = tmp_path / "outside" / "notes.txt"
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="trusted") as ctx:
        payload = json.loads(await shell.exec_command(f"cp {source} {target}"))

    assert payload["status"] == "elevation_required"
    assert payload["target"] == str(target.resolve(strict=False))
    assert backend_calls == []
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_trusted_shell_external_workdir_write_requires_elevation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[SandboxRequest] = []

    async def fake_backend(request: SandboxRequest, *, runtime: object = None) -> object:
        backend_calls.append(request)
        return SimpleNamespace(stdout="", stderr="", returncode=0, backend_notes=[])

    monkeypatch.setattr(shell, "run_under_backend", fake_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="trusted") as ctx:
        payload = json.loads(
            await shell.exec_command("echo hi > out.txt", workdir=str(outside))
        )

    assert payload["status"] == "elevation_required"
    assert payload["target"] == str(outside.resolve(strict=False))
    assert backend_calls == []
    assert get_approval_queue().list_pending("exec") == []
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_shell_absolute_redirection_requires_elevation_before_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    target = tmp_path / "outside" / "out.txt"
    backend_calls: list[object] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("backend should not run before redirection target is granted")

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="standard"):
        payload = json.loads(await shell.exec_command(f"echo hi > {target}"))

    assert payload["status"] == "elevation_required"
    assert payload["target"] == str(target.resolve(strict=False))
    assert backend_calls == []


@pytest.mark.asyncio
async def test_shell_simple_read_path_full_host_access_does_not_request_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    backend_calls: list[object] = []
    host_calls: list[tuple[str, str | None]] = []

    async def fail_backend(request: object, *, runtime: object = None) -> object:
        backend_calls.append(request)
        raise AssertionError("full host access should not use the sandbox backend")

    async def fake_host(
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str],
        stdin_bytes: bytes | None,
        effective_timeout: float,
    ) -> str:
        host_calls.append((command, cwd))
        return "host-ran"

    monkeypatch.setattr(shell, "run_under_backend", fail_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    with tool_context(workspace, run_mode="full"):
        result = await shell.exec_command(f"ls {outside}")

    assert result == "host-ran"
    assert host_calls == [(f"ls {outside}", str(workspace.resolve()))]
    assert backend_calls == []


@pytest.mark.asyncio
async def test_default_write_does_not_create_legacy_mount_approval(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"

    with tool_context(workspace):
        payload = json.loads(await fs.write_file(str(outside), "outside body\n"))

    assert payload["status"] == "elevation_required"
    assert get_approval_queue().list_pending("exec") == []
    assert not outside.exists()


@pytest.mark.asyncio
async def test_exact_write_elevation_does_not_persist_a_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"
    monkeypatch.setattr(fs.asyncio, "get_event_loop", lambda: _InlineExecutorLoop())

    with tool_context(workspace) as ctx:
        payload = json.loads(
            await fs.write_file(
                str(outside),
                "outside body\n",
                sandbox_permissions="require_escalated",
                justification="Write the exact file requested by the user.",
            )
        )
        approval_id = str(payload["approval_id"])
        get_approval_queue().resolve(approval_id, True)
        retried = await fs.write_file(
            str(outside),
            "outside body\n",
            sandbox_permissions="require_escalated",
            justification="Write the exact file requested by the user.",
            approval_id=approval_id,
        )

    assert "Written 13 bytes" in retried
    assert outside.read_text(encoding="utf-8") == "outside body\n"
    assert ctx.sandbox_mounts == []


@pytest.mark.asyncio
async def test_write_elevation_record_has_no_persistent_mount_choices(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    outside = tmp_path / "outside" / "notes.txt"

    with tool_context(workspace):
        payload = json.loads(
            await fs.write_file(
                str(outside),
                "outside body\n",
                sandbox_permissions="require_escalated",
                justification="Write the exact file requested by the user.",
                prefix_rule=["write_file"],
            )
        )
        approval_id = str(payload["approval_id"])

    params = get_approval_queue().get(approval_id).params
    assert params["approvalKind"] == "sandbox_elevation"
    assert "choices" not in params
    assert params["action"]["prefix_rule"] == ["write_file"]
    assert not outside.exists()
