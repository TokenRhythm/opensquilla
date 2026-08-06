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
| `max_retries` | no | `0` | Compatibility budget used only for a failure proven to occur before a paid submit. Provider responses and ambiguous transport failures always stop. |
| `fallback_model` | no | `""` | Compatibility fallback used only after a proven safe pre-submit failure. It is never selected in response to a provider result or ambiguous paid POST. |
| `placeholder_on_fail` | no | `no` | `yes` / `no`. When every model refuses, write a 720x1280 solid-colour PNG with a "Scene placeholder" label so a downstream merge step still has a file in this slot. |

To pass an input image for edit mode, invoke the script directly with
`--input-image PATH`. The meta-skill engine does not route input images
through `with:` by convention; for edit workflows call the script.

## Auth

API-key resolution order (first hit wins):
1. `--api-key` CLI argument (rarely used; meta-skills don't pass it)
2. The parent-injected atomic connection
   `OPENSQUILLA_META_CAPABILITY_PROVIDER`,
   `OPENSQUILLA_META_CAPABILITY_API_KEY`, and
   `OPENSQUILLA_META_CAPABILITY_BASE_URL`. An optional
   `OPENSQUILLA_META_CAPABILITY_PROXY` applies only to requests to that
   matching provider API. These internal, volatile values are scoped to this
   bundled skill and are not written to argv, run inputs, or transcripts.
3. `OPENSQUILLA_META_OPENROUTER_API_KEY`, retained for older parent runtimes
   and bound only to OpenRouter's official API origin.
4. `OPENROUTER_API_KEY` environment variable for direct CLI use, also bound
   only to OpenRouter's official API origin.

The parent-injected generic key is accepted only when its provider and base URL
are present as one tuple. `--base-url` may change the path for a parent or
canonical credential but cannot change its scheme, hostname, or effective
port. To intentionally use a different API origin from the direct CLI, pass
both `--api-key` and `--base-url`. Authenticated requests reject URL userinfo,
queries, fragments, malformed ports, and redirects before a key can be sent.

The child script never discovers or parses `opensquilla.toml` from its current
working directory and never lets a workspace choose an arbitrary
`llm.api_key_env`. Configure the active Gateway normally; the parent runtime
performs that resolution before launching this bundled subprocess.

No Google Gemini key needed — OpenRouter routes the request to the
Gemini image model on the user's behalf.

When a parent-resolved profile-pool credential receives an authentication,
credit, or rate-limit failure, OpenSquilla parks that key for the next
explicitly authorized run. It never repeats the current paid generation
automatically.

## Output

Prints the absolute path of the saved PNG, then an
`IMAGE_GENERATION_RECEIPT: {...}` line on stdout. A matching sanitized
receipt is saved as `<filename>.receipt.json`; it records provider,
model, provider request id when available, and whether the file is a
real model result or a local placeholder. It never contains the API key
or prompt. Provider image MIME, decoded format, dimensions, and payload
integrity are verified and supported formats are normalized to PNG before a
generated receipt is written. Consumers must not report a placeholder receipt as a real
image-generation success. A provider result without a request id is
marked `generated_unverified` and likewise cannot satisfy verified E2E
provenance. Non-zero exit on any hard error; stderr carries the
diagnostic.

Image generation is a paid, non-idempotent operation. The script never submits
a second paid request automatically after any provider response, timeout, lost
connection, malformed response, policy refusal, or other ambiguous outcome.
The retry/fallback inputs remain accepted for compatibility, but can advance
only from an explicitly classified pre-submit failure.

## Cost / latency

- 1K ~ 4-8s
- 2K ~ 8-15s
- 4K ~ 20-40s
- Use 1K for draft, 4K only when the prompt is locked.

## Common failures

- `no OpenRouter API key found` → for direct CLI use, set
  `OPENROUTER_API_KEY` or pass `--api-key`; for a meta-skill run, configure the
  Gateway's OpenRouter provider connection and retry from that Gateway.
- `OpenRouter returned no image` → the model rejected the prompt
  (content moderation or unsupported request). Rewrite prompt; check
  IP-safety rules in `ai-video-script`.
- `OpenRouter HTTP 402 / 429` → out of credits / rate-limited.
