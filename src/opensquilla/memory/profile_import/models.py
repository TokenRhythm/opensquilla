"""Typed contracts for model-assisted profile import."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
PROMPT_VERSION = "profile-fusion-v2"
EXPORT_PROMPT_VERSION = "profile-export-v1"
MAX_RAW_BYTES = 256 * 1024
PREVIEW_TTL_SECONDS = 24 * 60 * 60

_SAFE_AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_OPAQUE_ID = re.compile(r"[a-f0-9]{32}\Z")


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CamelModel(BaseModel):
    """Strict model with camelCase serialization and snake_case Python access."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=False,
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class DecisionOutcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    UNRESOLVED = "unresolved"


class DecisionTarget(StrEnum):
    USER = "USER"
    MEMORY = "MEMORY"
    IMPORT = "IMPORT"
    NONE = "NONE"


class ModelConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FileChangeStatus(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class ImportStatus(StrEnum):
    PREVIEW = "preview"
    APPLIED = "applied"
    DISCARDED = "discarded"
    UNDONE = "undone"


class ProfileImportJobStatus(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    READY = "ready"
    FAILED = "failed"
    APPLIED = "applied"
    DISCARDED = "discarded"


class ProfileImportJobStage(StrEnum):
    READING = "reading"
    MODEL = "model"
    DIFF = "diff"


class FusionCandidate(StrictModel):
    user_md: str
    memory_md: str
    import_md: str | None = None

    @field_validator("user_md", "memory_md", "import_md")
    @classmethod
    def reject_nul(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("candidate content contains a NUL byte")
        return value


class FusionDecision(StrictModel):
    outcome: DecisionOutcome
    target: DecisionTarget
    source_excerpt: str = Field(min_length=1, max_length=2048)
    candidate_excerpt: str = Field(default="", max_length=4096)
    date: str
    model_confidence: ModelConfidence
    reason: str = Field(default="", max_length=2048)

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        if value == "unknown":
            return value
        if not _ISO_DATE.fullmatch(value):
            raise ValueError("date must be YYYY-MM-DD or unknown")
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date is not a valid calendar date") from exc
        return value

    @model_validator(mode="after")
    def validate_outcome_target(self) -> FusionDecision:
        if self.outcome is DecisionOutcome.APPLIED and self.target is DecisionTarget.NONE:
            raise ValueError("applied decisions require a file target")
        if self.outcome is DecisionOutcome.UNRESOLVED:
            if self.target is not DecisionTarget.NONE:
                raise ValueError("unresolved decisions must target NONE")
            if self.candidate_excerpt:
                raise ValueError("unresolved decisions cannot provide candidate content")
        return self


class FusionOutput(StrictModel):
    schema_version: Literal[1]
    candidate: FusionCandidate
    decisions: list[FusionDecision] = Field(default_factory=list, max_length=1000)
    summary: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("summary items must contain 1-500 characters")
        return value


class ModelIdentity(CamelModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    is_loopback: bool = False


class FusionModelRequest(CamelModel):
    """Provider-neutral request passed to the injected single-model adapter."""

    system_prompt: str
    user_prompt: str
    response_schema: dict[str, Any]
    identity: ModelIdentity


FusionCompletion = Callable[[FusionModelRequest], Awaitable[str]]


class ProfileImportPaths(CamelModel):
    """Resolved roots for one agent.

    ``state_dir`` is the shared OpenSquilla state root. Private batches are
    namespaced below ``state_dir/profile-imports/<agent_id>`` so opaque ID
    locators can survive Gateway restarts.
    """

    agent_id: str
    agent_workspace_dir: Path
    memory_workspace_dir: Path
    state_dir: Path
    profile_home_dir: Path | None = None

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if value in {".", ".."} or not _SAFE_AGENT_ID.fullmatch(value):
            raise ValueError("agent_id is not safe for a state namespace")
        return value

    @field_validator(
        "agent_workspace_dir",
        "memory_workspace_dir",
        "state_dir",
        "profile_home_dir",
    )
    @classmethod
    def make_absolute(cls, value: Path | None) -> Path | None:
        # Bind configured symlink/junction roots to their current physical
        # destination. A later service instance will resolve a retargeted root
        # differently and fail the persisted root-identity check.
        return value.expanduser().resolve(strict=False) if value is not None else None

    @property
    def user_path(self) -> Path:
        return self.agent_workspace_dir / "USER.md"

    @property
    def memory_path(self) -> Path:
        return self.memory_workspace_dir / "MEMORY.md"

    @property
    def imports_dir(self) -> Path:
        return self.memory_workspace_dir / "memory" / "imports"

    @property
    def agent_import_state_dir(self) -> Path:
        return self.state_dir / "profile-imports" / self.agent_id

    @property
    def locator_dir(self) -> Path:
        return self.state_dir / "profile-imports" / "_locators"

    @property
    def operation_lock_root(self) -> Path:
        if self.profile_home_dir is not None:
            return self.profile_home_dir
        return self.state_dir.parent if self.state_dir.name == "state" else self.state_dir


class ProfileImportQuotas(CamelModel):
    max_file_size_kb: int = Field(default=1024, ge=0)
    max_total_size_kb: int = Field(default=102400, ge=0)
    max_files: int = Field(default=500, ge=0)
    max_raw_bytes: int = Field(default=MAX_RAW_BYTES, gt=0)
    max_prompt_bytes: int = Field(default=1024 * 1024, gt=0)
    max_request_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Model input budget including the system prompt and response schema; 0 disables."
        ),
    )


class ProfileImportPreviewRequest(CamelModel):
    raw_text: str
    ui_locale: str = Field(default="en", min_length=2, max_length=32)
    export_prompt_version: str = Field(default=EXPORT_PROMPT_VERSION, min_length=1, max_length=128)
    client_request_id: str = Field(min_length=1, max_length=256)
    declared_source: str | None = Field(default=None, max_length=128)

    @field_validator("raw_text")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("raw_text must not be empty")
        if "\x00" in value:
            raise ValueError("raw_text contains a NUL byte")
        return value


class DecisionCounts(CamelModel):
    applied: int = Field(default=0, ge=0)
    duplicate: int = Field(default=0, ge=0)
    unresolved: int = Field(default=0, ge=0)


class ProfileImportFileDiff(CamelModel):
    target: DecisionTarget
    display_name: str
    relative_path: str
    status: FileChangeStatus
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    diff: str


class RecentProfileImport(CamelModel):
    receipt_id: str
    batch_id: str
    status: Literal["applied", "undone"]
    provider: str
    model: str
    summary: list[str]
    applied_at: datetime
    undone_at: datetime | None = None
    file_count: int = Field(ge=0)
    targets: list[DecisionTarget] = Field(default_factory=list)
    index_status: Literal["ready", "pending"] = "pending"


class ProfileImportInfo(CamelModel):
    schema_version: Literal[1] = 1
    available: bool
    provider: str
    model: str
    is_loopback: bool
    max_raw_bytes: int
    prompt_version: str = PROMPT_VERSION
    recent_import: RecentProfileImport | None = None
    draft_job: ProfileImportJob | None = None


class ProfileImportPreview(CamelModel):
    schema_version: Literal[1] = 1
    preview_id: str
    batch_id: str
    candidate_hash: str
    provider: str
    model: str
    summary: list[str]
    decision_counts: DecisionCounts
    files: list[ProfileImportFileDiff]
    no_changes: bool
    expires_at: datetime


class ProfileImportJobRecord(CamelModel):
    schema_version: Literal[1] = 1
    job_id: str
    batch_id: str
    agent_id: str
    status: ProfileImportJobStatus = ProfileImportJobStatus.QUEUED
    stage: ProfileImportJobStage = ProfileImportJobStage.READING
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    raw_hash: str
    reuse_key: str
    client_request_id: str
    retry_request_ids: list[str] = Field(default_factory=list, max_length=100)
    export_prompt_version: str
    prompt_version: str = PROMPT_VERSION
    ui_locale: str
    declared_source: str | None = None
    model_identity: ModelIdentity
    baseline_user_hash: str
    baseline_memory_hash: str
    baseline_history_hash: str
    agent_workspace_root_hash: str = Field(default="", max_length=64)
    memory_workspace_root_hash: str = Field(default="", max_length=64)
    preview_id: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2048)

    @field_validator("job_id", "batch_id")
    @classmethod
    def validate_opaque_id(cls, value: str) -> str:
        if not _OPAQUE_ID.fullmatch(value):
            raise ValueError("invalid opaque import id")
        return value


class ProfileImportJob(CamelModel):
    schema_version: Literal[1] = 1
    job_id: str
    batch_id: str
    status: ProfileImportJobStatus
    stage: ProfileImportJobStage
    provider: str
    model: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    preview_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    can_retry: bool = False
    preview: ProfileImportPreview | None = None


class ProfileImportApplyResult(CamelModel):
    schema_version: Literal[1] = 1
    status: Literal["applied", "alreadyApplied", "noChanges"]
    receipt_id: str
    batch_id: str
    index_status: Literal["ready", "pending"] = "pending"
    applied_at: datetime


class UndoFileContext(CamelModel):
    target: DecisionTarget
    relative_path: str
    existed_before: bool
    before_content: str
    imported_content: str
    current_exists: bool
    current_content: str


class UndoReviewContext(CamelModel):
    batch_id: str
    reason: str
    current_files: list[UndoFileContext]
    original_files: list[UndoFileContext]


class ProfileImportUndoResult(CamelModel):
    schema_version: Literal[1] = 1
    status: Literal["undone", "alreadyUndone", "reviewRequired"]
    receipt_id: str
    index_status: Literal["pending"] | None = None
    preview: ProfileImportPreview | None = None
    review_context: UndoReviewContext | None = Field(default=None, exclude=True)


class InternalFilePlan(CamelModel):
    target: DecisionTarget
    display_name: str
    relative_path: str
    root_kind: Literal["agent_workspace", "memory_workspace"]
    before_exists: bool
    before_content: str
    before_hash: str
    after_exists: bool
    after_content: str
    after_hash: str
    status: FileChangeStatus
    additions: int
    deletions: int
    diff: str


class InternalPreviewRecord(CamelModel):
    schema_version: Literal[1] = 1
    preview_id: str
    batch_id: str
    agent_id: str
    operation: Literal["import", "undo_review"] = "import"
    source_receipt_id: str | None = None
    status: ImportStatus = ImportStatus.PREVIEW
    created_at: datetime
    expires_at: datetime
    raw_hash: str
    reuse_key: str
    client_request_id: str
    export_prompt_version: str
    prompt_version: str = PROMPT_VERSION
    ui_locale: str
    declared_source: str | None = None
    model_identity: ModelIdentity
    fusion_output: FusionOutput
    candidate_hash: str
    baseline_user_hash: str
    baseline_memory_hash: str
    baseline_history_hash: str
    agent_workspace_root_hash: str = Field(default="", max_length=64)
    memory_workspace_root_hash: str = Field(default="", max_length=64)
    files: list[InternalFilePlan]
    receipt_id: str | None = None
    applied_at: datetime | None = None
    idempotency_key_hash: str | None = None

    @field_validator("preview_id", "batch_id")
    @classmethod
    def validate_opaque_id(cls, value: str) -> str:
        if not _OPAQUE_ID.fullmatch(value):
            raise ValueError("invalid opaque import id")
        return value

    @field_validator("agent_workspace_root_hash", "memory_workspace_root_hash")
    @classmethod
    def validate_optional_root_hash(cls, value: str) -> str:
        invalid_character = any(
            character not in "0123456789abcdef" for character in value
        )
        if value and (len(value) != 64 or invalid_character):
            raise ValueError("invalid profile import root hash")
        return value


class ProfileImportReceipt(CamelModel):
    schema_version: Literal[1] = 1
    receipt_id: str
    preview_id: str
    batch_id: str
    agent_id: str
    status: Literal["applied", "undone"] = "applied"
    source_type: Literal["profile_text"] = "profile_text"
    raw_hash: str
    declared_source: str | None = None
    export_prompt_version: str
    prompt_version: str = PROMPT_VERSION
    ui_locale: str
    model_identity: ModelIdentity
    summary: list[str]
    decisions: list[FusionDecision]
    files: list[InternalFilePlan]
    candidate_hash: str
    baseline_user_hash: str = ""
    baseline_memory_hash: str = ""
    baseline_history_hash: str = ""
    applied_user_hash: str = ""
    applied_memory_hash: str = ""
    applied_history_hash: str = ""
    agent_workspace_root_hash: str = Field(default="", max_length=64)
    memory_workspace_root_hash: str = Field(default="", max_length=64)
    applied_at: datetime
    undone_at: datetime | None = None
    undo_client_request_ids: list[str] = Field(default_factory=list)
    index_status: Literal["ready", "pending"] = "pending"


class PublishedFileIdentity(CamelModel):
    """Identity of a candidate temp file, preserved when it is renamed into place."""

    target: DecisionTarget
    device: int
    inode: int
    mode: int
    size: int
    modified_at_ns: int
    reparse_tag: int | None = None
    link_target: str | None = None


class TransactionJournal(CamelModel):
    schema_version: Literal[1] = 1
    operation: Literal["profile-import-apply", "profile-import-undo"]
    transaction_id: str
    phase: Literal[
        "applying",
        "published",
        "committed",
        "rolling_back",
        "rolled_back",
        "rollback_failed",
        "recovery_conflict",
    ]
    preview_id: str
    receipt_id: str
    batch_id: str
    completed_targets: list[DecisionTarget] = Field(default_factory=list)
    active_target: DecisionTarget | None = None
    publication_identities: list[PublishedFileIdentity] = Field(default_factory=list)
    plans: list[InternalFilePlan]
    receipt: ProfileImportReceipt
    updated_at: datetime
