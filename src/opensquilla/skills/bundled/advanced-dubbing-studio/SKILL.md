---
name: advanced-dubbing-studio
description: "Submit audio or video for multilingual dubbing, poll status, and download dubbed audio. Use when the user asks for dubbing, 多语言配音, 视频翻译配音, 译制片, or wants a source clip dubbed into another language."
description_zh: "提交音频或视频进行多语言配音，轮询状态并下载配音后的音频。当用户需要配音、多语言配音、视频翻译配音、译制片，或想把源片段配成另一种语言时使用。"
triggers:
  - "dubbing"
  - "dub this"
  - "多语言配音"
  - "视频配音"
  - "翻译配音"
  - "译制"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-read, filesystem-write]
    requires_tools:
      - dubbing_generate
      - dubbing_status
      - dubbing_download
      - audio_provider_capabilities
---

# advanced-dubbing-studio

Runs provider-backed dubbing for local audio/video assets. OpenRouter can help
translate, review style, or summarize job status, but the dubbing job itself
must use `dubbing_generate`, `dubbing_status`, and `dubbing_download`.

## Request triage

Before calling tools, extract these fields from the user request:

- source media path and whether the file is local, intentional, and
  user-provided
- source rights, speaker consent, and whether the clip contains third-party
  copyrighted material
- source language, target language, target locale, desired accent, speaker
  count, and translation style
- output expectation: quick preview, full dub, audio-only result, or follow-up
  video muxing outside this skill
- whether the user needs polling now or only a submitted job ID

OpenRouter can help translate or adapt lines, but it is not an audio provider
and cannot perform the dubbing job itself.

## Required workflow

1. Verify the source file is local and intentionally provided.
2. Confirm the user has rights to dub the source media.
3. Identify source language, target language, target locale, and desired
   locale-appropriate accent. For Chinese target output, choose Mandarin/普通话
   unless the user explicitly requests another dialect.
4. Call `audio_provider_capabilities` if dubbing availability is uncertain.
5. Submit with `dubbing_generate`.
6. Poll with `dubbing_status` or use `dubbing_download` with polling when
   appropriate.
7. Return the downloaded dubbed audio as a playable audio artifact.

## Preview-first

For long videos, uncertain accents, or high-value assets, submit or prepare a
short preview clip first when the workflow permits it. Use the preview to check
translation style, target locale, speaker count, pacing, and whether the
provider preserves speaker separation.

If only the full source file is available, explain that the first run may need
one retry for locale/accent tuning and keep the target locale explicit in the
job notes.

## Tool-result handling

- If `dubbing_generate` returns `status=ok`, return the job ID and tell the
  user whether download is pending or already being polled.
- If `dubbing_status` is not ready, report the current status without claiming
  failure.
- If `dubbing_download` returns audio, put the playable artifact/path first.
- If any dubbing tool returns `not_available` or an error, quote the `note` and
  distinguish provider setup, feature gating, key/quota limits, source format,
  language support, and provider processing delay.

## Locale and accent constraints

When dubbing, the target language is not enough; choose the target locale and
accent as well:

- Chinese: prefer 普通话 / Mainland Mandarin target settings unless the user asks
  for Cantonese, Taiwanese Mandarin, Sichuan dialect, etc.
- English: preserve en-US, en-GB, en-AU, en-IN, en-SG, or any locale named by
  the user.
- Spanish: distinguish es-ES and Latin American variants such as es-MX.
- Portuguese: distinguish pt-BR and pt-PT.
- French: distinguish fr-FR and fr-CA when requested.
- Japanese/Korean/German/Italian/etc.: use native target-language voices rather
  than English-accented fallback voices.

- Keep translated lines natural in Chinese, not word-for-word English order.
- Avoid unnecessary English names or romanization unless the original requires
  it.
- If the result sounds like the wrong accent, retry with shorter translated
  lines, clearer punctuation, and a voice native to the target locale.

## Rights and copyright guard

- Copyright / 版权: do not dub movies, TV, anime, games, songs, audiobooks, paid
  courses, podcasts, or third-party videos unless the user states they have
  permission or the source is licensed for this use.
- 授权: if the source contains identifiable private speakers, require consent
  for voice processing and translation.
- Public figure policy: do not preserve, clone, or imitate public figure voices
  without provider-supported rights and explicit authorization.
- For user-owned marketing/demo/training clips, keep the rights summary in the
  final response.

## Output contract

Return:

- dubbing job ID
- final status
- target language
- target locale / accent assumption
- output path
- playable audio artifact status
- rights/authorization summary
