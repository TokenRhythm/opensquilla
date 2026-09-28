from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from opensquilla import execution_workspaces as workspaces
from opensquilla.project_workspaces import ProjectWorkspaceStateError


@pytest.fixture(autouse=True)
def fixed_creation_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspaces, "date", SimpleNamespace(today=lambda: date(2025, 2, 3)))


@pytest.mark.parametrize("existing_kind", ["directory", "file"])
def test_short_id_collision_preserves_existing_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_kind: str,
) -> None:
    first = UUID("11111111-1111-4111-8111-111111111111")
    collision = UUID("11111111-1111-4222-8222-222222222222")
    available = UUID("22222222-2222-4222-8222-222222222222")
    generate_id = Mock(side_effect=[first, collision, available])
    monkeypatch.setattr(workspaces, "uuid4", generate_id)
    original = workspaces.prepare_managed_workspace(tmp_path)
    existing = Path(original.binding["root"])
    assert existing.name == "20250203-111111111111"
    if existing_kind == "file":
        existing.rmdir()
        material = existing
    else:
        material = existing / "source.txt"
    material.write_text("preserve existing material", encoding="utf-8")

    prepared = workspaces.prepare_managed_workspace(tmp_path)
    root = Path(prepared.binding["root"])
    assert root.name == "20250203-222222222222"
    assert prepared.binding["id"] == available.hex
    assert generate_id.call_count == 3
    assert workspaces.validate_execution_workspace(prepared.binding) == prepared.binding
    prepared.rollback()
    assert not root.exists()
    assert material.read_text(encoding="utf-8") == "preserve existing material"


def test_repeated_short_id_collision_fails_without_reusing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    generate_id = Mock(return_value=UUID("11111111-1111-4111-8111-111111111111"))
    monkeypatch.setattr(workspaces, "uuid4", generate_id)
    original = workspaces.prepare_managed_workspace(tmp_path)
    root = Path(original.binding["root"])
    generate_id.reset_mock()

    with pytest.raises(ProjectWorkspaceStateError, match="unavailable"):
        workspaces.prepare_managed_workspace(tmp_path)

    assert generate_id.call_count == 16
    assert list(root.parent.iterdir()) == [root]
    assert workspaces.validate_execution_workspace(original.binding) == original.binding


def test_allocation_does_not_retry_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_mkdir = Path.mkdir
    attempts = []

    def deny_task_directory(path: Path, *args, **kwargs) -> None:
        if path.parent == tmp_path / "tasks":
            attempts.append(path)
            raise PermissionError("task directory denied")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_task_directory)
    with pytest.raises(ProjectWorkspaceStateError, match="unavailable") as error:
        workspaces.prepare_managed_workspace(tmp_path)

    assert isinstance(error.value.__cause__, PermissionError)
    assert len(attempts) == 1
    assert list((tmp_path / "tasks").iterdir()) == []
