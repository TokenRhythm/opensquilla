---
name: video-still-animator
description: "Turn a single still image (PNG/JPG) into a short MP4 with a slow Ken-Burns zoom and a silent audio track. Pure ffmpeg wrapper. Designed as the on_failure substitute for AI video-gen steps that get blocked by content moderation: when seedance refuses, this skill emits a valid replacement clip from the already-generated still so a downstream merge can still produce a complete deliverable."
description_zh: "将单张静态图片（PNG/JPG）转为带缓慢Ken-Burns缩放和静音音轨的短MP4。纯ffmpeg封装。设计为AI视频生成步骤因内容审核受阻时的on_failure替代：当seedance拒绝时，本技能从已生成的静图产出有效的替代片段，使下游合并仍能产出完整交付物。"
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
  command: python {baseDir}/scripts/animate.py
  args:
    - --input
    - "{{ with.input_image }}"
    - --output
    - "{{ with.output_path }}"
    - --duration
    - "{{ with.duration | default(5) }}"
    - --width
    - "{{ with.width | default(720) }}"
    - --height
    - "{{ with.height | default(1280) }}"
    - --fps
    - "{{ with.fps | default(24) }}"
    - --zoom-rate
    - "{{ with.zoom_rate | default(0.0015) }}"
  parse: text
  timeout: 120
---

# video-still-animator — Ken-Burns fallback clip from a still

Produces a short MP4 from a single PNG/JPG using ffmpeg's `zoompan`
filter, so a downstream merge step still has a clip for every shot when
an upstream AI video generation step gets blocked.

## Why this skill exists

`seedance-2-prompt` (and any platform-moderated video model) can refuse
individual requests for reasons that have nothing to do with the
prompt's narrative quality:

- input photo contains a recognisable real person face
- output audio gets flagged by the safety classifier
- the moderation model's similarity threshold drifts day to day

In `meta-short-drama` we always have a fresh PNG on disk before the
seedance call runs, so the cheapest "the show must go on" fallback is to
turn that PNG into a Ken-Burns clip and feed it to the merger. The clip
won't have the camera motion the prompt asked for, but the character,
framing, and timing will still match the script.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `input_image` | yes | — | PNG or JPG path. |
| `output_path` | yes | — | Output `.mp4` path. Parent dir created if missing. |
| `duration` | no | `5` | Clip length in seconds. |
| `width` | no | `720` | Output width. Match the merge step's pipeline. |
| `height` | no | `1280` | Output height. 720x1280 = 9:16. |
| `fps` | no | `24` | Output frame rate. |
| `zoom_rate` | no | `0.0015` | Per-frame zoom delta; 0.0015 over 5s ≈ 1.18× final zoom. |

## Output specs

- H.264 video at the requested resolution + fps
- AAC silent audio track (so merge steps that demux/remux audio do not
  trip on a missing stream)
- `+faststart` MP4 (web playback friendly)

## Dependencies

- ffmpeg ≥ 5.0 on PATH (or pass `--ffmpeg-path` directly).

On Windows the script also probes the winget Gyan.FFmpeg install path,
Scoop, Chocolatey, and `C:\Program Files\ffmpeg\bin` before giving up.

## Use as `on_failure` substitute

In a meta-skill DAG, hang it off the real video step:

```yaml
- id: shot1_video
  kind: skill_exec
  skill: seedance-2-prompt
  on_failure: shot1_video_fallback
  with: { ... }

- id: shot1_video_fallback
  kind: skill_exec
  skill: video-still-animator
  with:
    input_image: "{{ inputs.workspace_dir }}/.../1_shot.png"
    output_path: "{{ inputs.workspace_dir }}/.../1_shot.mp4"
```

The fallback must be a standalone step (no `depends_on`, no own
`on_failure`) per the meta-skill engine rules. The PNG must already
exist on disk by the time the parent step fails — that's true here
because the image step always runs before the video step.

## Limits

- Static framing only. The zoom is uniform centre-anchored; no pan, no
  rotation, no parallax. For richer fallback motion, generate a fresh
  set of stills and call this multiple times.
- No real audio. The intent is a placeholder that survives the merge;
  add a real soundtrack downstream if needed.
- ffmpeg version drift: very old ffmpeg (<4) may not accept the exact
  `zoompan` filter syntax; install ≥ 5.
