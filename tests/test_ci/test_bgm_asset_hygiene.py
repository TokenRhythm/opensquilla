from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MUSIC_ROOTS = (
    "opensquilla-webui/public/music",
    "opensquilla-webui/dist/music",
    "src/opensquilla/gateway/static/dist/music",
)
AUDIO_EXTENSIONS = ("mp3", "m4a", "ogg", "flac", "wav")


def test_personal_bgm_audio_is_ignored_at_every_supported_depth() -> None:
    candidates = [
        f"{root}/{relative}.{extension}"
        for root in MUSIC_ROOTS
        for relative in ("track", "album/track", "album/live/track")
        for extension in AUDIO_EXTENSIONS
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

    assert not not_ignored, "personal BGM audio could be committed:\n" + "\n".join(not_ignored)
