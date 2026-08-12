from __future__ import annotations

import io
import stat
import zipfile

import pytest

from opensquilla.skills.hub.archive import (
    ArchiveLimits,
    ArchiveNormalizationError,
    normalize_skill_archive,
    normalize_skill_archive_result,
)


def _zip(entries: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_flat_archive_preserves_nested_resource_paths() -> None:
    files = normalize_skill_archive(
        _zip({"SKILL.md": b"---\nname: demo\n---\n", "scripts/run.py": b"print(1)\n"})
    )

    assert files == {
        "SKILL.md": "---\nname: demo\n---\n",
        "scripts/run.py": "print(1)\n",
    }


def test_single_packaging_wrapper_is_removed() -> None:
    files = normalize_skill_archive(
        _zip({"demo-1.0/SKILL.md": b"---\nname: demo\n---\n", "demo-1.0/a.bin": b"\xff"})
    )

    assert set(files) == {"SKILL.md", "a.bin"}
    assert files["a.bin"] == b"\xff"


@pytest.mark.parametrize("manifest_name", ["skill.md", "skills.md"])
def test_legacy_manifest_spelling_reaches_candidate_normalization(
    manifest_name: str,
) -> None:
    files = normalize_skill_archive(
        _zip({manifest_name: b"---\nname: demo\n---\n", "reference.md": b"reference\n"})
    )

    assert set(files) == {manifest_name, "reference.md"}


def test_explicit_subpath_accepts_one_repository_wrapper() -> None:
    files = normalize_skill_archive(
        _zip(
            {
                "repo/skills/demo/SKILL.md": b"---\nname: demo\n---\n",
                "repo/skills/demo/reference.md": b"reference\n",
            }
        ),
        selected_subpath="skills/demo",
    )

    assert set(files) == {"SKILL.md", "reference.md"}


@pytest.mark.parametrize(
    "entries",
    [
        {"SKILL.md": b"---\n---\n", "../escape": b"bad"},
        {"wrapper/SKILL.md": b"---\n---\n", "outside.txt": b"bad"},
        {"SKILL.md": b"---\n---\n", "nested/SKILL.md": b"---\n---\n"},
        {"SKILL.md": b"---\n---\n", "nested/skills.md": b"---\n---\n"},
        {"SKILL.md": b"---\n---\n", ".git/config": b"bad"},
        {"SKILL.md": b"---\n---\n", ".opensquilla-staging/payload": b"bad"},
        {"SKILL.md": b"---\n---\n", "assets/NUL.txt": b"bad"},
    ],
)
def test_unsafe_or_ambiguous_archive_rejects_the_whole_bundle(
    entries: dict[str, bytes],
) -> None:
    with pytest.raises(ArchiveNormalizationError):
        normalize_skill_archive(_zip(entries))


def test_casefold_and_unicode_nfc_collisions_are_rejected() -> None:
    archive = _zip(
        {
            "SKILL.md": b"---\n---\n",
            "refs/\N{LATIN SMALL LETTER E WITH ACUTE}.txt": b"one",
            "refs/e\N{COMBINING ACUTE ACCENT}.txt": b"two",
        }
    )

    with pytest.raises(ArchiveNormalizationError, match="collide"):
        normalize_skill_archive(archive)


def test_implicit_directory_casefold_collision_is_rejected() -> None:
    archive = _zip(
        {
            "SKILL.md": b"---\n---\n",
            "Refs/a.txt": b"one",
            "refs/b.txt": b"two",
        }
    )

    with pytest.raises(ArchiveNormalizationError, match="directory paths collide"):
        normalize_skill_archive(archive)


@pytest.mark.parametrize("path", ['refs/bad?.txt', 'refs/bad|name.txt', 'refs/bad\x1f.txt'])
def test_windows_invalid_path_characters_are_rejected(path: str) -> None:
    with pytest.raises(ArchiveNormalizationError, match="portable to Windows"):
        normalize_skill_archive(_zip({"SKILL.md": b"---\n---\n", path: b"bad"}))


def test_file_and_descendant_path_collision_is_rejected() -> None:
    archive = _zip(
        {
            "SKILL.md": b"---\n---\n",
            "References": b"not a directory",
            "references/guide.md": b"unreachable",
        }
    )

    with pytest.raises(ArchiveNormalizationError, match="file/directory paths collide"):
        normalize_skill_archive(archive)


def test_depth_and_compression_ratio_limits_are_enforced() -> None:
    deep_path = "/".join(["d"] * 33) + "/payload.txt"
    with pytest.raises(ArchiveNormalizationError, match="depth"):
        normalize_skill_archive(_zip({"SKILL.md": b"---\n---\n", deep_path: b"x"}))

    compressed = _zip(
        {"SKILL.md": b"---\n---\n", "payload.txt": b"x" * 100_000},
        compression=zipfile.ZIP_DEFLATED,
    )
    with pytest.raises(ArchiveNormalizationError, match="ratio"):
        normalize_skill_archive(compressed)


def test_declared_and_expanded_byte_limits_are_enforced() -> None:
    limits = ArchiveLimits(
        max_archive_bytes=1_000,
        max_entries=10,
        max_entry_bytes=16,
        max_expanded_bytes=20,
    )
    with pytest.raises(ArchiveNormalizationError, match="size limit"):
        normalize_skill_archive(
            _zip({"SKILL.md": b"---\n---\n", "payload": b"x" * 17}),
            limits=limits,
        )


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK])
def test_links_and_special_files_are_rejected(file_type: int) -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", b"---\n---\n")
        info = zipfile.ZipInfo("special")
        info.create_system = 3
        info.external_attr = (file_type | 0o755) << 16
        archive.writestr(info, b"target")

    with pytest.raises(ArchiveNormalizationError, match="unsupported"):
        normalize_skill_archive(output.getvalue())


def test_asi_unix_link_metadata_is_rejected() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", b"---\n---\n")
        info = zipfile.ZipInfo("hardlink")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        info.extra = (0x756E).to_bytes(2, "little") + (0).to_bytes(2, "little")
        archive.writestr(info, b"target")

    with pytest.raises(ArchiveNormalizationError, match="link metadata"):
        normalize_skill_archive(output.getvalue())


def test_posix_permission_bits_are_retained_as_bundle_metadata() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", b"---\n---\n")
        info = zipfile.ZipInfo("scripts/run.sh")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        archive.writestr(info, b"#!/bin/sh\n")

    normalized = normalize_skill_archive_result(output.getvalue())

    assert normalized.file_modes["scripts/run.sh"] == 0o755


def test_default_archive_and_expanded_limits_are_fifty_mib() -> None:
    limits = ArchiveLimits()

    assert limits.max_archive_bytes == 50 * 1024 * 1024
    assert limits.max_expanded_bytes == 50 * 1024 * 1024
