"""Core sandbox value types.

This module defines the plain-data vocabulary the rest of the ``opensquilla.sandbox``
package works with: the security level enum, the shape of a network mode, the
mount specification, the assembled :class:`SandboxPolicy`, and the
request/result pair the backends consume and produce.

The types intentionally stay decoupled from the gateway settings model. A
settings object is translated into a :class:`SandboxPolicy` by
``opensquilla.sandbox.policy`` so that the policy object has no dependency on
Pydantic and can be constructed directly in unit tests.

``SandboxResult`` here is the *backend* outcome shape; it is distinct from the
``SandboxResult`` in :mod:`opensquilla.safety.sandbox`, which describes a resource-
limited subprocess run. The two types are composed (the noop backend and the
bubblewrap fallback path build a backend result from a safety-layer result),
never merged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path, PurePath, PurePosixPath
from typing import Literal

from opensquilla.sandbox.permissions import FileSystemPermissionProfile


class SecurityLevel(IntEnum):
    """Security grading levels ordered by strictness.

    Integer ordering is load-bearing: callers can write ``level >= STRICT`` to
    mean "at least strict", and the :mod:`policy` module uses comparisons to
    decide whether approval is required. Do not reorder.

    * ``DISABLED`` — legacy/compatibility mode. Only reachable when
      ``SandboxSettings.allow_legacy_mode`` is explicitly true. Never the
      silent default.
    * ``STANDARD`` — default for normal agent tool execution: workspace-rw,
      ephemeral tmp, ``network=none``, modest resource caps.
    * ``STRICT`` — for higher-risk actions: read-only root, writable only to
      the workspace, tighter limits, may require human approval.
    * ``LOCKED`` — minimum-visibility deny-by-default posture with required
      approval. Reserved for untrusted or injection-exposed execution paths.
    """

    DISABLED = 0
    STANDARD = 1
    STRICT = 2
    LOCKED = 3

    @property
    def label(self) -> str:
        return {
            SecurityLevel.DISABLED: "L0-disabled",
            SecurityLevel.STANDARD: "L1-standard",
            SecurityLevel.STRICT: "L2-strict",
            SecurityLevel.LOCKED: "L3-locked",
        }[self]


class NetworkMode(StrEnum):
    """Network visibility selected by a :class:`SandboxPolicy`.

    ``NONE`` is a real restriction (the Linux backend unshares the network
    namespace); ``HOST`` is only valid when the sandbox is disabled entirely
    or the caller has explicitly opted in via a future allowlist path.
    ``PROXY_ALLOWLIST`` routes egress through a local managed proxy so the
    backend can keep raw network access unavailable while applying domain
    allowlist decisions before forwarding.
    """

    NONE = "none"
    PROXY_ALLOWLIST = "proxy_allowlist"
    HOST = "host"


@dataclass(frozen=True)
class NetworkProxySpec:
    """Local proxy endpoint assigned to a managed-network sandbox."""

    host: str
    port: int


MountMode = Literal["ro", "rw"]
SANDBOX_WORKSPACE_PATH = PurePosixPath("/workspace")


def sandbox_path_text(path: str | PurePath) -> str:
    """Return the sandbox-side path as POSIX text regardless of host OS."""

    if isinstance(path, PurePath):
        return path.as_posix()
    return str(path).replace("\\", "/")


@dataclass(frozen=True)
class MountSpec:
    """A single host→sandbox path mapping.

    ``required`` distinguishes essentials (the workspace) from optional extras
    (e.g. a user-configured cache directory). The bubblewrap backend refuses
    to start if a required mount's host path does not exist; optional mounts
    are skipped with a debug log so operator typos don't block execution.
    """

    host_path: Path
    sandbox_path: PurePath
    mode: MountMode = "ro"
    required: bool = True

    def with_mode(self, mode: MountMode) -> MountSpec:
        return MountSpec(
            host_path=self.host_path,
            sandbox_path=self.sandbox_path,
            mode=mode,
            required=self.required,
        )


@dataclass(frozen=True)
class ResourceLimits:
    """Resource caps applied inside the sandbox.

    Units are explicit in the field names so callers never have to guess.
    ``pids`` is advisory — the Linux backend maps it through the kernel's
    PID namespace rlimit when available; the noop backend ignores it and
    relies on :func:`opensquilla.safety.sandbox.run_sandboxed` for CPU/memory.
    """

    cpu_seconds: int = 30
    memory_mb: int = 1024
    pids: int = 256
    wall_timeout_s: float = 60.0


@dataclass(frozen=True)
class SandboxPolicy:
    """Fully-resolved policy for a single execution.

    Produced by :func:`opensquilla.sandbox.policy.build_policy` once the action
    kind and security level are known. Backends consume this directly and
    must never mutate it.
    """

    level: SecurityLevel
    network: NetworkMode
    mounts: tuple[MountSpec, ...]
    workspace_rw: bool
    tmp_writable: bool
    limits: ResourceLimits
    env_allowlist: tuple[str, ...]
    require_approval: bool
    description: str = ""
    network_proxy: NetworkProxySpec | None = None
    unreadable_globs: tuple[str, ...] = ()
    file_system: FileSystemPermissionProfile | None = None

    def summary(self) -> dict[str, object]:
        """Flat structured summary used in log lines and debug output."""
        network_proxy = (
            {"host": self.network_proxy.host, "port": self.network_proxy.port}
            if self.network_proxy is not None
            else None
        )
        return {
            "level": self.level.label,
            "network": self.network.value,
            "network_proxy": network_proxy,
            "mounts": [
                {
                    "host": str(m.host_path),
                    "sandbox": sandbox_path_text(m.sandbox_path),
                    "mode": m.mode,
                }
                for m in self.mounts
            ],
            "workspace_rw": self.workspace_rw,
            "tmp_writable": self.tmp_writable,
            "cpu_seconds": self.limits.cpu_seconds,
            "memory_mb": self.limits.memory_mb,
            "wall_timeout_s": self.limits.wall_timeout_s,
            "require_approval": self.require_approval,
            "unreadable_globs": list(self.unreadable_globs),
            "file_system": (
                {
                    "entries": [
                        {"path": str(entry.path), "access": entry.access.value}
                        for entry in self.file_system.entries
                    ],
                    "denied_read_globs": list(self.file_system.denied_read_globs),
                }
                if self.file_system is not None
                else None
            ),
        }


@dataclass(frozen=True)
class SandboxRequest:
    """A unit of work to run under the sandbox.

    ``action_kind`` is a short dotted tag (``"shell.exec"``, ``"code.exec"``,
    ``"patch.apply"``) used by the policy layer to select a level and by
    governance to bucket denial signatures. ``reason`` is a short free-text
    description the backend includes in log lines for auditability.
    """

    argv: tuple[str, ...]
    cwd: Path
    action_kind: str
    policy: SandboxPolicy
    stdin: bytes | None = None
    env: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    session_id: str = ""
    run_mode: str = ""

    def with_policy(self, policy: SandboxPolicy) -> SandboxRequest:
        return SandboxRequest(
            argv=self.argv,
            cwd=self.cwd,
            action_kind=self.action_kind,
            policy=policy,
            stdin=self.stdin,
            env=dict(self.env),
            reason=self.reason,
            session_id=self.session_id,
            run_mode=self.run_mode,
        )


@dataclass
class SandboxResult:
    """Outcome of a backend run.

    ``truncated_stdout`` / ``truncated_stderr`` flag when the backend had to
    cap output to keep the agent context bounded. ``policy_used`` is a
    diagnostic summary (:meth:`SandboxPolicy.summary`) — we deliberately do
    not serialise the policy object itself so logs stay grep-friendly.
    """

    returncode: int
    stdout: str
    stderr: str
    wall_time_s: float
    backend_used: str
    policy_used: dict[str, object] = field(default_factory=dict)
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    timed_out: bool = False
    backend_notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class SandboxBackendError(RuntimeError):
    """Raised when a backend cannot run a request.

    Distinct from a non-zero exit code: this is for setup failures (missing
    ``bwrap`` binary, invalid mount path, unsupported platform). Callers must
    surface it as a denial or propagate — *never* fall back to unsandboxed
    host execution.
    """


class DenialReason(StrEnum):
    """Why a request was denied.

    These values are serialised into the denial envelope seen by tool callers
    and by the orchestration layer, so the exact strings form part of the
    public contract.
    """

    HUMAN_REJECTED = "human_rejected"
    POLICY_DENIED = "policy_denied"
    THRESHOLD_EXCEEDED = "threshold_exceeded"
    REPEATED_SAME_INTENT = "repeated_same_intent"
    RUNTIME_UNCONFIGURED = "runtime_unconfigured"
    SEATBELT_DENIED = "seatbelt_denied"


class SuggestedNextStep(StrEnum):
    """The single follow-up the agent is permitted after a denial (§8.4)."""

    REPLAN = "replan"
    ASK_USER = "ask_user"
    LOWER_PRIVILEGE = "lower_privilege"
    NARROWER_APPROVAL = "narrower_approval"


class FollowupTag(StrEnum):
    """How the agent tagged a follow-up request after a prior denial.

    The post-denial guard consumes this to allow only the three flavours
    listed in §8.4 and to block silent retries.
    """

    NONE = "none"
    LOWER_PRIVILEGE = "lower_privilege"
    EXPLAIN = "explain"
    NARROWER_APPROVAL = "narrower_approval"


@dataclass(frozen=True)
class DenialResult:
    """Structured denial event returned from the approval gate.

    This is the §8.2 envelope. It is *disjoint* from the tool failure
    envelope in :mod:`opensquilla.tools.envelope` — consumers distinguish the two
    on the ``status`` field (``"denied"`` here, ``"error"`` there).
    """

    reason: DenialReason
    suggested_next_step: SuggestedNextStep
    level: SecurityLevel
    action_fingerprint: str
    message: str
    retryable: bool = True
    status: Literal["denied"] = "denied"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason.value,
            "suggested_next_step": self.suggested_next_step.value,
            "level": self.level.label,
            "action_fingerprint": self.action_fingerprint,
            "message": self.message,
            "retryable": self.retryable,
        }


class _AllowSentinel:
    """Sentinel type; :data:`ALLOW` is the only instance.

    Using a dedicated class rather than ``bool`` or ``None`` lets callers
    match ``isinstance(decision, DenialResult)`` for the deny branch and
    ``decision is ALLOW`` for the allow branch without ambiguity.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "ALLOW"

    def __bool__(self) -> bool:
        return True


ALLOW: _AllowSentinel = _AllowSentinel()

ApprovalDecision = _AllowSentinel | DenialResult


__all__ = [
    "ALLOW",
    "ApprovalDecision",
    "DenialReason",
    "DenialResult",
    "FollowupTag",
    "MountMode",
    "MountSpec",
    "NetworkMode",
    "NetworkProxySpec",
    "ResourceLimits",
    "SANDBOX_WORKSPACE_PATH",
    "SandboxBackendError",
    "SandboxPolicy",
    "SandboxRequest",
    "SandboxResult",
    "SecurityLevel",
    "SuggestedNextStep",
    "sandbox_path_text",
]
