---
name: voice-clone-lab
description: "Create and register cloned voices for later TTS only when the speaker has explicit consent. Use when the user asks for voice clone, clone voice, 克隆音色, 复刻声音, or wants a reusable voice_id."
description_zh: "仅在说话者明确同意的情况下创建并注册克隆音色以供后续TTS使用。当用户需要语音克隆、克隆音色、复刻声音，或想要可复用的voice_id时使用。"
triggers:
  - "voice clone"
  - "clone voice"
  - "克隆音色"
  - "复刻声音"
  - "声音克隆"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-read, filesystem-write]
    requires_tools:
      - voice_clone
      - audio_provider_capabilities
---

# voice-clone-lab

Creates a reusable provider voice from a local sample. OpenRouter may help
summarize the request or produce labels, but cloning must use the direct
audio provider through `voice_clone`.

## Request triage

Before calling tools, extract these fields from the user request:

- sample path and whether the file is local, intentional, and user-provided
- speaker identity class: self, employee/team member, private person, public
  figure, fictional character, or unknown
- consent metadata: speaker, consent, sample source, permitted use, requested
  by, retention expectation, and whether commercial use is allowed
- target use: TTS narration, IVR, dubbing, training content, or internal demo
- target language, target locale, and desired locale-appropriate accent

OpenRouter can summarize consent text or label a voice, but it is not an audio
provider and cannot replace explicit consent.

## Consent-first workflow

1. Confirm the sample audio path is local and intentionally provided.
2. Require `consent_metadata` before calling `voice_clone`.
3. Include at minimum:
   - `speaker`
   - `consent: true`
   - `sample_source`
   - `permitted_use`
   - `requested_by`
4. Reject or stop when consent is missing, vague, or contradicted by the
   request.
5. Call `audio_provider_capabilities` if cloning availability is uncertain.
6. Call `voice_clone` with the sample, name, description, and consent metadata.
7. Return the created voice ID and the allowed usage summary.

## Tool-result handling

- If `voice_clone` returns `status=ok`, return the voice ID first, then the
  consent summary, intended locale/accent, and any sample-quality warning.
- If it returns `consent_required`, do not proceed with a workaround. Ask for
  the missing consent metadata in one concise question.
- If the provider returns `not_available`, quote the `note` and distinguish
  disabled provider, key/quota limits, feature gating, and sample format issues.
- Never suggest scraping, downloading, or extracting third-party voice samples
  as a fallback.

## Rights and copyright guard

- 授权 is mandatory. The speaker must own or control the voice sample and agree
  to cloning for this use.
- Copyright / 版权: do not use copyrighted recordings, film/TV/game clips, music
  stems, interviews, or scraped audio unless the user states they have rights.
- Public figure policy: do not clone or imitate a public figure, celebrity,
  politician, influencer, actor, singer, or fictional character voice.
- Do not help bypass provider safety checks or watermark/disclosure duties.
- Store only the returned provider voice ID and consent summary in ordinary
  output; do not duplicate raw sample audio.

## Locale and accent quality notes

Ask which target language and locale the cloned voice will be used for. A clone
works best when the sample matches the desired locale-appropriate accent.

- Chinese neutral narration: use clean 普通话 sample audio.
- American English: use clean en-US sample audio.
- British English: use clean en-GB sample audio.
- Japanese/Korean/French/German/Spanish/etc.: use samples spoken in that target
  language, not an English sample repurposed cross-lingually.
- Strong dialect, code-switching, room echo, music, or singing can produce odd
  accent transfer in later TTS. Recommend 30-90 seconds of dry speech when
  possible.

## Output contract

Return:

- provider
- voice ID
- voice name
- consent summary
- allowed use
- target language / locale assumption
- warning if the source sample quality may harm target-language accent quality
