---
name: music-and-singing-studio
description: "Generate instrumental music, background beds, jingles, or sung songs with lyrics through OpenSquilla audio tools. Use when the user asks for BGM, music generation, 唱歌, 生成歌曲, lyrics to song, or a playable music audio artifact."
description_zh: "通过OpenSquilla音频工具生成器乐、背景音乐、广告曲或带歌词的演唱歌曲。当用户需要BGM、音乐生成、唱歌、生成歌曲、歌词谱曲或可播放的音乐音频时使用。"
triggers:
  - "generate music"
  - "music generation"
  - "song generate"
  - "lyrics to song"
  - "唱歌"
  - "生成歌曲"
  - "生成音乐"
  - "BGM"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: medium
    capabilities: [network-read, filesystem-write]
    requires_tools:
      - music_generate
      - song_generate
      - audio_provider_capabilities
---

# music-and-singing-studio

Generates instrumental music or songs with sung vocals. OpenRouter can help
draft original lyrics, structure prompts, or translate style notes, but audio
generation must use `music_generate` or `song_generate`.

## Request triage

Before calling tools, extract these fields from the user request:

- task type: instrumental BGM, jingle, short demo, loop, full song, or sung
  lyrics
- whether lyrics are user-provided, newly original, or a prohibited copyrighted
  cover request
- target language, target locale/accent, vocal traits, backing style, mood,
  tempo, and desired duration
- quota posture: short demo first, full generation, or user-specified duration
- output expectation: one playable take, multiple variants, or background bed

OpenRouter can draft original lyrics and prompts, but it is not an audio
provider. Do not imply OpenRouter created the audio.

## Choose the tool

- Use `music_generate` for BGM, loopable beds, intro/outro, ads, transitions,
  and instrumental moods.
- Use `song_generate` when the user provides lyrics or asks for singing,
  vocals, chorus, verse, jingle with words, or 唱歌.

## Required workflow

1. Confirm whether the user wants instrumental music or sung vocals.
2. Create only original lyrics unless the user provides rights to existing
   lyrics.
3. Avoid "in the style of" living artists, bands, copyrighted songs, game
   themes, film scores, or franchise music.
4. Call `audio_provider_capabilities` if music/singing availability is
   uncertain.
5. For `music_generate`, pass a concise prompt, optional style, duration, and
   output path.
6. For `song_generate`, pass original `lyrics`, vocal style, backing style,
   duration, and output path.
7. Return the result as a playable audio artifact.

## Preview-first

For unspecified song length, generate a short demo first instead of a full
song. Use 8-15 seconds for sung demos and 10-20 seconds for instrumental BGM
unless the user explicitly asks for a longer duration.

For singing, keep first-pass lyrics compact: one hook plus one short verse is
usually enough to test vocal language, accent, melody feel, and quota behavior.
If the user asks for a complete song, generate or present the full lyrics, but
send a short demo to `song_generate` first and then scale up after approval or
after confirming key quota.

If the provider returns `quota_retry.strategy=short_preview`, treat that as a
successful short demo, not as a failed generation.

## Tool-result handling

- If `music_generate` or `song_generate` returns `status=ok`, put the playable
  artifact/path first, then duration, style, and rights summary.
- If `song_generate` returns `quota_retry.strategy=short_preview`, say a short
  demo was generated because the full request exceeded the API key quota.
- If a provider error occurs, quote the `note` and distinguish account credits,
  API key quota, feature gating, duration, format, content policy, and network
  failures.
- If no tool was called, do not speculate about credits or availability. Call
  the relevant tool or say generation was not attempted.

## Availability and credits handling

- Do not claim credits are insufficient unless `music_generate`,
  `song_generate`, or `audio_provider_capabilities(probe_live=true)` returned
  that exact provider error.
- If a provider error occurs, quote the tool's `note` / provider message and
  distinguish account credits from feature gating, duration limits, output
  format restrictions, API key quota, and content-policy rejection.
- If `song_generate` returns `status=ok` with `quota_retry.strategy=short_preview`,
  the song was generated as a shorter playable demo after an API key quota
  retry. Do not say generation failed; explain that a short version was
  produced and include the playable artifact.
- If no tool was called, do not speculate about credits. Call the relevant
  audio tool first or say the generation was not attempted.

## Copyright and authorization guard

- Copyright / 版权: do not reproduce protected lyrics, melodies, arrangements,
  backing tracks, or recognizable artist styles.
- 授权: if the user gives existing lyrics, ask for or record that they own or
  have permission to use them.
- Public figure policy: do not request a singer, actor, public figure, or band
  imitation. Use generic traits like "clear warm Mandarin pop vocal".
- If the user asks for a cover, explain that this skill can create an original
  song inspired by non-identifying mood/tempo/instrumentation instead.

## Locale and accent singing notes

For singing, first identify the target language and desired accent. The final
vocal should use a locale-appropriate accent. Lyrics, vocal style, and
pronunciation notes should match that target.

- Chinese lyrics: keep lyrics in natural Chinese and avoid unnecessary
  translation through OpenRouter. Use 普通话 phrasing unless the user asks for a
  dialect.
- English lyrics: preserve requested accent or locale, such as en-US, en-GB,
  en-AU, en-IN, or en-SG.
- Spanish/Portuguese/French and other languages: keep regional variants
  explicit when requested.
- If the vocal sounds like the wrong accent, simplify lyrics, reduce
  code-switching, add punctuation at phrase boundaries, and try a shorter
  sample before generating the full song.

## Output contract

Return:

- provider
- tool used
- duration
- output path
- playable audio artifact status
- copyright/rights summary
- target language / locale assumption
- whether this is a preview or full asset
