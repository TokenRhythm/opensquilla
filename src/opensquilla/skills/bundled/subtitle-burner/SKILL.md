---
name: subtitle-burner
description: "Burn an SRT subtitle file into an MP4 via ffmpeg's subtitles filter (libass). Single-pass re-encode of video; audio copied as-is. Uses a verified managed Noto Sans CJK font when available. Used by meta-short-drama as the final subtitling step after merge."
description_zh: "通过 ffmpeg 的 subtitles 滤镜（libass）把 SRT 字幕烧录进 MP4；视频单遍重编码，音频原样复制。可用时使用经过验证的托管 Noto Sans CJK 字体。由 meta-short-drama 在合并后执行最终字幕步骤。"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [filesystem-write, process-control]
    requires:
      bins: ["ffmpeg", "ffprobe"]
      anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/burn.py
  args:
    - --input
    - "{{ with.input }}"
    - --subtitles
    - "{{ with.subtitles }}"
    - --output
    - "{{ with.output }}"
    - --font
    - "{{ with.font | default('Noto Sans CJK SC') }}"
    - --fonts-dir
    - "{{ with.fonts_dir | default('') }}"
    - --font-size
    - "{{ with.font_size | default(42) }}"
    - --margin-v
    - "{{ with.margin_v | default(80) }}"
    - --play-res
    - "{{ with.play_res | default('auto') }}"
    - --crf
    - "{{ with.crf | default(20) }}"
    - --preset
    - "{{ with.preset | default('medium') }}"
    - --ffmpeg-path
    - "{{ with.ffmpeg_path | default('ffmpeg') }}"
    - --ffprobe-path
    - "{{ with.ffprobe_path | default('ffprobe') }}"
  parse: text
  timeout: 600
---

# subtitle-burner

Burns an SRT subtitle stream into an MP4. The video is re-encoded
(H.264 + faststart), the audio is copied untouched. libass renders the
text per ASS-style override flags, so Chinese / Japanese / Korean
characters render through the managed Noto Sans CJK font. The font directory
is supplied through ``OPENSQUILLA_MEDIA_FONTS_DIR`` by the managed toolchain.
An empty/whitespace-only SRT is a valid no-subtitle request: the input video is
probed, copied to the requested output, and reported as
`SUBTITLES_SKIPPED: empty` without invoking libass.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `input` | yes | — | Source MP4 path. |
| `subtitles` | yes | — | `.srt` path (UTF-8). |
| `output` | yes | — | Output MP4 path. Parent dir created if missing. |
| `font` | no | `Noto Sans CJK SC` | One libass `FontName`; comma-separated names are not a fallback chain. |
| `fonts_dir` | no | managed environment | Directory containing subtitle fonts, normally supplied by OpenSquilla. |
| `font_size` | no | `42` | Font size. When `play_res=auto` this is in source-video pixels. |
| `margin_v` | no | `80` | Bottom margin in source-video pixels (because `play_res=auto` sets PlayRes to the input W×H). |
| `play_res` | no | `auto` | `auto` probes the input MP4 for resolution; or pass `WxH` like `720x1280`. Setting this makes FontSize/MarginV act in source pixels rather than libass's 384×288 default. |
| `crf` | no | `20` | x264 CRF (0-51, lower = better quality). |
| `preset` | no | `medium` | x264 preset. |

## Output

Prints the absolute path of the subtitled MP4 on stdout. Empty SRT input first
prints `SUBTITLES_SKIPPED: empty`. Both paths stage the result in the output
directory, require ffprobe to confirm a decodable positive-duration video
stream, and atomically replace the destination only after validation. Non-zero
exit on any encoding, copy, probe, or output-installation failure; stderr tails
the last 2.5 KB of the encoder log for diagnosis.

## Dependencies

- ffmpeg ≥ 5.0 with libass, libx264, AAC, xfade, and zoompan support.
- ffprobe from the matching ffmpeg distribution.
- Noto Sans CJK Regular (managed by OpenSquilla; OFL-1.1).
- Python 3.8+.

The script auto-locates ffmpeg via PATH; on Windows it falls back to
the winget Gyan.FFmpeg / scoop / chocolatey install paths if PATH
inheritance failed (matches the resolution logic in `video-merger` and
`video-still-animator`).

## Path-escaping notes

ffmpeg's `subtitles=` filter is picky on Windows:
- Drive-letter colons (`C:/…`) must be backslash-escaped (`C\:/…`).
- The path uses forward slashes regardless of host OS.
- Single quotes inside the path get backslash-escaped.

The script applies these rules so callers don't have to.

## Style chain

The `force_style` defaults render white text with a 2-px black outline
on a transparent background (`BorderStyle=3`), bottom-centred,
80 px above the frame edge. Override any of the `--*` flags via
`with.*` if you want a different look.
