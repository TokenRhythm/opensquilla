from __future__ import annotations

from typing import Any

import pytest
import structlog.testing

from opensquilla.skills.hub.github import GitHubSource


class _Response:
    def __init__(
        self,
        *,
        json_data: dict[str, Any] | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data or {}
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _AsyncClient:
    tree_entries = [
        {"path": "skills/demo/SKILL.md", "type": "blob"},
        {"path": "skills/demo/scripts/run.py", "type": "blob"},
        {"path": "skills/demo/assets/logo.bin", "type": "blob"},
        {"path": "skills/other/SKILL.md", "type": "blob"},
    ]
    raw_payloads = {
        "skills/demo/SKILL.md": b"---\nname: demo\ndescription: Demo skill.\n---\n\n# Demo\n",
        "skills/demo/scripts/run.py": b"print('demo')\n",
        "skills/demo/assets/logo.bin": b"\x00\xff",
    }
    requests: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append((url, kwargs))
        if "/git/trees/" in url:
            return _Response(json_data={"tree": self.tree_entries, "truncated": False})
        marker = "raw.githubusercontent.com/acme/skillpack/main/"
        if marker in url:
            rel_path = url.split(marker, 1)[1]
            return _Response(content=self.raw_payloads[rel_path])
        raise AssertionError(f"unexpected URL: {url}")


@pytest.mark.asyncio
async def test_fetch_github_tree_url_downloads_whole_skill_directory(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch("https://github.com/acme/skillpack/tree/main/skills/demo")

    assert bundle is not None
    assert bundle.name == "demo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py", "assets/logo.bin"}
    assert bundle.files["scripts/run.py"] == "print('demo')\n"
    assert bundle.files["assets/logo.bin"] == b"\x00\xff"
    assert bundle.meta is not None
    assert bundle.meta.source_id == "github"
    assert bundle.meta.identifier == "acme/skillpack@main:skills/demo/SKILL.md"


@pytest.mark.asyncio
async def test_fetch_github_blob_url_uses_parent_skill_directory(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch(
        "https://github.com/acme/skillpack/blob/main/skills/demo/SKILL.md"
    )

    assert bundle is not None
    assert bundle.name == "demo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py", "assets/logo.bin"}


@pytest.mark.asyncio
async def test_fetch_legacy_identifier_keeps_support_and_downloads_directory(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch("acme/skillpack@main:skills/demo/SKILL.md")

    assert bundle is not None
    assert bundle.name == "demo"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py", "assets/logo.bin"}


class _ExplodingClient(_AsyncClient):
    """Any HTTP call at all is the failure this guards against."""

    async def get(self, url: str, **kwargs: Any) -> _Response:
        raise AssertionError(f"unauthenticated search must not reach the network: {url}")


@pytest.mark.asyncio
async def test_search_without_a_token_makes_no_request_and_logs_no_warning(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _ExplodingClient)

    with structlog.testing.capture_logs() as captured:
        results = await GitHubSource().search("pdf")

    assert results == []
    assert [entry for entry in captured if entry["log_level"] == "warning"] == []
    assert [entry["event"] for entry in captured] == ["github.search_skipped_unauthenticated"]


@pytest.mark.asyncio
async def test_search_with_a_token_still_queries_code_search(monkeypatch) -> None:
    import httpx

    seen: list[tuple[str, dict[str, Any]]] = []

    class _SearchClient(_AsyncClient):
        async def get(self, url: str, **kwargs: Any) -> _Response:
            seen.append((url, kwargs))
            return _Response(
                json_data={
                    "items": [
                        {
                            "path": "skills/demo/SKILL.md",
                            "repository": {
                                "full_name": "acme/skillpack",
                                "description": "Demo skill.",
                                "html_url": "https://github.com/acme/skillpack",
                            },
                        }
                    ]
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", _SearchClient)

    results = await GitHubSource(token="t0ken").search("pdf")

    assert [meta.name for meta in results] == ["demo"]
    assert len(seen) == 1
    url, kwargs = seen[0]
    assert url == "https://api.github.com/search/code"
    assert kwargs["headers"]["Authorization"] == "token t0ken"


@pytest.mark.asyncio
async def test_search_with_a_token_still_warns_when_the_request_fails(monkeypatch) -> None:
    import httpx

    class _UnauthorizedClient(_AsyncClient):
        async def get(self, url: str, **kwargs: Any) -> _Response:
            return _Response(status_code=401)

    monkeypatch.setattr(httpx, "AsyncClient", _UnauthorizedClient)

    with structlog.testing.capture_logs() as captured:
        results = await GitHubSource(token="expired").search("pdf")

    assert results == []
    assert [
        entry["event"] for entry in captured if entry["log_level"] == "warning"
    ] == ["github.search_failed"]


@pytest.mark.asyncio
async def test_fetch_stays_available_without_a_token(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    _AsyncClient.requests = []

    bundle = await GitHubSource().fetch("https://github.com/acme/skillpack/tree/main/skills/demo")

    assert bundle is not None
    assert bundle.name == "demo"


def test_default_gateway_router_exposes_github_without_token(monkeypatch) -> None:
    import opensquilla.gateway.rpc_skills as rpc_skills
    from opensquilla.skills.hub import defaults

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    defaults._default_router = None

    try:
        router = rpc_skills._get_default_router()
        assert "github" in router.source_ids
    finally:
        defaults._default_router = None
