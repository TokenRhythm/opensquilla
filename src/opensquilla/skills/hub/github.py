"""GitHub skill source — searches and installs SKILL.md directories."""

from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urlparse

import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.skills.hub.archive import (
    DEFAULT_ARCHIVE_LIMITS,
    ArchiveNormalizationError,
    normalize_relative_path,
    validate_portable_file_paths,
)
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
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

_GITHUB_HOSTS = {"github.com", "www.github.com"}
_RAW_GITHUB_HOST = "raw.githubusercontent.com"
_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:@(?P<ref>[^:]+))?(?::(?P<path>.+))?$"
)
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MANIFEST_NAME = "SKILL.md"
_RESERVED_COMPONENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".opensquilla",
        ".opensquilla-rollback",
        ".opensquilla-staging",
        ".quarantine",
        ".staging",
        "__macosx",
    }
)


def _valid_repository(owner: str, repo: str) -> bool:
    return bool(
        owner not in {".", ".."}
        and repo not in {".", ".."}
        and _REPOSITORY_RE.fullmatch(f"{owner}/{repo}")
    )


@dataclass(frozen=True)
class _GitHubSkillRef:
    owner: str
    repo: str
    ref: str
    path: str

    @property
    def repo_full(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def skill_dir(self) -> str:
        path = self.path.strip("/")
        if path.rsplit("/", 1)[-1] == _MANIFEST_NAME and "/" in path:
            return path.rsplit("/", 1)[0]
        if path == _MANIFEST_NAME:
            return ""
        return path

    @property
    def skill_file(self) -> str:
        directory = self.skill_dir
        return f"{directory}/SKILL.md" if directory else "SKILL.md"

    @property
    def canonical_identifier(self) -> str:
        return f"{self.repo_full}@{self.ref}:{self.skill_file}"

    @property
    def homepage(self) -> str:
        if self.skill_dir:
            return f"https://github.com/{self.repo_full}/tree/{self.ref}/{self.skill_dir}"
        return f"https://github.com/{self.repo_full}/tree/{self.ref}"


def _clean_repo_name(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def _split_path(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _normalize_skill_path(path: str) -> str | None:
    raw = path.strip().replace("\\", "/").strip("/")
    if not raw:
        return ""
    try:
        return normalize_relative_path(raw).as_posix()
    except ArchiveNormalizationError:
        return None


def _parse_identifier(identifier: str) -> _GitHubSkillRef | None:
    raw = identifier.strip()
    if raw.startswith("github.com/"):
        raw = "https://" + raw

    if raw.startswith(("http://", "https://")):
        parsed = urlparse(raw)
        host = parsed.netloc.lower()
        parts = _split_path(parsed.path)
        if host in _GITHUB_HOSTS:
            if len(parts) < 2:
                return None
            owner, repo = parts[0], _clean_repo_name(parts[1])
            if not _valid_repository(owner, repo):
                return None
            ref = "HEAD"
            skill_path = ""
            if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
                ref = parts[3]
                skill_path = "/".join(parts[4:])
            normalized_path = _normalize_skill_path(skill_path)
            if normalized_path is None:
                return None
            return _GitHubSkillRef(owner, repo, ref, normalized_path)

        if host == _RAW_GITHUB_HOST:
            if len(parts) < 4:
                return None
            owner, repo = parts[0], _clean_repo_name(parts[1])
            if not _valid_repository(owner, repo):
                return None
            ref = parts[2]
            skill_path = "/".join(parts[3:])
            normalized_path = _normalize_skill_path(skill_path)
            if normalized_path is None:
                return None
            return _GitHubSkillRef(owner, repo, ref, normalized_path)

        return None

    match = _REPO_RE.match(raw)
    if match is None:
        return None
    normalized_path = _normalize_skill_path(match.group("path") or "")
    if normalized_path is None:
        return None
    owner = match.group("owner")
    repo = _clean_repo_name(match.group("repo"))
    if not _valid_repository(owner, repo):
        return None
    return _GitHubSkillRef(
        owner,
        repo,
        match.group("ref") or "HEAD",
        normalized_path,
    )


def package_identifier_for(identifier: str) -> str:
    """Normalize a GitHub install reference to repository plus Skill subpath."""

    ref = _parse_identifier(identifier)
    if ref is None:
        return ""
    repository = ref.repo_full.casefold()
    return f"{repository}:{ref.skill_dir}" if ref.skill_dir else repository


def _relative_to_skill_dir(path: str, skill_dir: str) -> str | None:
    if not skill_dir:
        return path
    prefix = skill_dir.rstrip("/") + "/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return None


def _decode_file(path: str, content: bytes) -> str | bytes:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content


async def _fetch_tree_payload(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = await client.get(url, headers=headers)
    raise_for_source_http_status(
        response,
        phase=DiagnosticPhase.FETCH,
        source_name="GitHub",
    )
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise source_invalid_response_error(
            phase=DiagnosticPhase.FETCH,
            source_name="GitHub",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
        raise source_invalid_response_error(
            phase=DiagnosticPhase.FETCH,
            source_name="GitHub",
        )
    return payload


def _github_tree_url(ref: _GitHubSkillRef, treeish: str, *, recursive: bool) -> str:
    suffix = "?recursive=1" if recursive else ""
    return (
        f"https://api.github.com/repos/{ref.repo_full}/git/trees/"
        f"{quote(treeish, safe='')}{suffix}"
    )


async def _fetch_explicit_subtree(
    client: Any,
    ref: _GitHubSkillRef,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Resolve an explicit Skill directory without trusting a truncated root tree."""

    treeish = ref.ref
    for component in PurePosixPath(ref.skill_dir).parts:
        payload = await _fetch_tree_payload(
            client,
            _github_tree_url(ref, treeish, recursive=False),
            headers=headers,
        )
        if payload.get("truncated"):
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_TREE_TRUNCATED",
                "GitHub truncated a directory while resolving the Skill subpath.",
                phase=DiagnosticPhase.FETCH,
                hint="Reduce the number of entries in the selected repository directory.",
            )
        matches = [
            item
            for item in payload["tree"]
            if isinstance(item, dict) and item.get("path") == component
        ]
        if len(matches) != 1:
            raise SkillSourceFetchError.diagnostic(
                "FETCH_NOT_FOUND",
                f"GitHub could not resolve the Skill subpath component: {component}",
                phase=DiagnosticPhase.FETCH,
                hint="Verify the exact commit and Skill subpath.",
                path=ref.skill_dir,
            )
        selected = matches[0]
        if selected.get("type") != "tree":
            raise SkillSourceFetchError.diagnostic(
                "ARTIFACT_FILE_TYPE_UNSUPPORTED",
                f"GitHub Skill subpath is not a regular directory: {ref.skill_dir}",
                phase=DiagnosticPhase.SECURITY,
                path=ref.skill_dir,
            )
        next_treeish = str(selected.get("sha") or "")
        if not _COMMIT_RE.fullmatch(next_treeish):
            raise source_invalid_response_error(
                phase=DiagnosticPhase.FETCH,
                source_name="GitHub",
            )
        treeish = next_treeish

    return await _fetch_tree_payload(
        client,
        _github_tree_url(ref, treeish, recursive=True),
        headers=headers,
    )


async def _read_bounded_blob(
    client: Any,
    url: str,
    *,
    headers: dict[str, str],
    aggregate_remaining: int,
) -> bytes:
    limit = min(DEFAULT_ARCHIVE_LIMITS.max_entry_bytes, aggregate_remaining)
    stream = getattr(client, "stream", None)
    if callable(stream):
        chunks: list[bytes] = []
        size = 0
        async with stream("GET", url, headers=headers) as response:
            raise_for_source_http_status(
                response,
                phase=DiagnosticPhase.FETCH,
                source_name="GitHub",
            )
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise ValueError("GitHub Skill blob exceeds the download limit")
                chunks.append(chunk)
        return b"".join(chunks)

    # One-cycle compatibility for source adapter test doubles.
    response = await client.get(url, headers=headers)
    raise_for_source_http_status(
        response,
        phase=DiagnosticPhase.FETCH,
        source_name="GitHub",
    )
    content = bytes(response.content)
    if len(content) > limit:
        raise ValueError("GitHub Skill blob exceeds the download limit")
    return content


def _bundle_digest(files: dict[str, str | bytes]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(files):
        content = files[path]
        raw = content.encode("utf-8") if isinstance(content, str) else content
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(raw).to_bytes(8, "big"))
        hasher.update(raw)
    return hasher.hexdigest()


def _frontmatter_field(skill_md: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:\s*(.+?)\s*$", skill_md, re.MULTILINE)
    if match is None:
        return ""
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _fallback_name(ref: _GitHubSkillRef) -> str:
    if ref.skill_dir:
        return ref.skill_dir.rstrip("/").rsplit("/", 1)[-1]
    return ref.repo


class GitHubSource(SkillSource):
    """Skill source backed by GitHub code search and repository tree fetches."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token

    @property
    def source_id(self) -> str:
        return "github"

    @property
    def trust_level(self) -> str:
        return "community"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
        if self._token:
            h["Authorization"] = f"token {self._token}"
        return h

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        import httpx

        # GitHub rejects unauthenticated code search outright, so without a token
        # this request cannot succeed — every Community search would spend a round
        # trip earning a 401 and then warn about it, while the results the user sees
        # come from the other sources regardless. Skip the call instead of crying
        # wolf. Only search needs the token: fetch and inspect read the tree and raw
        # endpoints, which do serve anonymous callers, so installing a GitHub skill
        # by identifier keeps working.
        if not self._token:
            log.debug("github.search_skipped_unauthenticated", query=query)
            return []

        search_query = f"{query} filename:SKILL.md"
        url = "https://api.github.com/search/code"
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=_trust_env()) as client:
                resp = await client.get(
                    url,
                    params={"q": search_query, "per_page": min(limit, 30)},
                    headers=self._headers(),
                )
        except Exception as exc:
            log.warning("github.search_failed", error=str(exc))
            raise source_transport_error(
                exc,
                phase=DiagnosticPhase.SOURCE,
                source_name="GitHub",
            ) from exc

        raise_for_source_http_status(
            resp,
            phase=DiagnosticPhase.SOURCE,
            source_name="GitHub",
        )
        try:
            data = resp.json()
        except (TypeError, ValueError) as exc:
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="GitHub",
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="GitHub",
            )

        items = data["items"]
        results: list[SkillMeta] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            repo = item.get("repository", {})
            if not isinstance(repo, dict):
                continue
            full_name = str(repo.get("full_name") or "")
            repository_parts = full_name.split("/", 1)
            if len(repository_parts) != 2 or not _valid_repository(*repository_parts):
                continue
            path = str(item.get("path") or "").strip("/")
            path_parts = path.split("/")
            if (
                not path
                or path_parts[-1] != _MANIFEST_NAME
                or any(part in {"", ".", ".."} for part in path_parts)
            ):
                continue
            # Extract the skill name from the parent directory of a SKILL.md path.
            parts = path.rsplit("/", 2)
            skill_name = parts[-2] if len(parts) >= 2 else repository_parts[1]

            results.append(
                SkillMeta(
                    name=skill_name,
                    description=str(repo.get("description") or ""),
                    source_id=self.source_id,
                    trust_level=self.trust_level,
                    identifier=f"{full_name}:{path}",
                    homepage=str(repo.get("html_url") or ""),
                    canonical_identifier=f"{full_name}:{path}",
                )
            )
        if items and not results:
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="GitHub",
            )
        return results[:limit]

    async def resolve(self, identifier: str) -> SourceResolution | None:
        """Resolve a branch, tag, or HEAD to one immutable commit SHA."""

        import httpx

        ref = _parse_identifier(identifier)
        if ref is None:
            return None

        commit = ref.ref.lower() if _COMMIT_RE.fullmatch(ref.ref) else ""
        if not commit:
            commit_url = (
                f"https://api.github.com/repos/{ref.repo_full}/commits/"
                f"{quote(ref.ref, safe='')}"
            )
            try:
                async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                    response = await client.get(commit_url, headers=self._headers())
            except Exception as exc:
                log.warning("github.resolve_failed", identifier=identifier, error=str(exc))
                raise source_transport_error(
                    exc,
                    phase=DiagnosticPhase.SOURCE,
                    source_name="GitHub",
                ) from exc
            raise_for_source_http_status(
                response,
                phase=DiagnosticPhase.SOURCE,
                source_name="GitHub",
            )
            try:
                response_data = response.json()
            except (TypeError, ValueError) as exc:
                log.warning("github.resolve_invalid_json", identifier=identifier)
                raise source_invalid_response_error(
                    phase=DiagnosticPhase.SOURCE,
                    source_name="GitHub",
                ) from exc
            if not isinstance(response_data, dict) or not isinstance(
                response_data.get("sha"), str
            ):
                raise source_invalid_response_error(
                    phase=DiagnosticPhase.SOURCE,
                    source_name="GitHub",
                )
            commit = str(response_data["sha"]).lower()
        if not _COMMIT_RE.fullmatch(commit):
            log.warning("github.resolve_mutable_ref", identifier=identifier, resolved=commit)
            raise source_invalid_response_error(
                phase=DiagnosticPhase.SOURCE,
                source_name="GitHub",
            )

        resolved_ref = _GitHubSkillRef(ref.owner, ref.repo, commit, ref.path)
        canonical_identifier = resolved_ref.canonical_identifier
        meta = SkillMeta(
            name=_fallback_name(resolved_ref),
            version=commit,
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=canonical_identifier,
            homepage=resolved_ref.homepage,
            canonical_identifier=canonical_identifier,
        )
        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=identifier,
            canonical_identifier=canonical_identifier,
            immutable=True,
            revision=commit,
            artifact_kind="github-tree",
            repository=resolved_ref.repo_full,
            skill_path=resolved_ref.skill_dir,
            trust_state=self.trust_level,
            publisher=resolved_ref.owner,
            version=commit,
            upstream_url=resolved_ref.homepage,
            package_identifier=(
                f"{resolved_ref.repo_full.casefold()}:{resolved_ref.skill_dir}"
                if resolved_ref.skill_dir
                else resolved_ref.repo_full.casefold()
            ),
            meta=meta,
        )

    async def fetch(self, identifier: str) -> SkillBundle | None:
        """Resolve and fetch one immutable snapshot for direct source callers."""

        resolution = await self.resolve(identifier)
        if resolution is None:
            return None
        try:
            return await self.fetch_resolved(resolution)
        except SkillSourceFetchError as exc:
            log.warning(
                "github.fetch_rejected",
                identifier=resolution.canonical_identifier,
                diagnostics=[item.code for item in exc.diagnostics],
            )
            return None

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle | None:
        """Fetch every file beneath the selected path at the resolved commit."""

        import httpx

        repository = resolution.repository.strip()
        safe_skill_path = _normalize_skill_path(resolution.skill_path)
        if (
            "/" not in repository
            or not _COMMIT_RE.fullmatch(resolution.revision)
            or safe_skill_path is None
        ):
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_FETCH_RESOLUTION_INVALID",
                "GitHub fetch resolution is not a valid commit-pinned Skill path.",
                phase=DiagnosticPhase.FETCH,
            )
        owner, repo = repository.split("/", 1)
        if not _valid_repository(owner, repo):
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_FETCH_RESOLUTION_INVALID",
                "GitHub fetch resolution contains an invalid repository identity.",
                phase=DiagnosticPhase.FETCH,
            )
        ref = _GitHubSkillRef(owner, repo, resolution.revision.lower(), safe_skill_path)

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                tree_data = await _fetch_tree_payload(
                    client,
                    _github_tree_url(ref, ref.ref, recursive=True),
                    headers=self._headers(),
                )
                tree_path_prefix = ""
                if tree_data.get("truncated"):
                    log.warning(
                        "github.fetch_tree_truncated",
                        identifier=resolution.canonical_identifier,
                    )
                    if not ref.skill_dir:
                        raise SkillSourceFetchError.diagnostic(
                            "SOURCE_TREE_TRUNCATED",
                            (
                                "GitHub truncated the repository tree; "
                                "the Skill cannot be fetched safely."
                            ),
                            phase=DiagnosticPhase.FETCH,
                            hint="Install from an explicit Skill subpath.",
                        )
                    tree_data = await _fetch_explicit_subtree(
                        client,
                        ref,
                        headers=self._headers(),
                    )
                    tree_path_prefix = ref.skill_dir
                    if tree_data.get("truncated"):
                        raise SkillSourceFetchError.diagnostic(
                            "SOURCE_TREE_TRUNCATED",
                            "GitHub truncated the selected Skill directory.",
                            phase=DiagnosticPhase.FETCH,
                            hint="Reduce the number of files in the selected Skill directory.",
                        )

                files: dict[str, str | bytes] = {}
                selected: list[tuple[str, str, int, int]] = []
                declared_total = 0
                missing_modes = False
                for item in tree_data["tree"]:
                    if not isinstance(item, dict):
                        raise source_invalid_response_error(
                            phase=DiagnosticPhase.FETCH,
                            source_name="GitHub",
                        )
                    path = str(item.get("path") or "")
                    if not tree_path_prefix and ref.skill_dir:
                        selected_parts = PurePosixPath(ref.skill_dir).parts
                        candidate_parts = tuple(path.split("/"))
                        if candidate_parts[: len(selected_parts)] != selected_parts:
                            continue
                    try:
                        relative_tree_path = normalize_relative_path(path).as_posix()
                        safe_path = (
                            normalize_relative_path(
                                f"{tree_path_prefix}/{relative_tree_path}"
                            ).as_posix()
                            if tree_path_prefix
                            else relative_tree_path
                        )
                    except ArchiveNormalizationError:
                        log.warning("github.fetch_unsafe_tree_path", path=path)
                        raise SkillSourceFetchError.diagnostic(
                            "ARTIFACT_PATH_UNSAFE",
                            f"GitHub returned an unsafe tree path: {path}",
                            phase=DiagnosticPhase.SECURITY,
                            path=path,
                        ) from None
                    rel_path = _relative_to_skill_dir(safe_path, ref.skill_dir)
                    selected_root_entry = bool(
                        ref.skill_dir and safe_path == ref.skill_dir
                    )
                    if rel_path is None and not selected_root_entry:
                        continue
                    entry_type = str(item.get("type") or "")
                    if entry_type == "tree":
                        continue
                    if entry_type != "blob":
                        log.warning(
                            "github.fetch_unsupported_tree_entry",
                            path=safe_path,
                            entry_type=entry_type,
                        )
                        raise SkillSourceFetchError.diagnostic(
                            "ARTIFACT_FILE_TYPE_UNSUPPORTED",
                            f"GitHub Skill contains a submodule or unsupported entry: {safe_path}",
                            phase=DiagnosticPhase.SECURITY,
                            path=safe_path,
                        )
                    if not rel_path:
                        continue
                    relative = PurePosixPath(rel_path)
                    if (
                        len(relative.parts) > DEFAULT_ARCHIVE_LIMITS.max_depth
                        or any(
                            part.casefold() in _RESERVED_COMPONENTS
                            for part in relative.parts
                        )
                    ):
                        log.warning("github.fetch_unsafe_skill_path", path=safe_path)
                        raise SkillSourceFetchError.diagnostic(
                            "ARTIFACT_PATH_UNSAFE",
                            f"GitHub Skill path is unsafe or exceeds the depth limit: {safe_path}",
                            phase=DiagnosticPhase.SECURITY,
                            path=safe_path,
                        )
                    try:
                        declared_size = max(0, int(item.get("size") or 0))
                    except (TypeError, ValueError):
                        declared_size = 0
                    if declared_size > DEFAULT_ARCHIVE_LIMITS.max_entry_bytes:
                        log.warning("github.fetch_entry_too_large", path=safe_path)
                        raise SkillSourceFetchError.diagnostic(
                            "FETCH_SIZE_LIMIT",
                            f"GitHub Skill file exceeds the 50 MiB entry limit: {safe_path}",
                            phase=DiagnosticPhase.FETCH,
                            path=safe_path,
                        )
                    raw_mode = str(item.get("mode") or "")
                    file_mode = 0
                    if raw_mode:
                        try:
                            parsed_mode = int(raw_mode, 8)
                        except ValueError:
                            log.warning("github.fetch_invalid_file_mode", path=safe_path)
                            raise SkillSourceFetchError.diagnostic(
                                "ARTIFACT_FILE_TYPE_UNSUPPORTED",
                                f"GitHub returned invalid file mode metadata: {safe_path}",
                                phase=DiagnosticPhase.SECURITY,
                                path=safe_path,
                            ) from None
                        if stat.S_IFMT(parsed_mode) != stat.S_IFREG:
                            log.warning("github.fetch_unsupported_file_type", path=safe_path)
                            raise SkillSourceFetchError.diagnostic(
                                "ARTIFACT_FILE_TYPE_UNSUPPORTED",
                                f"GitHub Skill contains a link or special file: {safe_path}",
                                phase=DiagnosticPhase.SECURITY,
                                path=safe_path,
                            )
                        file_mode = stat.S_IMODE(parsed_mode)
                    else:
                        missing_modes = True
                    declared_total += declared_size
                    if declared_total > DEFAULT_ARCHIVE_LIMITS.max_expanded_bytes:
                        log.warning(
                            "github.fetch_tree_too_large",
                            identifier=resolution.canonical_identifier,
                        )
                        raise SkillSourceFetchError.diagnostic(
                            "FETCH_SIZE_LIMIT",
                            "GitHub Skill exceeds the 50 MiB expanded-size limit.",
                            phase=DiagnosticPhase.FETCH,
                        )
                    selected.append((safe_path, rel_path, declared_size, file_mode))

                try:
                    validate_portable_file_paths(item[1] for item in selected)
                except ArchiveNormalizationError as exc:
                    log.warning("github.fetch_colliding_tree_path", error=str(exc))
                    raise SkillSourceFetchError.diagnostic(
                        "ARTIFACT_PATH_COLLISION",
                        f"GitHub Skill contains colliding portable paths: {exc}",
                        phase=DiagnosticPhase.SECURITY,
                    ) from None

                if len(selected) > DEFAULT_ARCHIVE_LIMITS.max_entries:
                    log.warning(
                        "github.fetch_too_many_files",
                        identifier=resolution.canonical_identifier,
                    )
                    raise SkillSourceFetchError.diagnostic(
                        "FETCH_ENTRY_LIMIT",
                        "GitHub Skill contains more than 2048 files.",
                        phase=DiagnosticPhase.FETCH,
                    )

                actual_total = 0
                file_modes: dict[str, int] = {}
                for path, rel_path, _declared_size, file_mode in selected:
                    raw_url = (
                        f"https://raw.githubusercontent.com/{ref.repo_full}/"
                        f"{quote(ref.ref, safe='')}/{quote(path, safe='/')}"
                    )
                    remaining = DEFAULT_ARCHIVE_LIMITS.max_expanded_bytes - actual_total
                    content = await _read_bounded_blob(
                        client,
                        raw_url,
                        headers=self._headers(),
                        aggregate_remaining=remaining,
                    )
                    actual_total += len(content)
                    files[rel_path] = _decode_file(rel_path, content)
                    if file_mode:
                        file_modes[rel_path] = file_mode
        except SkillSourceFetchError:
            raise
        except Exception as exc:
            log.warning(
                "github.fetch_failed",
                identifier=resolution.canonical_identifier,
                error=str(exc),
            )
            if isinstance(exc, ValueError) and "exceeds" in str(exc).lower():
                raise SkillSourceFetchError.diagnostic(
                    "FETCH_SIZE_LIMIT",
                    str(exc),
                    phase=DiagnosticPhase.FETCH,
                ) from exc
            raise source_transport_error(
                exc,
                phase=DiagnosticPhase.FETCH,
                source_name="GitHub",
            ) from exc

        accepted_manifest_names = (
            {_MANIFEST_NAME, "skill.md", "skills.md"}
            if resolution.allow_legacy_manifest_names
            else {_MANIFEST_NAME}
        )
        manifest_paths = [
            path
            for path in files
            if PurePosixPath(path).name in accepted_manifest_names
        ]
        if (
            len(manifest_paths) != 1
            or PurePosixPath(manifest_paths[0]).parent != PurePosixPath(".")
        ):
            log.warning(
                "github.fetch_ambiguous_manifest",
                identifier=resolution.canonical_identifier,
                manifests=manifest_paths,
            )
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_TREE_AMBIGUOUS",
                "GitHub selection must contain exactly one root Skill manifest.",
                phase=DiagnosticPhase.ARCHIVE,
                details={"manifests": manifest_paths},
                hint="Use an explicit repository subpath containing one Skill.",
            )
        skill_md = files[manifest_paths[0]]
        if not isinstance(skill_md, str):
            raise SkillSourceFetchError.diagnostic(
                "MANIFEST_ENCODING_INVALID",
                "The GitHub Skill manifest is not valid UTF-8 text.",
                phase=DiagnosticPhase.MANIFEST,
                path=manifest_paths[0],
            )

        name = _frontmatter_field(skill_md, "name") or _fallback_name(ref)
        meta = SkillMeta(
            name=name,
            description=_frontmatter_field(skill_md, "description"),
            version=resolution.revision,
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=resolution.canonical_identifier,
            homepage=ref.homepage,
            canonical_identifier=resolution.canonical_identifier,
        )
        actual_digest = _bundle_digest(files)
        if resolution.expected_digest and resolution.expected_digest.lower() not in {
            actual_digest,
            f"sha256:{actual_digest}",
        }:
            log.warning(
                "github.fetch_digest_mismatch",
                identifier=resolution.canonical_identifier,
            )
            raise SkillSourceFetchError.diagnostic(
                "ARTIFACT_DIGEST_MISMATCH",
                "The GitHub Skill content digest does not match the immutable resolution.",
                phase=DiagnosticPhase.SECURITY,
            )
        resolved = replace(resolution, expected_digest=actual_digest)
        if missing_modes and not any(
            diagnostic.code == "FILE_MODE_UNAVAILABLE"
            for diagnostic in resolved.diagnostics
        ):
            resolved = replace(
                resolved,
                diagnostics=(
                    *resolved.diagnostics,
                    SkillDiagnostic(
                        code="FILE_MODE_UNAVAILABLE",
                        severity=DiagnosticSeverity.WARNING,
                        phase=DiagnosticPhase.FETCH,
                        message="GitHub did not return POSIX mode metadata for every file.",
                    ),
                ),
            )
        return SkillBundle(
            name=name,
            files=files,
            meta=meta,
            resolution=resolved,
            file_modes=file_modes,
        )

    async def inspect(self, identifier: str) -> SkillMeta | None:
        ref = _parse_identifier(identifier)
        if ref is None:
            return None
        return SkillMeta(
            name=_fallback_name(ref),
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=ref.canonical_identifier,
            homepage=ref.homepage,
            canonical_identifier=ref.canonical_identifier,
        )
