"""Runtime Pack manager transactions and cross-platform contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from opensquilla.runtime_packs import RuntimePackResolver
from opensquilla.runtime_packs import manager as runtime_pack_manager
from opensquilla.runtime_packs.catalog import (
    RuntimePackCatalog,
    RuntimePackCatalogError,
    RuntimePackDescriptor,
)
from opensquilla.runtime_packs.manager import (
    RuntimePackService,
    runtime_pack_state_scope,
    runtime_packs_root,
)
from opensquilla.runtime_packs.models import (
    RuntimeAvailability,
    RuntimeOperationKind,
    RuntimeOperationState,
    RuntimeSource,
)
from opensquilla.sandbox.run_mode import RunMode

_REAL_RUNTIME_PROBE = runtime_pack_manager._run_probe


@pytest.fixture(autouse=True)
def _isolate_synthetic_archive_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep transaction fixtures independent of the host operating system."""

    def probe(
        descriptor: RuntimePackDescriptor,
        layout: runtime_pack_manager.PackLayout,
        package: Path,
    ) -> None:
        if descriptor.version.endswith("+test"):
            assert layout.component_id == descriptor.component_id
            assert layout.target == descriptor.target
            assert layout.version == descriptor.version
            for relative in layout.executables.values():
                assert (package / "payload" / Path(relative)).is_file()
            return
        _REAL_RUNTIME_PROBE(descriptor, layout, package)

    monkeypatch.setattr(runtime_pack_manager, "_run_probe", probe)


class _Response:
    def __init__(self, body: bytes, *, status: int, start: int, total: int) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.headers: dict[str, str] = {"Content-Length": str(len(body))}
        if status == 206:
            self.headers["Content-Range"] = f"bytes {start}-{total - 1}/{total}"

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def geturl(self) -> str:
        return "https://downloads.example.invalid/runtime.tar.xz"


class _MemoryOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.ranges: list[str | None] = []

    def __call__(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        raw_range = request.headers.get("Range")
        self.ranges.append(raw_range)
        if raw_range:
            start = int(raw_range.removeprefix("bytes=").removesuffix("-"))
            return _Response(
                self.body[start:], status=206, start=start, total=len(self.body)
            )
        return _Response(self.body, status=200, start=0, total=len(self.body))


class _AssetMemoryOpener:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies

    def __call__(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        asset = request.full_url.rsplit("/", 1)[-1]
        body = self.bodies[asset]
        return _Response(body, status=200, start=0, total=len(body))


class _IgnoringRangeOpener(_MemoryOpener):
    def __call__(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        self.ranges.append(request.headers.get("Range"))
        return _Response(self.body, status=200, start=0, total=len(self.body))


class _RejectRangeOnceOpener(_MemoryOpener):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.urls: list[str] = []

    def __call__(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        raw_range = request.headers.get("Range")
        self.ranges.append(raw_range)
        self.urls.append(request.full_url)
        if len(self.urls) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                416,
                "Range Not Satisfiable",
                {},
                io.BytesIO(),
            )
        assert raw_range is None
        return _Response(self.body, status=200, start=0, total=len(self.body))


class _InvalidContentRangeOpener(_MemoryOpener):
    def __call__(self, request: Any, *, timeout: int) -> _Response:
        assert timeout == 60
        raw_range = request.headers.get("Range")
        self.ranges.append(raw_range)
        start = int(raw_range.removeprefix("bytes=").removesuffix("-")) if raw_range else 0
        response = _Response(
            self.body[start:],
            status=206,
            start=start,
            total=len(self.body),
        )
        response.headers["Content-Range"] = (
            f"bytes {start + 1}-{len(self.body) - 1}/{len(self.body)}"
        )
        return response


def _runtime_archive(
    tmp_path: Path,
    *,
    catalog_version: str = "test.1",
    component_id: str = "python",
    version: str = "3.13.14+test",
    extra_root: bool = False,
    payload_bytes: int = 0,
) -> bytes:
    package = tmp_path / f"archive-root-{component_id}-{catalog_version}"
    bin_dir = package / "payload" / "bin"
    bin_dir.mkdir(parents=True)
    executables = (
        {"python": "bin/python"}
        if component_id == "python"
        else {"node": "bin/node", "npm": "bin/npm", "npx": "bin/npx"}
    )
    for name, relative in executables.items():
        binary = package / "payload" / Path(relative)
        output = (
            f"Python {version.split('+', 1)[0]}"
            if name == "python"
            else f"v{version.split('+', 1)[0]}"
        )
        binary.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
        binary.chmod(0o755)
    if payload_bytes:
        padding = package / "payload" / "share" / "fixture.bin"
        padding.parent.mkdir(parents=True)
        padding.write_bytes(b"x" * payload_bytes)
    (package / "licenses").mkdir()
    (package / "licenses" / "LICENSE.txt").write_text("test\n", encoding="utf-8")
    (package / "SBOM.spdx.json").write_text("{}\n", encoding="utf-8")
    (package / "pack-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "catalogVersion": catalog_version,
                "componentId": component_id,
                "target": "linux-x64",
                "version": version,
                "binDirs": ["bin"],
                "executables": executables,
            }
        ),
        encoding="utf-8",
    )
    if extra_root:
        (package / "unexpected.txt").write_text("not allowed\n", encoding="utf-8")
    archive = tmp_path / f"runtime-{component_id}-{catalog_version}.tar.xz"
    with tarfile.open(archive, "w:xz") as output:
        for child in sorted(package.iterdir()):
            output.add(child, arcname=child.name)
    return archive.read_bytes()


def _catalog(
    body: bytes,
    *,
    catalog_version: str = "test.1",
    version: str = "3.13.14+test",
    trusted_archive_sha256: tuple[str, ...] = (),
    unpacked_size_bytes: int = 1024 * 1024,
) -> RuntimePackCatalog:
    return RuntimePackCatalog.model_validate(
        {
            "schemaVersion": 1,
            "catalogVersion": catalog_version,
            "releaseTag": f"v{catalog_version}",
            "finalized": True,
            "targets": {
                "linux-x64": {
                    "python": {
                        "asset": "OpenSquilla-Runtime-python-test-linux-x64.tar.xz",
                        "archiveType": "tar.xz",
                        "version": version,
                        "sizeBytes": len(body),
                        "unpackedSizeBytes": unpacked_size_bytes,
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "trustedArchiveSha256": list(trusted_archive_sha256),
                    }
                }
            },
        }
    )


def _service(tmp_path: Path, body: bytes, opener: _MemoryOpener) -> RuntimePackService:
    return RuntimePackService(
        _catalog(body),
        root=tmp_path / "state",
        target="linux-x64",
        source_bases={
            RuntimeSource.GITHUB: "https://github.example.invalid/runtime-packs",
            RuntimeSource.OSS: "https://oss.example.invalid/runtime-packs",
        },
        opener=opener,
    )


def test_python_probe_runs_declared_executable_with_runtime_only_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "probe-package"
    executable = package / "payload" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic executable placeholder")
    descriptor = RuntimePackDescriptor(
        component_id="python",
        target="linux-x64",
        asset="OpenSquilla-Runtime-python-test-linux-x64.tar.xz",
        archive_type="tar.xz",
        version="3.13.14+probe",
        size_bytes=1,
        unpacked_size_bytes=1,
        sha256="a" * 64,
        trusted_archive_sha256=(),
    )
    layout = runtime_pack_manager.PackLayout(
        component_id="python",
        target="linux-x64",
        version="3.13.14+probe",
        bin_dirs=("bin",),
        executables={"python": "bin/python"},
    )
    observed: dict[str, object] = {}
    probe_output = b"Python 3.13.14\n"

    def run_probe(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        observed.update(
            command=command,
            check=check,
            capture_output=capture_output,
            timeout=timeout,
            env=env,
        )
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=probe_output,
            stderr=b"",
        )

    monkeypatch.setattr(runtime_pack_manager.subprocess, "run", run_probe)

    _REAL_RUNTIME_PROBE(descriptor, layout, package)

    assert observed["command"] == [str(executable), "--version"]
    assert observed["check"] is False
    assert observed["capture_output"] is True
    environment = observed["env"]
    assert isinstance(environment, dict)
    path_key = next(key for key in environment if key.casefold() == "path")
    assert environment[path_key] == str(executable.parent)

    probe_output = b"Python 3.12.10\n"
    with pytest.raises(runtime_pack_manager.RuntimePackError, match="python probe failed"):
        _REAL_RUNTIME_PROBE(descriptor, layout, package)


def test_status_reports_each_component_installed_size_independently(
    tmp_path: Path,
) -> None:
    python_asset = "OpenSquilla-Runtime-python-test-linux-x64.tar.xz"
    node_asset = "OpenSquilla-Runtime-node-test-linux-x64.tar.xz"
    python_body = _runtime_archive(tmp_path, payload_bytes=1_024)
    node_body = _runtime_archive(
        tmp_path,
        component_id="node",
        version="24.18.1+test",
        payload_bytes=4_096,
    )
    catalog = RuntimePackCatalog.model_validate(
        {
            "schemaVersion": 1,
            "catalogVersion": "test.1",
            "releaseTag": "vtest.1",
            "finalized": True,
            "targets": {
                "linux-x64": {
                    "python": {
                        "asset": python_asset,
                        "archiveType": "tar.xz",
                        "version": "3.13.14+test",
                        "sizeBytes": len(python_body),
                        "unpackedSizeBytes": 1024 * 1024,
                        "sha256": hashlib.sha256(python_body).hexdigest(),
                    },
                    "node": {
                        "asset": node_asset,
                        "archiveType": "tar.xz",
                        "version": "24.18.1+test",
                        "sizeBytes": len(node_body),
                        "unpackedSizeBytes": 1024 * 1024,
                        "sha256": hashlib.sha256(node_body).hexdigest(),
                    },
                }
            },
        }
    )
    service = RuntimePackService(
        catalog,
        root=tmp_path / "state",
        target="linux-x64",
        source_bases={
            RuntimeSource.GITHUB: "https://github.example.invalid/runtime-packs",
            RuntimeSource.OSS: "https://oss.example.invalid/runtime-packs",
        },
        opener=_AssetMemoryOpener(
            {python_asset: python_body, node_asset: node_body}
        ),
    )

    for component_id in ("python", "node"):
        operation = service.start_install(component_id)
        completed = service.wait_for_operation(operation.operation_id)
        assert completed is not None
        assert completed.state is RuntimeOperationState.COMPLETED

    active = {
        component_id: service.active_runtime(component_id)
        for component_id in ("python", "node")
    }
    assert all(runtime is not None for runtime in active.values())

    def package_size(component_id: str) -> int:
        runtime = active[component_id]
        assert runtime is not None
        marker = runtime.package / runtime_pack_manager._PACK_MARKER
        return sum(
            path.stat().st_size
            for path in runtime.package.rglob("*")
            if path.is_file() and path != marker
        )

    expected = {component_id: package_size(component_id) for component_id in active}
    observed = {
        component.component_id: component.installed_bytes
        for component in service.status().components
        if component.component_id in active
    }

    assert expected["python"] != expected["node"]
    assert observed == expected


def test_install_resume_activate_resolve_and_remove(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    opener = _MemoryOpener(body)
    service = _service(tmp_path, body, opener)
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    partial = service.root / "downloads" / f"{descriptor.sha256}.part"
    partial.write_bytes(body[: len(body) // 2])

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None
    assert completed.state is RuntimeOperationState.COMPLETED
    assert opener.ranges == [f"bytes={len(body) // 2}-"]
    status = service.status()
    python = next(item for item in status.components if item.component_id == "python")
    assert python.availability is RuntimeAvailability.READY
    assert python.active_version == "3.13.14+test"
    assert not partial.exists()

    resolver = RuntimePackResolver(service)
    host = (tmp_path / "host",)
    safe_path = resolver.path_for(RunMode.SAFE, host)
    full_path = resolver.path_for(RunMode.FULL, host)
    strict_path = resolver.path_for(RunMode.SAFE, host, require_managed=True)
    assert safe_path[-1] == host[0]
    assert full_path[0] == host[0]
    assert strict_path and host[0] not in strict_path
    assert resolver.resolve_component_binary("python", "python") is not None

    remove = service.remove("python")
    removed = service.wait_for_operation(remove.operation_id)
    assert removed is not None
    assert removed.state is RuntimeOperationState.COMPLETED
    python = next(
        item for item in service.status().components if item.component_id == "python"
    )
    assert python.availability is RuntimeAvailability.MISSING


def test_range_ignored_with_200_restarts_partial_from_zero(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    opener = _IgnoringRangeOpener(body)
    service = _service(tmp_path, body, opener)
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    partial = service.root / "downloads" / f"{descriptor.sha256}.part"
    partial.write_bytes(b"stale-prefix")

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None and completed.state is RuntimeOperationState.COMPLETED
    assert opener.ranges == [f"bytes={len(b'stale-prefix')}-"]
    assert service.active_runtime("python") is not None


def test_416_rebuilds_from_zero_on_same_source_before_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_RUNTIME_PACK_SOURCE_ORDER", "github,oss")
    body = _runtime_archive(tmp_path)
    opener = _RejectRangeOnceOpener(body)
    service = _service(tmp_path, body, opener)
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    partial = service.root / "downloads" / f"{descriptor.sha256}.part"
    partial.write_bytes(body[: len(body) // 2])

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None and completed.state is RuntimeOperationState.COMPLETED
    assert opener.ranges == [f"bytes={len(body) // 2}-", None]
    assert len(opener.urls) == 2
    assert all("github.example.invalid" in url for url in opener.urls)


def test_invalid_content_range_discards_partial_and_falls_back(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    opener = _InvalidContentRangeOpener(body)
    service = _service(tmp_path, body, opener)
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    partial = service.root / "downloads" / f"{descriptor.sha256}.part"
    partial.write_bytes(body[: len(body) // 2])

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None and completed.state is RuntimeOperationState.FAILED
    assert completed.error is not None and completed.error.code == "INVALID_RANGE"
    assert opener.ranges == [f"bytes={len(body) // 2}-", None]
    assert not partial.exists()


def test_partial_symlink_is_replaced_without_touching_external_file(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    external = tmp_path / "external-partial"
    external.write_bytes(b"must stay unchanged")
    partial = service.root / "downloads" / f"{descriptor.sha256}.part"
    try:
        partial.symlink_to(external)
    except OSError as exc:  # pragma: no cover - restricted Windows agents
        pytest.skip(f"symlinks unavailable: {exc}")

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None and completed.state is RuntimeOperationState.COMPLETED
    assert external.read_bytes() == b"must stay unchanged"


def test_corrupt_archive_never_activates(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    corrupt = body[:-1] + bytes([body[-1] ^ 1])
    opener = _MemoryOpener(corrupt)
    service = _service(tmp_path, body, opener)

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id, timeout=10)

    assert completed is not None
    assert completed.state is RuntimeOperationState.FAILED
    assert completed.error is not None
    assert completed.error.code == "VERIFICATION_FAILED"
    assert service.active_runtime("python") is None


def test_missing_pack_strict_mode_never_inherits_host_path(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    resolver = RuntimePackResolver(service)

    assert resolver.path_for(
        RunMode.SAFE,
        (Path(os.environ.get("PATH", "/usr/bin")),),
        require_managed=True,
    ) == ()


def test_failed_update_keeps_historical_activation_ready_and_removable(
    tmp_path: Path,
) -> None:
    old_body = _runtime_archive(tmp_path)
    original = _service(tmp_path, old_body, _MemoryOpener(old_body))
    installed = original.start_install("python")
    assert original.wait_for_operation(installed.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    old_active = original.active_runtime("python")
    assert old_active is not None
    old_package = old_active.package

    new_body = _runtime_archive(
        tmp_path,
        catalog_version="test.2",
        version="3.13.15+test",
    )
    corrupt = new_body[:-1] + bytes([new_body[-1] ^ 1])
    updated = RuntimePackService(
        _catalog(
            new_body,
            catalog_version="test.2",
            version="3.13.15+test",
            trusted_archive_sha256=(hashlib.sha256(old_body).hexdigest(),),
        ),
        root=original.root,
        target="linux-x64",
        source_bases={
            RuntimeSource.GITHUB: "https://github.example.invalid/runtime-packs",
            RuntimeSource.OSS: "https://oss.example.invalid/runtime-packs",
        },
        opener=_MemoryOpener(corrupt),
    )

    before = updated.active_runtime("python")
    assert before is not None
    assert before.version == "3.13.14+test"
    operation = updated.start_install("python")
    failed = updated.wait_for_operation(operation.operation_id, timeout=10)
    assert failed is not None and failed.state is RuntimeOperationState.FAILED
    after = updated.active_runtime("python")
    assert after is not None
    assert after.version == "3.13.14+test"

    removal = updated.remove("python")
    assert updated.wait_for_operation(removal.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    assert not old_package.exists()
    assert updated.active_runtime("python") is None


def test_integrity_status_is_cached_until_invalidated(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    operation = service.start_install("python")
    assert service.wait_for_operation(operation.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    original = runtime_pack_manager._payload_manifest
    calls = 0

    def counted(package: Path) -> tuple[dict[str, dict[str, object]], int]:
        nonlocal calls
        calls += 1
        return original(package)

    monkeypatch.setattr(runtime_pack_manager, "_payload_manifest", counted)
    assert service.active_runtime("python") is not None
    assert service.active_runtime("python") is not None
    assert calls == 1

    active = service.active_runtime("python")
    assert active is not None
    (active.package / "licenses" / "LICENSE.txt").write_text(
        "tampered\n", encoding="utf-8"
    )
    # Arbitrary dependency mutations are bounded by the short TTL; explicit
    # state transitions invalidate immediately.
    assert service.active_runtime("python") is not None
    service.invalidate_integrity_cache("python")
    assert service.active_runtime("python") is None
    assert calls == 2


def test_unknown_linux_libc_is_not_supported(monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime_pack_manager, "sys_platform_is_linux", lambda: True)
    monkeypatch.setattr(runtime_pack_manager.Path, "exists", lambda _path: False)
    monkeypatch.setattr(runtime_pack_manager.platform_module, "libc_ver", lambda: ("", ""))
    monkeypatch.delattr(runtime_pack_manager.os, "confstr", raising=False)

    assert runtime_pack_manager._host_is_supported_linux_libc() is False


def test_state_scope_selects_configured_runtime_pack_root(tmp_path: Path) -> None:
    with runtime_pack_state_scope(tmp_path / "custom-state"):
        assert runtime_packs_root() == (
            tmp_path / "custom-state" / "runtime-packs" / "v1"
        )


def test_archive_with_unexpected_root_is_rejected_and_discarded(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path, extra_root=True)
    service = _service(tmp_path, body, _MemoryOpener(body))
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None
    assert completed.state is RuntimeOperationState.FAILED
    assert not (service.root / "downloads" / f"{descriptor.sha256}.part").exists()
    assert not (service.root / "downloads" / f"{descriptor.sha256}.meta.json").exists()
    assert service.active_runtime("python") is None


def test_pinned_unpacked_limit_is_enforced_before_activation(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    service = RuntimePackService(
        _catalog(body, unpacked_size_bytes=1),
        root=tmp_path / "state",
        target="linux-x64",
        opener=_MemoryOpener(body),
    )
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None

    operation = service.start_install("python")
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None and completed.state is RuntimeOperationState.FAILED
    assert not (service.root / "downloads" / f"{descriptor.sha256}.part").exists()
    assert service.active_runtime("python") is None


def test_node_layout_requires_node_npm_and_npx(tmp_path: Path) -> None:
    package = tmp_path / "node-package"
    bin_dir = package / "payload" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("node", "npm", "npx"):
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    (package / "licenses").mkdir()
    (package / "SBOM.spdx.json").write_text("{}\n", encoding="utf-8")
    manifest = {
        "schemaVersion": 1,
        "catalogVersion": "test.1",
        "componentId": "node",
        "target": "linux-x64",
        "version": "24.18.1",
        "binDirs": ["bin"],
        "executables": {"node": "bin/node"},
    }
    manifest_path = package / "pack-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(runtime_pack_manager.RuntimePackError, match="required executables"):
        runtime_pack_manager._load_pack_layout_identity(
            package,
            catalog_version="test.1",
            component_id="node",
            target="linux-x64",
            version="24.18.1",
            activated=False,
        )

    manifest["executables"] = {
        "node": "bin/node",
        "npm": "bin/npm",
        "npx": "bin/npx",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    layout = runtime_pack_manager._load_pack_layout_identity(
        package,
        catalog_version="test.1",
        component_id="node",
        target="linux-x64",
        version="24.18.1",
        activated=False,
    )
    assert set(layout.executables) == {"node", "npm", "npx"}


def test_activation_keeps_only_current_and_one_previous_package(tmp_path: Path) -> None:
    root = tmp_path / "state"
    previous_digests: tuple[str, ...] = ()
    services: list[RuntimePackService] = []
    for index, version in enumerate(
        ("3.13.14+test", "3.13.15+test", "3.13.16+test"),
        start=1,
    ):
        catalog_version = f"test.{index}"
        body = _runtime_archive(
            tmp_path,
            catalog_version=catalog_version,
            version=version,
        )
        service = RuntimePackService(
            _catalog(
                body,
                catalog_version=catalog_version,
                version=version,
                trusted_archive_sha256=previous_digests,
            ),
            root=root,
            target="linux-x64",
            source_bases={
                RuntimeSource.GITHUB: "https://github.example.invalid/runtime-packs",
                RuntimeSource.OSS: "https://oss.example.invalid/runtime-packs",
            },
            opener=_MemoryOpener(body),
        )
        operation = service.start_install("python")
        assert service.wait_for_operation(operation.operation_id).state is (
            RuntimeOperationState.COMPLETED
        )
        services.append(service)
        previous_digests = (hashlib.sha256(body).hexdigest(),)

    packages = runtime_pack_manager._component_package_paths(root, "python")
    assert {path.parent.name for path in packages} == {
        "3.13.15+test",
        "3.13.16+test",
    }
    active = services[-1].active_runtime("python")
    assert active is not None and active.version == "3.13.16+test"


def test_remove_discards_component_downloads_but_preserves_other_components(
    tmp_path: Path,
) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    install = service.start_install("python")
    assert service.wait_for_operation(install.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    downloads = service.root / "downloads"
    historical = "a" * 64
    unrelated = "b" * 64
    for digest, component_id in (
        (descriptor.sha256, "python"),
        (historical, "python"),
        (unrelated, "node"),
    ):
        (downloads / f"{digest}.part").write_bytes(b"partial")
        (downloads / f"{digest}.meta.json").write_text(
            json.dumps({"componentId": component_id, "sha256": digest}),
            encoding="utf-8",
        )

    removal = service.remove("python")
    assert service.wait_for_operation(removal.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    assert not (downloads / f"{descriptor.sha256}.part").exists()
    assert not (downloads / f"{historical}.part").exists()
    assert not (downloads / f"{historical}.meta.json").exists()
    assert (downloads / f"{unrelated}.part").is_file()
    assert (downloads / f"{unrelated}.meta.json").is_file()


def test_untrusted_historical_activation_is_not_added_to_path(tmp_path: Path) -> None:
    old_body = _runtime_archive(tmp_path)
    original = _service(tmp_path, old_body, _MemoryOpener(old_body))
    install = original.start_install("python")
    assert original.wait_for_operation(install.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    new_body = _runtime_archive(
        tmp_path,
        catalog_version="test.2",
        version="3.13.15+test",
    )
    updated = RuntimePackService(
        _catalog(new_body, catalog_version="test.2", version="3.13.15+test"),
        root=original.root,
        target="linux-x64",
        opener=_MemoryOpener(new_body),
    )

    assert updated.active_runtime("python") is None
    python = next(
        item for item in updated.status().components if item.component_id == "python"
    )
    assert python.availability is RuntimeAvailability.CORRUPT


def test_active_runtime_rejects_receipt_for_a_different_target(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    install = service.start_install("python")
    assert service.wait_for_operation(install.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None

    assert runtime_pack_manager._active_runtime(
        service.root,
        "python",
        required_target="darwin-x64",
        trusted_archive_sha256=frozenset({descriptor.sha256}),
    ) is None


def test_operation_start_failure_is_persisted_instead_of_escaping(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("cannot start thread at /private/user/path")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    operation = service.start_install("python")

    assert operation.state is RuntimeOperationState.FAILED
    assert operation.error is not None
    assert operation.error.code == "INSTALL_FAILED"
    assert "/private/user/path" not in operation.error.message
    assert not service._threads


def test_cancel_does_not_promise_cancellation_after_activation_boundary(
    tmp_path: Path,
) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    operation = service._write_operation(
        service._new_operation(
            "python",
            RuntimeOperationKind.INSTALL,
            total_bytes=len(body),
        )
    )
    operation = service._update_operation(
        operation,
        state=RuntimeOperationState.ACTIVATING,
    )
    event = threading.Event()
    service._cancel_events[operation.operation_id] = event

    returned = service.cancel("python", operation.operation_id)

    assert returned.state is RuntimeOperationState.ACTIVATING
    assert not event.is_set()


def test_cancel_during_probe_keeps_old_activation_and_resumable_download(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    old_body = _runtime_archive(tmp_path)
    original = _service(tmp_path, old_body, _MemoryOpener(old_body))
    install = original.start_install("python")
    assert original.wait_for_operation(install.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    new_body = _runtime_archive(
        tmp_path,
        catalog_version="test.2",
        version="3.13.15+test",
    )
    service = RuntimePackService(
        _catalog(
            new_body,
            catalog_version="test.2",
            version="3.13.15+test",
            trusted_archive_sha256=(hashlib.sha256(old_body).hexdigest(),),
        ),
        root=original.root,
        target="linux-x64",
        opener=_MemoryOpener(new_body),
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_probe(*_args: object) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(runtime_pack_manager, "_run_probe", blocking_probe)
    operation = service.start_install("python")
    assert entered.wait(5)
    cancelling = service.cancel("python", operation.operation_id)
    assert cancelling.state is RuntimeOperationState.CANCELLING
    release.set()
    completed = service.wait_for_operation(operation.operation_id)

    assert completed is not None and completed.state is RuntimeOperationState.CANCELLED
    active = service.active_runtime("python")
    assert active is not None and active.version == "3.13.14+test"
    descriptor = service.catalog.descriptor("linux-x64", "python")
    assert descriptor is not None
    assert (service.root / "downloads" / f"{descriptor.sha256}.part").is_file()
    assert not any((service.root / "staging").iterdir())


def test_second_service_does_not_recover_or_clean_live_install(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    body = _runtime_archive(tmp_path)
    opener = _MemoryOpener(body)
    service = _service(tmp_path, body, opener)
    entered = threading.Event()
    release = threading.Event()

    def blocking_probe(*_args: object) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(runtime_pack_manager, "_run_probe", blocking_probe)
    operation = service.start_install("python")
    assert entered.wait(5)
    staging_before = tuple((service.root / "staging").iterdir())

    second = RuntimePackService(
        service.catalog,
        root=service.root,
        target="linux-x64",
        opener=opener,
    )

    observed = second._read_operation("python")
    assert observed is not None
    assert observed.operation_id == operation.operation_id
    assert observed.state is RuntimeOperationState.PROBING
    assert tuple((service.root / "staging").iterdir()) == staging_before
    release.set()
    completed = service.wait_for_operation(operation.operation_id)
    assert completed is not None and completed.state is RuntimeOperationState.COMPLETED
    assert service.active_runtime("python") is not None


def _race_first_operation_reads(
    services: tuple[RuntimePackService, RuntimePackService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    for service in services:
        original = service._read_operation
        first = True

        def synchronized_read(
            component_id: str,
            *,
            _original=original,
            _first=[first],
        ):
            if _first[0]:
                _first[0] = False
                barrier.wait(5)
                return _original(component_id)
            return _original(component_id)

        monkeypatch.setattr(service, "_read_operation", synchronized_read)


def _wait_shared_operation(
    service: RuntimePackService,
    operation_id: str,
) -> Any:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = service._read_operation("python")
        if (
            operation is not None
            and operation.operation_id == operation_id
            and operation.state.terminal
        ):
            return operation
        time.sleep(0.01)
    return service._read_operation("python")


def test_two_services_share_one_cross_process_install_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _runtime_archive(tmp_path)
    opener = _MemoryOpener(body)
    first = _service(tmp_path, body, opener)
    second = RuntimePackService(
        first.catalog,
        root=first.root,
        target="linux-x64",
        opener=opener,
    )
    _race_first_operation_reads((first, second), monkeypatch)
    results: list[Any] = []
    threads = [
        threading.Thread(
            target=lambda service=service: results.append(
                service.start_install("python")
            )
        )
        for service in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 2
    assert results[0].operation_id == results[1].operation_id
    completed = _wait_shared_operation(first, results[0].operation_id)
    assert completed is not None and completed.state is RuntimeOperationState.COMPLETED
    assert len(opener.ranges) == 1


def test_two_services_share_one_cross_process_remove_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _runtime_archive(tmp_path)
    opener = _MemoryOpener(body)
    first = _service(tmp_path, body, opener)
    install = first.start_install("python")
    assert first.wait_for_operation(install.operation_id).state is RuntimeOperationState.COMPLETED
    second = RuntimePackService(
        first.catalog,
        root=first.root,
        target="linux-x64",
        opener=opener,
    )
    _race_first_operation_reads((first, second), monkeypatch)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda service=service: results.append(service.remove("python")))
        for service in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(results) == 2
    assert results[0].operation_id == results[1].operation_id
    completed = _wait_shared_operation(first, results[0].operation_id)
    assert completed is not None and completed.state is RuntimeOperationState.COMPLETED
    assert first.active_runtime("python") is None


def test_service_recovers_interrupted_operation_when_all_component_locks_are_free(
    tmp_path: Path,
) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    operation = service._write_operation(
        service._new_operation(
            "python",
            RuntimeOperationKind.INSTALL,
            total_bytes=len(body),
        )
    )
    stale_staging = service.root / "staging" / "python-stale"
    stale_staging.mkdir()
    stale_trash = service.root / "trash" / "python-stale"
    stale_trash.mkdir()

    recovered = RuntimePackService(
        service.catalog,
        root=service.root,
        target="linux-x64",
        opener=_MemoryOpener(body),
    )

    observed = recovered._read_operation("python")
    assert observed is not None
    assert observed.operation_id == operation.operation_id
    assert observed.state is RuntimeOperationState.INTERRUPTED
    assert not stale_staging.exists()
    assert not stale_trash.exists()


def test_same_version_receipt_failure_restores_previous_active_package(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    old_body = _runtime_archive(tmp_path, catalog_version="test.1")
    original = _service(tmp_path, old_body, _MemoryOpener(old_body))
    installed = original.start_install("python")
    assert original.wait_for_operation(installed.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    old_active = original.active_runtime("python")
    assert old_active is not None
    old_manifest = (old_active.package / "pack-manifest.json").read_bytes()

    new_body = _runtime_archive(tmp_path, catalog_version="test.2")
    updated = RuntimePackService(
        _catalog(
            new_body,
            catalog_version="test.2",
            trusted_archive_sha256=(hashlib.sha256(old_body).hexdigest(),),
        ),
        root=original.root,
        target="linux-x64",
        opener=_MemoryOpener(new_body),
    )

    def fail_activation(*_args: object) -> None:
        raise OSError("injected active receipt failure")

    monkeypatch.setattr(runtime_pack_manager, "_write_activation", fail_activation)
    operation = updated.start_install("python")
    failed = updated.wait_for_operation(operation.operation_id)

    assert failed is not None and failed.state is RuntimeOperationState.FAILED
    updated.invalidate_integrity_cache("python")
    restored = updated.active_runtime("python")
    assert restored is not None
    assert (restored.package / "pack-manifest.json").read_bytes() == old_manifest
    assert not any((updated.root / "trash").iterdir())


def test_remove_never_follows_component_package_symlink(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    component_root = service.root / "packages" / "python"
    component_root.symlink_to(external, target_is_directory=True)

    removal = service.remove("python")
    assert service.wait_for_operation(removal.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_active_runtime_rejects_symlinked_package_ancestor(tmp_path: Path) -> None:
    body = _runtime_archive(tmp_path)
    service = _service(tmp_path, body, _MemoryOpener(body))
    install = service.start_install("python")
    assert service.wait_for_operation(install.operation_id).state is (
        RuntimeOperationState.COMPLETED
    )
    component_root = service.root / "packages" / "python"
    relocated = service.root / "trash" / "relocated-python"
    component_root.rename(relocated)
    component_root.symlink_to(relocated, target_is_directory=True)
    service.invalidate_integrity_cache("python")

    assert service.active_runtime("python") is None
    python = next(
        item for item in service.status().components if item.component_id == "python"
    )
    assert python.availability is RuntimeAvailability.CORRUPT


def test_unavailable_status_never_exposes_catalog_path(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import opensquilla.runtime_packs as runtime_packs

    secret_path = tmp_path / "private" / "runtime-pack-catalog.json"

    def unavailable(_state_dir: object = None) -> RuntimePackService:
        raise RuntimePackCatalogError(f"could not read {secret_path}")

    monkeypatch.setattr(runtime_packs, "get_runtime_pack_service", unavailable)
    status = runtime_packs.status_snapshot()

    assert status.management_supported is False
    assert all(
        component.last_error is not None
        and str(secret_path) not in component.last_error.message
        for component in status.components
    )
