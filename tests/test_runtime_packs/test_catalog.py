from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from pathlib import Path

import pytest

from opensquilla.runtime_packs import catalog as runtime_pack_catalog
from opensquilla.runtime_packs.catalog import (
    RuntimePackCatalog,
    RuntimePackCatalogError,
)


def _asset(component: str, target: str) -> dict[str, object]:
    return {
        "asset": f"OpenSquilla-Runtime-{component}-test-{target}.tar.xz",
        "archiveType": "tar.xz",
        "version": "1.2.3",
        "sizeBytes": 123,
        "unpackedSizeBytes": 456,
        "sha256": hashlib.sha256(f"{component}:{target}".encode()).hexdigest(),
    }


def test_catalog_never_accepts_download_urls() -> None:
    asset = _asset("python", "linux-x64")
    asset["url"] = "https://example.invalid/untrusted"
    with pytest.raises(RuntimePackCatalogError, match="must not contain URLs"):
        RuntimePackCatalog.model_validate(
            {
                "schemaVersion": 1,
                "catalogVersion": "test.1",
                "releaseTag": "vtest.1",
                "finalized": True,
                "targets": {"linux-x64": {"python": asset}},
            }
        )


def test_unfinalized_catalog_is_explicitly_not_release_complete(tmp_path: Path) -> None:
    path = tmp_path / "runtime-pack-catalog.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "catalogVersion": "2026-07-30.1",
                "releaseTag": "v2026.07.30.1",
                "finalized": False,
                "targets": {},
            }
        ),
        encoding="utf-8",
    )

    catalog = RuntimePackCatalog.from_path(path)
    assert catalog.finalized is False
    with pytest.raises(RuntimePackCatalogError, match="not finalized"):
        catalog.validate_release_matrix()


def test_release_matrix_requires_every_platform_component() -> None:
    target_components = {
        "darwin-arm64": ("python", "node"),
        "darwin-x64": ("python", "node"),
        "windows-arm64": ("python", "node", "gitBash"),
        "windows-x64": ("python", "node", "gitBash"),
        "linux-arm64": ("python", "node"),
        "linux-x64": ("python", "node"),
    }
    catalog = RuntimePackCatalog.model_validate(
        {
            "schemaVersion": 1,
            "catalogVersion": "test.1",
            "releaseTag": "vtest.1",
            "finalized": True,
            "targets": {
                target: {
                    component: _asset(component, target) for component in components
                }
                for target, components in target_components.items()
            },
        }
    )

    catalog.validate_release_matrix()


def test_catalog_environment_cannot_replace_application_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    untrusted = tmp_path / "runtime-pack-catalog.json"
    untrusted.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_PACK_CATALOG", str(untrusted))
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_MANIFEST", str(tmp_path / "runtime-manifest.json"))

    assert runtime_pack_catalog.discover_catalog_path() != untrusted


def test_installed_package_catalog_precedes_python_environment_ancestors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "site-packages" / "opensquilla" / "runtime_packs"
    package.mkdir(parents=True)
    packaged = package / "runtime-pack-catalog.json"
    packaged.write_text("{}\n", encoding="utf-8")
    venv = tmp_path / "venv"
    executable = venv / "bin" / "python"
    executable.parent.mkdir(parents=True)
    competing = venv / "runtime-pack-catalog.json"
    competing.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(runtime_pack_catalog, "__file__", str(package / "catalog.py"))
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert runtime_pack_catalog.discover_catalog_path() == packaged


@pytest.mark.parametrize("layout", ("onedir", "flat"))
def test_frozen_catalog_discovery_uses_only_fixed_desktop_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    package = tmp_path / "isolated" / "opensquilla" / "runtime_packs"
    package.mkdir(parents=True)
    runtime_root = tmp_path / "OpenSquilla.app" / "Contents" / "Resources" / "runtime"
    gateway_root = runtime_root / "gateway"
    executable = (
        gateway_root / "opensquilla-gateway" / "opensquilla-gateway"
        if layout == "onedir"
        else gateway_root / "opensquilla-gateway"
    )
    executable.parent.mkdir(parents=True, exist_ok=True)
    intended = runtime_root / "runtime-pack-catalog.json"
    intended.write_text("{}\n", encoding="utf-8")
    unrelated = tmp_path / "OpenSquilla.app" / "runtime-pack-catalog.json"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(runtime_pack_catalog, "__file__", str(package / "catalog.py"))
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert runtime_pack_catalog.discover_catalog_path() == intended

    intended.unlink()
    assert runtime_pack_catalog.discover_catalog_path() is None


def test_build_maps_one_canonical_catalog_into_wheel_and_sdist() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    )
    targets = pyproject["tool"]["hatch"]["build"]["targets"]
    source = "desktop/electron/runtime/runtime-pack-catalog.json"

    assert targets["wheel"]["force-include"][source] == (
        "opensquilla/runtime_packs/runtime-pack-catalog.json"
    )
    assert targets["sdist"]["force-include"][source] == source


def test_catalog_pins_trusted_historical_archive_digests() -> None:
    asset = _asset("python", "linux-x64")
    asset["trustedArchiveSha256"] = ["b" * 64, "b" * 64]
    catalog = RuntimePackCatalog.model_validate(
        {
            "schemaVersion": 1,
            "catalogVersion": "test.2",
            "releaseTag": "vtest.2",
            "finalized": True,
            "targets": {"linux-x64": {"python": asset}},
        }
    )
    descriptor = catalog.descriptor("linux-x64", "python")

    assert descriptor is not None
    assert descriptor.trusted_archive_sha256 == ("b" * 64,)

    asset["trustedArchiveSha256"] = ["B" * 64]
    with pytest.raises(RuntimePackCatalogError, match="lowercase hex"):
        RuntimePackCatalog.model_validate(
            {
                "schemaVersion": 1,
                "catalogVersion": "test.2",
                "releaseTag": "vtest.2",
                "finalized": True,
                "targets": {"linux-x64": {"python": asset}},
            }
        )


def test_catalog_bounds_and_uniquely_scopes_historical_digest_trust() -> None:
    too_many = _asset("python", "linux-x64")
    too_many["trustedArchiveSha256"] = [f"{index:064x}" for index in range(1, 6)]
    with pytest.raises(RuntimePackCatalogError, match="history limit"):
        RuntimePackCatalog.model_validate(
            {
                "schemaVersion": 1,
                "catalogVersion": "test.2",
                "releaseTag": "vtest.2",
                "finalized": True,
                "targets": {"linux-x64": {"python": too_many}},
            }
        )

    shared = "b" * 64
    python = _asset("python", "linux-x64")
    python["trustedArchiveSha256"] = [shared]
    node = _asset("node", "linux-x64")
    node["sha256"] = shared
    with pytest.raises(RuntimePackCatalogError, match="one target component"):
        RuntimePackCatalog.model_validate(
            {
                "schemaVersion": 1,
                "catalogVersion": "test.2",
                "releaseTag": "vtest.2",
                "finalized": True,
                "targets": {"linux-x64": {"python": python, "node": node}},
            }
        )
