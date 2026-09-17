from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.sandbox.policy_models import RuntimePolicySettings
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.runtime_manifest import (
    BundledRuntimeResolver,
    RuntimeManifest,
    RuntimeManifestError,
)


def _manifest_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "runtimeSet": "test-set",
        "assets": {
            "windows-x64": {
                "python": {
                    "id": "python-test",
                    "version": "3.13.1",
                    "url": "https://example.invalid/python.tar.gz",
                    "sha256": "a" * 64,
                    "archiveType": "tar.gz",
                    "installDir": "python",
                    "stripComponents": 1,
                    "binDirs": ["."],
                    "executables": {"python": "python.exe"},
                },
                "node": {
                    "id": "node-test",
                    "version": "24.1.0",
                    "url": "https://example.invalid/node.zip",
                    "sha256": "b" * 64,
                    "archiveType": "zip",
                    "installDir": "node",
                    "stripComponents": 1,
                    "binDirs": ["."],
                    "executables": {"node": "node.exe"},
                },
                "gitBash": {
                    "id": "git-test",
                    "version": "2.50.0",
                    "url": "https://example.invalid/git.exe",
                    "sha256": "c" * 64,
                    "archiveType": "7z-sfx",
                    "installDir": "git-bash",
                    "stripComponents": 0,
                    "binDirs": ["cmd", "bin", "usr/bin"],
                    "executables": {
                        "git": "cmd/git.exe",
                        "bash": "bin/bash.exe",
                    },
                },
            }
        },
    }


@pytest.fixture
def resolver(tmp_path: Path) -> BundledRuntimeResolver:
    manifest = RuntimeManifest.model_validate(_manifest_payload())
    return BundledRuntimeResolver(
        manifest,
        resource_root=tmp_path,
        platform="win32",
        arch="x64",
    )


def test_safe_path_puts_bundled_tools_first(
    resolver: BundledRuntimeResolver,
    tmp_path: Path,
) -> None:
    host = (tmp_path / "host-a", tmp_path / "host-b")
    path = resolver.path_for(RunMode.SAFE, host)
    assert path[0].name == "python"
    assert path[-2:] == host


def test_full_path_keeps_host_first(
    resolver: BundledRuntimeResolver,
    tmp_path: Path,
) -> None:
    host = (tmp_path / "host-a", tmp_path / "host-b")
    path = resolver.path_for(RunMode.FULL, host)
    assert path[:2] == host
    assert path[2].name == "python"


def test_runtime_toggles_remove_disabled_tools(
    resolver: BundledRuntimeResolver,
) -> None:
    path = resolver.path_for(
        RunMode.SAFE,
        (),
        policy=RuntimePolicySettings(node=False, git_bash=False),
    )
    assert [entry.name for entry in path] == ["python"]


def test_disabled_runtime_module_keeps_only_host_path(
    resolver: BundledRuntimeResolver,
    tmp_path: Path,
) -> None:
    host = (tmp_path / "host",)
    path = resolver.path_for(
        RunMode.SAFE,
        host,
        policy=RuntimePolicySettings(enabled=False),
    )
    assert path == host


def test_resolver_exposes_pinned_executable_paths(
    resolver: BundledRuntimeResolver,
) -> None:
    executables = resolver.executable_paths()
    assert executables["python"].name == "python.exe"
    assert executables["node"].name == "node.exe"
    assert executables["git"].name == "git.exe"
    assert executables["bash"].name == "bash.exe"


def test_manifest_rejects_unpinned_or_unsafe_assets() -> None:
    payload = _manifest_payload()
    payload["assets"]["windows-x64"]["python"]["sha256"] = "latest"
    with pytest.raises(RuntimeManifestError, match="sha256"):
        RuntimeManifest.model_validate(payload)

    payload = _manifest_payload()
    payload["assets"]["windows-x64"]["python"]["installDir"] = "../escape"
    with pytest.raises(RuntimeManifestError, match="installDir"):
        RuntimeManifest.model_validate(payload)


def test_manifest_loads_camel_case_json(tmp_path: Path) -> None:
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(_manifest_payload()), encoding="utf-8")
    manifest = RuntimeManifest.from_path(manifest_path)
    assert manifest.schema_version == 1
    assert manifest.runtime_set == "test-set"


def test_unknown_platform_fails_closed(tmp_path: Path) -> None:
    resolver = BundledRuntimeResolver(
        RuntimeManifest.model_validate(_manifest_payload()),
        resource_root=tmp_path,
        platform="linux",
        arch="arm64",
    )
    with pytest.raises(RuntimeManifestError, match="linux-arm64"):
        resolver.path_for(RunMode.SAFE, ())


def test_unix_target_requires_python_and_node_without_windows_git_bash(
    tmp_path: Path,
) -> None:
    windows_assets = _manifest_payload()["assets"]["windows-x64"]
    payload = {
        "schemaVersion": 1,
        "runtimeSet": "portable",
        "assets": {
            "linux-x64": {
                "python": windows_assets["python"],
                "node": windows_assets["node"],
            }
        },
    }
    resolver = BundledRuntimeResolver(
        RuntimeManifest.model_validate(payload),
        resource_root=tmp_path,
        platform="linux",
        arch="x64",
    )

    assert set(resolver.executable_paths()) == {"python", "node"}
