from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from typing import Any

import pytest

from opensquilla.skills.hub.clawhub import ClawHubSource, _extract_skill_zip
from opensquilla.skills.hub.installer import SkillInstaller
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillFetchError


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _skill_zip(slug: str = "foo", layout: str = "root") -> bytes:
    if layout == "root":
        return _zip_bytes(
            {
                "SKILL.md": b"---\nname: foo\n---\n# Foo\n",
                "scripts/run.py": b"print('foo')\n",
            }
        )
    if layout == "skills-sh":
        return _zip_bytes(
            {
                f"{slug}-main/skills/{slug}/SKILL.md": b"---\nname: foo\n---\n# Foo\n",
                f"{slug}-main/skills/{slug}/notes.md": b"details\n",
                f"{slug}-main/skills/other/SKILL.md": b"---\nname: other\n---\n",
                f"{slug}-main/README.md": b"repo readme\n",
            }
        )
    raise AssertionError(f"unknown layout: {layout}")


def _search_item(
    slug: str,
    owner_handle: str = "",
    source: str = "clawhub",
    identity_owner: str = "",
    identity_repo: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "displayName": slug,
        "slug": slug,
        "ownerHandle": owner_handle,
        "source": source,
        "summary": f"Summary of {slug}",
    }
    if identity_owner and identity_repo:
        item["sourceIdentity"] = {"owner": identity_owner, "repo": identity_repo}
    return item


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"",
        json_data: Any = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.content = content
        self._json_data = json_data
        self.text = text if text else content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Routes GET requests by URL and query params to canned responses.

    Routes live on the class (like the existing hub test doubles) so the
    adapter's own ``httpx.AsyncClient(...)`` construction picks them up.
    """

    routes: list[tuple[Callable, int, Any]] = []
    requests: list[tuple[str, dict[str, str]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(
        self, url: str, params: dict[str, str] | None = None, **kwargs: Any
    ) -> _FakeResponse:
        params = params or {}
        self.requests.append((url, dict(params)))
        for match, status, payload in self.routes:
            if match(url, params):
                if isinstance(payload, bytes):
                    return _FakeResponse(status, content=payload)
                return _FakeResponse(status, json_data=payload)
        raise AssertionError(f"unhandled request: {url} params={params}")


def _download_route(
    slug: str, *, owner_handle: str | None = None, status: int = 200, payload: Any = b""
) -> tuple[Callable, int, Any]:
    def match(url: str, params: dict[str, str]) -> bool:
        if not url.endswith("/api/v1/download"):
            return False
        if params.get("slug") != slug:
            return False
        if owner_handle is not None:
            return params.get("ownerHandle") == owner_handle
        return "ownerHandle" not in params
    return match, status, payload


def _search_route(
    query: str, items: list[dict[str, Any]], status: int = 200
) -> tuple[Callable, int, Any]:
    def match(url: str, params: dict[str, str]) -> bool:
        return url.endswith("/api/v1/search") and params.get("q") == query
    return match, status, {"results": items}


def _github_route(
    repo_full: str, *, branch: str, status: int = 200, payload: Any = b""
) -> tuple[Callable, int, Any]:
    url = f"https://github.com/{repo_full}/archive/refs/heads/{branch}.zip"

    def match(url_: str, params: dict[str, str]) -> bool:
        return url_ == url
    return match, status, payload


@pytest.mark.asyncio
async def test_fetch_native_zip_root_skill_md(monkeypatch) -> None:
    import httpx

    _FakeClient.routes = [
        _download_route("foo", status=200, payload=_skill_zip("foo", layout="root")),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    bundle = await ClawHubSource().fetch("foo")

    assert bundle is not None
    assert bundle.name == "foo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py"}
    assert bundle.files["scripts/run.py"] == "print('foo')\n"
    # exactly one download attempt, no ownerHandle
    assert len([r for r in _FakeClient.requests if "download" in r[0]]) == 1
    assert _FakeClient.requests[0][1] == {"slug": "foo"}


@pytest.mark.asyncio
async def test_fetch_ambiguous_slug_resolves_owner_handle(monkeypatch) -> None:
    import httpx

    _FakeClient.routes = [
        _download_route(
            "agent",
            status=409,
            payload=b'Ambiguous skill slug "agent". Multiple publishers use this slug.',
        ),
        _search_route("agent", [_search_item("agent", owner_handle="ivangdavila")]),
        _download_route(
            "agent",
            owner_handle="ivangdavila",
            status=200,
            payload=_skill_zip("agent", layout="root"),
        ),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    bundle = await ClawHubSource().fetch("agent")

    assert bundle is not None
    assert bundle.name == "agent"
    downloads = [r for r in _FakeClient.requests if "download" in r[0]]
    assert len(downloads) == 2
    assert downloads[1][1] == {"slug": "agent", "ownerHandle": "ivangdavila"}


@pytest.mark.asyncio
async def test_fetch_skills_sh_entry_falls_back_to_github_archive(monkeypatch) -> None:
    import httpx

    slug = "make-interfaces-feel-better"
    _FakeClient.routes = [
        _download_route(slug, status=404, payload=b"Skill not found"),
        _search_route(
            slug,
            [
                _search_item(
                    slug,
                    owner_handle="jakubkrehel",
                    source="skills-sh",
                    identity_owner="jakubkrehel",
                    identity_repo=slug,
                )
            ],
        ),
        _github_route("jakubkrehel/" + slug, branch="main", status=404, payload=b""),
        _github_route(
            "jakubkrehel/" + slug,
            branch="master",
            status=200,
            payload=_skill_zip(slug, layout="skills-sh"),
        ),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    bundle = await ClawHubSource().fetch(slug)

    assert bundle is not None
    assert bundle.name == slug
    # only the skill directory contents, with the wrapper and sibling skills
    # stripped away.
    assert set(bundle.files) == {"SKILL.md", "notes.md"}
    github_hits = [r for r in _FakeClient.requests if "github.com" in r[0]]
    assert len(github_hits) == 2
    assert github_hits[0][0].endswith("/main.zip")
    assert github_hits[1][0].endswith("/master.zip")


@pytest.mark.asyncio
async def test_fetch_rate_limited_raises_actionable_error(monkeypatch) -> None:
    import httpx

    _FakeClient.routes = [
        _download_route("foo", status=429, payload=b"rate limit exceeded"),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    with pytest.raises(SkillFetchError, match="rate-limited"):
        await ClawHubSource().fetch("foo")


@pytest.mark.asyncio
async def test_fetch_not_found_without_fallback_raises(monkeypatch) -> None:
    import httpx

    _FakeClient.routes = [
        _download_route("ghost", status=404, payload=b"Skill not found"),
        _search_route("ghost", []),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    with pytest.raises(SkillFetchError, match="was not found"):
        await ClawHubSource().fetch("ghost")


@pytest.mark.asyncio
async def test_installer_surfaces_fetch_error_reason(monkeypatch, tmp_path) -> None:
    import httpx

    _FakeClient.routes = [
        _download_route("foo", status=429, payload=b"rate limit exceeded"),
    ]
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    installer = SkillInstaller(
        router=SourceRouter([ClawHubSource()]),
        managed_dir=tmp_path / "managed",
        quarantine_dir=tmp_path / "quarantine",
        lockfile_path=tmp_path / "lock.json",
    )

    result = await installer.install("foo", "clawhub")

    assert result.success is False
    assert "rate-limited" in result.message


def test_extract_skill_zip_prefers_matching_subdirectory() -> None:
    slug = "target"
    content = _skill_zip(slug, layout="skills-sh")
    files = _extract_skill_zip(content, preferred_name=slug)
    assert files is not None
    assert set(files) == {"SKILL.md", "notes.md"}


def test_extract_skill_zip_rejects_zip_slip_paths() -> None:
    content = _zip_bytes(
        {
            "SKILL.md": b"---\nname: x\n---\n",
            "../evil.txt": b"nope",
        }
    )
    files = _extract_skill_zip(content, preferred_name=None)
    assert files is not None
    assert "evil.txt" not in files
    assert "../evil.txt" not in files
