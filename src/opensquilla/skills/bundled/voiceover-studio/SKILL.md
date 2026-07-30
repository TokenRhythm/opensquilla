---
name: voiceover-studio
description: "Generate narration, product voiceover, IVR prompts, podcast reads, or short-video VOICEOVER audio through OpenSquilla audio tools. Use when the user asks for TTS, 配音, 旁白, 口播, audio narration, or wants script text turned into a playable audio artifact."
description_zh: "通过OpenSquilla音频工具生成旁白、产品配音、IVR提示、播客朗读或短视频VOICEOVER音频。当用户需要TTS、配音、旁白、口播、音频解说，或想将脚本文字转为可播放音频时使用。"
triggers:
  - "tts"
  - "text to speech"
  - "voiceover"
  - "配音"
  - "旁白"
  - "口播"
  - "生成语音"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: medium
    capabilities: [network-read, filesystem-write]
    requires_tools:
      - tts
      - voice_search
      - audio_provider_capabilities
---

# voiceover-studio

Turns text into spoken audio with OpenSquilla's configured direct audio
provider. OpenRouter may be used by the orchestrator to draft or polish
copy, but the actual audio generation must go through the `tts` tool and
the active audio provider capability report.

## Use cases

- Single-line TTS, product demos, accessibility reads, IVR prompts.
- Batch narration from a script or `ai-video-script` `VOICEOVER` lines.
- Short-video voiceover where the result should be a playable audio artifact
  in the Web UI, not only a downloadable file path.

## Request triage

Before calling tools, extract these fields from the user request:

- task type: one-shot TTS, batch narration, IVR, podcast, accessibility read,
  or short-video voiceover
- source text and whether the user wants copy editing, translation, or exact
  preservation
- target language, target locale, desired accent, emotion, speaking pace, and
  output duration constraints
- voice source: configured default, searched shared voice, or user-provided
  voice ID
- output expectation: quick sample, final asset, or multiple takes

OpenRouter can refine script wording, but it is not an audio provider. Never
answer as if OpenRouter generated the voice.

## Preview-first

When voice quality, accent, or pacing is uncertain, generate a short sample
before a full batch. For Chinese, English locale changes, or mixed-language
copy, keep the preview to one or two natural sentences and pass
`language_code`, `speed`, and a searched `voice_id` to `tts`.

If the user asks for a long script and does not specify that they need the full
asset immediately, create the first paragraph as a preview and explain that the
remaining paragraphs can be generated after voice approval.

## Tool-result handling

- If `tts` returns `status=ok`, return the playable audio artifact/path first,
  then mention voice, language code, speed, and any inferred locale.
- If `tts` returns `not_available`, quote the `note` and distinguish provider
  configuration, missing voice ID, language mismatch, and provider errors.
- If `voice_search` returns weak or empty matches, do not force the default
  English voice for non-English text. Ask for a preferred voice or retry with a
  broader locale/accent.

## Required workflow

1. Call `audio_provider_capabilities` when the provider, paid features, or
   available voices are uncertain.
2. When the requested language, locale, or accent may not match the configured
   default voice, call `voice_search` first with the target language/locale/accent
   (for example `language=zh` + `accent=beijing mandarin`, or `language=en` +
   `accent=british`). Use a matching `voice_id` in `tts`.
3. Preserve the user's source text. Do not rewrite factual claims unless the
   user asked for copy editing.
4. Choose a voice only from configured, searched, or user-provided voice IDs.
   Do not imitate a public figure, celebrity, private person, or copyrighted
   character voice unless the user provides explicit authorization and the
   provider permits it.
5. For long text, split into natural paragraphs under the provider limit and
   generate stable filenames.
6. Call `tts` with `text`, optional `voice`, optional `language_code`, optional
   voice settings, optional `speed`, and optional `output_path`.
7. Return the resulting path and artifact metadata. Prefer surfaces that render
   the result as a playable audio artifact.

## Locale and accent constraints

First identify the target language, target locale, and desired accent. Optimize
for a locale-appropriate accent, not a one-size-fits-all "AI narrator" voice.

General rules:

- Keep source text in the target language unless the user asked for translation.
- Choose a voice that natively supports the target language when possible.
- If the user specifies a locale, preserve it: en-US, en-GB, en-AU, zh-CN,
  zh-TW, ja-JP, ko-KR, fr-FR, de-DE, es-ES, es-MX, etc.
- If the user does not specify locale, infer the most likely neutral standard
  for the language and mention the choice in the final notes.
- Avoid reading non-English languages with an English accent. Avoid reading
  English with a random accent when the user requested a specific locale.
- Keep punctuation and phrasing natural for the target language. Bad punctuation
  often causes bad accent and pacing.

Chinese defaults:

- Keep Chinese text in Chinese. Do not translate it to English before TTS.
- Prefer a Mandarin-capable voice and, when the provider exposes such labels,
  choose 普通话 / Mainland Mandarin / Chinese-native voice settings.
- Avoid unnecessary English punctuation, pinyin, romanized names, and mixed
  Latin filler unless the user wrote them intentionally.
- Keep sentence boundaries short and natural. Replace overly long comma chains
  with Chinese punctuation so the TTS model pauses correctly.
- For names, acronyms, product names, and numerals, add Chinese-readable
  wording in the text itself when needed, e.g. `A I` -> `人工智能` or
  `A-I` only when the brand requires it.
- For Chinese output, start with `speed` 0.9-1.0. Very fast speed often makes
  中文口音 sound odd.
- Before batch generation, create one short sample and ask for a listen/retry
  if the user is tuning voice quality.

English defaults:

- For American English, prefer en-US / neutral American delivery.
- For British English, prefer en-GB and avoid Americanized pronunciation.
- For Australian, Indian, Singaporean, or other English locales, keep the locale
  explicit instead of silently falling back to en-US.

Other languages:

- Japanese: prefer ja-JP voice and Japanese punctuation/cadence.
- Korean: prefer ko-KR voice and Korean punctuation/cadence.
- French/German/Spanish/Portuguese: keep regional variants explicit when the
  user names them, e.g. fr-FR vs fr-CA, es-ES vs es-MX, pt-PT vs pt-BR.

## Rights and copyright guard

- Copyright: generate only text/audio the user owns, licensed material, or
  clearly original content created for this task.
- 授权: do not synthesize a person's voice identity without consent.
- Public figure policy: do not clone or mimic a public figure voice. Use a
  generic descriptive voice such as "warm Mandarin female narrator" instead.
- If the user asks for "像某明星/某角色一样", convert it into non-identifying
  traits: age range, energy, pacing, timbre, and emotion.

## Output contract

Return concise generation notes:

- generated preview or final asset
- provider and model/capability when known
- voice ID or configured default
- language code / locale and accent assumption
- speed
- output path
- playable audio artifact status
