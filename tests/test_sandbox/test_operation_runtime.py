from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.sandbox.operation_runtime import (
    ArtifactOperationRequest,
    FilesystemOperationRequest,
    MediaOperationRequest,
    NetworkOperationRequest,
    OperationApproval,
    OperationPermissions,
    ProcessOperationRequest,
    SandboxOperation,
    SandboxOperationResult,
    SandboxOperationRuntime,
    SandboxToolDescriptor,
)
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
)


def _filesystem_operation(tmp_path) -> SandboxOperation:
    return SandboxOperation.filesystem(
        kind="read_file",
        workspace=tmp_path,
        run_mode="trusted",
        path=tmp_path / "file.txt",
        paths=(tmp_path / "file.txt",),
    )


def test_operation_uses_common_header_and_typed_request(tmp_path) -> None:
    target = tmp_path / "file.txt"

    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=tmp_path,
        run_mode="trusted",
        path=target,
        paths=(target,),
        content="hello",
    )

    assert operation.domain == "filesystem"
    assert operation.kind == "write_text"
    assert operation.workspace == tmp_path
    assert operation.run_mode == "trusted"
    assert isinstance(operation.permissions, OperationPermissions)
    assert isinstance(operation.approval, OperationApproval)
    assert isinstance(operation.request, FilesystemOperationRequest)
    assert operation.request.path == target
    assert operation.request.content == "hello"

    for legacy_field in (
        "path",
        "paths",
        "content",
        "old_text",
        "new_text",
        "patch",
        "root",
        "network",
        "artifact",
        "media",
        "payload",
        "process_request",
    ):
        assert not hasattr(operation, legacy_field), legacy_field


def test_tool_descriptor_builds_common_header_and_typed_request(tmp_path) -> None:
    descriptor = SandboxToolDescriptor.network(
        kind="web.fetch",
        argv_factory=lambda args: ("web_fetch", str(args["url"])),
        request_factory=lambda args: NetworkOperationRequest(
            url=str(args["url"]),
            method="GET",
            host="example.com",
        ),
    )

    operation = descriptor.build_operation(
        tool_name="web_fetch",
        arguments={"url": "https://example.com/index.html"},
        workspace=tmp_path,
        run_mode="trusted",
    )

    assert operation.domain == "network"
    assert operation.kind == "web.fetch"
    assert operation.workspace == tmp_path
    assert operation.run_mode == "trusted"
    assert operation.tool_name == "web_fetch"
    assert isinstance(operation.permissions, OperationPermissions)
    assert isinstance(operation.approval, OperationApproval)
    assert isinstance(operation.request, NetworkOperationRequest)
    assert operation.request.url == "https://example.com/index.html"


def test_tool_descriptor_builds_artifact_and_media_requests(tmp_path) -> None:
    artifact_descriptor = SandboxToolDescriptor.artifact(
        kind="artifact.publish",
        request_factory=lambda args: ArtifactOperationRequest(
            path=tmp_path / str(args["path"])
        ),
    )
    media_descriptor = SandboxToolDescriptor.media(
        kind="media.generate",
        request_factory=lambda args: MediaOperationRequest(
            path=tmp_path / str(args["filename"]),
            media_type="image",
            options={"prompt": str(args["prompt"])},
        ),
    )

    artifact_operation = artifact_descriptor.build_operation(
        tool_name="publish_artifact",
        arguments={"path": "out.txt"},
        workspace=tmp_path,
        run_mode="trusted",
    )
    media_operation = media_descriptor.build_operation(
        tool_name="image_generate",
        arguments={"filename": "image.png", "prompt": "a chart"},
        workspace=tmp_path,
        run_mode="trusted",
    )

    assert artifact_operation.domain == "artifact"
    assert artifact_operation.kind == "artifact.publish"
    assert isinstance(artifact_operation.request, ArtifactOperationRequest)
    assert artifact_operation.request.path == tmp_path / "out.txt"
    assert media_operation.domain == "media"
    assert media_operation.kind == "media.generate"
    assert isinstance(media_operation.request, MediaOperationRequest)
    assert media_operation.request.path == tmp_path / "image.png"
    assert media_operation.request.media_type == "image"


def _process_request(tmp_path) -> SandboxRequest:
    return SandboxRequest(
        argv=("python", "-c", "print('ok')"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD,
            network=NetworkMode.NONE,
            mounts=(MountSpec(host_path=tmp_path, sandbox_path=tmp_path, mode="rw"),),
            workspace_rw=True,
            tmp_writable=True,
            limits=ResourceLimits(),
            env_allowlist=(),
            require_approval=False,
        ),
    )


@pytest.mark.asyncio
async def test_operation_runtime_bypasses_when_sandbox_is_disabled(tmp_path) -> None:
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=False),
        backend=SimpleNamespace(name="noop"),
    )

    result = await SandboxOperationRuntime(runtime).run(_filesystem_operation(tmp_path))

    assert result is None


@pytest.mark.asyncio
async def test_operation_runtime_bypasses_for_full_host_access(tmp_path) -> None:
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=SimpleNamespace(name="noop"),
    )

    result = await SandboxOperationRuntime(runtime, host_execution_active=True).run(
        _filesystem_operation(tmp_path)
    )

    assert result is None


@pytest.mark.asyncio
async def test_operation_runtime_never_sandboxes_full_run_mode(tmp_path) -> None:
    class _Backend:
        name = "windows_default"

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run_operation(self, operation: SandboxOperation) -> SandboxOperationResult:
            raise AssertionError(f"full host operation reached sandbox backend: {operation}")

    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=_Backend(),
    )
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=tmp_path,
        run_mode="full",
        path=tmp_path / "file.txt",
        paths=(tmp_path / "file.txt",),
    )

    result = await SandboxOperationRuntime(runtime).run(operation)

    assert result is None


@pytest.mark.asyncio
async def test_operation_runtime_filesystem_fails_closed_without_backend_worker(tmp_path) -> None:
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=SimpleNamespace(name="custom_backend"),
    )

    with pytest.raises(SandboxBackendError, match="must sandbox filesystem operations"):
        await SandboxOperationRuntime(runtime).run(_filesystem_operation(tmp_path))


@pytest.mark.asyncio
async def test_operation_runtime_delegates_filesystem_to_backend_worker(tmp_path) -> None:
    seen: list[SandboxOperation] = []

    class _Backend:
        name = "windows_default"

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run_operation(self, operation: SandboxOperation) -> SandboxOperationResult:
            seen.append(operation)
            return SandboxOperationResult(message="ok")

    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=_Backend(),
    )
    filesystem_operation = _filesystem_operation(tmp_path)

    result = await SandboxOperationRuntime(runtime).run(filesystem_operation)

    assert result == SandboxOperationResult(message="ok")
    assert seen == [filesystem_operation]


@pytest.mark.asyncio
async def test_operation_runtime_delegates_process_to_backend_run(tmp_path) -> None:
    seen: list[SandboxRequest] = []

    class _Backend:
        name = "windows_default"

        async def run(self, request: SandboxRequest) -> SandboxResult:
            seen.append(request)
            return SandboxResult(
                returncode=0,
                stdout="ok",
                stderr="",
                wall_time_s=0.0,
                backend_used="windows_default",
            )

    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=_Backend(),
    )
    request = _process_request(tmp_path)
    operation = SandboxOperation.process(request)

    assert isinstance(operation.request, ProcessOperationRequest)

    result = await SandboxOperationRuntime(runtime).run(operation)

    assert result == SandboxResult(
        returncode=0,
        stdout="ok",
        stderr="",
        wall_time_s=0.0,
        backend_used="windows_default",
    )
    assert seen == [request]


@pytest.mark.asyncio
async def test_operation_runtime_reserves_unimplemented_domains_without_host_fallback(
    tmp_path,
) -> None:
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=SimpleNamespace(name="windows_default"),
    )
    operation = SandboxOperation(
        domain="artifact",
        kind="publish",
        workspace=tmp_path,
        run_mode="trusted",
        request=ArtifactOperationRequest(path=tmp_path / "out.bin"),
    )

    with pytest.raises(SandboxBackendError, match="artifact operations are not implemented"):
        await SandboxOperationRuntime(runtime).run(operation)
