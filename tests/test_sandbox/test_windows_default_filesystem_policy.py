from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.types import SandboxBackendError


def test_windows_filesystem_targets_use_actual_path_not_declared_paths(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend import windows_default as mod

    actual = tmp_path / "actual.txt"
    declared = tmp_path / "declared.txt"
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=tmp_path,
        run_mode="trusted",
        path=actual,
        paths=(declared,),
        content="new\n",
        file_system_profile=FileSystemPermissionProfile(
            entries=(FileSystemPermissionEntry(tmp_path, FileSystemAccess.WRITE),)
        ),
    )

    with pytest.raises(SandboxBackendError, match="declared filesystem paths"):
        mod._filesystem_operation_targets(operation, operation.request)


def test_windows_filesystem_targets_derive_apply_patch_paths(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend import windows_default as mod

    actual = tmp_path / "created.txt"
    patch = """*** Begin Patch
*** Add File: created.txt
+hello
*** End Patch"""
    operation = SandboxOperation.filesystem(
        kind="apply_patch",
        workspace=tmp_path,
        run_mode="trusted",
        paths=(actual,),
        patch=patch,
        root=tmp_path,
        file_system_profile=FileSystemPermissionProfile(
            entries=(FileSystemPermissionEntry(tmp_path, FileSystemAccess.WRITE),)
        ),
    )

    assert mod._filesystem_operation_targets(operation, operation.request) == (actual,)


def test_windows_filesystem_profile_denies_write_to_readonly_target(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend import windows_default as mod

    target = tmp_path / "readonly" / "notes.txt"
    target.parent.mkdir()
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=tmp_path,
        run_mode="trusted",
        path=target,
        paths=(target,),
        content="new\n",
        file_system_profile=FileSystemPermissionProfile(
            entries=(FileSystemPermissionEntry(tmp_path, FileSystemAccess.READ),)
        ),
    )

    with pytest.raises(SandboxBackendError, match="requires write access"):
        mod._filesystem_operation_request(operation)

    assert not target.exists()
    assert not (tmp_path / ".opensquilla-cache").exists()
