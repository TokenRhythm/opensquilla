"""SourceRouter — aggregates search/fetch across multiple SkillSource adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import cast

import structlog

from opensquilla.skills.hub.contracts import DiagnosticPhase, SkillDiagnostic
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    SkillSourceFetchError,
    SourceResolution,
    source_invalid_response_error,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SourceSearchReport:
    """Truthful aggregate outcome used beside the list-returning compatibility API."""

    results: tuple[SkillMeta, ...] = ()
    diagnostics: tuple[SkillDiagnostic, ...] = ()
    searched_sources: tuple[str, ...] = ()
    successful_sources: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return bool(self.diagnostics and self.successful_sources)

    @property
    def all_sources_unavailable(self) -> bool:
        return bool(self.diagnostics and not self.successful_sources)


def _source_diagnostics(
    exc: SkillSourceFetchError,
    source_id: str,
) -> tuple[SkillDiagnostic, ...]:
    return tuple(
        replace(
            diagnostic,
            details={**diagnostic.details, "source": source_id},
        )
        for diagnostic in exc.diagnostics
    )


def _invalid_source_diagnostics(source_id: str) -> tuple[SkillDiagnostic, ...]:
    exc = source_invalid_response_error(
        phase=DiagnosticPhase.SOURCE,
        source_name=source_id,
    )
    return _source_diagnostics(exc, source_id)


async def search_router_with_diagnostics(
    router: object,
    query: str,
    *,
    limit: int = 20,
    source_id: str | None = None,
) -> SourceSearchReport:
    """Use the aggregate contract while retaining legacy injected routers."""

    detailed_search = getattr(router, "search_with_diagnostics", None)
    diagnostic_source = source_id or "skill-source-router"
    try:
        if callable(detailed_search):
            report = await detailed_search(query, limit=limit, source_id=source_id)
            if isinstance(report, SourceSearchReport):
                return report
            return SourceSearchReport(
                diagnostics=_invalid_source_diagnostics(diagnostic_source),
                searched_sources=(diagnostic_source,),
            )

        legacy_search = getattr(router, "search")
        results = await legacy_search(query, limit=limit, source_id=source_id)
    except SkillSourceFetchError as exc:
        return SourceSearchReport(
            diagnostics=_source_diagnostics(exc, diagnostic_source),
            searched_sources=(diagnostic_source,),
        )
    except Exception as exc:
        log.warning(
            "router.legacy_search_failed",
            source_id=diagnostic_source,
            error=str(exc),
        )
        return SourceSearchReport(
            diagnostics=_invalid_source_diagnostics(diagnostic_source),
            searched_sources=(diagnostic_source,),
        )

    if not isinstance(results, list) or any(not isinstance(row, SkillMeta) for row in results):
        return SourceSearchReport(
            diagnostics=_invalid_source_diagnostics(diagnostic_source),
            searched_sources=(diagnostic_source,),
        )
    successful_sources = tuple(
        dict.fromkeys(row.source_id for row in results if row.source_id)
    ) or (diagnostic_source,)
    return SourceSearchReport(
        results=tuple(results),
        searched_sources=(diagnostic_source,),
        successful_sources=successful_sources,
    )


class SourceRouter:
    """Routes skill operations to the appropriate source adapter."""

    def __init__(self, sources: list[SkillSource] | None = None) -> None:
        self._sources: dict[str, SkillSource] = {}
        for s in sources or []:
            self._sources[s.source_id] = s

    def add_source(self, source: SkillSource) -> None:
        self._sources[source.source_id] = source

    def get_source(self, source_id: str) -> SkillSource | None:
        return self._sources.get(source_id)

    @property
    def source_ids(self) -> list[str]:
        return list(self._sources.keys())

    async def search(
        self, query: str, limit: int = 20, source_id: str | None = None
    ) -> list[SkillMeta]:
        """Compatibility search returning only results.

        Truth-aware callers should use :meth:`search_with_diagnostics`. A
        source-specific call retains the historical exception behavior.
        """
        if source_id:
            src = self._sources.get(source_id)
            if src is None:
                return []
            results = await src.search(query, limit=limit)
            self._fill_source_ids(results, source_id)
            return self._deduplicate(results, limit)

        report = await self.search_with_diagnostics(query, limit=limit)
        return list(report.results)

    async def search_with_diagnostics(
        self,
        query: str,
        limit: int = 20,
        source_id: str | None = None,
    ) -> SourceSearchReport:
        """Search sources without confusing source failure with zero results."""

        if source_id:
            source = self._sources.get(source_id)
            if source is None:
                return SourceSearchReport(
                    diagnostics=_invalid_source_diagnostics(source_id),
                    searched_sources=(source_id,),
                )
            sources = [source]
        else:
            sources = list(self._sources.values())

        searched_sources = tuple(source.source_id for source in sources)
        if not sources:
            return SourceSearchReport(searched_sources=searched_sources)

        tasks = [src.search(query, limit=limit) for src in sources]
        all_results: list[SkillMeta] = []
        diagnostics: list[SkillDiagnostic] = []
        successful_sources: list[str] = []
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        for source, result_list in zip(sources, gathered, strict=True):
            if isinstance(result_list, SkillSourceFetchError):
                diagnostics.extend(_source_diagnostics(result_list, source.source_id))
                log.warning(
                    "router.search_source_failed",
                    source_id=source.source_id,
                    error=str(result_list),
                )
                continue
            if isinstance(result_list, asyncio.CancelledError):
                raise result_list
            if isinstance(result_list, BaseException):
                diagnostics.extend(_invalid_source_diagnostics(source.source_id))
                log.warning(
                    "router.search_source_failed",
                    source_id=source.source_id,
                    error=str(result_list),
                )
                continue
            if not isinstance(result_list, list) or any(
                not isinstance(result, SkillMeta) for result in result_list
            ):
                diagnostics.extend(_invalid_source_diagnostics(source.source_id))
                log.warning(
                    "router.search_source_invalid_response",
                    source_id=source.source_id,
                )
                continue

            successful_sources.append(source.source_id)
            self._fill_source_ids(result_list, source.source_id)
            all_results.extend(result_list)

        return SourceSearchReport(
            results=tuple(self._deduplicate(all_results, limit)),
            diagnostics=tuple(diagnostics),
            searched_sources=searched_sources,
            successful_sources=tuple(successful_sources),
        )

    @staticmethod
    def _fill_source_ids(results: list[SkillMeta], source_id: str) -> None:
        for result in results:
            if not result.source_id:
                result.source_id = source_id

    @staticmethod
    def _deduplicate(results: list[SkillMeta], limit: int) -> list[SkillMeta]:
        """Deduplicate exact packages without collapsing same-name publishers."""

        seen: set[tuple[str, str]] = set()
        deduped: list[SkillMeta] = []
        for result in results:
            identifier = result.canonical_identifier or result.identifier or result.name
            identity = (result.source_id, identifier)
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(result)
        return deduped[:limit]

    async def resolve(self, identifier: str, source_id: str) -> SourceResolution | None:
        """Resolve through a modern source or synthesize a legacy fetch contract."""

        src = self._sources.get(source_id)
        if src is None:
            log.warning("router.resolve_unknown_source", source_id=source_id)
            return None
        resolver = getattr(src, "resolve", None)
        if callable(resolver):
            return cast(SourceResolution | None, await resolver(identifier))
        return SourceResolution(
            source_id=source_id,
            requested_identifier=identifier,
            canonical_identifier=identifier,
        )

    async def fetch(self, identifier: str, source_id: str) -> SkillBundle | None:
        """Resolve then fetch, while retaining support for fetch-only adapters."""

        src = self._sources.get(source_id)
        if src is None:
            log.warning("router.fetch_unknown_source", source_id=source_id)
            return None
        resolution = await self.resolve(identifier, source_id)
        if resolution is None:
            return None
        fetch_resolved = getattr(src, "fetch_resolved", None)
        if callable(fetch_resolved):
            bundle = cast(SkillBundle | None, await fetch_resolved(resolution))
        else:
            bundle = await src.fetch(identifier)
        if bundle is not None and bundle.resolution is None:
            bundle.resolution = resolution
        return bundle

    async def inspect(self, identifier: str, source_id: str) -> SkillMeta | None:
        """Get metadata from a specific source."""
        src = self._sources.get(source_id)
        if src is None:
            return None
        return await src.inspect(identifier)
