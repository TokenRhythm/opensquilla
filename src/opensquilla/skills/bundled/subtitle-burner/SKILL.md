---
name: subtitle-burner
description: "Burn an SRT subtitle file into an MP4 via ffmpeg's subtitles filter (libass). Single-pass re-encode of video; audio copied as-is. CJK-friendly font fallback chain (Microsoft YaHei → SimHei → Arial Unicode MS → Arial). Used by meta-short-drama as the final subtitling step after merge."
description_zh: "通过ffmpeg的subtitles滤镜（libass）将SRT字幕烧录进MP4。单遍重编码视频，音频原样复制。对CJK友好的字体回退链（Microsoft YaHei → SimHei → Arial Unicode MS → Arial）。由meta-short-drama在合并后作为最终字幕步骤使用。"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [filesystem-write, process-control]
    requires:
      bins: ["ffmpeg"]
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
    - "{{ with.font | default('Microsoft YaHei,SimHei,Arial Unicode MS,Arial') }}"
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
  parse: text
  timeout: 600
---

# subtitle-burner

Burns an SRT subtitle stream into an MP4. The video is re-encoded
(H.264 + faststart), the audio is copied untouched. libass renders the
text per ASS-style override flags, so Chinese / Japanese / Korean
characters survive when the host has any of the listed fallback fonts
installed (on Windows, Microsoft YaHei and SimHei ship with the OS).

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `input` | yes | — | Source MP4 path. |
| `subtitles` | yes | — | `.srt` path (UTF-8). |
| `output` | yes | — | Output MP4 path. Parent dir created if missing. |
| `font` | no | `Microsoft YaHei,SimHei,Arial Unicode MS,Arial` | libass `FontName` fallback chain. First wins. |
| `font_size` | no | `42` | Font size. When `play_res=auto` this is in source-video pixels. |
| `margin_v` | no | `80` | Bottom margin in source-video pixels (because `play_res=auto` sets PlayRes to the input W×H). |
| `play_res` | no | `auto` | `auto` probes the input MP4 for resolution; or pass `WxH` like `720x1280`. Setting this makes FontSize/MarginV act in source pixels rather than libass's 384×288 default. |
| `crf` | no | `20` | x264 CRF (0-51, lower = better quality). |
| `preset` | no | `medium` | x264 preset. |

## Output

Prints the absolute path of the subtitled MP4 on stdout. Non-zero exit
on any ffmpeg failure; stderr tails the last 2.5 KB of the encoder log
for diagnosis.

## Dependencies

- ffmpeg ≥ 5.0 (libass support is standard in any modern build).
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
