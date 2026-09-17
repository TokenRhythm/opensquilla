from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.verify_webui_artifact import source_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGER = REPO_ROOT / "opensquilla-webui" / "scripts" / "stage-dist.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_normalizer_excludes_only_music_maintainer_readme_before_manifest_and_stage(
    tmp_path: Path,
) -> None:
    webui = tmp_path / "opensquilla-webui"
    scripts = webui / "scripts"
    dist = webui / "dist"
    scripts.mkdir(parents=True)
    for script in ("normalize-dist.mjs", "verify-dist.mjs", "stage-dist.mjs"):
        shutil.copyfile(STAGER.parent / script, scripts / script)
    (webui / "public/music").mkdir(parents=True)
    source_readme = webui / "public/music/README.md"
    source_readme.write_text("Music maintainer guide\n", encoding="utf-8")
    (dist / "music").mkdir(parents=True)
    shutil.copyfile(source_readme, dist / "music/README.md")
    playlist = b'{"tracks": []}\n'
    (dist / "music/playlist.json").write_bytes(playlist)
    (dist / "assets").mkdir()
    (dist / "assets/app.js").write_text("export {};\n", encoding="utf-8")
    (dist / "assets/app.css").write_text("body{}\n", encoding="utf-8")
    # Identically named files outside the exact music path remain untouched.
    (dist / "assets/README.md").write_text("Unrelated asset documentation\n", encoding="utf-8")
    entrypoint = (
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">'
    )
    for name in ("index.html", "desktop.html"):
        (dist / name).write_text(entrypoint, encoding="utf-8")

    for script, options in (
        ("normalize-dist.mjs", []), ("verify-dist.mjs", ["--write"]), ("stage-dist.mjs", []),
    ):
        completed = subprocess.run(
            ["node", str(scripts / script), *options],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert completed.returncode == 0, completed.stderr

    assert source_readme.read_text(encoding="utf-8") == "Music maintainer guide\n"
    staged = tmp_path / "src/opensquilla/gateway/static/dist"
    for artifact in (dist, staged):
        assert not (artifact / "music/README.md").exists()
        assert (artifact / "music/playlist.json").read_bytes() == playlist
        assert (artifact / "assets/README.md").read_text(encoding="utf-8") == (
            "Unrelated asset documentation\n"
        )
        manifest_path = artifact / "webui-artifact-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "music/README.md" not in {entry["path"] for entry in manifest["files"]}
    assert (staged / "webui-artifact-manifest.json").read_bytes() == (
        dist / "webui-artifact-manifest.json"
    ).read_bytes()


def _write_manifest(webui_root: Path, dist: Path) -> None:
    records = []
    for path in sorted(dist.rglob("*")):
        if not path.is_file() or path.name == "webui-artifact-manifest.json":
            continue
        content = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(dist).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    (dist / "webui-artifact-manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "sourceFingerprint": source_fingerprint(webui_root),
                "files": records,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_stage_dist_copies_verified_source_owned_artifact(tmp_path: Path) -> None:
    webui = tmp_path / "opensquilla-webui"
    source = webui / "dist"
    staged = tmp_path / "src/opensquilla/gateway/static/dist"
    (webui / "src").mkdir(parents=True)
    (webui / ".node-version").write_text("22.12.0\n", encoding="utf-8")
    (webui / "package.json").write_text("{}\n", encoding="utf-8")
    (webui / "src/App.vue").write_text("<template>probe</template>\n", encoding="utf-8")
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    (source / "desktop.html").write_text(
        '<script type="module" src="assets/app.js"></script>'
        '<link rel="stylesheet" href="assets/app.css">',
        encoding="utf-8",
    )
    (source / "assets/app.js").write_text("export {};\n", encoding="utf-8")
    (source / "assets/app.css").write_text("body{}\n", encoding="utf-8")
    _write_manifest(webui, source)

    result = subprocess.run(
        [
            "node",
            str(STAGER),
            "--source",
            str(source),
            "--destination",
            str(staged),
            "--webui-root",
            str(webui),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert staged.is_dir()
    assert (staged / "webui-artifact-manifest.json").read_bytes() == (
        source / "webui-artifact-manifest.json"
    ).read_bytes()

    check = subprocess.run(
        [
            "node",
            str(STAGER),
            "--check",
            "--source",
            str(source),
            "--destination",
            str(staged),
            "--webui-root",
            str(webui),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr

    # A real stage replaces the destination, so files removed from the source
    # cannot survive in the package copy.  This protects wheel builds from a
    # stale asset left by an earlier Vite build.
    stale_asset = staged / "assets/removed-by-next-build.js"
    stale_asset.write_text("stale\n", encoding="utf-8")
    restage = subprocess.run(
        [
            "node",
            str(STAGER),
            "--source",
            str(source),
            "--destination",
            str(staged),
            "--webui-root",
            str(webui),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert restage.returncode == 0, restage.stderr
    assert not stale_asset.exists()

    (staged / "assets/app.js").write_text("export { changed };\n", encoding="utf-8")
    stale = subprocess.run(
        [
            "node",
            str(STAGER),
            "--check",
            "--source",
            str(source),
            "--destination",
            str(staged),
            "--webui-root",
            str(webui),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert stale.returncode != 0
    assert "missing or stale" in stale.stderr

    # The manifest binds the artifact to the exact source tree.  A consumer
    # must reject a source/stage pair that no longer belongs together.
    (webui / "src/App.vue").write_text("<template>changed</template>\n", encoding="utf-8")
    source_stale = subprocess.run(
        [
            "node",
            str(STAGER),
            "--check",
            "--source",
            str(source),
            "--destination",
            str(staged),
            "--webui-root",
            str(webui),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert source_stale.returncode != 0
    assert "stale for the current frontend source" in source_stale.stderr
