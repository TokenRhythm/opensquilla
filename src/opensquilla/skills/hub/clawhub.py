"""ClawHub Community source adapter - connects to clawhub.ai API."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.skills.hub.archive import (
    DEFAULT_ARCHIVE_LIMITS,
    ArchiveNormalizationError,
    normalize_relative_path,
    normalize_skill_archive_result,
)
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.github import GitHubSource
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    SkillSourceFetchError,
    SourceResolution,
    raise_for_source_http_status,
    source_invalid_response_error,
    source_transport_error,
)

log = structlog.get_logger(__name__)


def _archive_diagnostic_error(exc: ArchiveNormalizationError) -> SkillSourceFetchError:
    message = str(exc) or "The Skill archive is invalid."
    lowered = message.lower()
    if "collid" in lowered:
        code = "ARTIFACT_PATH_COLLISION"
        phase = DiagnosticPhase.SECURITY
    elif any(
        marker in lowered
        for marker in ("path", "symlink", "link metadata", "special file", "reserved")
    ):
        code = "ARTIFACT_PATH_UNSAFE"
        phase = DiagnosticPhase.SECURITY
    elif any(
        marker in lowered
        for marker in ("limit", "too many", "compression ratio", "size")
    ):
        code = "ARCHIVE_LIMIT_EXCEEDED"
        phase = DiagnosticPhase.ARCHIVE
    elif any(
        marker in lowered
        for marker in ("exactly one", "multiple root", "one wrapper", "skill.md root")
    ):
        code = "SOURCE_TREE_AMBIGUOUS"
        phase = DiagnosticPhase.ARCHIVE
    else:
        code = "ARCHIVE_INVALID"
        phase = DiagnosticPhase.ARCHIVE
    return SkillSourceFetchError.diagnostic(code, message, phase=phase)

_DEFAULT_BASE_URL = "https://clawhub.ai"
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?$")
_OWNER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,38}[a-z0-9])?$")
_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_GITHUB_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_ARTIFACT_REDIRECTS = 5
_MAX_REGISTRY_DESCRIPTION_LENGTH = 1_024
_SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")


def _validate_artifact_url(url: str) -> list[str]:
    """Validate lazily so importing Skill tools cannot recurse through built-ins."""

    from opensquilla.tools.ssrf import validate_http_url_for_fetch

    return validate_http_url_for_fetch(url)


def _artifact_proxy_url(url: str) -> str | None:
    from opensquilla.tools.ssrf import environment_proxy_url

    return environment_proxy_url(url)


def _artifact_transport(url: str, vetted_ips: list[str], **kwargs: object) -> object | None:
    from opensquilla.tools.ssrf import pinned_transport

    return pinned_transport(url, vetted_ips, **kwargs)


@dataclass(frozen=True)
class _ClawHubRef:
    slug: str
    owner_handle: str = ""
    requested_reference: str = ""

    @property
    def package_ref(self) -> str:
        if self.requested_reference:
            return self.requested_reference
        if self.owner_handle:
            return f"@{self.owner_handle}/{self.slug}"
        return self.slug


def _parse_identifier(identifier: str) -> _ClawHubRef | None:
    value = identifier.strip()
    if value.startswith("skills-sh:"):
        parts = value[len("skills-sh:") :].split("/")
        if (
            len(parts) != 3
            or not _GITHUB_OWNER_RE.fullmatch(parts[0])
            or not _GITHUB_REPO_NAME_RE.fullmatch(parts[1])
            or parts[1] in {".", ".."}
            or not _SLUG_RE.fullmatch(parts[2])
        ):
            return None
        return _ClawHubRef(slug=parts[2], requested_reference=value)
    if value.startswith("skills-sh/"):
        return None
    if value.startswith("@"):
        parts = value[1:].split("/")
        owner = parts[0].lower() if parts else ""
        if (
            len(parts) != 2
            or not _OWNER_RE.fullmatch(owner)
            or not _SLUG_RE.fullmatch(parts[1])
        ):
            return None
        return _ClawHubRef(slug=parts[1], owner_handle=owner)
    if not _SLUG_RE.fullmatch(value):
        return None
    return _ClawHubRef(slug=value)


def _blocking_resolution(
    ref: _ClawHubRef,
    requested_identifier: str,
    *,
    code: str,
    message: str,
    phase: DiagnosticPhase = DiagnosticPhase.SOURCE,
    details: dict[str, object] | None = None,
) -> SourceResolution:
    diagnostic = SkillDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        phase=phase,
        message=message,
        blocking=True,
        details=details or {},
    )
    return SourceResolution(
        source_id="clawhub",
        requested_identifier=requested_identifier,
        canonical_identifier=ref.package_ref,
        artifact_kind="unsupported",
        publisher=ref.owner_handle,
        diagnostics=(diagnostic,),
    )


def _safe_artifact_url(base_url: str, value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    resolved = urljoin(base_url.rstrip("/") + "/", raw)
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    return resolved


def _response_owner(data: dict[str, Any]) -> str:
    raw_owner = data.get("owner")
    owner_mapping = raw_owner if isinstance(raw_owner, dict) else {}
    value = str(
        data.get("ownerHandle")
        or owner_mapping.get("handle")
        or data.get("publisher")
        or ""
    ).strip().lower()
    return value if _OWNER_RE.fullmatch(value) else ""


def _resolved_registry_identity(
    data: dict[str, Any],
    ref: _ClawHubRef,
    resolved_slug: str,
) -> tuple[str, str] | None:
    server_ref_raw = str(data.get("installRef") or "").strip()
    server_ref = _parse_identifier(server_ref_raw) if server_ref_raw else None
    if server_ref is not None and server_ref.slug != resolved_slug:
        return None

    server_owner = _response_owner(data)
    if ref.owner_handle and server_owner and ref.owner_handle != server_owner:
        return None
    if (
        ref.owner_handle
        and server_ref is not None
        and server_ref.owner_handle
        and ref.owner_handle != server_ref.owner_handle
    ):
        return None

    if ref.requested_reference:
        publisher = server_owner or ref.requested_reference.split(":", 1)[-1].split("/", 1)[0]
        return ref.package_ref, publisher
    owner = ref.owner_handle or (server_ref.owner_handle if server_ref else "") or server_owner
    if not owner:
        return None
    return f"@{owner}/{resolved_slug}", owner


def _registry_description(data: dict[str, Any]) -> str:
    """Return bounded descriptive metadata carried by an install resolution.

    ClawHub deployments have emitted this metadata both at the response root
    and inside a structured ``skill`` object.  Only strings are accepted: an
    optional, type-polluted registry field must never become a synthesized
    manifest value through ``str(...)`` coercion.
    """

    raw_skill = data.get("skill")
    skill = raw_skill if isinstance(raw_skill, dict) else {}
    for value in (
        data.get("summary"),
        data.get("description"),
        skill.get("summary"),
        skill.get("description"),
    ):
        if not isinstance(value, str):
            continue
        description = value.strip()
        if description:
            return description[:_MAX_REGISTRY_DESCRIPTION_LENGTH]
    return ""


class ClawHubSource(SkillSource):
    """Skill source backed by the ClawHub community registry."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        token: str | None = None,
        github_source: GitHubSource | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._github_source = github_source or GitHubSource()
        # Search results already carry the registry summary used by the WebUI.
        # Retain it by exact install reference so a subsequent resolution can
        # normalize a legacy manifest without another network request.
        self._registry_descriptions: dict[str, str] = {}

    @property
    def source_id(self) -> str:
        return "clawhub"

    @property
    def trust_level(self) -> str:
        return "community"

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _download_headers(self, url: str) -> dict[str, str]:
        headers = {"Accept": "application/zip, application/octet-stream"}
        base = urlparse(self._base_url)
        target = urlparse(url)
        if self._token and (base.scheme.lower(), base.netloc.lower()) == (
            target.scheme.lower(),
            target.netloc.lower(),
        ):
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        import httpx

        url = f"{self._base_url}/api/v1/search"
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=_trust_env()) as client:
                response = await client.get(
                    url,
                    params={"q": query, "limit": limit},
                    headers=self._headers(),
                )
        except Exception as exc:
            log.warning("clawhub.search_failed", error=str(exc))
            raise source_transport_error(
                exc,
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            ) from exc

        raise_for_source_http_status(
            response,
            phase=DiagnosticPhase.SOURCE,
            source_name="ClawHub",
        )
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            log.warning("clawhub.search_invalid_json")
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            ) from exc

        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "error" not in data:
            if "results" in data:
                items = data["results"]
            elif "skills" in data:
                items = data["skills"]
            else:
                items = None
        else:
            items = None
        if not isinstance(items, list):
            log.warning("clawhub.search_error", data=str(data)[:100])
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            )

        results: list[SkillMeta] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("name") or "")
            owner = str(item.get("ownerHandle") or item.get("author") or "")
            normalized_owner = owner.lower()
            raw_install_ref = str(item.get("installRef") or "").strip()
            install_ref = (
                raw_install_ref
                if raw_install_ref.startswith(("@", "skills-sh:"))
                and _parse_identifier(raw_install_ref) is not None
                else ""
            )
            if not install_ref and normalized_owner and _OWNER_RE.fullmatch(normalized_owner):
                install_ref = f"@{normalized_owner}/{slug}"
            if not install_ref:
                # Search is the exact-identity path used by new clients. Bare
                # slugs remain accepted only by resolve() for legacy callers.
                continue
            identifier = install_ref
            if _parse_identifier(identifier) is None:
                continue
            meta = SkillMeta(
                name=str(item.get("displayName") or item.get("name") or slug),
                description=_registry_description(item),
                version=str(item.get("version") or ""),
                author=owner,
                source_id=self.source_id,
                trust_level=self.trust_level,
                identifier=identifier,
                homepage=str(item.get("homepage") or ""),
                license=str(item.get("license") or ""),
                tags=list(item.get("tags") or []),
                canonical_identifier=identifier,
            )
            results.append(meta)
            if meta.description:
                self._registry_descriptions[identifier] = meta.description
            else:
                self._registry_descriptions.pop(identifier, None)
        if items and not results:
            log.warning("clawhub.search_all_rows_invalid", row_count=len(items))
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            )
        return results[:limit]

    async def resolve(self, identifier: str) -> SourceResolution | None:
        import httpx

        ref = _parse_identifier(identifier)
        if ref is None:
            return None
        params: dict[str, str] = {}
        if ref.owner_handle:
            params["ownerHandle"] = ref.owner_handle
        if ref.requested_reference:
            params["reference"] = ref.requested_reference
        url = f"{self._base_url}/api/v1/skills/{quote(ref.slug, safe='')}/install"
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                response = await client.get(url, params=params, headers=self._headers())
        except Exception as exc:
            log.warning("clawhub.resolve_failed", identifier=identifier, error=str(exc))
            raise source_transport_error(
                exc,
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            ) from exc

        # ClawHub returns structured policy blocks for selected conflict and
        # lifecycle statuses. Authentication failures are never reclassified
        # as package policy or absence.
        if response.status_code not in {409, 410, 423}:
            raise_for_source_http_status(
                response,
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            )
        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            log.warning("clawhub.resolve_invalid_json", identifier=identifier)
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            ) from exc
        if not isinstance(data, dict):
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            )
        if not data.get("ok"):
            return _blocking_resolution(
                ref,
                identifier,
                code="SOURCE_RESOLUTION_BLOCKED",
                message=str(data.get("message") or "ClawHub blocked this installation."),
                details={
                    "status": data.get("status"),
                    "reason": str(data.get("reason") or "resolution_blocked"),
                },
            )

        resolved_slug = str(data.get("slug") or ref.slug)
        if not _SLUG_RE.fullmatch(resolved_slug):
            return _blocking_resolution(
                ref,
                identifier,
                code="SOURCE_INVALID_SLUG",
                message="ClawHub returned an invalid canonical slug.",
            )
        homepage = f"{self._base_url}/skills/{quote(resolved_slug, safe='')}"
        install_kind = str(data.get("installKind") or "")
        raw_trust = data.get("trust")
        if raw_trust is not None and not isinstance(raw_trust, dict):
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="ClawHub",
            )
        trust_state = str((raw_trust or {}).get("state") or self.trust_level)

        if install_kind == "archive":
            archive = data.get("archive")
            if not isinstance(archive, dict):
                raise source_invalid_response_error(
                    phase=DiagnosticPhase.SOURCE,
                    source_name="ClawHub",
                )
            version = str(archive.get("version") or "").strip()
            expected_digest = str(
                archive.get("sha256") or archive.get("digest") or ""
            ).strip()
            artifact_url = _safe_artifact_url(self._base_url, archive.get("downloadUrl"))
            if not version or not artifact_url:
                return _blocking_resolution(
                    ref,
                    identifier,
                    code="SOURCE_INVALID_ARCHIVE_HANDOFF",
                    message="ClawHub did not provide an immutable archive hand-off.",
                )
            if expected_digest and not _SHA256_RE.fullmatch(expected_digest):
                return _blocking_resolution(
                    ref,
                    identifier,
                    code="SOURCE_INVALID_ARTIFACT_DIGEST",
                    message="ClawHub returned an invalid SHA-256 artifact digest.",
                    phase=DiagnosticPhase.SECURITY,
                )
            if urlparse(artifact_url).scheme != "https" and not expected_digest:
                return _blocking_resolution(
                    ref,
                    identifier,
                    code="ARTIFACT_TRANSPORT_INSECURE",
                    message=(
                        "ClawHub returned a plaintext artifact URL without a verifiable "
                        "SHA-256 digest."
                    ),
                    phase=DiagnosticPhase.SECURITY,
                    details={"scheme": urlparse(artifact_url).scheme},
                )
            identity = _resolved_registry_identity(data, ref, resolved_slug)
            if identity is None:
                return _blocking_resolution(
                    ref,
                    identifier,
                    code="SOURCE_PUBLISHER_UNRESOLVED",
                    message="ClawHub did not bind the archive to a stable publisher identity.",
                )
            package_ref, publisher = identity
            canonical = f"{package_ref}@{version}"
            meta = SkillMeta(
                name=resolved_slug,
                description=(
                    _registry_description(data)
                    or self._registry_descriptions.get(package_ref, "")
                ),
                version=version,
                author=publisher,
                source_id=self.source_id,
                trust_level=self.trust_level,
                identifier=canonical,
                homepage=homepage,
                canonical_identifier=canonical,
            )
            return SourceResolution(
                source_id=self.source_id,
                requested_identifier=identifier,
                canonical_identifier=canonical,
                immutable=True,
                revision=version,
                artifact_kind="archive",
                artifact_url=artifact_url,
                expected_digest=expected_digest,
                trust_state=trust_state,
                publisher=publisher,
                version=version,
                upstream_url=homepage,
                package_identifier=package_ref,
                meta=meta,
            )

        if install_kind == "github":
            github = data.get("github")
            if not isinstance(github, dict):
                raise source_invalid_response_error(
                    phase=DiagnosticPhase.SOURCE,
                    source_name="ClawHub",
                )
            repository = str(github.get("repo") or "").strip()
            commit = str(github.get("commit") or "").strip().lower()
            content_hash = str(github.get("contentHash") or "").strip()
            raw_path = str(github.get("path") or "").strip().strip("/")
            try:
                skill_path = normalize_relative_path(raw_path).as_posix() if raw_path else ""
            except ArchiveNormalizationError:
                skill_path = ""
                repository = ""
            if skill_path and PurePosixPath(skill_path).name.casefold() in {
                "skill.md",
                "skills.md",
            }:
                parent = PurePosixPath(skill_path).parent
                skill_path = "" if str(parent) == "." else parent.as_posix()
            upstream_url = _safe_artifact_url(self._base_url, github.get("sourceUrl"))
            if (
                not _GITHUB_REPO_RE.fullmatch(repository)
                or not _COMMIT_RE.fullmatch(commit)
                or not content_hash
                or not upstream_url
            ):
                return _blocking_resolution(
                    ref,
                    identifier,
                    code="SOURCE_INVALID_GITHUB_HANDOFF",
                    message="ClawHub did not provide a commit-pinned GitHub hand-off.",
                )
            identity = _resolved_registry_identity(data, ref, resolved_slug)
            if identity is None:
                return _blocking_resolution(
                    ref,
                    identifier,
                    code="SOURCE_PUBLISHER_UNRESOLVED",
                    message=(
                        "ClawHub did not bind the GitHub hand-off to a stable "
                        "publisher identity."
                    ),
                )
            package_ref, registry_publisher = identity
            canonical = f"{package_ref}@{commit}"
            meta = SkillMeta(
                name=resolved_slug,
                description=(
                    _registry_description(data)
                    or self._registry_descriptions.get(package_ref, "")
                ),
                version=commit,
                author=registry_publisher or repository.split("/", 1)[0],
                source_id=self.source_id,
                trust_level=self.trust_level,
                identifier=canonical,
                homepage=upstream_url,
                canonical_identifier=canonical,
            )
            return SourceResolution(
                source_id=self.source_id,
                requested_identifier=identifier,
                canonical_identifier=canonical,
                immutable=True,
                revision=commit,
                artifact_kind="github-tree",
                artifact_url=upstream_url,
                repository=repository,
                skill_path=skill_path,
                resolver_content_hash=content_hash,
                trust_state=trust_state,
                publisher=meta.author,
                version=commit,
                upstream_url=upstream_url,
                package_identifier=package_ref,
                meta=meta,
            )

        return _blocking_resolution(
            ref,
            identifier,
            code="SOURCE_HANDOFF_UNSUPPORTED",
            message=f"ClawHub returned unsupported install kind {install_kind!r}.",
            details={"installKind": install_kind},
        )

    async def fetch(self, identifier: str) -> SkillBundle | None:
        resolution = await self.resolve(identifier)
        if resolution is None:
            return None
        try:
            return await self.fetch_resolved(resolution)
        except SkillSourceFetchError as exc:
            log.warning(
                "clawhub.fetch_rejected",
                identifier=resolution.canonical_identifier,
                diagnostics=[item.code for item in exc.diagnostics],
            )
            return None

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle | None:
        if not resolution.immutable or any(
            diagnostic.blocking for diagnostic in resolution.diagnostics
        ):
            return None
        if resolution.artifact_kind == "github-tree":
            delegated = SourceResolution(
                source_id="github",
                requested_identifier=resolution.requested_identifier,
                canonical_identifier=(
                    f"{resolution.repository}@{resolution.revision}:"
                    f"{resolution.skill_path + '/' if resolution.skill_path else ''}SKILL.md"
                ),
                immutable=True,
                revision=resolution.revision,
                artifact_kind="github-tree",
                repository=resolution.repository,
                skill_path=resolution.skill_path,
                resolver_content_hash=resolution.resolver_content_hash,
                trust_state=resolution.trust_state or self.trust_level,
                publisher=resolution.publisher,
                version=resolution.version,
                upstream_url=resolution.upstream_url,
                package_identifier=(
                    f"{resolution.repository}:{resolution.skill_path}"
                    if resolution.skill_path
                    else resolution.repository
                ),
                allow_legacy_manifest_names=True,
            )
            bundle = await self._github_source.fetch_resolved(delegated)
            if bundle is None:
                return None
            meta = resolution.meta or bundle.meta
            name = meta.name if meta is not None else bundle.name
            fetched_resolution = bundle.resolution
            artifact_digest = (
                fetched_resolution.expected_digest
                if fetched_resolution is not None
                else ""
            )
            return SkillBundle(
                name=name,
                files=bundle.files,
                meta=meta,
                resolution=replace(resolution, expected_digest=artifact_digest),
                file_modes=bundle.file_modes,
            )
        if resolution.artifact_kind != "archive" or not resolution.artifact_url:
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_HANDOFF_UNSUPPORTED",
                "ClawHub resolution does not contain a supported immutable artifact hand-off.",
                phase=DiagnosticPhase.FETCH,
            )

        import httpx

        try:
            current_url = resolution.artifact_url
            content = b""
            for _redirect_count in range(_MAX_ARTIFACT_REDIRECTS + 1):
                if urlparse(current_url).scheme != "https" and not resolution.expected_digest:
                    raise SkillSourceFetchError.diagnostic(
                        "ARTIFACT_TRANSPORT_INSECURE",
                        (
                            "ClawHub redirected to a plaintext artifact URL without a "
                            "verifiable SHA-256 digest."
                        ),
                        phase=DiagnosticPhase.SECURITY,
                    )
                vetted = _validate_artifact_url(current_url)
                transport_kwargs: dict[str, object] = {}
                if _trust_env():
                    proxy_url = _artifact_proxy_url(current_url)
                    if proxy_url is not None:
                        transport_kwargs["proxy"] = proxy_url
                transport = _artifact_transport(current_url, vetted, **transport_kwargs)
                client_kwargs: dict[str, object] = {
                    "timeout": 30,
                    "trust_env": _trust_env(),
                    "follow_redirects": False,
                }
                if transport is not None:
                    client_kwargs["transport"] = transport
                async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
                    stream = getattr(client, "stream", None)
                    if callable(stream):
                        async with stream(
                            "GET",
                            current_url,
                            headers=self._download_headers(current_url),
                        ) as response:
                            if response.status_code in _REDIRECT_STATUSES:
                                location = response.headers.get("location")
                            else:
                                raise_for_source_http_status(
                                    response,
                                    phase=DiagnosticPhase.FETCH,
                                    source_name="ClawHub",
                                )
                                chunks: list[bytes] = []
                                size = 0
                                async for chunk in response.aiter_bytes():
                                    size += len(chunk)
                                    if size > DEFAULT_ARCHIVE_LIMITS.max_archive_bytes:
                                        raise ValueError(
                                            "Skill archive exceeds the 50 MiB download limit"
                                        )
                                    chunks.append(chunk)
                                content = b"".join(chunks)
                                location = None
                    else:  # One-cycle compatibility for source adapter test doubles.
                        response = await client.get(
                            current_url,
                            headers=self._download_headers(current_url),
                        )
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                        else:
                            raise_for_source_http_status(
                                response,
                                phase=DiagnosticPhase.FETCH,
                                source_name="ClawHub",
                            )
                            content = response.content
                if response.status_code not in _REDIRECT_STATUSES:
                    break
                if not location:
                    raise ValueError("Skill archive redirect has no location")
                current_url = urljoin(current_url, location)
            else:
                raise ValueError("Skill archive exceeded the redirect limit")
        except SkillSourceFetchError:
            raise
        except Exception as exc:
            log.warning(
                "clawhub.fetch_failed",
                identifier=resolution.canonical_identifier,
                error=str(exc),
            )
            lowered = str(exc).lower()
            if any(marker in lowered for marker in ("50 mib", "size limit", "too large")):
                code = "FETCH_SIZE_LIMIT"
                phase = DiagnosticPhase.FETCH
            elif "redirect" in lowered:
                code = "FETCH_REDIRECT_INVALID"
                phase = DiagnosticPhase.FETCH
            elif isinstance(exc, ValueError) and any(
                marker in lowered
                for marker in ("private", "blocked", "unsafe", "dns", "address")
            ):
                code = "ARTIFACT_URL_UNSAFE"
                phase = DiagnosticPhase.SECURITY
            else:
                raise source_transport_error(
                    exc,
                    phase=DiagnosticPhase.FETCH,
                    source_name="ClawHub",
                ) from exc
            raise SkillSourceFetchError.diagnostic(
                code,
                str(exc) or "ClawHub artifact fetch failed.",
                phase=phase,
                hint="Check source availability and the immutable install reference.",
            ) from exc
        if len(content) > DEFAULT_ARCHIVE_LIMITS.max_archive_bytes:
            log.warning("clawhub.fetch_archive_too_large", size=len(content))
            raise SkillSourceFetchError.diagnostic(
                "FETCH_SIZE_LIMIT",
                "Skill archive exceeds the 50 MiB download limit.",
                phase=DiagnosticPhase.FETCH,
            )
        try:
            normalized = normalize_skill_archive_result(content)
        except ArchiveNormalizationError as exc:
            log.warning(
                "clawhub.fetch_invalid_archive",
                identifier=resolution.canonical_identifier,
                error=str(exc),
            )
            raise _archive_diagnostic_error(exc) from exc

        digest = hashlib.sha256(content).hexdigest()
        if resolution.expected_digest and resolution.expected_digest.lower() not in {
            digest,
            f"sha256:{digest}",
        }:
            log.warning("clawhub.fetch_digest_mismatch", identifier=resolution.canonical_identifier)
            raise SkillSourceFetchError.diagnostic(
                "ARTIFACT_DIGEST_MISMATCH",
                "The downloaded ClawHub archive digest does not match its resolution.",
                phase=DiagnosticPhase.SECURITY,
            )
        diagnostics = resolution.diagnostics
        if set(normalized.files) - set(normalized.file_modes) and not any(
            diagnostic.code == "FILE_MODE_UNAVAILABLE"
            for diagnostic in diagnostics
        ):
            diagnostics = (
                *diagnostics,
                SkillDiagnostic(
                    code="FILE_MODE_UNAVAILABLE",
                    severity=DiagnosticSeverity.WARNING,
                    phase=DiagnosticPhase.ARCHIVE,
                    message="The archive omitted POSIX mode metadata for one or more files.",
                ),
            )
        if not resolution.expected_digest:
            resolution = replace(
                resolution,
                expected_digest=digest,
                diagnostics=diagnostics,
            )
        elif diagnostics != resolution.diagnostics:
            resolution = replace(resolution, diagnostics=diagnostics)
        meta = resolution.meta
        name = meta.name if meta is not None else resolution.canonical_identifier.split("@", 1)[0]
        return SkillBundle(
            name=name,
            files=normalized.files,
            meta=meta,
            resolution=resolution,
            file_modes=normalized.file_modes,
        )

    async def inspect(self, identifier: str) -> SkillMeta | None:
        import httpx

        ref = _parse_identifier(identifier)
        if ref is None:
            return None
        params = {"ownerHandle": ref.owner_handle} if ref.owner_handle else None
        url = f"{self._base_url}/api/v1/skills/{quote(ref.slug, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=_trust_env()) as client:
                response = await client.get(url, params=params, headers=self._headers())
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            log.warning("clawhub.inspect_failed", identifier=identifier, error=str(exc))
            return None

        if not isinstance(data, dict):
            return None
        raw_item = data.get("skill")
        raw_latest = data.get("latestVersion")
        raw_owner = data.get("owner")
        item: dict[str, Any] = raw_item if isinstance(raw_item, dict) else data
        latest: dict[str, Any] = raw_latest if isinstance(raw_latest, dict) else {}
        owner: dict[str, Any] = raw_owner if isinstance(raw_owner, dict) else {}
        canonical = ref.package_ref
        return SkillMeta(
            name=str(item.get("displayName") or item.get("name") or item.get("slug") or ref.slug),
            description=str(item.get("summary") or item.get("description") or ""),
            version=str(latest.get("version") or item.get("version") or ""),
            author=str(owner.get("handle") or item.get("author") or ref.owner_handle),
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=canonical,
            homepage=str(item.get("homepage") or ""),
            license=str(item.get("license") or ""),
            tags=list(item.get("tags") or []) if isinstance(item.get("tags"), list) else [],
            canonical_identifier=canonical,
        )
