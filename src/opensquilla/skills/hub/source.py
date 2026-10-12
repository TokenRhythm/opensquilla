"""SkillSource ABC and Community source data models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)


class SkillSourceFetchError(RuntimeError):
    """A source operation failure carrying stable user-facing diagnostics.

    The historical name is retained for compatibility.  Resolution and fetch
    adapters both use this exception so a caller can preserve the precise
    pipeline phase instead of collapsing transport and protocol failures into
    a false ``not found`` result.
    """

    def __init__(self, *diagnostics: SkillDiagnostic) -> None:
        if not diagnostics:
            raise ValueError("at least one fetch diagnostic is required")
        self.diagnostics = tuple(diagnostics)
        super().__init__(diagnostics[-1].message)

    @classmethod
    def diagnostic(
        cls,
        code: str,
        message: str,
        *,
        phase: DiagnosticPhase,
        hint: str = "",
        details: dict[str, Any] | None = None,
        path: str = "",
    ) -> SkillSourceFetchError:
        """Build one blocking fetch failure with the public diagnostic shape."""

        return cls(
            SkillDiagnostic(
                code=code,
                severity=DiagnosticSeverity.ERROR,
                phase=phase,
                message=message,
                blocking=True,
                hint=hint,
                details=details or {},
                path=path,
            )
        )


def raise_for_source_http_status(
    response: Any,
    *,
    phase: DiagnosticPhase,
    source_name: str,
) -> None:
    """Raise one stable diagnostic for an unsuccessful source HTTP response."""

    try:
        status_code = int(getattr(response, "status_code", 0))
    except (TypeError, ValueError):
        status_code = 0
    if status_code < 400:
        return

    prefix = "SOURCE" if phase is DiagnosticPhase.SOURCE else "FETCH"
    headers = getattr(response, "headers", {})

    def header(name: str) -> str:
        getter = getattr(headers, "get", None)
        if callable(getter):
            return str(getter(name) or getter(name.lower()) or "")
        return ""

    # HTTPX raises ResponseNotRead when ``text`` is accessed inside an unread
    # ``client.stream()`` context. Status mapping must remain usable before a
    # potentially large or untrusted error body is consumed. Preserve the
    # historical body hint only for responses whose body is already buffered
    # (and for simple compatibility test doubles without stream state).
    response_text = ""
    if bool(getattr(response, "is_stream_consumed", True)):
        try:
            response_text = str(getattr(response, "text", "") or "").lower()
        except RuntimeError:
            response_text = ""
    rate_limited = status_code == 429 or (
        status_code == 403
        and (
            header("x-ratelimit-remaining") == "0"
            or "rate limit" in response_text
            or "rate-limit" in response_text
        )
    )
    details: dict[str, Any] = {"statusCode": status_code}
    retry_after = header("retry-after")
    if retry_after:
        details["retryAfter"] = retry_after

    if status_code == 404:
        code = f"{prefix}_NOT_FOUND"
        message = f"{source_name} could not find the requested Skill artifact."
        hint = "Verify the exact owner, repository, version, and Skill subpath."
    elif rate_limited:
        code = f"{prefix}_RATE_LIMITED"
        message = f"{source_name} rate-limited the Skill request."
        hint = "Retry after the provider rate limit resets or configure credentials."
    elif status_code in {401, 403}:
        code = f"{prefix}_AUTH_FAILED"
        message = f"{source_name} rejected the Skill request credentials or permissions."
        hint = "Check the configured source token and its repository or registry access."
    elif 500 <= status_code <= 599:
        code = f"{prefix}_SERVER_FAILED"
        message = f"{source_name} failed while processing the Skill request."
        hint = "Retry later; the remote source returned a server error."
    else:
        code = f"{prefix}_HTTP_FAILED"
        message = f"{source_name} rejected the Skill request with HTTP {status_code}."
        hint = "Verify the install reference and source policy, then retry."

    raise SkillSourceFetchError.diagnostic(
        code,
        message,
        phase=phase,
        hint=hint,
        details=details,
    )


def source_transport_error(
    exc: Exception,
    *,
    phase: DiagnosticPhase,
    source_name: str,
) -> SkillSourceFetchError:
    """Map DNS, timeout, and client transport failures without claiming absence."""

    prefix = "SOURCE" if phase is DiagnosticPhase.SOURCE else "FETCH"
    return SkillSourceFetchError.diagnostic(
        f"{prefix}_TRANSPORT_FAILED",
        f"Could not reach {source_name} for the Skill request.",
        phase=phase,
        hint="Check DNS, proxy, TLS, and network connectivity, then retry.",
        details={"errorType": type(exc).__name__},
    )


def source_invalid_response_error(
    *,
    phase: DiagnosticPhase,
    source_name: str,
) -> SkillSourceFetchError:
    """Return a stable diagnostic for malformed provider protocol data."""

    prefix = "SOURCE" if phase is DiagnosticPhase.SOURCE else "FETCH"
    return SkillSourceFetchError.diagnostic(
        f"{prefix}_INVALID_RESPONSE",
        f"{source_name} returned an invalid Skill response.",
        phase=phase,
        hint="Retry later or report the incompatible source response to its operator.",
    )


@dataclass
class SkillMeta:
    """Metadata for a skill in a Community source listing."""

    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    source_id: str = ""
    trust_level: str = "community"  # "builtin" | "trusted" | "community"
    identifier: str = ""  # source-specific ID (e.g. slug@version)
    homepage: str = ""
    license: str = ""
    tags: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    canonical_identifier: str = ""

    @property
    def canonical_identity(self) -> str:
        """Stable source-qualified identity used for cross-source result merging."""

        identifier = self.canonical_identifier or self.identifier or self.name
        return f"{self.source_id}:{identifier}"


@dataclass(frozen=True)
class SourceResolution:
    """Immutable source selection produced before artifact bytes are fetched.

    ``canonical_identifier`` names the exact selected artifact, while the
    source-specific fetch fields avoid reparsing a user-facing identifier.
    Legacy sources can use the defaults and continue fetching by the original
    identifier through :meth:`SkillSource.fetch_resolved`.
    """

    source_id: str
    requested_identifier: str
    canonical_identifier: str
    immutable: bool = False
    revision: str = ""
    artifact_kind: str = "legacy"
    artifact_url: str = ""
    repository: str = ""
    skill_path: str = ""
    expected_digest: str = ""
    # Opaque registry provenance. It is not treated as an artifact digest
    # unless the registry publishes a verifiable algorithm contract.
    resolver_content_hash: str = ""
    trust_state: str = ""
    publisher: str = ""
    version: str = ""
    upstream_url: str = ""
    # Stable package identity without a mutable ref, version, or revision.
    # This distinguishes an update from a same-name package replacement.
    package_identifier: str = ""
    meta: SkillMeta | None = None
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    # Fetch-only policy used by a registry adapter that explicitly supports
    # bounded legacy manifest filenames. Direct GitHub resolutions leave this
    # false and therefore continue to require canonical ``SKILL.md``.
    allow_legacy_manifest_names: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the stable management/RPC wire representation."""

        return {
            "source": self.source_id,
            "requestedIdentifier": self.requested_identifier,
            "canonicalIdentifier": self.canonical_identifier,
            "packageIdentifier": self.package_identifier,
            "publisher": self.publisher,
            "version": self.version,
            "immutableRevision": self.revision,
            "upstreamUrl": self.upstream_url or self.artifact_url,
            "artifactKind": self.artifact_kind,
            "artifactDigest": self.expected_digest,
            "resolverContentHash": self.resolver_content_hash,
            "trustState": self.trust_state,
            "immutable": self.immutable,
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }

    def as_dict(self) -> dict[str, Any]:
        """Compatibility alias for dataclass-oriented callers."""

        return self.to_dict()


@dataclass
class SkillBundle:
    """Downloaded skill ready for installation."""

    name: str
    files: dict[str, str | bytes] = field(default_factory=dict)  # relative_path → content
    meta: SkillMeta | None = None
    resolution: SourceResolution | None = None
    file_modes: dict[str, int] = field(default_factory=dict)

    @property
    def skill_md(self) -> str | None:
        content = self.files.get("SKILL.md")
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            try:
                return content.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


class SkillSource(ABC):
    """Abstract base class for skill Community sources."""

    @abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        """Search for skills matching query."""

    @abstractmethod
    async def fetch(self, identifier: str) -> SkillBundle | None:
        """Download a skill by its source-specific identifier."""

    async def resolve(self, identifier: str) -> SourceResolution | None:
        """Resolve an identifier before fetching, with a legacy-safe default."""

        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=identifier,
            canonical_identifier=identifier,
        )

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle | None:
        """Fetch a prior resolution; legacy sources still receive the old identifier."""

        return await self.fetch(resolution.requested_identifier)

    @abstractmethod
    async def inspect(self, identifier: str) -> SkillMeta | None:
        """Get metadata for a skill without downloading."""

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Unique identifier for this source (e.g. 'clawhub', 'github')."""

    @property
    @abstractmethod
    def trust_level(self) -> str:
        """Trust level: 'builtin', 'trusted', or 'community'."""
