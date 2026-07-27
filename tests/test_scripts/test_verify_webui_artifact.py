from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from scripts.verify_webui_artifact import (
    MANIFEST_NAME,
    WHEEL_PREFIX,
    ArtifactError,
    source_fingerprint,
    verify_dist,
    verify_wheel,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE_VERIFIER = REPO_ROOT / "opensquilla-webui" / "scripts" / "verify-dist.mjs"


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _record(root: Path, relative: str) -> dict[str, object]:
    content = (root / relative).read_bytes()
    return {
        "path": relative,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _write_manifest(webui: Path, dist: Path) -> None:
    relatives = sorted(
        (
            path.relative_to(dist).as_posix()
            for path in dist.rglob("*")
            if path.is_file() and path.name != MANIFEST_NAME
        ),
        key=_utf8_key,
    )
    manifest = {
        "schemaVersion": 1,
        "sourceFingerprint": source_fingerprint(webui),
        "files": [_record(dist, relative) for relative in relatives],
    }
    (dist / MANIFEST_NAME).write_text(
        f"{json.dumps(manifest, indent=2)}\n",
        encoding="utf-8",
    )


def _artifact(
    tmp_path: Path,
    *,
    include_personal_audio: bool = False,
    include_local_playlist: bool = False,
    personal_audio_name: str = "local.mp3",
    tracked_playlist: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    webui = tmp_path / "opensquilla-webui"
    dist = tmp_path / "dist"
    (webui / "src").mkdir(parents=True)
    (webui / ".node-version").write_text("22.12.0\n", encoding="utf-8")
    (webui / "package.json").write_text('{"scripts":{"build":"vite build"}}\n')
    (webui / "src/App.vue").write_text("<template>Hello</template>\n")
    if include_personal_audio:
        (webui / "public/music").mkdir(parents=True)
        (webui / "public/music" / personal_audio_name).write_bytes(b"synthetic personal audio")
    if include_local_playlist:
        (webui / "public/music").mkdir(parents=True, exist_ok=True)
        (webui / "public/music/playlist.local.json").write_text('{"tracks": []}\n')
    if tracked_playlist is not None:
        (webui / "public/music").mkdir(parents=True, exist_ok=True)
        (webui / "public/music/playlist.json").write_text(
            f"{json.dumps(tracked_playlist)}\n",
            encoding="utf-8",
        )

    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    (dist / "assets/app.js").write_text("console.log('hello')\n", encoding="utf-8")
    (dist / "assets/app.css").write_text("body { color: black; }\n", encoding="utf-8")
    if include_personal_audio:
        (dist / "music").mkdir()
        (dist / "music" / personal_audio_name).write_bytes(b"synthetic personal audio")
    if include_local_playlist:
        (dist / "music").mkdir(exist_ok=True)
        (dist / "music/playlist.local.json").write_text('{"tracks": []}\n')
    if tracked_playlist is not None:
        (dist / "music").mkdir(exist_ok=True)
        (dist / "music/playlist.json").write_text(
            f"{json.dumps(tracked_playlist)}\n",
            encoding="utf-8",
        )
    _write_manifest(webui, dist)
    return webui, dist


def test_verify_dist_accepts_artifact_bound_to_current_source(tmp_path: Path) -> None:
    webui, dist = _artifact(tmp_path)

    files = verify_dist(dist, webui_root=webui)

    assert set(files) == {
        "assets/app.css",
        "assets/app.js",
        "index.html",
        MANIFEST_NAME,
    }


def test_verify_dist_rejects_artifact_after_source_changes(tmp_path: Path) -> None:
    webui, dist = _artifact(tmp_path)
    (webui / "src/App.vue").write_text("<template>Changed</template>\n")

    with pytest.raises(ArtifactError, match="stale for the current frontend source"):
        verify_dist(dist, webui_root=webui)


def test_source_fingerprint_ignores_ds_store_but_tracks_personal_bgm(
    tmp_path: Path,
) -> None:
    webui, _ = _artifact(tmp_path)
    baseline = source_fingerprint(webui)

    (webui / "src/.DS_Store").write_bytes(b"Finder metadata")
    (webui / "public").mkdir()
    (webui / "public/.DS_Store").write_bytes(b"more Finder metadata")
    assert source_fingerprint(webui) == baseline

    env_file = webui / ".env.production"
    env_file.write_bytes(b"VITE_FLAG=enabled\r\n")
    env_fingerprint = source_fingerprint(webui)
    env_file.write_bytes(b"VITE_FLAG=enabled\n")
    assert source_fingerprint(webui) == env_fingerprint
    assert env_fingerprint != baseline
    env_file.unlink()

    music = webui / "public/music"
    music.mkdir()
    (music / "private.mp3").write_bytes(b"private audio")
    assert source_fingerprint(webui) != baseline


def test_verify_dist_rejects_tampered_generated_file(tmp_path: Path) -> None:
    webui, dist = _artifact(tmp_path)
    (dist / "assets/app.js").write_text("console.log('tampered')\n")

    with pytest.raises(ArtifactError, match="do not match the generated manifest"):
        verify_dist(dist, webui_root=webui)


@pytest.mark.parametrize(
    "relative",
    (
        ".DS_Store",
        ".env",
        "config/.env.production",
        "config/.npmrc",
        "config/client.pem",
        "config/private.key",
    ),
)
def test_verify_dist_rejects_metadata_and_sensitive_files(
    tmp_path: Path,
    relative: str,
) -> None:
    webui, dist = _artifact(tmp_path)
    target = dist / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret or metadata\n", encoding="utf-8")
    _write_manifest(webui, dist)

    with pytest.raises(ArtifactError, match="forbidden metadata or sensitive files"):
        verify_dist(dist, webui_root=webui)


def test_personal_audio_is_local_only_not_globally_forbidden(tmp_path: Path) -> None:
    webui, dist = _artifact(tmp_path, include_personal_audio=True)

    files = verify_dist(dist, webui_root=webui)
    assert files["music/local.mp3"] == b"synthetic personal audio"

    with pytest.raises(ArtifactError, match="forbidden in official WebUI artifacts"):
        verify_dist(dist, webui_root=webui, forbid_personal_bgm=True)


def test_official_guard_rejects_audio_extensions_outside_the_documented_list(
    tmp_path: Path,
) -> None:
    webui, dist = _artifact(
        tmp_path,
        include_personal_audio=True,
        personal_audio_name="voice.aac",
    )

    assert "music/voice.aac" in verify_dist(dist, webui_root=webui)
    with pytest.raises(ArtifactError, match="music/voice.aac"):
        verify_dist(dist, webui_root=webui, forbid_personal_bgm=True)


def test_local_playlist_override_is_forbidden_only_in_official_artifacts(
    tmp_path: Path,
) -> None:
    webui, dist = _artifact(tmp_path, include_local_playlist=True)

    assert "music/playlist.local.json" in verify_dist(dist, webui_root=webui)
    with pytest.raises(ArtifactError, match="playlist.local.json"):
        verify_dist(dist, webui_root=webui, forbid_personal_bgm=True)


def test_official_guard_rejects_tracks_in_the_tracked_playlist(tmp_path: Path) -> None:
    webui, dist = _artifact(
        tmp_path,
        tracked_playlist={
            "tracks": [
                {
                    "id": "private-stream",
                    "title": "Private stream",
                    "src": "https://example.com/private.mp3",
                }
            ]
        },
    )

    assert "music/playlist.json" in verify_dist(dist, webui_root=webui)
    with pytest.raises(ArtifactError, match="must keep its tracks list empty"):
        verify_dist(dist, webui_root=webui, forbid_personal_bgm=True)


def test_invalid_manifest_and_entrypoint_return_actionable_artifact_errors(
    tmp_path: Path,
) -> None:
    webui, dist = _artifact(tmp_path)
    (dist / MANIFEST_NAME).write_text("[]\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="unsupported schema"):
        verify_dist(dist, webui_root=webui)

    webui, dist = _artifact(tmp_path / "invalid-index")
    (dist / "index.html").write_bytes(b"\xff\xfe")
    manifest = json.loads((dist / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["files"] = [_record(dist, record["path"]) for record in manifest["files"]]
    (dist / MANIFEST_NAME).write_text(f"{json.dumps(manifest, indent=2)}\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="index.html is not valid UTF-8"):
        verify_dist(dist, webui_root=webui)


def test_verify_wheel_requires_byte_identical_artifact(tmp_path: Path) -> None:
    webui, dist = _artifact(tmp_path)
    wheel = tmp_path / "opensquilla-0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in sorted(dist.rglob("*")):
            if path.is_file():
                archive.write(path, f"{WHEEL_PREFIX}{path.relative_to(dist).as_posix()}")

    verify_wheel(dist, wheel, webui_root=webui)

    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(f"{WHEEL_PREFIX}unexpected.txt", "not allowed")
    with pytest.raises(ArtifactError, match="file set differs"):
        verify_wheel(dist, wheel, webui_root=webui)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_and_python_source_fingerprints_share_order_and_line_endings(
    tmp_path: Path,
) -> None:
    webui = tmp_path / "opensquilla-webui"
    source = webui / "src"
    public = webui / "public"
    source.mkdir(parents=True)
    public.mkdir()
    (webui / ".node-version").write_text("22.12.0\n", encoding="utf-8")
    (source / "😀.vue").write_text("<template>emoji</template>\n", encoding="utf-8")
    (source / "Ａ.vue").write_text("<template>full width</template>\n", encoding="utf-8")
    (public / "site.webmanifest").write_text('{"name":"OpenSquilla"}\n', encoding="utf-8")
    baseline = source_fingerprint(webui)

    (webui / ".node-version").write_bytes(b"22.12.0\r\n")
    (source / "😀.vue").write_bytes(b"<template>emoji</template>\r\n")
    (public / "site.webmanifest").write_bytes(b'{"name":"OpenSquilla"}\r\n')
    (source / ".DS_Store").write_bytes(b"ignored metadata")

    script = (
        f"import {{ sourceFingerprint }} from {json.dumps(NODE_VERIFIER.as_uri())};"
        "console.log(sourceFingerprint(process.argv[1]));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script, str(webui)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert source_fingerprint(webui) == baseline
    assert result.stdout.strip() == baseline


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_verifier_runs_when_invoked_through_symlink(tmp_path: Path) -> None:
    symlink = tmp_path / "verify-dist-link.mjs"
    try:
        symlink.symlink_to(NODE_VERIFIER)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    result = subprocess.run(
        ["node", str(symlink), "--definitely-invalid-option"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "verify-dist:" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_verifier_rejects_sensitive_artifact_files(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets/app.js").write_text("console.log('hello')\n", encoding="utf-8")
    (dist / "assets/app.css").write_text("body{}\n", encoding="utf-8")
    (dist / "index.html").write_text(
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    (dist / ".env.production").write_text(
        "VITE_PRIVATE_TOKEN=must-not-ship\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(NODE_VERIFIER), "--write", str(dist)],
        cwd=REPO_ROOT / "opensquilla-webui",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "forbidden metadata or sensitive files" in result.stderr
    assert ".env.production" in result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_python_accepts_node_manifest_with_unicode_artifact_names(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (assets / "😀.js").write_text("console.log('emoji')\n", encoding="utf-8")
    (assets / "Ａ.css").write_text("body{}\n", encoding="utf-8")
    (dist / "index.html").write_text(
        '<script type="module" src="assets/😀.js"></script>'
        '<link rel="stylesheet" href="assets/Ａ.css">',
        encoding="utf-8",
    )

    subprocess.run(
        ["node", str(NODE_VERIFIER), "--write", str(dist)],
        cwd=REPO_ROOT / "opensquilla-webui",
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    files = verify_dist(dist, webui_root=REPO_ROOT / "opensquilla-webui")
    assert "assets/😀.js" in files
    assert "assets/Ａ.css" in files


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_official_guard_rejects_tracks_in_the_tracked_playlist(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "music").mkdir()
    (dist / "assets/app.js").write_text("console.log('hello')\n", encoding="utf-8")
    (dist / "assets/app.css").write_text("body{}\n", encoding="utf-8")
    (dist / "index.html").write_text(
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    (dist / "music/playlist.json").write_text(
        '{"tracks":[{"id":"private","src":"https://example.com/private.mp3"}]}\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["node", str(NODE_VERIFIER), "--write", str(dist)],
        cwd=REPO_ROOT / "opensquilla-webui",
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    result = subprocess.run(
        ["node", str(NODE_VERIFIER), "--forbid-personal-bgm", str(dist)],
        cwd=REPO_ROOT / "opensquilla-webui",
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "must keep its tracks list empty" in result.stderr
