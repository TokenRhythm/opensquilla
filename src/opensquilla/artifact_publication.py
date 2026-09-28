"""Host-injected validation for explicitly protected artifact publication."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

AUTHORIZATION_VERSION = "artifact-publication-authorization/1"


class ArtifactPublicationError(ValueError):
    """A safe publication failure; never contains validator stderr or host paths."""


@dataclass(frozen=True)
class ArtifactPublicationRequest:
    session_id: str
    session_key: str
    execution_id: str
    path: str
    name: str
    mime: str
    bundle: str


@dataclass(frozen=True)
class ArtifactPublicationCandidate:
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.payload) is not bytes:
            raise TypeError("publication candidates require immutable bytes")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class ArtifactPublicationAuthorization:
    session_id: str
    session_key: str
    execution_id: str
    sha256: str
    validator_id: str
    receipt_id: str
    schema_version: str = AUTHORIZATION_VERSION

    def public_metadata(self) -> dict[str, str]:
        return {
            "schemaVersion": self.schema_version,
            "sha256": self.sha256,
            "validatorId": self.validator_id,
            "receiptId": self.receipt_id,
        }


class ArtifactPublicationPolicy(Protocol):
    async def authorize(
        self,
        request: ArtifactPublicationRequest,
        candidate: ArtifactPublicationCandidate,
    ) -> ArtifactPublicationAuthorization:
        """Validate trusted inputs and the candidate, or raise to deny publication.

        The host owns this object and its validator/receipt identity bindings.
        Workspace files or model-supplied manifests alone cannot grant approval.
        """
        ...


async def authorize_publication(
    policy: ArtifactPublicationPolicy,
    request: ArtifactPublicationRequest,
    candidate: ArtifactPublicationCandidate,
) -> ArtifactPublicationAuthorization:
    if not all((request.session_id, request.session_key, request.execution_id)):
        raise ArtifactPublicationError("Protected publication requires an active execution scope.")
    try:
        authorization = await policy.authorize(request, candidate)
    except Exception as exc:  # noqa: BLE001 - the host policy is an external validation boundary
        raise ArtifactPublicationError(
            "Artifact publication was denied by host validation. "
            "Correct the draft and validate again."
        ) from exc
    if (
        not isinstance(authorization, ArtifactPublicationAuthorization)
        or authorization.schema_version != AUTHORIZATION_VERSION
        or authorization.session_id != request.session_id
        or authorization.session_key != request.session_key
        or authorization.execution_id != request.execution_id
        or authorization.sha256 != candidate.sha256
        or not _safe_audit_id(authorization.validator_id)
        or not _safe_audit_id(authorization.receipt_id)
    ):
        raise ArtifactPublicationError(
            "Host validation did not authorize these exact artifact bytes for this execution."
        )
    return authorization


def _safe_audit_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and all(
            character.isascii() and (character.isalnum() or character in "_.:-")
            for character in value
        )
    )


def read_publication_candidate(
    workspace: Path,
    target: Path,
    max_bytes: int | None,
) -> ArtifactPublicationCandidate:
    """Snapshot a regular workspace file without following swapped directory links."""

    try:
        parts = target.relative_to(workspace).parts
        if any(part in {".", ".."} for part in parts):
            raise ArtifactPublicationError(
                "Artifact snapshot paths must stay inside the workspace."
            )
        if not parts or os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise ArtifactPublicationError(
                "Protected artifact snapshots are unavailable on this host."
            )
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(workspace, directory_flags)
        try:
            for part in parts[:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
        finally:
            os.close(directory_fd)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactPublicationError("Protected publication requires a regular file.")
            if max_bytes is not None and info.st_size > max_bytes:
                raise ArtifactPublicationError(
                    "Artifact exceeds the configured publication size limit."
                )
            payload = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        if not payload:
            raise ArtifactPublicationError("Artifact payload is empty.")
        if max_bytes is not None and len(payload) > max_bytes:
            raise ArtifactPublicationError(
                "Artifact exceeds the configured publication size limit."
            )
        return ArtifactPublicationCandidate(payload)
    except ArtifactPublicationError:
        raise
    except (OSError, ValueError) as exc:
        raise ArtifactPublicationError(
            "The protected artifact could not be safely snapshotted."
        ) from exc
