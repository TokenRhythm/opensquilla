"""Compatibility and stability contracts for Skill tree fingerprints."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from opensquilla.skills import file_hash
from opensquilla.skills.file_hash import (
    _HASH_CHUNK_SIZE,
    _TreeChangedDuringHashError,
)
from opensquilla.skills.hub.lockfile import compute_sha256
from opensquilla.skills.tree import compute_tree_sha256

DigestFunction = Callable[[Path], str]


def _stat_view(info: os.stat_result, **changes: int) -> os.stat_result:
    values = {
        "st_mode": info.st_mode,
        "st_dev": info.st_dev,
        "st_ino": info.st_ino,
        "st_size": info.st_size,
        "st_mtime_ns": info.st_mtime_ns,
        "st_ctime_ns": info.st_ctime_ns,
    }
    values.update(changes)
    return cast("os.stat_result", SimpleNamespace(**values))


def _whole_buffer_tree_sha256(directory: Path) -> str:
    """Reference the pre-streaming v2 framing without sharing production helpers."""

    hasher = hashlib.sha256()
    entries = sorted(
        directory.rglob("*"),
        key=lambda item: item.relative_to(directory).as_posix(),
    )
    for path in entries:
        relative = path.relative_to(directory).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        hasher.update(relative.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        if stat.S_ISREG(mode):
            content = path.read_bytes()
            hasher.update(b"file\0")
            hasher.update(len(content).to_bytes(8, "big"))
            hasher.update(content)
        elif stat.S_ISLNK(mode):
            target = os.readlink(path).encode("utf-8", errors="surrogateescape")
            hasher.update(b"symlink\0")
            hasher.update(len(target).to_bytes(8, "big"))
            hasher.update(target)
        else:
            hasher.update(f"special:{stat.S_IFMT(mode):o}".encode("ascii"))
            hasher.update(b"\0")
    return hasher.hexdigest()


def _whole_buffer_legacy_sha256(directory: Path) -> str:
    """Reference the legacy native-path and hidden-file behavior exactly."""

    hasher = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            hasher.update(str(relative).encode())
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _payload(size: int) -> bytes:
    seed = bytes(range(256))
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"filesystem symlinks are unavailable: {exc}")


def test_tree_and_legacy_digest_goldens_are_unchanged(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / ".hidden").write_bytes(b"secret")
    (skill_dir / "a.txt").write_bytes(b"alpha")

    assert compute_tree_sha256(skill_dir) == (
        "9f4defe7510decc3c6054afd608a87bcbc4f71b480ee9714edcfa2838f8dfb62"
    )
    assert compute_sha256(skill_dir) == (
        "08535ec7e85547dbeb33f7f701bc147c04cd5e5515cedb1bb68d71e7686a9f52"
    )


@pytest.mark.parametrize(
    "size",
    [
        0,
        1,
        _HASH_CHUNK_SIZE - 1,
        _HASH_CHUNK_SIZE,
        _HASH_CHUNK_SIZE + 1,
        (2 * _HASH_CHUNK_SIZE) + 17,
    ],
)
def test_streamed_digests_match_whole_buffer_references_at_chunk_boundaries(
    tmp_path: Path,
    size: int,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "payload.bin").write_bytes(_payload(size))

    assert compute_tree_sha256(skill_dir) == _whole_buffer_tree_sha256(skill_dir)
    assert compute_sha256(skill_dir) == _whole_buffer_legacy_sha256(skill_dir)


def test_streamed_digests_match_reference_for_nested_and_binary_content(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "skill"
    assets = skill_dir / "nested" / "assets"
    assets.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(b"---\r\nname: demo\r\n---\r\n")
    (skill_dir / ".catalog").write_bytes(b"hidden\0metadata")
    (assets / "binary.bin").write_bytes(b"\0\xff\x80binary\r\n")
    (assets / "资源.txt").write_text("你好\n", encoding="utf-8")

    assert compute_tree_sha256(skill_dir) == _whole_buffer_tree_sha256(skill_dir)
    assert compute_sha256(skill_dir) == _whole_buffer_legacy_sha256(skill_dir)


def test_hidden_files_and_permission_bits_keep_historical_semantics(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    visible = skill_dir / "visible.txt"
    visible.write_bytes(b"visible")

    tree_without_hidden = compute_tree_sha256(skill_dir)
    legacy_without_hidden = compute_sha256(skill_dir)
    (skill_dir / ".hidden").write_bytes(b"hidden")

    assert compute_tree_sha256(skill_dir) != tree_without_hidden
    assert compute_sha256(skill_dir) == legacy_without_hidden

    tree_before_chmod = compute_tree_sha256(skill_dir)
    legacy_before_chmod = compute_sha256(skill_dir)
    original_mode = stat.S_IMODE(visible.stat().st_mode)
    toggled_mode = original_mode ^ (stat.S_IWRITE if os.name == "nt" else stat.S_IXUSR)
    visible.chmod(toggled_mode)
    try:
        assert compute_tree_sha256(skill_dir) == tree_before_chmod
        assert compute_sha256(skill_dir) == legacy_before_chmod
    finally:
        visible.chmod(original_mode)


def test_tree_does_not_follow_file_symlinks_but_legacy_digest_does(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"first external value")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    link = skill_dir / "linked.txt"
    _symlink_or_skip(link, outside)

    tree_before = compute_tree_sha256(skill_dir)
    legacy_before = compute_sha256(skill_dir)
    assert tree_before == _whole_buffer_tree_sha256(skill_dir)
    assert legacy_before == _whole_buffer_legacy_sha256(skill_dir)

    outside.write_bytes(b"second external value")

    assert compute_tree_sha256(skill_dir) == tree_before
    assert compute_sha256(skill_dir) != legacy_before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files are POSIX-only")
def test_special_files_keep_v2_type_framing(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    fifo = skill_dir / "events.fifo"
    os.mkfifo(fifo)

    assert compute_tree_sha256(skill_dir) == _whole_buffer_tree_sha256(skill_dir)
    assert compute_sha256(skill_dir) == _whole_buffer_legacy_sha256(skill_dir)


@pytest.mark.parametrize(
    "digest_function",
    [pytest.param(compute_tree_sha256, id="v2"), pytest.param(compute_sha256, id="legacy")],
)
def test_file_reads_are_bounded_and_span_multiple_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    digest_function: DigestFunction,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "large.bin").write_bytes(_payload((2 * _HASH_CHUNK_SIZE) + 17))
    requested_sizes: list[int] = []
    original_read_chunk = file_hash._read_chunk

    def tracked_read_chunk(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read_chunk(descriptor, size)

    monkeypatch.setattr(file_hash, "_read_chunk", tracked_read_chunk)

    digest_function(skill_dir)

    assert len(requested_sizes) >= 4
    assert all(0 < size <= _HASH_CHUNK_SIZE for size in requested_sizes)
    assert requested_sizes.count(_HASH_CHUNK_SIZE) >= 2


def test_windows_path_and_descriptor_ctime_difference_hashes_stable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "payload.bin").write_bytes(b"stable payload")
    original_fstat = file_hash.os.fstat

    def windows_fstat(descriptor: int) -> os.stat_result:
        info = original_fstat(descriptor)
        return _stat_view(info, st_ctime_ns=info.st_ctime_ns + 1)

    monkeypatch.setattr(file_hash, "_IS_WINDOWS", True)
    monkeypatch.setattr(file_hash.os, "fstat", windows_fstat)

    assert compute_tree_sha256(skill_dir) == _whole_buffer_tree_sha256(skill_dir)


@pytest.mark.parametrize("changed_field", ["type", "device", "inode", "size", "mtime"])
def test_windows_path_and_descriptor_reliable_field_difference_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "payload.bin").write_bytes(b"stable payload")
    original_fstat = file_hash.os.fstat

    def inconsistent_fstat(descriptor: int) -> os.stat_result:
        info = original_fstat(descriptor)
        changes = {
            "type": {"st_mode": stat.S_IFDIR | stat.S_IMODE(info.st_mode)},
            "device": {"st_dev": info.st_dev + 1},
            "inode": {"st_ino": info.st_ino + 1},
            "size": {"st_size": info.st_size + 1},
            "mtime": {"st_mtime_ns": info.st_mtime_ns + 1},
        }[changed_field]
        return _stat_view(info, **changes)

    monkeypatch.setattr(file_hash, "_IS_WINDOWS", True)
    monkeypatch.setattr(file_hash.os, "fstat", inconsistent_fstat)

    with pytest.raises(_TreeChangedDuringHashError):
        compute_tree_sha256(skill_dir)


@pytest.mark.parametrize("failure_point", ["open", "read"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
@pytest.mark.parametrize(
    "digest_function",
    [pytest.param(compute_tree_sha256, id="v2"), pytest.param(compute_sha256, id="legacy")],
)
def test_stable_io_failure_preserves_original_os_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    error_number: int,
    digest_function: DigestFunction,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    payload = skill_dir / "payload.bin"
    payload.write_bytes(b"stable payload")

    if failure_point == "open":

        def denied_open(_path: Path, _flags: int) -> int:
            raise OSError(error_number, "stable open failure")

        monkeypatch.setattr(file_hash.os, "open", denied_open)
    else:

        def denied_read(_descriptor: int, _size: int) -> bytes:
            raise OSError(error_number, "stable read failure")

        monkeypatch.setattr(file_hash, "_read_chunk", denied_read)

    with pytest.raises(OSError) as caught:
        digest_function(skill_dir)

    assert not isinstance(caught.value, _TreeChangedDuringHashError)
    assert caught.value.errno == error_number
    if error_number == errno.EACCES:
        assert isinstance(caught.value, PermissionError)


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ELOOP])
def test_open_path_race_errno_fails_closed_even_after_aba_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "payload.bin").write_bytes(b"stable again before the identity probe")

    def raced_open(_path: Path, _flags: int) -> int:
        raise OSError(error_number, "transient pathname race")

    monkeypatch.setattr(file_hash.os, "open", raced_open)

    with pytest.raises(_TreeChangedDuringHashError):
        compute_tree_sha256(skill_dir)


def test_readlink_einval_fails_closed_even_after_aba_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    target = tmp_path / "target.txt"
    target.write_bytes(b"target")
    link = skill_dir / "linked.txt"
    _symlink_or_skip(link, target)

    def raced_readlink(_path: Path) -> str:
        raise OSError(errno.EINVAL, "transient non-link state")

    monkeypatch.setattr(file_hash.os, "readlink", raced_readlink)

    with pytest.raises(_TreeChangedDuringHashError):
        compute_tree_sha256(skill_dir)


def _grow_file(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b"grew while hashing")


def _truncate_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.truncate(_HASH_CHUNK_SIZE // 2)


def _replace_file(path: Path) -> None:
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(b"r" * path.stat().st_size)
    os.replace(replacement, path)


@pytest.mark.parametrize(
    "digest_function",
    [pytest.param(compute_tree_sha256, id="v2"), pytest.param(compute_sha256, id="legacy")],
)
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_grow_file, id="growth"),
        pytest.param(_truncate_file, id="truncation"),
        pytest.param(_replace_file, id="replacement"),
    ],
)
def test_file_change_after_first_chunk_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    digest_function: DigestFunction,
    mutate: Callable[[Path], None],
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    payload = skill_dir / "payload.bin"
    payload.write_bytes(_payload((2 * _HASH_CHUNK_SIZE) + 17))
    original_read_chunk = file_hash._read_chunk
    original_path_matches = file_hash._path_matches_snapshot
    mutated = False
    replacement_simulated = False

    def mutate_after_first_chunk(descriptor: int, size: int) -> bytes:
        nonlocal mutated, replacement_simulated
        content = original_read_chunk(descriptor, size)
        if content and not mutated:
            try:
                mutate(payload)
            except PermissionError:
                if mutate is not _replace_file or os.name != "nt":
                    raise
                # Windows may deny replacing an open CRT descriptor. Simulate
                # the same final pathname-identity mismatch through its seam.
                replacement_simulated = True
                mutated = True
            else:
                mutated = True
        return content

    def path_matches_snapshot(
        path: Path,
        snapshot: os.stat_result,
        *,
        follow_symlinks: bool,
    ) -> bool:
        if replacement_simulated and path == payload:
            return False
        return original_path_matches(
            path,
            snapshot,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(file_hash, "_read_chunk", mutate_after_first_chunk)
    monkeypatch.setattr(file_hash, "_path_matches_snapshot", path_matches_snapshot)

    with pytest.raises(_TreeChangedDuringHashError):
        digest_function(skill_dir)
    assert mutated is True


def test_symlink_replacement_during_readlink_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    first_target = tmp_path / "first.txt"
    second_target = tmp_path / "second.txt"
    first_target.write_bytes(b"first")
    second_target.write_bytes(b"second")
    link = skill_dir / "linked.txt"
    _symlink_or_skip(link, first_target)
    original_readlink = file_hash.os.readlink
    mutated = False

    def replace_after_readlink(path: Path) -> str:
        nonlocal mutated
        target = original_readlink(path)
        if not mutated:
            link.unlink()
            link.symlink_to(second_target)
            mutated = True
        return target

    monkeypatch.setattr(file_hash.os, "readlink", replace_after_readlink)

    with pytest.raises(_TreeChangedDuringHashError):
        compute_tree_sha256(skill_dir)
    assert mutated is True


def test_legacy_symlink_retargeted_to_same_inode_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    target = skill_dir / "z-target.bin"
    hardlink = skill_dir / "y-hardlink.bin"
    link = skill_dir / "a-link.bin"
    target.write_bytes(_payload((2 * _HASH_CHUNK_SIZE) + 17))
    try:
        os.link(target, hardlink)
    except OSError as exc:
        pytest.skip(f"filesystem hard links are unavailable: {exc}")
    _symlink_or_skip(link, target)
    original_read_chunk = file_hash._read_chunk
    mutated = False

    def retarget_after_first_chunk(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        content = original_read_chunk(descriptor, size)
        if content and not mutated:
            link.unlink()
            link.symlink_to(hardlink)
            mutated = True
        return content

    monkeypatch.setattr(file_hash, "_read_chunk", retarget_after_first_chunk)

    with pytest.raises(_TreeChangedDuringHashError):
        compute_sha256(skill_dir)
    assert mutated is True
