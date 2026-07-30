---
name: seedance-2-prompt
description: "Render a single 3-15s video clip via Seedance 2.0. Supports two backends: OpenRouter (default, model bytedance/seedance-2.0) and the official Volcengine ARK / BytePlus ModelArk endpoint (model doubao-seedance-2-0-260128 / dreamina-seedance-2-0-260128). Accepts a structured English video prompt, optional first-frame image, and optional identity/style reference image. Trigger when the user asks for AI video clip generation, 分镜视频, seedance, or wants a short cinematic shot from a prompt + frame."
description_zh: "通过Seedance 2.0渲染单个3-15秒视频片段。支持两种后端：OpenRouter（默认，模型 bytedance/seedance-2.0）和官方火山引擎ARK / BytePlus ModelArk端点（模型 doubao-seedance-2-0-260128 / dreamina-seedance-2-0-260128）。接受结构化英文视频提示词、可选首帧图像和可选身份/风格参考图。当用户需要AI视频片段生成、分镜视频、seedance，或想根据提示词+画面生成短电影镜头时触发。"
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/dandysuper/seedance-2-prompt-engineering-skill
  upstream_version: "2.0.0"
  maintained_by: OpenSquilla
  modifications: "Added scripts/generate_video.py with dual-provider support: OpenRouter async /videos API and Volcengine ARK / BytePlus ModelArk /contents/generations/tasks. References kept from upstream."
metadata:
  opensquilla:
    risk: medium
    capabilities: [network-read, filesystem-write]
    requires:
      anyBins: ["python", "python3"]
      envAny: ["OPENROUTER_API_KEY", "ARK_API_KEY"]
entrypoint:
  command: python {baseDir}/scripts/generate_video.py
  args:
    - --prompt
    - "{{ with.prompt }}"
    - --filename
    - "{{ with.filename }}"
    - --provider
    - "{{ with.provider | default('openrouter') }}"
    - --aspect-ratio
    - "{{ with.aspect_ratio | default('9:16') }}"
    - --duration
    - "{{ with.duration | default(5) }}"
    - --resolution
    - "{{ with.resolution | default('720p') }}"
    - --model
    - "{{ with.model | default('') }}"
    - --input-image
    - "{{ with.input_image | default('') }}"
    - --input-reference
    - "{{ with.input_reference | default('') }}"
    - --input-reference
    - "{{ with.input_reference_2 | default('') }}"
    - --max-retries
    - "{{ with.max_retries | default(0) }}"
  parse: text
  timeout: 1500
---

# seedance-2-prompt — Seedance 2.0 video clip generator (dual backend)

Submits a Seedance 2.0 generation job and downloads the resulting MP4.
Two backends share one CLI, picked via `with.provider`:

| `with.provider` | Endpoint | Auth env | Default model |
|---|---|---|---|
| `openrouter` (default) | `https://openrouter.ai/api/v1/videos` | `OPENROUTER_API_KEY` | `bytedance/seedance-2.0` |
| `volcengine` (CN) | `https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks` | `ARK_API_KEY` (or `VOLC_ARK_API_KEY`) | `doubao-seedance-2-0-260128` |
| `byteplus` (intl) | `https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks` | `ARK_API_KEY` (or `BYTEPLUS_API_KEY`) | `dreamina-seedance-2-0-260128` |

Both flavours follow submit-then-poll, but differ in request shape
(OpenRouter uses a flat `prompt` field; ARK packs everything into a
`content[]` array), polling URL (OpenRouter returns `polling_url`; ARK
gets `id` and you construct `/contents/generations/tasks/{id}`), the
terminal-success status (`completed` vs `succeeded`), and where the
final MP4 URL sits (top-level `unsigned_urls[0]` vs `content.video_url`).
This script normalises both into a single Python contract.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `prompt` | yes | — | Structured English video prompt (use this skill's recipes). |
| `filename` | yes | — | Output `.mp4` path. |
| `provider` | no | `openrouter` | `openrouter`, `volcengine`, or `byteplus`. |
| `aspect_ratio` | no | `9:16` | `9:16`, `16:9`, `1:1`, `4:3`, `3:4`, `21:9`. |
| `duration` | no | `5` | Seconds, 3-15. Provider enforces 4-15 typically. |
| `resolution` | no | `720p` | `480p`, `720p`, `1080p`. Ignored by OpenRouter. |
| `model` | no | provider default | Override model id. Empty means use provider default. |
| `input_image` | no | `""` | Strict first-frame path. If set, video starts from this image. |
| `input_reference` | no | `""` | Primary soft identity/style anchor path. Used only when `input_image` is empty. Same anchor passed across shots locks the character. |
| `input_reference_2` | no | `""` | Optional second reference (e.g. per-shot scene composition). Forwarded as a second `--input-reference` so the underlying provider sees both. Empty strings are filtered out before the API call. |
| `max_retries` | no | `0` | Extra retries on transient submit/poll/download failures or non-success terminal status. `0` = single attempt; `2` = up to 3 total attempts with exponential backoff (2s, 4s, 8s capped at 15s). Set this on flows that fall back to a still-image animator on final failure. |

**`input_image` vs `input_reference`** — `input_image` becomes the literal
first frame. `input_reference` is a softer style + identity hint the
model uses without locking the frame. For multi-shot drama, pass the
same `input_reference` to every shot; pass `input_image` only when you
want a specific opening frame.

## Prompt rules (from upstream + OpenSquilla tightening)

1. **One major action per 3-5s segment.** Don't pack multiple motions.
2. **Identity continuity** — repeat the main character's full
   description in every shot's prompt.
3. **Specific over poetic** — `"a young woman in a red trench coat
   walks through rain-soaked neon streets"` >> `"a woman walking"`.
4. **Negative constraints inline** — append `no watermark, no logo,
   no subtitles, no on-screen text.`
5. **IP-safe** — invent original character/brand names.
6. **Aspect ratio explicit** — append `aspect_ratio: 9:16`.

See `references/recipes.md`, `references/modes-and-recipes.md`,
`references/camera-and-styles.md` for the upstream playbook.

## Auth

- `openrouter` provider API-key resolution order:
  1. `--api-key` CLI argument
  2. `OPENROUTER_API_KEY` env var
  3. `OPENSQUILLA_LLM_API_KEY` env var, only when the effective
     OpenSquilla LLM provider resolves to `openrouter`
  4. `llm.api_key` or `llm.api_key_env` from the selected OpenSquilla
     TOML config. Config discovery matches `GatewayConfig.load`:
     explicit `OPENSQUILLA_GATEWAY_CONFIG_PATH` first; otherwise
     `./opensquilla.toml`, then `default_opensquilla_home()/config.toml`.
     `OPENSQUILLA_STATE_DIR` changes `default_opensquilla_home()`, so a
     state-dir profile does not fall through to `~/.opensquilla`.
     Config-file credentials are consumed only when the selected config's
     `llm.provider` is `openrouter` or omitted.
- `volcengine` / `byteplus` provider reads `ARK_API_KEY` (with provider-
  specific fallbacks `VOLC_ARK_API_KEY` / `BYTEPLUS_API_KEY`). No
  config-file fallback for these — the OpenSquilla `[llm]` config
  describes the agent's selected LLM provider, not ARK / BytePlus video
  credentials.
- All three send the key as `Authorization: Bearer <key>`.
- For OpenRouter the same bearer is also added when downloading the
  resulting `unsigned_urls[0]`. Volcengine returns pre-signed object
  store URLs that reject extra headers, so the downloader strips
  Authorization for non-OpenRouter hosts.

## Output

Prints the absolute path of the saved `.mp4` on stdout. Non-zero
exit on any error; stderr carries the diagnostic.

## Cost / latency

- OpenRouter `bytedance/seedance-2.0` 5s @ 9:16 720p: ≈30-90s wall, ≈$0.76.
- Volcengine official 5s 1080p: ≈30-120s wall, ≈$0.93.
- Volcengine `doubao-seedance-2-0-fast-260128`: roughly half cost and faster.
- The returned `unsigned_urls` / `content.video_url` expire 24 hours
  after success on the Volcengine path — this script downloads them
  before that window so the local mp4 is durable.

## Multi-segment workflows (>15s)

Generate segments individually with `duration ≤ 15`, ending each on a
stable hand-off frame. Stitch with the `video-merger` skill. See
`references/modes-and-recipes.md` § "Multi-Segment Stitching".
