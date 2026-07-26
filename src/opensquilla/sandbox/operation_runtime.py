"""Unified sandbox operation runtime.

Tool handlers translate side-effecting work into a :class:`SandboxOperation`.
The runtime then decides whether the operation may run on the host
(sandbox disabled / Full Host Access) or must be delegated to the active
sandbox backend. This is the second-layer boundary between model-facing tools
and platform-specific sandbox backends.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.types import SandboxBackendError, SandboxRequest, SandboxResult

SandboxOperationDomain = Literal[
    "process",
    "filesystem",
    "network",
    "artifact",
    "media",
    "custom",
]

SANDBOX_FILESYSTEM_WRITE_KINDS = frozenset({"write_text", "edit_text", "apply_patch"})


@dataclass(frozen=True)
class OperationPermissions:
    """Unified permission model carried by every sandbox operation."""

    filesystem: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    process: dict[str, Any] = field(default_factory=dict)
    artifact: dict[str, Any] = field(default_factory=dict)
    media: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "filesystem": self.filesystem,
            "network": self.network,
            "process": self.process,
            "artifact": self.artifact,
            "media": self.media,
        }


@dataclass(frozen=True)
class OperationApproval:
    """Unified approval metadata carried by every sandbox operation."""

    required: bool = False
    reason: str = ""
    namespace: str = "sandbox"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "required": self.required,
            "reason": self.reason,
            "namespace": self.namespace,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class ProcessOperationRequest:
    """Typed request for a backend-managed process operation."""

    request: SandboxRequest | None = None
    argv: tuple[str, ...] = ()
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    stdin: bytes | None = None

    def to_payload(self) -> dict[str, object]:
        if self.request is not None:
            return {
                "argv": list(self.request.argv),
                "cwd": str(self.request.cwd),
                "actionKind": self.request.action_kind,
                "env": dict(self.request.env),
                "stdin": self.request.stdin.decode("utf-8", errors="replace")
                if self.request.stdin is not None
                else None,
            }
        return {
            "argv": list(self.argv),
            "cwd": str(self.cwd) if self.cwd is not None else None,
            "env": dict(self.env),
            "stdin": self.stdin.decode("utf-8", errors="replace")
            if self.stdin is not None
            else None,
        }


@dataclass(frozen=True)
class FilesystemOperationRequest:
    """Typed request for filesystem operations delegated to backend workers."""

    path: Path | None = None
    paths: tuple[Path, ...] = ()
    display_path: str = ""
    content: str = ""
    old_text: str = ""
    new_text: str = ""
    patch: str = ""
    root: Path | None = None
    offset: int | None = None
    limit: int | None = None
    pattern: str = ""
    include: str | None = None
    max_results: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path is not None else None,
            "paths": [str(path) for path in self.paths],
            "displayPath": self.display_path,
            "content": self.content,
            "oldText": self.old_text,
            "newText": self.new_text,
            "patch": self.patch,
            "root": str(self.root) if self.root is not None else None,
            "offset": self.offset,
            "limit": self.limit,
            "pattern": self.pattern,
            "include": self.include,
            "maxResults": self.max_results,
        }


@dataclass(frozen=True)
class NetworkOperationRequest:
    """Typed request placeholder for future sandboxed network tools."""

    url: str = ""
    method: str = ""
    host: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    output_path: Path | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "url": self.url,
            "method": self.method,
            "host": self.host,
            "headers": self.headers,
            "body": self.body,
            "outputPath": str(self.output_path) if self.output_path is not None else None,
        }


@dataclass(frozen=True)
class ArtifactOperationRequest:
    """Typed request placeholder for future artifact operations."""

    path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path is not None else None,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class MediaOperationRequest:
    """Typed request placeholder for future media operations."""

    path: Path | None = None
    media_type: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path is not None else None,
            "mediaType": self.media_type,
            "options": self.options,
        }


@dataclass(frozen=True)
class CustomOperationRequest:
    """Typed escape hatch for future domains while preserving one runtime shape."""

    data: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {"data": self.data}


SandboxOperationRequest = (
    ProcessOperationRequest
    | FilesystemOperationRequest
    | NetworkOperationRequest
    | ArtifactOperationRequest
    | MediaOperationRequest
    | CustomOperationRequest
)

OperationArguments = Mapping[str, Any]
OperationRequestFactory = Callable[[OperationArguments], SandboxOperationRequest]
OperationArgvFactory = Callable[[OperationArguments], tuple[str, ...]]
OperationCwdFactory = Callable[[OperationArguments], str | Path | None]
OperationEnvFactory = Callable[[OperationArguments], dict[str, str] | None]


def _jsonish(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(k): _jsonish(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonish(v) for v in value]
    return str(value)


@dataclass(frozen=True)
class SandboxToolDescriptor:
    """Typed sandbox declaration attached to every tool spec."""

    domain: SandboxOperationDomain
    kind: str
    request_factory: OperationRequestFactory | None = None
    argv_factory: OperationArgvFactory | None = None
    cwd_factory: OperationCwdFactory | None = None
    env_factory: OperationEnvFactory | None = None
    permissions: OperationPermissions = field(default_factory=OperationPermissions)
    approval: OperationApproval = field(default_factory=OperationApproval)
    summary: str = ""
    enforce: bool = False
    record_payload: bool = True
    hints: Any | None = None

    @classmethod
    def custom(cls, *, kind: str, enforce: bool = False) -> SandboxToolDescriptor:
        return cls(domain="custom", kind=kind, enforce=enforce)

    @classmethod
    def process(
        cls,
        *,
        kind: str,
        argv_factory: OperationArgvFactory,
        cwd_factory: OperationCwdFactory | None = None,
        env_factory: OperationEnvFactory | None = None,
        hints: Any | None = None,
        enforce: bool = True,
        record_payload: bool = True,
    ) -> SandboxToolDescriptor:
        return cls(
            domain="process",
            kind=kind,
            argv_factory=argv_factory,
            cwd_factory=cwd_factory,
            env_factory=env_factory,
            hints=hints,
            enforce=enforce,
            record_payload=record_payload,
        )

    @classmethod
    def filesystem(
        cls,
        *,
        kind: str,
        request_factory: OperationRequestFactory,
        argv_factory: OperationArgvFactory | None = None,
        cwd_factory: OperationCwdFactory | None = None,
        enforce: bool = True,
        record_payload: bool = True,
    ) -> SandboxToolDescriptor:
        return cls(
            domain="filesystem",
            kind=kind,
            request_factory=request_factory,
            argv_factory=argv_factory,
            cwd_factory=cwd_factory,
            enforce=enforce,
            record_payload=record_payload,
        )

    @classmethod
    def network(
        cls,
        *,
        kind: str,
        argv_factory: OperationArgvFactory,
        request_factory: OperationRequestFactory | None = None,
        hints: Any | None = None,
        enforce: bool = True,
        record_payload: bool = True,
    ) -> SandboxToolDescriptor:
        return cls(
            domain="network",
            kind=kind,
            argv_factory=argv_factory,
            request_factory=request_factory,
            hints=hints,
            enforce=enforce,
            record_payload=record_payload,
        )

    @classmethod
    def artifact(
        cls,
        *,
        kind: str,
        request_factory: OperationRequestFactory | None = None,
        enforce: bool = False,
        record_payload: bool = False,
    ) -> SandboxToolDescriptor:
        return cls(
            domain="artifact",
            kind=kind,
            request_factory=request_factory,
            enforce=enforce,
            record_payload=record_payload,
        )

    @classmethod
    def media(
        cls,
        *,
        kind: str,
        request_factory: OperationRequestFactory | None = None,
        enforce: bool = False,
        record_payload: bool = False,
    ) -> SandboxToolDescriptor:
        return cls(
            domain="media",
            kind=kind,
            request_factory=request_factory,
            enforce=enforce,
            record_payload=record_payload,
        )

    def build_operation(
        self,
        *,
        tool_name: str,
        arguments: OperationArguments,
        workspace: Path | None,
        run_mode: str | None,
    ) -> SandboxOperation:
        cwd = self._build_cwd(arguments)
        return SandboxOperation(
            domain=self.domain,
            kind=self.kind or tool_name,
            request=self._build_request(arguments),
            workspace=cwd or workspace,
            run_mode=run_mode or "",
            tool_name=tool_name,
            summary=self.summary,
            permissions=self.permissions,
            approval=self.approval,
        )

    def _build_request(self, arguments: OperationArguments) -> SandboxOperationRequest:
        if self.request_factory is not None:
            return self.request_factory(arguments)
        if self.domain == "process":
            return ProcessOperationRequest(
                argv=self._build_argv(arguments),
                cwd=self._build_cwd(arguments),
                env=self._build_env(arguments) or {},
            )
        if self.domain == "filesystem":
            return FilesystemOperationRequest()
        if self.domain == "network":
            return NetworkOperationRequest()
        if self.domain == "artifact":
            return ArtifactOperationRequest()
        if self.domain == "media":
            return MediaOperationRequest()
        return CustomOperationRequest(data={"arguments": _jsonish(dict(arguments))})

    def _build_argv(self, arguments: OperationArguments) -> tuple[str, ...]:
        if self.argv_factory is not None:
            return self.argv_factory(arguments)
        return (self.kind,)

    def _build_cwd(self, arguments: OperationArguments) -> Path | None:
        if self.cwd_factory is None:
            return None
        raw = self.cwd_factory(arguments)
        if raw is None or raw == "":
            return None
        return raw if isinstance(raw, Path) else Path(str(raw))

    def _build_env(self, arguments: OperationArguments) -> dict[str, str] | None:
        if self.env_factory is None:
            return None
        env = self.env_factory(arguments)
        if env is None:
            return None
        return {str(key): str(value) for key, value in env.items()}


@dataclass(frozen=True)
class SandboxOperation:
    """Common runtime header plus a domain-specific typed request."""

    domain: SandboxOperationDomain
    kind: str
    request: SandboxOperationRequest
    workspace: Path | None = None
    run_mode: str = ""
    tool_name: str = ""
    operation_id: str = ""
    summary: str = ""
    permissions: OperationPermissions = field(default_factory=OperationPermissions)
    approval: OperationApproval = field(default_factory=OperationApproval)
    file_system_profile: FileSystemPermissionProfile | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def process(cls, request: SandboxRequest) -> SandboxOperation:
        return cls(
            domain="process",
            kind=request.action_kind,
            request=ProcessOperationRequest(request),
            workspace=request.cwd,
            run_mode=request.run_mode,
            tool_name=request.argv[0] if request.argv else "",
        )

    @classmethod
    def filesystem(
        cls,
        *,
        kind: str,
        workspace: Path,
        run_mode: str,
        path: Path | None = None,
        paths: tuple[Path, ...] = (),
        display_path: str = "",
        content: str = "",
        old_text: str = "",
        new_text: str = "",
        patch: str = "",
        root: Path | None = None,
        offset: int | None = None,
        limit: int | None = None,
        pattern: str = "",
        include: str | None = None,
        max_results: int | None = None,
        file_system_profile: FileSystemPermissionProfile | None = None,
    ) -> SandboxOperation:
        operation_paths = paths or ((path,) if path is not None else ())
        request = FilesystemOperationRequest(
            path=path,
            paths=tuple(candidate for candidate in operation_paths if candidate is not None),
            display_path=display_path,
            content=content,
            old_text=old_text,
            new_text=new_text,
            patch=patch,
            root=root,
            offset=offset,
            limit=limit,
            pattern=pattern,
            include=include,
            max_results=max_results,
        )
        return cls(
            domain="filesystem",
            kind=kind,
            request=request,
            workspace=workspace,
            run_mode=run_mode,
            tool_name="filesystem",
            file_system_profile=file_system_profile,
        )

    def to_payload(self) -> dict[str, object]:
        """Serialize operation details for backend helper processes."""

        payload: dict[str, object] = {
            "domain": self.domain,
            "kind": self.kind,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "runMode": self.run_mode,
            "toolName": self.tool_name,
            "operationId": self.operation_id,
            "summary": self.summary,
            "permissions": self.permissions.to_payload(),
            "approval": self.approval.to_payload(),
            "request": self.request.to_payload()
            if hasattr(self.request, "to_payload")
            else {},
        }
        if isinstance(self.request, FilesystemOperationRequest):
            payload.update(self.request.to_payload())
        return payload


@dataclass(frozen=True)
class SandboxOperationResult:
    """Generic result for non-process sandbox operations."""

    message: str
    created: bool = False

    @classmethod
    def from_worker_stdout(cls, stdout: str) -> SandboxOperationResult:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise SandboxBackendError("sandbox operation worker returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxBackendError("sandbox operation worker returned invalid payload")
        message = payload.get("message")
        if not isinstance(message, str):
            raise SandboxBackendError("sandbox operation worker payload is missing message")
        return cls(message=message, created=bool(payload.get("created", False)))


def backend_supports_operation(backend: object, domain: str) -> bool:
    supported = getattr(backend, "operation_domains_supported", None)
    runner = getattr(backend, "run_operation", None)
    if not callable(supported) or not callable(runner):
        return False
    return domain in set(supported())


async def run_with_backend_if_supported(
    backend: object,
    operation: SandboxOperation,
) -> SandboxOperationResult | None:
    if not backend_supports_operation(backend, operation.domain):
        return None
    runner = getattr(backend, "run_operation")
    result = await runner(operation)
    if isinstance(result, SandboxOperationResult):
        return result
    message = getattr(result, "message", None)
    if not isinstance(message, str):
        raise SandboxBackendError(f"{operation.domain} backend returned invalid result")
    return SandboxOperationResult(
        message=message,
        created=bool(getattr(result, "created", False)),
    )


@dataclass(frozen=True)
class SandboxOperationRuntime:
    """Run operations through the active sandbox backend when required."""

    runtime: Any | None
    host_execution_active: bool = False

    async def run(self, operation: SandboxOperation) -> object | None:
        if self.runtime is None:
            return None
        if str(operation.run_mode).strip().lower() == "full":
            return None
        effective = getattr(self.runtime, "effective", None)
        if effective is not None and not effective.sandbox_enabled:
            return None
        if self.host_execution_active:
            return None
        if operation.domain == "process":
            return await self._run_process(operation)
        return await self._run_backend_operation(operation)

    async def _run_process(self, operation: SandboxOperation) -> object:
        runtime = self.runtime
        if runtime is None:
            raise SandboxBackendError("process operation is missing sandbox runtime")
        if not isinstance(operation.request, ProcessOperationRequest):
            raise SandboxBackendError("process operation is missing SandboxRequest")
        if operation.request.request is None:
            raise SandboxBackendError("process operation is missing resolved SandboxRequest")
        result = await runtime.backend.run(operation.request.request)
        if isinstance(result, SandboxResult):
            return result
        returncode = getattr(result, "returncode", None)
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", None)
        if (
            not isinstance(returncode, int)
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
        ):
            raise SandboxBackendError("process sandbox backend returned invalid result")
        return SandboxResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            wall_time_s=float(getattr(result, "wall_time_s", 0.0)),
            timed_out=bool(getattr(result, "timed_out", False)),
            backend_used=str(getattr(result, "backend_used", runtime.backend.name)),
            backend_notes=tuple(getattr(result, "backend_notes", ())),
        )

    async def _run_backend_operation(self, operation: SandboxOperation) -> object:
        runtime = self.runtime
        if runtime is None:
            raise SandboxBackendError(f"{operation.domain} operation is missing sandbox runtime")
        result = await run_with_backend_if_supported(runtime.backend, operation)
        if result is not None:
            return result
        backend_name = str(getattr(runtime.backend, "name", "") or "")
        if operation.domain == "filesystem":
            detail = "must sandbox filesystem operations"
        else:
            detail = f"{operation.domain} operations are not implemented"
        raise SandboxBackendError(f"{backend_name or 'sandbox'} backend {detail}")


@dataclass(frozen=True)
class SandboxToolGuard:
    """Prepared approval/network context for an in-process tool handler."""

    operation: SandboxOperation
    request: SandboxRequest | None = None
    policy: Any | None = None
    runtime: Any | None = None
    network_context: Any | None = None
    denial_payload: str | None = None
    record_payload: bool = True


class SandboxToolHandler(Protocol):
    def __call__(self, **kwargs: Any) -> Awaitable[Any]: ...


def _operation_process_request(
    operation: SandboxOperation,
) -> ProcessOperationRequest | None:
    if isinstance(operation.request, ProcessOperationRequest):
        return operation.request
    return None


async def prepare_tool_operation_guard(
    descriptor: SandboxToolDescriptor,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    workspace: Path | None,
    run_mode: str | None,
) -> SandboxToolGuard:
    """Build and approve the sandbox operation declared by a tool spec."""

    operation = descriptor.build_operation(
        tool_name=tool_name,
        arguments=arguments,
        workspace=workspace,
        run_mode=run_mode,
    )
    guard = SandboxToolGuard(
        operation=operation,
        record_payload=descriptor.record_payload,
    )
    if not descriptor.enforce:
        return guard

    from opensquilla.sandbox.integration import (
        _is_in_process_network_action,
        _prepare_in_process_managed_network,
        _prepare_network_none_in_process_action,
        gate_action,
        get_runtime,
    )
    from opensquilla.sandbox.types import (
        DenialReason,
        DenialResult,
        NetworkMode,
        SuggestedNextStep,
    )
    from opensquilla.tools.run_mode import full_host_access_active

    if full_host_access_active():
        return guard

    process_request = _operation_process_request(operation)
    argv = (
        process_request.request.argv
        if process_request is not None and process_request.request is not None
        else process_request.argv
        if process_request is not None and process_request.argv
        else descriptor._build_argv(arguments)
    )
    cwd = (
        process_request.request.cwd
        if process_request is not None and process_request.request is not None
        else process_request.cwd
        if process_request is not None and process_request.cwd is not None
        else operation.workspace
    )
    env = (
        process_request.request.env
        if process_request is not None and process_request.request is not None
        else process_request.env
        if process_request is not None and process_request.env
        else descriptor._build_env(arguments)
    )

    decision, policy, request = await gate_action(
        action_kind=operation.kind,
        argv=argv,
        cwd=cwd,
        env=env,
        hints=descriptor.hints,
    )
    if isinstance(decision, DenialResult):
        return SandboxToolGuard(
            operation=operation,
            request=request,
            policy=policy,
            runtime=get_runtime(),
            denial_payload=json.dumps(decision.to_dict()),
            record_payload=descriptor.record_payload,
        )

    if policy.network == NetworkMode.NONE and _is_in_process_network_action(operation.kind):
        runtime = get_runtime()
        if runtime is None:
            denial = DenialResult(
                reason=DenialReason.RUNTIME_UNCONFIGURED,
                suggested_next_step=SuggestedNextStep.ASK_USER,
                level=policy.level,
                action_fingerprint="",
                message=(
                    "Sandbox runtime is not configured. Network-disabled "
                    "in-process tools refuse to run."
                ),
                retryable=False,
            )
            return SandboxToolGuard(
                operation=operation,
                request=request,
                policy=policy,
                runtime=runtime,
                denial_payload=json.dumps(denial.to_dict()),
                record_payload=descriptor.record_payload,
            )
        prepared = await _prepare_network_none_in_process_action(request, runtime)
        if isinstance(prepared, DenialResult):
            return SandboxToolGuard(
                operation=operation,
                request=request,
                policy=policy,
                runtime=runtime,
                denial_payload=json.dumps(prepared.to_dict()),
                record_payload=descriptor.record_payload,
            )
        if isinstance(prepared, dict):
            return SandboxToolGuard(
                operation=operation,
                request=request,
                policy=policy,
                runtime=runtime,
                denial_payload=json.dumps(prepared),
                record_payload=descriptor.record_payload,
            )
        return SandboxToolGuard(
            operation=operation,
            request=request,
            policy=policy,
            runtime=runtime,
            network_context=prepared,
            record_payload=descriptor.record_payload,
        )

    if policy.network == NetworkMode.PROXY_ALLOWLIST:
        runtime = get_runtime()
        if runtime is None:
            denial = DenialResult(
                reason=DenialReason.RUNTIME_UNCONFIGURED,
                suggested_next_step=SuggestedNextStep.ASK_USER,
                level=policy.level,
                action_fingerprint="",
                message=(
                    "Sandbox runtime is not configured. Managed in-process "
                    "network tools refuse to run."
                ),
                retryable=False,
            )
            return SandboxToolGuard(
                operation=operation,
                request=request,
                policy=policy,
                runtime=runtime,
                denial_payload=json.dumps(denial.to_dict()),
                record_payload=descriptor.record_payload,
            )
        prepared = await _prepare_in_process_managed_network(request, runtime)
        if isinstance(prepared, DenialResult):
            return SandboxToolGuard(
                operation=operation,
                request=request,
                policy=policy,
                runtime=runtime,
                denial_payload=json.dumps(prepared.to_dict()),
                record_payload=descriptor.record_payload,
            )
        if isinstance(prepared, dict):
            return SandboxToolGuard(
                operation=operation,
                request=request,
                policy=policy,
                runtime=runtime,
                denial_payload=json.dumps(prepared),
                record_payload=descriptor.record_payload,
            )
        return SandboxToolGuard(
            operation=operation,
            request=request,
            policy=policy,
            runtime=runtime,
            network_context=prepared,
            record_payload=descriptor.record_payload,
        )

    return SandboxToolGuard(
        operation=operation,
        request=request,
        policy=policy,
        runtime=get_runtime(),
        record_payload=descriptor.record_payload,
    )


async def run_tool_handler_with_operation_guard(
    handler: SandboxToolHandler,
    arguments: Mapping[str, Any],
    guard: SandboxToolGuard,
) -> Any:
    if guard.denial_payload is not None:
        return guard.denial_payload
    if guard.network_context is None:
        return await handler(**dict(arguments))
    if guard.request is None or guard.runtime is None:
        raise SandboxBackendError("sandbox tool guard is missing network context")
    from opensquilla.sandbox.integration import _run_in_process_with_managed_network

    return await _run_in_process_with_managed_network(
        handler,
        (),
        dict(arguments),
        request=guard.request,
        runtime=guard.runtime,
        context=guard.network_context,
    )


async def record_tool_operation_success(
    guard: SandboxToolGuard,
    payload: Any,
) -> None:
    if guard.request is None or not guard.record_payload:
        return
    from opensquilla.sandbox.integration import record_success

    await record_success(guard.request, payload, runtime=guard.runtime)


__all__ = [
    "SANDBOX_FILESYSTEM_WRITE_KINDS",
    "ArtifactOperationRequest",
    "CustomOperationRequest",
    "FilesystemOperationRequest",
    "MediaOperationRequest",
    "NetworkOperationRequest",
    "OperationApproval",
    "OperationPermissions",
    "ProcessOperationRequest",
    "SandboxOperation",
    "SandboxOperationDomain",
    "SandboxOperationRequest",
    "SandboxOperationResult",
    "SandboxToolDescriptor",
    "SandboxToolGuard",
    "SandboxOperationRuntime",
    "backend_supports_operation",
    "prepare_tool_operation_guard",
    "record_tool_operation_success",
    "run_tool_handler_with_operation_guard",
    "run_with_backend_if_supported",
]
