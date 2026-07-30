#!/usr/bin/env python3
"""Build and compare desktop release-channel manifests.

GitHub Release assets remain the source of truth.  The OSS mirror publishes a
small, moving manifest only after those assets have passed ``SHA256SUMS``
verification.  Keeping the version and promotion rules here makes the release
workflow deterministic and gives clients one strict schema to consume.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
GITHUB_RELEASE_PAGE_ROOT = "https://github.com/opensquilla/opensquilla/releases/tag"
_TAG_RE = re.compile(
    r"^v(?P<major>0|[1-9]\d*)[.](?P<minor>0|[1-9]\d*)[.]"
    r"(?P<patch>0|[1-9]\d*)(?:rc(?P<rc>0|[1-9]\d*))?$"
)
_APP_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)[.](?P<minor>0|[1-9]\d*)[.]"
    r"(?P<patch>0|[1-9]\d*)(?:-rc(?P<rc>0|[1-9]\d*))?$"
)
_RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?(?:Z|[+-]\d{2}:\d{2})$")


class ManifestError(ValueError):
    """Raised when release metadata cannot satisfy the channel contract."""


@dataclass(frozen=True, order=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int
    # A final release sorts after every RC for the same base.
    final_rank: int
    rc: int

    @property
    def base(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def prerelease(self) -> bool:
        return self.final_rank == 0

    @property
    def app_version(self) -> str:
        if self.prerelease:
            return f"{self.base}-rc{self.rc}"
        return self.base


def _version_from_match(match: re.Match[str]) -> ReleaseVersion:
    rc_text = match.group("rc")
    return ReleaseVersion(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        0 if rc_text is not None else 1,
        int(rc_text or 0),
    )


def parse_release_tag(tag: str) -> ReleaseVersion:
    match = _TAG_RE.fullmatch(str(tag).strip())
    if match is None:
        raise ManifestError(f"unsupported OpenSquilla release tag: {tag!r}")
    return _version_from_match(match)


def parse_app_version(version: str) -> ReleaseVersion:
    match = _APP_VERSION_RE.fullmatch(str(version).strip())
    if match is None:
        raise ManifestError(f"unsupported Electron release version: {version!r}")
    return _version_from_match(match)


def _safe_asset_name(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
        raise ManifestError(f"{field} must be a single safe filename")
    return value


def _require_asset(asset_names: set[str], name: str) -> str:
    if name not in asset_names:
        raise ManifestError(f"missing release asset required by update channel: {name}")
    return name


def _published_at(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty RFC3339 timestamp")
    text = value.strip()
    if _RFC3339_RE.fullmatch(text) is None:
        raise ManifestError(f"{field} must be a valid RFC3339 timestamp")
    iso_text = text.removesuffix("Z") + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise ManifestError(f"{field} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{field} must include a timezone")
    return text


def _release_url(tag: str) -> str:
    return f"{GITHUB_RELEASE_PAGE_ROOT}/{tag}"


def _expected_platforms(version: ReleaseVersion) -> dict[str, dict[str, str]]:
    app_version = version.app_version
    return {
        "darwin-arm64": {
            "feed": "latest-mac.yml",
            "archive": f"OpenSquilla-{app_version}-mac-arm64.zip",
            "installer": f"OpenSquilla-{app_version}-mac-arm64.dmg",
        },
        "win32-x64": {
            "feed": "latest.yml",
            "installer": f"OpenSquilla-{app_version}-win-x64.exe",
        },
    }


def channel_targets(version: ReleaseVersion) -> tuple[str, ...]:
    targets = ["latest.json", f"preview/{version.base}.json"]
    if not version.prerelease:
        targets.insert(1, "stable.json")
    return tuple(targets)


def build_manifest(
    release: dict[str, Any],
    asset_names: Iterable[str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if release.get("isDraft") is not False:
        raise ManifestError("release isDraft must be false")

    tag = str(release.get("tagName") or "").strip()
    version = parse_release_tag(tag)
    declared_prerelease = release.get("isPrerelease")
    if not isinstance(declared_prerelease, bool):
        raise ManifestError("release isPrerelease must be a boolean")
    if declared_prerelease != version.prerelease:
        raise ManifestError(
            f"release prerelease flag does not match tag {tag}: {declared_prerelease}"
        )

    published_at = _published_at(release.get("publishedAt"), field="release publishedAt")
    release_url = release.get("url")
    expected_release_url = _release_url(tag)
    if release_url != expected_release_url:
        raise ManifestError(
            f"release url must be the canonical GitHub Release URL: {expected_release_url}"
        )

    safe_assets = [_safe_asset_name(name, field="release asset") for name in asset_names]
    assets = set(safe_assets)
    if len(assets) != len(safe_assets):
        raise ManifestError("release asset names must be unique")
    platforms = _expected_platforms(version)
    for entry in platforms.values():
        for name in entry.values():
            _require_asset(assets, name)
    _require_asset(assets, "SHA256SUMS")

    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "tag": tag,
        "version": version.app_version,
        "baseVersion": version.base,
        "prerelease": version.prerelease,
        "publishedAt": published_at,
        "releaseUrl": release_url,
        "sha256sums": "SHA256SUMS",
        "platforms": platforms,
    }
    validate_manifest(manifest)
    return manifest, channel_targets(version)


def validate_manifest(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ManifestError("channel manifest must be a JSON object")
    schema_version = payload.get("schemaVersion")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ManifestError("unsupported channel manifest schemaVersion")

    tag = payload.get("tag")
    version_text = payload.get("version")
    if not isinstance(tag, str) or not isinstance(version_text, str):
        raise ManifestError("channel manifest tag and version must be strings")
    tag_version = parse_release_tag(tag)
    app_version = parse_app_version(version_text)
    if tag_version != app_version:
        raise ManifestError("channel manifest tag and version disagree")
    if payload.get("baseVersion") != tag_version.base:
        raise ManifestError("channel manifest baseVersion disagrees with tag")
    if payload.get("prerelease") is not tag_version.prerelease:
        raise ManifestError("channel manifest prerelease disagrees with tag")

    _published_at(payload.get("publishedAt"), field="channel manifest publishedAt")
    release_url = payload.get("releaseUrl")
    if release_url != _release_url(tag):
        raise ManifestError("channel manifest releaseUrl is not canonical")
    if payload.get("sha256sums") != "SHA256SUMS":
        raise ManifestError("channel manifest sha256sums must be SHA256SUMS")

    platforms = payload.get("platforms")
    if not isinstance(platforms, dict):
        raise ManifestError("channel manifest platforms must be an object")
    for platform, entry in platforms.items():
        if not isinstance(entry, dict):
            raise ManifestError(f"channel manifest platform {platform} must be an object")
        for field, value in entry.items():
            _safe_asset_name(value, field=f"{platform}.{field}")
    expected_platforms = _expected_platforms(tag_version)
    if platforms != expected_platforms:
        raise ManifestError("channel manifest platform assets disagree with version")
    return payload


def manifest_version(payload: object) -> ReleaseVersion:
    validated = validate_manifest(payload)
    return parse_app_version(str(validated["version"]))


def _channel_base(channel: str) -> str | None:
    if channel in {"latest.json", "stable.json"}:
        return None
    match = re.fullmatch(r"preview/(\d+[.]\d+[.]\d+)[.]json", channel)
    if match is None:
        raise ManifestError(f"unsupported channel target: {channel}")
    base = match.group(1)
    parsed = parse_app_version(base)
    if parsed.prerelease or parsed.base != base:
        raise ManifestError(f"unsupported preview channel base: {channel}")
    return base


def _validate_channel_version(version: ReleaseVersion, *, channel: str) -> None:
    if channel == "stable.json":
        if version.prerelease:
            raise ManifestError("a prerelease cannot occupy or advance the stable channel")
        return
    if channel == "latest.json":
        return
    base = _channel_base(channel)
    if version.base != base:
        raise ManifestError(f"preview channel {channel} contains another release line")


def should_promote(
    current: object,
    candidate: object,
    *,
    channel: str,
) -> bool:
    current_version = manifest_version(current)
    candidate_version = manifest_version(candidate)

    _validate_channel_version(current_version, channel=channel)
    _validate_channel_version(candidate_version, channel=channel)

    return candidate_version >= current_version


def release_is_channel_head(
    releases: object,
    candidate: object,
    *,
    channel: str,
) -> bool:
    """Return whether candidate is the highest published release for channel.

    This check is independent of OSS state.  It prevents the first manual
    backfill after introducing manifests from treating a missing channel object
    as permission to replace newer aliases that predate the manifest contract.
    """

    candidate_manifest = validate_manifest(candidate)
    candidate_version = parse_app_version(str(candidate_manifest["version"]))
    candidate_tag = str(candidate_manifest["tag"])
    _validate_channel_version(candidate_version, channel=channel)
    if not isinstance(releases, list):
        raise ManifestError("GitHub release inventory must be a JSON array")

    published: list[ReleaseVersion] = []
    candidate_seen = False
    for index, item in enumerate(releases):
        if not isinstance(item, dict):
            raise ManifestError(f"GitHub release inventory item {index} must be an object")
        tag = item.get("tagName")
        if not isinstance(tag, str):
            raise ManifestError(f"GitHub release inventory item {index} tagName is invalid")
        try:
            version = parse_release_tag(tag)
        except ManifestError:
            # Other project/documentation tags do not participate in desktop
            # release channels.
            continue

        is_draft = item.get("isDraft")
        is_prerelease = item.get("isPrerelease")
        if not isinstance(is_draft, bool) or not isinstance(is_prerelease, bool):
            raise ManifestError(f"GitHub release inventory metadata is invalid for {tag}")
        if is_draft:
            continue
        if is_prerelease != version.prerelease:
            raise ManifestError(f"GitHub release prerelease flag disagrees with tag {tag}")
        _published_at(item.get("publishedAt"), field=f"GitHub release {tag} publishedAt")
        if tag == candidate_tag:
            candidate_seen = True
        try:
            _validate_channel_version(version, channel=channel)
        except ManifestError:
            continue
        published.append(version)

    if not candidate_seen:
        raise ManifestError(
            f"candidate {candidate_tag} is absent from the published GitHub release inventory"
        )
    if not published:
        raise ManifestError(f"GitHub release inventory has no releases for {channel}")
    return candidate_version == max(published)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"unable to read JSON from {path}: {exc}") from exc


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _cmd_build(args: argparse.Namespace) -> int:
    release = _load_json(args.release_json)
    if not isinstance(release, dict):
        raise ManifestError("release metadata must be a JSON object")
    assets = [
        path.name
        for path in args.assets_dir.iterdir()
        if path.is_file() and path.name != "CHECKSUMMED_ASSETS"
    ]
    manifest, targets = build_manifest(release, assets)
    _write_json(args.output, manifest)
    args.targets_output.parent.mkdir(parents=True, exist_ok=True)
    args.targets_output.write_text("\n".join(targets) + "\n", encoding="utf-8")
    return 0


def _cmd_should_promote(args: argparse.Namespace) -> int:
    current = _load_json(args.current)
    candidate = _load_json(args.candidate)
    if should_promote(current, candidate, channel=args.channel):
        print("promote")
        return 0
    print("skip")
    return 3


def _cmd_is_release_head(args: argparse.Namespace) -> int:
    releases = _load_json(args.releases)
    candidate = _load_json(args.candidate)
    if release_is_channel_head(releases, candidate, channel=args.channel):
        print("head")
        return 0
    print("not-head")
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build a channel manifest")
    build.add_argument("--release-json", type=Path, required=True)
    build.add_argument("--assets-dir", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--targets-output", type=Path, required=True)
    build.set_defaults(handler=_cmd_build)

    compare = subparsers.add_parser(
        "should-promote", help="exit 0 when a channel may move to the candidate"
    )
    compare.add_argument("--channel", required=True)
    compare.add_argument("--current", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.set_defaults(handler=_cmd_should_promote)

    head = subparsers.add_parser(
        "is-release-head",
        help="exit 0 when candidate is the highest published release for a channel",
    )
    head.add_argument("--channel", required=True)
    head.add_argument("--releases", type=Path, required=True)
    head.add_argument("--candidate", type=Path, required=True)
    head.set_defaults(handler=_cmd_is_release_head)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except ManifestError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
