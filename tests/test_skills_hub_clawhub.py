from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path
from typing import Any, ClassVar

import pytest

from opensquilla.skills.hub.clawhub import ClawHubSource
from opensquilla.skills.hub.github import GitHubSource
from opensquilla.skills.hub.lockfile import Lockfile
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillSourceFetchError,
    SourceResolution,
)
from opensquilla.skills.manifest import parse_skill_frontmatter

_COMMIT = "b" * 40


def _zip(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in entries.items():
            info = zipfile.ZipInfo(path)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, content)
    return output.getvalue()


class _Response:
    def __init__(
        self,
        *,
        json_data: object | None = None,
        content: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self._json_data = json_data
        self.content = content
        self.text = content.decode("utf-8", errors="replace")
        self.status_code = status_code

    def json(self) -> object:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _InvalidJsonResponse(_Response):
    def json(self) -> object:
        raise ValueError("invalid JSON")


class _AsyncClient:
    responses: ClassVar[dict[str, _Response]] = {}
    requests: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _AsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append((url, kwargs))
        try:
            return self.responses[url]
        except KeyError as exc:
            raise AssertionError(f"unexpected URL: {url}") from exc


class _StaticGitHubSource(GitHubSource):
    def __init__(self, skill_md: str) -> None:
        super().__init__()
        self._skill_md = skill_md

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle:
        return SkillBundle(
            name="weather",
            files={"SKILL.md": self._skill_md},
            resolution=resolution,
        )


def _mock_httpx(monkeypatch, responses: dict[str, _Response]) -> None:
    import httpx

    _AsyncClient.requests = []
    _AsyncClient.responses = responses
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)
    monkeypatch.setattr(
        "opensquilla.skills.hub.clawhub._validate_artifact_url",
        lambda _url: ["203.0.113.10"],
    )
    monkeypatch.setattr(
        "opensquilla.skills.hub.clawhub._artifact_transport",
        lambda _url, _ips, **_kwargs: None,
    )


def _offline_service(tmp_path: Path, source: ClawHubSource) -> SkillManagementService:
    return SkillManagementService(
        router=SourceRouter([source]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )


def _installed_frontmatter(tmp_path: Path, name: str) -> dict[str, Any]:
    text = (tmp_path / "managed" / name / "SKILL.md").read_text(encoding="utf-8")
    frontmatter, _body = parse_skill_frontmatter(text)
    return frontmatter


@pytest.mark.asyncio
async def test_search_preserves_exact_publisher_and_skills_sh_install_refs(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/search": _Response(
                json_data={
                    "results": [
                        {
                            "slug": "weather",
                            "ownerHandle": "alice",
                            "displayName": "Weather",
                        },
                        {
                            "slug": "weather",
                            "installRef": "skills-sh:acme/skills/weather",
                            "displayName": "Weather",
                        },
                        {
                            "slug": "weather",
                            "displayName": "Ambiguous legacy row",
                        },
                    ]
                }
            )
        },
    )

    results = await ClawHubSource(base_url="https://hub.test").search("weather")

    assert [result.canonical_identifier for result in results] == [
        "@alice/weather",
        "skills-sh:acme/skills/weather",
    ]


@pytest.mark.asyncio
async def test_search_empty_results_are_not_reported_as_source_failure(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/search": _Response(json_data={"results": []})
        },
    )

    results = await ClawHubSource(base_url="https://hub.test").search("missing")

    assert results == []


@pytest.mark.asyncio
async def test_search_all_malformed_rows_are_not_reported_as_zero_results(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/search": _Response(
                json_data={
                    "results": [
                        {"slug": "missing-publisher"},
                        {"installRef": "not-an-install-reference"},
                        "not-an-object",
                    ]
                }
            )
        },
    )

    with pytest.raises(SkillSourceFetchError) as raised:
        await ClawHubSource(base_url="https://hub.test").search("weather")

    assert [item.code for item in raised.value.diagnostics] == ["SOURCE_INVALID_RESPONSE"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        (401, "SOURCE_AUTH_FAILED"),
        (403, "SOURCE_AUTH_FAILED"),
        (429, "SOURCE_RATE_LIMITED"),
        (500, "SOURCE_SERVER_FAILED"),
        (503, "SOURCE_SERVER_FAILED"),
        ("transport", "SOURCE_TRANSPORT_FAILED"),
        ("invalid-json", "SOURCE_INVALID_RESPONSE"),
        ("invalid-shape", "SOURCE_INVALID_RESPONSE"),
    ],
)
async def test_search_failures_surface_stable_source_diagnostics(
    monkeypatch,
    outcome: int | str,
    expected_code: str,
) -> None:
    search_url = "https://hub.test/api/v1/search"
    if outcome == "invalid-json":
        response = _InvalidJsonResponse()
    elif outcome == "invalid-shape":
        response = _Response(json_data={"results": {"slug": "weather"}})
    elif isinstance(outcome, int):
        response = _Response(status_code=outcome)
    else:
        response = _Response(json_data={"results": []})
    _mock_httpx(monkeypatch, {search_url: response})
    if outcome == "transport":

        async def failing_get(
            _client: _AsyncClient,
            _url: str,
            **_kwargs: Any,
        ) -> _Response:
            raise OSError("simulated DNS failure")

        monkeypatch.setattr(_AsyncClient, "get", failing_get)

    with pytest.raises(SkillSourceFetchError) as raised:
        await ClawHubSource(base_url="https://hub.test").search("weather")

    assert [(item.code, item.phase.value) for item in raised.value.diagnostics] == [
        (expected_code, "source")
    ]


@pytest.mark.asyncio
async def test_archive_resolution_fetches_exact_version_without_leaking_token(monkeypatch) -> None:
    archive = _zip(
        {
            "SKILL.md": b"---\nname: weather\n---\n",
            "scripts/run.py": b"print('ok')\n",
        }
    )
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather-1.2.3.zip",
                    },
                }
            ),
            "https://cdn.test/weather-1.2.3.zip": _Response(content=archive),
        },
    )

    bundle = await ClawHubSource(base_url="https://hub.test", token="secret").fetch(
        "@alice/weather"
    )

    assert bundle is not None
    assert bundle.name == "weather"
    assert set(bundle.files) == {"SKILL.md", "scripts/run.py"}
    assert bundle.resolution is not None
    assert bundle.resolution.canonical_identifier == "@alice/weather@1.2.3"
    assert bundle.resolution.version == "1.2.3"
    assert bundle.resolution.trust_state == "community"
    assert bundle.resolution.expected_digest
    assert bundle.file_modes["SKILL.md"] == 0o600
    install_request = _AsyncClient.requests[0][1]
    assert install_request["params"] == {"ownerHandle": "alice"}
    download_request = _AsyncClient.requests[1][1]
    assert "Authorization" not in download_request["headers"]


@pytest.mark.asyncio
async def test_archive_registry_summary_wins_over_body_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive = _zip(
        {
            "SKILL.md": (
                b"---\nname: weather\n---\n"
                b"Body paragraph must not replace registry metadata.\n"
            )
        }
    )
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "summary": "Registry supplied weather description.",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                    },
                }
            ),
            "https://cdn.test/weather.zip": _Response(content=archive),
        },
    )
    source = ClawHubSource(base_url="https://hub.test")

    result = await _offline_service(tmp_path, source).install("@alice/weather", "clawhub")

    assert result.success is True
    assert result.resolution is not None
    assert result.resolution.meta is not None
    assert result.resolution.meta.description == "Registry supplied weather description."
    assert _installed_frontmatter(tmp_path, "weather")["description"] == (
        "Registry supplied weather description."
    )


@pytest.mark.asyncio
async def test_exact_search_summary_flows_into_legacy_archive_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive = _zip(
        {
            "SKILL.md": (
                b"---\nname: weather\n---\n"
                b"Body paragraph must remain the lower-priority fallback.\n"
            )
        }
    )
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/search": _Response(
                json_data={
                    "results": [
                        {
                            "slug": "weather",
                            "ownerHandle": "alice",
                            "summary": "Description retained from exact search metadata.",
                        }
                    ]
                }
            ),
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                    },
                }
            ),
            "https://cdn.test/weather.zip": _Response(content=archive),
        },
    )
    source = ClawHubSource(base_url="https://hub.test")

    results = await source.search("weather")
    result = await _offline_service(tmp_path, source).install(
        results[0].canonical_identifier,
        "clawhub",
    )

    assert result.success is True
    assert _installed_frontmatter(tmp_path, "weather")["description"] == (
        "Description retained from exact search metadata."
    )


@pytest.mark.asyncio
async def test_github_handoff_registry_description_allows_empty_legacy_body(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "skill": {"description": "Registry description for GitHub handoff."},
                    "installKind": "github",
                    "github": {
                        "repo": "acme/skills",
                        "path": "skills/weather",
                        "commit": _COMMIT,
                        "contentHash": "tree-content-hash",
                        "sourceUrl": "https://github.com/acme/skills/tree/main/skills/weather",
                    },
                }
            )
        },
    )
    source = ClawHubSource(
        base_url="https://hub.test",
        github_source=_StaticGitHubSource("---\nname: weather\n---\n"),
    )

    result = await _offline_service(tmp_path, source).install("@alice/weather", "clawhub")

    assert result.success is True
    assert result.resolution is not None
    assert result.resolution.meta is not None
    assert result.resolution.meta.description == "Registry description for GitHub handoff."
    assert _installed_frontmatter(tmp_path, "weather")["description"] == (
        "Registry description for GitHub handoff."
    )


@pytest.mark.asyncio
async def test_github_handoff_legacy_runtime_name_binds_to_registry_package_slug(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/metro-home-pro/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "metro-home-pro",
                    "ownerHandle": "alice",
                    "installKind": "github",
                    "github": {
                        "repo": "acme/skills",
                        "path": "skills/metro-home",
                        "commit": _COMMIT,
                        "contentHash": "tree-content-hash",
                        "sourceUrl": (
                            "https://github.com/acme/skills/tree/"
                            f"{_COMMIT}/skills/metro-home"
                        ),
                    },
                }
            )
        },
    )
    source = ClawHubSource(
        base_url="https://hub.test",
        github_source=_StaticGitHubSource(
            "---\nname: metro_home\n"
            "description: Synthetic legacy runtime name.\n"
            "---\nInstructions.\n"
        ),
    )

    result = await _offline_service(tmp_path, source).install(
        "@alice/metro-home-pro",
        "clawhub",
    )

    assert result.success is True
    assert result.name == "metro_home"
    assert result.resolution is not None
    assert result.resolution.package_identifier == "@alice/metro-home-pro"
    assert result.resolution.skill_path == "skills/metro-home"
    assert _installed_frontmatter(tmp_path, "metro-home-pro")["name"] == "metro_home"
    assert not (tmp_path / "managed" / "metro_home").exists()
    entry = Lockfile.load(tmp_path / "skills-lock.json").get("metro-home-pro")
    assert entry is not None
    assert entry.manifest_name == "metro_home"


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_name", ["skill.md", "skills.md"])
async def test_real_github_handoff_normalizes_legacy_manifest_name(
    monkeypatch,
    tmp_path: Path,
    manifest_name: str,
) -> None:
    manifest_path = f"skills/weather/{manifest_name}"
    manifest_body = b"Legacy weather instructions.\n"
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "github",
                    "github": {
                        "repo": "acme/skills",
                        "path": manifest_path,
                        "commit": _COMMIT,
                        "contentHash": "opaque-registry-content-hash",
                        "sourceUrl": (
                            "https://github.com/acme/skills/tree/"
                            f"{_COMMIT}/skills/weather"
                        ),
                    },
                }
            ),
            (
                "https://api.github.com/repos/acme/skills/git/trees/"
                f"{_COMMIT}?recursive=1"
            ): _Response(
                json_data={
                    "tree": [
                        {
                            "path": manifest_path,
                            "type": "blob",
                            "mode": "100644",
                            "size": len(manifest_body),
                        }
                    ],
                    "truncated": False,
                }
            ),
            f"https://raw.githubusercontent.com/acme/skills/{_COMMIT}/{manifest_path}": (
                _Response(content=manifest_body)
            ),
        },
    )
    source = ClawHubSource(
        base_url="https://hub.test",
        github_source=GitHubSource(),
    )

    result = await _offline_service(tmp_path, source).install(
        "@alice/weather",
        "clawhub",
    )

    assert result.success is True
    installed_dir = tmp_path / "managed" / "weather"
    assert (installed_dir / "SKILL.md").is_file()
    installed_names = {child.name for child in installed_dir.iterdir()}
    assert "SKILL.md" in installed_names
    assert manifest_name not in installed_names
    assert _installed_frontmatter(tmp_path, "weather") == {
        "name": "weather",
        "description": "Legacy weather instructions.",
    }


@pytest.mark.asyncio
async def test_real_github_handoff_rejects_ambiguous_legacy_manifests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    manifest_body = b"Legacy weather instructions.\n"
    manifest_paths = ("skills/weather/skill.md", "skills/weather/skills.md")
    responses: dict[str, _Response] = {
        "https://hub.test/api/v1/skills/weather/install": _Response(
            json_data={
                "ok": True,
                "slug": "weather",
                "ownerHandle": "alice",
                "installKind": "github",
                "github": {
                    "repo": "acme/skills",
                    "path": "skills/weather",
                    "commit": _COMMIT,
                    "contentHash": "opaque-registry-content-hash",
                    "sourceUrl": (
                        "https://github.com/acme/skills/tree/"
                        f"{_COMMIT}/skills/weather"
                    ),
                },
            }
        ),
        (
            "https://api.github.com/repos/acme/skills/git/trees/"
            f"{_COMMIT}?recursive=1"
        ): _Response(
            json_data={
                "tree": [
                    {
                        "path": path,
                        "type": "blob",
                        "mode": "100644",
                        "size": len(manifest_body),
                    }
                    for path in manifest_paths
                ],
                "truncated": False,
            }
        ),
    }
    responses.update(
        {
            f"https://raw.githubusercontent.com/acme/skills/{_COMMIT}/{path}": _Response(
                content=manifest_body
            )
            for path in manifest_paths
        }
    )
    _mock_httpx(monkeypatch, responses)
    source = ClawHubSource(
        base_url="https://hub.test",
        github_source=GitHubSource(),
    )

    result = await _offline_service(tmp_path, source).install(
        "@alice/weather",
        "clawhub",
    )

    assert result.success is False
    assert any(item.code == "SOURCE_TREE_AMBIGUOUS" for item in result.diagnostics)
    assert not (tmp_path / "managed" / "weather").exists()


@pytest.mark.asyncio
async def test_archive_body_is_description_fallback_without_registry_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive = _zip(
        {
            "SKILL.md": (
                b"---\nname: weather\n---\n"
                b"Body supplied weather description.\n\nDetailed instructions.\n"
            )
        }
    )
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                    },
                }
            ),
            "https://cdn.test/weather.zip": _Response(content=archive),
        },
    )

    result = await _offline_service(
        tmp_path,
        ClawHubSource(base_url="https://hub.test"),
    ).install("@alice/weather", "clawhub")

    assert result.success is True
    assert _installed_frontmatter(tmp_path, "weather")["description"] == (
        "Body supplied weather description."
    )


@pytest.mark.asyncio
async def test_archive_without_registry_or_body_description_uses_safe_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    archive = _zip({"SKILL.md": b"---\nname: weather\n---\n"})
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                    },
                }
            ),
            "https://cdn.test/weather.zip": _Response(content=archive),
        },
    )

    result = await _offline_service(
        tmp_path,
        ClawHubSource(base_url="https://hub.test"),
    ).install("@alice/weather", "clawhub")

    assert result.success is True
    assert _installed_frontmatter(tmp_path, "weather")["description"] == (
        "Community Skill weather"
    )


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
async def test_clawhub_resolution_failures_keep_source_diagnostics(
    monkeypatch,
    tmp_path: Path,
    outcome: int | str,
    expected_code: str,
) -> None:
    install_url = "https://hub.test/api/v1/skills/weather/install"
    if outcome == "invalid-json":
        response = _InvalidJsonResponse()
    elif isinstance(outcome, int):
        response = _Response(status_code=outcome)
    else:
        response = _Response()
    _mock_httpx(monkeypatch, {install_url: response})
    if outcome == "transport":

        async def failing_get(
            _client: _AsyncClient,
            _url: str,
            **_kwargs: Any,
        ) -> _Response:
            raise OSError("simulated DNS failure")

        monkeypatch.setattr(_AsyncClient, "get", failing_get)
    service = SkillManagementService(
        router=SourceRouter([ClawHubSource(base_url="https://hub.test")]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install("@alice/weather", "clawhub")

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
    ],
)
async def test_clawhub_fetch_failures_keep_fetch_diagnostics(
    monkeypatch,
    tmp_path: Path,
    outcome: int | str,
    expected_code: str,
) -> None:
    install_url = "https://hub.test/api/v1/skills/weather/install"
    archive_url = "https://cdn.test/weather.zip"
    install_response = _Response(
        json_data={
            "ok": True,
            "slug": "weather",
            "ownerHandle": "alice",
            "installKind": "archive",
            "archive": {
                "version": "1.2.3",
                "downloadUrl": archive_url,
            },
        }
    )
    archive_response = _Response(
        status_code=outcome if isinstance(outcome, int) else 200
    )
    _mock_httpx(
        monkeypatch,
        {
            install_url: install_response,
            archive_url: archive_response,
        },
    )
    if outcome == "transport":
        original_get = _AsyncClient.get

        async def failing_archive_get(
            client: _AsyncClient,
            url: str,
            **kwargs: Any,
        ) -> _Response:
            if url == archive_url:
                raise OSError("simulated DNS failure")
            return await original_get(client, url, **kwargs)

        monkeypatch.setattr(_AsyncClient, "get", failing_archive_get)
    service = SkillManagementService(
        router=SourceRouter([ClawHubSource(base_url="https://hub.test")]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install("@alice/weather", "clawhub")

    assert result.success is False
    assert [(item.code, item.phase.value) for item in result.diagnostics] == [
        (expected_code, "fetch")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("archive", "digest", "expected_code", "expected_phase"),
    [
        (
            _zip(
                {
                    "one/SKILL.md": b"---\nname: one\ndescription: One.\n---\n",
                    "two/SKILL.md": b"---\nname: two\ndescription: Two.\n---\n",
                }
            ),
            "",
            "SOURCE_TREE_AMBIGUOUS",
            "archive",
        ),
        (
            _zip(
                {
                    "SKILL.md": (
                        b"---\nname: weather\ndescription: Weather.\n---\nBody.\n"
                    )
                }
            ),
            "0" * 64,
            "ARTIFACT_DIGEST_MISMATCH",
            "security",
        ),
    ],
)
async def test_clawhub_archive_diagnostics_reach_management(
    monkeypatch,
    tmp_path: Path,
    archive: bytes,
    digest: str,
    expected_code: str,
    expected_phase: str,
) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                        "sha256": digest,
                    },
                }
            ),
            "https://cdn.test/weather.zip": _Response(content=archive),
        },
    )
    source = ClawHubSource(base_url="https://hub.test")
    service = SkillManagementService(
        router=SourceRouter([source]),
        managed_dir=tmp_path / "managed",
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )

    result = await service.install("@alice/weather", "clawhub")

    assert result.success is False
    assert [(item.code, item.phase.value) for item in result.diagnostics] == [
        (expected_code, expected_phase)
    ]


@pytest.mark.asyncio
async def test_bare_slug_resolution_binds_server_publisher(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                    },
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve("weather")

    assert resolution is not None
    assert resolution.canonical_identifier == "@alice/weather@1.2.3"
    assert resolution.package_identifier == "@alice/weather"
    assert resolution.publisher == "alice"


@pytest.mark.asyncio
async def test_bare_slug_resolution_without_publisher_is_blocked(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "https://cdn.test/weather.zip",
                    },
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve("weather")

    assert resolution is not None
    assert resolution.immutable is False
    assert resolution.diagnostics[0].code == "SOURCE_PUBLISHER_UNRESOLVED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("digest", "expected_code"),
    [
        ("", "ARTIFACT_TRANSPORT_INSECURE"),
        ("not-a-sha256", "SOURCE_INVALID_ARTIFACT_DIGEST"),
    ],
)
async def test_archive_resolution_rejects_unverifiable_handoff(
    monkeypatch,
    digest: str,
    expected_code: str,
) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "http://cdn.test/weather.zip",
                        "sha256": digest,
                    },
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve(
        "@alice/weather"
    )

    assert resolution is not None
    assert resolution.immutable is False
    assert resolution.diagnostics[0].code == expected_code
    assert resolution.diagnostics[0].phase.value == "security"


@pytest.mark.asyncio
async def test_archive_resolution_allows_digest_pinned_plaintext_handoff(monkeypatch) -> None:
    digest = "a" * 64
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "ownerHandle": "alice",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "http://cdn.test/weather.zip",
                        "sha256": digest,
                    },
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve(
        "@alice/weather"
    )

    assert resolution is not None
    assert resolution.immutable is True
    assert resolution.expected_digest == digest


@pytest.mark.asyncio
async def test_archive_fetch_rejects_ssrf_target_before_download(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "installKind": "archive",
                    "archive": {
                        "version": "1.2.3",
                        "downloadUrl": "http://127.0.0.1/internal.zip",
                    },
                }
            ),
        },
    )

    def blocked(_url: str) -> list[str]:
        raise ValueError("blocked private target")

    monkeypatch.setattr(
        "opensquilla.skills.hub.clawhub._validate_artifact_url",
        blocked,
    )

    bundle = await ClawHubSource(base_url="https://hub.test").fetch("weather")

    assert bundle is None
    assert [url for url, _kwargs in _AsyncClient.requests] == [
        "https://hub.test/api/v1/skills/weather/install"
    ]


@pytest.mark.asyncio
async def test_github_handoff_requires_a_full_commit_and_returns_diagnostic(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "installKind": "github",
                    "github": {
                        "repo": "acme/skills",
                        "path": "skills/weather",
                        "commit": "main",
                        "contentHash": "sha256:expected",
                        "sourceUrl": "https://github.com/acme/skills",
                    },
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve("weather")

    assert resolution is not None
    assert resolution.immutable is False
    assert resolution.diagnostics[0].code == "SOURCE_INVALID_GITHUB_HANDOFF"
    assert resolution.diagnostics[0].blocking is True


@pytest.mark.asyncio
async def test_commit_pinned_github_handoff_has_stable_wire_shape(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "installKind": "github",
                    "trust": {"state": "not-scanned-by-clawhub"},
                    "github": {
                        "repo": "acme/skills",
                        "path": "skills/weather/SKILL.md",
                        "commit": _COMMIT,
                        "contentHash": "tree-content-hash",
                        "sourceUrl": "https://github.com/acme/skills/tree/main/skills/weather",
                    },
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve(
        "skills-sh:acme/skills/weather"
    )

    assert resolution is not None
    assert resolution.immutable is True
    assert resolution.skill_path == "skills/weather"
    assert resolution.revision == _COMMIT
    assert resolution.expected_digest == ""
    assert resolution.resolver_content_hash == "tree-content-hash"
    assert resolution.trust_state == "not-scanned-by-clawhub"
    assert resolution.as_dict() == resolution.to_dict()
    assert resolution.to_dict()["immutableRevision"] == _COMMIT
    assert resolution.to_dict()["artifactDigest"] == ""
    assert resolution.to_dict()["resolverContentHash"] == "tree-content-hash"
    request = _AsyncClient.requests[0][1]
    assert request["params"] == {"reference": "skills-sh:acme/skills/weather"}


@pytest.mark.asyncio
async def test_unknown_install_handoff_is_structured_instead_of_silent_none(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        {
            "https://hub.test/api/v1/skills/weather/install": _Response(
                json_data={
                    "ok": True,
                    "slug": "weather",
                    "installKind": "magic",
                }
            )
        },
    )

    resolution = await ClawHubSource(base_url="https://hub.test").resolve("weather")

    assert resolution is not None
    assert resolution.artifact_kind == "unsupported"
    assert resolution.diagnostics[0].code == "SOURCE_HANDOFF_UNSUPPORTED"
    assert resolution.diagnostics[0].details == {"installKind": "magic"}
