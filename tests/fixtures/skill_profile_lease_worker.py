"""Subprocess workers for the Gateway-lease versus offline-Skill-writer gate."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from opensquilla.profile_operation_lock import ProfileOperationLock
from opensquilla.recovery.errors import ProfileLockBusyError
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    SourceResolution,
)

_LOCK_BUSY_EXIT_CODE = 75
_INSTALL_FAILED_EXIT_CODE = 76
_HOLDER_TIMEOUT_EXIT_CODE = 77
_BOOT_RECOVERY_MUTATED_EXIT_CODE = 78
_BOOT_RECOVERY_NOT_QUARANTINED_EXIT_CODE = 79


class _OfflineSource(SkillSource):
    @property
    def source_id(self) -> str:
        return "process-fixture"

    @property
    def trust_level(self) -> str:
        return "community"

    async def search(self, query: str, limit: int = 20) -> list[SkillMeta]:
        return []

    async def resolve(self, identifier: str) -> SourceResolution:
        return SourceResolution(
            source_id=self.source_id,
            requested_identifier=identifier,
            canonical_identifier=f"fixture/{identifier}@1.0.0",
            immutable=True,
            revision="f" * 40,
            expected_digest="fixture-artifact",
            publisher="fixture",
            version="1.0.0",
            trust_state="community",
            meta=SkillMeta(
                name=identifier,
                description="Synthetic process-lock fixture",
                source_id=self.source_id,
            ),
        )

    async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle:
        return SkillBundle(
            name=resolution.requested_identifier,
            files={
                "SKILL.md": (
                    "---\n"
                    "name: process-skill\n"
                    "description: Synthetic process-lock fixture\n"
                    "---\n"
                    "Follow the synthetic instructions.\n"
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


def _hold_profile_lease(profile_home: Path, ready: Path) -> None:
    with ProfileOperationLock(profile_home):
        ready.write_text("ready", encoding="utf-8")
        # The parent owns the write end. A deliberate close releases the lease,
        # and an abnormal parent exit produces EOF without a fixture deadline.
        sys.stdin.buffer.read(1)


def _offline_install(
    profile_home: Path,
    managed: Path,
    lockfile: Path,
    journal: Path,
    marker: Path,
    ready: Path | None = None,
    release: Path | None = None,
) -> None:
    service = SkillManagementService(
        router=SourceRouter([_OfflineSource()]),
        managed_dir=managed,
        lockfile_path=lockfile,
        journal_path=journal,
        offline=True,
    )
    try:
        with ProfileOperationLock(profile_home):
            if ready is not None and release is not None:
                ready.write_text("ready", encoding="utf-8")
                deadline = time.monotonic() + 15.0
                while not release.exists():
                    if time.monotonic() >= deadline:
                        os._exit(_HOLDER_TIMEOUT_EXIT_CODE)
                    time.sleep(0.01)
            result = asyncio.run(service.install("process-skill", "process-fixture"))
    except ProfileLockBusyError:
        os._exit(_LOCK_BUSY_EXIT_CODE)
    if not result.success:
        os._exit(_INSTALL_FAILED_EXIT_CODE)
    marker.write_text(result.name, encoding="utf-8")


def _probe_unleased_build_services(
    profile_home: Path,
    managed: Path,
    orphan_reservation: Path,
    marker: Path,
) -> None:
    """Exercise the public standalone builder while another process owns H."""

    from opensquilla.gateway.boot import build_services
    from opensquilla.gateway.config import GatewayConfig

    os.environ["OPENSQUILLA_STATE_DIR"] = str(profile_home)
    config = GatewayConfig(
        state_dir=str(profile_home / "state"),
        workspace_dir=str(profile_home / "workspace"),
        skills={"allow_bundled": False, "managed_dir": str(managed)},
        control_ui={"enabled": False},
        channels={"channels": []},
        mcp={"enabled": False},
        memory={"flush_enabled": False},
        sandbox={"auto_setup": False},
    )

    async def run() -> tuple[str, ...]:
        services = await build_services(
            config=config,
            session_db_path=":memory:",
            seed_agent_workspaces=False,
        )
        try:
            return tuple(
                str(getattr(item, "code", ""))
                for item in services.skill_management_state.get(
                    "recovery_diagnostics",
                    (),
                )
            )
        finally:
            await services.close()

    codes = asyncio.run(run())
    if not orphan_reservation.exists():
        os._exit(_BOOT_RECOVERY_MUTATED_EXIT_CODE)
    if "PROFILE_LEASE_REQUIRED" not in codes:
        os._exit(_BOOT_RECOVERY_NOT_QUARANTINED_EXIT_CODE)
    marker.write_text(json.dumps({"diagnostics": codes}), encoding="utf-8")


def main() -> None:
    mode = sys.argv[1]
    if mode == "hold":
        _hold_profile_lease(Path(sys.argv[2]), Path(sys.argv[3]))
        return
    if mode == "install":
        _offline_install(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            Path(sys.argv[6]),
        )
        return
    if mode == "install_wait":
        _offline_install(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            Path(sys.argv[6]),
            Path(sys.argv[7]),
            Path(sys.argv[8]),
        )
        return
    if mode == "boot_probe":
        _probe_unleased_build_services(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
        )
        return
    raise SystemExit(f"unknown worker mode: {mode}")


if __name__ == "__main__":
    main()
