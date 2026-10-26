"""Task-local evidence that bounded stream cleanup completed."""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class StreamCloseEvidence:
    failed: bool = False


stream_close_evidence: ContextVar[StreamCloseEvidence | None] = ContextVar(
    "stream_close_evidence", default=None,
)


def record_stream_close_failure() -> None:
    evidence = stream_close_evidence.get()
    if evidence is not None:
        evidence.failed = True
