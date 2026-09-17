"""Reject missing or substituted generators before publishing Contract artifacts."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from scripts.contracts import generate_gateway_contracts as runner
from scripts.contracts import generate_sessions_list_contract as legacy


@pytest.mark.parametrize(
    ("name", "expected", "wrong"),
    [("json-schema-to-typescript", "16.0.0", "15.0.4"), ("ajv", "8.20.0", "8.17.1")],
)
def test_npm_generator_metadata_rejects_missing_wrong_version_and_wrong_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, expected: str, wrong: str,
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    package = tmp_path / "opensquilla-webui/node_modules" / name / "package.json"
    with pytest.raises(FileNotFoundError):
        runner._verify_npm_generator(name, expected)

    package.parent.mkdir(parents=True)
    for document in ({"name": name, "version": wrong}, {"name": "other", "version": expected}):
        package.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(runner.ContractConfigurationError, match=f"must be {expected}"):
            runner._verify_npm_generator(name, expected)

    package.write_text(json.dumps({"name": name, "version": expected}), encoding="utf-8")
    runner._verify_npm_generator(name, expected)


@pytest.mark.parametrize("installed", [None, "0.75.1"])
def test_ordinary_renderer_rejects_missing_or_legacy_python_generator(
    monkeypatch: pytest.MonkeyPatch, installed: str | None,
) -> None:
    spec = next(spec for spec in runner.discover_contracts() if not spec.uses_legacy_generator)

    def discover(name: str) -> str:
        assert name == "datamodel-code-generator"
        if installed is None:
            raise PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(runner, "distribution_version", discover)
    monkeypatch.setattr(runner, "_run", lambda *args, **kwargs: pytest.fail("must not generate"))
    if installed is None:
        with pytest.raises(PackageNotFoundError):
            runner.render_generic(spec)
    else:
        with pytest.raises(
            runner.ContractConfigurationError, match="Python generator must be 0.81.0"
        ):
            runner.render_generic(spec)


def test_frozen_renderer_requires_its_separate_python_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(legacy, "LEGACY_PYTHON", tmp_path / "absent-python")
    with pytest.raises(RuntimeError, match="prepare_codegen_toolchains.py"):
        legacy.render()


def test_frozen_renderer_rejects_current_python_even_in_the_legacy_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "python"
    executable.touch()
    monkeypatch.setattr(legacy, "LEGACY_PYTHON", executable)
    monkeypatch.setattr(legacy, "_capture", lambda *args, **kwargs: "0.81.0\n")
    monkeypatch.setattr(legacy, "_run", lambda *args, **kwargs: pytest.fail("must not generate"))
    with pytest.raises(RuntimeError, match="Frozen Python generator must be 0.75.1"):
        legacy.render()


def test_frozen_renderer_rejects_missing_or_substituted_npm_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "python"
    executable.touch()
    alias = tmp_path / "json-schema-to-typescript-legacy"
    monkeypatch.setattr(legacy, "LEGACY_PYTHON", executable)
    monkeypatch.setattr(legacy, "LEGACY_TYPESCRIPT_PACKAGE", alias)
    monkeypatch.setattr(legacy, "_capture", lambda *args, **kwargs: "0.75.1\n")
    monkeypatch.setattr(legacy, "_run", lambda *args, **kwargs: pytest.fail("must not generate"))
    with pytest.raises(FileNotFoundError):
        legacy.render()
    alias.mkdir()
    for document in (
        {"name": "json-schema-to-typescript", "version": "16.0.0"},
        {"name": "other", "version": "15.0.4"},
    ):
        (alias / "package.json").write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(RuntimeError, match="Frozen TypeScript generator must be"):
            legacy.render()
