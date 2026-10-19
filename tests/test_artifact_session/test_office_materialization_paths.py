"""Office staging and persisted-plan recovery on native Windows long paths."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from opensquilla.artifact_session import office_materialization as disk
from opensquilla.artifact_session.errors import ArtifactConflictError, ArtifactValidationError
from opensquilla.artifact_session.working_files import checked_path
from opensquilla.paths import native_io_path
from tests.test_office_format import office_package

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")


@pytest.fixture
def long_workspace(tmp_path):
    root = tmp_path / "long-office"
    workspace = root
    while len(str(workspace)) <= 275:
        workspace /= "workspace-segment-" + "x" * 35
    native_io_path(workspace).mkdir(parents=True)
    try:
        yield workspace
    finally:
        shutil.rmtree(native_io_path(root))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("applied", [False, True])
def test_long_office_plan_roundtrip_and_recovery(long_workspace, existing, applied):
    workspace = long_workspace
    target = workspace / "artifacts" / "report.docx"
    old, new = office_package(text="Before"), office_package(text="After")
    assert old != new
    if existing:
        native_io_path(target.parent).mkdir(parents=True)
        native_io_path(target).write_bytes(old)
    plan = disk.stage_office_file(
        document_id="office-long-path",
        workspace=str(workspace),
        target=target.relative_to(workspace).as_posix(),
        payload=new,
        expected_sha256=hashlib.sha256(old).hexdigest() if existing else None,
        allow_existing=existing,
    )
    # The durable schema and layout are unchanged, including legacy JSON lists
    # for stat tuples. Native spellings must never escape into persisted plans.
    encoded = json.dumps(plan.to_dict())
    restored = disk.OfficeFilePlan.from_dict(json.loads(encoded))
    assert restored.workspace == str(workspace)
    assert restored.entry == target
    assert not str(restored.entry).startswith("\\\\?\\")
    assert restored.directory == (
        workspace / "artifacts" / ".office-materializations"
        / hashlib.sha256(plan.document_id.encode()).hexdigest() / plan.token
    )
    assert disk.office_stage_unconsumed(restored)
    staged_inode = native_io_path(restored.member("new")).stat().st_ino
    disk.apply_office_file(restored)
    assert disk.read_office_file(target) == new
    # Replacement remains a same-volume move of the staged file, not a copy.
    assert native_io_path(target).stat().st_ino == staged_inode
    assert not disk.office_stage_unconsumed(restored)
    if applied:
        # A committed receipt must also recover a missing canonical file.
        native_io_path(target).unlink()
    disk.reconcile_office_file(restored, applied=applied, committed_payload=lambda: new)
    if applied or existing:
        assert disk.read_office_file(target) == (new if applied else old)
    else:
        assert not native_io_path(target).exists()
    disk.cleanup_office_plan(restored, preserve_backup=False)
    assert not native_io_path(restored.directory).exists()


def test_long_office_recovery_preserves_external_edits_and_unknown_siblings(long_workspace):
    target = long_workspace / "report.docx"
    old, new = office_package(text="Before"), office_package(text="After")
    native_io_path(target).write_bytes(old)
    plan = disk.stage_office_file(
        document_id="office-external-edit",
        workspace=str(long_workspace),
        target=target.name,
        payload=new,
        expected_sha256=hashlib.sha256(old).hexdigest(),
        allow_existing=True,
    )
    disk.apply_office_file(plan)
    external = office_package(text="External edit")
    native_io_path(target).write_bytes(external)
    sibling = native_io_path(plan.directory / "unknown.txt")
    sibling.write_bytes(b"retain")
    with pytest.raises(ArtifactConflictError, match="externally modified"):
        disk.reconcile_office_file(plan, applied=False)
    assert disk.read_office_file(target) == external
    disk.cleanup_office_plan(plan, preserve_backup=True)
    assert disk.read_office_file(plan.member("old")) == old
    assert sibling.read_bytes() == b"retain"
    disk.cleanup_office_plan(plan, preserve_backup=False)
    assert not native_io_path(plan.member("old")).exists()
    assert sibling.read_bytes() == b"retain"


@pytest.mark.parametrize("inside", [False, True])
def test_long_office_path_still_rejects_junctions(long_workspace, tmp_path, inside):
    destination = (long_workspace if inside else tmp_path) / "destination"
    native_io_path(destination).mkdir()
    junction = long_workspace / "linked"
    linked = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(native_io_path(junction)),
         str(native_io_path(destination))],
        capture_output=True,
    )
    assert linked.returncode == 0, linked.stderr.decode(errors="replace")
    try:
        with pytest.raises(ArtifactValidationError, match="contains a link"):
            checked_path(long_workspace, "linked/presentation.pptx")
        with pytest.raises(ArtifactValidationError, match="contains a link"):
            checked_path(junction, "presentation.pptx")
    finally:
        native_io_path(junction).rmdir()


@pytest.mark.parametrize("target", ["../outside.pptx", "/outside.pptx", "a\\b", "C:/x"])
def test_long_office_path_still_rejects_invalid_targets(long_workspace, target):
    with pytest.raises(ArtifactValidationError, match="Invalid working resource path"):
        disk.stage_office_file(
            document_id="invalid-target",
            workspace=str(long_workspace),
            target=target,
            payload=office_package("pptx"),
            expected_sha256=None,
            allow_existing=False,
        )
