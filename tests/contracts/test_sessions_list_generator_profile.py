"""The legacy CLI retains types without publishing verification-only validators."""

from pathlib import Path

import pytest

from scripts.contracts import generate_sessions_list_contract as legacy


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, ...]:
    names = (
        "PYTHON_OUTPUT",
        "PYTHON_METADATA_OUTPUT",
        "TYPESCRIPT_OUTPUT",
        "VALIDATOR_OUTPUT",
        "VALIDATOR_DECLARATIONS_OUTPUT",
    )
    paths = tuple(tmp_path / name for name in names)
    for name, path in zip(names, paths, strict=True):
        monkeypatch.setattr(legacy, name, path)
    return paths


def test_legacy_write_does_not_reintroduce_verification_artifacts(
    outputs: tuple[Path, ...],
) -> None:
    rendered = legacy.Rendered("python", "metadata", "typescript", "validator", "declarations")
    legacy._write(rendered)
    assert tuple(path.read_text() for path in outputs[:3]) == (
        "python",
        "metadata",
        "typescript",
    )
    assert not any(path.exists() for path in outputs[3:])


def test_legacy_check_requires_all_production_types_but_not_verification_artifacts(
    outputs: tuple[Path, ...],
) -> None:
    rendered = legacy.Rendered("python", "metadata", "typescript", "validator", "declarations")
    for path, content in zip(outputs[:3], ("python", "metadata", "typescript"), strict=True):
        path.write_text(content)
    assert legacy._check(rendered) == 0
    for path in outputs[:3]:
        original = path.read_text()
        path.write_text("stale")
        assert legacy._check(rendered) == 1
        path.write_text(original)
