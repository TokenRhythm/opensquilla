"""JSON payloads for the Linux sandbox helper."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opensquilla.sandbox.backend.linux_paths import canonical_linux_policy
from opensquilla.sandbox.operation_runtime import FilesystemOperationRequest, SandboxOperation
from opensquilla.sandbox.types import (
    SandboxPolicy,
    SandboxRequest,
    sandbox_path_text,
)

OperationType = Literal["process", "filesystem"]


@dataclass(frozen=True)
class ProcessHelperPayload:
    argv: list[str]
    stdin_base64: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "stdinBase64": self.stdin_base64,
        }

    @classmethod
    def from_json(cls, value: object) -> ProcessHelperPayload | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("process payload must be an object")
        argv = value.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ValueError("process.argv must be a list of strings")
        stdin = value.get("stdinBase64")
        if stdin is not None and not isinstance(stdin, str):
            raise ValueError("process.stdinBase64 must be a string")
        return cls(argv=list(argv), stdin_base64=stdin)


@dataclass(frozen=True)
class FilesystemHelperPayload:
    kind: str
    worker_payload_path: str
    worker_payload: dict[str, Any]

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "workerPayloadPath": self.worker_payload_path,
            "workerPayload": self.worker_payload,
        }

    @classmethod
    def from_json(cls, value: object) -> FilesystemHelperPayload | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("filesystem payload must be an object")
        kind = value.get("kind")
        worker_payload_path = value.get("workerPayloadPath")
        worker_payload = value.get("workerPayload")
        if not isinstance(kind, str) or not kind:
            raise ValueError("filesystem.kind must be a non-empty string")
        if not isinstance(worker_payload_path, str) or not worker_payload_path:
            raise ValueError("filesystem.workerPayloadPath must be a non-empty string")
        if not isinstance(worker_payload, dict):
            raise ValueError("filesystem.workerPayload must be an object")
        return cls(
            kind=kind,
            worker_payload_path=worker_payload_path,
            worker_payload=dict(worker_payload),
        )


@dataclass(frozen=True)
class HelperPayload:
    operation_type: OperationType
    action_kind: str
    run_mode: str
    session_id: str
    cwd: str
    env: dict[str, str]
    policy: dict[str, Any]
    process: ProcessHelperPayload | None
    filesystem: FilesystemHelperPayload | None

    def to_json(self) -> dict[str, object]:
        return {
            "operationType": self.operation_type,
            "actionKind": self.action_kind,
            "runMode": self.run_mode,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "env": dict(self.env),
            "policy": self.policy,
            "process": self.process.to_json() if self.process is not None else None,
            "filesystem": self.filesystem.to_json() if self.filesystem is not None else None,
        }

    @classmethod
    def from_json(cls, value: object) -> HelperPayload:
        if not isinstance(value, dict):
            raise ValueError("helper payload must be an object")
        operation_type = value.get("operationType")
        if operation_type not in {"process", "filesystem"}:
            raise ValueError(f"unknown operationType: {operation_type!r}")
        env = value.get("env")
        policy = value.get("policy")
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in env.items()
        ):
            raise ValueError("env must be an object of strings")
        if not isinstance(policy, dict):
            raise ValueError("policy must be an object")
        return cls(
            operation_type=operation_type,
            action_kind=_required_string(value, "actionKind"),
            run_mode=_required_string(value, "runMode"),
            session_id=_required_string(value, "sessionId"),
            cwd=_required_string(value, "cwd"),
            env={str(key): str(item) for key, item in env.items()},
            policy=dict(policy),
            process=ProcessHelperPayload.from_json(value.get("process")),
            filesystem=FilesystemHelperPayload.from_json(value.get("filesystem")),
        )


def _required_string(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def encode_payload(payload: HelperPayload) -> str:
    return json.dumps(payload.to_json(), ensure_ascii=False, sort_keys=True)


def encode_policy_b64(policy: SandboxPolicy | dict[str, Any]) -> str:
    payload = _policy_payload(policy) if isinstance(policy, SandboxPolicy) else dict(policy)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_payload(raw: str) -> HelperPayload:
    return HelperPayload.from_json(json.loads(raw))


def build_process_helper_payload(request: SandboxRequest) -> HelperPayload:
    return HelperPayload(
        operation_type="process",
        action_kind=request.action_kind,
        run_mode=request.run_mode,
        session_id=request.session_id,
        cwd=_sandbox_cwd(request),
        env=_env_payload(request.policy, request.env),
        policy=_policy_payload(request.policy),
        process=ProcessHelperPayload(
            argv=list(request.argv),
            stdin_base64=base64.b64encode(request.stdin).decode("ascii")
            if request.stdin is not None
            else None,
        ),
        filesystem=None,
    )


def build_filesystem_helper_payload(
    operation: SandboxOperation,
    *,
    policy: SandboxPolicy,
    session_id: str,
    worker_payload_path: Path,
) -> HelperPayload:
    if not isinstance(operation.request, FilesystemOperationRequest):
        raise ValueError("filesystem operation request is required")
    worker_payload = operation.request.to_payload()
    worker_payload["kind"] = operation.kind
    return HelperPayload(
        operation_type="filesystem",
        action_kind=f"fs.worker.{operation.kind}",
        run_mode=operation.run_mode,
        session_id=session_id,
        cwd=str(worker_payload_path.parent),
        env={},
        policy=_policy_payload(policy),
        process=None,
        filesystem=FilesystemHelperPayload(
            kind=operation.kind,
            worker_payload_path=str(worker_payload_path),
            worker_payload=worker_payload,
        ),
    )


def _policy_payload(policy: SandboxPolicy) -> dict[str, Any]:
    policy = canonical_linux_policy(policy)
    return {
        "level": policy.level.label,
        "network": policy.network.value,
        "mounts": [
            {
                "host": str(mount.host_path),
                "sandbox": sandbox_path_text(mount.sandbox_path),
                "mode": mount.mode,
                "required": mount.required,
            }
            for mount in policy.mounts
        ],
        "envAllowlist": list(policy.env_allowlist),
        "unreadableGlobs": list(policy.unreadable_globs),
        "fileSystem": (
            {
                "entries": [
                    {"path": str(entry.path), "access": entry.access.value}
                    for entry in policy.file_system.entries
                ],
                "deniedReadGlobs": list(policy.file_system.denied_read_globs),
                "defaultAccess": policy.file_system.default_access.value,
            }
            if policy.file_system is not None
            else None
        ),
        "tmpWritable": policy.tmp_writable,
        "cpuSeconds": policy.limits.cpu_seconds,
        "memoryMb": policy.limits.memory_mb,
        "pids": policy.limits.pids,
        "wallTimeoutS": policy.limits.wall_timeout_s,
    }


def _env_payload(policy: SandboxPolicy, override_env: dict[str, str]) -> dict[str, str]:
    allowlist = set(policy.env_allowlist)
    env: dict[str, str] = {}
    for key in policy.env_allowlist:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for key, value in override_env.items():
        if key in allowlist:
            env[key] = value
    return env


def _sandbox_cwd(request: SandboxRequest) -> str:
    policy = canonical_linux_policy(request.policy)
    for mount in policy.mounts:
        try:
            rel = request.cwd.relative_to(mount.host_path)
        except ValueError:
            continue
        sandbox_root = Path(sandbox_path_text(mount.sandbox_path))
        return str(sandbox_root.joinpath(*rel.parts))
    return str(request.cwd)
