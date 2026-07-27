from __future__ import annotations

import copy

import pytest

from scripts.release_channel_manifest import (
    ManifestError,
    build_manifest,
    release_is_channel_head,
    should_promote,
    validate_manifest,
)


def _release(tag: str, *, prerelease: bool) -> dict[str, object]:
    return {
        "tagName": tag,
        "isDraft": False,
        "isPrerelease": prerelease,
        "publishedAt": "2026-07-15T00:00:00Z",
        "url": f"https://github.com/opensquilla/opensquilla/releases/tag/{tag}",
    }


def _assets(version: str) -> set[str]:
    return {
        f"OpenSquilla-{version}-mac-arm64.dmg",
        f"OpenSquilla-{version}-mac-arm64.zip",
        f"OpenSquilla-{version}-win-x64.exe",
        "latest-mac.yml",
        "latest.yml",
        "SHA256SUMS",
    }


def _manifest(tag: str, version: str, *, prerelease: bool) -> dict[str, object]:
    manifest, _ = build_manifest(_release(tag, prerelease=prerelease), _assets(version))
    return manifest


def _inventory(*releases: dict[str, object]) -> list[dict[str, object]]:
    return list(releases)


def test_builds_prerelease_manifest_and_scoped_targets() -> None:
    manifest, targets = build_manifest(_release("v0.5.0rc4", prerelease=True), _assets("0.5.0-rc4"))

    assert manifest["schemaVersion"] == 1
    assert manifest["version"] == "0.5.0-rc4"
    assert manifest["baseVersion"] == "0.5.0"
    assert manifest["prerelease"] is True
    assert manifest["platforms"] == {
        "darwin-arm64": {
            "feed": "latest-mac.yml",
            "archive": "OpenSquilla-0.5.0-rc4-mac-arm64.zip",
            "installer": "OpenSquilla-0.5.0-rc4-mac-arm64.dmg",
        },
        "win32-x64": {
            "feed": "latest.yml",
            "installer": "OpenSquilla-0.5.0-rc4-win-x64.exe",
        },
    }
    assert targets == ("latest.json", "preview/0.5.0.json")


def test_final_release_advances_stable_and_same_base_preview() -> None:
    _, targets = build_manifest(_release("v0.5.0", prerelease=False), _assets("0.5.0"))

    assert targets == ("latest.json", "stable.json", "preview/0.5.0.json")


def test_manifest_requires_release_flag_and_assets_to_match_tag() -> None:
    with pytest.raises(ManifestError, match="prerelease flag"):
        build_manifest(_release("v0.5.0rc4", prerelease=False), _assets("0.5.0-rc4"))

    with pytest.raises(ManifestError, match="missing release asset"):
        build_manifest(
            _release("v0.5.0rc4", prerelease=True),
            _assets("0.5.0-rc4") - {"latest-mac.yml"},
        )

    missing_draft = _release("v0.5.0rc4", prerelease=True)
    del missing_draft["isDraft"]
    with pytest.raises(ManifestError, match="isDraft must be false"):
        build_manifest(missing_draft, _assets("0.5.0-rc4"))


@pytest.mark.parametrize("tag", ["V0.5.0rc4", "v0.5.0RC4", "v0.5.0-rc4"])
def test_manifest_rejects_noncanonical_release_tag_spelling(tag: str) -> None:
    with pytest.raises(ManifestError, match="unsupported OpenSquilla release tag"):
        build_manifest(_release(tag, prerelease=True), _assets("0.5.0-rc4"))


def test_manifest_requires_canonical_release_metadata() -> None:
    wrong_url = _release("v0.5.0rc4", prerelease=True)
    wrong_url["url"] = "https://github.com/example/project/releases/tag/v0.5.0rc4"
    with pytest.raises(ManifestError, match="canonical GitHub Release URL"):
        build_manifest(wrong_url, _assets("0.5.0-rc4"))

    bad_date = _release("v0.5.0rc4", prerelease=True)
    bad_date["publishedAt"] = "not-a-date"
    with pytest.raises(ManifestError, match="valid RFC3339"):
        build_manifest(bad_date, _assets("0.5.0-rc4"))


def test_manifest_rejects_unsafe_asset_paths() -> None:
    manifest = _manifest("v0.5.0rc4", "0.5.0-rc4", prerelease=True)
    broken = copy.deepcopy(manifest)
    broken["platforms"]["win32-x64"]["installer"] = "../OpenSquilla.exe"  # type: ignore[index]

    with pytest.raises(ManifestError, match="safe filename"):
        validate_manifest(broken)


def test_manifest_rejects_safe_but_wrong_versioned_asset_names() -> None:
    manifest = _manifest("v0.5.0rc4", "0.5.0-rc4", prerelease=True)
    broken = copy.deepcopy(manifest)
    broken["platforms"]["win32-x64"]["installer"] = "OpenSquilla.exe"  # type: ignore[index]

    with pytest.raises(ManifestError, match="platform assets disagree"):
        validate_manifest(broken)

    broken = copy.deepcopy(manifest)
    broken["sha256sums"] = "checksums.txt"
    with pytest.raises(ManifestError, match="must be SHA256SUMS"):
        validate_manifest(broken)

    broken = copy.deepcopy(manifest)
    broken["version"] = "0.5.0-RC4"
    with pytest.raises(ManifestError, match="unsupported Electron release version"):
        validate_manifest(broken)

    broken = copy.deepcopy(manifest)
    broken["schemaVersion"] = True
    with pytest.raises(ManifestError, match="unsupported channel manifest schemaVersion"):
        validate_manifest(broken)


def test_promotion_is_numeric_monotonic_and_final_outranks_rc() -> None:
    rc9 = _manifest("v0.5.0rc9", "0.5.0-rc9", prerelease=True)
    rc10 = _manifest("v0.5.0rc10", "0.5.0-rc10", prerelease=True)
    final = _manifest("v0.5.0", "0.5.0", prerelease=False)

    assert should_promote(rc9, rc10, channel="preview/0.5.0.json") is True
    assert should_promote(rc10, rc9, channel="preview/0.5.0.json") is False
    assert should_promote(rc10, final, channel="preview/0.5.0.json") is True
    assert should_promote(final, rc10, channel="latest.json") is False


def test_latest_compares_release_lines_but_preview_refuses_cross_base() -> None:
    stable_050 = _manifest("v0.5.0", "0.5.0", prerelease=False)
    rc_060 = _manifest("v0.6.0rc1", "0.6.0-rc1", prerelease=True)

    assert should_promote(stable_050, rc_060, channel="latest.json") is True
    with pytest.raises(ManifestError, match="another release line"):
        should_promote(stable_050, rc_060, channel="preview/0.5.0.json")


def test_stable_channel_rejects_prerelease_candidate() -> None:
    stable = _manifest("v0.4.1", "0.4.1", prerelease=False)
    preview = _manifest("v0.5.0rc4", "0.5.0-rc4", prerelease=True)

    with pytest.raises(ManifestError, match="cannot occupy or advance the stable"):
        should_promote(stable, preview, channel="stable.json")

    with pytest.raises(ManifestError, match="cannot occupy or advance the stable"):
        should_promote(preview, stable, channel="stable.json")


def test_published_release_head_blocks_first_bootstrap_downgrade() -> None:
    rc3 = _manifest("v0.5.0rc3", "0.5.0-rc3", prerelease=True)
    releases = _inventory(
        _release("v0.5.0rc3", prerelease=True),
        _release("v0.5.0rc4", prerelease=True),
        {
            **_release("v0.5.0rc99", prerelease=True),
            "isDraft": True,
            "publishedAt": None,
        },
        {
            "tagName": "documentation-2026-07-15",
            "isDraft": False,
            "isPrerelease": False,
            "publishedAt": "2026-07-15T01:00:00Z",
        },
    )

    assert release_is_channel_head(releases, rc3, channel="preview/0.5.0.json") is False
    assert release_is_channel_head(releases, rc3, channel="latest.json") is False


def test_published_release_head_is_scoped_by_channel() -> None:
    final_050 = _manifest("v0.5.0", "0.5.0", prerelease=False)
    releases = _inventory(
        _release("v0.5.0rc4", prerelease=True),
        _release("v0.5.0", prerelease=False),
        _release("v0.6.0rc1", prerelease=True),
    )

    assert release_is_channel_head(releases, final_050, channel="stable.json") is True
    assert release_is_channel_head(releases, final_050, channel="preview/0.5.0.json") is True
    assert release_is_channel_head(releases, final_050, channel="latest.json") is False


def test_published_release_head_requires_candidate_in_inventory() -> None:
    rc4 = _manifest("v0.5.0rc4", "0.5.0-rc4", prerelease=True)

    with pytest.raises(ManifestError, match="absent from the published"):
        release_is_channel_head(
            _inventory(_release("v0.5.0rc3", prerelease=True)),
            rc4,
            channel="preview/0.5.0.json",
        )


def test_published_release_head_rejects_mislabeled_strict_release() -> None:
    rc4 = _manifest("v0.5.0rc4", "0.5.0-rc4", prerelease=True)

    with pytest.raises(ManifestError, match="prerelease flag disagrees"):
        release_is_channel_head(
            _inventory(_release("v0.5.0rc4", prerelease=False)),
            rc4,
            channel="preview/0.5.0.json",
        )
