---
name: meta-short-drama
description: "Use this meta-skill instead of answering directly when the current user asks to generate an AI short-drama or 短剧 from a topic. The workflow infers render style, character identity, and shot count (1-10, default 5) from the request (filling in conservative defaults when missing), drafts a strict shot-by-shot shooting script, pauses for one free-form review (the user can approve, adjust render style / character / shot count / shot details, or cancel in plain language), optionally re-drafts the script with the user's adjustments, generates one universal full-cast identity-reference image plus per-shot composition images, then per-shot video clips (each video anchored to BOTH the universal reference image and its own composition image so the character identity AND scene layout stay consistent), bookends them with a title card and an ending card, burns subtitles in the user's language, and saves the script alongside the final MP4. Do not use it for slide decks, document-decision analysis, single-image generation, isolated script writing, or pasted historical short-drama examples."
description_zh: "当用户要求根据主题生成AI短剧或短剧时，使用此元技能而非直接回答。工作流从请求中推断渲染风格、角色身份和镜头数（1-10，默认5），起草严格的逐镜头拍摄脚本，暂停一次供用户自由评审（可批准、调整渲染风格/角色/镜头数/镜头细节或取消），可选地按用户调整重新起草脚本，生成一张通用全员身份参考图和各镜头构图图，再生成逐镜视频（每段视频同时锚定通用参考图和自身构图图以保持角色身份与场景布局一致），加上片头片尾卡，按用户语言烧录字幕，并将脚本与最终MP4一并保存。不用于幻灯片、文档决策分析、单图生成、独立脚本写作或粘贴的历史短剧示例。"
kind: meta
meta_priority: 75
always: false
final_text_mode: "step:deliver"
request_template:
  outcome: "Short-drama script and generation plan with review pause before media generation."
  outcome_zh: "短剧剧本和生成计划，并在媒体生成前暂停让用户审阅。"
  outcome_en: "Short-drama script and generation plan with review pause before media generation."
  fields:
    - name: story_topic
      label_zh: "故事主题"
      label_en: "Story topic"
      required: true
    - name: render_style
      label_zh: "渲染风格"
      label_en: "Render style"
      required: false
    - name: character_identity
      label_zh: "角色设定"
      label_en: "Character identity"
      required: false
    - name: shot_count
      label_zh: "镜头数量"
      label_en: "Shot count"
      required: false
      default: 5
    - name: audience
      label_zh: "受众"
      label_en: "Audience"
      required: false
      default: "short-video viewer"
      default_zh: "短视频观众"
      default_en: "short-video viewer"
    - name: language
      label_zh: "输出语言"
      label_en: "Output language"
      required: false
      default: "match the user's language"
      default_zh: "跟随用户语言"
      default_en: "match the user's language"
  assumptions:
    - "Pause for one free-form review before generating media."
    - "Keep shot count between 1 and 10 and use conservative defaults when unspecified."
  assumptions_zh:
    - "生成媒体前会暂停一次，允许用户自由审阅和修改。"
    - "镜头数量保持在 1 到 10 之间，未说明时使用保守默认值。"
  assumptions_en:
    - "Pause for one free-form review before generating media."
    - "Keep shot count between 1 and 10 and use conservative defaults when unspecified."
output_contract:
  append_to_final_text: false
  required_sections:
    - "Story/script summary"
    - "Review or adjustment status"
    - "Generated media status"
    - "Saved deliverable locations"
  assumptions:
    - "Visual identity and shot count use conservative defaults when absent."
  unverified:
    - "Third-party media generation quality until generated assets are inspected."
  artifacts:
    - name: "short_drama_video"
      required: false
    - name: "script_file"
      required: false
eval_prompts:
  - name: "short-drama-baseline"
    prompt: "Create a five-shot short-drama plan from a topic, including review status and deliverable locations."
    rubric:
      - "Story/script summary"
      - "Review or adjustment status"
      - "Generated media status"
      - "Saved deliverable locations"
preference_keys:
  - preferred_language
  - short_drama_render_style
policy_tags:
  - generated-media-review
  - user-approval-before-media
triggers:
  - "生成短剧"
  - "生成一个短剧"
  - "生成一段短剧"
  - "做一个AI短剧"
  - "帮我做一个短剧"
  - "三分镜短剧"
  - "短视频分镜成片"
  - "分镜成片"
  - "generate a short drama"
  - "generate short drama"
  - "make a short drama from"
  - "topic to short drama mp4"
  - "shot list to final mp4"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: high
    capabilities: [network-read, filesystem-write, process-control]
    composition_skills:
      - ai-video-script
      - nano-banana-pro
      - seedance-2-prompt
      - video-still-animator
      - video-merger
      - srt-from-script
      - subtitle-burner
      - title-card-image
      - text-file-read
composition:
  steps:
    # =========================================================================
    # 1. Best-effort intake — extract RENDER_STYLE / IDENTITY_ANCHOR / N_SHOTS
    #    from the user message, or fill in conservative defaults. Never asks
    #    the user here; the user gets one combined chance to adjust after
    #    seeing the actual script in step 3.
    # =========================================================================
    - id: intake_extract
      label: "需求提取"
      label_en: "Requirement extraction"
      kind: llm_chat
      with:
        system: "Extract or invent a short-drama intake contract. Match the user's language for RENDER_STYLE / IDENTITY_ANCHOR. Be conservative — pick safe defaults rather than asking the user."
        task: |
          Read the request and emit exactly this 7-line block, in this
          order, with no extra commentary:

          TOPIC: <one short line — the actual story/product topic>
          RENDER_STYLE: <render aesthetic, one line in user's language>
          AUTO_FILLED_RENDER_STYLE: <yes|no>
          IDENTITY_ANCHOR: <one line in user's language describing main character(s)>
          AUTO_FILLED_IDENTITY_ANCHOR: <yes|no>
          N_SHOTS: <integer 1..10, default 5>
          AUTO_FILLED_N_SHOTS: <yes|no>

          Rules:
          - Detect dominant language of the request. Use that language for
            RENDER_STYLE and IDENTITY_ANCHOR. Downstream models accept
            Chinese natively (seedance is Chinese-first).
          - If user named a render style verbatim → copy it, AUTO_FILLED_RENDER_STYLE: no.
          - Else INFER a render style from the TOPIC's genre, era, and
            tone — DO NOT default to anime. Pick whichever of these
            best fits the story you just read; fall through to a fresh
            descriptor if none match exactly. Use the user's language.
              * 现代职场 / 都市爽剧 / 商战 / 反转 / corporate drama →
                  电影级写实, 真实摄影, 戏剧化强光对比, 高对比度色调
                  / Cinematic realism, dramatic high-contrast lighting
              * 古风 / 武侠 / 仙侠 / 宫廷 / wuxia / xianxia →
                  水墨风, 中国传统工笔画, 柔和留白构图
                  / Ink-wash painting, traditional Chinese gongbi style
              * 校园 / 青春 / 恋爱 / 治愈 / slice-of-life / romance →
                  日系胶片质感, 柔和自然光, 浅景深, 温暖调色
                  / Japanese film aesthetic, soft natural light, warm grade
              * 科幻 / 赛博朋克 / 未来 / sci-fi / cyberpunk →
                  赛博朋克霓虹, 体积光雾气, 高对比反射, 未来感
                  / Cyberpunk neon, volumetric haze, future-noir
              * 恐怖 / 悬疑 / 惊悚 / horror / thriller / noir →
                  低调照明, 高反差暗调, 电影黑色风格
                  / Low-key lighting, high-contrast noir, cinematic shadow
              * 童话 / 绘本 / 儿童 / fairytale / picture-book / kids →
                  水彩绘本插画, 柔和纸面纹理, 暖色调
                  / Watercolour storybook, soft paper texture, warm palette
              * 商品 / 广告 / 带货 / product / commercial →
                  影棚布光, 浅景深产品特写, 干净背景
                  / Studio lighting, hero-product close-up, clean background
              * 美食 / 烹饪 / food / cooking →
                  顶光美食摄影, 自然质感, 浅景深
                  / Top-down food photography, natural texture, shallow DOF
              * 科普 / 教学 / 信息图 / explainer / educational →
                  扁平信息图风格, 简洁配色, 平面构图
                  / Flat infographic style, clean palette, geometric layout
              * 卡通 / 动画 / 二次元 / 萌系 — only when the user really
                wants anime → 2D 动漫插画, 扁平上色, 柔和赛璐璐阴影
                                / 2D anime illustration, flat cel-shading
              * none of the above → write ONE descriptive line that
                matches the topic's mood (NOT anime by default). Examples:
                  documentary realism / oil-painting cinematic / vintage
                  super-8 grain / minimalist black-and-white photography.
            AUTO_FILLED_RENDER_STYLE: yes
          - If user described main character(s) with at least
            ethnicity + age + hair + outfit → summarise ≤40 words,
            AUTO_FILLED_IDENTITY_ANCHOR: no.
          - Else invent ONE or TWO original characters fitting the TOPIC.
          - If user named shot count (3 个分镜 / "5 shots" / etc.) → use it
            clamped 1..10, AUTO_FILLED_N_SHOTS: no.
          - Else default N_SHOTS: 5, AUTO_FILLED_N_SHOTS: yes.
          - Never ask the user a question. The user reviews in step 3.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1500) }}

    # =========================================================================
    # 2. Draft the script with whatever values we have. Free (LLM only).
    # =========================================================================
    - id: script_draft
      label: "剧本初稿"
      label_en: "Draft script"
      kind: agent
      skill: ai-video-script
      depends_on: [intake_extract]
      with:
        task: |
          Generate a strict-format short-drama shooting script following
          ai-video-script's SKILL.md OUTPUT FORMAT section. Use the
          N_SHOTS value from the intake contract below (clamp 1..10).
          Default DURATION_S total: 50 (~10s per shot for the default 5
          shots). ASPECT_RATIO: 9:16.

          Output style: plain text only. No emoji, no decorative symbols.
          Do not call publish_artifact or any other tool. The meta-skill
          captures this step's final assistant text directly, so your final
          message must contain the complete script itself, not a file link,
          artifact marker, or "[Used tool: ...]" placeholder.

          Language: match the user's request language for every field.
          Both downstream models accept CJK natively — do NOT translate
          Chinese stories into English.

          IDENTITY_ANCHOR and RENDER_STYLE below are caller-supplied —
          paste them byte-for-byte into every shot's IMAGE_PROMPT and
          VIDEO_PROMPT. Do not paraphrase or invent alternates.

          Intake contract:
          {{ outputs.intake_extract | truncate(1500) }}

          User original request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Emit OVERVIEW.IDENTITY_ANCHOR, OVERVIEW.RENDER_STYLE, and
          OVERVIEW.N_SHOTS lines so downstream steps can re-extract them.

    # =========================================================================
    # 2b. Persist the draft to disk BEFORE the review pause so the user
    #     can hand-edit the file directly while reviewing. The next step
    #     reads it back so manual edits propagate even when the user's
    #     reply doesn't mention them.
    # =========================================================================
    - id: script_save_draft
      label: "保存初稿"
      label_en: "Save draft"
      kind: tool_call
      tool: write_file
      tool_allowlist: [write_file]
      depends_on: [script_draft]
      tool_args:
        path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/script.txt"
        content: "{{ outputs.script_draft }}"

    # =========================================================================
    # 3. ONE combined review gate — free-form. The user can approve,
    #    rewrite anything, or cancel.
    # =========================================================================
    - id: review_gate
      label: "审查门禁"
      label_en: "Review gate"
      kind: user_input
      depends_on: [script_save_draft, script_draft, intake_extract]
      clarify:
        mode: form
        intro: |
          脚本就绪。下面是脚本预览 + 我对风格/角色/分镜数做的假设
          (标 AUTO_FILLED: yes 的项是我替你填的,你可以改)。

          脚本草稿已存到本次运行目录的 script.txt —— 想直接改文件也行,
          下一步会重新读盘,你的手动编辑会一起带进去。

          你怎么回都行 —— 不用按固定格式:
            - 满意就直接说 "ok" / "继续" / "proceed"
            - 想换风格 → 写一句新的 RENDER_STYLE
            - 想换角色 → 写新的 IDENTITY_ANCHOR
            - 想改分镜数 → 直接说 "5 个分镜" / "改成 7 镜头"
            - 想改某镜内容 → 直接说 "镜头2节奏快点" / "shot 3 换成屋顶场景"
            - 不想做了 → 说 "取消" / "cancel" / "停"

          预估成本(选继续才会发生):
            - N 张镜头图 + 1 张全角色参考图 (nano-banana-pro)  ≈ N × $0.05 + $0.05-$0.10
            - N 段视频 (seedance-2.0)   ≈ $0.15/s × 总时长
              (脚本里每镜 DURATION_S 决定时长)
            - 封面 + 结尾卡 (本地 Pillow + ffmpeg,免费)
            - ffmpeg 拼接 + 烧字幕
            合计随 N_SHOTS 与总时长缩放。

          === 我做的假设 ===
          {{ outputs.intake_extract | truncate(800) }}

          === 脚本草稿 ===
          {{ outputs.script_draft | truncate(3500) }}
        intro_zh: |
          脚本就绪。下面是脚本预览，以及我对风格、角色和分镜数做的假设。

          标 AUTO_FILLED: yes 的项是我替你填的，你可以改。脚本草稿已存到本次运行目录的 script.txt；如果你直接改文件，下一步会重新读盘并带入修改。

          你怎么回都行：满意就说“继续”；想换风格、角色、分镜数或某个镜头，直接说你的修改；不想做了就说“取消”。

          预估成本只会在你选择继续后发生，主要随镜头数和总时长变化。

          === 我做的假设 ===
          {{ outputs.intake_extract | truncate(800) }}

          === 脚本草稿 ===
          {{ outputs.script_draft | truncate(3500) }}
        intro_en: |
          The script is ready. Below is the script preview plus the assumptions I made about style, character identity, and shot count.

          Items marked AUTO_FILLED: yes were filled conservatively and can be changed. The draft script was saved to script.txt in this run directory; if you edit that file directly, the next step will reread it and include your manual edits.

          Reply naturally: say "continue" if it looks good, describe any style, character, shot-count, or shot-level changes, or say "cancel" to stop.

          Estimated media cost only happens if you continue, and mainly scales with shot count and total duration.

          === Assumptions I made ===
          {{ outputs.intake_extract | truncate(800) }}

          === Script draft ===
          {{ outputs.script_draft | truncate(3500) }}
        nl_extract: true
        fields:
          - name: review
            type: string
            required: true
            prompt: |
              用户对脚本草稿的整段回复 — 直接把用户说的所有文字原样
              放进这个字段,不要总结、不要重写、不要解释。这是一个
              catch-all 字段:任何同意/拒绝/修改意见/吐槽/闲聊都属于这里。
              The user's verbatim reply about the script draft. Copy the
              user's entire reply text into this single field — do not
              summarise, paraphrase, translate, or split it. This is a
              catch-all: approvals, rejections, edits, off-topic remarks
              all belong here. If the user's reply is empty or pure
              whitespace, emit "(empty)" so the field always has a value.
            prompt_zh: |
              用户对脚本草稿的整段回复。原样放进这个字段，不要总结、不要重写、不要解释。任何同意、拒绝、修改意见、吐槽或闲聊都属于这里。
            prompt_en: |
              The user's verbatim reply about the script draft. Copy the entire reply into this single field; do not summarize, paraphrase, translate, or split it. Approvals, rejections, edits, off-topic remarks, and empty replies all belong here.
            max_chars: 4000
        cancel_keywords: ["cancel", "取消", "算了", "停止", "stop", "abort"]
        timeout_hours: 24

    # =========================================================================
    # 4. Parse the free-form review.
    # =========================================================================
    - id: review_normalize
      label: "审查归一"
      label_en: "Review normalization"
      kind: llm_chat
      depends_on: [review_gate]
      with:
        system: "Emit a strict 6-line block. No commentary outside it."
        task: |
          Parse the user's free-form review of the script draft and emit
          exactly this block:

          DECISION: <proceed|cancel>
          HAS_OVERRIDES: <yes|no>
          NEW_RENDER_STYLE: <new one-line value, or "unchanged">
          NEW_IDENTITY_ANCHOR: <new one-line value, or "unchanged">
          NEW_N_SHOTS: <integer 1..10, or "unchanged">
          NEW_NOTES: <any other adjustments to story / shots / voiceover, or "unchanged">

          Rules:
          - DECISION: cancel only on explicit cancel/取消/算了/停 words.
          - DECISION: proceed otherwise (approvals AND adjustments).
          - HAS_OVERRIDES: yes if ANY of NEW_RENDER_STYLE /
            NEW_IDENTITY_ANCHOR / NEW_N_SHOTS / NEW_NOTES differs from
            "unchanged".
          - NEW_RENDER_STYLE / NEW_IDENTITY_ANCHOR / NEW_NOTES: use the
            same language as the user's reply.
          - NEW_N_SHOTS: extract integer (e.g. "改成 5 镜头" → 5).
            Clamp 1..10. Else "unchanged".

          Free-form user review:
          {{ inputs.get('collected', {}).get('review_gate', {}) | tojson | truncate(2200) }}

          Original assumptions (for delta detection):
          {{ outputs.intake_extract | truncate(800) }}

    # =========================================================================
    # 4b. Re-read the script from disk so any hand-edits the user made to
    #     script.txt during the review pause are honoured by the redraft
    #     step. When the user didn't touch the file this is just an echo
    #     of the original draft.
    # =========================================================================
    - id: script_reread
      label: "剧本复读"
      label_en: "Script reread"
      kind: skill_exec
      skill: text-file-read
      depends_on: [review_gate, script_save_draft]
      with:
        input: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/script.txt"

    # =========================================================================
    # 5. Re-draft script when the user supplied adjustments. Free.
    # =========================================================================
    - id: script_revised
      label: "剧本修订"
      label_en: "Script revision"
      kind: agent
      skill: ai-video-script
      depends_on: [review_normalize, script_reread]
      when: "'DECISION: proceed' in outputs.review_normalize and 'HAS_OVERRIDES: yes' in outputs.review_normalize"
      with:
        task: |
          Re-draft the script applying the user's overrides. Keep the
          same OUTPUT FORMAT as ai-video-script's SKILL.md. If NEW_N_SHOTS
          is an integer, use exactly that many shot blocks (1..10).
          Otherwise keep the original N_SHOTS.

          Output style: plain text only. No emoji.
          Language: keep the user's original request language.

          Apply overrides in priority: NEW_NOTES → NEW_N_SHOTS →
          NEW_RENDER_STYLE → NEW_IDENTITY_ANCHOR. "unchanged" fields
          inherit from the previous script verbatim.

          Previous script (re-read from disk — if the user hand-edited
          script.txt during review, those edits are already baked in
          here, so preserve them):
          {{ outputs.script_reread | truncate(8000) }}

          Parsed overrides:
          {{ outputs.review_normalize | truncate(1500) }}

          User original request:
          {{ inputs.user_message | xml_escape | truncate(800) }}

    # =========================================================================
    # 6. Pick the final script everyone downstream reads.
    # =========================================================================
    - id: final_script
      label: "最终剧本"
      label_en: "Final script"
      kind: llm_chat
      depends_on: [review_normalize, script_reread, script_revised]
      with:
        system: "Echo one of two inputs verbatim. No commentary. No new content."
        task: |
          If a revised script block is present below, echo it verbatim.
          Otherwise echo the re-read script verbatim (this preserves any
          hand-edits the user made to script.txt during review).

          REVISED (may be empty):
          {{ outputs.get('script_revised', '') | truncate(8000) }}

          RE-READ FROM DISK:
          {{ outputs.script_reread | truncate(8000) }}

    # =========================================================================
    # 7. Save the final script to disk (overwrites the draft so the file
    #    on disk always reflects the post-review canonical script —
    #    important when the LLM produced a revision the user didn't write
    #    by hand).
    # =========================================================================
    - id: script_save
      label: "保存剧本"
      label_en: "Save script"
      kind: tool_call
      tool: write_file
      tool_allowlist: [write_file]
      depends_on: [final_script]
      tool_args:
        path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/script.txt"
        content: "{{ outputs.final_script }}"

    # =========================================================================
    # 8. Title / subtitle / ending text extracts (cheap llm_chat).
    # =========================================================================
    - id: title_extract
      label: "标题提取"
      label_en: "Title extraction"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          From the script, output exactly the value after "TITLE:"
          inside the "=== OVERVIEW ===" block. Single line.

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: subtitle_extract
      label: "字幕提取"
      label_en: "Subtitle extraction"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          Compose a short subtitle for the cover card describing this
          drama in 5-12 characters (or 2-4 English words). Match the
          script's language. Examples:
            Chinese script → "AI 短剧 · 30 秒"
            English script → "AI Short Drama · 30s"

          Script (read OVERVIEW.TITLE / DURATION_S / AUDIENCE):
          {{ outputs.final_script | truncate(2000) }}

    - id: ending_text_extract
      label: "结尾文案"
      label_en: "Closing copy"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          Output the appropriate ending-card text. Single line, no commentary.
            Chinese script  → 完
            English script  → THE END
            Other languages → THE END

          Script (sample to detect language):
          {{ outputs.final_script | truncate(1500) }}

    # =========================================================================
    # 8b. Universal identity-reference image. One full-cast neutral lineup
    #     PNG that every shot's video step uses as the IDENTITY anchor
    #     (input_reference). Each shot ALSO passes its own composition
    #     PNG (N_shot.png) as a second reference. Two-anchor model:
    #       slot 1 (reference.png)  → who the characters look like
    #       slot 2 (N_shot.png)     → how the scene is laid out
    # =========================================================================
    - id: reference_prompt_extract
      label: "参考提示"
      label_en: "Reference prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          Build a single-line image prompt for a full-cast identity
          reference card. The picture must show EVERY named character
          that appears in ANY shot of the script (NOT just the
          OVERVIEW.IDENTITY_ANCHOR anchors — supporting cast, cameo
          characters, anyone the script mentions by name in any SHOT
          block also belongs here), standing together in a neutral
          lineup against a neutral backdrop. The downstream video model
          uses this image as the universal identity anchor for every
          shot.

          Procedure (do these silently in your head; only emit the final
          single-line prompt):

          1. Read the entire script. Enumerate every distinct named
             character that appears in ANY SHOT_N block's IMAGE_PROMPT
             or VIDEO_PROMPT. Include characters who appear in only one
             shot. Deduplicate by name. Let N be the count.
          2. For each character, write the most complete canonical
             attribute string the script gives them (name, age,
             ethnicity, hair, outfit, distinguishing accessory). Pull
             missing fields from OVERVIEW.IDENTITY_ANCHOR if needed.
          3. Compose the final prompt as a single line in this exact
             order:

               <char 1 description>; <char 2 description>; ...; <char N description>, ALL <N> characters standing side by side in a horizontal full-body group lineup, every character clearly visible from head to toe, evenly spaced across frame, wide-angle group photo, neutral studio lighting, neutral light grey backdrop, no props, no background scene, group portrait composition, <OVERVIEW.RENDER_STYLE verbatim>, --ar 9:16

             - Use ; (semicolon) BETWEEN characters, exactly as in the
               examples above.
             - State the integer N explicitly inside "ALL <N> characters".
             - If N = 1, still say "ALL 1 character" and drop the
               "side by side / horizontal lineup" phrasing — write
               "single-character full-body portrait" instead.

          Output a single line. No quotes. No commentary outside the
          prompt itself.

          Script (READ THE FULL SCRIPT, including every SHOT_N block,
          not just OVERVIEW):
          {{ outputs.final_script | truncate(8000) }}

    - id: reference_image
      label: "参考图"
      label_en: "Reference image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [reference_prompt_extract, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        prompt: "{{ outputs.reference_prompt_extract | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        # Use 3-pro as primary here: this image runs ONCE per drama and
        # has to render every cast member visibly, which 3-pro handles
        # better than 3.1-flash on dense multi-subject prompts. Per-shot
        # images keep 3.1-flash for cost.
        model: "google/gemini-3-pro-image-preview"
        max_retries: 1
        fallback_model: "google/gemini-3.1-flash-image-preview"
        placeholder_on_fail: "yes"

    # =========================================================================
    # 9. Cover card image + 2s video (gated on proceed).
    # =========================================================================
    - id: cover_image
      label: "封面图"
      label_en: "Cover image"
      kind: skill_exec
      skill: title-card-image
      depends_on: [title_extract, subtitle_extract, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        text: "{{ outputs.title_extract | truncate(40) }}"
        subtitle: "{{ outputs.subtitle_extract | truncate(40) }}"
        output: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/0_cover.png"
        background: "#101018"
        text_color: "#ffffff"
        font_size: 80
        subtitle_size: 32
        width: 720
        height: 1280

    - id: cover_video
      label: "封面视频"
      label_en: "Cover video"
      kind: skill_exec
      skill: video-still-animator
      depends_on: [cover_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/0_cover.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/0_cover.mp4"
        duration: 2
        width: 720
        height: 1280
        fps: 24
        zoom_rate: 0.0008

    # ---- SHOT_1 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot1_img_prompt
      label: "镜头1图提示"
      label_en: "Shot 1 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_1 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 1):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot1_vid_prompt
      label: "镜头1视频提示"
      label_en: "Shot 1 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_1 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot1_duration
      label: "镜头1时长"
      label_en: "Shot 1 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_1 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_2 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot2_img_prompt
      label: "镜头2图提示"
      label_en: "Shot 2 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_2 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 2):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot2_vid_prompt
      label: "镜头2视频提示"
      label_en: "Shot 2 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_2 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot2_duration
      label: "镜头2时长"
      label_en: "Shot 2 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_2 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_3 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot3_img_prompt
      label: "镜头3图提示"
      label_en: "Shot 3 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_3 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 3):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot3_vid_prompt
      label: "镜头3视频提示"
      label_en: "Shot 3 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_3 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot3_duration
      label: "镜头3时长"
      label_en: "Shot 3 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_3 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_4 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot4_img_prompt
      label: "镜头4图提示"
      label_en: "Shot 4 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_4 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 4):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot4_vid_prompt
      label: "镜头4视频提示"
      label_en: "Shot 4 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_4 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot4_duration
      label: "镜头4时长"
      label_en: "Shot 4 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_4 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_5 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot5_img_prompt
      label: "镜头5图提示"
      label_en: "Shot 5 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_5 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 5):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot5_vid_prompt
      label: "镜头5视频提示"
      label_en: "Shot 5 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_5 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot5_duration
      label: "镜头5时长"
      label_en: "Shot 5 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_5 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_6 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot6_img_prompt
      label: "镜头6图提示"
      label_en: "Shot 6 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_6 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 6):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot6_vid_prompt
      label: "镜头6视频提示"
      label_en: "Shot 6 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_6 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot6_duration
      label: "镜头6时长"
      label_en: "Shot 6 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_6 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_7 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot7_img_prompt
      label: "镜头7图提示"
      label_en: "Shot 7 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_7 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 7):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot7_vid_prompt
      label: "镜头7视频提示"
      label_en: "Shot 7 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_7 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot7_duration
      label: "镜头7时长"
      label_en: "Shot 7 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_7 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_8 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot8_img_prompt
      label: "镜头8图提示"
      label_en: "Shot 8 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_8 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 8):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot8_vid_prompt
      label: "镜头8视频提示"
      label_en: "Shot 8 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_8 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot8_duration
      label: "镜头8时长"
      label_en: "Shot 8 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_8 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_9 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot9_img_prompt
      label: "镜头9图提示"
      label_en: "Shot 9 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_9 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 9):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot9_vid_prompt
      label: "镜头9视频提示"
      label_en: "Shot 9 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_9 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot9_duration
      label: "镜头9时长"
      label_en: "Shot 9 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_9 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_10 extracts (run even if shot doesn't exist; returns sentinel) ----
    - id: shot10_img_prompt
      label: "镜头10图提示"
      label_en: "Shot 10 image prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_10 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 10):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot10_vid_prompt
      label: "镜头10视频提示"
      label_en: "Shot 10 video prompt"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_10 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    - id: shot10_duration
      label: "镜头10时长"
      label_en: "Shot 10 duration"
      kind: llm_chat
      depends_on: [final_script]
      with:
        system: "Return exactly one integer or the literal __SHOT_ABSENT__. No commentary."
        task: |
          If the script contains a "=== SHOT_10 ===" block:
            output exactly the integer after "DURATION_S:" inside that
            block, clamped to [3, 15]. Digits only, no units.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | truncate(8000) }}

    # ---- SHOT_1 image / video / fallback ----
    - id: shot1_image
      label: "镜头1图像"
      label_en: "Shot 1 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot1_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot1_img_prompt"
      with:
        prompt: "{{ outputs.shot1_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/1_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot1_video
      label: "镜头1视频"
      label_en: "Shot 1 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot1_vid_prompt, shot1_duration, reference_image, shot1_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot1_vid_prompt"
      on_failure: shot1_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot1_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/1_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/1_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot1_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot1_video_fallback
      label: "镜头1视频兜底"
      label_en: "Shot 1 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/1_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/1_shot.mp4"
        duration: "{{ outputs.shot1_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_2 image / video / fallback ----
    - id: shot2_image
      label: "镜头2图像"
      label_en: "Shot 2 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot2_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot2_img_prompt"
      with:
        prompt: "{{ outputs.shot2_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/2_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot2_video
      label: "镜头2视频"
      label_en: "Shot 2 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot2_vid_prompt, shot2_duration, reference_image, shot2_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot2_vid_prompt"
      on_failure: shot2_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot2_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/2_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/2_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot2_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot2_video_fallback
      label: "镜头2视频兜底"
      label_en: "Shot 2 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/2_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/2_shot.mp4"
        duration: "{{ outputs.shot2_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_3 image / video / fallback ----
    - id: shot3_image
      label: "镜头3图像"
      label_en: "Shot 3 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot3_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot3_img_prompt"
      with:
        prompt: "{{ outputs.shot3_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/3_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot3_video
      label: "镜头3视频"
      label_en: "Shot 3 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot3_vid_prompt, shot3_duration, reference_image, shot3_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot3_vid_prompt"
      on_failure: shot3_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot3_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/3_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/3_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot3_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot3_video_fallback
      label: "镜头3视频兜底"
      label_en: "Shot 3 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/3_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/3_shot.mp4"
        duration: "{{ outputs.shot3_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_4 image / video / fallback ----
    - id: shot4_image
      label: "镜头4图像"
      label_en: "Shot 4 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot4_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot4_img_prompt"
      with:
        prompt: "{{ outputs.shot4_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/4_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot4_video
      label: "镜头4视频"
      label_en: "Shot 4 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot4_vid_prompt, shot4_duration, reference_image, shot4_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot4_vid_prompt"
      on_failure: shot4_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot4_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/4_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/4_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot4_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot4_video_fallback
      label: "镜头4视频兜底"
      label_en: "Shot 4 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/4_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/4_shot.mp4"
        duration: "{{ outputs.shot4_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_5 image / video / fallback ----
    - id: shot5_image
      label: "镜头5图像"
      label_en: "Shot 5 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot5_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot5_img_prompt"
      with:
        prompt: "{{ outputs.shot5_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/5_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot5_video
      label: "镜头5视频"
      label_en: "Shot 5 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot5_vid_prompt, shot5_duration, reference_image, shot5_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot5_vid_prompt"
      on_failure: shot5_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot5_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/5_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/5_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot5_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot5_video_fallback
      label: "镜头5视频兜底"
      label_en: "Shot 5 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/5_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/5_shot.mp4"
        duration: "{{ outputs.shot5_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_6 image / video / fallback ----
    - id: shot6_image
      label: "镜头6图像"
      label_en: "Shot 6 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot6_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot6_img_prompt"
      with:
        prompt: "{{ outputs.shot6_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/6_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot6_video
      label: "镜头6视频"
      label_en: "Shot 6 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot6_vid_prompt, shot6_duration, reference_image, shot6_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot6_vid_prompt"
      on_failure: shot6_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot6_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/6_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/6_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot6_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot6_video_fallback
      label: "镜头6视频兜底"
      label_en: "Shot 6 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/6_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/6_shot.mp4"
        duration: "{{ outputs.shot6_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_7 image / video / fallback ----
    - id: shot7_image
      label: "镜头7图像"
      label_en: "Shot 7 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot7_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot7_img_prompt"
      with:
        prompt: "{{ outputs.shot7_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/7_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot7_video
      label: "镜头7视频"
      label_en: "Shot 7 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot7_vid_prompt, shot7_duration, reference_image, shot7_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot7_vid_prompt"
      on_failure: shot7_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot7_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/7_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/7_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot7_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot7_video_fallback
      label: "镜头7视频兜底"
      label_en: "Shot 7 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/7_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/7_shot.mp4"
        duration: "{{ outputs.shot7_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_8 image / video / fallback ----
    - id: shot8_image
      label: "镜头8图像"
      label_en: "Shot 8 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot8_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot8_img_prompt"
      with:
        prompt: "{{ outputs.shot8_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/8_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot8_video
      label: "镜头8视频"
      label_en: "Shot 8 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot8_vid_prompt, shot8_duration, reference_image, shot8_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot8_vid_prompt"
      on_failure: shot8_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot8_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/8_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/8_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot8_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot8_video_fallback
      label: "镜头8视频兜底"
      label_en: "Shot 8 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/8_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/8_shot.mp4"
        duration: "{{ outputs.shot8_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_9 image / video / fallback ----
    - id: shot9_image
      label: "镜头9图像"
      label_en: "Shot 9 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot9_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot9_img_prompt"
      with:
        prompt: "{{ outputs.shot9_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/9_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot9_video
      label: "镜头9视频"
      label_en: "Shot 9 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot9_vid_prompt, shot9_duration, reference_image, shot9_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot9_vid_prompt"
      on_failure: shot9_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot9_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/9_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/9_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot9_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot9_video_fallback
      label: "镜头9视频兜底"
      label_en: "Shot 9 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/9_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/9_shot.mp4"
        duration: "{{ outputs.shot9_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_10 image / video / fallback ----
    - id: shot10_image
      label: "镜头10图像"
      label_en: "Shot 10 image"
      kind: skill_exec
      skill: nano-banana-pro
      depends_on: [shot10_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot10_img_prompt"
      with:
        prompt: "{{ outputs.shot10_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/10_shot.png"
        aspect_ratio: "9:16"
        image_size: "1K"
        max_retries: 1
        fallback_model: "google/gemini-3-pro-image-preview"
        placeholder_on_fail: "yes"

    - id: shot10_video
      label: "镜头10视频"
      label_en: "Shot 10 video"
      kind: skill_exec
      skill: seedance-2-prompt
      depends_on: [shot10_vid_prompt, shot10_duration, reference_image, shot10_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '__SHOT_ABSENT__' not in outputs.shot10_vid_prompt"
      on_failure: shot10_video_fallback
      with:
        # Prepend Assets Mapping so seedance knows the role of each
        # input_reference image. Mirrors the upstream JiMeng prompt
        # convention (see references/recipes.md "Mode: All-Reference"):
        #   @image1 / reference[1] = identity anchor (full-cast lineup)
        #   @image2 / reference[2] = scene composition (this shot)
        # Keeping the preamble in English even when the shot directive
        # is Chinese — seedance parses English instruction prefixes
        # reliably regardless of the user-content language.
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast identity anchor (USE strictly for character likeness, faces, hair, skin tone, outfits, and accessories — keep these byte-identical to the reference across cuts). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot10_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/10_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/10_shot.png"
        aspect_ratio: "9:16"
        # `| int(5)` parses the duration extract as an integer, falling
        # back to 5 if the LLM emitted anything non-numeric (sentinel
        # __SHOT_ABSENT__, units like "10s", chain-of-thought text). A
        # raw truncate would slice "__SHOT_ABSENT__" to "__S" and crash
        # the downstream CLI's duration validator.
        duration: "{{ outputs.shot10_duration | int(5) }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot10_video_fallback
      label: "镜头10视频兜底"
      label_en: "Shot 10 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/10_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/10_shot.mp4"
        duration: "{{ outputs.shot10_duration | int(5) }}"
        width: 720
        height: 1280
        fps: 24

    # =========================================================================
    # Ending card image + 1.5s video.
    # =========================================================================
    - id: ending_image
      label: "结尾图"
      label_en: "Closing image"
      kind: skill_exec
      skill: title-card-image
      depends_on: [ending_text_extract, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        text: "{{ outputs.ending_text_extract | truncate(20) }}"
        subtitle: ""
        output: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/99_ending.png"
        background: "#0a0a10"
        text_color: "#e0e0e8"
        font_size: 96
        width: 720
        height: 1280

    - id: ending_video
      label: "结尾视频"
      label_en: "Closing video"
      kind: skill_exec
      skill: video-still-animator
      depends_on: [ending_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/99_ending.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/99_ending.mp4"
        duration: 2
        width: 720
        height: 1280
        fps: 24
        zoom_rate: 0.0005

    # =========================================================================
    # Stitch cover + shots(1..10 that exist) + ending. video-merger sorts
    # numeric prefix; 0_cover < 1..10_shot < 99_ending.
    # =========================================================================
    - id: merge
      label: "视频合并"
      label_en: "Video merge"
      kind: skill_exec
      skill: video-merger
      depends_on:
        - cover_video
        - shot1_video
        - shot2_video
        - shot3_video
        - shot4_video
        - shot5_video
        - shot6_video
        - shot7_video
        - shot8_video
        - shot9_video
        - shot10_video
        - ending_video
        - review_normalize
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        input_dir: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/final.mp4"
        mode: "full"
        transition: 0.5
        fps: 24
        crf: 22
        preset: "medium"

    - id: subtitles_srt
      label: "字幕 SRT"
      label_en: "Subtitle SRT"
      kind: skill_exec
      skill: srt-from-script
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        script: "{{ outputs.final_script }}"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/subs.srt"
        gap_ms: 200
        leading_offset_ms: 2000

    - id: subtitled_final
      label: "字幕成片"
      label_en: "Subtitled video"
      kind: skill_exec
      skill: subtitle-burner
      depends_on: [merge, subtitles_srt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        input: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/final.mp4"
        subtitles: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/subs.srt"
        output: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.user_message | slugify | truncate(40) }}/final_subtitled.mp4"
        font_size: 42
        margin_v: 80

    - id: deliver
      label: "交付"
      label_en: "Delivery"
      kind: llm_chat
      depends_on: [final_script, review_normalize, script_save]
      with:
        system: "Write a concise delivery message in the user's language. No emoji. Branch on DECISION."
        task: |
          Compose a 4-10 line summary tailored to the user's decision.

          User original request:
          {{ inputs.user_message | xml_escape | truncate(400) }}

          Decision marker:
          {{ outputs.review_normalize | truncate(400) }}

          Final script:
          {{ outputs.final_script | truncate(2500) }}

          Script saved at:
          {{ outputs.script_save | truncate(200) }}

          Merge output:
          {{ outputs.get('merge', '') | truncate(800) }}

          Subtitled-final output:
          {{ outputs.get('subtitled_final', '') | truncate(800) }}

          Branching rules:
          - If "DECISION: proceed":
              * Title (from final_script OVERVIEW.TITLE), shot count, total duration.
              * Headline path = subtitled_final (the burned-in subtitle MP4).
              * Also list: un-subtitled merge path, SRT path, script.txt path,
                folder containing intermediates.
              * Mention HAS_OVERRIDES if yes.
          - If "DECISION: cancel":
              * Acknowledge, note the script was still saved at script_save's
                path so it's not lost.
              * Offer to re-trigger.
          Respond in the same language as the user's original request.
---

# meta-short-drama

End-to-end short-drama generator with one free-form user-review gate
before any paid step. **1-10 shots** (default 5), title card + ending
card, in-language burned subtitles, and the generated script is saved
to disk regardless of outcome.

## What it does

1. **`intake_extract`** scans the user message for RENDER_STYLE,
   IDENTITY_ANCHOR, and N_SHOTS (1-10). Fills in defaults when missing.
2. **`script_draft`** calls `ai-video-script` with the inferred values
   pasted verbatim into every shot prompt.
3. **`review_gate`** — single free-form pause. The user can approve,
   rewrite render style / character / shot count / shot details, or
   cancel in plain language.
4. **`review_normalize`** parses the free-form reply.
5. **`script_revised`** (conditional) redrafts when overrides present.
6. **`final_script`** echoes the canonical script.
7. **`script_save`** writes `script.txt` to the run folder
   (always — even on cancel, so the user keeps the draft).
8. **`title_extract` / `subtitle_extract` / `ending_text_extract`**
   pull cover/ending text in the script's language.
9. **`cover_image` + `cover_video`** — Pillow title card + 2s Ken-Burns
   clip (`0_cover.mp4` — sorts first in merge).
10. **Per-shot extracts × 10** — for shots 1..10 the LLM emits either
    the real prompts/duration OR the literal sentinel `__SHOT_ABSENT__`.
    Image/video steps gate on the sentinel so unused slots stay dormant.
11. **Image generation per active shot** — `nano-banana-pro`, retry +
    fallback model + placeholder PNG (image step never aborts DAG).
12. **`reference_prompt_extract` + `reference_image`** — one extra
    `nano-banana-pro` call produces `reference.png`, a full-cast neutral
    lineup of every named character on a neutral backdrop. Used as the
    universal IDENTITY anchor for every shot's seedance call so the
    character does not drift across cuts (nano-banana would otherwise
    re-roll subtly different faces per shot).
13. **Video generation per active shot** — `seedance-2.0`, retry twice;
    on persistent refusal the Ken-Burns substitute fires using the
    shot's PNG. Each shot passes TWO reference images to seedance,
    AND the per-shot prompt is wrapped with an explicit "Assets
    Mapping" preamble in the upstream JiMeng convention so seedance
    knows the role of each reference:
      reference[1] = `reference.png` (full-cast identity anchor — used
                     strictly for character likeness / faces / hair /
                     outfits / accessories across all shots)
      reference[2] = `N_shot.png`    (this shot's scene composition
                     reference — used for camera angle, framing,
                     blocking, prop placement, background layout)
    The Assets Mapping preamble is in English even when the per-shot
    directive is Chinese — seedance parses English instruction prefixes
    reliably regardless of the user-content language. Empty / missing
    references are still filtered before the API call (so direct CLI
    callers using a single anchor remain backwards-compatible).
13. **`ending_image` + `ending_video`** — Pillow "完" / "THE END" card
    + 1.5s Ken-Burns clip (`99_ending.mp4` — sorts last).
14. **`merge`** — `video-merger` stitches `0_cover` + active shots
    + `99_ending` via numeric-prefix sort. ffmpeg cross-fade transitions.
15. **`subtitles_srt`** — SRT cues from VOICEOVER per shot, shifted by
    the 2-second cover duration so cue timing matches the merged
    timeline.
16. **`subtitled_final`** — `subtitle-burner` burns the SRT into
    `final_subtitled.mp4`.
17. **`deliver`** — always runs, branches on DECISION. Lists the saved
    script path so the user keeps a copy regardless.

## Outputs

```
<workspace>/meta_short_drama/<slug>/
    script.txt              # full final script (always)
    reference.png           # full-cast identity reference (used by every shot_video)
    0_cover.png  0_cover.mp4
    1_shot.png   1_shot.mp4   ┐
    2_shot.png   2_shot.mp4   ├ only for active shots (1..N_SHOTS)
    ...                       ┘
    99_ending.png 99_ending.mp4
    subs.srt
    final.mp4               # merged, no subtitles
    final_subtitled.mp4     # subtitled — the deliverable
```

## Dependencies

| Skill | Purpose | Models / Tools |
|---|---|---|
| `ai-video-script` | Structured shot list (1-10 shots) | LLM |
| `nano-banana-pro` | Per-shot first-frame PNG | OpenRouter Gemini 3.1 / 3 pro |
| `seedance-2-prompt` | Per-shot MP4 | OpenRouter Seedance 2.0 (or Volcengine ARK) |
| `video-still-animator` | Ken-Burns fallback / cover & ending clips | ffmpeg ≥ 5.0 |
| `video-merger` | Stitch cover + shots + ending | ffmpeg ≥ 5.0 |
| `srt-from-script` | VOICEOVER → SRT with cover offset | Python stdlib |
| `subtitle-burner` | Burn SRT into MP4 | ffmpeg + libass |
| `title-card-image` | Pillow cover + ending PNG cards | Pillow |
| (builtin) `write_file` | Save script.txt (no skill needed) | OpenSquilla builtin |
| `text-file-read` | Re-read script.txt after review pause | Python stdlib |

Environment:
- `OPENROUTER_API_KEY` must be set.
- `ffmpeg` and `ffprobe` on PATH.
- Pillow installed (already in opensquilla deps).

## Risk

`high` — writes files, spends real OpenRouter credits, runs ffmpeg
subprocesses. The review_gate ensures user consent before any paid step.

## Limits (v2)

- 1-10 shots; default 5. The DAG always declares 10 slots but
  `__SHOT_ABSENT__` gating keeps unused slots dormant.
- Per-shot duration follows the script's DURATION_S (clamped 3-15s by
  seedance API). Total drama length scales linearly.
- 9:16 portrait.
- Per-shot seedance failures fall back to Ken-Burns. Image step
  has its own placeholder fallback. Prompt-extract llm_chats still
  abort the run if they return malformed output.
- Concurrent runs with identical user_message collide on the same
  slug-derived subdir.

## When NOT to use

- Single image / single clip / script-only / stitch-only — use the
  underlying skills directly.
