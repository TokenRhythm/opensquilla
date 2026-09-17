from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release_dependency_inventory.py"
SPEC = importlib.util.spec_from_file_location("release_dependency_inventory", SCRIPT)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def write_wheel(directory: Path, name: str, version: str, suffix: str = "") -> Path:
    wheel = directory / f"{name}-{version}{suffix}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n"
        )
    return wheel


def test_wheel_inventory_checks_metadata_against_lock_and_hashes_artifact(tmp_path: Path):
    wheel = write_wheel(tmp_path, "some_package", "2.0")
    result = inventory.wheel_packages(tmp_path, {"some-package": {"2.0"}})
    assert result == [{
        "name": "some-package", "version": "2.0", "bundled": True,
        "files": [{"path": wheel.name, "sha256": inventory.digest(wheel)}],
    }]


def test_wheel_inventory_rejects_old_dependency_even_when_filename_claims_new_version(tmp_path):
    wheel = write_wheel(tmp_path, "example", "1.0")
    wheel.rename(tmp_path / "example-2.0-py3-none-any.whl")
    with pytest.raises(ValueError, match="does not match uv.lock"):
        inventory.wheel_packages(tmp_path, {"example": {"2.0"}})


def test_wheel_inventory_rejects_multiple_versions(tmp_path):
    write_wheel(tmp_path, "example", "1.0")
    write_wheel(tmp_path, "example", "2.0")
    with pytest.raises(ValueError, match="Multiple wheels"):
        inventory.wheel_packages(tmp_path, {"example": {"1.0", "2.0"}})


def test_wheel_inventory_rejects_missing_metadata(tmp_path):
    with ZipFile(tmp_path / "bad.whl", "w") as archive:
        archive.writestr("example.py", "")
    with pytest.raises(ValueError, match="one distribution metadata"):
        inventory.wheel_packages(tmp_path, {})


def distribution(root: Path, name: str, version: str, files: list[str]):
    return SimpleNamespace(
        metadata={"Name": name}, version=version, files=files,
        locate_file=lambda relative: root / relative,
    )


def test_frozen_inventory_distinguishes_collected_dependency_from_build_only(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    source = site / "runtime.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr(([('runtime', str(source), 'PYMODULE')],)), encoding="utf-8")
    result = inventory.frozen_packages(tmp_path, analysis, {"runtime": {"2"}, "builder": {"3"}}, [
        distribution(site, "runtime", "2", ["runtime.py"]),
        distribution(site, "builder", "3", []),
    ])
    assert result[0] == {"name": "builder", "version": "3", "bundled": False, "files": []}
    assert result[1]["bundled"] is True
    assert result[1]["files"][0]["sha256"] == inventory.digest(source)


def test_frozen_inventory_rejects_unowned_collected_dependency(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    source = site / "untracked.py"
    source.write_text("", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr([("untracked", str(source), "PYMODULE")]), encoding="utf-8")
    with pytest.raises(ValueError, match="no distribution ownership"):
        inventory.frozen_packages(tmp_path, analysis, {}, [])


def test_frozen_inventory_rejects_version_drift_in_build_environment(tmp_path):
    with pytest.raises(ValueError, match="does not match uv.lock"):
        inventory.frozen_packages(tmp_path, tmp_path / "unused.toc", {"runtime": {"2"}}, [
            distribution(tmp_path, "runtime", "1", []),
        ])


def test_frozen_inventory_does_not_execute_toc(tmp_path):
    marker = tmp_path / "must-not-exist"
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(f"__import__('pathlib').Path({str(marker)!r}).touch()", encoding="utf-8")
    with pytest.raises((ValueError, SyntaxError)):
        inventory.frozen_packages(tmp_path, analysis, {}, [])
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["PYMODULE-1", "PYMODULE-2", "PYSOURCE-1", "PYSOURCE-2"])
def test_optimized_python_modules_remain_in_inventory(kind):
    record = ("example", "/build/site-packages/example.py", kind)
    assert list(inventory.toc_records([record])) == [record]
