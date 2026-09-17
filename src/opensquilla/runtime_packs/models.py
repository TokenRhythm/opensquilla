"""Public state models for Runtime Pack management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RuntimeAvailability(StrEnum):
    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    READY = "ready"
    CORRUPT = "corrupt"


class RuntimeOperationState(StrEnum):
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

    @property
    def terminal(self) -> bool:
        return self in {
            RuntimeOperationState.COMPLETED,
            RuntimeOperationState.CANCELLED,
            RuntimeOperationState.FAILED,
            RuntimeOperationState.INTERRUPTED,
        }


class RuntimeOperationKind(StrEnum):
    INSTALL = "install"
    REMOVE = "remove"


class RuntimeSource(StrEnum):
    OSS = "oss"
    GITHUB = "github"


@dataclass(frozen=True)
class RuntimeError:
    code: str
    message: str
    retryable: bool
    source: RuntimeSource | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "source": self.source.value if self.source is not None else None,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> RuntimeError | None:
        if not isinstance(value, dict):
            return None
        try:
            raw_source = value.get("source")
            source = RuntimeSource(str(raw_source)) if raw_source else None
            return cls(
                code=str(value["code"]),
                message=str(value["message"]),
                retryable=bool(value["retryable"]),
                source=source,
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RuntimeOperation:
    operation_id: str
    component_id: str
    kind: RuntimeOperationKind
    state: RuntimeOperationState
    progress_bytes: int
    total_bytes: int
    source: RuntimeSource | None
    started_at_ms: int
    updated_at_ms: int
    error: RuntimeError | None = None

    @property
    def progress_percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, max(0, int(self.progress_bytes * 100 / self.total_bytes)))

    def to_public_dict(self) -> dict[str, object]:
        return {
            "operationId": self.operation_id,
            "componentId": self.component_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "downloadedBytes": self.progress_bytes,
            "totalBytes": self.total_bytes,
            "progressPercent": self.progress_percent,
            "source": self.source.value if self.source is not None else None,
            "startedAtMs": self.started_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "error": self.error.to_public_dict() if self.error is not None else None,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> RuntimeOperation | None:
        if not isinstance(value, dict):
            return None
        try:
            raw_source = value.get("source")
            return cls(
                operation_id=str(value["operationId"]),
                component_id=str(value["componentId"]),
                kind=RuntimeOperationKind(str(value["kind"])),
                state=RuntimeOperationState(str(value["state"])),
                progress_bytes=max(
                    0,
                    int(
                        value.get("downloadedBytes", value.get("progressBytes", 0))
                        or 0
                    ),
                ),
                total_bytes=max(0, int(value.get("totalBytes", 0))),
                source=RuntimeSource(str(raw_source)) if raw_source else None,
                started_at_ms=int(value["startedAtMs"]),
                updated_at_ms=int(value["updatedAtMs"]),
                error=RuntimeError.from_mapping(value.get("error")),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class RuntimeComponentStatus:
    component_id: str
    availability: RuntimeAvailability
    catalog_version: str | None
    active_version: str | None
    installed_bytes: int | None
    removable: bool
    resume_available: bool
    resume_bytes: int
    operation: RuntimeOperation | None
    last_error: RuntimeError | None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "componentId": self.component_id,
            "availability": self.availability.value,
            "catalogVersion": self.catalog_version,
            "activeVersion": self.active_version,
            "installedBytes": self.installed_bytes,
            "removable": self.removable,
            "resumeAvailable": self.resume_available,
            "resumeBytes": self.resume_bytes,
            "operation": (
                self.operation.to_public_dict() if self.operation is not None else None
            ),
            "lastError": (
                self.last_error.to_public_dict() if self.last_error is not None else None
            ),
        }


@dataclass(frozen=True)
class RuntimePackStatus:
    schema_version: int
    management_supported: bool
    target: str | None
    catalog_version: str | None
    source_order: tuple[RuntimeSource, ...]
    components: tuple[RuntimeComponentStatus, ...]
    next_poll_after_ms: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "managementSupported": self.management_supported,
            "target": self.target,
            "catalogVersion": self.catalog_version,
            "sourceOrder": [source.value for source in self.source_order],
            "components": [component.to_public_dict() for component in self.components],
            "nextPollAfterMs": self.next_poll_after_ms,
        }


__all__ = [
    "RuntimeAvailability",
    "RuntimeComponentStatus",
    "RuntimeError",
    "RuntimeOperation",
    "RuntimeOperationKind",
    "RuntimeOperationState",
    "RuntimePackStatus",
    "RuntimeSource",
]
