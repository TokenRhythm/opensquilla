"""Stable errors for the profile import domain."""

from __future__ import annotations


class ProfileImportError(RuntimeError):
    """Base error carrying the stable RPC-facing error code."""

    code = "MEMORY_IMPORT_WRITE_FAILED"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or type(self).code


class ProfileImportUnavailableError(ProfileImportError):
    code = "MEMORY_IMPORT_UNAVAILABLE"


class ProfileImportInputTooLargeError(ProfileImportError):
    code = "MEMORY_IMPORT_INPUT_TOO_LARGE"


class ProfileImportModelError(ProfileImportError):
    code = "MEMORY_IMPORT_MODEL_FAILED"


class ProfileImportInvalidOutputError(ProfileImportError):
    code = "MEMORY_IMPORT_INVALID_OUTPUT"


class ProfileImportPreviewExpiredError(ProfileImportError):
    code = "MEMORY_IMPORT_PREVIEW_EXPIRED"


class ProfileImportStalePreviewError(ProfileImportError):
    code = "MEMORY_IMPORT_STALE_PREVIEW"


class ProfileImportWriteError(ProfileImportError):
    code = "MEMORY_IMPORT_WRITE_FAILED"


class ProfileImportNotFoundError(ProfileImportError):
    """A deliberately generic not-found error that does not reveal other agents."""

    code = "MEMORY_IMPORT_PREVIEW_EXPIRED"


class ProfileImportBusyError(ProfileImportError):
    code = "MEMORY_IMPORT_BUSY"


class ProfileImportJobNotFoundError(ProfileImportError):
    code = "MEMORY_IMPORT_JOB_NOT_FOUND"
