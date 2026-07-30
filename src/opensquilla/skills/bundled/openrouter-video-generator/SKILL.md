---
name: openrouter-video-generator
description: "Generate or declare a configured OpenRouter video asset for AwesomeWebpageMetaSkill. Use only when the meta skill needs a local webpage video and the provider, model, API key, and output directory come from config."
description_zh: "为AwesomeWebpageMetaSkill生成或声明一个配置好的OpenRouter视频资产。仅当元技能需要本地网页视频，且提供商、模型、API密钥和输出目录均来自配置时使用。"
homepage: ""
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-write, filesystem-read, filesystem-write]
    requires:
      env: []
      config:
        - awesome_webpage.openrouter.api_key
        - awesome_webpage.openrouter.api_key_env
        - awesome_webpage.openrouter.base_url
        - awesome_webpage.openrouter.models.video_generation
        - awesome_webpage.output_dir
entrypoint:
  command: python {baseDir}/scripts/openrouter_video.py
  args:
    - --model
    - "{{ with.model | default('bytedance/seedance-2.0-fast') }}"
    - --base-url
    - "{{ with.base_url | default('https://openrouter.ai/api/v1') }}"
    - --api-key-env
    - "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}"
    - --output-dir
    - "{{ with.output_dir }}"
    - --filename
    - "{{ with.filename | default('intro.mp4') }}"
    - --duration
    - "{{ with.duration | default(10) }}"
    - --aspect-ratio
    - "{{ with.aspect_ratio | default('16:9') }}"
  env:
    "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}": "{{ with.api_key | default('') }}"
  stdin: "{{ with.prompt | default(inputs.user_message) }}"
  parse: text
  timeout: 420
---

# OpenRouter Video Generator

Create a short browser-playable video asset for `AwesomeWebpageMetaSkill`.
This is an adapter around the configured OpenRouter video model, not a place to
choose providers or invent model ids.

## Meta-Skill Entrypoint

Meta-skills should run this skill as `skill_exec`. The entrypoint is a
deterministic Python adapter around OpenRouter's async video endpoint; it uses
an explicit `with.api_key` value by injecting it into the configured
`with.api_key_env` child process environment variable, writes the MP4 under the supplied
output directory, and prints either `VIDEO_READY:` or a single failure label.
Do not spawn an LLM sub-agent just to generate video.

## Contract

- Read provider settings from `config.awesome_webpage.openrouter`.
- Resolve the API key from `awesome_webpage.openrouter.api_key`; if empty, use
  the environment variable named by `awesome_webpage.openrouter.api_key_env`.
- Use only `awesome_webpage.openrouter.models.video_generation` as the model.
- Use only `awesome_webpage.output_dir` as the output root.
- Save generated files under `project/assets/video/`.
- Return a media manifest with local path, MIME type, duration if known,
  provenance, and replacement notes.

## OpenRouter Video API Contract (hard rule)

OpenRouter video models are exposed through an **async job endpoint**, not
the chat-completions endpoint. Hitting `/chat/completions` with a video
model id returns HTTP 500 and burns the full per-attempt budget. Do not
attempt it.

- **Submit**: `POST {base_url}/videos` with JSON body
  `{"model": "<video_generation>", "prompt": "<text>", "duration": <int>}`.
  Optional fields: `resolution`, `aspect_ratio`, `frame_images`,
  `input_references`, `provider`, `callback_url`. Do NOT send `messages` or
  `modalities` — those are chat-only and will be rejected.
- **Response** (immediate): `{"id": "...", "polling_url": "...",
  "status": "pending"}`.
- **Poll**: GET the `polling_url` with the same `Authorization: Bearer ...`
  header every 8-15 seconds until `status` becomes one of
  `completed | failed | cancelled | expired`. Cap the loop at ~5 minutes per
  clip; treat anything beyond that as `VIDEO_GENERATION_FAILED`.
- **Download** (only on `completed`): take the first URL from
  `unsigned_urls`, GET it with the same bearer token, and save the body to
  `<output_dir>/project/assets/video/<slug>.mp4`. Set MIME to `video/mp4`.
- **Terminal-failure statuses** map to `VIDEO_GENERATION_FAILED`; include
  the job `id` and a replacement-slot path so the page can render a clean
  placeholder.

Typical successful job completes in 60-120 s for short clips; do not abort
before that window.

## Failure Labels

Return one of these labels instead of silently skipping video:

- `VIDEO_CONFIG_NEEDED`: model, API key, base URL, or output directory is
  missing.
- `VIDEO_MODEL_UNSUPPORTED`: the configured model cannot return a local
  browser-playable asset in this environment.
- `VIDEO_GENERATION_FAILED`: the provider call failed after a concrete attempt.

When returning a failure label, also return a replacement slot such as
`project/assets/video/replace-with-topic-intro.mp4` and enough prompt/context
for a later repair pass.

## Output Requirements

- Prefer `.mp4` with `video/mp4`; `.webm` is acceptable when browser playable.
- Keep clips short, usually 8-20 seconds.
- Do not use remote embeds unless config explicitly allows them.
- Do not hardcode OpenRouter model names, API keys, or output directories.

### On success: `VIDEO_READY` manifest line (required)

After every successful download, end your reply with one single-line JSON
record per file so `AwesomeWebpageMetaSkill` can collect and bind the asset:

```
VIDEO_READY: {"local_path": "project/assets/video/<slug>.mp4", "mime": "video/mp4", "duration_s": <int_or_null>, "resolution": "<WxH_or_null>", "prompt_preview": "<first 80 chars>"}
```

- One `VIDEO_READY:` line per video file. No trailing prose on that line.
- `local_path` MUST be the relative path `project/assets/video/...`. Do NOT
  emit an absolute path here.
- On failure, emit one of `VIDEO_CONFIG_NEEDED`, `VIDEO_MODEL_UNSUPPORTED`, or
  `VIDEO_GENERATION_FAILED` as a single-line label with the replacement-slot
  path so the page can render a placeholder.
