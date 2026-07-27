---
name: title-card-image
description: "Render a static title / ending card PNG with Pillow. Centered headline + optional subtitle on a solid-colour background. CJK-friendly font fallback (Microsoft YaHei → SimHei → Songti → Noto CJK → bitmap). Pure deterministic, no LLM, no network. Used by meta-short-drama for opening and closing cards."
description_zh: "用Pillow渲染静态片头/片尾卡PNG。纯色背景上居中的标题及可选副标题。对CJK友好的字体回退（Microsoft YaHei → SimHei → Songti → Noto CJK → 位图）。纯确定性，无LLM、无网络。由meta-short-drama用于片头和片尾卡。"
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
  command: python {baseDir}/scripts/render.py
  args:
    - --text
    - "{{ with.text }}"
    - --output
    - "{{ with.output }}"
    - --subtitle
    - "{{ with.subtitle | default('') }}"
    - --background
    - "{{ with.background | default('#101018') }}"
    - --text-color
    - "{{ with.text_color | default('#ffffff') }}"
    - --font-size
    - "{{ with.font_size | default(96) }}"
    - --subtitle-size
    - "{{ with.subtitle_size | default(36) }}"
    - --width
    - "{{ with.width | default(720) }}"
    - --height
    - "{{ with.height | default(1280) }}"
  parse: text
  timeout: 30
---

# title-card-image

Renders a centered-text PNG suitable for a cover / ending card before
animating into a clip with `video-still-animator`.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `text` | yes | — | Headline text. Auto-wraps at ~10 chars per line for CJK. |
| `output` | yes | — | Output `.png` path. |
| `subtitle` | no | `""` | Smaller line beneath the headline. |
| `background` | no | `#101018` | Hex color `#RRGGBB`. |
| `text_color` | no | `#ffffff` | Headline color. |
| `font_size` | no | `96` | Headline font size in pixels. |
| `subtitle_size` | no | `36` | Subtitle font size in pixels. |
| `width` | no | `720` | Output width in pixels. Match the merge pipeline. |
| `height` | no | `1280` | Output height. 720x1280 = 9:16. |

## Output

Prints the absolute path of the written PNG on stdout. The PNG is RGB
(no alpha), JPEG-quality-equivalent file ~30-80 KB depending on text
length.

## Font fallback

Tries `--font` if explicit, else walks a CJK-aware list of platform
defaults (Microsoft YaHei / SimHei on Windows, PingFang on macOS, Noto
CJK / WenQuanYi on Linux). If nothing loads, falls back to Pillow's
bundled bitmap font — CJK characters render as squares ("tofu") in that
worst-case but the program never crashes.

## Limits

- No alpha / transparency.
- No rich text styling (italic / drop-shadow / gradient). For richer
  cards, generate a real image via `nano-banana-pro` instead.
- Headline wrap is character-count-based for CJK and whitespace-based
  for ASCII; mixed strings break at the CJK character count.
