---
name: openrouter-video-generator
description: "Generate or declare an OpenRouter video asset for AwesomeWebpageMetaSkill using a parent-leased Provider Settings connection and configured non-secret model/output values."
description_zh: "使用父级租用的 Provider Settings 连接及已配置的非敏感模型/输出参数，为 AwesomeWebpageMetaSkill 生成或声明 OpenRouter 视频资产。"
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
        - awesome_webpage.openrouter.models.video_generation
        - awesome_webpage.output_dir
entrypoint:
  command: python {baseDir}/scripts/openrouter_video.py
  args:
    - --model
    - "{{ with.model | default('bytedance/seedance-2.0-fast') }}"
    - --output-dir
    - "{{ with.output_dir }}"
    - --filename
    - "{{ with.filename | default('intro.mp4') }}"
    - --duration
    - "{{ with.duration | default(10) }}"
    - --aspect-ratio
    - "{{ with.aspect_ratio | default('16:9') }}"
  env:
    OPENSQUILLA_META_CAPABILITY_LEASE_REQUIRED: "1"
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
deterministic Python adapter around OpenRouter's async video endpoint. During a
MetaSkill run it receives a short-lived connection from ordinary Provider
Settings in the child process only; the credential, endpoint, and proxy never
enter `with`, argv, the plan, or persisted run data. It writes the MP4 under
the supplied output directory and prints either `VIDEO_READY:` or a failure
label. Do not spawn an LLM sub-agent just to generate video.

## Contract

- Use the code-owned OpenRouter capability candidate and the volatile provider
  lease resolved from ordinary Provider Settings after explicit approval.
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

A missing/invalid required MetaSkill lease emits `VIDEO_CONFIG_NEEDED` and exits
78 before submit. Failures after a concrete submit stay an exit-0 degradation
so downstream packaging can bind a replacement slot without automatic replay.

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
