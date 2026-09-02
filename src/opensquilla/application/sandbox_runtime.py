"""Transport-neutral sandbox use cases.

The application Module owns ordering, validation, immutable results, and
domain errors. Gateway-specific context, scopes, wire field names, persistence,
and process-global registries stay behind the narrow Ports below.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class SandboxSetupState(StrEnum):
    NOT_SETUP = "not_setup"
    SETTING_UP = "setting_up"
    READY = "ready"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SandboxSetupStatus:
    state: SandboxSetupState
    platform: str
    message: str
    requires_admin: bool = False
    detail: str | None = None

@dataclass(frozen=True, slots=True)
class SandboxCapability:
    available: bool
    backend: str
    platform: str
    code: str
    reason: str
    setup_supported: bool
    restart_required: bool
    probe_version: int
    capabilities: frozenset[str]


class SandboxSystemToolsPolicy(StrEnum):
    AUTO = "auto"
    PROMPT = "prompt"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class SandboxFilePolicy:
    custom_deny_write_paths: tuple[str, ...]
    recursive_delete_backup_enabled: bool
    backup_quota_bytes: int


@dataclass(frozen=True, slots=True)
class SandboxCommandPolicy:
    require_approval_prefixes: tuple[tuple[str, ...], ...]
    auto_allow_prefixes: tuple[tuple[str, ...], ...]
    system_tools: SandboxSystemToolsPolicy


@dataclass(frozen=True, slots=True)
class SandboxNetworkPolicy:
    block_all_network: bool
    allow_domains: tuple[str, ...]
    deny_domains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SandboxRuntimePolicy:
    enabled: bool
    python: bool
    node: bool
    git_bash: bool


@dataclass(frozen=True, slots=True)
class SandboxPolicyDocument:
    """Immutable, typed policy crossing the application seam."""

    schema_version: Literal[2]
    policy_version: int
    files: SandboxFilePolicy
    commands: SandboxCommandPolicy
    network: SandboxNetworkPolicy
    runtimes: SandboxRuntimePolicy

    def __post_init__(self) -> None:
        if self.schema_version != 2:
            raise ValueError("sandbox policy schema version must be 2")
        if (
            isinstance(self.policy_version, bool)
            or not isinstance(self.policy_version, int)
            or self.policy_version < 0
        ):
            raise ValueError("sandbox policy version must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SandboxRuntimeVersion:
    component_id: str
    version: str
    available: bool


@dataclass(frozen=True, slots=True)
class SandboxPolicyDefaults:
    builtin_deny_write_paths: tuple[str, ...]
    runtime_target: str | None
    runtime_versions: tuple[SandboxRuntimeVersion, ...]


class SandboxRuntimeSource(StrEnum):
    OSS = "oss"
    GITHUB = "github"


class SandboxRuntimeAvailability(StrEnum):
    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    READY = "ready"
    CORRUPT = "corrupt"


class SandboxRuntimeOperationKind(StrEnum):
    INSTALL = "install"
    REMOVE = "remove"


class SandboxRuntimeOperationState(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    EXTRACTING = "extracting"
    PROBING = "probing"
    ACTIVATING = "activating"
    CANCELLING = "cancelling"
    REMOVING = "removing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SandboxRuntimeError:
    code: str
    message: str
    retryable: bool
    source: SandboxRuntimeSource | None = None


@dataclass(frozen=True, slots=True)
class SandboxRuntimeOperation:
    operation_id: str
    component_id: str
    kind: SandboxRuntimeOperationKind
    state: SandboxRuntimeOperationState
    progress_bytes: int
    total_bytes: int
    source: SandboxRuntimeSource | None
    started_at_ms: int
    updated_at_ms: int
    error: SandboxRuntimeError | None = None

    @property
    def progress_percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, max(0, int(self.progress_bytes * 100 / self.total_bytes)))


@dataclass(frozen=True, slots=True)
class SandboxRuntimeComponent:
    component_id: str
    availability: SandboxRuntimeAvailability
    catalog_version: str | None
    active_version: str | None
    installed_bytes: int | None
    removable: bool
    resume_available: bool
    resume_bytes: int
    operation: SandboxRuntimeOperation | None
    last_error: SandboxRuntimeError | None


@dataclass(frozen=True, slots=True)
class SandboxRuntimePackDocument:
    """Immutable, typed Runtime Pack status crossing the application seam."""

    schema_version: int
    management_supported: bool
    target: str | None
    catalog_version: str | None
    source_order: tuple[SandboxRuntimeSource, ...]
    components: tuple[SandboxRuntimeComponent, ...]
    next_poll_after_ms: int


class SandboxRunMode(StrEnum):
    SAFE = "safe"
    FULL = "full"

    @classmethod
    def parse(cls, value: object) -> SandboxRunMode:
        try:
            return cls(str(value or "").strip())
        except ValueError as exc:
            raise ValueError("sandbox run mode must be safe or full") from exc


@dataclass(frozen=True, slots=True)
class SandboxRunModePreference:
    mode: SandboxRunMode
    source: str


@dataclass(frozen=True, slots=True)
class SandboxRuntimePackSnapshot:
    status: SandboxRuntimePackDocument


@dataclass(frozen=True, slots=True)
class SandboxRuntimeOperationResult:
    operation: SandboxRuntimeOperation


@dataclass(frozen=True, slots=True)
class SandboxRuntimeDiscardResult:
    status: SandboxRuntimePackDocument


@dataclass(frozen=True, slots=True)
class SandboxResumeResult:
    session_key: str
    resumed: bool
    autonomous_paused: bool = False


class SandboxApplicationError(RuntimeError):
    """Base class for domain errors projected by a transport Adapter."""


@dataclass(frozen=True, slots=True)
class SandboxCapabilityUnavailableError(SandboxApplicationError):
    report: SandboxCapability

    def __str__(self) -> str:
        return "Safe mode cannot be enabled because sandbox initialization is not ready."


@dataclass(frozen=True, slots=True)
class SandboxPolicyConflictError(SandboxApplicationError):
    expected_version: int
    current_policy: SandboxPolicyDocument

    def __str__(self) -> str:
        return "The sandbox policy changed in another client."


@dataclass(frozen=True, slots=True)
class SandboxRuntimeOperationConflictError(SandboxApplicationError):
    component_id: str

    def __str__(self) -> str:
        return "The Runtime Pack operation changed; refresh its status and try again."


@dataclass(frozen=True, slots=True)
class SandboxRuntimeDiscardError(SandboxApplicationError):
    component_id: str

    def __str__(self) -> str:
        return (
            "Runtime Pack downloaded data could not be removed. "
            "Retry after closing running tools."
        )


@dataclass(frozen=True, slots=True)
class SandboxRuntimeIdentityError(SandboxApplicationError):
    expected_component_id: str
    actual_component_id: str
    expected_operation_id: str | None = None
    actual_operation_id: str | None = None

    def __str__(self) -> str:
        return "The Runtime Pack operation identity did not match the request."


@dataclass(frozen=True, slots=True)
class SandboxUnavailableError(SandboxApplicationError):
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class SandboxSetupPort(Protocol):
    async def read_setup_status(self) -> SandboxSetupStatus: ...

    async def ensure_setup(self) -> SandboxSetupStatus: ...

    async def read_capability(self, *, refresh: bool) -> SandboxCapability: ...


class SandboxPolicyPort(Protocol):
    async def read_policy(self) -> SandboxPolicyDocument: ...

    async def read_policy_defaults(self) -> SandboxPolicyDefaults: ...

    async def replace_policy(
        self,
        base_policy_version: int,
        policy: SandboxPolicyDocument,
    ) -> SandboxPolicyDocument: ...


class SandboxRunModePort(Protocol):
    async def read_run_mode(self) -> SandboxRunModePreference: ...

    async def persist_run_mode(self, mode: SandboxRunMode) -> SandboxRunMode: ...


class SandboxRunModeEventsPort(Protocol):
    async def publish_run_mode_changed(self, preference: SandboxRunModePreference) -> None: ...


class SandboxRuntimePackPort(Protocol):
    async def read_runtime_status(self) -> SandboxRuntimePackDocument: ...

    async def install_runtime(self, component_id: str) -> SandboxRuntimeOperation: ...

    async def cancel_runtime(
        self,
        component_id: str,
        operation_id: str,
    ) -> SandboxRuntimeOperation: ...

    async def remove_runtime(self, component_id: str) -> SandboxRuntimeOperation: ...

    async def discard_runtime_download(
        self,
        component_id: str,
    ) -> SandboxRuntimePackDocument: ...


class SandboxResumePort(Protocol):
    async def clear_pause(self, session_key: str) -> bool: ...


class SandboxRuntime:
    """Deep application Module for explicit sandbox use cases."""

    def __init__(
        self,
        *,
        setup: SandboxSetupPort,
        policy: SandboxPolicyPort,
        run_modes: SandboxRunModePort,
        run_mode_events: SandboxRunModeEventsPort,
        runtime_packs: SandboxRuntimePackPort,
        resume: SandboxResumePort,
    ) -> None:
        self._setup = setup
        self._policy = policy
        self._run_modes = run_modes
        self._run_mode_events = run_mode_events
        self._runtime_packs = runtime_packs
        self._resume = resume

    async def inspect_setup(self) -> SandboxSetupStatus:
        return await self._setup.read_setup_status()

    async def prepare(self) -> SandboxSetupStatus:
        return await self._setup.ensure_setup()

    async def inspect_capability(self, *, refresh: bool = False) -> SandboxCapability:
        return await self._setup.read_capability(refresh=bool(refresh))

    async def read_policy(self) -> SandboxPolicyDocument:
        return await self._policy.read_policy()

    async def read_policy_defaults(self) -> SandboxPolicyDefaults:
        return await self._policy.read_policy_defaults()

    async def replace_policy(
        self,
        base_policy_version: int,
        policy: SandboxPolicyDocument,
    ) -> SandboxPolicyDocument:
        if isinstance(base_policy_version, bool) or not isinstance(base_policy_version, int):
            raise ValueError("base policy version must be an integer")
        if not isinstance(policy, SandboxPolicyDocument):
            raise ValueError("sandbox policy must be a typed policy document")
        return await self._policy.replace_policy(base_policy_version, policy)

    async def read_run_mode(self) -> SandboxRunModePreference:
        return await self._run_modes.read_run_mode()

    async def select_run_mode(self, mode: str | SandboxRunMode) -> SandboxRunModePreference:
        requested = SandboxRunMode.parse(mode)
        if requested is SandboxRunMode.SAFE:
            report = await self._setup.read_capability(refresh=False)
            if not report.available:
                raise SandboxCapabilityUnavailableError(report)

        confirmed = await self._run_modes.persist_run_mode(requested)
        preference = SandboxRunModePreference(confirmed, "preference")
        await self._run_mode_events.publish_run_mode_changed(preference)
        return preference

    async def inspect_runtime_packs(self) -> SandboxRuntimePackSnapshot:
        return SandboxRuntimePackSnapshot(await self._runtime_packs.read_runtime_status())

    async def install_runtime_pack(self, component_id: str) -> SandboxRuntimeOperationResult:
        operation = await self._runtime_packs.install_runtime(component_id)
        self._require_runtime_identity(operation, component_id)
        return SandboxRuntimeOperationResult(operation)

    async def cancel_runtime_pack_install(
        self,
        component_id: str,
        operation_id: str,
    ) -> SandboxRuntimeOperationResult:
        operation = await self._runtime_packs.cancel_runtime(component_id, operation_id)
        self._require_runtime_identity(operation, component_id, operation_id)
        return SandboxRuntimeOperationResult(operation)

    async def remove_runtime_pack(self, component_id: str) -> SandboxRuntimeOperationResult:
        operation = await self._runtime_packs.remove_runtime(component_id)
        self._require_runtime_identity(operation, component_id)
        return SandboxRuntimeOperationResult(operation)

    async def discard_runtime_pack_download(
        self,
        component_id: str,
    ) -> SandboxRuntimeDiscardResult:
        status = await self._runtime_packs.discard_runtime_download(component_id)
        return SandboxRuntimeDiscardResult(status)

    async def resume_paused_session(self, session_key: str) -> SandboxResumeResult:
        normalized = str(session_key or "").strip()
        if not normalized:
            raise ValueError("session key is required")
        resumed = await self._resume.clear_pause(normalized)
        return SandboxResumeResult(session_key=normalized, resumed=bool(resumed))

    @staticmethod
    def _require_runtime_identity(
        operation: SandboxRuntimeOperation,
        component_id: str,
        operation_id: str | None = None,
    ) -> None:
        component_matches = operation.component_id == component_id
        operation_matches = operation_id is None or operation.operation_id == operation_id
        if component_matches and operation_matches:
            return
        raise SandboxRuntimeIdentityError(
            expected_component_id=component_id,
            actual_component_id=operation.component_id,
            expected_operation_id=operation_id,
            actual_operation_id=operation.operation_id,
        )


__all__ = [
    "SandboxApplicationError",
    "SandboxCapability",
    "SandboxCapabilityUnavailableError",
    "SandboxCommandPolicy",
    "SandboxFilePolicy",
    "SandboxNetworkPolicy",
    "SandboxPolicyConflictError",
    "SandboxPolicyDefaults",
    "SandboxPolicyDocument",
    "SandboxPolicyPort",
    "SandboxResumePort",
    "SandboxResumeResult",
    "SandboxRunMode",
    "SandboxRunModeEventsPort",
    "SandboxRunModePort",
    "SandboxRunModePreference",
    "SandboxRuntime",
    "SandboxRuntimeAvailability",
    "SandboxRuntimeComponent",
    "SandboxRuntimeDiscardError",
    "SandboxRuntimeDiscardResult",
    "SandboxRuntimeError",
    "SandboxRuntimeIdentityError",
    "SandboxRuntimeOperation",
    "SandboxRuntimeOperationConflictError",
    "SandboxRuntimeOperationKind",
    "SandboxRuntimeOperationResult",
    "SandboxRuntimeOperationState",
    "SandboxRuntimePackPort",
    "SandboxRuntimePackDocument",
    "SandboxRuntimePackSnapshot",
    "SandboxRuntimePolicy",
    "SandboxRuntimeSource",
    "SandboxRuntimeVersion",
    "SandboxSetupState",
    "SandboxSetupStatus",
    "SandboxSetupPort",
    "SandboxSystemToolsPolicy",
    "SandboxUnavailableError",
]
