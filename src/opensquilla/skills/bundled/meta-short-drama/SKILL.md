---
name: meta-short-drama
description: "Use this meta-skill instead of answering directly when the current user asks to generate an AI short-drama or 短剧 from a topic. The workflow infers render style, character identity, and shot count (1-10, default 5) from the request (filling in conservative defaults when missing), drafts a strict shot-by-shot shooting script, and pauses for a free-form review. A direct approval can continue; an adjustment only re-drafts and previews the script, then requires a second explicit approval before any external media call. After approval it generates one universal full-cast identity-reference image plus per-shot composition images, then per-shot video clips (each video anchored to BOTH the universal reference image and its own composition image so the character identity AND scene layout stay consistent), bookends them with a title card and an ending card, burns subtitles in the user's language, and saves the script alongside the final MP4. Do not use it for slide decks, document-decision analysis, single-image generation, isolated script writing, or pasted historical short-drama examples."
description_zh: "当用户要求根据主题生成 AI 短剧时，使用此元技能而非直接回答。工作流会推断渲染风格、角色身份和镜头数（1-10，默认 5），起草逐镜头脚本并暂停供自由评审。直接批准后继续；若用户提出调整，只会重新起草和预览脚本，并在任何外部媒体调用前再次要求明确批准。批准后生成通用全员身份参考图、逐镜构图图和视频片段，以双重参考保持角色与布局一致，再添加片头片尾、烧录用户语言字幕，并把脚本与最终 MP4 一并保存。不用于幻灯片、文档决策、单图生成、独立脚本写作或粘贴的历史短剧示例。"
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
    - "Pause for a free-form review; if edits are requested, show the revised script and require a second explicit approval before generating media."
    - "Keep shot count between 1 and 10 and use conservative defaults when unspecified."
  assumptions_zh:
    - "生成媒体前允许用户自由审阅；若提出修改，会先展示修订稿并再次要求明确批准。"
    - "镜头数量保持在 1 到 10 之间，未说明时使用保守默认值。"
  assumptions_en:
    - "Pause for a free-form review; if edits are requested, show the revised script and require a second explicit approval before generating media."
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
  platform:
    requires:
      bins: ["ffmpeg", "ffprobe"]
    install:
      - kind: toolchain
        id: media-ffmpeg
        label: "Install verified FFmpeg toolchain"
        bins: ["ffmpeg", "ffprobe"]
        os: [darwin, linux, windows]
  opensquilla:
    risk: high
    capabilities: [network-read, filesystem-write, process-control]
    composition_skills:
      - ai-video-script
      - short-drama-review-normalizer
      - nano-banana-pro
      - seedance-2-prompt
      - short-drama-delivery-audit
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
          Read the request and emit exactly this 8-line block, in this
          order, with no extra commentary:

          TOPIC: <one short line — the actual story/product topic>
          RENDER_STYLE: <render aesthetic, one line in user's language>
          AUTO_FILLED_RENDER_STYLE: <yes|no>
          STYLE_POLICY_WARNING: <none, or one concise warning in user's language>
          IDENTITY_ANCHOR: <one line in user's language describing main character(s)>
          AUTO_FILLED_IDENTITY_ANCHOR: <yes|no>
          N_SHOTS: <integer 1..10, default 5>
          AUTO_FILLED_N_SHOTS: <yes|no>

          Rules:
          - Detect dominant language of the request. Use that language for
            RENDER_STYLE and IDENTITY_ANCHOR. Downstream models accept
            Chinese natively (seedance is Chinese-first).
          - If user named a render style verbatim → copy it exactly,
            AUTO_FILLED_RENDER_STYLE: no. Never silently rewrite an explicit
            style. If that explicit style asks for photorealistic human footage,
            realistic human portraits, or a real-person likeness, emit this
            concise warning in the user's language in STYLE_POLICY_WARNING:
              Chinese: 写实人物参考可能被上游媒体提供商策略拒绝；若发生将停止重试并使用降级动效。
              English: Photoreal human references may be rejected by the upstream media provider; if so, retries stop and a degraded animation is used.
            Otherwise emit STYLE_POLICY_WARNING: none.
          - Else INFER a render style from the TOPIC's genre, era, and
            tone. For any human-led or urban story, prefer an unmistakably
            fictional stylized illustration, not photoreal footage or a
            real-person appearance. Pick whichever of these
            best fits the story you just read; fall through to a fresh
            descriptor if none match exactly. Use the user's language.
              * 现代职场 / 都市爽剧 / 商战 / 反转 / corporate drama →
                  虚构 2D 编辑插画, 图形小说阴影, 戏剧化高对比配色
                  / Clearly fictional 2D editorial illustration, graphic-novel shading, dramatic high-contrast palette
              * 古风 / 武侠 / 仙侠 / 宫廷 / wuxia / xianxia →
                  水墨风, 中国传统工笔画, 柔和留白构图
                  / Ink-wash painting, traditional Chinese gongbi style
              * 校园 / 青春 / 恋爱 / 治愈 / slice-of-life / romance →
                  虚构手绘青春插画, 柔和纸张纹理, 温暖调色
                  / Clearly fictional hand-drawn slice-of-life illustration, soft paper texture, warm palette
              * 科幻 / 赛博朋克 / 未来 / sci-fi / cyberpunk →
                  虚构 2D 科幻概念插画, 赛博朋克霓虹, 体积光雾气
                  / Clearly fictional 2D sci-fi concept illustration, cyberpunk neon, volumetric haze
              * 恐怖 / 悬疑 / 惊悚 / horror / thriller / noir →
                  虚构黑色图形小说插画, 高反差暗调, 风格化阴影
                  / Clearly fictional noir graphic-novel illustration, high contrast, stylized shadow
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
                matches the topic's mood. If people are central, it MUST say
                clearly fictional stylized illustration. Photography may only
                be auto-selected for non-human subjects such as products or food.
            AUTO_FILLED_RENDER_STYLE: yes
            STYLE_POLICY_WARNING: none
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
          DURATION_S always means STORY-CONTENT duration: the sum of the
          active shot durations. Default content duration: 50 (~10s per
          shot for the default 5 shots). The finished MP4 adds a fixed
          2s title card + 2s ending card, so its expected duration is
          content DURATION_S + 4s. ASPECT_RATIO: 9:16.

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
        path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/script.txt"
        content: "{{ outputs.script_draft }}"

    # =========================================================================
    # 3. Draft review gate — free-form. A direct approval authorizes media.
    #    An adjustment only authorizes a free re-draft; the revised preview
    #    gets its own explicit confirmation before any provider call.
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

          修改意见只会触发免费重拟稿，不代表同意调用媒体提供商。修改后
          会展示修订稿，并要求你再次明确说“继续生成”才会产生外部调用。

          本次待授权脚本的美元成本区间(选继续才会发生):
          {{ outputs.script_draft | short_drama_media_cost('zh') }}
          这个授权只适用于下方当前脚本的镜头数和计费剧情时长。

          数据边界：继续即表示你同意将脚本提示词和生成的参考图发送给
          已配置的外部图像/视频提供商。请勿上传或要求复刻未经授权的
          真人照片或其他个人敏感资料；写实人物输入可能被上游策略拒绝。

          时长说明: 脚本 DURATION_S 是剧情镜头总时长；最终成片还会
          固定加入 2 秒片头和 2 秒片尾。例如 3 秒剧情的成片约 7 秒。

          === 我做的假设 ===
          {{ outputs.intake_extract | truncate(1200) }}

          === 脚本草稿 ===
          {{ outputs.script_draft }}
        intro_zh: |
          脚本就绪。下面是脚本预览，以及我对风格、角色和分镜数做的假设。

          标 AUTO_FILLED: yes 的项是我替你填的，你可以改。脚本草稿已存到本次运行目录的 script.txt；如果你直接改文件，下一步会重新读盘并带入修改。

          你怎么回都行：满意就说“继续”；想换风格、角色、分镜数或某个镜头，直接说你的修改；不想做了就说“取消”。修改意见只会触发免费重拟稿，不代表同意调用媒体提供商；修改后会展示修订稿，并要求你再次明确说“继续生成”。

          本次待授权脚本的美元成本区间：
          {{ outputs.script_draft | short_drama_media_cost('zh') }}
          这个授权只适用于下方当前脚本的镜头数和计费剧情时长；只会在你选择继续后发生。

          数据边界：继续即表示你同意将脚本提示词和生成的参考图发送给已配置的外部图像/视频提供商。请勿上传或要求复刻未经授权的真人照片或其他个人敏感资料；写实人物输入可能被上游策略拒绝。

          时长说明：脚本 DURATION_S 是剧情镜头总时长；最终成片还会固定加入 2 秒片头和 2 秒片尾。例如 3 秒剧情的成片约 7 秒。

          === 我做的假设 ===
          {{ outputs.intake_extract | truncate(1200) }}

          === 脚本草稿 ===
          {{ outputs.script_draft }}
        intro_en: |
          The script is ready. Below is the script preview plus the assumptions I made about style, character identity, and shot count.

          Items marked AUTO_FILLED: yes were filled conservatively and can be changed. The draft script was saved to script.txt in this run directory; if you edit that file directly, the next step will reread it and include your manual edits.

          Reply naturally: say "continue" if it looks good, describe any style, character, shot-count, or shot-level changes, or say "cancel" to stop. An edit request only triggers a free re-draft and does not authorize any media provider call. After an edit, the revised preview requires a new explicit "continue generation" approval.

          USD cost range for the script awaiting authorization:
          {{ outputs.script_draft | short_drama_media_cost('en') }}
          This authorization applies only to the shot count and billable story duration in the current script below, and cost only occurs if you continue.

          Data boundary: continuing sends script prompts and generated reference images to the configured external image/video providers. Do not upload or request replication of unauthorized real-person photos or other personal sensitive data; photoreal human inputs may be rejected by upstream policy.

          Duration note: script DURATION_S is story-shot content time. The final MP4 adds a fixed 2-second title card and 2-second ending card; for example, 3 seconds of content produces an approximately 7-second final video.

          === Assumptions I made ===
          {{ outputs.intake_extract | truncate(1200) }}

          === Script draft ===
          {{ outputs.script_draft }}
        # A single string field already preserves a multi-line free-form reply.
        # Keep model extraction off so no prefill can be mistaken for consent.
        nl_extract: false
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
              all belong here. Empty replies remain invalid and never imply
              consent.
            prompt_zh: |
              用户对脚本草稿的整段回复。原样放进这个字段，不要总结、不要重写、不要解释。任何同意、拒绝、修改意见、吐槽或闲聊都属于这里。
            prompt_en: |
              The user's verbatim reply about the script draft. Copy the entire reply into this single field; do not summarize, paraphrase, translate, or split it. Approvals, rejections, edits, off-topic remarks, and empty replies all belong here.
            max_chars: 4000
        cancel_keywords: ["cancel", "取消", "算了", "停止", "stop", "abort"]
        timeout_hours: 24

    # =========================================================================
    # 4. Deterministically classify the first review. Explicit approval may
    #    proceed; recognizable adjustments emit DECISION: revise and cannot
    #    authorize external media; cancel/unclear replies fail closed.
    # =========================================================================
    - id: review_intent
      label: "审查意图"
      label_en: "Review intent"
      kind: skill_exec
      skill: short-drama-review-normalizer
      depends_on: [review_gate]
      with:
        payload:
          review: "{{ inputs.get('collected', {}).get('review_gate', {}).get('review', '') | truncate(4000) }}"

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
        input: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/script.txt"

    # =========================================================================
    # 5. Re-draft script when the user supplied adjustments. Free.
    # =========================================================================
    - id: script_revised
      label: "剧本修订"
      label_en: "Script revision"
      kind: agent
      skill: ai-video-script
      depends_on: [review_intent, script_reread]
      when: "'DECISION: revise' in outputs.review_intent and 'HAS_OVERRIDES: yes' in outputs.review_intent"
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

          NEW_NOTES is the user's verbatim requested adjustment. Apply style,
          identity, shot-count, and shot-detail instructions found there even
          when the corresponding normalized field says "unchanged".

          Previous script (re-read from disk — if the user hand-edited
          script.txt during review, those edits are already baked in
          here, so preserve them):
          {{ outputs.script_reread }}

          Parsed overrides:
          {{ outputs.review_intent | truncate(1500) }}

          User original request:
          {{ inputs.user_message | xml_escape | truncate(800) }}

    # =========================================================================
    # 5b. Any revision or direct file edit gets a second visible user-input
    #     gate. The preview and price are rendered from the exact immutable
    #     script snapshot that will later be saved and submitted.
    # =========================================================================
    - id: revision_confirm_gate
      label: "修订确认"
      label_en: "Revision confirmation"
      kind: user_input
      depends_on: [review_intent, script_draft, script_reread, script_revised]
      when: "'DECISION: revise' in outputs.review_intent or ('DECISION: proceed' in outputs.review_intent and outputs.script_reread != outputs.script_draft)"
      clarify:
        mode: form
        intro: |
          待执行脚本快照已就绪。请先审阅下面的完整预览。只有明确回复“继续生成”
          / "approve" / "proceed" 才会把提示词和参考图发送给已配置的
          外部媒体提供商并产生费用。修改意见本身从不代表授权。

          当前快照的美元成本区间(取代上一版估算):
          {{ (outputs.get('script_revised', '') or outputs.script_reread) | short_drama_media_cost('zh') }}
          你的新授权只适用于下方快照的镜头数和实际提供商计费剧情时长。
          确认后的文件修改不会改变本次执行快照。

          === 待执行脚本快照 ===
          {{ outputs.get('script_revised', '') or outputs.script_reread }}
        intro_zh: |
          待执行脚本快照已就绪。请审阅下面的预览。只有明确回复“继续生成”才会把提示词和参考图发送给已配置的外部媒体提供商并产生费用；修改意见本身从不代表授权。

          当前快照的美元成本区间(取代上一版估算):
          {{ (outputs.get('script_revised', '') or outputs.script_reread) | short_drama_media_cost('zh') }}
          你的新授权只适用于下方快照的镜头数和实际提供商计费剧情时长。确认后的文件修改不会改变本次执行快照。

          === 待执行脚本快照 ===
          {{ outputs.get('script_revised', '') or outputs.script_reread }}
        intro_en: |
          The exact script snapshot awaiting execution is ready. Review it below. Only a new explicit "continue generation", "approve", or "proceed" reply authorizes sending prompts and reference images to the configured external media providers and incurring cost. An edit request never counts as approval.

          Updated USD cost range for this snapshot (replaces the previous estimate):
          {{ (outputs.get('script_revised', '') or outputs.script_reread) | short_drama_media_cost('en') }}
          Your new authorization applies only to the shot count and actual provider-billed story duration in the snapshot below. File edits after confirmation do not change this execution snapshot.

          === Script snapshot awaiting execution ===
          {{ outputs.get('script_revised', '') or outputs.script_reread }}
        nl_extract: false
        fields:
          - name: review
            type: string
            required: true
            prompt: |
              用户对修订稿的整段确认回复。原样复制，不要总结或改写。
              Copy the user's complete confirmation reply verbatim. Do not
              summarize, paraphrase, translate, or infer approval.
            prompt_zh: "用户对修订稿的整段确认回复。原样复制，不要总结或改写。"
            prompt_en: "Copy the user's complete confirmation reply verbatim; do not summarize, paraphrase, translate, or infer approval."
            max_chars: 4000
        cancel_keywords: ["cancel", "取消", "算了", "停止", "stop", "abort"]
        timeout_hours: 24

    # =========================================================================
    # 5c. Final deterministic media-consent authority. If the first reply was
    #     an adjustment, only an explicit reply from revision_confirm_gate can
    #     produce DECISION: proceed. Missing/unclear/cancelled replies fail closed.
    # =========================================================================
    - id: review_normalize
      label: "审查归一"
      label_en: "Review normalization"
      kind: skill_exec
      skill: short-drama-review-normalizer
      depends_on: [review_intent, revision_confirm_gate]
      with:
        payload:
          phase: "media_approval"
          review: "{{ inputs.get('collected', {}).get('review_gate', {}).get('review', '') | truncate(4000) }}"
          confirmation: "{{ inputs.get('collected', {}).get('revision_confirm_gate', {}).get('review', '') | truncate(4000) }}"
          approval_snapshot_changed: "{{ outputs.script_reread != outputs.script_draft }}"

    # =========================================================================
    # 6. Persist the exact consented snapshot, then freeze that same scheduler
    #    value in memory. script.txt remains a user-visible artifact, but a
    #    post-approval file edit can never alter paid count/duration/arguments.
    # =========================================================================
    - id: script_save
      label: "保存剧本"
      label_en: "Save script"
      kind: tool_call
      tool: write_file
      tool_allowlist: [write_file]
      depends_on: [review_normalize, script_reread, script_revised]
      tool_args:
        path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/script.txt"
        content: "{{ outputs.get('script_revised', '') or outputs.script_reread }}"

    - id: final_script
      label: "最终剧本"
      label_en: "Final script"
      kind: skill_exec
      skill: short-drama-review-normalizer
      depends_on: [script_save, review_normalize]
      with:
        payload:
          phase: "canonical_script_snapshot"
          approval: "{{ outputs.review_normalize }}"
          script: "{{ outputs.get('script_revised', '') or outputs.script_reread }}"

    # =========================================================================
    # 8. Title / subtitle / ending text extracts (cheap llm_chat).
    # =========================================================================
    - id: title_extract
      label: "标题提取"
      label_en: "Title extraction"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          From the script, output exactly the value after "TITLE:"
          inside the "=== OVERVIEW ===" block. Single line.

          Script:
          {{ outputs.final_script | short_drama_section('OVERVIEW') }}

    - id: subtitle_extract
      label: "字幕提取"
      label_en: "Subtitle extraction"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          Compose a short subtitle for the cover card describing this
          drama in 5-12 characters (or 2-4 English words). Match the
          script's language. Examples:
            Chinese script → "AI 短剧 · 30 秒"
            English script → "AI Short Drama · 30s"

          Script (read OVERVIEW.TITLE / DURATION_S / AUDIENCE):
          {{ outputs.final_script | short_drama_section('OVERVIEW') }}

    - id: ending_text_extract
      label: "结尾文案"
      label_en: "Closing copy"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          Output the appropriate ending-card text. Single line, no commentary.
            Chinese script  → 完
            English script  → THE END
            Other languages → THE END

          Script (sample to detect language):
          {{ outputs.final_script | short_drama_section('OVERVIEW') }}

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
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize"
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

               <char 1 description>; <char 2 description>; ...; <char N description>, ALL <N> characters standing side by side in a horizontal full-body group lineup, every character clearly visible from head to toe, evenly spaced across frame, wide-angle full-cast lineup, neutral studio lighting, neutral light grey backdrop, no props, no background scene, character-design lineup composition, <OVERVIEW.RENDER_STYLE verbatim>, --ar 9:16

             - Use ; (semicolon) BETWEEN characters, exactly as in the
               examples above.
             - State the integer N explicitly inside "ALL <N> characters".
             - If N = 1, still say "ALL 1 character" and drop the
               "side by side / horizontal lineup" phrasing — write
               "single-character full-body portrait" instead.

          Output a single line. No quotes. No commentary outside the
          prompt itself.

          Reference-relevant context (from the exact full script snapshot;
          includes every SHOT_N block, not just OVERVIEW):
          {{ outputs.final_script | short_drama_reference_context }}

    - id: reference_image
      label: "参考图"
      label_en: "Reference image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [reference_prompt_extract, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid)"
      with:
        prompt: "{{ outputs.reference_prompt_extract | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
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
        output: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/0_cover.png"
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
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/0_cover.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/0_cover.mp4"
        duration: 2
        width: 720
        height: 1280
        fps: 24
        zoom_rate: 0.0008

    # ---- SHOT_1 extracts (deterministically skip absent script blocks) ----
    - id: shot1_img_prompt
      label: "镜头1图提示"
      label_en: "Shot 1 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_1 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_1 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 1):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_1') }}

    - id: shot1_vid_prompt
      label: "镜头1视频提示"
      label_en: "Shot 1 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_1 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_1 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_1') }}

    # ---- SHOT_2 extracts (deterministically skip absent script blocks) ----
    - id: shot2_img_prompt
      label: "镜头2图提示"
      label_en: "Shot 2 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_2 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_2 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 2):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_2') }}

    - id: shot2_vid_prompt
      label: "镜头2视频提示"
      label_en: "Shot 2 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_2 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_2 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_2') }}

    # ---- SHOT_3 extracts (deterministically skip absent script blocks) ----
    - id: shot3_img_prompt
      label: "镜头3图提示"
      label_en: "Shot 3 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_3 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_3 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 3):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_3') }}

    - id: shot3_vid_prompt
      label: "镜头3视频提示"
      label_en: "Shot 3 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_3 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_3 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_3') }}

    # ---- SHOT_4 extracts (deterministically skip absent script blocks) ----
    - id: shot4_img_prompt
      label: "镜头4图提示"
      label_en: "Shot 4 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_4 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_4 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 4):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_4') }}

    - id: shot4_vid_prompt
      label: "镜头4视频提示"
      label_en: "Shot 4 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_4 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_4 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_4') }}

    # ---- SHOT_5 extracts (deterministically skip absent script blocks) ----
    - id: shot5_img_prompt
      label: "镜头5图提示"
      label_en: "Shot 5 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_5 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_5 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 5):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_5') }}

    - id: shot5_vid_prompt
      label: "镜头5视频提示"
      label_en: "Shot 5 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_5 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_5 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_5') }}

    # ---- SHOT_6 extracts (deterministically skip absent script blocks) ----
    - id: shot6_img_prompt
      label: "镜头6图提示"
      label_en: "Shot 6 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_6 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_6 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 6):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_6') }}

    - id: shot6_vid_prompt
      label: "镜头6视频提示"
      label_en: "Shot 6 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_6 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_6 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_6') }}

    # ---- SHOT_7 extracts (deterministically skip absent script blocks) ----
    - id: shot7_img_prompt
      label: "镜头7图提示"
      label_en: "Shot 7 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_7 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_7 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 7):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_7') }}

    - id: shot7_vid_prompt
      label: "镜头7视频提示"
      label_en: "Shot 7 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_7 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_7 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_7') }}

    # ---- SHOT_8 extracts (deterministically skip absent script blocks) ----
    - id: shot8_img_prompt
      label: "镜头8图提示"
      label_en: "Shot 8 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_8 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_8 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 8):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_8') }}

    - id: shot8_vid_prompt
      label: "镜头8视频提示"
      label_en: "Shot 8 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_8 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_8 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_8') }}

    # ---- SHOT_9 extracts (deterministically skip absent script blocks) ----
    - id: shot9_img_prompt
      label: "镜头9图提示"
      label_en: "Shot 9 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_9 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_9 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 9):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_9') }}

    - id: shot9_vid_prompt
      label: "镜头9视频提示"
      label_en: "Shot 9 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_9 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_9 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_9') }}

    # ---- SHOT_10 extracts (deterministically skip absent script blocks) ----
    - id: shot10_img_prompt
      label: "镜头10图提示"
      label_en: "Shot 10 image prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_10 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_10 ===" block:
            output exactly the value after "IMAGE_PROMPT:" inside that block.
            Single line, no quotes, no label.
          If it does NOT (because N_SHOTS < 10):
            output exactly the literal sentinel: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_10') }}

    - id: shot10_vid_prompt
      label: "镜头10视频提示"
      label_en: "Shot 10 video prompt"
      kind: llm_chat
      depends_on: [final_script, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '=== SHOT_10 ===' in outputs.final_script.splitlines()"
      with:
        system: "Return one line of text. No quotes, no prefix, no commentary."
        task: |
          If the script contains a "=== SHOT_10 ===" block:
            output exactly the value after "VIDEO_PROMPT:" inside that block.
            Single line.
          If it does NOT: output exactly: __SHOT_ABSENT__

          Script:
          {{ outputs.final_script | short_drama_section('SHOT_10') }}

    # ---- SHOT_1 image / video / fallback ----
    - id: shot1_image
      label: "镜头1图像"
      label_en: "Shot 1 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot1_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_1 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot1_img_prompt"
      with:
        prompt: "{{ outputs.shot1_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/1_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot1_vid_prompt, reference_image, shot1_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_1 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot1_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot1_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/1_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/1_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_1') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot1_video_fallback
      label: "镜头1视频兜底"
      label_en: "Shot 1 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/1_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/1_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_1') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_2 image / video / fallback ----
    - id: shot2_image
      label: "镜头2图像"
      label_en: "Shot 2 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot2_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_2 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot2_img_prompt"
      with:
        prompt: "{{ outputs.shot2_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/2_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot2_vid_prompt, reference_image, shot2_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_2 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot2_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot2_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/2_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/2_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_2') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot2_video_fallback
      label: "镜头2视频兜底"
      label_en: "Shot 2 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/2_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/2_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_2') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_3 image / video / fallback ----
    - id: shot3_image
      label: "镜头3图像"
      label_en: "Shot 3 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot3_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_3 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot3_img_prompt"
      with:
        prompt: "{{ outputs.shot3_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/3_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot3_vid_prompt, reference_image, shot3_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_3 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot3_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot3_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/3_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/3_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_3') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot3_video_fallback
      label: "镜头3视频兜底"
      label_en: "Shot 3 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/3_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/3_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_3') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_4 image / video / fallback ----
    - id: shot4_image
      label: "镜头4图像"
      label_en: "Shot 4 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot4_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_4 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot4_img_prompt"
      with:
        prompt: "{{ outputs.shot4_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/4_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot4_vid_prompt, reference_image, shot4_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_4 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot4_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot4_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/4_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/4_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_4') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot4_video_fallback
      label: "镜头4视频兜底"
      label_en: "Shot 4 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/4_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/4_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_4') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_5 image / video / fallback ----
    - id: shot5_image
      label: "镜头5图像"
      label_en: "Shot 5 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot5_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_5 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot5_img_prompt"
      with:
        prompt: "{{ outputs.shot5_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/5_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot5_vid_prompt, reference_image, shot5_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_5 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot5_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot5_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/5_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/5_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_5') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot5_video_fallback
      label: "镜头5视频兜底"
      label_en: "Shot 5 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/5_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/5_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_5') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_6 image / video / fallback ----
    - id: shot6_image
      label: "镜头6图像"
      label_en: "Shot 6 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot6_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_6 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot6_img_prompt"
      with:
        prompt: "{{ outputs.shot6_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/6_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot6_vid_prompt, reference_image, shot6_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_6 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot6_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot6_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/6_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/6_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_6') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot6_video_fallback
      label: "镜头6视频兜底"
      label_en: "Shot 6 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/6_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/6_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_6') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_7 image / video / fallback ----
    - id: shot7_image
      label: "镜头7图像"
      label_en: "Shot 7 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot7_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_7 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot7_img_prompt"
      with:
        prompt: "{{ outputs.shot7_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/7_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot7_vid_prompt, reference_image, shot7_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_7 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot7_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot7_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/7_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/7_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_7') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot7_video_fallback
      label: "镜头7视频兜底"
      label_en: "Shot 7 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/7_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/7_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_7') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_8 image / video / fallback ----
    - id: shot8_image
      label: "镜头8图像"
      label_en: "Shot 8 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot8_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_8 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot8_img_prompt"
      with:
        prompt: "{{ outputs.shot8_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/8_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot8_vid_prompt, reference_image, shot8_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_8 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot8_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot8_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/8_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/8_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_8') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot8_video_fallback
      label: "镜头8视频兜底"
      label_en: "Shot 8 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/8_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/8_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_8') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_9 image / video / fallback ----
    - id: shot9_image
      label: "镜头9图像"
      label_en: "Shot 9 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot9_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_9 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot9_img_prompt"
      with:
        prompt: "{{ outputs.shot9_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/9_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot9_vid_prompt, reference_image, shot9_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_9 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot9_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot9_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/9_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/9_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_9') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot9_video_fallback
      label: "镜头9视频兜底"
      label_en: "Shot 9 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/9_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/9_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_9') }}"
        width: 720
        height: 1280
        fps: 24

    # ---- SHOT_10 image / video / fallback ----
    - id: shot10_image
      label: "镜头10图像"
      label_en: "Shot 10 image"
      kind: skill_exec
      skill: nano-banana-pro
      side_effect: external_paid_submit
      depends_on: [shot10_img_prompt, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_10 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot10_img_prompt"
      with:
        prompt: "{{ outputs.shot10_img_prompt | truncate(800) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/10_shot.png"
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
      side_effect: external_paid_submit
      depends_on: [shot10_vid_prompt, reference_image, shot10_image, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and (outputs.final_script | short_drama_duration_contract_valid) and '=== SHOT_10 ===' in outputs.final_script.splitlines() and '__SHOT_ABSENT__' not in outputs.shot10_vid_prompt"
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
        prompt: "Mode: All-Reference. Assets Mapping: reference[1] is the full-cast fictional character-design anchor (USE for silhouette, hairstyle, costumes, and accessories; preserve the original design while allowing natural motion and expression; never infer or reproduce a real-person likeness). reference[2] is THIS shot's scene composition reference (USE for camera angle, framing, character blocking, prop placement, and background layout). Shot directive: {{ outputs.shot10_vid_prompt | truncate(700) }}"
        filename: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/10_shot.mp4"
        input_image: ""
        input_reference: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/reference.png"
        input_reference_2: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/10_shot.png"
        aspect_ratio: "9:16"
        # Parse the exact unique DURATION_S from the approved script with
        # the same strict local contract used for consent-time pricing.
        # Missing, repeated, non-integer, or out-of-range values fail closed.
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_10') }}"
        model: "bytedance/seedance-2.0"
        max_retries: 2

    - id: shot10_video_fallback
      label: "镜头10视频兜底"
      label_en: "Shot 10 video fallback"
      kind: skill_exec
      skill: video-still-animator
      with:
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/10_shot.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/10_shot.mp4"
        duration: "{{ outputs.final_script | short_drama_shot_duration('SHOT_10') }}"
        width: 720
        height: 1280
        fps: 24

    # =========================================================================
    # Ending card image + 2s video.
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
        output: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/99_ending.png"
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
        input_image: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/99_ending.png"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/99_ending.mp4"
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
        input_dir: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}"
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/final.mp4"
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
        output_path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/subs.srt"
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
        input: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/final.mp4"
        subtitles: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/subs.srt"
        output: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/final_subtitled.mp4"
        font_size: 42
        margin_v: 80

    # =========================================================================
    # Deterministic delivery gate. It parses the canonical script and receipt
    # JSON files, incorporates runtime fallback evidence plus the bounded
    # parent-owned paid-submission dispositions, then uses ffprobe to verify
    # every active shot and the final MP4. The delivery LLM may only restate
    # this structured verdict; it never decides provenance itself.
    # =========================================================================
    - id: delivery_audit
      label: "交付真实性与时长校验"
      label_en: "Delivery provenance and duration audit"
      kind: skill_exec
      skill: short-drama-delivery-audit
      depends_on:
        - final_script
        - reference_image
        - subtitled_final
        - shot1_image
        - shot1_video
        - shot1_video_fallback
        - shot2_image
        - shot2_video
        - shot2_video_fallback
        - shot3_image
        - shot3_video
        - shot3_video_fallback
        - shot4_image
        - shot4_video
        - shot4_video_fallback
        - shot5_image
        - shot5_video
        - shot5_video_fallback
        - shot6_image
        - shot6_video
        - shot6_video_fallback
        - shot7_image
        - shot7_video
        - shot7_video_fallback
        - shot8_image
        - shot8_video
        - shot8_video_fallback
        - shot9_image
        - shot9_video
        - shot9_video_fallback
        - shot10_image
        - shot10_video
        - shot10_video_fallback
        - review_normalize
      when: "'DECISION: proceed' in outputs.review_normalize"
      with:
        run_dir: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}"
        runtime:
          paid_submission_dispositions: "{{ outputs.get('__opensquilla_paid_submission_dispositions_v1__', '{}') | truncate(8000) }}"
          paid_submission_receipt_proofs: "{{ outputs.get('__opensquilla_paid_submission_receipt_proofs_v1__', '{}') | truncate(8000) }}"
          fallback_outputs:
            "1": "{{ outputs.get('shot1_video_fallback', '') | truncate(400) }}"
            "2": "{{ outputs.get('shot2_video_fallback', '') | truncate(400) }}"
            "3": "{{ outputs.get('shot3_video_fallback', '') | truncate(400) }}"
            "4": "{{ outputs.get('shot4_video_fallback', '') | truncate(400) }}"
            "5": "{{ outputs.get('shot5_video_fallback', '') | truncate(400) }}"
            "6": "{{ outputs.get('shot6_video_fallback', '') | truncate(400) }}"
            "7": "{{ outputs.get('shot7_video_fallback', '') | truncate(400) }}"
            "8": "{{ outputs.get('shot8_video_fallback', '') | truncate(400) }}"
            "9": "{{ outputs.get('shot9_video_fallback', '') | truncate(400) }}"
            "10": "{{ outputs.get('shot10_video_fallback', '') | truncate(400) }}"

    - id: publish_final_video
      label: "发布字幕成片"
      label_en: "Publish subtitled video"
      kind: tool_call
      tool: publish_artifact
      tool_allowlist: [publish_artifact]
      depends_on: [subtitled_final, delivery_audit, review_normalize]
      when: "'DECISION: proceed' in outputs.review_normalize and '\"status\": \"blocked\"' not in outputs.delivery_audit"
      tool_args:
        path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/final_subtitled.mp4"
        name: "final_subtitled.mp4"
        mime: "video/mp4"

    - id: publish_script
      label: "发布剧本"
      label_en: "Publish script"
      kind: tool_call
      tool: publish_artifact
      tool_allowlist: [publish_artifact]
      depends_on: [script_save]
      tool_args:
        path: "{{ inputs.workspace_dir }}/meta_short_drama/{{ inputs.meta_run_id }}/script.txt"
        name: "script.txt"
        mime: "text/plain"

    - id: deliver
      label: "交付"
      label_en: "Delivery"
      kind: llm_chat
      depends_on:
        - final_script
        - review_normalize
        - script_save
        - merge
        - subtitles_srt
        - subtitled_final
        - delivery_audit
        - publish_final_video
        - publish_script
      with:
        system: "Write a concise delivery message in the user's language. No emoji. Branch on DECISION. DELIVERY_AUDIT_JSON is the sole authority for provenance, decode status, provider identifiers, and durations; only restate it and never create your own verdict."
        task: |
          Compose a 5-12 line summary tailored to the user's decision.

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

          Published final-video artifact:
          {{ outputs.get('publish_final_video', '') | truncate(800) }}

          Published script artifact:
          {{ outputs.get('publish_script', '') | truncate(800) }}

          DELIVERY_AUDIT_JSON (machine-owned, sole authority):
          {{ outputs.get('delivery_audit', '') | truncate(12000) }}

          Branching rules:
          - If "DECISION: proceed":
              * Title (from final_script OVERVIEW.TITLE) and active shot count.
              * Report two distinct durations from DELIVERY_AUDIT_JSON:
                content_duration_s is the story-shot content duration;
                final_duration_s is the probed finished-MP4 duration including
                the fixed 2s title card and 2s ending card. Never call a 7s
                finished MP4 a 3s final video.
              * If audit status is verified or degraded, headline path =
                subtitled_final (the burned-in subtitle MP4) and confirm the
                published video + script artifacts from their publication
                results. If status is blocked, say the final video was not
                published and do not present its path as a usable deliverable;
                the script artifact remains available.
              * Do not invent or print URLs; use artifact ids/names from the
                publication results.
              * Also list: un-subtitled merge path, SRT path, script.txt path,
                folder containing intermediates.
              * Mention HAS_OVERRIDES if yes.
              * Include exactly one line beginning "Media provenance:" and
                copy the value of DELIVERY_AUDIT_JSON.media_provenance.
              * Never upgrade degraded/blocked to verified and never infer
                API success from paths, step output, or the script.
              * List the audit issue codes/assets when status is degraded or
                blocked. A fallback or missing provider receipt must remain
                explicitly non-verified even if the MP4 is playable.
              * If DELIVERY_AUDIT_JSON.may_have_been_billed is true, state that
                provider acceptance and billing are unknown, list only the
                sanitized paid_submission_status_unknown_assets, and tell the
                user to check provider history before starting a replacement
                generation. Never expose fallback output or raw failure text.
              * Assets listed in DELIVERY_AUDIT_JSON.safe_no_submit_assets were
                proven by the parent runtime to have failed before provider
                submission. Do not warn that those assets may have been billed.
              * If DELIVERY_AUDIT_JSON.unexpected_paid_assets is non-empty,
                state that paid-media evidence exists outside the canonical
                script's active shots and list only those sanitized asset names.
              * For VIDEO_POLICY_REJECTED, explain that the upstream media
                provider rejected that shot under policy. Copy only the
                sanitized reason/policy_code from DELIVERY_AUDIT_JSON; never
                expose raw provider text, URLs, tokens, or request identifiers.
              * Provider/model/request_id/job_id may only come from the
                sanitized fields already present in DELIVERY_AUDIT_JSON.
          - If "DECISION: cancel":
              * Acknowledge, note the script was still saved at script_save's
                path so it's not lost.
              * Offer to re-trigger.
          - If "DECISION: hold":
              * If CONSENT_BASIS is external_transfer_refused, state that the
                user declined external transfer. If CONSENT_BASIS is
                generation_deferred, state that the user explicitly postponed
                generation and can approve it later. Otherwise state that the
                reply was not clear enough to authorize sending prompts or
                reference images to external media providers.
              * State that no image/video generation was submitted and the
                draft script was still saved and published.
              * Ask the user to re-trigger and explicitly approve or provide a
                concrete style, character, shot-count, or shot-detail change.
          Respond in the same language as the user's original request.
---

# meta-short-drama

End-to-end short-drama generator with an explicit-consent review flow before
any paid external-media step. **1-10 shots** (default 5), title card + ending
card, in-language burned subtitles, and the generated script is saved to disk
regardless of outcome. A direct approval continues immediately; an edit only
produces a revised preview and requires a second explicit approval.

## What it does

1. **`intake_extract`** scans the user message for RENDER_STYLE,
   IDENTITY_ANCHOR, and N_SHOTS (1-10). Fills in defaults when missing.
2. **`script_draft`** calls `ai-video-script` with the inferred values
   pasted verbatim into every shot prompt.
3. **`review_gate`** — free-form draft review. The user can approve,
   request changes to render style / character / shot count / shot details,
   or cancel in plain language.
4. **`review_intent`** is local and deterministic. Explicit approval may
   proceed, while a recognizable adjustment emits `DECISION: revise`; the
   adjustment never authorizes an external call.
5. **`script_revised`** (conditional) applies requested overrides, then
   **`revision_confirm_gate`** shows the revised preview and requires a new
   explicit approval. **`review_normalize`** is the final paid-media consent
   authority; cancel, missing, ambiguous, off-topic, and further-edit replies
   fail closed without provider calls.
6. **`final_script`** freezes the canonical scheduler snapshot in memory; it
   never re-reads the user-editable artifact.
7. **`script_save`** writes that same canonical content to `script.txt` in the run folder
   (always — even on cancel, so the user keeps the draft).
8. **`title_extract` / `subtitle_extract` / `ending_text_extract`**
   pull cover/ending text in the script's language.
9. **`cover_image` + `cover_video`** — Pillow title card + 2s Ken-Burns
   clip (`0_cover.mp4` — sorts first in merge).
10. **Per-shot extracts × 10** — all slots are declared, but an exact
    `=== SHOT_N ===` header check deterministically skips absent script blocks
    before calling the LLM. Active extracts still use `__SHOT_ABSENT__` as a
    second fail-closed guard, and paid image/video steps repeat the exact-header
    check so an LLM cannot activate an unused slot.
11. **Image generation per active shot** — `nano-banana-pro`, at most one paid
    submit, followed by a local placeholder PNG on a verified policy refusal
    (the image step never aborts the DAG). Provider responses and ambiguous
    transport outcomes never trigger an automatic second paid request.
12. **`reference_prompt_extract` + `reference_image`** — one extra
    `nano-banana-pro` call produces `reference.png`, a full-cast neutral
    lineup of every named character on a neutral backdrop. Used as the
    universal IDENTITY anchor for every shot's seedance call so the
    character does not drift across cuts (nano-banana would otherwise
    re-roll subtly different character designs per shot).
13. **Video generation per active shot** — `seedance-2.0`; paid submit
    failures are never retried automatically because an ambiguous response may
    already represent a billed job. After a job id is issued, transient polling
    failures may retry that same job up to the configured limit. Any provider-policy
    refusals stop immediately without another paid submission. The Ken-Burns
    substitute then fires using the
    shot's PNG. Each shot passes TWO reference images to seedance,
    AND the per-shot prompt is wrapped with an explicit "Assets
    Mapping" preamble in the upstream JiMeng convention so seedance
    knows the role of each reference:
      reference[1] = `reference.png` (full-cast fictional design anchor —
                     preserves silhouette / hairstyle / costumes /
                     accessories without reproducing real-person likeness)
      reference[2] = `N_shot.png`    (this shot's scene composition
                     reference — used for camera angle, framing,
                     blocking, prop placement, background layout)
    The Assets Mapping preamble is in English even when the per-shot
    directive is Chinese — seedance parses English instruction prefixes
    reliably regardless of the user-content language. Empty / missing
    references are still filtered before the API call (so direct CLI
    callers using a single anchor remain backwards-compatible).
13. **`ending_image` + `ending_video`** — Pillow "完" / "THE END" card
    + 2s Ken-Burns clip (`99_ending.mp4` — sorts last).
14. **`merge`** — `video-merger` stitches `0_cover` + active shots
    + `99_ending` via numeric-prefix sort. ffmpeg cross-fade transitions.
15. **`subtitles_srt`** — SRT cues from VOICEOVER per shot, shifted by
    the 2-second cover duration so cue timing matches the merged
    timeline.
16. **`subtitled_final`** — `subtitle-burner` burns the SRT into
    `final_subtitled.mp4`.
17. **`publish_final_video` + `publish_script`** — register the final
    MP4 (`video/mp4`) and script (`text/plain`) with the active surface
    so browser users receive artifact controls instead of only a local
    path. The script is published even when the user cancels; the video
    is published only after a successful proceed path.
18. **`delivery_audit`** — deterministic receipt/fallback/ffprobe gate.
    It combines validated receipts with the scheduler's bounded, parent-owned
    paid-submission dispositions (`safe_no_submit`, `maybe_accepted`, or
    `receipt`; only a conclusive receipt becomes `confirmed`). It is the sole
    authority for API provenance and reports both story content duration and
    the probed final duration (content + 4s bookends). A fallback after a
    proven pre-submit failure does not trigger a billing warning; ambiguous
    submission outcomes still emit only a sanitized asset list and
    check-history warning.
19. **`deliver`** — always runs, branches on DECISION, and waits for
    the deterministic audit before composing delivery. It only restates
    the machine-owned verdict and cannot promote fallback media to a
    verified real-API result.

## Outputs

```
<workspace>/meta_short_drama/<meta_run_id>/
    script.txt              # full final script (always; published artifact)
    reference.png           # full-cast identity reference (used by every shot_video)
    0_cover.png  0_cover.mp4
    1_shot.png   1_shot.mp4   ┐
    2_shot.png   2_shot.mp4   ├ only for active shots (1..N_SHOTS)
    ...                       ┘
    *.png.receipt.json       # image provider/request or placeholder status
    *.mp4.receipt.json       # video provider/model/job status
    99_ending.png 99_ending.mp4
    subs.srt
    final.mp4               # merged, no subtitles
    final_subtitled.mp4     # subtitled — published video deliverable
```

## Dependencies

| Skill | Purpose | Models / Tools |
|---|---|---|
| `ai-video-script` | Structured shot list (1-10 shots) | LLM |
| `short-drama-review-normalizer` | Local fail-closed review/consent decision | Python stdlib |
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
subprocesses. The review gate plus deterministic normalizer ensures explicit
approval or a meaningful requested adjustment before any external media step.

## Limits (v2)

- 1-10 shots; default 5. The DAG always declares 10 slots but
  `__SHOT_ABSENT__` gating keeps unused slots dormant.
- Per-shot duration follows the script's DURATION_S (clamped 3-15s by
  seedance API). OVERVIEW.DURATION_S means story-shot content duration;
  the final MP4 adds a fixed 2s title + 2s ending (content + 4s).
- 9:16 portrait.
- Per-shot seedance failures fall back to Ken-Burns. Image step
  has its own placeholder fallback. Both are explicitly reported as
  degraded and cannot satisfy the verified-real-API E2E status.
  Prompt-extract llm_chats still abort the run if they return malformed
  output.
- Every run uses its runtime-owned `meta_run_id` subdirectory, so concurrent
  runs and post-review `additional_notes` cannot redirect or collide outputs.

## When NOT to use

- Single image / single clip / script-only / stitch-only — use the
  underlying skills directly.
