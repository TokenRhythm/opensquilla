from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from opensquilla.gateway import rpc_skills
from opensquilla.gateway.rpc import RpcContext
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.deps import DepResult
from opensquilla.skills.hub.installer import InstallResult
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_sha256
from opensquilla.skills.hub.management import SkillManagementService
from opensquilla.skills.hub.router import SourceRouter
from opensquilla.skills.hub.source import (
    SkillBundle,
    SkillMeta,
    SkillSource,
    SourceResolution,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.types import SkillLayer, SkillPlatformMeta, SkillRequires, SkillSpec


def _write_skill(dir_path: Path, name: str, body: str) -> None:
    skill_dir = dir_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


def _write_needs_key_skill(dir_path: Path) -> None:
    _write_skill(
        dir_path,
        "needs-key",
        """---
name: needs-key
description: Needs one of two API keys.
metadata:
  opensquilla:
    requires:
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
    install:
      - id: helper
        kind: uv
        label: Install helper
        package: helper-pkg
---

# body
""",
    )


def test_rpc_skill_install_uses_loader_managed_dir_and_list_sees_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        managed_dir = tmp_path / "managed"
        loader = SkillLoader(
            managed_dir=managed_dir,
            snapshot_path=tmp_path / "snapshot.json",
        )
        ctx = RpcContext(conn_id="test", skill_loader=loader)
        captured: dict[str, Path | None] = {}

        class FakeInstaller:
            def __init__(self, managed_dir: Path) -> None:
                self.managed_dir = managed_dir

            async def install(
                self,
                identifier: str,
                source_id: str,
                force: bool = False,
            ) -> InstallResult:
                skill_dir = self.managed_dir / identifier
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\n"
                    f"name: {identifier}\n"
                    "description: Installed from chat\n"
                    "---\n"
                    "Installed body.\n",
                    encoding="utf-8",
                )
                return InstallResult(
                    success=True,
                    name=identifier,
                    message="installed",
                    path=str(skill_dir),
                )

        def fake_builder(*, managed_dir: Path | None = None) -> FakeInstaller:
            assert managed_dir is not None
            captured["managed_dir"] = managed_dir
            return FakeInstaller(managed_dir)

        monkeypatch.setattr(rpc_skills, "build_default_skill_installer", fake_builder)
        assert await rpc_skills._handle_skills_list(None, ctx) == {"skills": []}
        assert loader._cached is not None
        rebuilds = 0
        original_build = loader._build_catalog

        def counted_build(*args, **kwargs):
            nonlocal rebuilds
            rebuilds += 1
            return original_build(*args, **kwargs)

        monkeypatch.setattr(loader, "_build_catalog", counted_build)

        installed = await rpc_skills._handle_skills_install(
            {"identifier": "plotter", "source": "clawhub"},
            ctx,
        )
        listed = await rpc_skills._handle_skills_list(None, ctx)

        assert captured["managed_dir"] == managed_dir
        assert installed["success"] is True
        assert Path(installed["path"]).name == "plotter"
        row = next(skill for skill in listed["skills"] if skill["name"] == "plotter")
        assert row["layer"] == "managed"
        assert row["description"] == "Installed from chat"
        assert rebuilds == 1

    asyncio.run(run())


@pytest.mark.asyncio
async def test_lifecycle_list_adds_tracked_shadowed_candidate_without_changing_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    _write_skill(
        managed,
        "shared-skill",
        "---\nname: shared-skill\ndescription: managed copy\n---\nManaged.\n",
    )
    _write_skill(
        workspace,
        "shared-skill",
        "---\nname: shared-skill\ndescription: workspace winner\n---\nWinner.\n",
    )
    entry = LockEntry(
        source="github",
        identifier="owner/repo:shared-skill",
        path=str(managed / "shared-skill"),
        relative_path="shared-skill",
        directory_name="shared-skill",
        manifest_name="shared-skill",
        install_id="install-1",
        resolved_identifier="owner/repo@" + "a" * 40 + ":shared-skill/SKILL.md",
        resolved_revision="a" * 40,
        artifact_sha256="artifact",
        tree_sha256=compute_sha256(managed / "shared-skill"),
        parser_version="community-strict-v1",
        dialect="instruction-first",
    )
    lockfile = Lockfile()
    lockfile.add("shared-skill", entry)
    lockfile.save(tmp_path / "skills-lock.json")
    loader = SkillLoader(
        managed_dir=managed,
        workspace_dir=workspace,
        snapshot_path=tmp_path / "snapshot.json",
    )
    loader.reload(force=True, reason="test")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    legacy = await rpc_skills._handle_skills_list(None, ctx)
    lifecycle = await rpc_skills._handle_skills_list(
        {"includeLifecycle": True},
        ctx,
    )

    assert len(legacy["skills"]) == 1
    assert legacy["skills"][0]["layer"] == "workspace"
    assert "lifecycle" not in legacy["skills"][0]
    managed_row = next(
        row for row in lifecycle["skills"] if row["layer"] == "managed"
    )
    assert managed_row["installed"] is True
    assert managed_row["active"] is False
    assert managed_row["instruction_usable"] is False
    assert managed_row["install_id"] == "install-1"
    assert managed_row["lifecycle"]["selection_state"] == "shadowed"


@pytest.mark.asyncio
async def test_skills_doctor_rpc_is_read_only_and_reports_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    _write_skill(
        managed,
        "doctor-skill",
        "---\nname: doctor-skill\ndescription: doctor test\n---\nBody.\n",
    )
    lockfile = Lockfile()
    lockfile.add(
        "doctor-skill",
        LockEntry(
            source="clawhub",
            identifier="@owner/doctor-skill",
            path=str(managed / "doctor-skill"),
            relative_path="doctor-skill",
            directory_name="doctor-skill",
            install_id="doctor-install",
            tree_sha256=compute_sha256(managed / "doctor-skill"),
            artifact_sha256="artifact",
            resolved_identifier="@owner/doctor-skill@1.0.0",
            resolved_revision="1.0.0",
            parser_version="community-strict-v1",
            dialect="instruction-first",
            extra={"degraded_capabilities": ["scoped_tool_permissions"]},
        ),
    )
    lockfile.save(tmp_path / "skills-lock.json")
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    payload = await rpc_skills._handle_skills_doctor(
        {"installId": "doctor-install"},
        ctx,
    )

    assert payload["constraints"] == {"network": False, "scripts": False, "llm": False}
    assert payload["skills"][0]["name"] == "doctor-skill"
    assert payload["skills"][0]["lifecycle"]["load_state"] == "loaded"
    assert payload["skills"][0]["lifecycle"]["compatibility_state"] == "degraded"
    assert payload["skills"][0]["instruction_usable"] is True
    assert payload["skills"][0]["lifecycle"]["invocation"]["scoped_tool_permissions"] is False
    assert "TOOL_PREAPPROVAL_IGNORED" in {
        item["code"] for item in payload["skills"][0]["diagnostics"]
    }


@pytest.mark.asyncio
async def test_skills_doctor_preserves_startup_recovery_failure_without_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    managed.mkdir()
    diagnostic = SkillDiagnostic(
        code="RECOVERY_REQUIRED",
        severity=DiagnosticSeverity.ERROR,
        phase=DiagnosticPhase.STORE,
        message="Interrupted Skill transaction requires operator recovery",
        blocking=True,
        path=str(tmp_path / "skill-transaction.json"),
    )
    ctx = RpcContext(
        conn_id="test",
        skill_loader=None,
        skill_management_state={
            "managed_dir": managed,
            "journal_path": tmp_path / "skill-transaction.json",
            "recovery_diagnostics": (diagnostic,),
        },
    )

    payload = await rpc_skills._handle_skills_doctor(None, ctx)

    assert payload["ok"] is False
    assert any(item["code"] == "RECOVERY_REQUIRED" for item in payload["diagnostics"])
    assert all(item["code"] != "MANAGED_ROOT_UNAVAILABLE" for item in payload["diagnostics"])

    install = await rpc_skills._handle_skills_install(
        {"identifier": "@alice/demo", "source": "clawhub"},
        ctx,
    )
    update = await rpc_skills._handle_skills_update({"name": "demo"}, ctx)
    uninstall = await rpc_skills._handle_skills_uninstall({"name": "demo"}, ctx)

    for mutation in (install, update, uninstall):
        assert mutation["success"] is False
        assert mutation["message"] == "Managed Skill store requires recovery before mutation"
        assert mutation["effectiveFrom"] == ""
        assert [item["code"] for item in mutation["diagnostics"]] == [
            "RECOVERY_REQUIRED"
        ]
    assert update["results"] == []


@pytest.mark.asyncio
async def test_runtime_management_recovery_failure_reaches_doctor_and_blocks_rpc_mutation(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    _write_skill(
        managed,
        "demo",
        "---\nname: demo\ndescription: existing version\n---\nBody.\n",
    )
    lock_path = tmp_path / "runtime-skills-lock.json"
    lockfile = Lockfile()
    lockfile.add(
        "demo",
        LockEntry(
            source="fake",
            identifier="demo",
            path=str(managed / "demo"),
            sha256=compute_sha256(managed / "demo"),
            install_id="install-demo",
        ),
    )
    lockfile.save(lock_path)
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.runtime-recovery")
    diagnostic = SkillDiagnostic(
        code="RECOVERY_REQUIRED",
        severity=DiagnosticSeverity.ERROR,
        phase=DiagnosticPhase.STORE,
        message="Runtime rollback could not restore the managed store",
        blocking=True,
    )

    service = SkillManagementService(
        router=SourceRouter([]),
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        journal_path=tmp_path / "transaction.json",
    )
    service._observe_recovery([diagnostic])

    ctx = RpcContext(
        conn_id="test",
        skill_loader=loader,
        skill_management_service=service,
        skill_management_state={
            "managed_dir": managed,
            "recovery_diagnostics": (diagnostic,),
        },
    )

    doctor = await rpc_skills._handle_skills_doctor(None, ctx)
    install = await rpc_skills._handle_skills_install(
        {"identifier": "demo", "source": "fake"},
        ctx,
    )
    update = await rpc_skills._handle_skills_update({"name": "demo"}, ctx)
    uninstall = await rpc_skills._handle_skills_uninstall({"name": "demo"}, ctx)

    assert doctor["ok"] is False
    assert any(item["code"] == "RECOVERY_REQUIRED" for item in doctor["diagnostics"])
    mutations = [install, update["results"][0], uninstall]
    for mutation in mutations:
        assert mutation["success"] is False
        assert mutation["installed"] is True
        assert mutation["path"] == str(managed / "demo")
        assert mutation["installId"] == "install-demo"
        assert mutation["lifecycle"]["install_state"] == "tracked"
        assert mutation["lifecycle"]["load_state"] == "loaded"
        assert mutation["effectiveFrom"] == ""
        assert [item["code"] for item in mutation["diagnostics"]] == [
            "RECOVERY_REQUIRED"
        ]


@pytest.mark.asyncio
async def test_two_concurrent_install_rpcs_share_one_transaction_writer(
    tmp_path: Path,
) -> None:
    class RpcFixtureSource(SkillSource):
        @property
        def source_id(self) -> str:
            return "rpc-fixture"

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
                revision=f"revision-{identifier}",
                package_identifier=identifier,
                expected_digest=f"artifact-{identifier}",
                publisher="fixture",
                version="1.0.0",
                trust_state="community",
                meta=SkillMeta(
                    name=identifier,
                    description=f"Concurrent {identifier}",
                    source_id=self.source_id,
                ),
            )

        async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle:
            name = resolution.requested_identifier
            return SkillBundle(
                name=name,
                files={
                    "SKILL.md": (
                        f"---\nname: {name}\n"
                        f"description: Concurrent {name}\n---\nBody.\n"
                    )
                },
                meta=resolution.meta,
                resolution=resolution,
            )

        async def fetch(self, identifier: str) -> SkillBundle | None:
            return await self.fetch_resolved(await self.resolve(identifier))

        async def inspect(self, identifier: str) -> SkillMeta | None:
            return None

    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.concurrent-rpc.initial")
    service = SkillManagementService(
        router=SourceRouter([RpcFixtureSource()]),
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        journal_path=tmp_path / "transaction.json",
    )
    ctx = RpcContext(
        conn_id="test",
        skill_loader=loader,
        skill_management_service=service,
    )

    first, second = await asyncio.gather(
        rpc_skills._handle_skills_install(
            {"identifier": "skill-a", "source": "rpc-fixture"},
            ctx,
        ),
        rpc_skills._handle_skills_install(
            {"identifier": "skill-b", "source": "rpc-fixture"},
            ctx,
        ),
    )

    assert first["success"] is True
    assert second["success"] is True
    lockfile = Lockfile.load(lock_path)
    assert set(lockfile.installed) == {"skill-a", "skill-b"}
    assert loader.get_by_name("skill-a") is not None
    assert loader.get_by_name("skill-b") is not None
    assert not (tmp_path / "transaction.json").exists()


@pytest.mark.asyncio
async def test_lifecycle_rpcs_wait_for_one_committed_store_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingFixtureSource(SkillSource):
        @property
        def source_id(self) -> str:
            return "blocking-fixture"

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
                revision="fixture-revision-1",
                package_identifier=identifier,
                expected_digest="fixture-artifact-1",
                publisher="fixture",
                version="1.0.0",
                trust_state="community",
                meta=SkillMeta(
                    name=identifier,
                    description="Pending lifecycle fixture",
                    source_id=self.source_id,
                ),
            )

        async def fetch_resolved(self, resolution: SourceResolution) -> SkillBundle:
            name = resolution.requested_identifier
            return SkillBundle(
                name=name,
                files={
                    "SKILL.md": (
                        f"---\nname: {name}\n"
                        "description: Pending lifecycle fixture\n---\nBody.\n"
                    )
                },
                meta=resolution.meta,
                resolution=resolution,
            )

        async def fetch(self, identifier: str) -> SkillBundle | None:
            return await self.fetch_resolved(await self.resolve(identifier))

        async def inspect(self, identifier: str) -> SkillMeta | None:
            return None

    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.lifecycle-rpc.initial")
    service = SkillManagementService(
        router=SourceRouter([BlockingFixtureSource()]),
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        journal_path=tmp_path / "transaction.json",
    )
    ctx = RpcContext(
        conn_id="test",
        skill_loader=loader,
        skill_management_service=service,
    )
    original_reload = loader.reload_verified
    reload_entered = threading.Event()
    release_reload = threading.Event()

    def blocked_reload(verifier, *args, **kwargs):
        reload_entered.set()
        if not release_reload.wait(timeout=5):
            raise TimeoutError("test did not release the verified catalog reload")
        return original_reload(verifier, *args, **kwargs)

    monkeypatch.setattr(loader, "reload_verified", blocked_reload)
    install_task = asyncio.create_task(
        rpc_skills._handle_skills_install(
            {"identifier": "pending-skill", "source": "blocking-fixture"},
            ctx,
        )
    )
    assert await asyncio.to_thread(reload_entered.wait, 5)
    assert (managed / "pending-skill" / "SKILL.md").exists()
    assert Lockfile.load(lock_path).get("pending-skill") is not None
    assert loader.snapshot().get_by_name("pending-skill") is None

    # The legacy winner-only surface remains on the publication-barrier
    # baseline and does not wait for lifecycle disk/lock consistency.
    legacy = await asyncio.wait_for(
        rpc_skills._handle_skills_list(None, ctx),
        timeout=1,
    )
    assert all(row["name"] != "pending-skill" for row in legacy["skills"])

    lifecycle_task = asyncio.create_task(
        rpc_skills._handle_skills_list({"includeLifecycle": True}, ctx)
    )
    detail_task = asyncio.create_task(
        rpc_skills._handle_skills_get(
            {"name": "pending-skill", "includeLifecycle": True},
            ctx,
        )
    )
    doctor_task = asyncio.create_task(
        rpc_skills._handle_skills_doctor({"name": "pending-skill"}, ctx)
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not lifecycle_task.done()
    assert not detail_task.done()
    assert not doctor_task.done()

    release_reload.set()
    installed, lifecycle, detail, doctor = await asyncio.gather(
        install_task,
        lifecycle_task,
        detail_task,
        doctor_task,
    )

    assert installed["success"] is True
    lifecycle_row = next(
        row for row in lifecycle["skills"] if row["name"] == "pending-skill"
    )
    assert lifecycle_row["installed"] is True
    assert lifecycle_row["lifecycle"]["load_state"] == "loaded"
    assert detail["installed"] is True
    assert detail["lifecycle"]["load_state"] == "loaded"
    assert doctor["skills"][0]["name"] == "pending-skill"
    assert doctor["skills"][0]["lifecycle"]["load_state"] == "loaded"


@pytest.mark.asyncio
async def test_rpc_skills_list_exposes_dependency_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    managed_dir = tmp_path / "managed"
    _write_needs_key_skill(managed_dir)
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    listed = await rpc_skills._handle_skills_list(None, ctx)

    row = next(skill for skill in listed["skills"] if skill["name"] == "needs-key")
    assert row["status"] == "needs_setup"
    assert row["eligible"] is False
    assert row["dependency_summary"]["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert row["dependency_summary"]["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert row["missing_env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert "OPENROUTER_API_KEY or ARK_API_KEY" in row["status_detail"]


@pytest.mark.asyncio
async def test_rpc_skills_status_exposes_dependency_summary_and_legacy_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    managed_dir = tmp_path / "managed"
    _write_needs_key_skill(managed_dir)
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    status_rows = await rpc_skills._handle_skills_status(None, ctx)

    row = next(skill for skill in status_rows if skill["name"] == "needs-key")
    assert row["status"] == "needs_setup"
    assert row["install"] == [
        {
            "id": "helper",
            "kind": "uv",
            "label": "Install helper",
            "bins": [],
        }
    ]
    assert row["dependency_summary"]["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert row["dependency_summary"]["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert row["missing_env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert row["missing_env"] == []
    assert row["missing_bins"] == []


@pytest.mark.asyncio
async def test_rpc_skills_get_exposes_dependency_summary_and_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    managed_dir = tmp_path / "managed"
    _write_needs_key_skill(managed_dir)
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    result = await rpc_skills._handle_skills_get({"name": "needs-key"}, ctx)

    assert result["name"] == "needs-key"
    assert result["status"] == "needs_setup"
    assert result["install"] == [
        {
            "id": "helper",
            "kind": "uv",
            "label": "Install helper",
            "bins": [],
        }
    ]
    assert result["dependency_summary"]["declared"]["api_env"]["any"] == [
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
    ]
    assert result["dependency_summary"]["missing"]["api_env"]["any"] == [
        ["OPENROUTER_API_KEY", "ARK_API_KEY"]
    ]
    assert result["missing_env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert result["content"] == "# body"
    assert Path(result["file_path"]).name == "SKILL.md"
    assert Path(result["base_dir"]).name == "needs-key"


@pytest.mark.asyncio
async def test_rpc_skills_deps_install_reports_env_any_missing_still(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    _write_skill(
        tmp_path,
        "env-any-install",
        """---
name: env-any-install
description: Has install metadata but still needs one API key.
metadata:
  opensquilla:
    requires:
      envAny: [OPENROUTER_API_KEY, ARK_API_KEY]
    install:
      - id: helper
        kind: uv
        label: Install helper
        package: helper-pkg
---

# body
""",
    )
    loader = SkillLoader(bundled_dir=tmp_path, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    from opensquilla.engine.steps import skills_filter

    skills_filter._elig_ctx.has_bin_cache["helper"] = False
    skills_filter._elig_ctx.env_cache["HELPER_TOKEN"] = None

    async def fake_install_deps(_specs: list[object]) -> list[DepResult]:
        return [DepResult(kind="uv", identifier="helper", success=True, message="Installed")]

    monkeypatch.setattr(rpc_skills, "install_deps", fake_install_deps)

    result = await rpc_skills._handle_skills_deps_install(
        {"name": "env-any-install", "install_id": "helper"},
        ctx,
    )

    assert result["success"] is True
    assert result["missing_still"]["env_any"] == [["OPENROUTER_API_KEY", "ARK_API_KEY"]]
    assert loader._dirty is False
    assert skills_filter._elig_ctx.has_bin_cache == {}
    assert skills_filter._elig_ctx.env_cache == {}


@pytest.mark.asyncio
async def test_rpc_skills_deps_install_uses_exact_shadowed_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    for directory, action in (("first", "first-action"), ("second", "second-action")):
        _write_skill(
            managed,
            directory,
            f"""---
name: shared-runtime
description: {directory} package
metadata:
  opensquilla:
    install:
      - id: {action}
        kind: uv
        package: {directory}-package
---
Instructions.
""",
        )
    lockfile = Lockfile()
    for directory, install_identity in (("first", "install-first"), ("second", "install-second")):
        target = managed / directory
        lockfile.add(
            directory,
            LockEntry(
                source="github",
                identifier=f"acme/{directory}:skills/{directory}",
                install_id=install_identity,
                manifest_name="shared-runtime",
                directory_name=directory,
                relative_path=directory,
                path=str(target),
                sha256=compute_sha256(target),
                parser_version="community-instruction-v1",
                dialect="instruction-first",
            ),
        )
    lockfile.save(tmp_path / "skills-lock.json")
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.exact-deps")
    ctx = RpcContext(conn_id="test", skill_loader=loader)
    observed: list[str] = []

    async def fake_install_deps(specs: list[object]) -> list[DepResult]:
        observed.append(str(getattr(specs[0], "id", "")))
        return [DepResult(kind="uv", identifier="second-action", success=True)]

    monkeypatch.setattr(rpc_skills, "install_deps", fake_install_deps)

    result = await rpc_skills._handle_skills_deps_install(
        {
            "name": "shared-runtime",
            "installId": "install-second",
            "install_id": "second-action",
        },
        ctx,
    )

    assert result["success"] is True
    assert observed == ["second-action"]

    current = Lockfile.load(tmp_path / "skills-lock.json")
    current.add(
        "missing",
        LockEntry(
            source="github",
            install_id="install-missing",
            manifest_name="shared-runtime",
            relative_path="missing",
            parser_version="community-instruction-v1",
        ),
    )
    current.save(tmp_path / "skills-lock.json")
    with pytest.raises(KeyError, match="not loaded"):
        await rpc_skills._handle_skills_deps_install(
            {
                "name": "shared-runtime",
                "installId": "install-missing",
                "install_id": "first-action",
            },
            ctx,
        )
    assert observed == ["second-action"]


@pytest.mark.asyncio
async def test_exact_rpc_identity_rejects_duplicate_install_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    managed = tmp_path / "managed"
    _write_needs_key_skill(managed)
    _write_skill(
        managed,
        "other-skill",
        """---
name: other-skill
description: Other duplicate identity fixture.
metadata:
  opensquilla:
    install:
      - id: helper
        kind: uv
        label: Install helper
        package: other-helper
---
Body.
""",
    )
    Lockfile(
        installed={
            name: LockEntry(
                source="github",
                identifier=f"owner/repo:{name}",
                relative_path=name,
                directory_name=name,
                manifest_name=name,
                install_id="duplicate-install",
                tree_sha256=compute_sha256(managed / name),
            )
            for name in ("needs-key", "other-skill")
        }
    ).save(tmp_path / "skills-lock.json")
    loader = SkillLoader(managed_dir=managed, snapshot_path=tmp_path / "snapshot.json")
    loader.reload(force=True, reason="test.duplicate-install-id")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    with pytest.raises(KeyError, match="identity is ambiguous"):
        await rpc_skills._handle_skills_get(
            {"installId": "duplicate-install"},
            ctx,
        )
    with pytest.raises(KeyError, match="identity is ambiguous"):
        await rpc_skills._handle_skills_deps_install(
            {"installId": "duplicate-install", "install_id": "helper"},
            ctx,
        )


def test_skill_payload_rolls_up_meta_subskill_requirements() -> None:
    python_skill = SkillSpec(
        name="docx",
        description="Docx export",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        metadata=SkillPlatformMeta(requires=SkillRequires(any_bins=["python", "python3"])),
    )
    ffmpeg_skill = SkillSpec(
        name="video-merger",
        description="Video merge",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        metadata=SkillPlatformMeta(requires=SkillRequires(bins=["ffmpeg", "ffprobe"])),
    )
    meta_skill = SkillSpec(
        name="meta-demo",
        description="Meta demo",
        layer=SkillLayer.BUNDLED,
        always=False,
        triggers=[],
        content="",
        kind="meta",
        composition_raw={
            "steps": [
                {"id": "export", "kind": "skill_exec", "skill": "docx"},
                {"id": "merge", "kind": "skill_exec", "skill": "video-merger"},
            ]
        },
    )

    ctx = rpc_skills.EligibilityContext.auto()
    ctx.has_bin_cache.update(
        {"python": True, "python3": True, "ffmpeg": False, "ffprobe": False}
    )
    skill_index = {s.name: s for s in (meta_skill, python_skill, ffmpeg_skill)}
    payload = rpc_skills._skill_to_dict(
        meta_skill,
        rpc_skills.diagnose_eligibility(meta_skill, ctx),
        ctx.os_name,
        skill_index=skill_index,
        eligibility_ctx=ctx,
    )

    assert payload["requirements"]["summary"] == "needs_setup"
    assert payload["requirements"]["items"] == [
        {
            "name": "docx",
            "source": "sub_skill",
            "status": "ready",
            "requires_bins": [],
            "requires_any_bins": ["python", "python3"],
            "requires_env": [],
            "missing_bins": [],
            "missing_env": [],
        },
        {
            "name": "video-merger",
            "source": "sub_skill",
            "status": "needs_setup",
            "requires_bins": ["ffmpeg", "ffprobe"],
            "requires_any_bins": [],
            "requires_env": [],
            "missing_bins": ["ffmpeg", "ffprobe"],
            "missing_env": [],
        },
    ]


def test_meta_paper_write_declares_pdf_compile_binaries() -> None:
    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(bundled_dir=bundled)
    skills = loader.load_all()
    skill_index = {skill.name: skill for skill in skills}
    ctx = rpc_skills.EligibilityContext.auto()
    spec = skill_index["meta-paper-write"]
    payload = rpc_skills._skill_to_dict(
        spec,
        rpc_skills.diagnose_eligibility(spec, ctx),
        ctx.os_name,
        skill_index=skill_index,
        eligibility_ctx=ctx,
    )

    own_requirements = next(
        item for item in payload["requirements"]["items"] if item["source"] == "self"
    )
    assert own_requirements["requires_bins"] == ["xelatex", "bibtex"]


def test_meta_payload_marks_only_trusted_provider_backed_plans_for_launch_check() -> None:
    bundled = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "opensquilla"
        / "skills"
        / "bundled"
    )
    loader = SkillLoader(bundled_dir=bundled)
    skills = loader.load_all()
    skill_index = {skill.name: skill for skill in skills}
    ctx = rpc_skills.EligibilityContext.auto()

    def payload(name: str) -> dict[str, object]:
        spec = skill_index[name]
        return rpc_skills._skill_to_dict(
            spec,
            rpc_skills.diagnose_eligibility(spec, ctx),
            ctx.os_name,
            skill_index=skill_index,
            eligibility_ctx=ctx,
        )

    assert payload("meta-short-drama")["provider_check_at_launch"] is True
    assert payload("AwesomeWebpageMetaSkill")["provider_check_at_launch"] is True
    assert payload("meta-paper-write")["provider_check_at_launch"] is False
    assert payload("meta-skill-creator")["provider_check_at_launch"] is False


@pytest.mark.asyncio
async def test_rpc_skills_list_exposes_meta_skill_dependency_rollup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    managed_dir = tmp_path / "managed"
    _write_skill(
        managed_dir,
        "child-needs-bin",
        """---
name: child-needs-bin
description: Child skill requiring a missing binary.
metadata:
  opensquilla:
    requires:
      bins: [missing-child-tool]
---

# body
""",
    )
    _write_skill(
        managed_dir,
        "parent-meta",
        """---
name: parent-meta
description: Meta skill referencing a child.
kind: meta
composition:
  steps:
    - id: child
      skill: child-needs-bin
---

# body
""",
    )
    loader = SkillLoader(managed_dir=managed_dir, snapshot_path=tmp_path / "snapshot.json")
    ctx = RpcContext(conn_id="test", skill_loader=loader)

    listed = await rpc_skills._handle_skills_list(None, ctx)

    row = next(skill for skill in listed["skills"] if skill["name"] == "parent-meta")
    assert row["dependency_summary"]["sub_skill_dependencies"]["missing_count"] == 1
