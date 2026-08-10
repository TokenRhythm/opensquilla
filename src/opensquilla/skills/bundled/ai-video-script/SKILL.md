---
name: ai-video-script
description: "Generate a structured short-video shooting script from a topic. Emits a strict, machine-parseable shot list (3 shots by default) with image prompt + video prompt + voiceover + on-screen text per shot. Trigger when the user asks for a video script, 分镜, 短视频文案, AI视频, 短剧脚本, or wants visual prompts ready for image/video generation."
description_zh: "根据主题生成结构化的短视频拍摄脚本，输出严格且可机器解析的分镜清单（默认3个镜头），每个镜头含图像提示词、视频提示词、旁白和屏幕文字。当用户需要视频脚本、分镜、短视频文案、AI视频、短剧脚本，或需要可用于图像/视频生成的视觉提示词时触发。"
provenance:
  origin: clawhub-mit0
  license: MIT-0
  upstream_url: https://clawhub.ai/aguo333/ai-video-script
  upstream_version: "1.0.0"
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: low
    capabilities: []
---

# ai-video-script — structured short-video script generator

Turns a topic/keyword + style + duration into a strict-format shooting script
the downstream `nano-banana-pro` and `seedance-2-prompt` skills can parse
without ambiguity. The default emits 3 shots; the caller may ask for 4 or 5.

## Inputs

Free-text via `with.task` / `with.request`:
- Topic / product / story
- Target audience (optional)
- Style (轻松/专业/故事/科普/带货) — narrative style, not render style
- Total duration (15s, 30s, 60s default)
- Aspect ratio (9:16 default, 16:9 optional)
- `N_SHOTS` override (5 default, **1-10 allowed**)

Caller-supplied anchors (used verbatim — this skill never invents them):
- `with.render_style` — one-line aesthetic the per-shot prompts must end
  with. Examples: `2D anime illustration, flat colour, soft cel-shading`,
  `watercolour storybook illustration`, `cinematic photoreal 35mm grain`.
  If absent / empty, emit the literal sentinel `(render style missing)`
  into the RENDER_STYLE field so downstream parsers can fail loudly.
- `with.identity_anchor` — one-line description of the main character(s)
  that every shot must reproduce byte-for-byte. Example: `Lin, a
  25-year-old East Asian woman with chin-length black bob, almond eyes,
  wearing sage-green oversized knit sweater and gold round earrings`. If
  absent / empty, emit the literal sentinel `(identity anchor missing)`
  so callers can detect the gap before spending on image/video gen.

This skill does **not** choose render style or character identity; the
orchestrator (or its user_input clarify step) does. This separation lets
the same skill serve product ads (no human) and short dramas (with
locked characters) without baked-in defaults.

## Output format (STRICT — orchestrators parse this)

Always emit exactly these top-level blocks, in this order:

```
=== OVERVIEW ===
TITLE: <one line>
DURATION_S: <int>
ASPECT_RATIO: <9:16|16:9>
STYLE: <one line>
AUDIENCE: <one line>
N_SHOTS: <int 3-5>
IDENTITY_ANCHOR: <copied verbatim from with.identity_anchor, or "(identity anchor missing)">
RENDER_STYLE: <copied verbatim from with.render_style, or "(render style missing)">

=== SHOT_1 ===
DURATION_S: <int 3-6>
CAMERA: <wide|medium|close-up + push/pull/pan/tilt/static>
IMAGE_PROMPT: <IDENTITY_ANCHOR verbatim>, <scene/action>, <RENDER_STYLE verbatim>, --ar 9:16
VIDEO_PROMPT: <IDENTITY_ANCHOR verbatim>, <one major action + camera move + duration hint>, <dialogue/voiceover/sound tags derived from VOICEOVER — see rule 11>, <RENDER_STYLE verbatim>, aspect_ratio: 9:16, no watermark, no logo, no subtitles
VOICEOVER: <one line, max 20 Chinese chars or 30 English words — kept verbatim for SRT subtitle burn-in>
ON_SCREEN_TEXT: <one short line or empty>

=== SHOT_2 ===
... (same fields, IMAGE_PROMPT and VIDEO_PROMPT must begin with the
exact same IDENTITY_ANCHOR bytes as SHOT_1)

=== SHOT_3 ===
... (same fields)
```

For any `N_SHOTS` between 1 and 10, emit exactly that many
`=== SHOT_K ===` blocks numbered 1..N_SHOTS, each with the same fields.
Do not emit shot blocks beyond `N_SHOTS`. Never skip a field; use the
literal value `none` for empty `ON_SCREEN_TEXT`.

`N_SHOTS` semantics:
- 1: a single hero shot (5-10s typical) — product/landscape vignette.
- 2-3: classic short-form story arc.
- 4-6: extended narrative with multiple beats; good for 45-60s drama.
- 7-10: stretched-form drama; total duration grows linearly with cost.

## Rules

1. **Identity continuity** — `with.identity_anchor` is pasted byte-for-byte
   at the start of every shot's IMAGE_PROMPT and VIDEO_PROMPT. Do not
   paraphrase, summarize, or pronoun-substitute it. If shot 3's anchor
   text differs by one comma from shot 1's, you wrote it wrong.
2. **Visual concreteness** — replace abstract verbs with observable action:
   "a young woman in a red trench coat walks through rain-soaked neon
   streets" >> "a woman walking".
3. **IP-safe** — do not use franchise names, character names, brand terms,
   or "style of" references. Invent original names if needed.
4. **No multi-line values** — IMAGE_PROMPT, VIDEO_PROMPT, VOICEOVER,
   ON_SCREEN_TEXT must each be a single line.
5. **Aspect ratio explicit** — every IMAGE_PROMPT ends with the literal
   token `--ar 9:16` (or `--ar 16:9`); every VIDEO_PROMPT ends with the
   literal token `aspect_ratio: 9:16` (or 16:9).
6. **Duration math** — `sum(SHOT_i.DURATION_S) == OVERVIEW.DURATION_S` ±2s.
7. **Voiceover length** — total voiceover should be speakable in
   `DURATION_S` seconds (~3 Chinese chars/sec, ~2 English words/sec).
8. **Match the user's language** — write **all** fields (TITLE, STYLE,
   AUDIENCE, IDENTITY_ANCHOR, RENDER_STYLE, IMAGE_PROMPT, VIDEO_PROMPT,
   VOICEOVER, ON_SCREEN_TEXT) in the **same language the user wrote in**.
   - The current downstream image/video models — `google/gemini-3.1-flash-image-preview`
     and `bytedance/seedance-2.0` — both accept Chinese natively.
     Seedance (ByteDance) is in fact a Chinese-first model and tends to
     produce **more on-topic results** with Chinese prompts when the
     story itself is Chinese (e.g. 咖啡店偶遇 / 国风武侠 / 校园回忆).
   - Do **not** translate the user's Chinese topic into English just to
     fill IMAGE_PROMPT — that loses cultural detail and often hallucinates
     a Western-coded substitute.
   - Mixed-language input (English topic + Chinese voiceover note,
     vice-versa) → the *bulk* of prompts follow whichever language the
     **topic/story** is in; localised fields like VOICEOVER may follow
     the language explicitly named by the user.
   - English remains valid: pick it when the user wrote in English, or
     when the user explicitly asked for English prompts.
9. **Plain text only — no emoji, no decorative symbols.** The script
   flows through Python subprocesses on Windows consoles whose default
   code page (cp936/GBK) cannot encode `✅`, `❌`, `✨`, `🎬`, or any
   non-BMP character. The orchestrator will crash with a
   `UnicodeEncodeError` if any field contains one. Use plain CJK + ASCII
   only. Do not "decorate" changed lines with checkmarks even when
   re-drafting.
10. **Style-tag exception** — RENDER_STYLE is a label, not a sentence.
   It's fine to keep canonical aesthetic tags in their native vocabulary:
   `2D anime illustration` and `水墨风, monochrome with one accent` are
   both valid; mixed-language tags like `水墨风 ink-wash, paper texture`
   also work. Whichever form the caller passes in via `with.render_style`
   is copied verbatim.
11. **VOICEOVER must also appear inside VIDEO_PROMPT as a dialogue/
    voiceover tag** — Seedance 2.0 natively generates audio AND lip-sync
    when given explicit dialogue cues in the prompt (see the upstream
    JiMeng "Short Drama with Dialogue" recipe). Without the tag,
    seedance produces silent video and the spoken line only appears
    via burned-in subtitles (no audio track). With the tag, seedance
    also generates the actual spoken audio, the speaker's mouth moves
    correctly, and the burned subtitle is reinforced by real sound.

    Choose ONE of these tag forms per shot, in this order:

    a. **Character dialogue** (the VOICEOVER reads as a character's
       quoted line — surrounded by quotes or paired with a name like
       "陆冷笑一声:'...'" / "Lu sneers, '...'"):

         Dialogue (<CharacterName from IDENTITY_ANCHOR>, <emotion>): "<the line>"

       Examples (placed inline in VIDEO_PROMPT after the action / camera
       segment, before RENDER_STYLE):
         Dialogue (Zhang, furious): "Take your internship report and get out of my company right now!"
         Dialogue (张, 愤怒): "拿着你的实习报告,立刻给我滚出公司!"
         Dialogue (Lu, sneering): "Are you sure you want to fire me, Manager Zhang?"

    b. **Narration / inner monologue** (VOICEOVER reads as a faceless
       narrator's line, no on-screen speaker, no quotes):

         Voiceover (narrator, <emotion>): "<the line>"

       Example:
         Voiceover (narrator, wistful): "推开那扇熟悉的咖啡店门。"

    c. **No voice this shot** (VOICEOVER is the literal value `none` /
       empty) — emit no dialogue tag at all in VIDEO_PROMPT. Optionally
       still add a `Sound: <ambient cue>` tag if a specific sound effect
       defines the shot.

    Additional rules for the tag:
      - The emotion label is one short adjective (calm / sad / excited
        / sneering / furious / panicked / 冷峻 / 愤怒 / 惊恐 / 平静).
        Seedance uses it to shape vocal prosody.
      - CharacterName MUST match a name token used in IDENTITY_ANCHOR
        (Zhang / 张 / Lin / 林 etc.) so seedance knows whose mouth to
        animate. Do not invent new names.
      - The "<the line>" inside the quotes is the SAME LANGUAGE as the
        VOICEOVER field. Do not translate. Keep punctuation; use ASCII
        single quotes inside dialogue if you need a quote-within-quote.
      - Negative constraint `no subtitles` STAYS in VIDEO_PROMPT —
        that's about not rendering subtitle bars inside the seedance
        video frame. The dialogue tag controls AUDIO; the subtitle bar
        is burned in by a downstream ffmpeg step from the VOICEOVER
        field.
      - Multiple speakers in one shot: chain tags with semicolons:
          Dialogue (Lu, sneering): "..." ; Dialogue (Zhang, panicked): "..."

12. **VIDEO_PROMPT length budget** — with the dialogue tag added, the
    practical ceiling is ≤500 chars per VIDEO_PROMPT. IMAGE_PROMPT
    stays at ≤220 chars. The downstream extract step in meta-short-
    drama truncates at 700 chars (after appending the Assets Mapping
    preamble), so individual VIDEO_PROMPTs that overshoot get clipped
    at the END — keep the dialogue tag BEFORE the RENDER_STYLE
    repetition so it survives truncation.

## Style presets (only adjust IMAGE_PROMPT/VIDEO_PROMPT modifiers)

- **商业 / Commercial**: "studio lighting, hero product shot, clean
  background, shallow depth of field"
- **故事 / Story**: "cinematic, soft natural light, 35mm film grain,
  shallow depth of field"
- **科普 / Educational**: "isometric infographic style, flat colour,
  bright key light, clean composition"
- **带货 / E-commerce**: "high-key lighting, white seamless background,
  product 360 spin"
- **轻松 / Casual**: "bright daylight, handheld feel, vibrant colours"

## Negative defaults (always add to VIDEO_PROMPT)

`no watermark, no logo, no subtitles, no on-screen text outside ON_SCREEN_TEXT.`

## Example A — Chinese request, all-Chinese script (50s, 5 shots, 9:16)

User wrote the request in Chinese, so every field is Chinese — including
IMAGE_PROMPT and VIDEO_PROMPT. Seedance 2.0 and Gemini 3.1 image both
handle these prompts natively. Note the RENDER_STYLE is photoreal
cinematic (an explicit opt-in style — downstream Seedance moderation MAY
refuse photoreal human references; a provider-policy refusal stops
immediately without another paid submission, then the workflow falls back
to a degraded Ken-Burns clip).

Caller passes:
- `with.identity_anchor` = `陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻;张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢`
- `with.render_style` = `电影级写实,真实摄影,戏剧化强光对比,高对比度色调`

```
=== OVERVIEW ===
TITLE: 职场反转：踢到铁板了
DURATION_S: 50
ASPECT_RATIO: 9:16
STYLE: 现代都市 / 职场爽剧
AUDIENCE: 18-35 职场青年 / 爽剧受众
N_SHOTS: 5
IDENTITY_ANCHOR: 陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻;张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢
RENDER_STYLE: 电影级写实,真实摄影,戏剧化强光对比,高对比度色调

=== SHOT_1 ===
DURATION_S: 10
CAMERA: 中景,快速跟拍
IMAGE_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢;陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,张在奢华的现代办公室里将一份文件夹狠狠摔在办公桌上,指着站在对面的陆,眼神充满鄙夷,电影级写实,真实摄影,高对比度色调,--ar 9:16
VIDEO_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢;陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,张愤怒地将文件摔在桌上并大声指责,陆面无表情地看着她,镜头快速推向张愤怒的脸,动作激进利落,0-10s,Dialogue (张, 愤怒): "拿着你的实习报告,立刻给我滚出公司!",电影级写实,真实摄影,画面无水印,无字幕,无logo,aspect_ratio: 9:16
VOICEOVER: "拿着你的实习报告，立刻给我滚出公司！"
ON_SCREEN_TEXT: 扫地出门

=== SHOT_2 ===
DURATION_S: 10
CAMERA: 特写,动态倾斜拉镜头
IMAGE_PROMPT: 陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,陆抬手摘下无框眼镜,嘴角勾起一抹极度冷酷且自信的嘲讽笑意,眼神凌厉,电影级写实,真实摄影,高对比度色调,--ar 9:16
VIDEO_PROMPT: 陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,陆动作极其干净利落地抬手摘下眼镜丢在桌上,嘴角瞬间勾起冰冷的笑意,镜头配合他的动作迅速拉近并微微倾斜,凸显压迫感,0-10s,Dialogue (陆, 冷笑): "张经理,你确定要开除我?",电影级写实,真实摄影,画面无水印,无字幕,无logo,aspect_ratio: 9:16
VOICEOVER: 陆冷笑一声："张经理，你确定要开除我？"
ON_SCREEN_TEXT: 临危不乱

=== SHOT_3 ===
DURATION_S: 10
CAMERA: 快速平移 + 瞬间推焦
IMAGE_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢;陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,陆从西装内口袋掏出一枚精致的金色集团徽章抛在桌上,张看到徽章后脸色瞬间惨白,冷汗直流,电影级写实,真实摄影,高对比度色调,--ar 9:16
VIDEO_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢;陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,陆利落地掏出金色徽章拍在桌上,镜头瞬间给徽章一个快速特写推焦,动作干净有力,0-10s,Dialogue (陆, 冷峻): "看清楚,这是什么。",电影级写实,真实摄影,画面无水印,无字幕,无logo,aspect_ratio: 9:16
VOICEOVER: "看清楚，这是什么。"
ON_SCREEN_TEXT: 亮出底牌

=== SHOT_4 ===
DURATION_S: 10
CAMERA: 特写 + 快速甩镜头（Whip Pan）
IMAGE_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,极度恐惧的表情,额头冒汗,瞪大双眼死死盯着桌上的徽章,双手颤抖,电影级写实,真实摄影,高对比度色调,--ar 9:16
VIDEO_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,极度恐惧的表情,张认出徽章后惊恐地倒退一步,浑身剧烈颤抖,镜头从桌上的徽章快速甩向张惨白的脸和颤抖的双手,节奏急促,0-10s,Dialogue (张, 惊恐): "董事长专属黑金徽章?!你...你是...",电影级写实,真实摄影,画面无水印,无字幕,无logo,aspect_ratio: 9:16
VOICEOVER: "董事长专属黑金徽章？！你……你是……"
ON_SCREEN_TEXT: 瞬间打脸

=== SHOT_5 ===
DURATION_S: 10
CAMERA: 低角度仰拍,动态跟拍
IMAGE_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢;陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,陆单手插兜转身走向总裁专属转椅利落坐下,张在背景中双腿发软扶住桌子,满脸绝望,电影级写实,真实摄影,高对比度色调,--ar 9:16
VIDEO_PROMPT: 张,35岁东亚女性,波浪长卷发,浓妆,白色职业套装,神情傲慢;陆,28岁东亚男性,背头黑发,无框眼镜,深灰色定制西装,气场冷峻,陆极其顺畅地转身上前坐上总裁椅,镜头紧跟他的动作,张在后方浑身颤抖,动作一气呵成无拖泥带水,0-10s,Dialogue (陆, 冷峻): "我的微服私访结束了。现在,收拾东西给我滚。",电影级写实,真实摄影,画面无水印,无字幕,无logo,aspect_ratio: 9:16
VOICEOVER: "我的微服私访结束了。现在，收拾东西给我滚。"
ON_SCREEN_TEXT: 终极逆袭
```

## Example B — English request, all-English script (50s, 5 shots, 9:16)

Caller passes:
- `with.identity_anchor` = `Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura; Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression`
- `with.render_style` = `Cinematic realism, authentic photography, dramatic high-contrast lighting, bold color grading`

```
=== OVERVIEW ===
TITLE: Corporate Revenge: Tricking the Titan
DURATION_S: 50
ASPECT_RATIO: 9:16
STYLE: Modern Urban / Corporate Drama / Revenge Short
AUDIENCE: 18-35 Professionals / Fast-paced Drama Enthusiasts
N_SHOTS: 5
IDENTITY_ANCHOR: Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura; Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression
RENDER_STYLE: Cinematic realism, authentic photography, dramatic high-contrast lighting, bold color grading

=== SHOT_1 ===
DURATION_S: 10
CAMERA: medium shot, fast tracking camera
IMAGE_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression; Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Zhang slams a folder onto the desk in a luxurious modern office, pointing at Lu with utter contempt, cinematic realism, authentic photography, high-contrast lighting, --ar 9:16
VIDEO_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression; Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Zhang aggressively slams a folder down and shouts angrily, Lu stares at her with a critical deadpan expression, camera snaps instantly into a tight zoom on Zhang's furious face, fast-paced action 0-10s, Dialogue (Zhang, furious): "Take your internship report and get out of my company right now!", cinematic realism, authentic photography, no watermark, no logo, no subtitles, aspect_ratio: 9:16
VOICEOVER: "Take your internship report and get out of my company right now!"
ON_SCREEN_TEXT: The Dismissal

=== SHOT_2 ===
DURATION_S: 10
CAMERA: close-up, dynamic camera tilt
IMAGE_PROMPT: Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Lu raises his hand to adjust his glasses with a sharp, swift motion, a cold and confident smirk appearing on his face, cinematic realism, authentic photography, high-contrast lighting, --ar 9:16
VIDEO_PROMPT: Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Lu adjusts his glasses with a swift, sharp finger gesture, a confident smirk appears on his lips, camera dynamically tilts up capturing his sharp eyes behind the lenses, fast pacing 0-10s, Dialogue (Lu, sneering): "Are you sure you want to fire me, Manager Zhang?", cinematic realism, authentic photography, no watermark, no logo, no subtitles, aspect_ratio: 9:16
VOICEOVER: Lu sneers, "Are you sure you want to fire me, Manager Zhang?"
ON_SCREEN_TEXT: Unshaken Confidence

=== SHOT_3 ===
DURATION_S: 10
CAMERA: rapid pan + sudden push-in zoom
IMAGE_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression; Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Lu pulls out a sleek gold corporate badge from his inner suit pocket and tosses it sharply onto the desk, Zhang's face turns pale with sheer terror, cinematic realism, authentic photography, high-contrast lighting, --ar 9:16
VIDEO_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression; Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Lu swiftly tosses a gold badge onto the desk, camera instantly snaps a sharp macro zoom onto the badge then pans up to Zhang's terrified expression, zero delay, fast-paced motion 0-10s, Dialogue (Lu, cold): "Look closely at what this is.", cinematic realism, authentic photography, no watermark, no logo, no subtitles, aspect_ratio: 9:16
VOICEOVER: Lu slams the badge down: "Look closely at what this is."
ON_SCREEN_TEXT: The Reveal

=== SHOT_4 ===
DURATION_S: 10
CAMERA: extreme close-up + rapid whip pan
IMAGE_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, terrified expression, sweat dripping down her face, staring down at the desk in sheer shock, eyes wide open, cinematic realism, authentic photography, high-contrast lighting, --ar 9:16
VIDEO_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, terrified expression, Zhang stumbles back a step, trembling violently as she recognizes the badge, camera executes a rapid whip pan from the badge to her trembling hands, fast pacing 0-10s, Dialogue (Zhang, panicked): "The global chairman's personal crest?! You... you are...", cinematic realism, authentic photography, no watermark, no logo, no subtitles, aspect_ratio: 9:16
VOICEOVER: "The global chairman's personal crest?! You... you are..."
ON_SCREEN_TEXT: Instant Regret

=== SHOT_5 ===
DURATION_S: 10
CAMERA: low angle, dynamic tracking shot
IMAGE_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression; Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Lu puts one hand in his pocket, turns around sharply, and sits down dominantly into the CEO executive chair, Zhang stands frozen in the background trembling with despair, cinematic realism, authentic photography, high-contrast lighting, --ar 9:16
VIDEO_PROMPT: Zhang, 35-year-old East Asian woman, wavy long hair, heavy makeup, white professional pantsuit, arrogant expression; Lu, 28-year-old East Asian man, slicked-back black hair, rimless glasses, dark grey tailored suit, cold and powerful aura, Lu turns around with a swift, smooth motion and sits firmly into the CEO chair, camera tracks his movement dynamically, Zhang trembles in panic in the background, fast pacing 0-10s, Dialogue (Lu, cold): "My undercover inspection is over. Now, pack your bags and get out.", cinematic realism, authentic photography, no watermark, no logo, no subtitles, aspect_ratio: 9:16
VOICEOVER: "My undercover inspection is over. Now, pack your bags and get out."
ON_SCREEN_TEXT: The Ultimate Payback
```

Notes on what makes these examples work:

- **IDENTITY_ANCHOR is the first comma-separated segment of every
  IMAGE_PROMPT and VIDEO_PROMPT**, byte-identical across all five
  shots. RENDER_STYLE sits near the end of each prompt, also repeated
  verbatim. That repetition is what gives the video model a stable
  identity + style anchor.
- Shots 2 and 4 deliberately drop the second character from the
  IDENTITY_ANCHOR fragment — only the character actually on-camera is
  named. The anchor "vocabulary" stays consistent (same name, same
  age/clothing string) but you may omit the off-camera person to keep
  the prompt focused.
- Each VIDEO_PROMPT carries ONE major action + ONE camera move per
  10-second beat. Trying to pack multiple beats into a single shot
  blurs the result.
- ON_SCREEN_TEXT is short (4 CJK chars or 2-3 English words) — long
  enough to read at 10 fps but short enough not to compete with the
  voiceover or main subject.
- VOICEOVER quotes punctuation goes verbatim into the SRT later; punctuation
  marks survive UTF-8 round-trip through ai-video-script → srt-from-script
  → subtitle-burner.

## What this skill does NOT do

- Does not call any image/video API itself — it only emits text.
- Does not invent SHOT durations that violate `OVERVIEW.DURATION_S`.
- Does not produce more than 10 shots in a single pass.
