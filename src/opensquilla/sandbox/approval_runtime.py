"""Exact sandbox approval actions and suspended-call continuation state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from opensquilla.sandbox.elevation import SandboxPermissionIntent
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.tool_boundary import ToolCall, ToolContinuation

ApprovalActionKind = Literal[
    "shell",
    "exec_command",
    "apply_patch",
    "filesystem",
    "code",
    "media",
    "network",
]

_PRIVATE_PAYLOAD_KEYS = frozenset({"body", "code", "content", "patch", "stdin"})


class SandboxOverride(StrEnum):
    USE_DEFAULT = "use_default"
    DANGER_FULL_ACCESS = "danger_full_access"
    NO_OVERRIDE = "no_override"


@dataclass(frozen=True)
class ApprovalAction:
    """The exact action reviewed before a sandbox override."""

    kind: ApprovalActionKind
    call_id: str
    tool_name: str
    cwd: Path
    payload: Mapping[str, Any]
    sandbox_permissions: SandboxPermissionIntent = "require_escalated"
    justification: str = ""

    @classmethod
    def apply_patch(
        cls,
        *,
        call_id: str,
        cwd: Path,
        files: tuple[Path, ...],
        patch: str,
        justification: str,
    ) -> ApprovalAction:
        return cls(
            kind="apply_patch",
            call_id=call_id,
            tool_name="apply_patch",
            cwd=cwd,
            payload={"files": [str(path) for path in files], "patch": patch},
            justification=justification,
        )

    @classmethod
    def filesystem(
        cls,
        *,
        call_id: str,
        tool_name: str,
        cwd: Path,
        paths: tuple[tuple[Path, str], ...],
        payload: Mapping[str, Any],
        justification: str,
    ) -> ApprovalAction:
        return cls(
            kind="filesystem",
            call_id=call_id,
            tool_name=tool_name,
            cwd=cwd,
            payload={
                "paths": [[str(path), access] for path, access in paths],
                **dict(payload),
            },
            justification=justification,
        )

    def audit_payload(self) -> dict[str, object]:
        safe_payload: dict[str, Any] = {}
        for key, value in self.payload.items():
            if key not in _PRIVATE_PAYLOAD_KEYS:
                safe_payload[str(key)] = _jsonish(value)
                continue
            text = value if isinstance(value, str) else json.dumps(_jsonish(value))
            safe_payload[f"{key}_length"] = len(text)
            safe_payload[f"{key}_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        return {
            "kind": self.kind,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "cwd": str(self.cwd),
            "sandbox_permissions": self.sandbox_permissions,
            "justification": self.justification,
            "payload": safe_payload,
        }


@dataclass
class SuspendedToolRequest:
    """One original tool request paused at the approval boundary."""

    tool_call: ToolCall
    action: ApprovalAction
    state: Literal["suspended", "approved", "executing", "completed", "denied"] = (
        "suspended"
    )
    approval_id: str | None = None
    execution_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def approve(self, approval_id: str) -> ToolCall:
        if self.state != "suspended":
            raise RuntimeError(f"cannot approve request in state {self.state}")
        self.approval_id = approval_id
        self.state = "approved"
        self.tool_call.continuation = ToolContinuation(
            approval_id=approval_id,
            tool_use_id=self.tool_call.tool_use_id,
            session_key=str(self.metadata.get("session_key") or ""),
        )
        return self.tool_call

    def deny(self, approval_id: str) -> None:
        if self.state != "suspended":
            raise RuntimeError(f"cannot deny request in state {self.state}")
        self.approval_id = approval_id
        self.state = "denied"

    def begin_execution(self) -> ToolCall:
        if self.state != "approved":
            raise RuntimeError(f"cannot execute request in state {self.state}")
        self.state = "executing"
        self.execution_count += 1
        return self.tool_call

    def complete(self) -> None:
        if self.state != "executing":
            raise RuntimeError(f"cannot complete request in state {self.state}")
        self.state = "completed"


def select_sandbox_override(
    intent: SandboxPermissionIntent,
    profile: FileSystemPermissionProfile,
) -> SandboxOverride:
    if intent == "use_default":
        return SandboxOverride.USE_DEFAULT
    if not profile.unsandboxed_execution_allowed:
        return SandboxOverride.NO_OVERRIDE
    return SandboxOverride.DANGER_FULL_ACCESS


def _jsonish(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonish(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonish(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = [
    "ApprovalAction",
    "SandboxOverride",
    "SuspendedToolRequest",
    "select_sandbox_override",
]
