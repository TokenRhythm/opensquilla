---
name: audio-cog
description: "OpenSquilla-compatible audio generation adapter for webpage audio requests. Prefer OpenRouter config/API key in OpenSquilla; preserve the upstream CellCog workflow only as optional ClawHub provenance."
description_zh: "与OpenSquilla兼容的音频生成适配器，用于网页音频请求。优先使用OpenSquilla中的OpenRouter配置/API密钥；仅将上游CellCog工作流作为可选的ClawHub来源保留。"
metadata:
  openclaw:
    emoji: "🎵"
    os: [darwin, linux, windows]
    requires:
      bins: [python3]
      env: [CELLCOG_API_KEY]
  opensquilla:
    risk: medium
    capabilities: [network-write, filesystem-write]
    requires:
      bins: [python3]
      env: []
      config:
        - awesome_webpage.openrouter.models.audio_generation
        - awesome_webpage.output_dir
author: CellCog
homepage: https://cellcog.ai
dependencies: [cellcog]
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/skills/audio-cog
  maintained_by: OpenSquilla
entrypoint:
  command: python {baseDir}/scripts/openrouter_audio.py
  args:
    - --model
    - "{{ with.model | default('openai/gpt-audio-mini') }}"
    - --output-dir
    - "{{ with.output_dir }}"
    - --filename
    - "{{ with.filename | default('narration.wav') }}"
    - --voice
    - "{{ with.voice | default('cedar') }}"
  env:
    OPENSQUILLA_META_CAPABILITY_LEASE_REQUIRED: "1"
  stdin: "{{ with.payload | default(with.prompt | default(inputs.user_message)) }}"
  parse: text
  timeout: 240
---
# Audio Cog - AI Audio Generation Powered by CellCog

Create professional audio with AI — voiceovers, music, sound effects, and personalized avatar voices.

## Meta-Skill Entrypoint

Meta-skills should run this skill as `skill_exec` when they need OpenRouter
audio. The entrypoint is a deterministic Python adapter. During MetaSkill
execution it receives a short-lived provider connection from ordinary Provider
Settings in the child process only; the credential and endpoint never enter
`with`, argv, the plan, or persisted run data. It calls the configured
OpenRouter audio model, writes a browser-playable WAV file under the supplied
output directory, and prints either `AUDIO_READY:` or a single failure label.
Do not spawn an LLM sub-agent just to generate audio.

Prefer JSON payload mode when the caller already has a narration script:

```json
{"script": "exact spoken narration text"}
```

In payload mode the adapter asks the audio model to speak exactly that
transcript and not add acknowledgements, titles, or setup text.

## OpenSquilla Compatibility Contract

When invoked from OpenSquilla, this skill is an adapter around the caller's
configured provider. Do not require `CELLCOG_API_KEY`, do not assume the
`cellcog` package is installed, and do not invent provider credentials.

For `AwesomeWebpageMetaSkill`:

- Use the code-owned OpenRouter capability candidate and the volatile provider
  lease resolved from ordinary Provider Settings after explicit approval.
- Use only `config.awesome_webpage.openrouter.models.audio_generation` for
  audio model selection.
- Save generated or processed files only under
  `config.awesome_webpage.output_dir/project/assets/audio`.
- If the OpenRouter key, audio model, or output directory is missing, return a
  concise `AUDIO_CONFIG_NEEDED` report listing the missing config keys.
- If the configured OpenRouter model cannot return a browser-playable audio
  file, return `AUDIO_MODEL_UNSUPPORTED` with the narration/script, desired
  duration, style, and target filename so the webpage can expose a clean
  replacement slot instead of failing the whole project.

### On success: `AUDIO_READY` manifest line (required)

After every successful save, end your reply with one single-line JSON record
per file so `AwesomeWebpageMetaSkill` can collect and bind the assets:

```
AUDIO_READY: {"local_path": "project/assets/audio/<slug>.wav", "mime": "audio/wav", "duration_s": <int_or_null>, "voice": "<voice>", "script_preview": "<first 80 chars>"}
```

- One `AUDIO_READY:` line per audio file. No trailing prose on that line.
- `local_path` MUST be the relative path `project/assets/audio/...`. Do NOT
  emit an absolute path here.
- On failure, emit one of `AUDIO_CONFIG_NEEDED`, `AUDIO_MODEL_UNSUPPORTED`, or
  `AUDIO_GENERATION_FAILED` as a single-line label with the replacement-slot
  path so the page can render a placeholder.

## OpenRouter Audio API Contract (hard rule for `openai/gpt-audio*`)

The default CellCog code-path is **wrong** for OpenSquilla and will fail.
OpenRouter routes `openai/gpt-audio` / `openai/gpt-audio-mini` through OpenAI's
audio-output mode, which has a strict request shape:

- `POST {base_url}/chat/completions` with body:
  ```
  {
    "model": "<audio_generation>",
    "stream": true,
    "modalities": ["text", "audio"],
    "audio": {"voice": "alloy", "format": "pcm16"},
    "messages": [...]
  }
  ```
- `stream: true` is REQUIRED. Non-streaming requests are rejected with
  HTTP 400 "Audio output requires stream: true".
- `audio.format` MUST be `pcm16` when streaming. `mp3`, `opus`, `flac`,
  `wav` are all rejected as "unsupported_value" — there is no alternative
  combo. Sending `format=mp3` (any stream setting) burns ~190 s of
  per-attempt timeout for nothing; do not try it.
- Read the SSE response, base64-decode each `delta.audio.data` chunk,
  concatenate the raw 24kHz mono signed-16-bit-little-endian PCM stream,
  then save it as a browser-playable WAV file.
- Final on-disk asset is `.wav`. Set MIME to `audio/wav` in the manifest.
- If the required MetaSkill provider lease is missing or invalid, emit
  `AUDIO_CONFIG_NEEDED` and exit 78 before any provider submission. Direct
  standalone CLI use may still read `OPENROUTER_API_KEY`; never fall back to
  `CELLCOG_API_KEY` or another provider.
- Provider/model failures after submission remain an exit-0 degradation with a
  structured replacement slot. They are never automatically replayed.

Upstream CellCog instructions are intentionally omitted from the executable
prompt body. OpenSquilla meta-skills use the entrypoint above; provenance is
kept in frontmatter for registry/audit purposes.
