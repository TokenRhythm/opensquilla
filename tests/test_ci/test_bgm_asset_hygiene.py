from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MUSIC_ROOTS = (
    "opensquilla-webui/public/music",
    "opensquilla-webui/dist/music",
    "src/opensquilla/gateway/static/dist/music",
)
PRIVATE_FILES = ("track.mp3", "track.aac", "playlist.local.json", "notes.txt")


def test_retired_personal_media_is_ignored_at_every_depth() -> None:
    candidates = list(MUSIC_ROOTS) + [
        f"{root}/{relative}{filename}"
        for root in MUSIC_ROOTS
        for relative in ("", "album/", "album/live/")
        for filename in PRIVATE_FILES
    ]
    not_ignored = [
        path
        for path in candidates
        if subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", path],
            cwd=REPO_ROOT,
            check=False,
        ).returncode
        != 0
    ]

    assert not not_ignored, "retired personal media could be committed:\n" + "\n".join(not_ignored)
