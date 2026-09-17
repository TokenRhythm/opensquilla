"""Shared defaults for Community skill sources and installer wiring."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from opensquilla.paths import default_opensquilla_home
from opensquilla.skills.hub.clawhub import ClawHubSource
from opensquilla.skills.hub.contracts import SkillDiagnostic
from opensquilla.skills.hub.github import GitHubSource
from opensquilla.skills.hub.installer import SkillInstaller
from opensquilla.skills.hub.lockfile import Lockfile
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillSource

_default_router: SourceRouter | None = None


def get_default_skill_router() -> SourceRouter:
    """Return the default Community source router shared by CLI, RPC, and tools."""

    global _default_router
    if _default_router is None:
        github = GitHubSource(token=os.environ.get("GITHUB_TOKEN"))
        sources: list[SkillSource] = [
            ClawHubSource(
                token=os.environ.get("CLAWHUB_TOKEN"),
                github_source=github,
            ),
            github,
        ]
        _default_router = SourceRouter(sources)
    return _default_router


def build_default_skill_installer(
    *,
    managed_dir: Path | None = None,
    loader: object | None = None,
    journal_path: Path | None = None,
    offline: bool | None = None,
) -> SkillInstaller:
    """Build the compatibility facade aligned to the active composition root."""

    return SkillInstaller(
        router=get_default_skill_router(),
        managed_dir=managed_dir,
        loader=loader,
        journal_path=journal_path,
        offline=offline,
    )


def build_default_skill_management_service(
    *,
    managed_dir: Path,
    loader: object | None = None,
    journal_path: Path | None = None,
    offline: bool = False,
    startup_recovery_diagnostics: Iterable[SkillDiagnostic] = (),
) -> SkillManagementService:
    """Build the shared service without reading GatewayConfig in core code."""

    return SkillManagementService(
        router=get_default_skill_router(),
        managed_dir=managed_dir,
        lockfile_path=default_opensquilla_home() / "skills-lock.json",
        loader=loader,
        journal_path=journal_path,
        offline=offline,
        startup_recovery_diagnostics=startup_recovery_diagnostics,
    )


def installed_skill_names() -> set[str]:
    """Return skill names recorded as Community installs in the lockfile."""

    lockfile_path = default_opensquilla_home() / "skills-lock.json"
    return set(Lockfile.load(lockfile_path).installed.keys())


def installed_skill_lockfile() -> Lockfile:
    """Load the canonical Community install identity records."""

    return Lockfile.load(default_opensquilla_home() / "skills-lock.json")
