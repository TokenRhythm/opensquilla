"""GitHub skill source — searches and installs SKILL.md directories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.skills.hub.source import SkillBundle, SkillMeta, SkillSource

log = structlog.get_logger(__name__)

_GITHUB_HOSTS = {"github.com", "www.github.com"}
_RAW_GITHUB_HOST = "raw.githubusercontent.com"
_REPO_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?:@(?P<ref>[^:]+))?(?::(?P<path>.+))?$"
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
        if path.endswith("/SKILL.md"):
            return path.rsplit("/", 1)[0]
        if path == "SKILL.md":
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


def _normalize_skill_path(path: str) -> str:
    return "/".join(part for part in path.replace("\\", "/").split("/") if part)


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
            ref = "HEAD"
            skill_path = ""
            if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
                ref = parts[3]
                skill_path = "/".join(parts[4:])
            return _GitHubSkillRef(owner, repo, ref, _normalize_skill_path(skill_path))

        if host == _RAW_GITHUB_HOST:
            if len(parts) < 4:
                return None
            owner, repo = parts[0], _clean_repo_name(parts[1])
            ref = parts[2]
            skill_path = "/".join(parts[3:])
            return _GitHubSkillRef(owner, repo, ref, _normalize_skill_path(skill_path))

        return None

    match = _REPO_RE.match(raw)
    if match is None:
        return None
    return _GitHubSkillRef(
        match.group("owner"),
        _clean_repo_name(match.group("repo")),
        match.group("ref") or "HEAD",
        _normalize_skill_path(match.group("path") or ""),
    )


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
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            log.warning("github.search_failed", error=str(exc))
            return []

        results = []
        for item in data.get("items", []):
            repo = item.get("repository", {})
            full_name = repo.get("full_name", "")
            path = item.get("path", "")
            # Extract the skill name from the parent directory of a SKILL.md path.
            parts = path.rsplit("/", 2)
            skill_name = parts[-2] if len(parts) >= 2 else full_name

            results.append(
                SkillMeta(
                    name=skill_name,
                    description=repo.get("description", ""),
                    source_id=self.source_id,
                    trust_level=self.trust_level,
                    identifier=f"{full_name}:{path}",
                    homepage=repo.get("html_url", ""),
                )
            )
        return results[:limit]

    async def fetch(self, identifier: str) -> SkillBundle | None:
        import httpx

        ref = _parse_identifier(identifier)
        if ref is None:
            return None

        try:
            async with httpx.AsyncClient(timeout=15, trust_env=_trust_env()) as client:
                tree_url = (
                    f"https://api.github.com/repos/{ref.repo_full}/git/trees/"
                    f"{quote(ref.ref, safe='')}?recursive=1"
                )
                tree_resp = await client.get(tree_url, headers=self._headers())
                tree_resp.raise_for_status()
                tree_data = tree_resp.json()
                if tree_data.get("truncated"):
                    log.warning("github.fetch_tree_truncated", identifier=identifier)
                    return None

                files: dict[str, str | bytes] = {}
                for item in tree_data.get("tree", []):
                    path = str(item.get("path") or "")
                    if item.get("type") != "blob":
                        continue
                    rel_path = _relative_to_skill_dir(path, ref.skill_dir)
                    if not rel_path:
                        continue
                    raw_url = (
                        f"https://raw.githubusercontent.com/{ref.repo_full}/"
                        f"{quote(ref.ref, safe='')}/{quote(path, safe='/')}"
                    )
                    raw_resp = await client.get(raw_url, headers=self._headers())
                    raw_resp.raise_for_status()
                    files[rel_path] = _decode_file(rel_path, raw_resp.content)
        except Exception as exc:
            log.warning("github.fetch_failed", identifier=identifier, error=str(exc))
            return None

        skill_md = files.get("SKILL.md")
        if not isinstance(skill_md, str):
            return None

        name = _frontmatter_field(skill_md, "name") or _fallback_name(ref)
        meta = SkillMeta(
            name=name,
            description=_frontmatter_field(skill_md, "description"),
            source_id=self.source_id,
            trust_level=self.trust_level,
            identifier=ref.canonical_identifier,
            homepage=ref.homepage,
        )
        return SkillBundle(name=name, files=files, meta=meta)

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
        )
