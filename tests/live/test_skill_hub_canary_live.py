"""Opt-in live canary for the Community Skill delivery lifecycle.

The remote artifact is a repository-owned, instruction-only fixture.  This test
does not execute anything from the artifact: it fetches bytes, publishes them
through the production loader, reads the instructions and one text resource,
checks the same-revision immutable no-op path, and uninstalls the candidate.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensquilla.skills.hub.github import GitHubSource
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.loader import SkillLoader
from opensquilla.tools.builtin import skill_tools as skill_tools_module
from opensquilla.tools.registry import get_default_registry

pytestmark = pytest.mark.live_skill_hub

_CANARY_NAME = "opensquilla-live-skill-hub-canary"
_BODY_MARKER = "synthetic, instruction-only Skill"
_RESOURCE_MARKER = "opensquilla-live-skill-hub-canary-resource"


def _require_live_canary() -> str:
    if os.environ.get("OPENSQUILLA_RUN_LIVE_SKILL_HUB") != "1":
        pytest.skip("set OPENSQUILLA_RUN_LIVE_SKILL_HUB=1 to run the live Skill Hub canary")
    identifier = os.environ.get("OPENSQUILLA_LIVE_SKILL_INSTALL_REFERENCE", "").strip()
    if not identifier:
        pytest.skip("set OPENSQUILLA_LIVE_SKILL_INSTALL_REFERENCE to the exact canary fixture")
    return identifier


async def _view_skill(name: str, file_path: str | None = None) -> str:
    registered = get_default_registry().get("skill_view")
    assert registered is not None
    return await registered.handler(name=name, file_path=file_path)


@pytest.mark.asyncio
async def test_live_github_skill_install_view_immutable_noop_uninstall(tmp_path: Path) -> None:
    identifier = _require_live_canary()
    managed_dir = tmp_path / "managed"
    loader = SkillLoader(
        managed_dir=managed_dir,
        snapshot_path=tmp_path / "skills-snapshot.json",
    )
    loader.reload(force=True, reason="live-skill-hub-canary.initial")
    service = SkillManagementService(
        router=SourceRouter([GitHubSource(token=os.environ.get("GITHUB_TOKEN"))]),
        managed_dir=managed_dir,
        lockfile_path=tmp_path / "skills-lock.json",
        loader=loader,
        journal_path=tmp_path / "skill-transaction.json",
    )

    previous_loader = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader, management_service=service)
    try:
        installed = await service.install(identifier, "github")
        assert installed.success is True, installed.to_dict()
        assert installed.active is True
        assert installed.instruction_usable is True
        assert installed.effective_from == "next_turn"
        assert installed.resolution is not None
        assert installed.resolution.immutable is True
        assert len(installed.resolution.revision) == 40

        assert _BODY_MARKER in await _view_skill(_CANARY_NAME)
        assert _RESOURCE_MARKER in await _view_skill(
            _CANARY_NAME,
            "references/probe.txt",
        )

        unchanged = await service.update(_CANARY_NAME)
        assert len(unchanged) == 1
        assert unchanged[0].success is True, unchanged[0].to_dict()
        assert unchanged[0].unchanged is True
        assert any(item.code == "ALREADY_CURRENT" for item in unchanged[0].diagnostics)
        assert loader.get_by_name(_CANARY_NAME) is not None

        uninstalled = await service.uninstall(_CANARY_NAME)
        assert uninstalled.success is True, uninstalled.to_dict()
        assert loader.get_by_name(_CANARY_NAME) is None
        assert not (managed_dir / _CANARY_NAME).exists()
    finally:
        skill_tools_module._loader = previous_loader
