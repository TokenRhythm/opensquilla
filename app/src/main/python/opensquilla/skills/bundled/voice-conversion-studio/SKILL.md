---
name: voice-conversion-studio
description: "Convert a local source recording into an authorized target voice. Use when the user asks for voice conversion, voice changer, 换声, 变声, 音色转换, or converting existing narration to another approved voice."
description_zh: "将本地源录音转换为已授权的目标音色。当用户需要语音转换、变声器、换声、变声、音色转换，或将现有旁白转为另一个已批准音色时使用。"
triggers:
  - "voice conversion"
  - "voice convert"
  - "voice changer"
  - "音色转换"
  - "换声"
  - "变声"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-read, filesystem-write]
    requires_tools:
      - voice_convert
      - audio_provider_capabilities
---

# voice-conversion-studio

Converts an existing local recording into a target voice using the configured
audio provider. OpenRouter can assist with planning or file naming, but the
conversion itself must use `voice_convert`.

## Request triage

Before calling tools, extract these fields from the user request:

- source audio path and whether it is local, intentional, and user-provided
- source rights: speaker consent and recording copyright
- target voice: provider-licensed voice, cloned voice ID, or user-provided
  voice ID
- target language, target locale, desired accent, emotion, pace, and output
  format
- output expectation: quick conversion sample, final asset, or multiple takes

OpenRouter can help summarize or translate instructions, but it is not an
audio provider and cannot authorize voice identity use.

## Required workflow

1. Check the source file is local and intentionally provided.
2. Confirm rights for both sides:
   - source recording copyright and speaker authorization
   - target voice consent or provider-licensed voice
3. Refuse public figure or copyrighted character imitation.
4. Use `audio_provider_capabilities` if conversion availability is uncertain.
5. Call `voice_convert` with `source_audio`, `voice`, optional `output_path`,
   and any supported provider controls.
6. Return the result as a playable audio artifact when the surface supports it.

## Preview-first

When source quality, accent transfer, or target voice fit is uncertain, convert
a short sample before processing a full recording. Recommend re-recording or
cleaning the source if the preview contains room echo, background music, strong
dialect mismatch, or heavy code-switching.

For multilingual conversion, avoid using a target voice that does not naturally
support the target language. A short preview is the fastest way to catch odd
accent transfer before spending quota on the whole asset.

## Tool-result handling

- If `voice_convert` returns `status=ok`, return the playable artifact/path
  first, then target voice, mime type, and rights summary.
- If it returns `consent_required`, ask for source and target consent metadata
  instead of attempting a different voice identity.
- If it returns `not_available`, quote the `note` and distinguish provider
  setup, feature gating, key/quota limits, file format, and source path issues.

## Rights and copyright guard

- 授权 is required for the source speaker and target voice.
- Copyright / 版权: do not convert songs, movie lines, podcasts, audiobooks, lectures,
  interviews, or game/animation dialogue unless the user says they have rights.
- Public figure policy: do not convert a recording to sound like a public
  figure, celebrity, actor, singer, politician, influencer, or fictional
  character.
- If the user asks for a risky identity target, offer a non-identifying target:
  "mature calm Mandarin narrator", "bright young commercial voice", etc.

## Locale and accent quality notes

For voice conversion, first identify the target language and locale. The source
recording and target voice should be compatible with the desired
locale-appropriate accent.

- Chinese neutral narration: prefer clean 普通话 source and target voice.
- English: preserve requested locale such as en-US, en-GB, en-AU, en-IN, or
  en-SG.
- Japanese/Korean/French/German/Spanish/etc.: prefer source/target voices that
  naturally support that language.
- Strong dialect, background music, reverberation, and heavy code-switching can
  cause odd accent transfer. Recommend re-recording a short, dry sample before
  converting a whole script.

## Output contract

Return:

- provider
- target voice
- output path
- mime type
- playable audio artifact status
- rights/consent summary
- target language / locale assumption
