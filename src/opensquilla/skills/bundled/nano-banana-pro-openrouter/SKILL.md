---
name: nano-banana-pro-openrouter
description: "Deterministic OpenRouter image generation adapter for Nano Banana Pro / Gemini image models. Use as skill_exec when a meta-skill needs local image files and structured IMAGE_READY records without spawning an LLM agent."
description_zh: "针对Nano Banana Pro / Gemini图像模型的确定性OpenRouter图像生成适配器。当元技能需要本地图片文件和结构化的IMAGE_READY记录且不想启动LLM代理时，作为skill_exec使用。"
user-invocable: false
disable-model-invocation: true
homepage: https://clawhub.ai/skills/nano-banana-pro-openrouter
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/skills/nano-banana-pro-openrouter
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-write, filesystem-write]
    requires:
      bins: [python3]
      env: []
      config:
        - awesome_webpage.openrouter.api_key_env
        - awesome_webpage.openrouter.base_url
        - awesome_webpage.openrouter.models.image_generation
        - awesome_webpage.output_dir
entrypoint:
  command: python {baseDir}/scripts/openrouter_image.py
  args:
    - --model
    - "{{ with.model | default('google/gemini-3-pro-image-preview') }}"
    - --base-url
    - "{{ with.base_url | default('https://openrouter.ai/api/v1') }}"
    - --api-key-env
    - "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}"
    - --output-dir
    - "{{ with.output_dir }}"
    - --filename
    - "{{ with.filename | default('image.png') }}"
    - --resolution
    - "{{ with.resolution | default('1K') }}"
    - --max-images
    - "{{ with.max_images | default('6') }}"
    - --local-path-prefix
    - "{{ with.local_path_prefix | default('project/assets/images') }}"
  env:
    "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}": "{{ with.api_key | default('') }}"
  stdin: "{{ with.payload | default(with.prompt | default(inputs.user_message)) }}"
  parse: text
  timeout: 300
---

# Nano Banana Pro OpenRouter Adapter

This skill is a deterministic adapter around OpenRouter image generation. It is
intended for meta-skill `skill_exec` use, not as an open-ended agent surface.

## Contract

- Uses an explicit `with.api_key` value by injecting it into the configured
  `with.api_key_env` child process environment variable; it never renders the
  key into argv.
- Does not read `.env` files, prompt for credentials, print credentials, or
  write credentials to disk.
- Uses only the model, base URL, output directory, and local path prefix passed
  by the caller.
- Saves generated image bytes under the supplied output directory.
- Emits one `IMAGE_READY:` JSON line per saved image.
- On missing config or provider failure, emits `IMAGE_CONFIG_NEEDED` or
  `IMAGE_GENERATION_FAILED` instead of raising non-zero process errors.

## Meta-Skill Payload Mode

When stdin is JSON containing `media_slots`, `image_slots`, `slots`, or
`page_outline`, the adapter generates image slots and preserves already
downloaded images. Structured slots are preferred over free-form outline text:

```json
{
  "requirement_framing": "...",
  "media_slots": {"slots": [{"slot_id": "hero-visual", "modality": "image"}]},
  "page_outline": "...",
  "image_download": "...",
  "include_images": "YES",
  "visual_style": "..."
}
```

Existing `IMAGE_READY:` records in `image_download` are preserved. If
`IMAGE_DOWNLOAD_INCOMPLETE:` lists `unfilled_slot_ids`, only those slots are
generated. If the caller requested images but both structured slots and outline
slot parsing are empty, the adapter synthesizes minimal webpage-safe image
slots from the brief instead of emitting `no_image_slots_to_generate`.

## Plain Prompt Mode

When stdin is plain text, the adapter generates one image using that text as the
prompt and the `--filename` stem as the `slot_id`.
