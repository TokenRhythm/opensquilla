from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.skills.eligibility import EligibilityContext
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillCompatibilityState,
    SkillDiagnostic,
    SkillInstallState,
    SkillLoadState,
    SkillReadinessState,
    SkillSelectionState,
)
from opensquilla.skills.hub.doctor import SkillDoctor, doctor
from opensquilla.skills.hub.lockfile import (
    LockEntry,
    Lockfile,
    compute_sha256,
    compute_tree_sha256,
)
from opensquilla.skills.hub.transaction import (
    SkillTransactionJournal,
    rollback_root,
    staging_root,
)
from opensquilla.skills.loader import SkillLoader

_V1_WRITER_ENTRY_FIELDS = {
    "source",
    "identifier",
    "version",
    "installed_at",
    "path",
    "sha256",
    "license",
    "upstream_url",
    "source_trust",
    "scan_verdict",
    "scan_strategy",
    "scan_findings",
}


def _rewrite_v2_with_v1_field_filter(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps(
            {
                "version": payload["version"],
                "installed": {
                    storage_key: {
                        key: value
                        for key, value in entry.items()
                        if key in _V1_WRITER_ENTRY_FIELDS
                    }
                    for storage_key, entry in payload["installed"].items()
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_skill(
    root: Path,
    *,
    name: str = "demo",
    metadata: str = "",
    extra_frontmatter: str = "",
) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Synthetic Doctor fixture\n"
        f"{extra_frontmatter}"
        f"{metadata}"
        "---\n"
        "Follow these instructions without running third-party setup.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_lock(
    path: Path,
    skill_dir: Path,
    *,
    name: str = "demo",
    install_id: str = "install-demo",
    dialect: str = "agent-skills",
    extra: dict[str, object] | None = None,
) -> None:
    digest = compute_tree_sha256(skill_dir)
    Lockfile(
        installed={
            name: LockEntry(
                source="github",
                identifier="owner/repo:demo",
                requested_identifier="owner/repo:demo@main",
                resolved_identifier="owner/repo:demo",
                resolved_version="1.2.3",
                resolved_revision="a" * 40,
                artifact_sha256="b" * 64,
                tree_sha256=digest,
                sha256=digest,
                install_id=install_id,
                manifest_name=name,
                directory_name=name,
                relative_path=name,
                dialect=dialect,
                extra=dict(extra or {}),
            )
        }
    ).save(path)


def test_doctor_reports_ignored_tool_preapproval_as_usable_degradation(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(
        managed,
        extra_frontmatter="allowed-tools: Bash(npx example@latest *)\n",
    )
    _write_lock(
        lock_path,
        skill_dir,
        extra={
            "degraded_capabilities": [
                "dynamic_context",
                "scoped_tool_permissions",
            ]
        },
    )
    loader = _loader(tmp_path, managed)
    loader.load_all()

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    item = report.skills[0]
    assert item.lifecycle.compatibility_state is SkillCompatibilityState.DEGRADED
    assert item.lifecycle.invocation.scoped_tool_permissions is False
    assert item.instruction_usable is True
    diagnostic = next(
        item for item in item.diagnostics if item.code == "TOOL_PREAPPROVAL_IGNORED"
    )
    assert diagnostic.blocking is False
    assert diagnostic.severity is DiagnosticSeverity.WARNING
    assert diagnostic.phase is DiagnosticPhase.COMPATIBILITY
    dynamic_diagnostic = next(
        item for item in item.diagnostics if item.code == "DYNAMIC_CONTEXT_UNSUPPORTED"
    )
    assert dynamic_diagnostic.blocking is False
    assert dynamic_diagnostic.field_name == "body.dynamic-context"


def test_doctor_reports_v2_identity_loss_without_disabling_instruction_projection(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    storage_key = "package-storage"
    runtime_name = "runtime-demo"
    skill_dir = managed / storage_key
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {runtime_name}\n"
        "description: Synthetic rollback Doctor fixture\n"
        "---\n"
        "Portable instructions.\n",
        encoding="utf-8",
    )
    digest = compute_tree_sha256(skill_dir)
    legacy_digest = compute_sha256(skill_dir)
    lock_path = tmp_path / "skills-lock.json"
    Lockfile(
        installed={
            storage_key: LockEntry(
                source="clawhub",
                identifier="demo",
                path=str(skill_dir),
                sha256=legacy_digest,
                tree_sha256=digest,
                install_id="install-demo",
                manifest_name=runtime_name,
                relative_path=storage_key,
                requested_identifier="demo",
                resolved_identifier="@verified-owner/demo@2.0.0",
                resolved_revision="2.0.0",
                source_package_id="clawhub:@verified-owner/demo",
                parser_version="community-instruction-v1",
                dialect="instruction-first",
            )
        }
    ).save(lock_path)
    _rewrite_v2_with_v1_field_filter(lock_path)

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    compatibility = next(
        item
        for item in report.diagnostics
        if item.code == "LOCKFILE_IDENTITY_METADATA_LOST"
    )
    assert compatibility.severity is DiagnosticSeverity.WARNING
    assert compatibility.blocking is False
    assert report.ok is True
    assert len(report.skills) == 1
    item = report.skills[0]
    assert item.name == runtime_name
    assert item.install_id == ""
    assert item.path == str(skill_dir)
    assert item.lifecycle.install_state is SkillInstallState.TRACKED
    assert item.lifecycle.load_state is SkillLoadState.VALIDATED_OFFLINE
    assert item.lifecycle.compatibility_state is SkillCompatibilityState.INSTRUCTION_ONLY
    assert item.resolution is not None
    assert item.resolution.requested_identifier == "demo"
    assert item.resolution.canonical_identifier == "demo"


@pytest.mark.parametrize(
    "raw_capabilities",
    ["scoped_tool_permissions", {"scoped_tool_permissions": True}, None],
)
def test_doctor_ignores_malformed_degraded_capability_lock_extensions(
    tmp_path: Path,
    raw_capabilities: object,
) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(managed)
    _write_lock(
        lock_path,
        skill_dir,
        extra={"degraded_capabilities": raw_capabilities},
    )
    loader = _loader(tmp_path, managed)
    loader.load_all()

    item = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        eligibility_context=EligibilityContext(os_name="linux"),
    ).skills[0]

    assert item.lifecycle.compatibility_state is SkillCompatibilityState.INSTRUCTION_ONLY
    assert "TOOL_PREAPPROVAL_IGNORED" not in {
        diagnostic.code for diagnostic in item.diagnostics
    }


def _loader(tmp_path: Path, managed: Path, *, workspace: Path | None = None) -> SkillLoader:
    return SkillLoader(
        bundled_dir=tmp_path / "bundled",
        managed_dir=managed,
        workspace_dir=workspace or tmp_path / "workspace",
        personal_agents_dir=tmp_path / "personal",
        project_agents_dir=tmp_path / "project",
        snapshot_path=tmp_path / "snapshot.json",
    )


def test_doctor_reports_live_active_winner_without_refreshing_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(
        managed,
        metadata=(
            "metadata:\n"
            "  opensquilla:\n"
            "    requires:\n"
            "      bins: [synthetic-doctor-bin]\n"
        ),
    )
    _write_lock(lock_path, skill_dir)
    loader = _loader(tmp_path, managed)
    loader.load_all()

    def unexpected_refresh(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Doctor must inspect the published snapshot only")

    monkeypatch.setattr(loader, "refresh_if_changed", unexpected_refresh)
    report = SkillDoctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        eligibility_context=EligibilityContext(
            os_name="linux",
            has_bin_cache={"synthetic-doctor-bin": True},
        ),
    ).doctor("install-demo")

    assert report.ok is True
    assert report.catalog_generation == loader.snapshot().generation
    assert len(report.skills) == 1
    item = report.skills[0]
    assert item.status == "ready"
    assert item.active is True
    assert item.instruction_usable is True
    assert item.lifecycle.install_state is SkillInstallState.TRACKED
    assert item.lifecycle.load_state is SkillLoadState.LOADED
    assert item.lifecycle.selection_state is SkillSelectionState.ACTIVE
    assert item.lifecycle.compatibility_state is SkillCompatibilityState.INSTRUCTION_ONLY
    assert item.lifecycle.readiness_state is SkillReadinessState.READY
    assert item.lifecycle.invocation.to_dict() == {
        "model_catalog": True,
        "skill_view": True,
        "user_completion": True,
        "direct_command": False,
        "argument_substitution": False,
        "scoped_tool_permissions": False,
        "sandbox_execution": "unknown",
    }
    assert report.to_dict()["constraints"] == {
        "network": False,
        "scripts": False,
        "llm": False,
    }


def test_doctor_readiness_uses_only_passive_managed_binary_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(
        managed,
        metadata=(
            "metadata:\n"
            "  opensquilla:\n"
            "    requires:\n"
            "      bins: [synthetic-passive-doctor-bin]\n"
        ),
    )
    _write_lock(lock_path, skill_dir)
    passive_calls: list[str] = []

    def active_resolver(_name: str) -> Path | None:
        raise AssertionError("Doctor must not use the active managed-binary resolver")

    def passive_resolver(name: str) -> Path | None:
        passive_calls.append(name)
        return None

    monkeypatch.setattr("opensquilla.skills.eligibility.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "opensquilla.skills.eligibility.resolve_managed_binary",
        active_resolver,
    )
    monkeypatch.setattr(
        "opensquilla.skills.eligibility.resolve_managed_binary_passive",
        passive_resolver,
    )

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    assert passive_calls == ["synthetic-passive-doctor-bin"]
    assert report.skills[0].status == "needs_setup"
    assert report.to_dict()["constraints"]["scripts"] is False


def test_doctor_distinguishes_shadowed_managed_candidate(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    lock_path = tmp_path / "skills-lock.json"
    managed_skill = _write_skill(managed)
    _write_skill(workspace)
    _write_lock(lock_path, managed_skill)
    loader = _loader(tmp_path, managed, workspace=workspace)
    loader.load_all()

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    item = report.skills[0]
    assert item.lifecycle.load_state is SkillLoadState.LOADED
    assert item.lifecycle.selection_state is SkillSelectionState.SHADOWED
    assert item.lifecycle.invocation.model_catalog is False
    assert item.lifecycle.invocation.skill_view is False
    assert "SKILL_SHADOWED" in {diagnostic.code for diagnostic in item.diagnostics}


def test_offline_doctor_never_claims_live_activation(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(managed)
    _write_lock(lock_path, skill_dir)

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    item = report.skills[0]
    assert item.lifecycle.load_state is SkillLoadState.VALIDATED_OFFLINE
    assert item.lifecycle.selection_state is SkillSelectionState.HIDDEN
    assert item.active is False
    assert item.lifecycle.invocation.model_catalog is False
    assert item.lifecycle.invocation.skill_view is False
    assert item.lifecycle.invocation.user_completion is False
    assert "CATALOG_VALIDATED_OFFLINE" in {
        diagnostic.code for diagnostic in item.diagnostics
    }
    assert report.to_dict()["catalogGeneration"] is None


def test_doctor_reports_drift_dependencies_and_unsupported_config(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(
        managed,
        metadata=(
            "metadata:\n"
            "  opensquilla:\n"
            "    requires:\n"
            "      bins: [missing-doctor-bin]\n"
            "      env: [SYNTHETIC_DOCTOR_TOKEN]\n"
            "      config: [third.party.setting]\n"
        ),
    )
    _write_lock(lock_path, skill_dir)
    (skill_dir / "README.md").write_text("local drift\n", encoding="utf-8")

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(
            os_name="linux",
            has_bin_cache={"missing-doctor-bin": False},
            env_cache={"SYNTHETIC_DOCTOR_TOKEN": None},
        ),
    )

    item = report.skills[0]
    codes = {diagnostic.code for diagnostic in item.diagnostics}
    assert item.status == "needs_setup"
    assert item.lifecycle.install_state is SkillInstallState.DRIFTED
    assert item.lifecycle.readiness_state is SkillReadinessState.NEEDS_SETUP
    assert item.lifecycle.compatibility_state is SkillCompatibilityState.DEGRADED
    assert {
        "TREE_DRIFT",
        "BINARY_REQUIREMENT_UNMET",
        "ENV_REQUIREMENT_UNMET",
        "REQUIREMENT_UNSUPPORTED",
    } <= codes
    assert report.ok is False


def test_doctor_surfaces_loader_last_known_good_state(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(managed)
    _write_lock(lock_path, skill_dir)
    loader = _loader(tmp_path, managed)
    loader.load_all()
    (skill_dir / "SKILL.md").write_text("invalid manifest\n", encoding="utf-8")

    reload_result = loader.reload(reason="test-doctor-lkg")
    assert reload_result.partial is True

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        loader=loader,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    item = report.skills[0]
    assert item.lifecycle.load_state is SkillLoadState.SERVING_PREVIOUS
    assert "LOADER_SERVING_PREVIOUS" in {
        diagnostic.code for diagnostic in item.diagnostics
    }


def test_corrupt_lock_is_reported_without_hiding_untracked_tree(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    _write_skill(managed)
    lock_path.write_text('{"version": 2, broken', encoding="utf-8")

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    assert report.ok is False
    assert {diagnostic.code for diagnostic in report.diagnostics} == {
        "LOCKFILE_CORRUPT"
    }
    assert report.skills[0].lifecycle.install_state is SkillInstallState.UNTRACKED
    assert "INSTALL_UNTRACKED" in {
        diagnostic.code for diagnostic in report.skills[0].diagnostics
    }


def test_doctor_rejects_static_resource_symlink(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    skill_dir = _write_skill(managed)
    resources = skill_dir / "references"
    resources.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        (resources / "escape.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this platform")
    _write_lock(lock_path, skill_dir)

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    item = report.skills[0]
    assert item.lifecycle.install_state is SkillInstallState.DRIFTED
    assert "RESOURCE_SYMLINK_ESCAPE" in {
        diagnostic.code for diagnostic in item.diagnostics
    }
    assert report.ok is False


def test_doctor_never_follows_absolute_lockfile_target(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    outside = _write_skill(tmp_path / "outside-root")
    Lockfile(
        installed={
            "demo": LockEntry(
                source="github",
                identifier="owner/repo",
                relative_path=str(outside),
            )
        }
    ).save(lock_path)

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    item = report.skills[0]
    assert item.path == ""
    assert item.lifecycle.install_state is SkillInstallState.MISSING
    assert "STORE_PATH_UNSAFE" in {
        diagnostic.code for diagnostic in item.diagnostics
    }
    assert report.ok is False


def test_doctor_v1_path_cannot_redirect_a_storage_key_to_another_child(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    expected = _write_skill(managed, name="storage-a")
    other = _write_skill(managed, name="storage-b")
    lock_path = tmp_path / "skills-lock.json"
    Lockfile(
        installed={
            "storage-a": LockEntry(
                source="clawhub",
                identifier="storage-a",
                path=str(other),
                sha256=compute_tree_sha256(expected),
            )
        }
    ).save(lock_path)

    report = SkillDoctor(
        managed_dir=managed,
        lockfile_path=lock_path,
    ).doctor()

    tracked = next(item for item in report.skills if item.installed)
    assert tracked.path == str(expected)
    assert any(
        item.code == "LEGACY_PATH_MISMATCH" for item in tracked.diagnostics
    )


def test_offline_doctor_reports_pending_journal_without_recovering_it(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    skill_dir = _write_skill(managed)
    _write_lock(lock_path, skill_dir)
    transaction_id = "d" * 32
    stage = staging_root(managed) / transaction_id / "demo"
    rollback = rollback_root(managed) / transaction_id / "demo"
    _write_skill(stage.parent, name="demo")
    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="demo",
        target=skill_dir,
        staging=stage,
        rollback=rollback,
        lockfile_path=lock_path,
    )
    journal.write(journal_path)
    journal_bytes = journal_path.read_bytes()
    staged_bytes = (stage / "SKILL.md").read_bytes()
    rollback_existed = rollback.exists()

    report = doctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        journal_path=journal_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    assert report.ok is False
    assert [item.code for item in report.diagnostics] == ["TRANSACTION_PENDING"]
    assert report.diagnostics[0].details["phase"] == "prepared"
    assert journal_path.read_bytes() == journal_bytes
    assert (stage / "SKILL.md").read_bytes() == staged_bytes
    assert rollback.exists() is rollback_existed


def test_doctor_deduplicates_startup_and_live_journal_diagnostics(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    lock_path = tmp_path / "skills-lock.json"
    journal_path = tmp_path / "transaction.json"
    skill_dir = _write_skill(managed)
    _write_lock(lock_path, skill_dir)
    stage = staging_root(managed) / ("e" * 32) / "demo"
    rollback = rollback_root(managed) / ("e" * 32) / "demo"
    _write_skill(stage.parent, name="demo")
    SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="demo",
        target=skill_dir,
        staging=stage,
        rollback=rollback,
        lockfile_path=lock_path,
    ).write(journal_path)
    startup_diagnostic = SkillDiagnostic(
        code="TRANSACTION_PENDING",
        severity=DiagnosticSeverity.ERROR,
        phase=DiagnosticPhase.STORE,
        message="Recovery was already detected during startup",
        blocking=True,
    )

    report = SkillDoctor(
        managed_dir=managed,
        lockfile_path=lock_path,
        journal_path=journal_path,
        eligibility_context=EligibilityContext(os_name="linux"),
        additional_diagnostics=(startup_diagnostic,),
    ).doctor()

    assert [item.code for item in report.diagnostics] == ["TRANSACTION_PENDING"]


def test_offline_doctor_reports_invalid_journal_without_creating_store(
    tmp_path: Path,
) -> None:
    managed = tmp_path / "managed"
    journal_path = tmp_path / "transaction.json"
    original = b'{"version":1,"old_lock_exists":"false"}'
    journal_path.write_bytes(original)

    report = doctor(
        managed_dir=managed,
        lockfile_path=tmp_path / "skills-lock.json",
        journal_path=journal_path,
        eligibility_context=EligibilityContext(os_name="linux"),
    )

    assert report.ok is False
    assert "RECOVERY_REQUIRED" in {item.code for item in report.diagnostics}
    assert journal_path.read_bytes() == original
    assert not managed.exists()
