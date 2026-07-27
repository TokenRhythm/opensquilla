---
name: nano-banana-pro
description: "Generate or edit a single image via OpenRouter (google/gemini-3.1-flash-image-preview by default). Accepts a text prompt and optional --input-image for image-to-image editing. Trigger when the user asks for an AI image, illustration, concept art, product render, or wants to modify an existing image."
description_zh: "通过OpenRouter（默认 google/gemini-3.1-flash-image-preview）生成或编辑单张图片。接受文本提示词及可选的 --input-image 进行图生图编辑。当用户需要AI图像、插画、概念图、产品渲染或修改现有图片时触发。"
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/steipete/nano-banana-pro
  upstream_version: "1.0.1"
  maintained_by: OpenSquilla
  modifications: "Rewired from Google Gemini SDK to OpenRouter /v1/chat/completions; pure-stdlib HTTP client."
metadata:
  opensquilla:
    risk: medium
    capabilities: [network-read, filesystem-write]
    requires:
      anyBins: ["python", "python3"]
      envAny: ["OPENROUTER_API_KEY"]
entrypoint:
  command: python {baseDir}/scripts/generate_image.py
  args:
    - --prompt
    - "{{ with.prompt }}"
    - --filename
    - "{{ with.filename }}"
    - --aspect-ratio
    - "{{ with.aspect_ratio | default('1:1') }}"
    - --image-size
    - "{{ with.image_size | default('1K') }}"
    - --model
    - "{{ with.model | default('google/gemini-3.1-flash-image-preview') }}"
    - --max-retries
    - "{{ with.max_retries | default(0) }}"
    - --fallback-model
    - "{{ with.fallback_model | default('') }}"
    - --placeholder-on-fail
    - "{{ with.placeholder_on_fail | default('no') }}"
  parse: text
  timeout: 600
---

# nano-banana-pro — single-image generator via OpenRouter

Generates one PNG from a text prompt (optionally seeded with an input
image for editing). Used by `meta-short-drama` for per-shot first-frame
generation, but standalone for any single-image request.

## Inputs (`with:`)

| key | required | default | notes |
|---|---|---|---|
| `prompt` | yes | — | Plain English prompt. Append `--ar 9:16` etc. as text. |
| `filename` | yes | — | Output path. Relative resolves against process cwd. |
| `aspect_ratio` | no | `1:1` | One of `1:1`, `3:2`, `2:3`, `4:3`, `3:4`, `16:9`, `9:16`. |
| `image_size` | no | `1K` | `1K`, `2K`, `4K`. Higher = slower + costlier. |
| `model` | no | `google/gemini-3.1-flash-image-preview` | Any OpenRouter image-capable model. |
| `max_retries` | no | `0` | Extra retries on the primary `model` before moving on to `fallback_model`. |
| `fallback_model` | no | `""` | Tried ONCE after the primary exhausts retries. Empty disables it. Common pick: `google/gemini-3-pro-image-preview`. |
| `placeholder_on_fail` | no | `no` | `yes` / `no`. When every model refuses, write a 720x1280 solid-colour PNG with a "Scene placeholder" label so a downstream merge step still has a file in this slot. |

To pass an input image for edit mode, invoke the script directly with
`--input-image PATH`. The meta-skill engine does not route input images
through `with:` by convention; for edit workflows call the script.

## Auth

API-key resolution order (first hit wins):
1. `--api-key` CLI argument (rarely used; meta-skills don't pass it)
2. `OPENROUTER_API_KEY` environment variable (gateway injects from `.env`)
3. `OPENSQUILLA_LLM_API_KEY` environment variable, only when the
   effective OpenSquilla LLM provider resolves to `openrouter`.
4. `llm.api_key` or `llm.api_key_env` from the selected OpenSquilla TOML
   config file. Config discovery matches `GatewayConfig.load`: explicit
   `OPENSQUILLA_GATEWAY_CONFIG_PATH` first; otherwise
   `./opensquilla.toml`, then `default_opensquilla_home()/config.toml`.
   `OPENSQUILLA_STATE_DIR` changes `default_opensquilla_home()`, so a
   state-dir profile does not fall through to `~/.opensquilla`.
   Config-file credentials are consumed only when the selected config's
   `llm.provider` is `openrouter` or omitted.

No Google Gemini key needed — OpenRouter routes the request to the
Gemini image model on the user's behalf.

## Output

Prints the absolute path of the saved PNG on stdout. Non-zero exit on
any error; stderr carries the diagnostic.

## Cost / latency

- 1K ~ 4-8s
- 2K ~ 8-15s
- 4K ~ 20-40s
- Use 1K for draft, 4K only when the prompt is locked.

## Common failures

- `no OpenRouter API key found` → set `OPENROUTER_API_KEY`, pass
  `--api-key`, or configure `[llm] provider = "openrouter"` with
  `api_key` / `api_key_env` in the selected OpenSquilla config.
- `OpenRouter returned no image` → the model rejected the prompt
  (content moderation or unsupported request). Rewrite prompt; check
  IP-safety rules in `ai-video-script`.
- `OpenRouter HTTP 402 / 429` → out of credits / rate-limited.
