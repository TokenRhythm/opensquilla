from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.skills import tree as skill_tree
from opensquilla.skills.hub import doctor as hub_doctor_module
from opensquilla.skills.hub import management as hub_management
from opensquilla.skills.hub import transaction as hub_transaction
from opensquilla.skills.hub.contracts import SkillInstallState
from opensquilla.skills.hub.doctor import doctor
from opensquilla.skills.hub.lockfile import LockEntry, Lockfile, compute_tree_sha256
from opensquilla.skills.hub.transaction import (
    SkillTransactionJournal,
    recover_pending_skill_transaction,
    rollback_root,
    staging_root,
)
from opensquilla.skills.loader import SkillLoader
from opensquilla.tools.builtin import skill_tools as skill_tools_module
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.types import ToolContext, current_tool_context
from tests.test_skills.test_hub_management_service import FakeImmutableSource, _service


def _write_skill(root: Path, name: str, body: str = "Instructions.\n") -> Path:
    skill_dir = root / name
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: hash consumer fixture\n---\n{body}",
        encoding="utf-8",
    )
    (skill_dir / "references" / "probe.txt").write_text(
        "resource bytes\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.mark.asyncio
async def test_pinned_skill_view_fails_closed_when_tree_hash_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "hash-view")
    loader = SkillLoader(bundled_dir=bundled)
    previous_loader = skill_tools_module._loader
    skill_tools_module.create_skill_tools(loader)
    loader.load_all()
    pinned = loader.snapshot()
    registered = get_default_registry().get("skill_view")
    assert registered is not None

    def unreadable_tree(_path: Path) -> str:
        raise OSError("synthetic tree read failure")

    monkeypatch.setattr(skill_tree, "compute_tree_sha256", unreadable_tree)
    token = current_tool_context.set(ToolContext(skill_catalog=pinned))
    try:
        result = await registered.handler(
            name="hash-view",
            file_path="references/probe.txt",
        )
    finally:
        current_tool_context.reset(token)
        skill_tools_module._loader = previous_loader

    assert "current catalog was pinned" in result
    assert "resource bytes" not in result


def test_doctor_marks_tree_drift_when_hash_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    skill_dir = _write_skill(managed, "hash-doctor")
    digest = compute_tree_sha256(skill_dir)
    lockfile_path = tmp_path / "skills-lock.json"
    Lockfile(
        installed={
            "hash-doctor": LockEntry(
                source="github",
                identifier="owner/repo:hash-doctor",
                relative_path="hash-doctor",
                manifest_name="hash-doctor",
                parser_version="community-instruction-v1",
                tree_sha256=digest,
                sha256=digest,
            )
        }
    ).save(lockfile_path)

    def unreadable_tree(_path: Path) -> str:
        raise OSError("synthetic tree read failure")

    monkeypatch.setattr(hub_doctor_module, "compute_tree_sha256", unreadable_tree)
    report = doctor(managed_dir=managed, lockfile_path=lockfile_path)

    assert report.ok is False
    assert report.skills[0].lifecycle.install_state is SkillInstallState.DRIFTED
    diagnostic = next(
        item for item in report.skills[0].diagnostics if item.code == "TREE_DIGEST_FAILED"
    )
    assert diagnostic.blocking is True
    assert "synthetic tree read failure" in diagnostic.message


@pytest.mark.asyncio
async def test_management_rolls_back_when_postflight_hash_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    loader = SkillLoader(managed_dir=managed)
    source = FakeImmutableSource(
        {
            "SKILL.md": (
                "---\nname: hash-management\ndescription: old fixture\n---\nOld instructions.\n"
            )
        }
    )
    service = _service(tmp_path, source, loader=loader)
    installed = await service.install("hash-management", "fake")
    assert installed.success is True
    old_tree = compute_tree_sha256(managed / "hash-management")

    source.files = {
        "SKILL.md": (
            "---\nname: hash-management\ndescription: new fixture\n---\nNew instructions.\n"
        )
    }
    source.revision = "b" * 40
    real_hash = hub_management.compute_tree_sha256
    failed = False

    def fail_new_published_tree_once(path: Path) -> str:
        nonlocal failed
        manifest = path / "SKILL.md"
        if (
            not failed
            and path == managed / "hash-management"
            and manifest.exists()
            and "New instructions." in manifest.read_text(encoding="utf-8")
        ):
            failed = True
            raise OSError("synthetic postflight hash read failure")
        return real_hash(path)

    monkeypatch.setattr(
        hub_management,
        "compute_tree_sha256",
        fail_new_published_tree_once,
    )
    result = (await service.update("hash-management"))[0]

    assert failed is True
    assert result.success is False
    assert result.rollback_performed is True
    assert any(item.code == "CATALOG_RELOAD_FAILED" for item in result.diagnostics)
    assert compute_tree_sha256(managed / "hash-management") == old_tree
    assert "Old instructions." in (managed / "hash-management" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    restored_entry = Lockfile.load(tmp_path / "skills-lock.json").get("hash-management")
    assert restored_entry is not None
    assert restored_entry.resolved_revision == "a" * 40


def test_transaction_recovery_retains_journal_when_tree_hash_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = tmp_path / "managed"
    target = _write_skill(managed, "hash-transaction", "Old instructions.\n")
    staging = staging_root(managed) / "tx" / "hash-transaction"
    rollback = rollback_root(managed) / "tx" / "hash-transaction"
    _write_skill(staging.parent, staging.name, "New instructions.\n")
    lockfile_path = tmp_path / "skills-lock.json"
    old_lock = b'{"version":1,"installed":{}}\n'
    lockfile_path.write_bytes(old_lock)
    journal_path = tmp_path / "transaction.json"
    journal = SkillTransactionJournal.prepare(
        operation="update",
        managed_dir=managed,
        name="hash-transaction",
        target=target,
        staging=staging,
        rollback=rollback,
        lockfile_path=lockfile_path,
    )
    journal.write(journal_path)

    def unreadable_tree(_path: Path) -> str:
        raise OSError("synthetic recovery hash read failure")

    monkeypatch.setattr(hub_transaction, "compute_tree_sha256", unreadable_tree)
    diagnostics = recover_pending_skill_transaction(
        managed_dir=managed,
        lockfile_path=lockfile_path,
        journal_path=journal_path,
    )

    assert [item.code for item in diagnostics] == ["RECOVERY_REQUIRED"]
    assert diagnostics[0].blocking is True
    assert "synthetic recovery hash read failure" in diagnostics[0].message
    assert journal_path.exists()
    assert target.exists()
    assert lockfile_path.read_bytes() == old_lock
