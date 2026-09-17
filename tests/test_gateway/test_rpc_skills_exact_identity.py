from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.gateway import rpc_skills
from opensquilla.gateway.rpc import RpcContext
from opensquilla.skills.hub.contracts import DiagnosticPhase
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_tree_sha256
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import SkillMeta, SkillSourceFetchError
from opensquilla.skills.loader import SkillLoader


def _write_skill(root: Path, directory: str, description: str, body: str) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: shared-skill\n"
        f"description: {description}\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_candidate_path_matching_uses_platform_case_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Candidate:
        base_dir = str(tmp_path / "Managed" / "Community-Skill")

    monkeypatch.setattr(
        rpc_skills.os.path,
        "normcase",
        lambda value: value.casefold(),
    )

    candidate = Candidate()
    matched = rpc_skills._candidate_by_path(
        (candidate,),
        candidate.base_dir.swapcase(),
    )

    assert matched is candidate


@pytest.mark.asyncio
async def test_registry_search_marks_only_exact_source_package_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        async def search(self, query, limit=20, source_id=None):
            assert (query, limit, source_id) == ("plot", 20, None)
            return [
                SkillMeta(
                    name="Same Display Name",
                    source_id="clawhub",
                    identifier="@alice/plotter",
                    canonical_identifier="@alice/plotter",
                ),
                SkillMeta(
                    name="Same Display Name",
                    source_id="clawhub",
                    identifier="@bob/plotter",
                    canonical_identifier="@bob/plotter",
                ),
            ]

    lockfile = Lockfile(
        installed={
            "plotter": LockEntry(
                source="clawhub",
                identifier="@alice/plotter",
                source_package_id="clawhub:@alice/plotter",
            )
        }
    )
    monkeypatch.setattr(rpc_skills, "installed_skill_lockfile", lambda: lockfile)
    ctx = RpcContext(conn_id="test")
    ctx._skill_router = FakeRouter()  # type: ignore[attr-defined]

    response = await rpc_skills._handle_skills_search({"query": "plot"}, ctx)

    assert [row["installed"] for row in response["results"]] == [True, False]


@pytest.mark.asyncio
async def test_registry_search_casefolds_github_repository_but_not_subpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRouter:
        async def search(self, query, limit=20, source_id=None):
            return [
                SkillMeta(
                    name="Exact path",
                    source_id="github",
                    identifier="openai/repo:Skills/Demo",
                    canonical_identifier="openai/repo:Skills/Demo",
                ),
                SkillMeta(
                    name="Different path case",
                    source_id="github",
                    identifier="openai/repo:skills/demo",
                    canonical_identifier="openai/repo:skills/demo",
                ),
            ]

    lockfile = Lockfile(
        installed={
            "demo": LockEntry(
                source="github",
                identifier="OpenAI/Repo:Skills/Demo",
                source_package_id="github:OpenAI/Repo:Skills/Demo",
            )
        }
    )
    monkeypatch.setattr(rpc_skills, "installed_skill_lockfile", lambda: lockfile)
    ctx = RpcContext(conn_id="test")
    ctx._skill_router = FakeRouter()  # type: ignore[attr-defined]

    response = await rpc_skills._handle_skills_search({"query": "demo"}, ctx)

    assert [row["installed"] for row in response["results"]] == [True, False]


@pytest.mark.asyncio
async def test_registry_search_uses_injected_management_router_and_lockfile(
    tmp_path: Path,
) -> None:
    class FakeRouter:
        async def search(self, query, limit=20, source_id=None):
            assert (query, limit, source_id) == ("plot", 20, None)
            return [
                SkillMeta(
                    name="Same Display Name",
                    source_id="clawhub",
                    identifier="@alice/plotter",
                    canonical_identifier="@alice/plotter",
                )
            ]

    custom_lock = tmp_path / "custom-skills-lock.json"
    Lockfile(
        installed={
            "plotter": LockEntry(
                source="clawhub",
                identifier="@alice/plotter",
                source_package_id="clawhub:@alice/plotter",
            )
        }
    ).save(custom_lock)
    service = SkillManagementService(
        router=FakeRouter(),  # type: ignore[arg-type]
        managed_dir=tmp_path / "managed",
        lockfile_path=custom_lock,
        journal_path=tmp_path / "transaction.json",
        offline=True,
    )
    ctx = RpcContext(conn_id="test", skill_management_service=service)

    response = await rpc_skills._handle_skills_search({"query": "plot"}, ctx)

    assert response["results"][0]["installed"] is True


@pytest.mark.asyncio
async def test_registry_search_preserves_results_container_with_source_diagnostics() -> None:
    class FailingRouter:
        async def search(self, query, limit=20, source_id=None):
            assert (query, limit, source_id) == ("weather", 20, "clawhub")
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_TRANSPORT_FAILED",
                "Could not reach ClawHub for the Skill request.",
                phase=DiagnosticPhase.SOURCE,
                hint="Check network connectivity, then retry.",
            )

    ctx = RpcContext(conn_id="test")
    ctx._skill_router = FailingRouter()  # type: ignore[attr-defined]

    response = await rpc_skills._handle_skills_search(
        {"query": "weather", "source": "clawhub"},
        ctx,
    )

    assert response["results"] == []
    assert response["diagnostics"] == [
        {
            "code": "SOURCE_TRANSPORT_FAILED",
            "severity": "error",
            "phase": "source",
            "message": "Could not reach ClawHub for the Skill request.",
            "blocking": True,
            "path": "",
            "field_name": "",
            "hint": "Check network connectivity, then retry.",
            "details": {"source": "clawhub"},
        }
    ]
    assert response["partial"] is False
    assert response["allSourcesUnavailable"] is True


@pytest.mark.asyncio
async def test_registry_search_returns_partial_results_with_source_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HealthySource:
        source_id = "healthy"

        async def search(self, query, limit=20):
            return [SkillMeta(name="Plotter", identifier="plotter")]

    class LimitedSource:
        source_id = "limited"

        async def search(self, query, limit=20):
            raise SkillSourceFetchError.diagnostic(
                "SOURCE_RATE_LIMITED",
                "The source rate-limited this search.",
                phase=DiagnosticPhase.SOURCE,
                details={"retryAfter": "45"},
            )

    monkeypatch.setattr(rpc_skills, "installed_skill_lockfile", lambda: Lockfile())
    ctx = RpcContext(conn_id="test")
    ctx._skill_router = SourceRouter(  # type: ignore[attr-defined]
        [HealthySource(), LimitedSource()]  # type: ignore[list-item]
    )

    response = await rpc_skills._handle_skills_search({"query": "plot"}, ctx)

    assert [row["name"] for row in response["results"]] == ["Plotter"]
    assert response["diagnostics"][0]["code"] == "SOURCE_RATE_LIMITED"
    assert response["diagnostics"][0]["details"] == {
        "source": "limited",
        "retryAfter": "45",
    }
    assert response["partial"] is True
    assert response["allSourcesUnavailable"] is False


@pytest.mark.asyncio
async def test_lifecycle_identity_opens_shadowed_candidate_not_bare_name_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    _write_skill(managed, "managed-copy", "managed copy", "Managed exact body.")
    _write_skill(workspace, "workspace-copy", "workspace winner", "Workspace winner body.")

    lockfile = Lockfile()
    lockfile.add(
        "shared-skill",
        LockEntry(
            source="github",
            identifier="owner/repo:managed-copy",
            path=str(managed / "managed-copy"),
            relative_path="managed-copy",
            directory_name="managed-copy",
            manifest_name="shared-skill",
            install_id="install-managed",
            resolved_identifier=f"owner/repo@{'a' * 40}:managed-copy/SKILL.md",
            resolved_revision="a" * 40,
            artifact_sha256="artifact",
            tree_sha256=compute_tree_sha256(managed / "managed-copy"),
            parser_version="community-strict-v1",
            dialect="instruction-first",
        ),
    )
    lockfile.save(tmp_path / "skills-lock.json")
    loader = SkillLoader(
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.reload(force=True, reason="test")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    legacy_detail = await rpc_skills._handle_skills_get({"name": "shared-skill"}, ctx)
    lifecycle = await rpc_skills._handle_skills_list({"includeLifecycle": True}, ctx)
    managed_row = next(row for row in lifecycle["skills"] if row["layer"] == "managed")
    workspace_row = next(row for row in lifecycle["skills"] if row["layer"] == "workspace")

    assert legacy_detail["description"] == "workspace winner"
    assert "instance_id" not in legacy_detail
    assert managed_row["instance_id"]
    assert workspace_row["instance_id"]
    assert managed_row["instance_id"] != workspace_row["instance_id"]

    exact = await rpc_skills._handle_skills_get(
        {
            "name": "shared-skill",
            "instanceId": managed_row["instance_id"],
            "installId": managed_row["install_id"],
            "includeLifecycle": True,
        },
        ctx,
    )

    assert exact["description"] == "managed copy"
    assert exact["content"] == "Managed exact body."
    assert exact["instance_id"] == managed_row["instance_id"]
    assert exact["install_id"] == "install-managed"
    assert exact["lifecycle"]["selection_state"] == "shadowed"


@pytest.mark.asyncio
async def test_skills_get_exact_identity_can_omit_name_and_rejects_mixed_identity(
    tmp_path: Path,
) -> None:
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "first", "first", "First body.")
    first_dir = bundled / "first"
    second_dir = bundled / "second"
    second_dir.mkdir(parents=True)
    (second_dir / "SKILL.md").write_text(
        "---\nname: second-skill\ndescription: second\n---\nSecond body.\n",
        encoding="utf-8",
    )
    loader = SkillLoader(bundled_dir=bundled, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test")
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    candidates = loader.snapshot().candidates
    first = next(item for item in candidates if Path(item.base_dir) == first_dir)
    second = next(item for item in candidates if Path(item.base_dir) == second_dir)

    detail = await rpc_skills._handle_skills_get({"instanceId": first.instance_id}, ctx)

    assert detail["name"] == "shared-skill"
    assert detail["content"] == "First body."
    assert detail["instance_id"] == first.instance_id
    with pytest.raises(KeyError, match="does not match name"):
        await rpc_skills._handle_skills_get(
            {"name": "second-skill", "instanceId": first.instance_id},
            ctx,
        )
    with pytest.raises(KeyError, match="not found"):
        await rpc_skills._handle_skills_get({"instanceId": "bundled:missing"}, ctx)
    assert first.instance_id != second.instance_id


@pytest.mark.asyncio
async def test_install_identity_returns_rejected_doctor_item_not_same_name_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    bundled = tmp_path / "bundled"
    managed = tmp_path / "managed"
    _write_skill(bundled, "winner", "bundled winner", "Winner body.")
    rejected = managed / "rejected"
    rejected.mkdir(parents=True)
    (rejected / "SKILL.md").write_text("not valid frontmatter\n", encoding="utf-8")
    lockfile = Lockfile()
    lockfile.add(
        "shared-skill",
        LockEntry(
            source="clawhub",
            identifier="@owner/shared-skill",
            path=str(rejected),
            relative_path="rejected",
            directory_name="rejected",
            manifest_name="shared-skill",
            install_id="install-rejected",
            artifact_sha256="artifact",
            tree_sha256=compute_tree_sha256(rejected),
            parser_version="community-strict-v1",
            dialect="instruction-first",
        ),
    )
    lockfile.save(tmp_path / "skills-lock.json")
    loader = SkillLoader(
        bundled_dir=bundled,
        managed_dir=managed,
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.reload(force=True, reason="test")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    winner = await rpc_skills._handle_skills_get({"name": "shared-skill"}, ctx)
    rejected_detail = await rpc_skills._handle_skills_get(
        {
            "name": "shared-skill",
            "installId": "install-rejected",
            "includeLifecycle": True,
        },
        ctx,
    )

    assert winner["description"] == "bundled winner"
    assert rejected_detail["install_id"] == "install-rejected"
    assert rejected_detail["instance_id"] == ""
    assert rejected_detail["content"] == ""
    assert rejected_detail["lifecycle"]["load_state"] == "rejected"
