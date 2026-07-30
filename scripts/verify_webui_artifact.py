#!/usr/bin/env python3
"""Validate the generated WebUI directory and its packaged wheel copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "webui-artifact-manifest.json"
WHEEL_PREFIX = "opensquilla/gateway/static/dist/"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEBUI_ROOT = REPOSITORY_ROOT / "opensquilla-webui"
DEFAULT_DIST_DIR = REPOSITORY_ROOT / "src/opensquilla/gateway/static/dist"
SOURCE_INPUT_ROOTS = (
    ".node-version",
    ".env",
    ".env.local",
    ".env.production",
    ".env.production.local",
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "public",
    "scripts",
    "src",
)
NORMALIZED_TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".svg",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".webmanifest",
    ".yaml",
    ".yml",
}
# Keep this platform-independent and mirrored in verify-dist.mjs. Canonical
# build normalization removes this OS metadata before manifest generation, and
# source distributions omit it even inside an otherwise canonical source root.
IGNORED_SOURCE_FILE_NAMES = frozenset({".DS_Store"})
FORBIDDEN_ARTIFACT_FILE_NAMES = frozenset({".ds_store", ".npmrc"})
FORBIDDEN_ARTIFACT_SUFFIXES = frozenset({".key", ".pem"})
OFFICIAL_MUSIC_FILES = {"music/README.md", "music/playlist.json"}


class ArtifactError(RuntimeError):
    """The WebUI artifact is missing, incomplete, or internally inconsistent."""


def _verify_official_music(files: dict[str, bytes]) -> None:
    """Reject personal music while allowing the tracked, empty library metadata."""

    personal_bgm = sorted(
        relative
        for relative in files
        if relative.startswith("music/") and relative not in OFFICIAL_MUSIC_FILES
    )
    if personal_bgm:
        raise ArtifactError(
            f"personal BGM content is forbidden in official WebUI artifacts: {personal_bgm}"
        )

    playlist_bytes = files.get("music/playlist.json")
    if playlist_bytes is None:
        return
    try:
        playlist = json.loads(playlist_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"official music/playlist.json is invalid: {exc}") from exc
    if not isinstance(playlist, dict) or playlist.get("tracks") != []:
        raise ArtifactError(
            "official music/playlist.json must keep its tracks list empty; "
            "use playlist.local.json only for private builds"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(dist_dir: Path) -> dict[str, bytes]:
    if not dist_dir.is_dir():
        raise ArtifactError(f"WebUI artifact directory is missing: {dist_dir}")
    files: dict[str, bytes] = {}
    for path in sorted(dist_dir.rglob("*")):
        if path.is_symlink():
            raise ArtifactError(f"WebUI artifact must not contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(dist_dir).as_posix()
        files[relative] = path.read_bytes()
    return files


def _forbidden_artifact_paths(files: dict[str, bytes]) -> list[str]:
    forbidden: list[str] = []
    for relative in files:
        name = PurePosixPath(relative).name
        lowered = name.lower()
        if (
            lowered in FORBIDDEN_ARTIFACT_FILE_NAMES
            or lowered == ".env"
            or lowered.startswith(".env.")
            or PurePosixPath(lowered).suffix in FORBIDDEN_ARTIFACT_SUFFIXES
        ):
            forbidden.append(relative)
    return sorted(forbidden, key=lambda path: path.encode("utf-8"))


def _source_files(webui_root: Path) -> list[Path]:
    if not webui_root.is_dir():
        raise ArtifactError(f"WebUI source directory is missing: {webui_root}")

    files: set[Path] = set()
    for relative_root in SOURCE_INPUT_ROOTS:
        root = webui_root / relative_root
        if not root.exists():
            continue
        if root.is_symlink():
            raise ArtifactError(f"WebUI build input must not be a symlink: {root}")
        candidates = [root] if root.is_file() else root.rglob("*")
        for path in candidates:
            if path.is_symlink():
                raise ArtifactError(f"WebUI build input must not be a symlink: {path}")
            if not path.is_file():
                continue
            if path.name in IGNORED_SOURCE_FILE_NAMES:
                continue
            files.add(path)
    return sorted(
        files,
        key=lambda path: path.relative_to(webui_root).as_posix().encode("utf-8"),
    )


def source_fingerprint(webui_root: Path = DEFAULT_WEBUI_ROOT) -> str:
    """Hash every input that can affect the canonical Vite artifact."""

    webui_root = webui_root.resolve()
    digest = hashlib.sha256()
    for path in _source_files(webui_root):
        relative = path.relative_to(webui_root).as_posix()
        content = path.read_bytes()
        if (
            relative == ".node-version"
            or relative.startswith(".env")
            or path.suffix.lower() in NORMALIZED_TEXT_SUFFIXES
        ):
            text = content.decode("utf-8", errors="replace")
            content = text.replace("\r\n", "\n").replace("\r", "\n").encode()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def verify_sdist_source_inventory(webui_root: Path = DEFAULT_WEBUI_ROOT) -> None:
    """Reject local frontend inputs that Hatch's Git-backed sdist will omit.

    A direct local wheel may intentionally contain private customizations. A
    standard sdist cannot: downstream wheel builds only see tracked sources,
    so accepting an untracked input would create a tarball whose embedded
    artifact can never validate against its extracted source tree.
    """

    webui_root = webui_root.resolve()
    repository_root = webui_root.parent
    if not (repository_root / ".git").exists():
        # An extracted sdist has no VCS metadata. Its source inventory was
        # already constrained when the archive was produced.
        return

    relative_webui = webui_root.relative_to(repository_root).as_posix()
    pathspecs = [f"{relative_webui}/{root}" for root in SOURCE_INPUT_ROOTS]
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "-z", "--", *pathspecs],
            cwd=repository_root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise ArtifactError(
            "cannot inspect untracked frontend inputs for the standard sdist"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ArtifactError(
            "cannot inspect untracked frontend inputs for the standard sdist"
            + (f": {stderr}" if stderr else "")
        )

    untracked = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        if PurePosixPath(relative).name in IGNORED_SOURCE_FILE_NAMES:
            continue
        untracked.append(relative)
    if untracked:
        joined = ", ".join(
            sorted(untracked, key=lambda path: path.encode("utf-8", errors="surrogateescape"))
        )
        raise ArtifactError(
            "standard sdists forbid untracked frontend build inputs because Hatch "
            f"will omit them: {joined}. Track or remove these files, or build a "
            "direct local wheel for a private customization"
        )


def verify_dist(
    dist_dir: Path,
    *,
    webui_root: Path = DEFAULT_WEBUI_ROOT,
    forbid_personal_bgm: bool = False,
) -> dict[str, bytes]:
    """Return artifact files after checking manifest and entrypoint integrity."""

    dist_dir = dist_dir.resolve()
    files = _files(dist_dir)
    forbidden = _forbidden_artifact_paths(files)
    if forbidden:
        raise ArtifactError(
            f"WebUI artifact contains forbidden metadata or sensitive files: {forbidden}"
        )
    index = files.get("index.html")
    manifest_bytes = files.get(MANIFEST_NAME)
    if not index:
        raise ArtifactError(f"WebUI entrypoint is missing or empty: {dist_dir / 'index.html'}")
    if not manifest_bytes:
        raise ArtifactError(f"WebUI artifact manifest is missing: {dist_dir / MANIFEST_NAME}")

    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"WebUI artifact manifest is invalid: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 1
        or not isinstance(manifest.get("sourceFingerprint"), str)
        or not isinstance(manifest.get("files"), list)
    ):
        raise ArtifactError("WebUI artifact manifest has an unsupported schema")
    current_fingerprint = source_fingerprint(webui_root)
    if manifest["sourceFingerprint"] != current_fingerprint:
        raise ArtifactError(
            "WebUI artifact is stale for the current frontend source; "
            "run `cd opensquilla-webui && npm run build`"
        )

    expected_records = [
        {
            "path": relative,
            "size": len(content),
            "sha256": _sha256(content),
        }
        for relative, content in sorted(files.items(), key=lambda item: item[0].encode("utf-8"))
        if relative != MANIFEST_NAME
    ]
    if manifest["files"] != expected_records:
        raise ArtifactError("WebUI artifact files do not match the generated manifest")
    if forbid_personal_bgm:
        _verify_official_music(files)

    try:
        html = index.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError(f"WebUI index.html is not valid UTF-8: {exc}") from exc
    references = []
    for raw in re.findall(r'\b(?:src|href)="([^"]+)"', html):
        if raw.startswith(("data:", "http://", "https://", "//", "#")):
            continue
        relative = raw.split("?", 1)[0].split("#", 1)[0].removeprefix("./")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise ArtifactError(f"WebUI entry asset escapes the artifact: {raw}")
        if relative:
            references.append(relative)
    if not any(path.endswith(".js") for path in references):
        raise ArtifactError("WebUI index.html has no JavaScript module entry")
    if not any(path.endswith(".css") for path in references):
        raise ArtifactError("WebUI index.html has no stylesheet entry")
    missing = sorted(path for path in references if path not in files)
    if missing:
        raise ArtifactError(f"WebUI index.html references missing assets: {missing}")

    return files


def verify_wheel(
    dist_dir: Path,
    wheel_path: Path,
    *,
    webui_root: Path = DEFAULT_WEBUI_ROOT,
    forbid_personal_bgm: bool = False,
) -> None:
    """Require the wheel's WebUI tree to be byte-identical to ``dist_dir``."""

    files = verify_dist(
        dist_dir,
        webui_root=webui_root,
        forbid_personal_bgm=forbid_personal_bgm,
    )
    if not wheel_path.is_file():
        raise ArtifactError(f"wheel is missing: {wheel_path}")
    with zipfile.ZipFile(wheel_path) as wheel:
        packaged = {
            name.removeprefix(WHEEL_PREFIX): wheel.read(name)
            for name in wheel.namelist()
            if name.startswith(WHEEL_PREFIX) and not name.endswith("/")
        }
    expected_names = set(files)
    packaged_names = set(packaged)
    if packaged_names != expected_names:
        missing = sorted(expected_names - packaged_names)
        unexpected = sorted(packaged_names - expected_names)
        raise ArtifactError(
            f"wheel WebUI file set differs from the verified artifact; "
            f"missing={missing}, unexpected={unexpected}"
        )
    changed = sorted(name for name in expected_names if packaged[name] != files[name])
    if changed:
        raise ArtifactError(f"wheel contains changed WebUI bytes: {changed}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=DEFAULT_DIST_DIR,
        help="generated WebUI directory",
    )
    parser.add_argument(
        "--webui-root",
        type=Path,
        default=DEFAULT_WEBUI_ROOT,
        help="frontend source directory used to validate the artifact fingerprint",
    )
    parser.add_argument("--wheel", type=Path, help="optional wheel to compare byte-for-byte")
    parser.add_argument(
        "--forbid-personal-bgm",
        action="store_true",
        help="reject local BGM files and overrides in an official artifact",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        files = verify_dist(
            args.dist,
            webui_root=args.webui_root,
            forbid_personal_bgm=args.forbid_personal_bgm,
        )
        if args.wheel is not None:
            verify_wheel(
                args.dist,
                args.wheel,
                webui_root=args.webui_root,
                forbid_personal_bgm=args.forbid_personal_bgm,
            )
    except (ArtifactError, OSError, zipfile.BadZipFile) as exc:
        print(f"verify_webui_artifact: {exc}", file=sys.stderr)
        return 1
    suffix = f" and wheel {args.wheel}" if args.wheel else ""
    print(f"Verified WebUI artifact ({len(files)} files){suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
