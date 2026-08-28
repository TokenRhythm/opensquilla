#!/usr/bin/env python3
"""Burn an SRT subtitle file into an MP4 video using ffmpeg.

Single-pass re-encode that overlays the SRT cues onto the video stream
via ffmpeg's ``subtitles=`` filter (libass under the hood). The audio
stream is copied without re-encoding. A managed Noto CJK font directory can
be supplied explicitly or through ``OPENSQUILLA_MEDIA_FONTS_DIR``.

Used by meta-short-drama after video-merger has produced the merged
``final.mp4``: this script writes ``final_subtitled.mp4`` next to it,
matching the user's voiceover language.

Usage:
    python burn.py --input final.mp4 --subtitles drama.srt \\
        --output final_subtitled.mp4 \\
        [--font "Noto Sans CJK SC"] [--fonts-dir /path/to/fonts] \\
        [--font-size 28] [--margin-v 80] [--ffmpeg-path ffmpeg]

Exit codes:
    0 — success, output written.
    1 — failure; stderr carries the ffmpeg tail.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from glob import glob
from pathlib import Path
from typing import NamedTuple

_WINGET_FFMPEG_GLOB = (
    "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_*/"
    "ffmpeg-*-full_build/bin"
)


class _VideoInfo(NamedTuple):
    width: int
    height: int
    duration_s: float


def _resolve_ffprobe(explicit: str, ffmpeg_bin: str) -> str:
    """Resolve ffprobe without rewriting directory names such as ffmpeg-full."""
    if explicit not in {"ffprobe", "ffprobe.exe"}:
        found = shutil.which(explicit)
        if found:
            return found
        if os.path.isabs(explicit) and os.path.isfile(explicit):
            return explicit
    ffmpeg_name = Path(ffmpeg_bin).name.lower()
    sibling_name = "ffprobe.exe" if ffmpeg_name.endswith(".exe") else "ffprobe"
    sibling = Path(ffmpeg_bin).with_name(sibling_name)
    if sibling.is_file():
        return str(sibling)
    found = shutil.which(explicit)
    if found:
        return found
    if os.path.isabs(explicit) and os.path.isfile(explicit):
        return explicit
    return explicit


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _probe_video(ffprobe_bin: str, video_path: Path) -> _VideoInfo | None:
    """Return verified video dimensions and duration, or ``None`` on any defect."""
    try:
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            return None
    except OSError:
        return None
    if not Path(ffprobe_bin).is_file() and shutil.which(ffprobe_bin) is None:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe_bin, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=codec_type,width,height,duration:format=duration",
                "-of", "json",
                str(video_path),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    raw = (
        proc.stdout.decode("utf-8", "replace")
        if isinstance(proc.stdout, bytes)
        else proc.stdout
    )
    try:
        payload = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        streams = payload["streams"]
        stream = next(
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        )
        width = int(stream["width"])
        height = int(stream["height"])
    except (KeyError, StopIteration, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    format_info = payload.get("format") if isinstance(payload, dict) else None
    format_duration = format_info.get("duration") if isinstance(format_info, dict) else None
    duration = _positive_float(format_duration) or _positive_float(stream.get("duration"))
    if duration is None:
        return None
    return _VideoInfo(width=width, height=height, duration_s=duration)


def _probe_resolution(ffmpeg_bin: str, video_path: Path) -> tuple[int, int] | None:
    """Compatibility helper returning W x H using ffprobe next to ffmpeg."""
    info = _probe_video(_resolve_ffprobe("ffprobe", ffmpeg_bin), video_path)
    return (info.width, info.height) if info else None


def _validation_error(ffprobe_bin: str, video_path: Path) -> str | None:
    try:
        if not video_path.is_file():
            return "output file was not created"
        if video_path.stat().st_size <= 0:
            return "output file is empty"
    except OSError as exc:
        return f"output file is unreadable ({exc})"
    if _probe_video(ffprobe_bin, video_path) is None:
        return "ffprobe could not decode a positive-duration video stream"
    return None


def _staged_output_path(out_path: Path) -> Path:
    """Create a closed same-directory temporary path suitable for atomic replace."""
    suffix = out_path.suffix if out_path.suffix else ".mp4"
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{out_path.stem}.",
        suffix=suffix,
        dir=out_path.parent,
    )
    os.close(fd)
    return Path(raw_path)


def _discard_staged(path: Path) -> None:
    """Best-effort cleanup that cannot mask the primary media error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _resolve_ffmpeg(explicit: str) -> str:
    """Return ffmpeg path; probe common Windows install locations as fallback."""
    found = shutil.which(explicit)
    if found:
        return found
    if os.path.isabs(explicit) and os.path.isfile(explicit):
        return explicit
    if os.name != "nt":
        return explicit
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        for bin_dir in glob(os.path.join(local_app, _WINGET_FFMPEG_GLOB)):
            candidate = os.path.join(bin_dir, "ffmpeg.exe")
            if os.path.isfile(candidate):
                return candidate
    user_profile = os.environ.get("USERPROFILE", "")
    candidates = [
        os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return explicit


def _escape_subtitle_path(path: str) -> str:
    """Escape a path for ffmpeg's ``subtitles=`` filter argument.

    libass on Windows is picky: drive-letter colons must be backslash-
    escaped, and the path uses forward slashes regardless of OS. Inside
    the filter graph, single quotes wrap the path so commas / brackets
    in the file name don't confuse the parser.
    """
    # Always use forward slashes inside the filter graph.
    normalised = path.replace("\\", "/")
    # Escape drive-letter colon ("C:" -> "C\:") to keep libass happy.
    if len(normalised) >= 2 and normalised[1] == ":" and normalised[0].isalpha():
        normalised = normalised[0] + r"\:" + normalised[2:]
    # Escape any remaining colon (e.g. an unusual file name).
    rest = normalised[3:] if len(normalised) >= 3 else ""
    if ":" in rest:
        normalised = normalised[:3] + rest.replace(":", r"\:")
    # Escape single quotes inside the path (rare on Windows but possible).
    normalised = normalised.replace("'", r"\'")
    return normalised


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", required=True, help="Input MP4 path")
    parser.add_argument("--subtitles", "-s", required=True, help="SRT file path")
    parser.add_argument("--output", "-o", required=True, help="Output MP4 path")
    parser.add_argument(
        "--font",
        default="Noto Sans CJK SC",
        help="One libass font family. Defaults to the managed pan-CJK family.",
    )
    parser.add_argument(
        "--fonts-dir",
        default=os.environ.get("OPENSQUILLA_MEDIA_FONTS_DIR", ""),
        help="Directory containing managed subtitle fonts (defaults from OpenSquilla).",
    )
    parser.add_argument("--font-size", type=int, default=42)
    parser.add_argument(
        "--primary-colour", default="&Hffffff",
        help="ASS colour code for fill (&HBBGGRR). Default white &Hffffff.",
    )
    parser.add_argument(
        "--outline-colour", default="&H000000",
        help="ASS colour code for outline. Default black &H000000.",
    )
    parser.add_argument(
        "--outline", type=int, default=2, help="Outline thickness in px.",
    )
    parser.add_argument(
        "--margin-v", type=int, default=80,
        help="Bottom margin in PX of the source video (we set PlayResX/Y to "
             "the source resolution so MarginV maps 1:1 to pixels).",
    )
    parser.add_argument(
        "--play-res", default="auto",
        help="libass PlayRes as 'WxH', or 'auto' to probe the input MP4. "
             "Setting this makes FontSize and MarginV act in source pixels.",
    )
    parser.add_argument(
        "--alignment", type=int, default=2,
        help="libass alignment: 1=bottom-left, 2=bottom-center, 3=bottom-right, "
             "7=top-left, 8=top-center, 9=top-right. Default 2.",
    )
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument(
        "--preset", default="medium",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"],
    )
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--ffprobe-path", default="ffprobe")
    args = parser.parse_args()

    in_path = Path(args.input)
    srt_path = Path(args.subtitles)
    out_path = Path(args.output)
    if not in_path.is_file():
        print(f"Error: --input not found: {in_path}", file=sys.stderr)
        return 1
    if not srt_path.is_file():
        print(f"Error: --subtitles not found: {srt_path}", file=sys.stderr)
        return 1
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = _resolve_ffmpeg(args.ffmpeg_path)
    ffprobe_bin = _resolve_ffprobe(args.ffprobe_path, ffmpeg_bin)

    try:
        subtitle_text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"Error: could not read --subtitles: {exc}", file=sys.stderr)
        return 1
    if not subtitle_text.strip():
        if _probe_video(ffprobe_bin, in_path) is None:
            print("Error: input is not a readable video", file=sys.stderr)
            return 1
        try:
            staged_path = _staged_output_path(out_path)
        except OSError as exc:
            print(f"Error: could not stage subtitle-free video: {exc}", file=sys.stderr)
            return 1
        try:
            shutil.copyfile(in_path, staged_path)
            validation_error = _validation_error(ffprobe_bin, staged_path)
            if validation_error:
                print(
                    f"Error: copied video validation failed: {validation_error}",
                    file=sys.stderr,
                )
                return 1
            os.replace(staged_path, out_path)
        except OSError as exc:
            print(f"Error: could not copy subtitle-free video: {exc}", file=sys.stderr)
            return 1
        finally:
            _discard_staged(staged_path)
        validation_error = _validation_error(ffprobe_bin, out_path)
        if validation_error:
            print(f"Error: output validation failed: {validation_error}", file=sys.stderr)
            return 1
        print("SUBTITLES_SKIPPED: empty")
        print(str(out_path.resolve()))
        return 0

    fonts_dir_arg = args.fonts_dir or os.environ.get("OPENSQUILLA_MEDIA_FONTS_DIR", "")
    if fonts_dir_arg:
        fonts_dir = Path(fonts_dir_arg).expanduser()
        if not fonts_dir.is_dir():
            print(f"Error: --fonts-dir not found: {fonts_dir}", file=sys.stderr)
            return 1
    else:
        fonts_dir = None

    input_info = _probe_video(ffprobe_bin, in_path)
    if input_info is None:
        print("Error: input is not a readable positive-duration video", file=sys.stderr)
        return 1

    # Resolve PlayRes so MarginV / FontSize map to source-video pixels.
    play_w, play_h = 0, 0
    if args.play_res == "auto":
        play_w, play_h = input_info.width, input_info.height
    elif "x" in args.play_res:
        try:
            play_w, play_h = (int(x) for x in args.play_res.split("x", 1))
        except ValueError:
            print(f"Warning: invalid --play-res {args.play_res!r}; ignoring.", file=sys.stderr)

    srt_arg = _escape_subtitle_path(str(srt_path.resolve()))
    force_style_parts = [
        f"FontName={args.font}",
        f"FontSize={args.font_size}",
        f"PrimaryColour={args.primary_colour}",
        f"OutlineColour={args.outline_colour}",
        "BorderStyle=3",
        f"Outline={args.outline}",
        "Shadow=0",
        f"Alignment={args.alignment}",
        f"MarginV={args.margin_v}",
    ]
    if play_w and play_h:
        force_style_parts.extend([f"PlayResX={play_w}", f"PlayResY={play_h}"])
    force_style = ",".join(force_style_parts)
    filter_parts = [f"subtitles='{srt_arg}'"]
    if fonts_dir is not None:
        filter_parts.append(f"fontsdir='{_escape_subtitle_path(str(fonts_dir.resolve()))}'")
    filter_parts.append(f"force_style='{force_style}'")
    vf = ":".join(filter_parts)

    try:
        staged_path = _staged_output_path(out_path)
    except OSError as exc:
        print(f"Error: could not stage subtitled video: {exc}", file=sys.stderr)
        return 1
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(in_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-crf", str(args.crf),
        "-preset", args.preset,
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(staged_path),
    ]
    print("==> burning subtitles", file=sys.stderr)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        _discard_staged(staged_path)
        print(f"Error: ffmpeg not found ({exc})", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        _discard_staged(staged_path)
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[-2500:])
        print(f"\nError: ffmpeg exited {proc.returncode}", file=sys.stderr)
        return 1
    validation_error = _validation_error(ffprobe_bin, staged_path)
    if validation_error:
        _discard_staged(staged_path)
        print(f"Error: ffmpeg output validation failed: {validation_error}", file=sys.stderr)
        return 1
    try:
        os.replace(staged_path, out_path)
    except OSError as exc:
        _discard_staged(staged_path)
        print(f"Error: could not install subtitled video: {exc}", file=sys.stderr)
        return 1
    validation_error = _validation_error(ffprobe_bin, out_path)
    if validation_error:
        print(f"Error: output validation failed: {validation_error}", file=sys.stderr)
        return 1
    print(str(out_path.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
