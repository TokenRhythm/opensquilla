---
name: srt-from-script
description: "Build an SRT subtitle file from a 3-shot short-drama script (ai-video-script OUTPUT FORMAT). Reads each SHOT_N block's DURATION_S + VOICEOVER, emits cumulative-timestamped SRT cues. Pure text-processing, no LLM, no network. Used by meta-short-drama between merge and the final subtitle-burn step."
description_zh: "从3镜头短剧脚本（ai-video-script输出格式）构建SRT字幕文件。读取每个SHOT_N块的DURATION_S和VOICEOVER，输出带累计时间戳的SRT字幕。纯文本处理，无LLM、无网络。由meta-short-drama在合并与最终字幕烧录步骤之间使用。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: low
    capabilities: [filesystem-write]
    requires:
      anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/build_srt.py
  args:
    - --output
    - "{{ with.output_path }}"
    - --gap-ms
    - "{{ with.gap_ms | default(200) }}"
    - --leading-offset-ms
    - "{{ with.leading_offset_ms | default(0) }}"
  stdin: "{{ with.script }}"
  parse: text
  timeout: 30
---

# srt-from-script

Parses an ai-video-script 3-shot script and writes an SRT subtitle file
whose cues track the script's VOICEOVER per shot, time-coded with
cumulative shot durations.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `script` | yes | — | Full script text (the entire OUTPUT FORMAT block, including OVERVIEW + SHOT_1..N). Passed via stdin so the orchestrator does not need to write a temp file. |
| `output_path` | yes | — | Output `.srt` path. Parent dir created if missing. |
| `gap_ms` | no | `200` | Tail pad subtracted from each cue's end so the subtitle vanishes ~200 ms before the next shot starts — avoids cuts clipping mid-character. |
| `leading_offset_ms` | no | `0` | Shifts every cue forward by this many ms. Set to the cover/intro clip duration when the merged video prepends a title card before SHOT_1. |

## Parsing rules

- A shot with `VOICEOVER: none` or empty contributes no SRT cue but its
  `DURATION_S` still advances the timeline cursor.
- Cue language follows the script verbatim. Chinese stays Chinese,
  English stays English — no translation.
- Cumulative timestamps: SHOT_1 starts at 00:00:00,000; SHOT_2 starts at
  SHOT_1.duration; etc.
- End time of each cue = next-shot start − `gap_ms`, clamped to ≥ 800 ms
  after start so very short voiceover lines remain readable.

## Output

Prints the absolute path of the written `.srt` on stdout.
The file is UTF-8 encoded so CJK voiceover lines survive when ffmpeg
reads them via the `subtitles=` filter.

## Limits

- Assumes the script follows ai-video-script's strict OUTPUT FORMAT
  (`=== SHOT_N ===` blocks with `DURATION_S:` and `VOICEOVER:` fields).
  Drift away from that format → zero cues, exit 1.
- 3-5 shots tested. Larger shot counts work but timestamps grow.
