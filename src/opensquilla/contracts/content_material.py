"""Transport-neutral exported bytes and their request-owned cleanup."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


class ContentExportError(RuntimeError):
    """A bounded HTML archive cannot be exported safely."""

    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ContentMaterial:
    path: Path | None
    media_type: str
    filename: str | None = None
    data: bytes | None = None
    cleanup: Callable[[], Awaitable[None]] | None = None
