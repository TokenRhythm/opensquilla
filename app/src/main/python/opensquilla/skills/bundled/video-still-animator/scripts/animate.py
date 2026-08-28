#!/usr/bin/env python3
"""Ken Burns animator: turn a single still image into a short MP4.

Wraps ffmpeg's zoompan filter to produce an N-second clip with a slow
zoom-in on the input PNG/JPG, padded to the target aspect ratio. Adds a
silent AAC audio track so downstream merger steps don't trip on
mixed-audio inputs.

Designed as the `on_failure` substitute for the meta-short-drama
seedance video steps: when seedance moderation blocks a video, this
script produces a valid replacement clip from the still that already
landed on disk.

Usage:
    python animate.py --input shot1.png --output shot1.mp4 \\
        [--duration 5] [--width 720] [--height 1280] [--fps 24] \\
        [--zoom-rate 0.0015] [--ffmpeg-path ffmpeg]

Exit codes:
    0  success — output MP4 written.
    1  failure — stderr carries the cause.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

WINGET_FFMPEG_GLOB = (
    "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_*/"
    "ffmpeg-*-full_build/bin/ffmpeg.exe"
)


def resolve_ffmpeg(explicit: str) -> str:
    """Return an ffmpeg binary path, falling back to common Windows locations."""
    found = shutil.which(explicit)
    if found:
        return found
    if os.name != "nt":
        return explicit  # let subprocess fail with the canonical error
    local_app = os.environ.get("LOCALAPPDATA", "")
    if not local_app:
        return explicit
    from glob import glob
    for hit in glob(os.path.join(local_app, WINGET_FFMPEG_GLOB)):
        if os.path.isfile(hit):
            return hit
    candidates = [
        os.path.join(os.environ.get("USERPROFILE", ""), "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return explicit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", required=True, help="Input PNG or JPG")
    parser.add_argument("--output", "-o", required=True, help="Output MP4 path")
    parser.add_argument("--duration", type=float, default=5.0, help="Clip length in seconds")
    parser.add_argument("--width", type=int, default=720)
    parser.add_argument("--height", type=int, default=1280)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument(
        "--zoom-rate", type=float, default=0.0015,
        help="Per-frame zoom increment; 0.0015 over 5s ≈ 1.18x final zoom",
    )
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"Error: input not found: {src}", file=sys.stderr)
        return 1

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    total_frames = max(1, int(round(args.duration * args.fps)))
    ffmpeg_bin = resolve_ffmpeg(args.ffmpeg_path)

    # Two-step pipeline inside one filter_complex:
    #   [0:v] scale to cover, then zoompan over N frames at fps.
    #   [1:a] anullsrc gives a silent stereo track at 44.1kHz.
    # -shortest cuts the audio to match video length.
    vf = (
        f"[0:v]scale={args.width * 4}:{args.height * 4}:flags=lanczos,"
        f"zoompan=z='min(zoom+{args.zoom_rate},1.2)':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={total_frames}:s={args.width}x{args.height}:fps={args.fps}[v]"
    )
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loop", "1",
        "-i", str(src),
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", vf,
        "-map", "[v]",
        "-map", "1:a",
        "-t", f"{args.duration}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        print(f"Error: ffmpeg not found ({exc}). Install via video-merger/install.ps1 or pass --ffmpeg-path.", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[-2000:])
        print(f"Error: ffmpeg exited {proc.returncode}", file=sys.stderr)
        return 1
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
