from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "desktop" / "electron" / "package-lock.json"


def _packages() -> dict[str, dict[str, object]]:
    payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    packages = payload.get("packages")
    assert isinstance(packages, dict)
    return packages


def _version_tuple(value: object) -> tuple[int, int, int]:
    assert isinstance(value, str)
    parts = value.split(".")
    assert len(parts) == 3
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def test_electron_runtime_yaml_parser_includes_empty_merge_source_dos_fix() -> None:
    packages = _packages()
    root = packages[""]
    updater = packages["node_modules/electron-updater"]
    js_yaml = packages["node_modules/js-yaml"]

    assert "electron-updater" in root["dependencies"]
    assert "js-yaml" in updater["dependencies"]
    assert js_yaml.get("dev") is not True
    assert _version_tuple(js_yaml["version"]) >= (4, 3, 2)


@pytest.mark.parametrize(
    ("name", "minimum"),
    [
        ("js-yaml", (4, 3, 2)),
        ("@xmldom/xmldom", (0, 8, 15)),
        ("fast-uri", (3, 1, 6)),
        ("electron", (42, 5, 1)),
    ],
)
def test_all_locked_parser_and_session_copies_include_security_fixes(
    name: str, minimum: tuple[int, int, int]
) -> None:
    versions = [
        _version_tuple(package["version"])
        for path, package in _packages().items()
        if path == f"node_modules/{name}" or path.endswith(f"/node_modules/{name}")
    ]
    assert versions, f"Missing expected dependency: {name}"
    assert all(version >= minimum for version in versions), (name, versions)


def test_undici_security_floors_preserve_supported_parent_major_versions() -> None:
    versions = [
        _version_tuple(package["version"])
        for path, package in _packages().items()
        if path == "node_modules/undici" or path.endswith("/node_modules/undici")
    ]
    assert versions
    for version in versions:
        # node-gyp still uses 6.x; forcing it onto 7.x would bypass its contract.
        if version[0] == 6:
            assert version >= (6, 28, 0)
        else:
            assert version >= (7, 29, 0)


def test_electron_build_dependencies_include_resource_exhaustion_fixes() -> None:
    packages = _packages()
    tar = packages["node_modules/tar"]
    brace_versions = [
        _version_tuple(package["version"])
        for path, package in packages.items()
        if path == "node_modules/brace-expansion"
        or path.endswith("/node_modules/brace-expansion")
    ]

    assert tar.get("dev") is True
    assert _version_tuple(tar["version"]) >= (7, 5, 19)
    assert brace_versions
    for version in brace_versions:
        if version[0] == 1:
            assert version >= (1, 1, 16)
        elif version[0] == 2:
            assert version >= (2, 1, 2)
        else:
            assert version >= (5, 0, 7)
