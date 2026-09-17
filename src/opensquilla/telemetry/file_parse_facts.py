"""Content-free file parsing facts shared by the attachment worker boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from opensquilla.telemetry.contracts.common import ResultOutcome
from opensquilla.telemetry.contracts.reliability import (
    FileParseErrorCode,
    FileSizeBucket,
    FileType,
)

_FILE_TYPES_BY_MIME = {
    "application/pdf": FileType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (FileType.DOCX),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (FileType.XLSX),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (FileType.PPTX),
    "message/rfc822": FileType.EMAIL,
    "application/mbox": FileType.EMAIL,
    "application/vnd.ms-outlook": FileType.EMAIL,
}


@dataclass(frozen=True, slots=True)
class FileParseReliabilityFacts:
    """Closed terminal facts for one locally parsed file attachment."""

    file_type: FileType
    size_bucket: FileSizeBucket
    outcome: ResultOutcome
    error_code: FileParseErrorCode | None
    duration_ms: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a non-negative integer")
        if (self.outcome is ResultOutcome.SUCCESS) != (self.error_code is None):
            raise ValueError("success requires no error_code; failures require one")


FileParseReliabilitySink = Callable[[FileParseReliabilityFacts], object]


def file_type_for_media_type(media_type: object) -> FileType | None:
    """Return a tracked parser family without considering file names or paths."""

    if not isinstance(media_type, str):
        return None
    if ";" in media_type or media_type != media_type.strip().lower():
        return None
    exact = _FILE_TYPES_BY_MIME.get(media_type)
    if exact is not None:
        return exact
    if media_type.startswith("text/") or media_type in {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }:
        return FileType.TEXT
    return None


def file_size_bucket(size_bytes: object) -> FileSizeBucket:
    """Bucket only the in-memory byte length used by the parser."""

    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise ValueError("size_bytes must be a non-negative integer")
    if size_bytes < 100 * 1024:
        return FileSizeBucket.LT_100_KIB
    if size_bytes < 1024 * 1024:
        return FileSizeBucket.KIB_100_TO_1_MIB
    if size_bytes < 10 * 1024 * 1024:
        return FileSizeBucket.MIB_1_TO_10
    if size_bytes < 50 * 1024 * 1024:
        return FileSizeBucket.MIB_10_TO_50
    return FileSizeBucket.GTE_50_MIB


__all__ = [
    "FileParseReliabilityFacts",
    "FileParseReliabilitySink",
    "file_size_bucket",
    "file_type_for_media_type",
]
