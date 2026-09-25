"""Tencent SkillHub public registry source.

SkillHub exposes a JSON registry and versioned ZIP artifacts.  The archive
normalisation, download limits, redirect validation, digest verification and
transactional installation are shared with the existing ClawHub adapter by
subclassing its archive fetch path; only the registry protocol is different.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from opensquilla.env import trust_env as _trust_env
from opensquilla.skills.hub.clawhub import (
    _SHA256_RE,
    ClawHubSource,
    _safe_artifact_url,
)
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.source import (
    SkillMeta,
    SourceResolution,
    raise_for_source_http_status,
    source_invalid_response_error,
    source_transport_error,
)

_DEFAULT_BASE_URL = "https://api.skillhub.cn"


def _payload(value: object) -> object:
    """Unwrap both the public envelope and direct v1 responses."""

    if isinstance(value, dict) and "data" in value:
        return value["data"]
    return value


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _version(*values: object) -> str:
    """Return the first usable version from alternate registry fields."""

    for value in values:
        if isinstance(value, dict):
            version = _text(value.get("version"), value.get("name"))
        else:
            version = _text(value)
        if version:
            return version
    return ""


class SkillHubSource(ClawHubSource):
    """Skill source backed by Tencent's public SkillHub registry."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        token: str | None = None,
    ) -> None:
        # ClawHubSource owns the hardened archive fetch implementation.  The
        # GitHub delegation path is not used by SkillHub, but keeping the
        # parent initialisation preserves the tested staging behaviour.
        super().__init__(base_url=base_url, token=token)

    @property
    def source_id(self) -> str:
        return "skillhub"

    @property
    def source_name(self) -> str:
        return "SkillHub"

    @property
    def trust_level(self) -> str:
        return "community"

    @property
    def requires_immutable_resolution(self) -> bool:
        """SkillHub installs are always pinned to the resolved version."""

        return True

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-API-Key"] = self._token
        return headers

    def _download_headers(self, url: str) -> dict[str, str]:
        headers = {"Accept": "application/zip, application/octet-stream"}
        if self._token:
            base = urlparse(self._base_url)
            target = urlparse(url)
            if (base.scheme.lower(), base.netloc.lower()) == (
                target.scheme.lower(),
                target.netloc.lower(),
            ):
                headers["X-API-Key"] = self._token
        return headers

    @staticmethod
    def _row_meta(row: dict[str, Any], *, source_id: str = "skillhub") -> SkillMeta:
        slug = _text(row.get("slug"), row.get("id"), row.get("name"))
        name = _text(row.get("displayName"), row.get("title"), row.get("name"), slug)
        author_value = (
            row.get("author")
            or row.get("publisher")
            or row.get("owner")
            or row.get("ownerName")
        )
        author = (
            _text(
                author_value.get("name"),
                author_value.get("displayName"),
                author_value.get("handle"),
                author_value.get("id"),
            )
            if isinstance(author_value, dict)
            else _text(author_value)
        )
        origin = _text(row.get("source"), row.get("origin"), row.get("originSource"))
        if origin.casefold() not in {"clawhub", "github"}:
            origin = ""
        upstream = _text(
            row.get("sourceUrl"), row.get("source_url"), row.get("upstreamUrl"),
            row.get("upstream_url"), row.get("homepage"),
        )
        verified = row.get("verified") is True or row.get("isVerified") is True
        # ``source`` describes provenance (for example a ClawHub mirror), not
        # the OpenSquilla trust tier.  Keep the latter within the source
        # contract's documented values and expose provenance separately.
        trust = "trusted" if verified else "community"
        signature = _text(row.get("signatureStatus"), row.get("signature_status"))
        content_hash = _text(row.get("contentHash"), row.get("content_hash"), row.get("sha256"))
        return SkillMeta(
            name=name,
            description=_text(row.get("description"), row.get("summary")),
            version=_version(
                row.get("version"),
                row.get("currentVersion"),
                row.get("latestVersion"),
            ),
            author=author,
            source_id=source_id,
            trust_level=trust,
            identifier=slug,
            homepage=upstream,
            license=_text(row.get("license"), row.get("licenseName"), row.get("spdx")),
            tags=[str(item) for item in row.get("tags", []) if isinstance(item, (str, int))]
            if isinstance(row.get("tags"), list)
            else [],
            canonical_identifier=slug,
            upstream_url=upstream,
            origin_source=origin,
            signature_status=signature,
            content_hash=content_hash,
        )

    @staticmethod
    def _blocked(
        identifier: str,
        code: str,
        message: str,
        *,
        phase: DiagnosticPhase = DiagnosticPhase.SOURCE,
    ) -> SourceResolution:
        diagnostic = SkillDiagnostic(
            code=code,
            severity=DiagnosticSeverity.ERROR,
            phase=phase,
            message=message,
            blocking=True,
        )
        return SourceResolution(
            source_id="skillhub",
            requested_identifier=identifier,
            canonical_identifier=identifier,
            artifact_kind="unsupported",
            diagnostics=(diagnostic,),
        )

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        import httpx

        url = f"{self._base_url}/api/skills"
        try:
            async with httpx.AsyncClient(timeout=12, trust_env=_trust_env()) as client:
                response = await client.get(
                    url,
                    params={"keyword": query, "pageSize": min(max(limit, 1), 100), "page": 1},
                    headers=self._headers(),
                )
        except Exception as exc:
            raise source_transport_error(
                exc,
                phase=DiagnosticPhase.SOURCE,
                source_name="SkillHub",
            ) from exc
        raise_for_source_http_status(response, phase=DiagnosticPhase.SOURCE, source_name="SkillHub")
        try:
            payload = _payload(response.json())
        except (TypeError, ValueError) as exc:
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="SkillHub",
            ) from exc
        data = _mapping(payload)
        rows: object = payload
        if data:
            # Empty collections are valid zero-hit searches. Select by key
            # presence so malformed values are not hidden by a fallback field.
            rows = next((data[key] for key in ("skills", "results", "items") if key in data), None)
        if not isinstance(rows, list):
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="SkillHub",
            )
        results: list[SkillMeta] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            meta = self._row_meta(row)
            if not meta.identifier:
                continue
            results.append(meta)
        if rows and not results:
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="SkillHub",
            )
        return results[:limit]

    async def resolve(self, identifier: str) -> SourceResolution | None:
        import httpx

        requested = identifier.strip()
        if not requested:
            return None
        slug, separator, requested_version = requested.rpartition("@")
        if not separator or not slug:
            slug, requested_version = requested, ""
        if "/" in slug or slug in {".", ".."}:
            return None
        url = f"{self._base_url}/api/v1/skills/{quote(slug, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                response = await client.get(url, headers=self._headers())
        except Exception as exc:
            raise source_transport_error(
                exc,
                phase=DiagnosticPhase.SOURCE,
                source_name="SkillHub",
            ) from exc
        raise_for_source_http_status(response, phase=DiagnosticPhase.SOURCE, source_name="SkillHub")
        try:
            payload = _payload(response.json())
        except (TypeError, ValueError) as exc:
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="SkillHub",
            ) from exc
        row = _mapping(payload)
        # The detail endpoint keeps ``latestVersion``/owner/namespace at the
        # response root and puts user-facing fields under ``skill``.  Merge
        # both layers so version resolution does not silently reject a valid
        # response merely because the registry split its envelope.
        nested_skill = row.get("skill")
        if isinstance(nested_skill, dict):
            row = {**row, **nested_skill}
        elif isinstance(row.get("data"), dict):
            row = row["data"]
        meta = self._row_meta(row)
        version = requested_version or _version(
            row.get("version"), row.get("currentVersion"), row.get("latestVersion"),
        )
        if not version:
            return self._blocked(
                requested,
                "SOURCE_VERSION_REQUIRED",
                "SkillHub did not return an immutable Skill version.",
            )
        expected_digest = _text(
            row.get("sha256"), row.get("artifactSha256"), row.get("artifact_sha256"),
            row.get("contentHash"), row.get("content_hash"),
        )
        if expected_digest and not _SHA256_RE.fullmatch(expected_digest):
            return self._blocked(
                requested,
                "SOURCE_INVALID_ARTIFACT_DIGEST",
                "SkillHub returned an invalid SHA-256 artifact digest.",
                phase=DiagnosticPhase.SECURITY,
            )
        # The detail endpoint describes the latest release.  If the caller
        # pinned an explicit version, never reuse a latest-release URL unless
        # the response explicitly binds that URL to the requested version.
        download_version = _version(
            row.get("downloadVersion"),
            row.get("download_version"),
            row.get("artifactVersion"),
            row.get("artifact_version"),
        )
        download = "" if requested_version and download_version != requested_version else _text(
            row.get("downloadUrl"), row.get("download_url")
        )
        artifact_url = _safe_artifact_url(self._base_url, download) if download else (
            f"{self._base_url}/api/v1/download?slug={quote(slug, safe='')}"
            f"&version={quote(version, safe='')}"
        )
        if not artifact_url:
            return self._blocked(
                requested,
                "SOURCE_INVALID_ARCHIVE_HANDOFF",
                "SkillHub did not provide a downloadable Skill artifact.",
            )
        canonical = f"{slug}@{version}"
        meta = self._row_meta({**row, "slug": slug, "version": version})
        meta.identifier = canonical
        meta.canonical_identifier = canonical
        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=requested,
            canonical_identifier=canonical,
            immutable=True,
            revision=version,
            artifact_kind="archive",
            artifact_url=artifact_url,
            expected_digest=expected_digest,
            resolver_content_hash=meta.content_hash,
            trust_state=meta.trust_level,
            publisher=meta.author,
            version=version,
            upstream_url=meta.upstream_url or f"https://skillhub.cn/skills/{quote(slug, safe='')}",
            package_identifier=slug,
            meta=meta,
        )

    async def inspect(self, identifier: str) -> SkillMeta | None:
        resolution = await self.resolve(identifier)
        if resolution is None or any(item.blocking for item in resolution.diagnostics):
            return None
        return resolution.meta


__all__ = ["SkillHubSource"]
