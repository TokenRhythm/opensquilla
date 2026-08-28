#!/usr/bin/env python3
"""Build an SRT subtitle file from an ai-video-script 3-shot script.

Parses each ``=== SHOT_N ===`` block, picks the per-shot ``DURATION_S``
and ``VOICEOVER`` fields, and emits SRT cues whose timestamps accumulate
across shots. Designed as the meta-short-drama subtitling step that
runs after merge.

The script is read from --script (path) or stdin if --script is empty.
Stdin lets the orchestrator pipe ``outputs.final_script`` directly
without writing a temp file.

Usage:
    python build_srt.py --output drama.srt [--script PATH] [--gap-ms 0]

Output: prints the absolute path of the written SRT on stdout.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SHOT_RE = re.compile(
    r"===\s*SHOT_(\d+)\s*===(.*?)(?====\s*SHOT_\d+\s*===|\Z)",
    re.DOTALL,
)
_DUR_RE = re.compile(r"^\s*DURATION_S\s*:\s*(\d+)", re.MULTILINE)
_VO_RE = re.compile(r"^\s*VOICEOVER\s*:\s*(.+?)\s*$", re.MULTILINE)


def parse_script(text: str) -> list[tuple[int, int, str]]:
    """Return [(shot_number, duration_s, voiceover), ...].

    Voiceover values of literal 'none' / empty / dashes are normalised
    to empty strings; such shots produce no SRT cue but their duration
    still advances the timestamp cursor.
    """
    out: list[tuple[int, int, str]] = []
    for match in _SHOT_RE.finditer(text):
        shot_no = int(match.group(1))
        block = match.group(2)
        dur_m = _DUR_RE.search(block)
        vo_m = _VO_RE.search(block)
        if not dur_m:
            continue
        duration = int(dur_m.group(1))
        voiceover = (vo_m.group(1) if vo_m else "").strip()
        if voiceover.lower() in {"", "none", "-", "--"}:
            voiceover = ""
        out.append((shot_no, duration, voiceover))
    return out


def fmt_ts(total_ms: int) -> str:
    """Convert milliseconds to SRT timestamp ``HH:MM:SS,mmm``."""
    ms = max(0, int(total_ms))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(
    shots: list[tuple[int, int, str]],
    gap_ms: int,
    leading_offset_ms: int = 0,
) -> str:
    """Compose SRT text from parsed shots.

    ``leading_offset_ms`` shifts every cue forward by that many ms — useful
    when the final merged video has a prepended cover clip before the
    shots' content actually starts.
    """
    lines: list[str] = []
    cursor_ms = max(0, leading_offset_ms)
    cue_index = 1
    for _shot_no, duration_s, voiceover in shots:
        shot_ms = duration_s * 1000
        if voiceover:
            start = cursor_ms
            # Hold the line until ~gap_ms before the next shot starts so
            # the cut doesn't visually clip the text.
            end = max(start + 800, cursor_ms + shot_ms - max(0, gap_ms))
            lines.append(str(cue_index))
            lines.append(f"{fmt_ts(start)} --> {fmt_ts(end)}")
            lines.append(voiceover)
            lines.append("")
            cue_index += 1
        cursor_ms += shot_ms
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--script", default="",
        help="Path to script text file. If empty/missing, read from stdin.",
    )
    parser.add_argument("--output", "-o", required=True, help="Output .srt path")
    parser.add_argument(
        "--gap-ms", type=int, default=200,
        help="Tail pad subtracted from each cue's end_time so the line "
             "vanishes ~Nms before the next shot starts. Default 200.",
    )
    parser.add_argument(
        "--leading-offset-ms", type=int, default=0,
        help="Shift every cue forward by this many milliseconds. Use to "
             "skip past a cover/intro clip that precedes SHOT_1 in the "
             "merged video. Default 0 (no shift).",
    )
    args = parser.parse_args()

    if args.script:
        try:
            text = Path(args.script).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error: cannot read --script {args.script!r}: {exc}", file=sys.stderr)
            return 1
    else:
        # Read raw bytes from stdin and decode as UTF-8 ourselves —
        # sys.stdin defaults to the Windows console code page (cp936/
        # GBK), which mangles UTF-8 CJK input into surrogates that the
        # SRT writer then refuses to encode.
        text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not text.strip():
        print("Error: empty script input.", file=sys.stderr)
        return 1

    shots = parse_script(text)
    if not shots:
        print(
            "Error: no SHOT_N blocks found in script. Did ai-video-script "
            "emit the OUTPUT FORMAT?",
            file=sys.stderr,
        )
        return 1

    srt = build_srt(
        shots,
        gap_ms=max(0, args.gap_ms),
        leading_offset_ms=max(0, args.leading_offset_ms),
    )
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(srt, encoding="utf-8")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
