from __future__ import annotations

from typing import Any

import pytest

from opensquilla.skills.hub.contracts import DiagnosticPhase
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSourceFetchError,
    SourceResolution,
)


class _SearchSource:
    def __init__(self, source_id: str, results: list[SkillMeta]) -> None:
        self.source_id = source_id
        self.trust_level = "community"
        self.results = results

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return self.results

    async def fetch(self, identifier: str) -> SkillBundle | None:
        return None

    async def inspect(self, identifier: str) -> SkillMeta | None:
        return None


@pytest.mark.asyncio
async def test_search_deduplicates_canonical_identity_not_display_name() -> None:
    source = _SearchSource(
        "clawhub",
        [
            SkillMeta(name="Weather", identifier="@alice/weather"),
            SkillMeta(name="Weather", identifier="@bob/weather"),
            SkillMeta(name="Renamed", identifier="legacy", canonical_identifier="@alice/weather"),
        ],
    )

    results = await SourceRouter([source]).search("weather")  # type: ignore[list-item]

    assert [result.identifier for result in results] == ["@alice/weather", "@bob/weather"]


@pytest.mark.asyncio
async def test_same_identifier_from_different_sources_is_not_collapsed() -> None:
    sources = [
        _SearchSource("clawhub", [SkillMeta(name="Demo", identifier="demo")]),
        _SearchSource("github", [SkillMeta(name="Demo", identifier="demo")]),
    ]

    results = await SourceRouter(sources).search("demo")  # type: ignore[arg-type]

    assert [(result.source_id, result.identifier) for result in results] == [
        ("clawhub", "demo"),
        ("github", "demo"),
    ]


class _FailingSearchSource(_SearchSource):
    def __init__(self, source_id: str, code: str) -> None:
        super().__init__(source_id, [])
        self.code = code

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        raise SkillSourceFetchError.diagnostic(
            self.code,
            f"{self.source_id} search failed.",
            phase=DiagnosticPhase.SOURCE,
            details={"retryAfter": "30"} if self.code == "SOURCE_RATE_LIMITED" else {},
        )


class _MalformedSearchSource(_SearchSource):
    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return {"results": "not-a-list"}  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_search_report_keeps_partial_results_and_source_diagnostics() -> None:
    router = SourceRouter(
        [
            _SearchSource("healthy", [SkillMeta(name="Demo", identifier="demo")]),
            _FailingSearchSource("limited", "SOURCE_RATE_LIMITED"),
        ]  # type: ignore[arg-type]
    )

    report = await router.search_with_diagnostics("demo")

    assert [(row.source_id, row.identifier) for row in report.results] == [
        ("healthy", "demo")
    ]
    assert [item.code for item in report.diagnostics] == ["SOURCE_RATE_LIMITED"]
    assert report.diagnostics[0].details == {"source": "limited", "retryAfter": "30"}
    assert report.successful_sources == ("healthy",)
    assert report.partial is True
    assert report.all_sources_unavailable is False


@pytest.mark.asyncio
async def test_search_report_distinguishes_all_failed_sources_from_valid_zero_results() -> None:
    failed = SourceRouter(
        [
            _FailingSearchSource("limited", "SOURCE_RATE_LIMITED"),
            _MalformedSearchSource("malformed", []),
        ]  # type: ignore[arg-type]
    )

    failed_report = await failed.search_with_diagnostics("demo")
    empty_report = await SourceRouter(
        [_SearchSource("healthy-empty", [])]  # type: ignore[list-item]
    ).search_with_diagnostics("demo")

    assert failed_report.results == ()
    assert [item.code for item in failed_report.diagnostics] == [
        "SOURCE_RATE_LIMITED",
        "SOURCE_INVALID_RESPONSE",
    ]
    assert failed_report.all_sources_unavailable is True
    assert empty_report.results == ()
    assert empty_report.diagnostics == ()
    assert empty_report.successful_sources == ("healthy-empty",)
    assert empty_report.all_sources_unavailable is False


class _LegacyFetchOnlySource:
    source_id = "legacy"
    trust_level = "community"

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return []

    async def fetch(self, identifier: str) -> SkillBundle | None:
        return SkillBundle(name=identifier, files={"SKILL.md": "---\n---\n"})

    async def inspect(self, identifier: str) -> SkillMeta | None:
        return None


@pytest.mark.asyncio
async def test_fetch_only_fake_adapter_gets_a_legacy_resolution() -> None:
    source = _LegacyFetchOnlySource()
    router = SourceRouter([source])  # type: ignore[list-item]

    bundle = await router.fetch("demo", "legacy")

    assert bundle is not None
    assert bundle.resolution == SourceResolution(
        source_id="legacy",
        requested_identifier="demo",
        canonical_identifier="demo",
    )


class _ModernSource(_LegacyFetchOnlySource):
    source_id = "modern"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def resolve(self, identifier: str) -> SourceResolution | None:
        self.calls.append(("resolve", identifier))
        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=identifier,
            canonical_identifier=f"{identifier}@fixed",
            immutable=True,
            revision="fixed",
        )

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle | None:
        self.calls.append(("fetch_resolved", resolution))
        return SkillBundle(name="demo", files={"SKILL.md": "---\n---\n"})

    async def fetch(self, identifier: str) -> SkillBundle | None:
        raise AssertionError("router must use fetch_resolved after resolve")


@pytest.mark.asyncio
async def test_modern_adapter_uses_resolve_then_fetch_resolved() -> None:
    source = _ModernSource()
    router = SourceRouter([source])  # type: ignore[list-item]

    bundle = await router.fetch("demo", "modern")

    assert bundle is not None
    assert [call[0] for call in source.calls] == ["resolve", "fetch_resolved"]
    assert bundle.resolution is not None
    assert bundle.resolution.canonical_identifier == "demo@fixed"
