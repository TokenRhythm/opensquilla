from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
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


@pytest.mark.parametrize("workers", [1, 4])
def test_frozen_inventory_distinguishes_collected_dependency_from_build_only(tmp_path, workers):
    site = tmp_path / "site-packages"
    site.mkdir()
    source = site / "runtime.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr(([('runtime', str(source), 'PYMODULE')],)), encoding="utf-8")
    result = inventory.frozen_packages(tmp_path, analysis, {"runtime": {"2"}, "builder": {"3"}}, [
        distribution(site, "runtime", "2", ["runtime.py"]),
        distribution(site, "builder", "3", []),
    ], workers=workers)
    assert result[0] == {"name": "builder", "version": "3", "bundled": False, "files": []}
    assert result[1]["bundled"] is True
    assert result[1]["files"][0]["sha256"] == inventory.digest(source)


@pytest.mark.parametrize("workers", [1, 4])
def test_frozen_inventory_rejects_unowned_collected_dependency(tmp_path, workers):
    site = tmp_path / "site-packages"
    site.mkdir()
    source = site / "untracked.py"
    source.write_text("", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr([("untracked", str(source), "PYMODULE")]), encoding="utf-8")
    with pytest.raises(ValueError, match="no distribution ownership"):
        inventory.frozen_packages(tmp_path, analysis, {}, [], workers=workers)


@pytest.mark.parametrize("workers", [1, 4])
def test_frozen_inventory_rejects_version_drift_in_build_environment(tmp_path, workers):
    with pytest.raises(ValueError, match="does not match uv.lock"):
        inventory.frozen_packages(tmp_path, tmp_path / "unused.toc", {"runtime": {"2"}}, [
            distribution(tmp_path, "runtime", "1", []),
        ], workers=workers)


@pytest.mark.parametrize("workers", [1, 4])
def test_frozen_inventory_does_not_execute_toc(tmp_path, workers):
    marker = tmp_path / "must-not-exist"
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(f"__import__('pathlib').Path({str(marker)!r}).touch()", encoding="utf-8")
    with pytest.raises((ValueError, SyntaxError)):
        inventory.frozen_packages(tmp_path, analysis, {}, [], workers=workers)
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["PYMODULE-1", "PYMODULE-2", "PYSOURCE-1", "PYSOURCE-2"])
def test_optimized_python_modules_remain_in_inventory(kind):
    record = ("example", "/build/site-packages/example.py", kind)
    assert list(inventory.toc_records([record])) == [record]


def test_parallel_frozen_inventory_preserves_json_order_duplicates_and_multiple_owners(
    tmp_path, monkeypatch,
):
    site = tmp_path / "site-packages"
    site.mkdir()
    first, second = site / "first.py", site / "second.py"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    rows = [
        ("first", str(first), "PYMODULE"),
        ("second", str(second), "PYMODULE"),
        ("duplicate-first", str(first), "PYMODULE"),
        ("first-data", str(first), "DATA"),
    ]
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr(rows), encoding="utf-8")
    distributions = [
        distribution(site, "zeta", "2", ["second.py", "first.py"]),
        distribution(site, "alpha", "3", ["first.py"]),
        distribution(site, "builder", "4", []),
    ]
    versions = {"zeta": {"2"}, "alpha": {"3"}, "builder": {"4"}}
    expected = inventory.frozen_packages(tmp_path, analysis, versions, distributions)
    assert [p["name"] for p in expected] == ["alpha", "builder", "zeta"]
    assert [f["module"] for f in expected[2]["files"]] == ["first", "second", "first-data"]
    assert [f["module"] for f in expected[0]["files"]] == ["first", "first-data"]

    digest = inventory.digest
    second_completed = threading.Event()
    completions = []

    def out_of_order_digest(path):
        if path == first:
            assert second_completed.wait(5), "independent hashes did not overlap"
        value = digest(path)
        completions.append(path.name)
        if path == second:
            second_completed.set()
        return value

    monkeypatch.setattr(inventory, "digest", out_of_order_digest)
    actual = inventory.frozen_packages(tmp_path, analysis, versions, distributions, workers=4)
    assert completions[0] == "second.py"
    assert json.dumps(actual, indent=2) == json.dumps(expected, indent=2)


@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize("fault", ["missing-file", "hash-error"])
def test_frozen_inventory_propagates_file_read_failures(tmp_path, monkeypatch, workers, fault):
    site = tmp_path / "site-packages"
    site.mkdir()
    source = site / "runtime.py"
    source.write_text("runtime", encoding="utf-8")
    analysis = tmp_path / "Analysis-00.toc"
    analysis.write_text(repr([("runtime", str(source), "PYMODULE")]), encoding="utf-8")
    if fault == "missing-file":
        source.unlink()
        error = FileNotFoundError
    else:
        def broken_digest(path):
            raise OSError("injected hash read failure")
        monkeypatch.setattr(inventory, "digest", broken_digest)
        error = OSError
    with pytest.raises(error):
        inventory.frozen_packages(tmp_path, analysis, {"runtime": {"2"}}, [
            distribution(site, "runtime", "2", ["runtime.py"]),
        ], workers=workers)


@pytest.mark.parametrize("workers", [1, 4])
def test_frozen_inventory_rejects_conflicting_installed_versions(tmp_path, workers):
    with pytest.raises(ValueError, match="Conflicting installed distribution versions"):
        inventory.frozen_packages(tmp_path, tmp_path / "unused.toc", {"runtime": {"1", "2"}}, [
            distribution(tmp_path, "runtime", "1", []),
            distribution(tmp_path, "runtime", "2", []),
        ], workers=workers)


@pytest.mark.parametrize("workers", [0, 5, -1])
def test_frozen_inventory_rejects_unbounded_worker_count(tmp_path, workers):
    with pytest.raises(ValueError, match="between 1 and 4"):
        inventory.frozen_packages(tmp_path, tmp_path / "unused.toc", {}, [], workers=workers)


@pytest.mark.parametrize("value", ["0", "5", "1.5", "invalid"])
def test_inventory_cli_rejects_invalid_worker_count_before_writing_output(tmp_path, value):
    output = tmp_path / "inventory.json"
    result = subprocess.run([
        sys.executable, "-B", str(SCRIPT), "--repo", str(tmp_path),
        "--kind", "pyinstaller", "--analysis", str(tmp_path / "unused.toc"),
        "--output", str(output), "--workers", value,
    ], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "--workers" in result.stderr
    assert not output.exists()


def test_wheelhouse_cli_remains_serial_with_frozen_worker_option(tmp_path, monkeypatch):
    wheel = write_wheel(tmp_path, "example", "2")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "example"\nversion = "2"\n', encoding="utf-8",
    )
    output = tmp_path / "inventory.json"
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT), "--repo", str(tmp_path), "--kind", "wheelhouse",
        "--packages", str(tmp_path), "--output", str(output), "--workers", "4",
    ])
    def forbid_executor(*args, **kwargs):
        raise AssertionError("wheelhouse must not create frozen inventory workers")
    monkeypatch.setattr(inventory, "ThreadPoolExecutor", forbid_executor)
    inventory.main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["packages"] == [{
        "name": "example", "version": "2", "bundled": True,
        "files": [{"path": wheel.name, "sha256": inventory.digest(wheel)}],
    }]
