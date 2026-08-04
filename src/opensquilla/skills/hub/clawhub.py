"""ClawHub Community source adapter - connects to clawhub.ai API."""

from __future__ import annotations

import io
import posixpath
import zipfile

import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillFetchError,
    SkillMeta,
    SkillSource,
)

log = structlog.get_logger(__name__)

_DEFAULT_BASE_URL = "https://clawhub.ai"
_GITHUB_ARCHIVE_URL = (
    "https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
)


def _extract_skill_zip(
    content: bytes, preferred_name: str | None = None
) -> dict[str, str | bytes] | None:
    """Extract a skill directory from a ZIP archive.

    Handles three layouts:
    - SKILL.md at the archive root (clawhub native downloads);
    - a single top-level wrapper directory (GitHub archive zips);
    - a multi-skill collection where the skill lives under ``skills/<name>/``
      (skills.sh repos mirrored on GitHub).

    Returns a ``{relative_path: content}`` mapping rooted at the skill
    directory, or ``None`` when no SKILL.md can be found.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                return None

            candidates = sorted(
                (n for n in names if n.rsplit("/", 1)[-1] == "SKILL.md"),
                key=lambda n: n.count("/"),
            )
            if not candidates:
                return None

            if preferred_name:
                exact = [
                    n
                    for n in candidates
                    if n.rsplit("/", 1)[0].rsplit("/", 1)[-1] == preferred_name
                ]
                if exact:
                    candidates = exact

            skill_md_path = candidates[0]
            skill_root = (
                skill_md_path.rsplit("/", 1)[0] if "/" in skill_md_path else ""
            )

            files: dict[str, str | bytes] = {}
            for name in names:
                rel = name
                if skill_root:
                    if not rel.startswith(skill_root + "/"):
                        continue
                    rel = rel[len(skill_root) + 1 :]
                rel = posixpath.normpath(rel)
                if rel.startswith("..") or rel.startswith("/"):
                    continue
                raw = zf.read(name)
                try:
                    files[rel] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    files[rel] = raw
            return files
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None


class ClawHubSource(SkillSource):
    """Skill source backed by the ClawHub community registry."""

    def __init__(self, base_url: str = _DEFAULT_BASE_URL, token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token

    @property
    def source_id(self) -> str:
        return "clawhub"

    @property
    def trust_level(self) -> str:
        return "community"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _skill_meta(self, item: dict) -> SkillMeta:
        """Build SkillMeta from a raw /api/v1/search item."""
        identity = item.get("sourceIdentity") or {}
        owner = identity.get("owner") or ""
        repo = identity.get("repo") or ""
        fallback_url = ""
        if owner and repo:
            fallback_url = _GITHUB_ARCHIVE_URL.format(
                owner=owner, repo=repo, branch="main"
            )
        return SkillMeta(
            name=item.get("displayName", item.get("name", item.get("slug", ""))),
            description=item.get("summary", item.get("description", "")),
            version=item.get("version", ""),
            author=item.get("ownerHandle", ""),
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=item.get("slug", item.get("name", "")),
            homepage=item.get("homepage", ""),
            license=item.get("license", ""),
            tags=item.get("tags", []),
            owner_handle=item.get("ownerHandle", ""),
            fallback_source=item.get("source", ""),
            fallback_url=fallback_url,
        )

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        import httpx

        url = f"{self._base_url}/api/v1/search"
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=_trust_env()) as client:
                resp = await client.get(
                    url, params={"q": query, "limit": limit}, headers=self._headers()
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            log.warning("clawhub.search_failed", error=str(exc))
            return []

        # Handle rate limit / error disguised as 200
        if isinstance(data, str) or (isinstance(data, dict) and "error" in data):
            log.warning("clawhub.search_error", data=str(data)[:100])
            return []

        results = []
        for item in data if isinstance(data, list) else data.get("results", data.get("skills", [])):
            results.append(self._skill_meta(item))
        return results[:limit]

    async def _resolve(self, identifier: str) -> SkillMeta | None:
        """Look up a skill listing by slug (registry search API)."""
        results = await self.search(identifier, limit=20)
        for meta in results:
            if meta.identifier == identifier:
                return meta
        return None

    async def _download(self, slug: str, owner_handle: str | None = None) -> bytes | None:
        """GET /api/v1/download. Returns raw bytes, None on 404/other failure."""
        import httpx

        params: dict[str, str] = {"slug": slug}
        if owner_handle:
            params["ownerHandle"] = owner_handle
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=_trust_env()) as client:
                resp = await client.get(
                    f"{self._base_url}/api/v1/download",
                    params=params,
                    headers=self._headers(),
                )
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 429:
                raise SkillFetchError(
                    "ClawHub is rate-limited right now. Try again later."
                )
            log.warning(
                "clawhub.download_failed",
                slug=slug,
                owner_handle=owner_handle,
                status=resp.status_code,
                body=resp.text[:100],
            )
            return None
        except SkillFetchError:
            raise
        except Exception as exc:
            log.warning("clawhub.download_error", slug=slug, error=str(exc))
            return None

    async def _fetch_fallback_zip(
        self, fallback_url: str, identifier: str, fallback_source: str
    ) -> dict[str, str | bytes] | None:
        """Download an upstream archive zip (e.g. GitHub) for aggregated entries."""
        import httpx

        urls = [fallback_url]
        if fallback_url.endswith("/main.zip"):
            urls.append(fallback_url.replace("/main.zip", "/master.zip"))

        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=30, trust_env=_trust_env()) as client:
                    resp = await client.get(url, follow_redirects=True)
                if resp.status_code != 200:
                    continue
                files = _extract_skill_zip(resp.content, preferred_name=identifier)
                if files is not None:
                    return files
            except Exception as exc:
                log.warning(
                    "clawhub.fallback_fetch_error",
                    identifier=identifier,
                    url=url,
                    error=str(exc),
                )
        log.warning(
            "clawhub.fallback_fetch_failed",
            identifier=identifier,
            fallback_source=fallback_source,
        )
        return None

    async def fetch(self, identifier: str) -> SkillBundle | None:
        """Download a skill, resolving duplicate slugs and aggregated entries.

        Strategy:
        1. Try ``/api/v1/download?slug=<identifier>`` (native clawhub entries).
        2. When that fails, resolve the listing via the search API:
           - aggregated entries (skills.sh, ...) are not served by the
             download endpoint at all, so their artifact is pulled from the
             upstream archive URL captured during search (GitHub zip);
           - duplicate slugs are retried with the publisher ``ownerHandle``.
        """
        # 1. Native download path.
        content = await self._download(identifier)
        if content is not None:
            files = _extract_skill_zip(content, preferred_name=identifier)
            if files is None:
                raise SkillFetchError(
                    f"Could not parse the ClawHub archive for '{identifier}'."
                )
            return SkillBundle(name=identifier, files=files)

        # 2. Resolve the listing to learn the publisher / upstream source.
        meta = await self._resolve(identifier)

        # 2a. Aggregated entries (skills.sh, ...) are not served by /download:
        #     pull the artifact from the upstream archive URL instead.
        if meta is not None and meta.fallback_url:
            files = await self._fetch_fallback_zip(
                meta.fallback_url, identifier, meta.fallback_source
            )
            if files is not None:
                return SkillBundle(name=identifier, files=files)
            raise SkillFetchError(
                f"Skill '{identifier}' comes from {meta.fallback_source or 'an upstream source'}, "
                "but its archive could not be downloaded."
            )

        # 2b. Ambiguous slug: retry with the publisher handle.
        if meta is not None and meta.owner_handle:
            content = await self._download(identifier, meta.owner_handle)
            if content is not None:
                files = _extract_skill_zip(content, preferred_name=identifier)
                if files is None:
                    raise SkillFetchError(
                        f"Could not parse the ClawHub archive for '{identifier}'."
                    )
                return SkillBundle(name=identifier, files=files)

        if meta is not None:
            raise SkillFetchError(
                f"ClawHub could not serve '{identifier}'. It may have been removed "
                "or its publisher disabled downloads."
            )

        raise SkillFetchError(
            f"Skill '{identifier}' was not found on ClawHub. It may have been "
            "removed from the community registry."
        )

    async def inspect(self, identifier: str) -> SkillMeta | None:
        """Get metadata for a skill without downloading."""
        return await self._resolve(identifier)
