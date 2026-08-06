from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
import runpy
import shutil
import stat
import subprocess
import tarfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import opensquilla.skills.toolchains.runtime as toolchain_runtime
from opensquilla.skills.toolchains import manager, registry
from opensquilla.skills.toolchains.manager import (
    DownloadVerificationError,
    ToolchainProbeError,
    UnsafeArchiveError,
)
from opensquilla.skills.toolchains.registry import ToolchainDescriptor, UnknownComponentError
from opensquilla.skills.toolchains.runtime import (
    managed_env,
    resolve_managed_binary,
    resolve_managed_resource,
)


def _descriptor(
    *,
    version: str = "test-v1",
    size: int = 1,
    sha256: str = "0" * 64,
) -> ToolchainDescriptor:
    return ToolchainDescriptor(
        component_id="paper-tex",
        display_name="Synthetic paper toolchain",
        version=version,
        platform_key="test-x64",
        supported=True,
        unsupported_reason=None,
        url="https://example.invalid/paper.tar.xz",
        sha256=sha256,
        size=size,
        install_backend="archive",
        brew_formula=None,
        archive_type="tar.xz",
        archive_root="Bundle",
        bin_relpaths=("Bundle/bin",),
        probe_commands=(),
        post_install=None,
        package_closure=(),
        auxiliary_assets=(),
        license="Test-only",
        license_url="https://example.invalid/license",
        source="https://example.invalid/source",
        closure_source=None,
        notes="Synthetic fixture",
    )


def _write_tar_xz(
    path: Path,
    files: dict[str, bytes],
    *,
    executable: set[str] | None = None,
) -> None:
    executable = executable or set()
    with tarfile.open(path, "w:xz", preset=0) as archive:
        directories: set[str] = set()
        for name in files:
            parent = Path(name).parent
            while str(parent) not in {"", "."}:
                if parent.is_absolute() or parent.parent == parent:
                    break
                directories.add(parent.as_posix())
                parent = parent.parent
        for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
            info = tarfile.TarInfo(f"{directory}/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name in executable else 0o644
            archive.addfile(info, io.BytesIO(data))


def _write_special_tar(path: Path, name: str, entry_type: bytes) -> None:
    with tarfile.open(path, "w:xz", preset=0) as archive:
        info = tarfile.TarInfo(name)
        info.type = entry_type
        info.linkname = "target"
        archive.addfile(info)


def _make_executable(path: Path, content: bytes = b"test") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def _write_runtime_activation(
    state_root: Path,
    descriptor: ToolchainDescriptor,
    *,
    extra_files: int = 0,
    resource_payloads: dict[str, bytes] | None = None,
) -> tuple[Path, Path]:
    package = (
        state_root
        / "packages"
        / descriptor.component_id
        / descriptor.version
        / descriptor.platform_key
    )
    executable_name = descriptor.probe_commands[0][0] if descriptor.probe_commands else "paperbin"
    executable = package / descriptor.bin_relpaths[0] / executable_name
    _make_executable(executable, b"verified payload")
    for index in range(extra_files):
        payload = package / "Bundle" / "share" / f"payload-{index:04d}.txt"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(f"payload-{index:04d}".encode())
    resource_payloads = resource_payloads or {}
    for asset in descriptor.auxiliary_assets:
        resource = package.joinpath(*Path(asset.destination).parts)
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_bytes(resource_payloads.get(asset.asset_id, b"font"))
    (package / ".opensquilla-toolchain.json").write_text(
        json.dumps(manager._package_marker(descriptor, package)),
        encoding="utf-8",
    )
    active = state_root / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / f"{descriptor.component_id}.json").write_text(
        json.dumps(
            {
                "component_id": descriptor.component_id,
                "version": descriptor.version,
                "platform_key": descriptor.platform_key,
                "sha256": descriptor.sha256,
                "install_backend": descriptor.install_backend,
                "package_relpath": package.relative_to(state_root).as_posix(),
                "external_root": None,
                "bin_relpaths": list(descriptor.bin_relpaths),
                "resources": {
                    asset.asset_id: asset.destination for asset in descriptor.auxiliary_assets
                },
            }
        ),
        encoding="utf-8",
    )
    return package, executable


def _hold_component_lock(root: str, ready: Any, release: Any) -> None:
    state_root = Path(root)
    manager._ensure_root(state_root)
    with manager._ComponentInstallLock(state_root, "paper-tex", 5):
        ready.set()
        release.wait(5)


def test_registry_is_code_owned_and_normalizes_supported_hosts() -> None:
    assert registry.component_ids() == ("paper-tex", "media-ffmpeg")
    assert registry.normalize_platform("macOS") == "darwin"
    assert registry.normalize_platform("WIN32") == "windows"
    assert registry.normalize_arch("AMD64") == "x64"
    assert registry.normalize_arch("aarch64") == "arm64"
    assert registry.platform_key("Darwin", "arm64") == "darwin-universal"
    assert registry.platform_key("Linux", "AMD64", "musl") == "linux-musl-x64"
    assert registry.platform_key("Linux", "aarch64", "musl") == "linux-musl-arm64"

    unsupported_musl_arm = registry.describe_component(
        "paper-tex",
        platform_name="linux",
        arch="aarch64",
        libc_name="musl",
    )
    assert unsupported_musl_arm.supported is False
    assert unsupported_musl_arm.platform_key == "linux-musl-arm64"

    with pytest.raises(UnknownComponentError, match="Unknown managed toolchain"):
        registry.describe_component("https://attacker.invalid/archive")


def test_registry_detects_musl_from_the_running_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry.platform_module, "libc_ver", lambda: ("glibc", "2.40"))
    monkeypatch.setattr(registry, "_process_uses_musl", lambda: True)

    assert registry.platform_key("Linux", "AMD64") == "linux-musl-x64"
    assert registry.platform_key("Linux", "AMD64", "glibc") == "linux-x64"


def test_registry_detects_musl_from_cpython_build_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sysconfig

    monkeypatch.setattr(registry.platform_module, "libc_ver", lambda: ("", ""))
    monkeypatch.setattr(registry, "_process_uses_musl", lambda: False)
    monkeypatch.setattr(sysconfig, "get_platform", lambda: "linux-x86_64")
    monkeypatch.setattr(
        sysconfig,
        "get_config_var",
        lambda name: "x86_64-alpine-linux-musl" if name == "BUILD_GNU_TYPE" else None,
    )

    assert registry.platform_key("Linux", "AMD64") == "linux-musl-x64"
    assert registry.platform_key("Linux", "AMD64", "glibc") == "linux-x64"

    monkeypatch.setattr(sysconfig, "get_config_var", lambda _name: "x86_64-pc-linux-gnu")
    assert registry.platform_key("Linux", "AMD64") == "linux-x64"


def test_registry_caches_default_host_platform_key_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry._reset_host_platform_key_cache_after_fork()
    calls = {"system": 0, "machine": 0, "libc": 0, "loader": 0, "markers": 0}
    process_id = [os.getpid() + 10_000]

    def system() -> str:
        calls["system"] += 1
        return "Linux"

    def machine() -> str:
        calls["machine"] += 1
        return "AMD64"

    def libc_ver() -> tuple[str, str]:
        calls["libc"] += 1
        return "glibc", "2.40"

    def process_uses_musl() -> bool:
        calls["loader"] += 1
        return False

    def platform_markers() -> tuple[str, ...]:
        calls["markers"] += 1
        return ("linux-x86_64",)

    monkeypatch.setattr(registry.os, "getpid", lambda: process_id[0])
    monkeypatch.setattr(registry.platform_module, "system", system)
    monkeypatch.setattr(registry.platform_module, "machine", machine)
    monkeypatch.setattr(registry.platform_module, "libc_ver", libc_ver)
    monkeypatch.setattr(registry, "_process_uses_musl", process_uses_musl)
    monkeypatch.setattr(registry, "_sysconfig_platform_markers", platform_markers)

    try:
        assert registry.platform_key() == "linux-x64"
        assert registry.platform_key() == "linux-x64"
        assert calls == {"system": 1, "machine": 1, "libc": 1, "loader": 1, "markers": 1}

        process_id[0] += 1
        assert registry.platform_key() == "linux-x64"
        assert calls == {"system": 2, "machine": 2, "libc": 2, "loader": 2, "markers": 2}
    finally:
        registry._reset_host_platform_key_cache_after_fork()


def test_registry_pins_monthly_tinytex_full_archives_on_all_supported_hosts() -> None:
    darwin = registry.describe_component("paper-tex", platform_name="darwin", arch="arm64")
    assert darwin.supported is True
    assert darwin.version == "2026.05"
    assert darwin.size == 206_982_916
    assert darwin.sha256 == "53f55f2ec100cc4e0ba5840f8a66086c6e37aa36b9aa4c64f924165352443e92"
    assert darwin.url == (
        "https://github.com/rstudio/tinytex-releases/releases/download/v2026.05/"
        "TinyTeX-darwin-v2026.05.tar.xz"
    )
    assert darwin.post_install == "paper-capability"
    assert darwin.package_closure == ()
    assert darwin.closure_source is None
    assert darwin.total_download_size == 226_472_001
    assert {asset.asset_id for asset in darwin.auxiliary_assets} == {
        "noto-cjk-font",
        "noto-cjk-license",
    }
    assert "self-contained" in darwin.notes

    linux_platforms = {
        ("arm64", "glibc"): ("linux-arm64", ".TinyTeX/bin/aarch64-linux"),
        ("x86_64", "glibc"): ("linux-x64", ".TinyTeX/bin/x86_64-linux"),
        ("x86_64", "musl"): ("linux-musl-x64", ".TinyTeX/bin/x86_64-linuxmusl"),
    }
    for (arch, libc_name), (platform_key, bin_relpath) in linux_platforms.items():
        linux = registry.describe_component(
            "paper-tex",
            platform_name="linux",
            arch=arch,
            libc_name=libc_name,
        )
        assert linux.supported is True
        assert linux.platform_key == platform_key
        assert linux.archive_root == ".TinyTeX"
        assert linux.bin_relpaths == (bin_relpath,)

    windows = registry.describe_component("paper-tex", platform_name="windows", arch="x86_64")
    assert windows.supported is True
    assert windows.version == "2026.05"
    assert windows.archive_type == "zip"
    assert windows.archive_root == "TinyTeX"
    assert windows.bin_relpaths == ("TinyTeX/bin/windows",)
    assert windows.size == 245_928_318
    assert windows.total_download_size == 265_417_403
    assert windows.sha256 == "64eab7759cc2a17231cb84bd4a08c0da2efd074ebcabf663d8919d6411070f4d"
    assert windows.url == (
        "https://github.com/rstudio/tinytex-releases/releases/download/v2026.05/"
        "TinyTeX-v2026.05.zip"
    )
    assert windows.unsupported_reason is None
    assert "ordinary ZIP" in windows.notes


def test_media_catalog_selects_safe_backends_and_enforces_linux_floor() -> None:
    darwin = registry.describe_component("media-ffmpeg", platform_name="darwin", arch="arm64")
    assert darwin.supported is True
    assert darwin.platform_key == "darwin-arm64"
    assert darwin.install_backend == "archive"
    assert darwin.version == "8.1.2"
    assert darwin.brew_formula is None
    assert darwin.archive_type == "zip"
    assert darwin.archive_member == "ffmpeg"
    assert darwin.archive_destination == "bin/ffmpeg"
    assert darwin.size == 28_196_358
    assert darwin.sha256 == (
        "ef1aa60006c7b77ce170c1608c08d8e4ba1c30c5746f2ac986ded932d0ac2c3c"
    )
    assert darwin.source.endswith("bb1d6db29cee948f9685bcd69e6caf17d960662b")
    assert {asset.asset_id for asset in darwin.auxiliary_assets} == {
        "noto-cjk-font",
        "noto-cjk-license",
        "ffprobe-archive",
        "ffmpeg-license-summary",
        "ffmpeg-gplv3-license",
    }
    ffprobe = next(
        asset for asset in darwin.auxiliary_assets if asset.asset_id == "ffprobe-archive"
    )
    assert ffprobe.executable is True
    assert ffprobe.archive_type == "zip"
    assert ffprobe.archive_member == "ffprobe"
    assert ffprobe.destination == "bin/ffprobe"
    assert darwin.total_download_size == 75_843_158
    assert "not Apple notarization" in darwin.notes

    darwin_x64 = registry.describe_component(
        "media-ffmpeg", platform_name="darwin", arch="x86_64"
    )
    assert darwin_x64.platform_key == "darwin-x64"
    assert darwin_x64.size == 33_586_778
    assert darwin_x64.total_download_size == 86_592_623

    old_macos = registry.describe_component(
        "media-ffmpeg",
        platform_name="darwin",
        arch="x86_64",
        macos_version="11.7.10",
    )
    assert old_macos.supported is False
    assert "macOS 12" in (old_macos.unsupported_reason or "")

    linux = registry.describe_component(
        "media-ffmpeg",
        platform_name="linux",
        arch="x86_64",
        libc_name="glibc",
        libc_version="2.31",
        kernel_release="5.15.0",
    )
    assert linux.supported is True
    assert linux.version == "7.1.5-2026.06.30"
    assert linux.install_backend == "archive"
    assert linux.archive_type == "tar.xz"
    assert linux.size == 118_937_200
    assert linux.total_download_size == 138_426_285
    assert linux.sha256 == ("f0c580f5f12af54e8c9c649c70b2d25f264edb35393203d34b20cf4f9c126288")

    old_glibc = registry.describe_component(
        "media-ffmpeg",
        platform_name="linux",
        arch="x86_64",
        libc_name="glibc",
        libc_version="2.27",
        kernel_release="5.15.0",
    )
    assert old_glibc.supported is False
    assert "glibc 2.28" in (old_glibc.unsupported_reason or "")
    old_kernel = registry.describe_component(
        "media-ffmpeg",
        platform_name="linux",
        arch="arm64",
        libc_name="glibc",
        libc_version="2.31",
        kernel_release="4.17.9",
    )
    assert old_kernel.supported is False
    assert "kernel 4.18" in (old_kernel.unsupported_reason or "")
    windows = registry.describe_component("media-ffmpeg", platform_name="windows", arch="amd64")
    assert windows.version == "8.1.2"


@pytest.mark.parametrize(
    ("name", "entry_type"),
    [
        ("Bundle/device", tarfile.CHRTYPE),
        ("Bundle/fifo", tarfile.FIFOTYPE),
    ],
)
def test_tar_extraction_rejects_links_devices_and_special_entries(
    tmp_path: Path,
    name: str,
    entry_type: bytes,
) -> None:
    archive = tmp_path / "unsafe.tar.xz"
    _write_special_tar(archive, name, entry_type)
    with pytest.raises(UnsafeArchiveError, match="device or special"):
        manager._extract_archive(archive, tmp_path / "out", "tar.xz", archive.stat().st_size)


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs host policy")
def test_tar_extraction_allows_valid_internal_symlink_chains_and_hardlinks(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "links.tar.xz"
    with tarfile.open(archive_path, "w:xz", preset=0) as archive:
        for name, data in (
            ("Bundle/bin/universal/xetex", b"xetex"),
            ("Bundle/texmf-dist/scripts/texlive/mktexlsr", b"mktexlsr"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
        for name, linkname in (
            ("Bundle/bin/universal/xelatex", "xetex"),
            ("Bundle/bin/universal/latex", "xelatex"),
            (
                "Bundle/bin/universal/mktexlsr",
                "../../texmf-dist/scripts/texlive/mktexlsr",
            ),
        ):
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = linkname
            archive.addfile(info)
        hardlink = tarfile.TarInfo("Bundle/bin/universal/xetex-hard")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "Bundle/bin/universal/xetex"
        archive.addfile(hardlink)

    output = tmp_path / "out"
    manager._extract_archive(archive_path, output, "tar.xz", archive_path.stat().st_size)
    assert (output / "Bundle/bin/universal/xelatex").is_symlink()
    assert os.readlink(output / "Bundle/bin/universal/xelatex") == "xetex"
    assert (output / "Bundle/bin/universal/latex").read_bytes() == b"xetex"
    assert (output / "Bundle/bin/universal/mktexlsr").read_bytes() == b"mktexlsr"
    assert (output / "Bundle/bin/universal/xetex-hard").stat().st_ino == (
        output / "Bundle/bin/universal/xetex"
    ).stat().st_ino


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("Bundle/bin/escape", "../../../outside"),
        ("Bundle/bin/absolute", "/tmp/outside"),
        ("Bundle/bin/drive", "C:/outside"),
        ("Bundle/bin/backslash", "..\\outside"),
        ("Bundle/bin/missing", "not-present"),
    ],
)
def test_tar_extraction_rejects_unsafe_or_missing_link_targets(
    tmp_path: Path,
    name: str,
    target: str,
) -> None:
    archive_path = tmp_path / "bad-link.tar.xz"
    with tarfile.open(archive_path, "w:xz", preset=0) as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
    with pytest.raises(UnsafeArchiveError):
        manager._extract_archive(
            archive_path,
            tmp_path / "out",
            "tar.xz",
            archive_path.stat().st_size,
        )


def test_tar_extraction_rejects_cyclic_and_link_ancestor_chains(tmp_path: Path) -> None:
    cyclic = tmp_path / "cyclic.tar.xz"
    with tarfile.open(cyclic, "w:xz", preset=0) as archive:
        for name, target in (("Bundle/a", "b"), ("Bundle/b", "a")):
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    with pytest.raises(UnsafeArchiveError, match="cyclic"):
        manager._extract_archive(cyclic, tmp_path / "cycle-out", "tar.xz", cyclic.stat().st_size)

    ancestor = tmp_path / "ancestor.tar.xz"
    with tarfile.open(ancestor, "w:xz", preset=0) as archive:
        directory = tarfile.TarInfo("Bundle/real/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        link = tarfile.TarInfo("Bundle/alias")
        link.type = tarfile.SYMTYPE
        link.linkname = "real"
        archive.addfile(link)
        child = tarfile.TarInfo("Bundle/alias/file")
        child.size = 1
        archive.addfile(child, io.BytesIO(b"x"))
    with pytest.raises(UnsafeArchiveError, match="descends through a link"):
        manager._extract_archive(
            ancestor,
            tmp_path / "ancestor-out",
            "tar.xz",
            ancestor.stat().st_size,
        )


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/windows", "bad\\path"])
def test_tar_extraction_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "unsafe.tar.xz"
    _write_tar_xz(archive, {name: b"no"})
    with pytest.raises(UnsafeArchiveError):
        manager._extract_archive(archive, tmp_path / "out", "tar.xz", archive.stat().st_size)


def test_tar_extraction_preserves_only_safe_files_and_execute_bits(tmp_path: Path) -> None:
    archive = tmp_path / "safe.tar.xz"
    _write_tar_xz(
        archive,
        {"Bundle/bin/tool": b"binary", "Bundle/share/data": b"data"},
        executable={"Bundle/bin/tool"},
    )
    output = tmp_path / "out"
    manager._extract_archive(archive, output, "tar.xz", archive.stat().st_size)
    assert (output / "Bundle/bin/tool").read_bytes() == b"binary"
    assert os.access(output / "Bundle/bin/tool", os.X_OK)
    assert (output / "Bundle/share/data").read_bytes() == b"data"


def test_single_file_archive_relocation_uses_only_cataloged_paths(tmp_path: Path) -> None:
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("ffmpeg")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        bundle.writestr(info, b"synthetic executable")
    output = tmp_path / "out"

    manager._extract_archive(archive, output, "zip", archive.stat().st_size)
    manager._relocate_cataloged_archive_member(
        output,
        member_name="ffmpeg",
        destination_name="bin/ffmpeg",
    )

    executable = output / "bin/ffmpeg"
    assert executable.read_bytes() == b"synthetic executable"
    assert os.access(executable, os.X_OK)
    assert sorted(path.relative_to(output).as_posix() for path in output.rglob("*")) == [
        "bin",
        "bin/ffmpeg",
    ]


def test_single_file_archive_relocation_rejects_extra_members(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    (output / "ffmpeg").write_bytes(b"executable")
    (output / "unexpected").write_bytes(b"extra")

    with pytest.raises(manager.UnsafeArchiveError, match="exactly its cataloged member"):
        manager._relocate_cataloged_archive_member(
            output,
            member_name="ffmpeg",
            destination_name="bin/ffmpeg",
        )


def test_extraction_size_limit_stops_decompression_bomb(tmp_path: Path, monkeypatch: Any) -> None:
    archive = tmp_path / "bomb.tar.xz"
    _write_tar_xz(archive, {"Bundle/large": b"x" * 2_000})
    monkeypatch.setattr(manager, "_MAX_EXPANSION_RATIO", 1)
    with pytest.raises(UnsafeArchiveError, match="extracted-size"):
        manager._extract_archive(archive, tmp_path / "out", "tar.xz", 1)


def test_zip_extraction_rejects_traversal_and_symlink(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../outside", b"no")
    with pytest.raises(UnsafeArchiveError, match="traversal"):
        manager._extract_archive(
            traversal, tmp_path / "traversal-out", "zip", traversal.stat().st_size
        )

    symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("Bundle/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(UnsafeArchiveError, match="link, device, or special"):
        manager._extract_archive(symlink, tmp_path / "symlink-out", "zip", symlink.stat().st_size)


class _Response(io.BytesIO):
    def __init__(
        self,
        data: bytes,
        *,
        content_length: int | None = None,
        final_url: str = "https://objects.example.invalid/artifact",
    ) -> None:
        super().__init__(data)
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}
        self._final_url = final_url

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_streaming_download_verifies_size_digest_https_and_reports_progress(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    data = b"verified artifact"
    descriptor = _descriptor(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(
        manager.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(data, content_length=len(data)),
    )
    progress: list[tuple[int, int]] = []
    destination = tmp_path / "artifact"
    manager._download(
        descriptor,
        destination,
        lambda current, total: progress.append((current, total)),
    )
    assert destination.read_bytes() == data
    assert progress[0] == (0, len(data))
    assert progress[-1] == (len(data), len(data))


@pytest.mark.parametrize("failure", ["too-large", "wrong-digest", "http-redirect"])
def test_streaming_download_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
    failure: str,
) -> None:
    data = b"artifact"
    descriptor = _descriptor(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    final_url = "https://objects.example.invalid/artifact"
    if failure == "too-large":
        descriptor = replace(descriptor, size=len(data) - 1)
    elif failure == "wrong-digest":
        descriptor = replace(descriptor, sha256="f" * 64)
    else:
        final_url = "http://objects.example.invalid/artifact"
    monkeypatch.setattr(
        manager.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(data, final_url=final_url),
    )
    with pytest.raises(DownloadVerificationError):
        manager._download(descriptor, tmp_path / failure, None)


def test_archive_payload_manifest_covers_runtime_tree_and_excludes_own_marker(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor()
    package = tmp_path / "package"
    _make_executable(package / "Bundle/bin/paperbin", b"verified executable")
    runtime_data = package / "Bundle/texmf-dist/tex/latex/example/example.sty"
    runtime_data.parent.mkdir(parents=True)
    runtime_data.write_bytes(b"verified runtime package")
    marker_path = package / ".opensquilla-toolchain.json"
    marker_path.write_text("pre-existing marker must be excluded", encoding="utf-8")

    marker = manager._package_marker(descriptor, package)
    manifest = marker["payload_manifest"]

    assert "Bundle/bin/paperbin" in manifest
    assert "Bundle/texmf-dist/tex/latex/example/example.sty" in manifest
    assert ".opensquilla-toolchain.json" not in manifest
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    assert manager.package_payload_matches(package, descriptor) is True

    runtime_data.write_bytes(b"tampered runtime package")
    assert manager.package_payload_matches(package, descriptor) is False


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation needs host policy")
def test_archive_payload_manifest_rejects_symlink_escape(tmp_path: Path) -> None:
    descriptor = _descriptor()
    package = tmp_path / "package"
    _make_executable(package / "Bundle/bin/paperbin")
    outside = tmp_path / "outside-runtime"
    outside.write_bytes(b"outside")
    (package / "Bundle/escaped-runtime").symlink_to(outside)

    with pytest.raises(manager.ToolchainError, match="symlink escaped"):
        manager._package_marker(descriptor, package)


def test_repeated_install_reuses_verified_package_without_downloading(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        archive,
        {
            f"Bundle/bin/{executable_name}": b"tool",
            "Bundle/texmf-dist/runtime.sty": b"runtime",
        },
        executable={f"Bundle/bin/{executable_name}"},
    )
    descriptor = _descriptor(size=archive.stat().st_size)
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    download_calls = 0

    def fake_download(
        _descriptor: ToolchainDescriptor,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        nonlocal download_calls
        download_calls += 1
        shutil.copyfile(archive, destination)

    monkeypatch.setattr(manager, "_download", fake_download)
    state_root = tmp_path / "state"

    first = manager.install_component("paper-tex", root=state_root)
    second = manager.install_component("paper-tex", root=state_root)

    assert download_calls == 1
    assert second == first

    (state_root / "active/paper-tex.json").unlink()
    repaired = manager.install_component("paper-tex", root=state_root)
    assert download_calls == 1
    assert repaired.receipt_id != first.receipt_id
    assert repaired.package_relpath == first.package_relpath


def test_install_accepts_cataloged_hidden_archive_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper-hidden-root.tar.xz"
    executable_relpath = f".TinyTeX/bin/test-platform/{executable_name}"
    _write_tar_xz(
        archive,
        {executable_relpath: b"tool"},
        executable={executable_relpath},
    )
    descriptor = replace(
        _descriptor(size=archive.stat().st_size),
        archive_root=".TinyTeX",
        bin_relpaths=(".TinyTeX/bin/test-platform",),
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    monkeypatch.setattr(
        manager,
        "_download",
        lambda _descriptor, destination, _progress, **_kwargs: shutil.copyfile(
            archive, destination
        ),
    )

    state_root = tmp_path / "state"
    receipt = manager.install_component("paper-tex", root=state_root)

    assert receipt.package_relpath is not None
    assert (state_root / receipt.package_relpath / executable_relpath).is_file()


def test_non_bin_runtime_tamper_forces_fresh_archive_download(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        archive,
        {
            f"Bundle/bin/{executable_name}": b"tool",
            "Bundle/texmf-dist/runtime.sty": b"verified runtime",
        },
        executable={f"Bundle/bin/{executable_name}"},
    )
    descriptor = _descriptor(size=archive.stat().st_size)
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    download_calls = 0

    def fake_download(
        _descriptor: ToolchainDescriptor,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        nonlocal download_calls
        download_calls += 1
        shutil.copyfile(archive, destination)

    monkeypatch.setattr(manager, "_download", fake_download)
    state_root = tmp_path / "state"
    receipt = manager.install_component("paper-tex", root=state_root)
    assert receipt.package_relpath is not None
    runtime_data = state_root / receipt.package_relpath / "Bundle/texmf-dist/runtime.sty"
    runtime_data.write_bytes(b"tampered runtime")

    repaired = manager.install_component("paper-tex", root=state_root)

    assert download_calls == 2
    assert repaired.receipt_id != receipt.receipt_id
    assert runtime_data.read_bytes() == b"verified runtime"


def test_install_activation_failure_rollback_and_runtime_path_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    executable_v1 = b"#!/bin/sh\nprintf 'v1'\n"
    executable_v2 = b"#!/bin/sh\nprintf 'v2'\n"
    font_v1 = b"font-v1"
    font_v2 = b"font-v2"
    archive_v1 = tmp_path / "v1.tar.xz"
    archive_v2 = tmp_path / "v2.tar.xz"
    _write_tar_xz(
        archive_v1,
        {f"Bundle/bin/{executable_name}": executable_v1},
        executable={f"Bundle/bin/{executable_name}"},
    )
    _write_tar_xz(
        archive_v2,
        {f"Bundle/bin/{executable_name}": executable_v2},
        executable={f"Bundle/bin/{executable_name}"},
    )

    def descriptor_for(version: str, archive: Path, font: bytes) -> ToolchainDescriptor:
        return replace(
            _descriptor(version=version, size=archive.stat().st_size),
            auxiliary_assets=(
                registry.AuxiliaryAssetDescriptor(
                    asset_id="test-font",
                    url=f"https://example.invalid/{version}.ttc",
                    sha256=hashlib.sha256(font).hexdigest(),
                    size=len(font),
                    destination="fonts/test-font.ttc",
                    license="OFL-1.1",
                    source="https://example.invalid/fonts",
                ),
            ),
        )

    selected = {
        "descriptor": descriptor_for("v1", archive_v1, font_v1),
        "archive": archive_v1,
    }
    real_describe_component = manager.registry.describe_component

    def fake_describe_component(component_id: str) -> ToolchainDescriptor:
        if component_id == "paper-tex":
            return selected["descriptor"]
        return real_describe_component(component_id)

    monkeypatch.setattr(manager.registry, "describe_component", fake_describe_component)

    def fake_download(
        _descriptor: ToolchainDescriptor,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        shutil.copyfile(selected["archive"], destination)

    monkeypatch.setattr(manager, "_download", fake_download)
    font_payloads = {
        "https://example.invalid/v1.ttc": font_v1,
        "https://example.invalid/v2.ttc": font_v2,
    }

    def fake_pinned_download(
        url: str,
        sha256: str,
        size: int,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        data = font_payloads[url]
        assert len(data) == size
        assert hashlib.sha256(data).hexdigest() == sha256
        destination.write_bytes(data)

    monkeypatch.setattr(manager, "_download_pinned", fake_pinned_download)
    state_root = tmp_path / "state"
    first = manager.install_component("paper-tex", root=state_root)
    active_path = state_root / "active/paper-tex.json"
    first_active = json.loads(active_path.read_text(encoding="utf-8"))
    assert first.version == "v1"
    assert first_active["receipt_id"] == first.receipt_id
    assert list((state_root / "receipts/paper-tex").glob("*.json"))

    selected.update(
        descriptor=descriptor_for("v2", archive_v2, font_v2),
        archive=archive_v2,
    )
    with pytest.raises(ToolchainProbeError, match="callback rejected"):
        manager.install_component(
            "paper-tex",
            root=state_root,
            probe_cb=lambda *_args: False,
        )
    assert json.loads(active_path.read_text(encoding="utf-8")) == first_active
    assert not (state_root / "packages/paper-tex/v2/test-x64").exists()

    second = manager.install_component("paper-tex", root=state_root)
    assert second.version == "v2"
    assert json.loads(active_path.read_text(encoding="utf-8"))["previous"]["version"] == "v1"

    system_dir = tmp_path / "system-bin"
    _make_executable(system_dir / executable_name, b"system")
    environment = managed_env({"PATH": str(system_dir)}, root=state_root)
    segments = environment["PATH"].split(os.pathsep)
    assert "packages/paper-tex/v2/test-x64/Bundle/bin" in segments[0].replace("\\", "/")
    assert segments[-1] == str(system_dir)
    assert (
        resolve_managed_binary(
            executable_name,
            root=state_root,
            base_env={"PATH": str(system_dir)},
        )
        == state_root / "packages/paper-tex/v2/test-x64/Bundle/bin" / executable_name
    )
    assert (
        resolve_managed_binary(
            executable_name,
            root=state_root,
            base_env={"PATH": ""},
        )
        == state_root / "packages/paper-tex/v2/test-x64/Bundle/bin" / executable_name
    )

    assert manager.rollback_component("paper-tex", root=state_root) is True
    rolled_back = json.loads(active_path.read_text(encoding="utf-8"))
    assert rolled_back["version"] == "v1"
    assert rolled_back["rolled_back_from"] == second.receipt_id

    changed_bins = dict(rolled_back)
    changed_bins["bin_relpaths"] = ["Bundle/other-bin"]
    active_path.write_text(json.dumps(changed_bins), encoding="utf-8")
    assert (
        resolve_managed_binary(
            executable_name,
            root=state_root,
            base_env={"PATH": ""},
        )
        is None
    )

    changed_resources = dict(rolled_back)
    changed_resources["resources"] = {"test-font": "fonts/renamed-font.ttc"}
    active_path.write_text(json.dumps(changed_resources), encoding="utf-8")
    assert (
        resolve_managed_resource(
            "test-font",
            component_id="paper-tex",
            root=state_root,
        )
        is None
    )
    active_path.write_text(json.dumps(rolled_back), encoding="utf-8")

    rolled_back_binary = state_root / "packages/paper-tex/v1/test-x64/Bundle/bin" / executable_name
    assert (
        resolve_managed_binary(
            executable_name,
            root=state_root,
            base_env={"PATH": ""},
        )
        == rolled_back_binary
    )
    if os.name == "nt":
        assert rolled_back_binary.read_bytes() == executable_v1
    else:
        completed = subprocess.run(
            [str(rolled_back_binary)],
            check=True,
            capture_output=True,
        )
        assert completed.stdout == b"v1"
    rolled_back_font = state_root / "packages/paper-tex/v1/test-x64/fonts/test-font.ttc"
    assert (
        resolve_managed_resource(
            "test-font",
            component_id="paper-tex",
            root=state_root,
        )
        == rolled_back_font
    )
    assert rolled_back_font.read_bytes() == font_v1
    assert toolchain_runtime.list_active_components(root=state_root) == (
        toolchain_runtime.ActiveComponentStatus(
            component_id="paper-tex",
            version="v1",
            platform_key="test-x64",
            install_backend="archive",
            supported=True,
        ),
    )

    # A historical marker is not a license to trust changed bytes.  The full
    # archived payload manifest gates binaries and resources after rollback.
    rolled_back_font.write_bytes(b"evil-v1")
    assert (
        resolve_managed_binary(
            executable_name,
            root=state_root,
            base_env={"PATH": ""},
        )
        is None
    )
    assert (
        resolve_managed_resource(
            "test-font",
            component_id="paper-tex",
            root=state_root,
        )
        is None
    )
    assert toolchain_runtime.list_active_components(root=state_root) == ()


def test_historical_archive_keeps_install_time_layout_and_rollback_is_atomic(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    font_v1 = b"historical-font-v1"
    font_v2 = b"current-font-v2"
    archive_v1 = tmp_path / "tool-v1.tar.xz"
    archive_v2 = tmp_path / "tool-v2.tar.xz"
    bin_v1 = "paper-build-v1/bin"
    bin_v2 = "paper-build-v2/bin"
    _write_tar_xz(
        archive_v1,
        {f"{bin_v1}/{executable_name}": b"historical executable"},
        executable={f"{bin_v1}/{executable_name}"},
    )
    _write_tar_xz(
        archive_v2,
        {f"{bin_v2}/{executable_name}": b"current executable"},
        executable={f"{bin_v2}/{executable_name}"},
    )

    def descriptor_for(
        version: str,
        archive: Path,
        bin_relpath: str,
        font: bytes,
    ) -> ToolchainDescriptor:
        return replace(
            _descriptor(version=version, size=archive.stat().st_size),
            archive_root=bin_relpath.partition("/")[0],
            bin_relpaths=(bin_relpath,),
            auxiliary_assets=(
                registry.AuxiliaryAssetDescriptor(
                    asset_id="test-font",
                    url=f"https://example.invalid/{version}.ttc",
                    sha256=hashlib.sha256(font).hexdigest(),
                    size=len(font),
                    destination=f"resources-{version}/test-font.ttc",
                    license="OFL-1.1",
                    source="https://example.invalid/fonts",
                ),
            ),
        )

    descriptors = {
        "v1": descriptor_for("v1", archive_v1, bin_v1, font_v1),
        "v2": descriptor_for("v2", archive_v2, bin_v2, font_v2),
    }
    archives = {"v1": archive_v1, "v2": archive_v2}
    fonts = {"v1": font_v1, "v2": font_v2}
    selected = {"version": "v1"}
    real_describe = manager.registry.describe_component

    def describe(component_id: str) -> ToolchainDescriptor:
        if component_id == "paper-tex":
            return descriptors[selected["version"]]
        return real_describe(component_id)

    monkeypatch.setattr(manager.registry, "describe_component", describe)
    monkeypatch.setattr(
        manager,
        "_download",
        lambda _descriptor, destination, _progress, **_kwargs: shutil.copyfile(
            archives[selected["version"]], destination
        ),
    )

    def download_font(
        _url: str,
        sha256: str,
        size: int,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        font = fonts[selected["version"]]
        assert len(font) == size
        assert hashlib.sha256(font).hexdigest() == sha256
        destination.write_bytes(font)

    monkeypatch.setattr(manager, "_download_pinned", download_font)
    state_root = tmp_path / "state"
    receipt_v1 = manager.install_component("paper-tex", root=state_root)
    package_v1 = state_root / str(receipt_v1.package_relpath)
    marker_v1 = json.loads(
        (package_v1 / ".opensquilla-toolchain.json").read_text(encoding="utf-8")
    )
    assert marker_v1["bin_relpaths"] == [bin_v1]
    assert marker_v1["resources"] == {
        "test-font": "resources-v1/test-font.ttc"
    }

    # Simulate a package installed by an earlier OpenSquilla build, before
    # package markers recorded the archive layout, then upgrade OpenSquilla
    # directly to a catalog whose archive root and resource paths have changed.
    marker_path_v1 = package_v1 / ".opensquilla-toolchain.json"
    for key in ("bin_relpaths", "resources", "auxiliary_asset_kinds"):
        marker_v1.pop(key)
    marker_path_v1.write_text(json.dumps(marker_v1), encoding="utf-8")
    selected["version"] = "v2"
    manager.invalidate_probe_cache()
    toolchain_runtime.invalidate_payload_validation_cache()

    old_binary = package_v1 / bin_v1 / executable_name
    old_font = package_v1 / "resources-v1/test-font.ttc"

    # A corroborated receipt is still insufficient when any manifest-covered
    # payload byte changed. Recovery fails without writing either state file.
    active_path = state_root / "active/paper-tex.json"
    active_v1 = active_path.read_bytes()
    marker_v1_legacy = marker_path_v1.read_bytes()
    old_font.write_bytes(b"tampered-font-v1")
    assert (
        resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})
        is None
    )
    assert active_path.read_bytes() == active_v1
    assert marker_path_v1.read_bytes() == marker_v1_legacy
    old_font.write_bytes(font_v1)
    manager.invalidate_probe_cache()
    toolchain_runtime.invalidate_payload_validation_cache()

    # An attacker-controlled active receipt cannot redirect recovery to another
    # manifest-covered directory: the durable receipt copy must corroborate the
    # layout, and a failed attempt leaves both marker and activation unchanged.
    tampered_active = json.loads(active_v1)
    tampered_active["bin_relpaths"] = ["resources-v1"]
    active_path.write_text(json.dumps(tampered_active), encoding="utf-8")
    tampered_active_bytes = active_path.read_bytes()
    assert (
        resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})
        is None
    )
    assert active_path.read_bytes() == tampered_active_bytes
    assert marker_path_v1.read_bytes() == marker_v1_legacy

    active_path.write_bytes(active_v1)
    manager.invalidate_probe_cache()
    toolchain_runtime.invalidate_payload_validation_cache()
    assert (
        resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})
        == old_binary
    )
    assert (
        resolve_managed_resource("test-font", component_id="paper-tex", root=state_root)
        == old_font
    )
    upgraded_marker_v1 = json.loads(marker_path_v1.read_text(encoding="utf-8"))
    assert upgraded_marker_v1["bin_relpaths"] == [bin_v1]
    assert upgraded_marker_v1["resources"] == {
        "test-font": "resources-v1/test-font.ttc"
    }
    assert upgraded_marker_v1["auxiliary_asset_kinds"] == {"test-font": "direct"}
    historical_probe = manager.probe_component(
        "paper-tex",
        root=state_root,
        base_env={"PATH": ""},
    )
    assert historical_probe.ready is True
    assert historical_probe.version == "v1"

    receipt_v2 = manager.install_component("paper-tex", root=state_root)
    assert receipt_v2.version == "v2"

    # Rollback independently repairs the same legacy marker when the old
    # activation is now nested as the current receipt's previous candidate.
    marker_v1 = json.loads(marker_path_v1.read_text(encoding="utf-8"))
    for key in ("bin_relpaths", "resources", "auxiliary_asset_kinds"):
        marker_v1.pop(key)
    marker_path_v1.write_text(json.dumps(marker_v1), encoding="utf-8")
    active_v2 = active_path.read_bytes()
    rollback_marker_before = marker_path_v1.read_bytes()
    tampered_rollback = json.loads(active_v2)
    tampered_rollback["previous"]["bin_relpaths"] = ["resources-v1"]
    active_path.write_text(json.dumps(tampered_rollback), encoding="utf-8")
    tampered_rollback_bytes = active_path.read_bytes()

    with pytest.raises(manager.ToolchainError, match="failed validation"):
        manager.rollback_component("paper-tex", root=state_root)

    assert active_path.read_bytes() == tampered_rollback_bytes
    assert marker_path_v1.read_bytes() == rollback_marker_before

    active_path.write_bytes(active_v2)
    manager.invalidate_probe_cache()
    toolchain_runtime.invalidate_payload_validation_cache()
    assert manager.rollback_component("paper-tex", root=state_root) is True
    assert (
        resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})
        == old_binary
    )
    assert (
        resolve_managed_resource("test-font", component_id="paper-tex", root=state_root)
        == old_font
    )

    # The inverse rollback candidate is v2. A single-file marker mutation must
    # fail before active state changes and must never report success.
    active_before = active_path.read_bytes()
    package_v2 = state_root / str(receipt_v2.package_relpath)
    marker_v2_path = package_v2 / ".opensquilla-toolchain.json"
    marker_v2 = json.loads(marker_v2_path.read_text(encoding="utf-8"))
    marker_v2["bin_relpaths"] = ["paper-build-v2/other-bin"]
    marker_v2_path.write_text(json.dumps(marker_v2), encoding="utf-8")

    with pytest.raises(manager.ToolchainError, match="failed validation"):
        manager.rollback_component("paper-tex", root=state_root)

    assert active_path.read_bytes() == active_before


def test_runtime_rejects_historical_brew_receipt_for_live_external_prefix() -> None:
    current = replace(
        _descriptor(version="v2"),
        component_id="media-ffmpeg",
        platform_key="darwin-universal",
        install_backend="brew",
        brew_formula="ffmpeg-full",
        url=None,
        sha256=None,
        size=None,
        archive_type=None,
        archive_root=None,
        bin_relpaths=("bin",),
    )
    receipt: dict[str, object] = {
        "component_id": current.component_id,
        "version": "v1",
        "platform_key": current.platform_key,
        "sha256": "",
        "install_backend": current.install_backend,
        "bin_relpaths": list(current.bin_relpaths),
    }
    marker: dict[str, object] = {
        "component_id": current.component_id,
        "version": "v1",
        "platform_key": current.platform_key,
        "sha256": None,
        "install_backend": current.install_backend,
        "payload_manifest_version": 1,
        "payload_manifest": {},
    }

    assert toolchain_runtime._descriptor_for_receipt(receipt, marker, current) is None


def test_rollback_rejects_historical_brew_before_mutating_active(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = replace(
        _descriptor(version="v2"),
        component_id="media-ffmpeg",
        platform_key="darwin-universal",
        install_backend="brew",
        brew_formula="ffmpeg-full",
        url=None,
        sha256=None,
        size=None,
        archive_type=None,
        archive_root=None,
        bin_relpaths=("bin",),
    )
    monkeypatch.setattr(
        manager.registry,
        "describe_component",
        lambda _component_id: descriptor,
    )
    state_root = tmp_path / "state"
    active_path = state_root / "active" / "media-ffmpeg.json"
    active_path.parent.mkdir(parents=True)
    current = {
        "component_id": "media-ffmpeg",
        "version": "v2",
        "platform_key": "darwin-universal",
        "install_backend": "brew",
        "previous": {
            "component_id": "media-ffmpeg",
            "version": "v1",
            "platform_key": "darwin-universal",
            "install_backend": "brew",
            "package_relpath": "packages/media-ffmpeg/v1/darwin-universal",
            "external_root": "/opt/homebrew/opt/ffmpeg-full",
        },
    }
    active_path.write_text(json.dumps(current), encoding="utf-8")
    before = active_path.read_bytes()

    with pytest.raises(manager.ToolchainError, match="Historical Homebrew"):
        manager.rollback_component("media-ffmpeg", root=state_root)

    assert active_path.read_bytes() == before


def test_rollback_serializes_with_install_per_root_and_component(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = _descriptor(version="v3")
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    first_root = tmp_path / "first-state"
    second_root = tmp_path / "second-state"
    manager._ensure_root(first_root)

    previous_package = first_root / "packages/paper-tex/v1/test-x64"
    previous_binary = previous_package / "Bundle/bin" / (
        "paperbin.exe" if os.name == "nt" else "paperbin"
    )
    _make_executable(previous_binary)
    previous_descriptor = replace(descriptor, version="v1", sha256="1" * 64)
    (previous_package / ".opensquilla-toolchain.json").write_text(
        json.dumps(manager._package_marker(previous_descriptor, previous_package)),
        encoding="utf-8",
    )
    previous = {
        "component_id": "paper-tex",
        "version": "v1",
        "platform_key": "test-x64",
        "sha256": "1" * 64,
        "install_backend": "archive",
        "package_relpath": previous_package.relative_to(first_root).as_posix(),
        "external_root": None,
        "bin_relpaths": ["Bundle/bin"],
        "resources": {},
        "activated_at_ms": 1,
        "receipt_id": "receipt-v1",
        "previous": None,
    }
    current = {
        **previous,
        "version": "v2",
        "package_relpath": "packages/paper-tex/v2/test-x64",
        "activated_at_ms": 2,
        "receipt_id": "receipt-v2",
        "previous": previous,
    }
    active_path = first_root / "active/paper-tex.json"
    manager._atomic_json(active_path, current)

    rollback_read = threading.Event()
    release_rollback = threading.Event()
    pause_guard = threading.Lock()
    pause_pending = True
    real_read_json = manager._read_json

    def pause_after_rollback_read(path: Path) -> dict[str, Any] | None:
        nonlocal pause_pending
        value = real_read_json(path)
        should_pause = False
        if path == active_path:
            with pause_guard:
                if pause_pending:
                    pause_pending = False
                    should_pause = True
        if should_pause:
            rollback_read.set()
            if not release_rollback.wait(5):
                raise AssertionError("timed out waiting to release the rollback test hook")
        return value

    monkeypatch.setattr(manager, "_read_json", pause_after_rollback_read)
    same_root_install_entered = threading.Event()
    other_root_install_entered = threading.Event()

    def fake_install_unlocked(
        _component_id: str,
        _progress_cb: object = None,
        *,
        root: Path | None = None,
        probe_cb: object = None,
    ) -> manager.ActivationReceipt:
        del probe_cb
        assert root is not None
        install_root = Path(root)
        if install_root == first_root:
            same_root_install_entered.set()
            active = real_read_json(active_path)
            assert active is not None
            receipt = {
                "component_id": descriptor.component_id,
                "version": descriptor.version,
                "platform_key": descriptor.platform_key,
                "sha256": descriptor.sha256 or "",
                "install_backend": descriptor.install_backend,
                "package_relpath": None,
                "external_root": None,
                "bin_relpaths": list(descriptor.bin_relpaths),
                "resources": {},
                "activated_at_ms": 3,
                "receipt_id": "receipt-v3",
                "previous": active,
            }
            manager._write_activation(install_root, receipt)
        else:
            assert install_root == second_root
            other_root_install_entered.set()
        return manager.ActivationReceipt(
            component_id=descriptor.component_id,
            version=descriptor.version,
            platform_key=descriptor.platform_key,
            sha256=descriptor.sha256 or "",
            install_backend=descriptor.install_backend,
            package_relpath=None,
            external_root=None,
            bin_relpaths=descriptor.bin_relpaths,
            resources={},
            activated_at_ms=3,
            receipt_id="receipt-v3",
        )

    monkeypatch.setattr(manager, "_install_component_unlocked", fake_install_unlocked)
    same_root_install_started = threading.Event()

    def install_same_root() -> manager.ActivationReceipt:
        same_root_install_started.set()
        return manager.install_component("paper-tex", root=first_root)

    with ThreadPoolExecutor(max_workers=3) as executor:
        rollback_future = executor.submit(
            manager.rollback_component,
            "paper-tex",
            root=first_root,
        )
        assert rollback_read.wait(2)
        other_install_future = executor.submit(
            manager.install_component,
            "paper-tex",
            root=second_root,
        )
        assert other_root_install_entered.wait(2)
        assert other_install_future.result(timeout=2).version == "v3"
        same_install_future = executor.submit(install_same_root)
        assert same_root_install_started.wait(2)
        try:
            assert not same_root_install_entered.wait(0.2)
        finally:
            release_rollback.set()
        assert rollback_future.result(timeout=2) is True
        assert same_install_future.result(timeout=2).version == "v3"

    final = real_read_json(active_path)
    assert final is not None
    assert final["version"] == "v3"
    assert final["previous"]["version"] == "v1"


def test_auxiliary_assets_are_pinned_receipted_and_runtime_resolvable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from opensquilla.skills.toolchains.registry import AuxiliaryAssetDescriptor

    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        archive,
        {f"Bundle/bin/{executable_name}": b"tool"},
        executable={f"Bundle/bin/{executable_name}"},
    )
    font_data = b"pinned font"
    license_data = b"pinned license"
    descriptor = replace(
        _descriptor(size=archive.stat().st_size),
        auxiliary_assets=(
            AuxiliaryAssetDescriptor(
                asset_id="test-font",
                url="https://example.invalid/font",
                sha256=hashlib.sha256(font_data).hexdigest(),
                size=len(font_data),
                destination="fonts/test-font.bin",
                license="OFL-1.1",
                source="https://example.invalid/fonts",
            ),
            AuxiliaryAssetDescriptor(
                asset_id="test-license",
                url="https://example.invalid/license.txt",
                sha256=hashlib.sha256(license_data).hexdigest(),
                size=len(license_data),
                destination="licenses/OFL.txt",
                license="OFL-1.1",
                source="https://example.invalid/fonts",
            ),
        ),
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    monkeypatch.setattr(
        manager,
        "_download",
        lambda _descriptor, destination, _progress, **_kwargs: shutil.copyfile(
            archive, destination
        ),
    )
    assets = {
        "https://example.invalid/font": font_data,
        "https://example.invalid/license.txt": license_data,
    }

    def fake_pinned_download(
        url: str,
        sha256: str,
        size: int,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        data = assets[url]
        assert len(data) == size
        assert hashlib.sha256(data).hexdigest() == sha256
        destination.write_bytes(data)

    monkeypatch.setattr(manager, "_download_pinned", fake_pinned_download)
    state_root = tmp_path / "state"
    receipt = manager.install_component("paper-tex", root=state_root)
    assert receipt.resources == {
        "test-font": "fonts/test-font.bin",
        "test-license": "licenses/OFL.txt",
    }
    assert (
        resolve_managed_resource("test-font", component_id="paper-tex", root=state_root)
        == state_root / receipt.package_relpath / "fonts/test-font.bin"
    )
    license_path = resolve_managed_resource(
        "test-license", component_id="paper-tex", root=state_root
    )
    assert license_path is not None
    assert license_path.read_bytes() == license_data


def test_auxiliary_archive_is_source_verified_manifested_and_reused(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from opensquilla.skills.toolchains.registry import AuxiliaryAssetDescriptor

    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    primary = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        primary,
        {f"Bundle/bin/{executable_name}": b"tool"},
        executable={f"Bundle/bin/{executable_name}"},
    )
    auxiliary_buffer = io.BytesIO()
    with zipfile.ZipFile(auxiliary_buffer, "w") as bundle:
        info = zipfile.ZipInfo("ffprobe")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        bundle.writestr(info, b"extracted executable")
    auxiliary_archive = auxiliary_buffer.getvalue()
    asset = AuxiliaryAssetDescriptor(
        asset_id="test-ffprobe-archive",
        url="https://example.invalid/ffprobe.zip",
        sha256=hashlib.sha256(auxiliary_archive).hexdigest(),
        size=len(auxiliary_archive),
        destination="bin/ffprobe",
        license="GPL-3.0-or-later",
        source="https://example.invalid/source",
        executable=True,
        archive_type="zip",
        archive_member="ffprobe",
    )
    descriptor = replace(
        _descriptor(size=primary.stat().st_size),
        auxiliary_assets=(asset,),
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    primary_downloads = 0
    auxiliary_downloads = 0

    def fake_primary_download(
        _descriptor: ToolchainDescriptor,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        nonlocal primary_downloads
        primary_downloads += 1
        shutil.copyfile(primary, destination)

    def fake_pinned_download(
        _url: str,
        sha256: str,
        size: int,
        destination: Path,
        _progress: object,
        **_kwargs: object,
    ) -> None:
        nonlocal auxiliary_downloads
        auxiliary_downloads += 1
        assert size == len(auxiliary_archive)
        assert sha256 == hashlib.sha256(auxiliary_archive).hexdigest()
        destination.write_bytes(auxiliary_archive)

    monkeypatch.setattr(manager, "_download", fake_primary_download)
    monkeypatch.setattr(manager, "_download_pinned", fake_pinned_download)
    state_root = tmp_path / "state"
    first = manager.install_component("paper-tex", root=state_root)
    assert first.package_relpath is not None
    installed = state_root / first.package_relpath / "bin/ffprobe"
    assert installed.read_bytes() == b"extracted executable"
    assert os.name == "nt" or os.access(installed, os.X_OK)
    marker = json.loads(
        (state_root / first.package_relpath / ".opensquilla-toolchain.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["auxiliary_assets"][asset.asset_id] == asset.sha256
    assert marker["payload_manifest"][asset.destination]["sha256"] == hashlib.sha256(
        b"extracted executable"
    ).hexdigest()

    second = manager.install_component("paper-tex", root=state_root)
    assert second == first
    assert primary_downloads == 1
    assert auxiliary_downloads == 1
    assert (
        resolve_managed_resource(asset.asset_id, component_id="paper-tex", root=state_root)
        == installed
    )

    installed.write_bytes(b"tampered")
    manager.install_component("paper-tex", root=state_root)
    assert primary_downloads == 2
    assert auxiliary_downloads == 2
    assert installed.read_bytes() == b"extracted executable"


def test_managed_env_exposes_pinned_paper_font_to_xetex(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    font_asset = registry.AuxiliaryAssetDescriptor(
        asset_id="noto-cjk-font",
        url="https://example.invalid/font",
        sha256=hashlib.sha256(b"font").hexdigest(),
        size=4,
        destination="fonts/NotoSansCJK-Regular.ttc",
        license="OFL-1.1",
        source="https://example.invalid/fonts",
    )
    descriptor = replace(
        _descriptor(),
        auxiliary_assets=(font_asset,),
    )
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    package = (
        state_root
        / "packages"
        / descriptor.component_id
        / descriptor.version
        / descriptor.platform_key
    )
    bin_dir = package / "Bundle/bin"
    _make_executable(bin_dir / ("paperbin.exe" if os.name == "nt" else "paperbin"))
    (package / "fonts").mkdir(parents=True)
    (package / "fonts/NotoSansCJK-Regular.ttc").write_bytes(b"font")
    (package / ".opensquilla-toolchain.json").write_text(
        json.dumps(manager._package_marker(descriptor, package)),
        encoding="utf-8",
    )
    active = state_root / "active"
    active.mkdir(parents=True)
    (active / "paper-tex.json").write_text(
        json.dumps(
            {
                "component_id": descriptor.component_id,
                "version": descriptor.version,
                "platform_key": descriptor.platform_key,
                "install_backend": descriptor.install_backend,
                "package_relpath": package.relative_to(state_root).as_posix(),
                "external_root": None,
                "bin_relpaths": list(descriptor.bin_relpaths),
                "resources": {"noto-cjk-font": font_asset.destination},
            }
        ),
        encoding="utf-8",
    )

    env = managed_env(
        {"PATH": "", "OSFONTDIR": "/system/fonts"},
        root=state_root,
    )

    assert env["OSFONTDIR"].split(os.pathsep) == [
        str(package / "fonts"),
        "/system/fonts",
    ]


def test_managed_env_uses_one_activation_snapshot_and_preserves_font_overrides(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    media_descriptor = replace(_descriptor(), component_id="media-ffmpeg")
    paper_descriptor = _descriptor()
    media_package = tmp_path / "media"
    paper_package = tmp_path / "paper"
    media_bin = media_package / "bin"
    paper_bin = paper_package / "bin"
    media_font = media_package / "fonts/NotoSansCJK-Regular.ttc"
    paper_font = paper_package / "fonts/NotoSansCJK-Regular.ttc"
    for path in (media_bin, paper_bin, media_font.parent, paper_font.parent):
        path.mkdir(parents=True, exist_ok=True)
    media_font.write_bytes(b"media font")
    paper_font.write_bytes(b"paper font")
    activations = {
        "media-ffmpeg": toolchain_runtime._ValidatedActivation(
            descriptor=media_descriptor,
            package=media_package,
            bin_dirs=(media_bin,),
            resources={"noto-cjk-font": "fonts/NotoSansCJK-Regular.ttc"},
        ),
        "paper-tex": toolchain_runtime._ValidatedActivation(
            descriptor=paper_descriptor,
            package=paper_package,
            bin_dirs=(paper_bin,),
            resources={"noto-cjk-font": "fonts/NotoSansCJK-Regular.ttc"},
        ),
    }
    validation_calls: list[str] = []
    state_root = tmp_path / "state"
    (state_root / "active").mkdir(parents=True)
    for component_id in activations:
        (state_root / "active" / f"{component_id}.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        toolchain_runtime.registry,
        "component_ids",
        lambda: ("paper-tex", "media-ffmpeg"),
    )
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda component_id: activations[component_id].descriptor,
    )

    def select_activation(
        _root: Path,
        descriptor: ToolchainDescriptor,
        *,
        verify_payload: bool,
    ) -> toolchain_runtime._ValidatedActivation:
        assert verify_payload is True
        validation_calls.append(descriptor.component_id)
        return activations[descriptor.component_id]

    monkeypatch.setattr(toolchain_runtime, "_validated_activation", select_activation)

    default_env = managed_env({"PATH": "/system/bin"}, root=state_root)
    operator_env = managed_env(
        {"PATH": "/system/bin", "OPENSQUILLA_MEDIA_FONTS_DIR": "/operator/fonts"},
        root=state_root,
    )
    empty_env = managed_env(
        {"PATH": "/system/bin", "OPENSQUILLA_MEDIA_FONTS_DIR": ""},
        root=state_root,
    )

    assert default_env["OPENSQUILLA_MEDIA_FONTS_DIR"] == str(media_font.parent)
    assert operator_env["OPENSQUILLA_MEDIA_FONTS_DIR"] == "/operator/fonts"
    assert empty_env["OPENSQUILLA_MEDIA_FONTS_DIR"] == ""
    assert default_env["OSFONTDIR"] == str(paper_font.parent)
    assert validation_calls == ["paper-tex", "media-ffmpeg"] * 3


def test_brew_backend_requires_bottle_and_records_external_prefix(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    brew = tmp_path / "brew"
    _make_executable(brew)
    prefix = tmp_path / "Cellar/ffmpeg-full/1.0"
    executable_suffix = ".exe" if os.name == "nt" else ""
    _make_executable(prefix / f"bin/ffmpeg{executable_suffix}")
    _make_executable(prefix / f"bin/ffprobe{executable_suffix}")
    descriptor = replace(
        _descriptor(),
        component_id="media-ffmpeg",
        platform_key="darwin-universal",
        install_backend="brew",
        brew_formula="ffmpeg-full",
        url=None,
        sha256=None,
        size=None,
        archive_type=None,
        archive_root=None,
        bin_relpaths=("bin",),
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    monkeypatch.setattr(
        manager.registry,
        "trusted_brew_executable",
        lambda: brew,
    )
    prefix_calls = 0

    def fake_prefix(*_args: object) -> Path | None:
        nonlocal prefix_calls
        prefix_calls += 1
        return None if prefix_calls == 1 else prefix

    monkeypatch.setattr(manager, "_brew_prefix", fake_prefix)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(manager, "_run_checked", fake_run)
    monkeypatch.setattr(toolchain_runtime, "_verified_brew_prefix", lambda _formula: prefix)
    state_root = tmp_path / "state"
    receipt = manager.install_component("media-ffmpeg", root=state_root)
    assert commands == [[str(brew), "install", "--force-bottle", "ffmpeg-full"]]
    assert receipt.install_backend == "brew"
    assert receipt.external_root == str(prefix)
    assert receipt.package_relpath is not None
    assert (state_root / receipt.package_relpath / ".opensquilla-toolchain.json").is_file()
    repeated = manager.install_component("media-ffmpeg", root=state_root)
    assert repeated == receipt
    assert commands == [[str(brew), "install", "--force-bottle", "ffmpeg-full"]]
    assert (
        resolve_managed_binary("ffmpeg", root=state_root, base_env={"PATH": ""})
        == prefix / f"bin/ffmpeg{executable_suffix}"
    )


def test_brew_backend_rejects_path_shadowed_brew(tmp_path: Path, monkeypatch: Any) -> None:
    malicious = tmp_path / "repo-bin/brew"
    _make_executable(malicious)
    descriptor = replace(
        _descriptor(),
        component_id="media-ffmpeg",
        install_backend="brew",
        brew_formula="ffmpeg-full",
        url=None,
        sha256=None,
        size=None,
        archive_type=None,
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _id: descriptor)
    monkeypatch.setattr(manager.registry, "trusted_brew_executable", lambda: None)
    monkeypatch.setenv("PATH", str(malicious.parent))
    with pytest.raises(manager.UnsupportedToolchainError, match="Homebrew is required"):
        manager.install_component("media-ffmpeg", root=tmp_path / "state")


def test_media_capability_probe_requires_cjk_font_filters_codecs_and_encode(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = replace(
        _descriptor(version="8.1.2"),
        component_id="media-ffmpeg",
        post_install="ffmpeg-media-capability",
    )
    bin_dir = tmp_path / "Bundle/bin"
    _make_executable(bin_dir / "ffmpeg")
    _make_executable(bin_dir / "ffprobe")
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts/NotoSansCJK-Regular.ttc").write_bytes(b"font")
    commands: list[tuple[list[str], Path]] = []
    subtitle_text: list[str] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append((command, cwd))
        if command[-1] == "-version":
            name = Path(command[0]).name
            configuration = "\nconfiguration: --enable-gpl" if name == "ffmpeg" else ""
            return subprocess.CompletedProcess(
                command,
                0,
                f"{name} version 8.1.2{configuration}\n".encode(),
                b"",
            )
        if "-filters" in command:
            return subprocess.CompletedProcess(command, 0, b"subtitles zoompan xfade", b"")
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, b"libx264 aac", b"")
        if Path(command[0]).name == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                b'{"streams":[{"codec_type":"video"},{"codec_type":"audio"}],'
                b'"format":{"duration":"1.25"}}',
                b"",
            )
        if "-c:v" in command:
            subtitle_text.append((cwd / "smoke.srt").read_text(encoding="utf-8"))
            Path(command[-1]).write_bytes(b"media")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(manager, "_run_checked", fake_run)
    manager._ffmpeg_media_capability(descriptor, tmp_path, (bin_dir,))
    encode = next(command for command, _cwd in commands if "-c:v" in command)
    filter_value = encode[encode.index("-filter_complex") + 1]
    assert encode.count("color=c=black:s=320x180:d=1:r=30") == 1
    assert "zoompan=" in filter_value
    assert "xfade=" in filter_value
    assert "fontsdir=fonts" in filter_value
    assert "Noto Sans CJK SC" in filter_value
    assert "中文烟测" in subtitle_text[0]


@pytest.mark.parametrize(
    ("version_output", "message"),
    [
        ("ffmpeg version 8.0\nconfiguration: --enable-gpl\n", "catalog version 8.1.2"),
        (
            "ffmpeg version 8.1.2\nconfiguration: --enable-gpl --enable-nonfree\n",
            "not legally redistributable",
        ),
        ("ffmpeg version 8.1.2\nconfiguration: --enable-version3\n", "cataloged GPL"),
    ],
)
def test_media_capability_rejects_wrong_or_nonredistributable_builds(
    tmp_path: Path,
    monkeypatch: Any,
    version_output: str,
    message: str,
) -> None:
    descriptor = replace(
        _descriptor(version="8.1.2"),
        component_id="media-ffmpeg",
        post_install="ffmpeg-media-capability",
    )
    bin_dir = tmp_path / "bin"
    _make_executable(bin_dir / "ffmpeg")
    _make_executable(bin_dir / "ffprobe")
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts/NotoSansCJK-Regular.ttc").write_bytes(b"font")

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        name = Path(command[0]).name
        output = version_output if name == "ffmpeg" else "ffprobe version 8.1.2\n"
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    monkeypatch.setattr(manager, "_run_checked", fake_run)
    with pytest.raises(ToolchainProbeError, match=message):
        manager._ffmpeg_media_capability(descriptor, tmp_path, (bin_dir,))


def test_macos_media_payload_replaces_and_verifies_signatures_in_order(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    payload = tmp_path / "payload"
    bin_dir = payload / "bin"
    _make_executable(bin_dir / "ffmpeg")
    _make_executable(bin_dir / "ffprobe")
    codesign = tmp_path / "codesign"
    _make_executable(codesign)
    descriptor = replace(
        _descriptor(version="8.1.2"),
        component_id="media-ffmpeg",
        platform_key="darwin-arm64",
        bin_relpaths=("bin",),
    )
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(manager, "_CODESIGN_PATH", codesign)
    monkeypatch.setattr(manager, "_run_checked", fake_run)
    manager._prepare_macos_media_payload(descriptor, payload, (bin_dir,))

    assert [command[1] for command in commands] == [
        "--remove-signature",
        "--force",
        "--verify",
        "--remove-signature",
        "--force",
        "--verify",
    ]
    assert [Path(command[-1]).name for command in commands] == [
        "ffmpeg",
        "ffmpeg",
        "ffmpeg",
        "ffprobe",
        "ffprobe",
        "ffprobe",
    ]


def test_component_install_lock_times_out_cross_process_and_releases(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    manager._ensure_root(state_root)
    with manager._ComponentInstallLock(state_root, "paper-tex", 1):
        with pytest.raises(manager.ToolchainError, match="Timed out"):
            with manager._ComponentInstallLock(state_root, "paper-tex", 0.01):
                pass
    with manager._ComponentInstallLock(state_root, "paper-tex", 0.1):
        pass

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_component_lock,
        args=(str(state_root), ready, release),
    )
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(manager.ToolchainError, match="Timed out"):
            with manager._ComponentInstallLock(state_root, "paper-tex", 0.1):
                pass
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    with manager._ComponentInstallLock(state_root, "paper-tex", 0.1):
        pass


def test_corrupt_package_retry_replaces_atomically_and_cleans_quarantine(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        archive,
        {f"Bundle/bin/{executable_name}": b"fresh"},
        executable={f"Bundle/bin/{executable_name}"},
    )
    descriptor = _descriptor(version="repair", size=archive.stat().st_size)
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    monkeypatch.setattr(
        manager,
        "_download",
        lambda _descriptor, destination, _progress, **_kwargs: shutil.copyfile(
            archive, destination
        ),
    )
    state_root = tmp_path / "state"
    receipt = manager.install_component("paper-tex", root=state_root)
    assert receipt.package_relpath is not None
    package = state_root / receipt.package_relpath
    (package / ".opensquilla-toolchain.json").write_text("corrupt", encoding="utf-8")
    (package / f"Bundle/bin/{executable_name}").write_bytes(b"corrupt")

    repaired = manager.install_component("paper-tex", root=state_root)
    assert repaired.package_relpath == receipt.package_relpath
    assert (package / f"Bundle/bin/{executable_name}").read_bytes() == b"fresh"
    assert not list(package.parent.glob(f".{package.name}.quarantine-*"))


def test_corrupt_package_activation_failure_restores_previous_bytes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        archive,
        {f"Bundle/bin/{executable_name}": b"fresh"},
        executable={f"Bundle/bin/{executable_name}"},
    )
    descriptor = _descriptor(version="restore", size=archive.stat().st_size)
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    monkeypatch.setattr(
        manager,
        "_download",
        lambda _descriptor, destination, _progress, **_kwargs: shutil.copyfile(
            archive, destination
        ),
    )
    state_root = tmp_path / "state"
    receipt = manager.install_component("paper-tex", root=state_root)
    assert receipt.package_relpath is not None
    package = state_root / receipt.package_relpath
    marker = package / ".opensquilla-toolchain.json"
    marker.write_text("corrupt-before-retry", encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "_write_activation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("receipt failed")),
    )
    with pytest.raises(RuntimeError, match="receipt failed"):
        manager.install_component("paper-tex", root=state_root)
    assert marker.read_text(encoding="utf-8") == "corrupt-before-retry"
    assert not list(package.parent.glob(f".{package.name}.quarantine-*"))


def test_corrupt_package_marker_failure_restores_previous_bytes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    archive = tmp_path / "paper.tar.xz"
    _write_tar_xz(
        archive,
        {f"Bundle/bin/{executable_name}": b"fresh"},
        executable={f"Bundle/bin/{executable_name}"},
    )
    descriptor = _descriptor(version="restore-marker", size=archive.stat().st_size)
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    monkeypatch.setattr(
        manager,
        "_download",
        lambda _descriptor, destination, _progress, **_kwargs: shutil.copyfile(
            archive, destination
        ),
    )
    state_root = tmp_path / "state"
    receipt = manager.install_component("paper-tex", root=state_root)
    assert receipt.package_relpath is not None
    package = state_root / receipt.package_relpath
    marker = package / ".opensquilla-toolchain.json"
    marker.write_text("corrupt-before-marker", encoding="utf-8")
    real_atomic_json = manager._atomic_json

    def fail_new_marker(path: Path, payload: dict[str, Any]) -> None:
        if path.name == ".opensquilla-toolchain.json":
            raise OSError("marker write failed")
        real_atomic_json(path, payload)

    monkeypatch.setattr(manager, "_atomic_json", fail_new_marker)
    with pytest.raises(OSError, match="marker write failed"):
        manager.install_component("paper-tex", root=state_root)
    assert marker.read_text(encoding="utf-8") == "corrupt-before-marker"
    assert not list(package.parent.glob(f".{package.name}.quarantine-*"))


def test_effective_capability_report_is_cached_and_invalidatable(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = replace(
        _descriptor(),
        probe_commands=(("xelatex", "--version"), ("bibtex", "--version")),
        post_install="paper-capability",
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    binaries = tmp_path / "system-bin"
    executable_suffix = ".exe" if os.name == "nt" else ""
    for name in ("xelatex", "bibtex", "kpsewhich"):
        _make_executable(binaries / f"{name}{executable_suffix}")
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "NotoSansCJK-Regular.ttc").write_bytes(b"font")
    from opensquilla.skills.toolchains import runtime as toolchain_runtime_module

    monkeypatch.setattr(
        toolchain_runtime_module,
        "managed_env",
        lambda base_env, **_kwargs: {
            **dict(base_env or {}),
            "OSFONTDIR": str(fonts),
        },
    )
    run_count = 0

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal run_count
        run_count += 1
        return subprocess.CompletedProcess(command, 0, b"", b"")

    capability_count = 0

    def fake_capability(*_args: object, **_kwargs: object) -> None:
        nonlocal capability_count
        capability_count += 1

    monkeypatch.setattr(manager, "_run_checked", fake_run)
    monkeypatch.setattr(manager, "_paper_capability", fake_capability)
    manager.invalidate_probe_cache()
    report = manager.probe_component(
        "paper-tex",
        root=tmp_path / "state",
        base_env={"PATH": str(binaries)},
    )
    assert report.supported is True
    assert report.ready is True
    assert report.reason == "ready"
    assert set(report.binaries) == {"bibtex", "kpsewhich", "xelatex"}
    assert (
        manager.probe_component(
            "paper-tex",
            root=tmp_path / "state",
            base_env={"PATH": str(binaries)},
        )
        is report
    )
    assert run_count == 2
    assert capability_count == 1
    manager.invalidate_probe_cache("paper-tex")
    manager.probe_component(
        "paper-tex",
        root=tmp_path / "state",
        base_env={"PATH": str(binaries)},
    )
    assert capability_count == 2


def test_media_capability_report_uses_active_managed_bins_and_font_env(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    from opensquilla.skills.toolchains.registry import AuxiliaryAssetDescriptor

    font_asset = AuxiliaryAssetDescriptor(
        asset_id="noto-cjk-font",
        url="https://example.invalid/font",
        sha256=hashlib.sha256(b"font").hexdigest(),
        size=4,
        destination="fonts/NotoSansCJK-Regular.ttc",
        license="OFL-1.1",
        source="https://example.invalid/fonts",
    )
    descriptor = replace(
        _descriptor(version="media-v1"),
        component_id="media-ffmpeg",
        probe_commands=(("ffmpeg", "-version"), ("ffprobe", "-version")),
        post_install="ffmpeg-media-capability",
        auxiliary_assets=(font_asset,),
    )
    monkeypatch.setattr(manager.registry, "describe_component", lambda _component: descriptor)
    state_root = tmp_path / "state"
    package = state_root / "packages/media-ffmpeg/media-v1/test-x64"
    bin_dir = package / "Bundle/bin"
    executable_suffix = ".exe" if os.name == "nt" else ""
    for name in ("ffmpeg", "ffprobe"):
        _make_executable(bin_dir / f"{name}{executable_suffix}")
    (package / "fonts").mkdir(parents=True)
    (package / "fonts/NotoSansCJK-Regular.ttc").write_bytes(b"font")
    (package / ".opensquilla-toolchain.json").write_text(
        json.dumps(manager._package_marker(descriptor, package)), encoding="utf-8"
    )
    active = state_root / "active"
    active.mkdir(parents=True)
    receipt = {
        "component_id": descriptor.component_id,
        "version": descriptor.version,
        "platform_key": descriptor.platform_key,
        "install_backend": descriptor.install_backend,
        "package_relpath": package.relative_to(state_root).as_posix(),
        "external_root": None,
        "bin_relpaths": list(descriptor.bin_relpaths),
        "resources": {"noto-cjk-font": font_asset.destination},
    }
    (active / "media-ffmpeg.json").write_text(json.dumps(receipt), encoding="utf-8")
    system_dir = tmp_path / "system"
    _make_executable(system_dir / f"ffmpeg{executable_suffix}", b"insufficient")
    _make_executable(system_dir / f"ffprobe{executable_suffix}", b"insufficient")
    monkeypatch.setattr(
        manager,
        "_run_checked",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, b"", b""),
    )
    capability_calls: list[tuple[Path, str]] = []

    def fake_media_capability(
        _descriptor: ToolchainDescriptor,
        payload: Path,
        _bin_dirs: tuple[Path, ...],
        *,
        env_override: dict[str, str],
        resource_paths: dict[str, Path],
    ) -> None:
        assert resource_paths["noto-cjk-font"] == package / font_asset.destination
        capability_calls.append((payload, env_override["PATH"]))

    monkeypatch.setattr(manager, "_ffmpeg_media_capability", fake_media_capability)
    manager.invalidate_probe_cache()
    report = manager.probe_component(
        "media-ffmpeg",
        root=state_root,
        base_env={"PATH": str(system_dir)},
    )
    assert report.ready is True
    assert Path(report.binaries["ffmpeg"]).parent == bin_dir
    assert report.resources["noto-cjk-font"] == str(package / "fonts/NotoSansCJK-Regular.ttc")
    assert capability_calls[0][0] == package
    assert capability_calls[0][1].split(os.pathsep)[0] == str(bin_dir)


def test_runtime_rejects_tampered_managed_executable_with_intact_marker(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = replace(
        _descriptor(),
        probe_commands=(("paperbin", "--version"),),
    )
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "component_ids",
        lambda: (descriptor.component_id,),
    )
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    package = (
        state_root
        / "packages"
        / descriptor.component_id
        / descriptor.version
        / descriptor.platform_key
    )
    executable_suffix = ".exe" if os.name == "nt" else ""
    executable = package / f"Bundle/bin/paperbin{executable_suffix}"
    _make_executable(executable, b"verified payload")
    marker_path = package / ".opensquilla-toolchain.json"
    marker_path.write_text(
        json.dumps(manager._package_marker(descriptor, package)),
        encoding="utf-8",
    )
    marker_before = marker_path.read_bytes()
    active = state_root / "active"
    active.mkdir(parents=True)
    (active / "paper-tex.json").write_text(
        json.dumps(
            {
                "component_id": descriptor.component_id,
                "version": descriptor.version,
                "platform_key": descriptor.platform_key,
                "install_backend": descriptor.install_backend,
                "package_relpath": package.relative_to(state_root).as_posix(),
                "external_root": None,
                "bin_relpaths": list(descriptor.bin_relpaths),
                "resources": {},
            }
        ),
        encoding="utf-8",
    )
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_calls = 0
    real_validate = toolchain_runtime.package_payload_matches

    def count_validation(package_path: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return real_validate(package_path, selected)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    assert resolve_managed_binary("paperbin", root=state_root, base_env={"PATH": ""}) == (
        executable
    )
    assert resolve_managed_binary("paperbin", root=state_root, base_env={"PATH": ""}) == (
        executable
    )
    assert validation_calls == 1

    executable.write_bytes(b"tampered payload")

    assert marker_path.read_bytes() == marker_before
    assert resolve_managed_binary("paperbin", root=state_root, base_env={"PATH": ""}) is None
    assert validation_calls == 2
    assert toolchain_runtime.list_active_components(root=state_root) == ()


def test_runtime_warm_activation_does_not_parse_or_walk_large_manifest(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    _, executable = _write_runtime_activation(state_root, descriptor, extra_files=256)
    toolchain_runtime.invalidate_payload_validation_cache()

    validation_calls = 0
    mapping_reads = 0
    lstat_calls = 0
    real_validate = toolchain_runtime.package_payload_matches
    real_read_mapping = toolchain_runtime._read_mapping
    real_lstat = Path.lstat

    def count_validation(package: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return real_validate(package, selected)

    def count_mapping(path: Path) -> dict[str, object] | None:
        nonlocal mapping_reads
        mapping_reads += 1
        return real_read_mapping(path)

    def count_lstat(path: Path) -> os.stat_result:
        nonlocal lstat_calls
        lstat_calls += 1
        return real_lstat(path)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    monkeypatch.setattr(toolchain_runtime, "_read_mapping", count_mapping)
    monkeypatch.setattr(Path, "lstat", count_lstat)

    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""}) == (
        executable
    )
    cold_reads = mapping_reads
    cold_lstats = lstat_calls
    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""}) == (
        executable
    )

    assert validation_calls == 1
    assert mapping_reads == cold_reads
    assert 0 < lstat_calls - cold_lstats <= 5


def test_runtime_skips_host_descriptor_resolution_without_active_receipt(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: (_ for _ in ()).throw(AssertionError("descriptor resolved")),
    )

    assert managed_env({"PATH": "/system/bin"}, root=tmp_path / "state") == {
        "PATH": "/system/bin"
    }


def test_real_artifact_validator_reports_bounded_runtime_hot_path(
    tmp_path: Path,
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    state_root = tmp_path / "state"
    _write_runtime_activation(state_root, descriptor, extra_files=64)
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    script = Path(__file__).resolve().parents[2] / "scripts/validate_managed_toolchain_artifacts.py"
    namespace = runpy.run_path(str(script))
    check_hot_path = namespace["_check_runtime_hot_path"]
    check_hot_path.__globals__["describe_component"] = lambda _component: descriptor

    assert check_hot_path("paper-tex", root=state_root) is True
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["event"] == "runtime_hot_path_result"
    assert event["ready"] is True
    assert event["cold_payload_validations"] == 1
    assert 0 < event["warm_lstat"] <= event["warm_lstat_budget"]
    assert event["warm_mapping_reads"] == 0
    assert event["warm_payload_validations"] == 0
    assert event["warm_stat"] <= event["warm_stat_budget"]


def test_runtime_revalidates_deep_payload_after_thirty_seconds(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    package, executable = _write_runtime_activation(state_root, descriptor, extra_files=1)
    clock = [100.0]
    monkeypatch.setattr(toolchain_runtime, "_monotonic", lambda: clock[0])
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_calls = 0
    real_validate = toolchain_runtime.package_payload_matches

    def count_validation(package_path: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return real_validate(package_path, selected)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""}) == (
        executable
    )
    (package / "Bundle/share/payload-0000.txt").write_bytes(b"tampered-0000")

    clock[0] = 129.999
    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""}) == (
        executable
    )
    assert validation_calls == 1

    clock[0] = 130.0
    assert resolve_managed_binary(
        executable_name,
        root=state_root,
        base_env={"PATH": ""},
    ) is None
    assert validation_calls == 2


def test_runtime_single_flights_concurrent_cold_validation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    _, executable = _write_runtime_activation(state_root, descriptor, extra_files=32)
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_started = threading.Event()
    release_validation = threading.Event()
    validation_calls = 0
    real_validate = toolchain_runtime.package_payload_matches

    def slow_validation(package: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        validation_started.set()
        assert release_validation.wait(2)
        return real_validate(package, selected)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", slow_validation)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                resolve_managed_binary,
                executable_name,
                root=state_root,
                base_env={"PATH": ""},
            )
            for _ in range(8)
        ]
        assert validation_started.wait(2)
        release_validation.set()
        assert [future.result(timeout=2) for future in futures] == [executable] * 8

    assert validation_calls == 1


def test_runtime_single_flights_concurrent_failed_validation_without_caching(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    _write_runtime_activation(state_root, descriptor, extra_files=32)
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_started = threading.Event()
    release_validation = threading.Event()
    waiters_joined = threading.Event()
    waiter_lock = threading.Lock()
    waiter_count = 0
    validation_calls = 0
    real_wait = toolchain_runtime._wait_for_validation_flight

    def reject_validation(_package: Path, _selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        validation_started.set()
        assert release_validation.wait(2)
        return False

    def counted_wait(flight: Any):
        nonlocal waiter_count
        with waiter_lock:
            waiter_count += 1
            if waiter_count == 7:
                waiters_joined.set()
        return real_wait(flight)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", reject_validation)
    monkeypatch.setattr(toolchain_runtime, "_wait_for_validation_flight", counted_wait)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(
                resolve_managed_binary,
                executable_name,
                root=state_root,
                base_env={"PATH": ""},
            )
            for _ in range(8)
        ]
        assert validation_started.wait(2)
        assert waiters_joined.wait(2)
        release_validation.set()
        assert [future.result(timeout=2) for future in futures] == [None] * 8

    assert validation_calls == 1
    assert toolchain_runtime._payload_validation_flights == {}
    assert resolve_managed_binary(
        executable_name,
        root=state_root,
        base_env={"PATH": ""},
    ) is None
    assert validation_calls == 2
    assert toolchain_runtime._payload_validation_flights == {}


def test_runtime_invalidation_prevents_inflight_validation_from_repopulating_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    package, _ = _write_runtime_activation(state_root, descriptor, extra_files=32)
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_complete = threading.Event()
    release_validation = threading.Event()
    validation_calls = 0
    real_validate = toolchain_runtime.package_payload_matches

    def paused_validation(package: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        result = real_validate(package, selected)
        validation_complete.set()
        assert release_validation.wait(2)
        return result

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", paused_validation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            resolve_managed_binary,
            executable_name,
            root=state_root,
            base_env={"PATH": ""},
        )
        assert validation_complete.wait(2)
        (package / "Bundle/share/payload-0000.txt").write_bytes(b"tampered-inflight")
        toolchain_runtime.invalidate_payload_validation_cache(
            "paper-tex",
            root=state_root,
        )
        release_validation.set()
        assert pending.result(timeout=2) is None

    assert resolve_managed_binary(
        executable_name,
        root=state_root,
        base_env={"PATH": ""},
    ) is None
    assert validation_calls == 2


def test_runtime_rejects_fixed_sentinel_change_during_full_validation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    _, executable = _write_runtime_activation(state_root, descriptor, extra_files=16)
    toolchain_runtime.invalidate_payload_validation_cache()
    real_validate = toolchain_runtime.package_payload_matches
    validation_calls = 0

    def mutate_after_validation(package: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        result = real_validate(package, selected)
        executable.write_bytes(b"tampered after full validation")
        return result

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", mutate_after_validation)

    assert resolve_managed_binary(
        executable_name,
        root=state_root,
        base_env={"PATH": ""},
    ) is None
    assert validation_calls == 1
    assert toolchain_runtime._payload_validation_cache == {}
    assert toolchain_runtime._payload_validation_flights == {}


def test_runtime_does_not_cache_failed_payload_validation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    _write_runtime_activation(state_root, descriptor)
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_calls = 0

    def reject_validation(_package: Path, _selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return False

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", reject_validation)
    for _ in range(2):
        assert resolve_managed_binary(
            executable_name,
            root=state_root,
            base_env={"PATH": ""},
        ) is None

    assert validation_calls == 2


def test_runtime_pid_change_drops_inherited_activation_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    _write_runtime_activation(state_root, descriptor)
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_calls = 0
    real_validate = toolchain_runtime.package_payload_matches

    def count_validation(package: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return real_validate(package, selected)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})
    current_pid = toolchain_runtime._payload_validation_cache_pid
    monkeypatch.setattr(toolchain_runtime.os, "getpid", lambda: current_pid + 1)
    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})

    assert validation_calls == 2


def test_runtime_fork_reset_discards_flights_without_signaling_inherited_events(
    tmp_path: Path,
) -> None:
    class InheritedEvent:
        def set(self) -> None:
            raise AssertionError("fork reset touched an inherited event")

    descriptor = _descriptor()
    key = toolchain_runtime._activation_cache_key(tmp_path / "state", descriptor)
    flight = toolchain_runtime._ValidationFlight()
    flight.event = InheritedEvent()  # type: ignore[assignment]
    previous_lock = toolchain_runtime._payload_validation_cache_lock
    toolchain_runtime._payload_validation_flights[key] = flight

    toolchain_runtime._reset_payload_validation_cache_after_fork()

    assert toolchain_runtime._payload_validation_flights == {}
    assert toolchain_runtime._payload_validation_cache_lock is not previous_lock


def test_activation_write_invalidates_only_changed_component_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    state_root = tmp_path / "state"
    manager._ensure_root(state_root)
    invalidated: list[tuple[str | None, Path | None]] = []
    monkeypatch.setattr(
        toolchain_runtime,
        "invalidate_payload_validation_cache",
        lambda component_id=None, *, root=None: invalidated.append((component_id, root)),
    )

    manager._write_activation(
        state_root,
        {"component_id": "paper-tex", "receipt_id": "synthetic-receipt"},
    )

    assert invalidated == [("paper-tex", state_root)]


def test_component_invalidation_preserves_other_validated_activation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_suffix = ".exe" if os.name == "nt" else ""
    paper_descriptor = replace(
        _descriptor(),
        probe_commands=((f"paperbin{executable_suffix}", "--version"),),
    )
    media_descriptor = replace(
        _descriptor(),
        component_id="media-ffmpeg",
        probe_commands=((f"mediabin{executable_suffix}", "--version"),),
    )
    descriptors = {
        "paper-tex": paper_descriptor,
        "media-ffmpeg": media_descriptor,
    }
    state_root = tmp_path / "state"
    _write_runtime_activation(state_root, paper_descriptor)
    _write_runtime_activation(state_root, media_descriptor)
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "component_ids",
        lambda: ("paper-tex", "media-ffmpeg"),
    )
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda component_id: descriptors[component_id],
    )
    toolchain_runtime.invalidate_payload_validation_cache()

    managed_env({"PATH": ""}, root=state_root)
    validation_calls: list[str] = []
    real_validate = toolchain_runtime.package_payload_matches

    def count_validation(package: Path, descriptor: ToolchainDescriptor) -> bool:
        validation_calls.append(descriptor.component_id)
        return real_validate(package, descriptor)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    toolchain_runtime.invalidate_payload_validation_cache("paper-tex")

    managed_env({"PATH": ""}, root=state_root)

    assert validation_calls == ["paper-tex"]


def test_root_invalidation_preserves_same_component_cache_in_other_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    descriptor = replace(_descriptor(), probe_commands=((executable_name, "--version"),))
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    first_root = tmp_path / "first-state"
    second_root = tmp_path / "second-state"
    first_package, _ = _write_runtime_activation(first_root, descriptor)
    _write_runtime_activation(second_root, descriptor)
    toolchain_runtime.invalidate_payload_validation_cache()
    managed_env({"PATH": ""}, root=first_root)
    managed_env({"PATH": ""}, root=second_root)
    validated_packages: list[Path] = []
    real_validate = toolchain_runtime.package_payload_matches

    def count_validation(package: Path, selected: ToolchainDescriptor) -> bool:
        validated_packages.append(package)
        return real_validate(package, selected)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    toolchain_runtime.invalidate_payload_validation_cache("paper-tex", root=first_root)

    managed_env({"PATH": ""}, root=first_root)
    managed_env({"PATH": ""}, root=second_root)

    assert validated_packages == [first_package]


@pytest.mark.parametrize("sentinel", ["receipt", "marker", "probe", "resource", "package"])
def test_runtime_sentinel_changes_force_immediate_revalidation(
    sentinel: str,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    executable_name = "paperbin.exe" if os.name == "nt" else "paperbin"
    font_payload = b"font"
    font_asset = registry.AuxiliaryAssetDescriptor(
        asset_id="noto-cjk-font",
        url="https://example.invalid/font",
        sha256=hashlib.sha256(font_payload).hexdigest(),
        size=len(font_payload),
        destination="fonts/NotoSansCJK-Regular.ttc",
        license="OFL-1.1",
        source="https://example.invalid/fonts",
    )
    descriptor = replace(
        _descriptor(),
        probe_commands=((executable_name, "--version"),),
        auxiliary_assets=(font_asset,),
    )
    monkeypatch.setattr(toolchain_runtime.registry, "component_ids", lambda: ("paper-tex",))
    monkeypatch.setattr(
        toolchain_runtime.registry,
        "describe_component",
        lambda _component: descriptor,
    )
    state_root = tmp_path / "state"
    package, executable = _write_runtime_activation(
        state_root,
        descriptor,
        resource_payloads={"noto-cjk-font": font_payload},
    )
    toolchain_runtime.invalidate_payload_validation_cache()
    validation_calls = 0
    activation_validation_calls = 0
    real_validate = toolchain_runtime.package_payload_matches
    real_activation_validate = toolchain_runtime._validated_activation_receipt

    def count_validation(package_path: Path, selected: ToolchainDescriptor) -> bool:
        nonlocal validation_calls
        validation_calls += 1
        return real_validate(package_path, selected)

    def count_activation_validation(*args: Any, **kwargs: Any):
        nonlocal activation_validation_calls
        activation_validation_calls += 1
        return real_activation_validate(*args, **kwargs)

    monkeypatch.setattr(toolchain_runtime, "package_payload_matches", count_validation)
    monkeypatch.setattr(
        toolchain_runtime,
        "_validated_activation_receipt",
        count_activation_validation,
    )
    assert resolve_managed_binary(executable_name, root=state_root, base_env={"PATH": ""})

    if sentinel == "receipt":
        receipt_path = state_root / "active/paper-tex.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["bin_relpaths"] = ["Bundle/other-bin"]
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    elif sentinel == "marker":
        marker_path = package / ".opensquilla-toolchain.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["sha256"] = "f" * 64
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
    elif sentinel == "probe":
        executable.write_bytes(b"tampered probe")
    elif sentinel == "resource":
        (package / font_asset.destination).write_bytes(b"tampered font")
    else:
        package.touch()

    resolved = resolve_managed_binary(
        executable_name,
        root=state_root,
        base_env={"PATH": ""},
    )
    assert activation_validation_calls == 2
    assert validation_calls == (1 if sentinel in {"receipt", "marker"} else 2)
    if sentinel == "package":
        assert resolved == executable
    else:
        assert resolved is None


def test_runtime_sentinel_identity_tracks_symlink_target(tmp_path: Path) -> None:
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.write_bytes(b"same payload")
    second_target.write_bytes(b"same payload")
    link = tmp_path / "probe"
    try:
        link.symlink_to(first_target.name)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    first = toolchain_runtime._path_identity(link)
    link.unlink()
    link.symlink_to(second_target.name)
    second = toolchain_runtime._path_identity(link)

    assert first is not None
    assert second is not None
    assert first.symlink_target == first_target.name
    assert second.symlink_target == second_target.name
    assert first != second


def test_native_probe_timeout_preserves_actionable_detail(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        manager.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=["ffmpeg", "-version"], timeout=3)
        ),
    )

    with pytest.raises(ToolchainProbeError, match="ffmpeg version probe timed out after 3 seconds"):
        manager._run_checked(
            ["/trusted/ffmpeg", "-version"],
            cwd=tmp_path,
            env={},
            timeout=3,
            label="ffmpeg version probe",
        )


def test_runtime_rejects_forged_external_root_and_symlink_escape(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = registry.describe_component("media-ffmpeg", platform_name="darwin", arch="arm64")
    monkeypatch.setattr(toolchain_runtime.registry, "describe_component", lambda _id: descriptor)
    state_root = tmp_path / "state"
    package = (
        state_root
        / "packages"
        / descriptor.component_id
        / descriptor.version
        / descriptor.platform_key
    )
    package.mkdir(parents=True)
    (package / ".opensquilla-toolchain.json").write_text(
        json.dumps(manager._package_marker(descriptor)), encoding="utf-8"
    )
    evil = tmp_path / "evil"
    _make_executable(evil / "bin/ffmpeg")
    active = state_root / "active"
    active.mkdir(parents=True)
    receipt = {
        "component_id": descriptor.component_id,
        "version": descriptor.version,
        "platform_key": descriptor.platform_key,
        "install_backend": descriptor.install_backend,
        "package_relpath": package.relative_to(state_root).as_posix(),
        "external_root": str(evil),
        "bin_relpaths": list(descriptor.bin_relpaths),
        "resources": {
            "noto-cjk-font": "fonts/NotoSansCJK-Regular.ttc",
        },
    }
    (active / "media-ffmpeg.json").write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(
        toolchain_runtime,
        "_verified_brew_prefix",
        lambda _formula: tmp_path / "real-prefix",
    )
    assert toolchain_runtime.managed_env({"PATH": ""}, root=state_root)["PATH"] == ""

    if os.name == "nt":
        return
    fonts = package / "fonts"
    fonts.mkdir()
    outside_font = tmp_path / "outside-font.ttc"
    outside_font.write_bytes(b"font")
    (fonts / "NotoSansCJK-Regular.ttc").symlink_to(outside_font)
    assert (
        toolchain_runtime.resolve_managed_resource(
            "noto-cjk-font", component_id="media-ffmpeg", root=state_root
        )
        is None
    )
    (fonts / "NotoSansCJK-Regular.ttc").unlink()
    fonts.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (package / ".opensquilla-toolchain.json").unlink()
    package.rmdir()
    package.symlink_to(outside, target_is_directory=True)
    (outside / ".opensquilla-toolchain.json").write_text(
        json.dumps(manager._package_marker(descriptor)), encoding="utf-8"
    )
    assert toolchain_runtime.managed_env({"PATH": ""}, root=state_root)["PATH"] == ""


def test_paper_post_install_is_self_contained_and_runs_smoke_test(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = replace(
        _descriptor(),
        post_install="paper-capability",
        package_closure=(),
        closure_source=None,
    )
    bin_dir = tmp_path / "Bundle/bin"
    for name in ("kpsewhich", "xelatex", "bibtex"):
        _make_executable(bin_dir / name)
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "NotoSansCJK-Regular.ttc").write_bytes(b"font")
    commands: list[tuple[list[str], Path]] = []
    tex_sources: list[str] = []

    def fake_run_checked(
        command: list[str], *, cwd: Path, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append((command, cwd))
        if Path(command[0]).name == "xelatex":
            (cwd / "paper.pdf").write_bytes(b"%PDF-test")
            tex_sources.append((cwd / "paper.tex").read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(manager, "_run_checked", fake_run_checked)
    manager._run_post_install(descriptor, tmp_path, (bin_dir,))

    tlmgr_commands = [command for command, _cwd in commands if Path(command[0]).name == "tlmgr"]
    assert tlmgr_commands == []
    kpse_commands = [command for command, _cwd in commands if Path(command[0]).name == "kpsewhich"]
    assert [command[-1] for command in kpse_commands] == [
        "fontspec.sty",
        "NotoSansCJK-Regular.ttc",
    ]
    xelatex_commands = [command for command, _cwd in commands if Path(command[0]).name == "xelatex"]
    assert len(xelatex_commands) == 3
    assert all("-no-shell-escape" in command for command in xelatex_commands)
    assert tex_sources
    assert r'\XeTeXlinebreaklocale "zh"' in tex_sources[0]
    assert r"\XeTeXlinebreakskip = 0pt plus 1pt" in tex_sources[0]
    assert 70 <= len(manager._PAPER_CJK_WRAP_PROBE) <= 100
    assert not any(character.isspace() for character in manager._PAPER_CJK_WRAP_PROBE)
    probe_lines = [
        line
        for line in tex_sources[0].splitlines()
        if manager._PAPER_CJK_WRAP_PROBE in line
    ]
    assert probe_lines == [manager._PAPER_CJK_WRAP_PROBE]


def test_paper_smoke_source_rejects_missing_canonical_cjk_line_breaking() -> None:
    source = manager._paper_probe_source()
    for line in manager._PAPER_CJK_LINEBREAK_LINES:
        broken = source.replace(f"{line}\n", "")
        with pytest.raises(ToolchainProbeError, match="missing CJK line breaking"):
            manager._validate_paper_probe_source(broken)


def test_paper_post_install_rejects_severe_cjk_layout_overflow(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    descriptor = replace(
        _descriptor(),
        post_install="paper-capability",
        package_closure=(),
        closure_source=None,
    )
    bin_dir = tmp_path / "Bundle/bin"
    for name in ("kpsewhich", "xelatex", "bibtex"):
        _make_executable(bin_dir / name)
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "NotoSansCJK-Regular.ttc").write_bytes(b"font")

    def fake_run_checked(
        command: list[str], *, cwd: Path, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if Path(command[0]).name == "xelatex":
            (cwd / "paper.pdf").write_bytes(b"%PDF-test")
        return subprocess.CompletedProcess(
            command,
            0,
            b"Overfull \\hbox (64.04889pt too wide) in paragraph at lines 10--20",
            b"",
        )

    monkeypatch.setattr(manager, "_run_checked", fake_run_checked)

    with pytest.raises(ToolchainProbeError, match=r"layout overflow: max=64\.05pt"):
        manager._run_post_install(descriptor, tmp_path, (bin_dir,))


def test_runtime_ignores_tampered_activation_receipt(tmp_path: Path) -> None:
    active = tmp_path / "active"
    active.mkdir(parents=True)
    (active / "paper-tex.json").write_text(
        json.dumps(
            {
                "component_id": "paper-tex",
                "package_relpath": "../outside",
                "bin_relpaths": ["bin"],
            }
        ),
        encoding="utf-8",
    )
    assert managed_env({"PATH": "/system/bin"}, root=tmp_path)["PATH"] == "/system/bin"
