---
name: video-merger
description: "Concatenate a directory of numbered MP4 segments (1_*.mp4, 2_*.mp4, ...) into one MP4 with optional fade transitions, unified resolution/fps/codec. Pure ffmpeg wrapper, no LLM. Trigger when a workflow has produced several short clips that need stitching into a final reel."
description_zh: "将一个目录中编号的MP4片段（1_*.mp4, 2_*.mp4, ...）拼接为一个MP4，可选淡入淡出转场，统一分辨率/帧率/编码。纯ffmpeg封装，无LLM。当工作流已产出多个需拼成成片的短片段时触发。"
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/machunlin/video-merger
  upstream_version: "1.1.0"
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: medium
    capabilities: [filesystem-write, process-control]
    requires:
      bins: ["ffmpeg", "ffprobe"]
      anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/merge.py
  args:
    - --input
    - "{{ with.input_dir }}"
    - --output
    - "{{ with.output_path }}"
    - --mode
    - "{{ with.mode | default('full') }}"
    - --transition
    - "{{ with.transition | default(0.5) }}"
    - --fps
    - "{{ with.fps | default(24) }}"
    - --crf
    - "{{ with.crf | default(22) }}"
    - --preset
    - "{{ with.preset | default('medium') }}"
  parse: text
  timeout: 600
---

# video-merger — numbered-segment MP4 concatenator

Combines `1_*.mp4`, `2_*.mp4`, ... in numeric order into one MP4 (or
multiple ~60s chunks), with optional fade transitions, codec/resolution/
fps normalisation.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `input_dir` | yes | — | Directory containing `\d+_*.mp4` segments. |
| `output_path` | yes | — | `.mp4` path (full mode) or directory (chunk mode). |
| `mode` | no | `full` | `full` or `chunk`. |
| `transition` | no | `0.5` | Fade duration in seconds. `0` disables. |
| `fps` | no | `24` | Target frame rate. |
| `crf` | no | `22` | x264 CRF (0-51, lower = better). |
| `preset` | no | `medium` | x264 preset. |

For chunk mode, pass `chunk_duration` directly to the script — the meta
engine entrypoint above does not template it; use a direct shell call
when chunking is needed.

## Dependencies

- `ffmpeg` ≥ 5.0
- `ffprobe`
- Python 3.8+
- No Python packages required beyond stdlib.

Install hints (or just run the bundled installer):

| OS | One-liner installer | Manual |
|---|---|---|
| Windows (PowerShell) | `pwsh -ExecutionPolicy Bypass -File install.ps1` | `winget install Gyan.FFmpeg` / `choco install ffmpeg` / `scoop install ffmpeg` |
| macOS | `bash install.sh` | `brew install ffmpeg` |
| Debian/Ubuntu | `bash install.sh` | `sudo apt install ffmpeg` |

The Windows installer (`install.ps1`) defaults to winget but accepts
`$env:OPENSQUILLA_FFMPEG_INSTALLER="choco"|"scoop"|"skip"` to switch
backends. It prints the absolute `ffmpeg`/`ffprobe` paths after install so
you can pass them to `merge.py` via `--ffmpeg-path` / `--ffprobe-path`
when subprocess PATH inheritance is unreliable.

## Filename ordering contract

The script picks files matching `^\d+_.*\.mp4$` and sorts by the leading
integer. Producers must save segments as `1_shot.mp4`, `2_shot.mp4`,
`3_shot.mp4`, etc. The `meta-short-drama` workflow already follows this.

## Output

Stdout: progress lines and the final output path. Non-zero exit on
ffmpeg failure.

## Failure modes

- `未找到ffmpeg` → install ffmpeg, ensure `ffmpeg` on PATH or pass
  `--ffmpeg-path` directly.
- `未找到符合命名规则...的MP4文件` → ensure segments are `\d+_*.mp4`.
- Mixed resolutions / fps: handled automatically when `--resolution`
  is set; otherwise ffmpeg preserves originals (no normalisation).
