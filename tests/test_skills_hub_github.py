from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opensquilla.skills.hub.archive import DEFAULT_ARCHIVE_LIMITS
from opensquilla.skills.hub.github import GitHubSource
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillSourceFetchError

_COMMIT = "a" * 40


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


class _InvalidJsonResponse(_Response):
    def json(self) -> dict[str, Any]:
        raise ValueError("invalid JSON")


class _AsyncClient:
    tree_entries = [
        {"path": "skills/demo/SKILL.md", "type": "blob", "mode": "100644"},
        {"path": "skills/demo/scripts/run.py", "type": "blob", "mode": "100755"},
        {"path": "skills/demo/assets/logo.bin", "type": "blob", "mode": "100644"},
        {"path": "skills/other/SKILL.md", "type": "blob", "mode": "100644"},
        {"path": "unrelated/bad?.txt", "type": "blob", "mode": "100644"},
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
        if "/commits/" in url:
            return _Response(json_data={"sha": _COMMIT})
        if "/git/trees/" in url:
            return _Response(json_data={"tree": self.tree_entries, "truncated": False})
        marker = f"raw.githubusercontent.com/acme/skillpack/{_COMMIT}/"
        if marker in url:
            rel_path = url.split(marker, 1)[1]
            return _Response(content=self.raw_payloads[rel_path])
        raise AssertionError(f"unexpected URL: {url}")


@pytest.mark.asyncio
async def test_search_returns_exact_github_skill_references(monkeypatch) -> None:
    import httpx

    async def search_get(
        _client: _AsyncClient,
        _url: str,
        **_kwargs: Any,
    ) -> _Response:
        return _Response(
            json_data={
                "items": [
                    {
                        "path": "skills/demo/SKILL.md",
                        "repository": {
                            "full_name": "acme/skillpack",
                            "description": "Demo Skill",
                            "html_url": "https://github.com/acme/skillpack",
                        },
                    }
                ]
            }
        )

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(_AsyncClient, "get", search_get)

    results = await GitHubSource().search("demo")

    assert [(row.name, row.canonical_identifier) for row in results] == [
        ("demo", "acme/skillpack:skills/demo/SKILL.md")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        (429, "SOURCE_RATE_LIMITED"),
        ("transport", "SOURCE_TRANSPORT_FAILED"),
        ("invalid-json", "SOURCE_INVALID_RESPONSE"),
        ("all-malformed", "SOURCE_INVALID_RESPONSE"),
    ],
)
async def test_search_failures_surface_stable_diagnostics(
    monkeypatch,
    outcome: int | str,
    expected_code: str,
) -> None:
    import httpx

    async def search_get(
        _client: _AsyncClient,
        _url: str,
        **_kwargs: Any,
    ) -> _Response:
        if outcome == "transport":
            raise OSError("simulated DNS failure")
        if outcome == "invalid-json":
            return _InvalidJsonResponse()
        if outcome == "all-malformed":
            return _Response(json_data={"items": [{"path": "SKILL.md"}]})
        assert isinstance(outcome, int)
        return _Response(status_code=outcome)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(_AsyncClient, "get", search_get)

    with pytest.raises(SkillSourceFetchError) as raised:
        await GitHubSource().search("demo")

    assert [item.code for item in raised.value.diagnostics] == [expected_code]


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
    assert bundle.file_modes["scripts/run.py"] == 0o755
    assert bundle.meta is not None
    assert bundle.meta.source_id == "github"
    assert bundle.meta.identifier == f"acme/skillpack@{_COMMIT}:skills/demo/SKILL.md"
    assert bundle.resolution is not None
    assert bundle.resolution.immutable is True
    assert bundle.resolution.revision == _COMMIT
    assert bundle.resolution.expected_digest
    assert bundle.resolution.trust_state == "community"
    assert all("/main/" not in url for url, _kwargs in _AsyncClient.requests)


@pytest.mark.asyncio
async def test_explicit_subpath_recovers_from_truncated_repository_tree(monkeypatch) -> None:
    import httpx

    skills_tree = "b" * 40
    demo_tree = "c" * 40

    class _TruncatedRepositoryClient(_AsyncClient):
        async def get(self, url: str, **kwargs: Any) -> _Response:
            type(self).requests.append((url, kwargs))
            if f"/git/trees/{_COMMIT}?recursive=1" in url:
                return _Response(json_data={"tree": [], "truncated": True})
            if url.endswith(f"/git/trees/{_COMMIT}"):
                return _Response(
                    json_data={
                        "tree": [
                            {"path": "skills", "type": "tree", "sha": skills_tree}
                        ],
                        "truncated": False,
                    }
                )
            if url.endswith(f"/git/trees/{skills_tree}"):
                return _Response(
                    json_data={
                        "tree": [
                            {"path": "demo", "type": "tree", "sha": demo_tree}
                        ],
                        "truncated": False,
                    }
                )
            if url.endswith(f"/git/trees/{demo_tree}?recursive=1"):
                return _Response(
                    json_data={
                        "tree": [
                            {
                                "path": "SKILL.md",
                                "type": "blob",
                                "mode": "100644",
                            },
                            {
                                "path": "scripts/run.py",
                                "type": "blob",
                                "mode": "100755",
                            },
                        ],
                        "truncated": False,
                    }
                )
            marker = f"raw.githubusercontent.com/acme/skillpack/{_COMMIT}/"
            if marker in url:
                rel_path = url.split(marker, 1)[1]
                return _Response(content=self.raw_payloads[rel_path])
            raise AssertionError(f"unexpected URL: {url}")

    _TruncatedRepositoryClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _TruncatedRepositoryClient)

    bundle = await GitHubSource().fetch(
        f"acme/skillpack@{_COMMIT}:skills/demo/SKILL.md"
    )

    assert bundle is not None
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py"}
    assert any(
        url.endswith(f"/git/trees/{demo_tree}?recursive=1")
        for url, _kwargs in _TruncatedRepositoryClient.requests
    )
    assert all(
        "skills/other" not in url
        for url, _kwargs in _TruncatedRepositoryClient.requests
    )


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


@pytest.mark.asyncio
async def test_full_commit_identifier_does_not_resolve_a_mutable_ref(monkeypatch) -> None:
    import httpx

    _AsyncClient.requests = []
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    bundle = await GitHubSource().fetch(f"acme/skillpack@{_COMMIT}:skills/demo")

    assert bundle is not None
    assert not any("/commits/" in url for url, _kwargs in _AsyncClient.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        (403, "SOURCE_AUTH_FAILED"),
        (429, "SOURCE_RATE_LIMITED"),
        (503, "SOURCE_SERVER_FAILED"),
        (404, "SOURCE_NOT_FOUND"),
        ("transport", "SOURCE_TRANSPORT_FAILED"),
        ("invalid-json", "SOURCE_INVALID_RESPONSE"),
    ],
)
async def test_github_resolution_failures_keep_source_diagnostics(
    monkeypatch,
    tmp_path: Path,
    outcome: int | str,
    expected_code: str,
) -> None:
    import httpx

    async def failing_get(
        _client: _AsyncClient,
        _url: str,
        **_kwargs: Any,
    ) -> _Response:
        if outcome == "transport":
            raise OSError("simulated DNS failure")
        if outcome == "invalid-json":
            return _InvalidJsonResponse()
        assert isinstance(outcome, int)
        return _Response(status_code=outcome)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(_AsyncClient, "get", failing_get)
    service = SkillManagementService(
        router=SourceRouter([GitHubSource()]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install("acme/skillpack@main:skills/demo", "github")

    assert result.success is False
    assert [(item.code, item.phase.value) for item in result.diagnostics] == [
        (expected_code, "source")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        (403, "FETCH_AUTH_FAILED"),
        (429, "FETCH_RATE_LIMITED"),
        (503, "FETCH_SERVER_FAILED"),
        (404, "FETCH_NOT_FOUND"),
        ("transport", "FETCH_TRANSPORT_FAILED"),
        ("invalid-json", "FETCH_INVALID_RESPONSE"),
    ],
)
async def test_github_fetch_failures_keep_fetch_diagnostics(
    monkeypatch,
    tmp_path: Path,
    outcome: int | str,
    expected_code: str,
) -> None:
    import httpx

    async def failing_get(
        _client: _AsyncClient,
        _url: str,
        **_kwargs: Any,
    ) -> _Response:
        if outcome == "transport":
            raise OSError("simulated DNS failure")
        if outcome == "invalid-json":
            return _InvalidJsonResponse()
        assert isinstance(outcome, int)
        return _Response(status_code=outcome)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(_AsyncClient, "get", failing_get)
    service = SkillManagementService(
        router=SourceRouter([GitHubSource()]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install(
        f"acme/skillpack@{_COMMIT}:skills/demo",
        "github",
    )

    assert result.success is False
    assert [(item.code, item.phase.value) for item in result.diagnostics] == [
        (expected_code, "fetch")
    ]


@pytest.mark.asyncio
async def test_repository_root_reports_ambiguous_tree_to_management(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        _AsyncClient,
        "tree_entries",
            [
                {"path": "SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "nested/SKILL.md", "type": "blob", "mode": "100644"},
        ],
    )
    monkeypatch.setattr(
        _AsyncClient,
        "raw_payloads",
        {
            "SKILL.md": b"---\nname: demo\ndescription: Demo.\n---\nBody.\n",
            "nested/SKILL.md": b"---\nname: nested\ndescription: Nested.\n---\nBody.\n",
        },
    )

    source = GitHubSource()
    service = SkillManagementService(
        router=SourceRouter([source]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install(f"acme/skillpack@{_COMMIT}", "github")

    assert result.success is False
    assert [(item.code, item.phase.value) for item in result.diagnostics] == [
        ("SOURCE_TREE_AMBIGUOUS", "archive")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tree_entries", "expected_code", "expected_phase"),
    [
        (
            [
                {"path": "SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "skill.md", "type": "blob", "mode": "100644"},
            ],
            "ARTIFACT_PATH_COLLISION",
            "security",
        ),
        (
            [
                {"path": "SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "Refs/a.txt", "type": "blob", "mode": "100644"},
                {"path": "refs/b.txt", "type": "blob", "mode": "100644"},
            ],
            "ARTIFACT_PATH_COLLISION",
            "security",
        ),
        (
            [
                {"path": "SKILL.md", "type": "blob", "mode": "100644"},
                {"path": "refs/bad?.txt", "type": "blob", "mode": "100644"},
            ],
            "ARTIFACT_PATH_UNSAFE",
            "security",
        ),
        (
            [
                {"path": "SKILL.md", "type": "blob", "mode": "100644"},
                {
                    "path": "vendor/community-helper",
                    "type": "commit",
                    "mode": "160000",
                },
            ],
            "ARTIFACT_FILE_TYPE_UNSUPPORTED",
            "security",
        ),
        (
            [
                {
                    "path": "SKILL.md",
                    "type": "blob",
                    "mode": "100644",
                    "size": DEFAULT_ARCHIVE_LIMITS.max_entry_bytes + 1,
                }
            ],
            "FETCH_SIZE_LIMIT",
            "fetch",
        ),
    ],
)
async def test_github_fetch_policy_diagnostics_reach_management(
    monkeypatch,
    tmp_path: Path,
    tree_entries: list[dict[str, Any]],
    expected_code: str,
    expected_phase: str,
) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(_AsyncClient, "tree_entries", tree_entries)
    source = GitHubSource()
    service = SkillManagementService(
        router=SourceRouter([source]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install(f"acme/skillpack@{_COMMIT}", "github")

    assert result.success is False
    assert [(item.code, item.phase.value) for item in result.diagnostics] == [
        (expected_code, expected_phase)
    ]


@pytest.mark.asyncio
async def test_explicit_github_subpath_rejects_legacy_manifest_name(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        _AsyncClient,
        "tree_entries",
        [{"path": "skills/demo/skills.md", "type": "blob", "mode": "100644"}],
    )
    monkeypatch.setattr(
        _AsyncClient,
        "raw_payloads",
        {
            "skills/demo/skills.md": (
                b"---\nname: demo\ndescription: Legacy spelling.\n---\nBody.\n"
            )
        },
    )

    bundle = await GitHubSource().fetch(
        f"acme/skillpack@{_COMMIT}:skills/demo"
    )

    assert bundle is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identifier",
    [
        "../skillpack:skills/demo",
        "acme/..:skills/demo",
        "https://github.com/../skillpack/tree/main/skills/demo",
        "https://raw.githubusercontent.com/acme/../main/SKILL.md",
    ],
)
async def test_github_identifiers_reject_dot_repository_segments(identifier: str) -> None:
    assert await GitHubSource().resolve(identifier) is None


@pytest.mark.asyncio
async def test_invalid_identifier_is_not_reported_as_remote_not_found(tmp_path: Path) -> None:
    service = SkillManagementService(
        router=SourceRouter([GitHubSource()]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install("not-a-github-reference", "github")

    assert [item.code for item in result.diagnostics] == ["SOURCE_IDENTIFIER_INVALID"]
