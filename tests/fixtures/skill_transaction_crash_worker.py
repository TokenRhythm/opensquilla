"""Subprocess worker that terminates at a persisted Skill transaction phase."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    SourceResolution,
)
from opensquilla.skills.hub.transaction import SkillTransactionJournal

_CRASH_EXIT_CODE = 73


class _UpdateSource(SkillSource):
    @property
    def source_id(self) -> str:
        return "crash-source"

    @property
    def trust_level(self) -> str:
        return "community"

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return []

    async def resolve(self, identifier: str) -> SourceResolution:
        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=identifier,
            canonical_identifier=f"fixture/{identifier}@2.0.0",
            immutable=True,
            revision="c" * 40,
            expected_digest="updated-fixture-artifact",
            publisher="fixture",
            version="2.0.0",
            trust_state="community",
            meta=SkillMeta(
                name=identifier,
                description="Updated process-crash fixture",
                source_id=self.source_id,
            ),
        )

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle:
        return SkillBundle(
            name=resolution.requested_identifier,
            files={
                "SKILL.md": (
                    "---\n"
                    "name: example\n"
                    "description: Updated process-crash fixture\n"
                    "---\n"
                    "new\n"
                )
            },
            meta=resolution.meta,
            resolution=resolution,
        )

    async def fetch(self, identifier: str) -> SkillBundle | None:
        resolution = await self.resolve(identifier)
        return await self.fetch_resolved(resolution)

    async def inspect(self, identifier: str) -> SkillMeta | None:
        return SkillMeta(name=identifier, source_id=self.source_id)


def main() -> None:
    managed = Path(sys.argv[1])
    lockfile_path = Path(sys.argv[2])
    journal_path = Path(sys.argv[3])
    phase = sys.argv[4]

    original_write = SkillTransactionJournal.write

    def crash_after_persisted_phase(
        journal: SkillTransactionJournal,
        path: Path,
    ) -> None:
        if phase == "pre_journal" and journal.phase == "prepared":
            os._exit(_CRASH_EXIT_CODE)
        original_write(journal, path)
        if journal.phase == phase:
            os._exit(_CRASH_EXIT_CODE)

    SkillTransactionJournal.write = crash_after_persisted_phase
    service = SkillManagementService(
        router=SourceRouter([_UpdateSource()]),
        managed_dir=managed,
        lockfile_path=lockfile_path,
        journal_path=journal_path,
        offline=True,
    )
    asyncio.run(service.install("example", "crash-source"))
    os._exit(79)


if __name__ == "__main__":
    main()
