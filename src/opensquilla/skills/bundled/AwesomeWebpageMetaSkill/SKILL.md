---
name: AwesomeWebpageMetaSkill
description: "Build a local multimedia webpage project from a user topic, audience, language, style, and media preferences by framing requirements, researching, planning, acquiring media, generating files, packaging, validating, repairing, and delivering usage guidance."
description_zh: "根据用户给定的主题、受众、语言、风格和媒体偏好，通过梳理需求、调研、规划、获取媒体、生成文件、打包、校验、修复并交付使用指引，构建一个本地多媒体网页项目。"
kind: meta
meta_priority: 65
always: false
final_text_mode: "step:delivery_guide"
request_template:
  outcome: "A packaged local multimedia webpage project with assets, validation notes, and usage guidance."
  outcome_zh: "打包好的本地多媒体网页项目，包含素材、验证说明和使用指南。"
  outcome_en: "A packaged local multimedia webpage project with assets, validation notes, and usage guidance."
  fields:
    - name: topic
      label: "Topic"
      label_zh: "网页主题"
      label_en: "Topic"
    - name: target_audience
      label: "Target audience"
      label_zh: "目标受众"
      label_en: "Target audience"
    - name: output_language
      label: "Output language"
      label_zh: "输出语言"
      label_en: "Output language"
    - name: visual_style
      label: "Visual style"
      label_zh: "视觉风格"
      label_en: "Visual style"
    - name: media_preferences
      label: "Media preferences"
      label_zh: "媒体偏好"
      label_en: "Media preferences"
triggers:
  - "create awesome webpage"
  - "build multimedia webpage project"
  - "生成图文音视频网页"
  - "做一个多媒体网页项目"
  - "AwesomeWebpageMetaSkill"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: high
    capabilities: [network-read, network-write, filesystem-read, filesystem-write, command-exec]
    requires:
      config:
        - awesome_webpage.provider
        - awesome_webpage.openrouter.api_key
        - awesome_webpage.openrouter.models.page_generation
        - awesome_webpage.openrouter.models.image_generation
        - awesome_webpage.openrouter.models.audio_generation
        - awesome_webpage.openrouter.models.video_generation
        - awesome_webpage.output_dir
        - awesome_webpage.media_strategy
config:
  awesome_webpage:
    provider: openrouter
    openrouter:
      api_key: null
      api_key_env: OPENROUTER_API_KEY
      base_url: https://openrouter.ai/api/v1
      models:
        page_generation: moonshotai/kimi-k2.6
        image_generation: google/gemini-3-pro-image-preview
        audio_generation: openai/gpt-audio-mini
        video_generation: bytedance/seedance-2.0-fast
    output_dir: "{{ inputs.workspace_dir }}/awesome-webpage-output"
    media_strategy:
      search_first: true
      aigc_fallback_when_search_empty: true
      allow_remote_embeds: false
      default_modalities: [text, images, audio, video]
      search_modalities: [images]
      direct_aigc_modalities: [audio, video]
      confirmation_steps: [ask_images, ask_audio, ask_video, ask_style]
      aigc_policy: search_images_direct_generate_audio_video
      placeholder_policy: visible_replacement_slot_when_generation_unavailable
      target_assets:
        images: 6
        audio: 1
        video: 1
      licensing: prefer_reusable_or_user_supplied
    clawhub_skills:
      web_search:
        skill: web-search
        url: https://clawhub.ai/billyutw/web-search
      image_generation:
        skill: nano-banana-pro-openrouter
        url: https://clawhub.ai/skills/nano-banana-pro-openrouter
        opensquilla_compatibility: deterministic-skill-exec
        notes: |
          The image_aigc step runs this deterministic skill_exec adapter.
          It does not invoke a prompt-building skill as an agent. No
          GEMINI_API_KEY required; only OPENROUTER_API_KEY.
      audio_generation:
        skill: audio-cog
        url: https://clawhub.ai/skills/audio-cog
        opensquilla_compatibility: openrouter-config-first
      video_generation:
        skill: openrouter-video-generator
        url: openrouter-configured-video-generation
      webpage_generation:
        skill: html-coder
        url: https://clawhub.ai/jhauga/html-coder
        opensquilla_compatibility: scoped-agent
        notes: |
          Invoked by the webpage_generation step as the HTML/CSS/JS authoring
          skill. The meta-skill task constrains it to source JSON only;
          packaging, validation, repair, and media generation remain separate
          steps.
      filesystem:
        skill: filesystem
        url: https://clawhub.ai/gtrusler/clawdbot-filesystem
composition:
  steps:
    - id: requirement_framing
      kind: llm_chat
      with:
        system: |
          You are the Requirement Framing stage for AwesomeWebpageMetaSkill.
          Produce a compact, structured brief. Do not call tools.
        task: |
          {{ inputs.language_instruction }}

          Extract and normalize the user's webpage request.

          Required fields:
          - topic
          - target_audience
          - output_language
          - visual_style
          - media_preferences: infer the user's requested images, audio, video, remote embeds, and local-only needs
          - configured_output_dir: use the resolved config below; only mark CONFIG_NEEDED if it is empty
          - constraints and risks

          Do not invent provider names, model ids, API keys, output paths, or media strategy values.
          They must come from the `awesome_webpage` config.
          A missing OpenRouter model value means CONFIG_NEEDED for that generator; it does not mean
          the user does not want that media modality. Do not mark audio or video as unwanted only
          because its configured model is null.

          Resolved awesome_webpage config for this run:
          provider: openrouter
          openrouter.api_key_env: OPENROUTER_API_KEY
          openrouter.models.page_generation: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('page_generation', 'moonshotai/kimi-k2.6') }}
          openrouter.models.image_generation: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('image_generation', 'google/gemini-3-pro-image-preview') }}
          openrouter.models.audio_generation: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('audio_generation', 'openai/gpt-audio-mini') }}
          openrouter.models.video_generation: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('video_generation', 'bytedance/seedance-2.0-fast') }}
          output_dir: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}
          This resolved output_dir is configured and must not be reported as CONFIG_NEEDED.
          media_strategy.search_first: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('search_first', true) }}
          media_strategy.search_modalities: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('search_modalities', ['images']) | join(', ') }}
          media_strategy.direct_aigc_modalities: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('direct_aigc_modalities', ['audio', 'video']) | join(', ') }}
          media_strategy.aigc_fallback_when_search_empty: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('aigc_fallback_when_search_empty', true) }}
          media_strategy.allow_remote_embeds: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('allow_remote_embeds', false) }}

          User request:
          {{ inputs.user_message | xml_escape | truncate(1600) }}

    - id: project_slug
      kind: llm_chat
      depends_on: [requirement_framing]
      with:
        system: |
          You produce a single filesystem-safe slug identifying this webpage
          project. Do not call tools.
        task: |
          Output ONLY a single slug derived from the topic in the framed
          requirements below. Constraints:
          - lowercase ASCII letters, digits, and hyphens only
          - no spaces, no path separators, no file extension, no quotes
          - max 40 characters
          - topic-derived, so different topics produce different slugs

          Examples (do NOT reuse — derive from the actual topic):
          - "海洋塑料污染" → ocean-plastic-pollution
          - "Quantum computing intro" → quantum-computing-intro
          - "我们公司年会回顾" → company-annual-recap

          If you cannot derive a meaningful slug, output `webpage`.

          Output: a single line containing just the slug. No prefix, no
          explanation, no backticks, no quotes.

          Framed requirements:
          {{ outputs.requirement_framing | truncate(1500) }}

    - id: ask_images
      kind: user_input
      depends_on: [requirement_framing]
      clarify:
        mode: chat
        intro: |
          先逐项确认网页媒体配置，再继续研究、搜索素材和生成项目。
          默认需要图片；后续会先搜索，搜不到合适素材才生成。
        intro_zh: |
          先逐项确认网页媒体配置，再继续研究、搜索素材和生成项目。
          默认需要图片；后续会先搜索，搜不到合适素材才生成。
        intro_en: |
          I will confirm the webpage media settings before research, asset search,
          and project generation. Images are included by default; I will search
          first and generate only when suitable assets are unavailable.
        nl_extract: true
        fields:
          - name: include_images
            type: enum
            choices: ["YES", "NO"]
            default: "YES"
            prompt: "是否需要图片？"
            prompt_zh: "是否需要图片？"
            prompt_en: "Do you want images?"
        cancel_keywords: ["取消", "算了", "cancel", "stop", "abort"]
        timeout_hours: 24

    - id: ask_audio
      kind: user_input
      depends_on: [ask_images]
      clarify:
        mode: chat
        intro: |
          已记录图片选择。现在确认音频。
          默认需要音频；音频不走素材搜索，会直接按 OpenRouter 配置生成或给出可替换位置。
        intro_zh: |
          已记录图片选择。现在确认音频。
          默认需要音频；音频不走素材搜索，会直接按 OpenRouter 配置生成或给出可替换位置。
        intro_en: |
          I recorded the image choice. Now I need to confirm audio.
          Audio is included by default; it is generated from the OpenRouter
          configuration or represented as a replaceable slot.
        nl_extract: true
        fields:
          - name: include_audio
            type: enum
            choices: ["YES", "NO"]
            default: "YES"
            prompt: "是否需要音频？"
            prompt_zh: "是否需要音频？"
            prompt_en: "Do you want audio?"
        cancel_keywords: ["取消", "算了", "cancel", "stop", "abort"]
        timeout_hours: 24

    - id: ask_video
      kind: user_input
      depends_on: [ask_audio]
      clarify:
        mode: chat
        intro: |
          已记录音频选择。现在确认视频。
          默认需要视频；视频不走素材搜索，会直接按 OpenRouter 配置生成或给出可替换位置。
        intro_zh: |
          已记录音频选择。现在确认视频。
          默认需要视频；视频不走素材搜索，会直接按 OpenRouter 配置生成或给出可替换位置。
        intro_en: |
          I recorded the audio choice. Now I need to confirm video.
          Video is included by default; it is generated from the OpenRouter
          configuration or represented as a replaceable slot.
        nl_extract: true
        fields:
          - name: include_video
            type: enum
            choices: ["YES", "NO"]
            default: "YES"
            prompt: "是否需要视频？"
            prompt_zh: "是否需要视频？"
            prompt_en: "Do you want video?"
        cancel_keywords: ["取消", "算了", "cancel", "stop", "abort"]
        timeout_hours: 24

    - id: ask_style
      kind: user_input
      depends_on: [ask_video]
      clarify:
        mode: chat
        intro: |
          已记录视频选择。最后确认网页整体风格，然后开始研究、规划和素材获取。
        intro_zh: |
          已记录视频选择。最后确认网页整体风格，然后开始研究、规划和素材获取。
        intro_en: |
          I recorded the video choice. Last, confirm the overall visual style;
          then I will start research, planning, and asset collection.
        nl_extract: true
        fields:
          - name: visual_style
            type: string
            prompt: "网页整体风格是什么？例如：科技感、纪录片风、极简、儿童科普、商业发布会风。"
            prompt_zh: "网页整体风格是什么？例如：科技感、纪录片风、极简、儿童科普、商业发布会风。"
            prompt_en: "What overall style should the webpage use? For example: futuristic, documentary, minimal, kids science, or product launch."
            max_chars: 500
        cancel_keywords: ["取消", "算了", "cancel", "stop", "abort"]
        timeout_hours: 24

    - id: deep_research
      kind: agent
      skill: awesome-webpage-research
      depends_on: [requirement_framing, ask_images, ask_audio, ask_video, ask_style]
      with:
        question: |
          Research the topic for a multimedia webpage project.

          Single bounded round only. Produce a short cited brief with 3-5
          page anchors. Do not run multi-round investigation.

          Requirement framing:
          {{ outputs.requirement_framing | truncate(3000) }}

          Confirmed interactive choices:
          images: {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}
          audio: {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}

          Original request:
          {{ inputs.user_message | xml_escape | truncate(1000) }}

    - id: page_outline
      kind: llm_chat
      depends_on: [requirement_framing, project_slug, ask_images, ask_audio, ask_video, ask_style, deep_research]
      with:
        system: |
          You are the Page Outline stage for AwesomeWebpageMetaSkill.
          Produce a structural outline + media intent list — NOT a final
          asset binding. Later steps will normalize media results into a
          compact manifest and then bind actual files to sections. Do not
          call tools.
        task: |
          {{ inputs.language_instruction }}

          Output the webpage OUTLINE — section structure + media intents
          only. Downstream media steps read this to decide WHAT to fetch
          or generate; the deterministic `media_assets_collect` step later
          turns producer output into concrete browser-relative asset paths.

          Required sections:

          1. Page hierarchy
             - Ordered list of sections: section_id, title, narrative
               purpose, 1-2 sentence summary.
             - Overall narrative arc (one paragraph).

          2. Visual style summary (one paragraph)

          3. Media intent list — one entry per desired asset, with:
             - slot_id: short kebab-case key, unique within outline
               (e.g. `hero-ocean`, `foodchain-flow`, `narration-intro`).
             - modality: image | audio | video
             - placement: which section_id it belongs to + role
               (e.g. "hero background", "section illustration",
               "section narration").
             - subject: one-sentence concrete description of what the
               asset should depict / convey.
             - prompt_hint: 1-2 sentences of stylistic / compositional
               guidance for AIGC steps.
             - search_keywords: 3-6 short keywords for image search
               (only meaningful for image slots).
             - load_bearing: true | false. `true` means the section
               structurally relies on this asset (e.g. hero video). If
               the asset later fails to produce, the final media binding
               gate reports it instead of hiding the missing modality.
               `false` means the section degrades gracefully to
               text-only.

             HARD: do NOT specify filenames or paths. slot_id is a
             semantic key; the producer step (image_download,
             image_aigc, audio_aigc, video_aigc) decides the on-disk
             filename and the deterministic media binding step performs
             final src/path checks.

          4. Local file plan: project/index.html, project/style.css,
             project/script.js.

          5. Accessibility plan: alt-text strategy, ARIA roles,
             keyboard navigation, reduced-motion handling.

          Hard rules:
          - No filenames. No paths. No `.jpg` / `.mp3` / `.mp4`.
          - If the user said NO to a modality, do not include any media
            intents of that modality.
          - Include only as many media intents as the section narrative
            actually needs — do not pad to hit a target count.
          - Outline must remain coherent even if every media intent
            fails downstream (text + headings carry the page).

          Requirement framing:
          {{ outputs.requirement_framing | truncate(3000) }}

          Research report:
          {{ outputs.deep_research | truncate(6000) }}

          Confirmed interactive choices:
          images: {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}
          audio: {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}

    - id: media_slots_normalize
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [page_outline]
      tool_args:
        command: |
          python -m opensquilla.skills.bundled.AwesomeWebpageMetaSkill.scripts.media_slots_normalize
        timeout: 20
        stdin: |
          {"page_outline": {{ outputs.page_outline | tojson }},
           "requirement_framing": {{ outputs.requirement_framing | tojson }},
           "include_image": {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') | tojson }},
           "include_audio": {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') | tojson }},
           "include_video": {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') | tojson }},
           "visual_style": {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') | tojson }}}

    - id: media_search
      kind: agent
      skill: web-search
      depends_on: [media_slots_normalize]
      when: "inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') == 'YES' and inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('search_first', True) and 'images' in inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('search_modalities', ['images'])"
      with:
        query: |
          Search only for reusable webpage image candidates for this plan.
          Do not search for audio or video; those modalities are generated directly
          through the configured OpenRouter models when the user requests them.
          Prefer assets that can be used locally or replaced cleanly. Return URLs, titles,
          image type, likely license/provenance, and download/replacement notes.

          Tool contract for this step:
          - Use the platform-provided `web_search` tool ONLY (provider is configured
            globally; you do not need to know which engine — just call the tool).
          - Do NOT run any local Python script (no `python scripts/search.py`,
            no `duckduckgo-search`, no `pip install`).
          - Do NOT call `web_fetch`. Snippets from `web_search` are enough.
          - Issue at most 2 `web_search` calls, each using only the supported
            arguments `query` and `max_results=6`; include image intent in the
            query text itself, grouped by visual theme — do not search per asset.
          - Stop as soon as you have ~4-6 usable candidates total.
          - If results are empty, unusable, off-topic, or licensing is unclear,
            stop immediately and return:
            {"status":"NO_USABLE_IMAGE_CANDIDATES","candidates":[]}
          - Otherwise return one JSON object with:
            candidates[] (title, url, source_domain, likely_license, suggested_alt).

          Normalized media slots (authoritative):
          {{ outputs.media_slots_normalize | truncate(3500) }}

          Page plan context:
          {{ outputs.page_outline | truncate(1200) }}

          Confirmed interactive choices:
          images: {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}
          audio: {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}
        format: json

    - id: media_strategy
      kind: llm_classify
      output_choices:
        - IMAGE_SEARCH_READY
        - NEEDS_AIGC_IMAGE
      depends_on: [media_search, media_slots_normalize]
      with:
        text: |
          Decide whether searched image media is enough or whether image AIGC fallback is required.

          Fast media policy:
          - Use the confirmed interactive choices as the source of truth.
          - Search is image-only and only runs when config.awesome_webpage.media_strategy.search_first
            is true and `images` is present in search_modalities.
          - When image search is disabled by config, ignore media_search output and choose
            NEEDS_AIGC_IMAGE for requested images so the direct image generator handles them.
          - Audio and video must not be evaluated through web search.
          - If include_images is NO, choose IMAGE_SEARCH_READY.
          - If include_images is YES, evaluate only image candidates from web-search.
          - Choose NEEDS_AIGC_IMAGE when image search is empty, unusable, off-topic,
            not downloadable/localizable, or has unclear licensing.
          - Choose IMAGE_SEARCH_READY only when the search results provide enough usable
            local/downloadable image assets for the requested page.
          - Audio and video are direct AIGC modalities. They are handled by audio_aigc
            and video_aigc based on user confirmation and OpenRouter config, not by this classifier.

          Rules:
          - IMAGE_SEARCH_READY: image search results are sufficient, or images were not requested.
          - NEEDS_AIGC_IMAGE: image generation is needed.

          If search is empty and config.awesome_webpage.media_strategy.aigc_fallback_when_search_empty is true,
          choose NEEDS_AIGC_IMAGE. Do not choose a model here; model choice belongs to config.

          Resolved media strategy:
          search_first: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('search_first', true) }}
          search_modalities: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('search_modalities', ['images']) | join(', ') }}
          aigc_fallback_when_search_empty: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('aigc_fallback_when_search_empty', true) }}

          Requirement framing:
          {{ outputs.requirement_framing | truncate(2000) }}

          Page plan:
          {{ outputs.page_outline | truncate(2500) }}

          Normalized media slots:
          {{ outputs.media_slots_normalize | truncate(2000) }}

          Confirmed interactive choices:
          images: {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}
          audio: {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}

          Primary search:
          {{ outputs.media_search | truncate(3500) }}

    - id: image_download
      kind: skill_exec
      skill: awesome-webpage-image-download
      depends_on: [media_strategy, media_slots_normalize]
      when: "inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') == 'YES' and outputs.media_strategy == 'IMAGE_SEARCH_READY'"
      with:
        output_dir: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project/assets/images"
        local_path_prefix: project/assets/images
        payload: |
          {"media_slots": {{ outputs.media_slots_normalize | tojson }},
           "media_search": {{ outputs.media_search | tojson }}}

    - id: image_aigc
      kind: skill_exec
      skill: nano-banana-pro-openrouter
      depends_on: [media_strategy, image_download, media_slots_normalize]
      when: "inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') == 'YES' and (outputs.media_strategy == 'NEEDS_AIGC_IMAGE' or 'IMAGE_DOWNLOAD_INCOMPLETE:' in outputs.get('image_download', '') or (outputs.media_strategy == 'IMAGE_SEARCH_READY' and 'IMAGE_READY:' not in outputs.get('image_download', '')))"
      with:
        model: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('image_generation', 'google/gemini-3-pro-image-preview') }}"
        base_url: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('base_url', 'https://openrouter.ai/api/v1') }}"
        api_key: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('api_key', '') }}"
        api_key_env: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('api_key_env', 'OPENROUTER_API_KEY') }}"
        output_dir: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project/assets/images"
        filename: "{{ outputs.project_slug | slugify }}.png"
        resolution: "1K"
        max_images: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('media_strategy', {}).get('target_assets', {}).get('images', 6) }}"
        local_path_prefix: project/assets/images
        payload: |
          {
            "requirement_framing": {{ outputs.requirement_framing | tojson }},
            "media_slots": {{ outputs.media_slots_normalize | tojson }},
            "page_outline": {{ outputs.page_outline | tojson }},
            "image_download": {{ outputs.get('image_download', '') | tojson }},
            "include_images": {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') | tojson }},
            "visual_style": {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') | tojson }}
          }

    - id: audio_aigc
      kind: skill_exec
      skill: audio-cog
      depends_on: [audio_script]
      when: "inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') == 'YES'"
      with:
        model: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('audio_generation', 'openai/gpt-audio-mini') }}"
        base_url: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('base_url', 'https://openrouter.ai/api/v1') }}"
        api_key: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('api_key', '') }}"
        api_key_env: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('api_key_env', 'OPENROUTER_API_KEY') }}"
        output_dir: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project/assets/audio"
        filename: "{{ outputs.project_slug | slugify }}-narration.wav"
        voice: cedar
        payload: |
          {
            "script": {{ outputs.audio_script | tojson }},
            "requirement_framing": {{ outputs.requirement_framing | tojson }},
            "media_slots": {{ outputs.media_slots_normalize | tojson }}
          }

    - id: audio_script
      kind: llm_chat
      depends_on: [requirement_framing, page_outline, media_slots_normalize]
      when: "inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') == 'YES'"
      with:
        system: |
          You write only final spoken narration text for webpage audio.
          Do not call tools. Do not acknowledge the request.
        task: |
          {{ inputs.language_instruction }}

          Write the exact narration script that the audio model should speak.
          Output spoken text only. No title, no markdown, no filename, no JSON,
          no labels, no stage directions, no "我明白了", no "接下来", no
          "我将为你生成", no assistant-style acknowledgement.

          Requirements:
          - Make it a polished webpage narration or guide, not a response to
            the user.
          - 45-75 seconds when spoken.
          - Match the requested language and style.
          - Use the audio slot placement/subject from media slots when present.
          - It must stand alone as content a visitor hears on the page.

          Requirement framing:
          {{ outputs.requirement_framing | truncate(1800) }}

          Page outline:
          {{ outputs.page_outline | truncate(2600) }}

          Normalized media slots:
          {{ outputs.media_slots_normalize | truncate(2200) }}

    - id: video_aigc
      kind: skill_exec
      skill: openrouter-video-generator
      depends_on: [page_outline]
      when: "inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') == 'YES'"
      with:
        model: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('models', {}).get('video_generation', 'bytedance/seedance-2.0-fast') }}"
        base_url: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('base_url', 'https://openrouter.ai/api/v1') }}"
        api_key: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('api_key', '') }}"
        api_key_env: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('openrouter', {}).get('api_key_env', 'OPENROUTER_API_KEY') }}"
        output_dir: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project/assets/video"
        filename: "{{ outputs.project_slug | slugify }}-intro.mp4"
        duration: "10"
        aspect_ratio: "16:9"
        prompt: |
          Generate the short video asset requested by the page outline. Do not
          run web search for video assets.

          Requirement framing:
          {{ outputs.requirement_framing | truncate(1800) }}

          Page plan:
          {{ outputs.page_outline | truncate(3000) }}

          Confirmed interactive choices:
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}

    - id: media_assets_collect
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on:
        - image_download
        - image_aigc
        - audio_aigc
        - video_aigc
      tool_args:
        command: |
          python -m opensquilla.skills.bundled.AwesomeWebpageMetaSkill.scripts.media_assets_collect
        timeout: 30
        env:
          PROJECT_ROOT: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project"
          IMAGE_DOWNLOAD: "{{ outputs.get('image_download', '') }}"
          IMAGE_AIGC: "{{ outputs.get('image_aigc', '') }}"
          AUDIO_AIGC: "{{ outputs.get('audio_aigc', '') }}"
          VIDEO_AIGC: "{{ outputs.get('video_aigc', '') }}"

    - id: webpage_generation
      kind: agent
      skill: html-coder
      depends_on:
        - requirement_framing
        - deep_research
        - page_outline
        - media_slots_normalize
        - media_assets_collect
      with:
        mode: generate
        task: |
          {{ inputs.language_instruction }}

          You are the Webpage Source Authoring stage for AwesomeWebpageMetaSkill.
          Produce source text only. Do not call tools and do not write files.
          Apply a professional HTML/CSS quality standard: clear visual
          hierarchy, semantic HTML, restrained but distinctive typography and
          color, accessible media controls, responsive layout, and intentional
          composition rather than a thin placeholder page.

          Generate a complete local webpage as strict JSON with exactly these keys:
          - `index_html`
          - `style_css`
          - `script_js`
          - `summary`

          Output JSON only. Do not wrap it in Markdown fences. Ignore
          html-coder's default Markdown/code-block output format for this
          invocation; the final answer must be the strict JSON object only.

          Scope:
          - Author only the contents for project/index.html, project/style.css,
            and project/script.js.
          - Do not download, search, generate, copy, move, delete, package, validate, repair, normalize paths, or clean up assets.
          - Do not mention or choose filesystem paths. A deterministic later
            step writes the files to the exact project root.
          - Use only browser-relative `assets/...` paths listed in
            `media_assets_collect.assets[]`.
          - Include available image, audio, and video assets in the authored
            page. A deterministic later step patches any asset the model misses.
          - Do not show "pending", "to be added", or "replace this asset"
            placeholders for requested media.
          - Place audio controls according to the audio slot placement in
            `media_slots_normalize.slots[]`; for narration/guide audio, put
            the player near the intro/hero or the referenced section, never as
            footer-only/end-of-page content unless the slot explicitly says so.

          Design-quality contract, adapted from html-coder
          (https://clawhub.ai/jhauga/html-coder):
          - Build a real, production-quality webpage, not a short landing-page
            stub. Represent the page outline in semantic
            HTML (`header`, `main`, `section`, `figure`, `footer`).
          - Use strong visual hierarchy, clear grid alignment, intentional
            whitespace, readable line lengths, and a balanced 2-3 color system.
            Avoid a one-note dark/slate/blue page unless the requested style
            specifically requires it.
          - Include accessible local media: `<audio controls>` for any
            audio asset, `<video>` for any video asset, and `<img>` with
            meaningful alt text/captions for every image asset.
          - If `media_assets_collect` contains extra image assets, render a gallery,
            evidence wall, or visual sequence section so generated assets do
            not sit unused on disk.
          - Add keyboard-accessible controls where interaction exists, visible
            focus states, reduced-motion handling, and mobile layouts without
            overlapping text.
          - Do not show "replace this asset" placeholders when a real asset is
            present in `media_assets_collect`.

          Media assets:
          The authoritative asset facts are `media_assets_collect` below.
          Use only `media_assets_collect.assets[].src` values in generated
          HTML/CSS/JS. Those values are already `assets/...` browser paths.
          Do not scan raw producer output and do not use raw
          `project/assets/...` local_path values as browser src paths.

          Page outline (narrative + section structure):
          {{ outputs.page_outline | truncate(1500) }}

          Normalized media slots (placement/role contract):
          {{ outputs.media_slots_normalize | truncate(3500) }}

          Media strategy:
          {{ outputs.media_strategy }}

          Confirmed interactive choices:
          images: {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}
          audio: {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}

          Media assets:
          {{ outputs.media_assets_collect | truncate(6000) }}

    - id: webpage_source_validate
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [webpage_generation]
      tool_args:
        command: |
          python -m opensquilla.skills.bundled.AwesomeWebpageMetaSkill.scripts.webpage_source_validate
        timeout: 15
        stdin: "{{ outputs.webpage_generation | tojson }}"

    - id: webpage_generation_retry
      kind: llm_chat
      depends_on: [webpage_generation, webpage_source_validate, media_slots_normalize]
      when: >-
        not outputs.get('webpage_generation', '').strip() or
        'WEBPAGE_SOURCE_INVALID:' in outputs.get('webpage_source_validate', '')
      with:
        system: |
          You are the fallback Webpage Source Authoring stage for
          AwesomeWebpageMetaSkill. The primary source authoring step returned
          empty or invalid output. Produce compact source JSON only.
        task: |
          {{ inputs.language_instruction }}

          Generate strict JSON with exactly these keys:
          - `index_html`
          - `style_css`
          - `script_js`
          - `summary`

          Output JSON only. Do not wrap it in Markdown fences.

          Scope:
          - Author only project/index.html, project/style.css, and
            project/script.js contents.
          - Do not call tools, write files, download assets, search, generate
            media, package, validate, repair, or normalize paths.
          - Use only `assets/...` browser paths already present in
            media_assets_collect.assets[].src.
          - Do not invent pending audio/video controls or "to be added"
            placeholders.
          - Place audio controls near the audio slot placement from
            media_slots_normalize, not as footer-only/end-of-page content
            unless the slot explicitly says footer.
          - Keep the page production-quality: semantic sections, accessible
            local media, responsive layout, meaningful hierarchy, and no
            overlapping mobile text.

          Page outline:
          {{ outputs.page_outline | truncate(900) }}

          Normalized media slots:
          {{ outputs.media_slots_normalize | truncate(2200) }}

          Media assets:
          {{ outputs.media_assets_collect | truncate(3500) }}

          Confirmed interactive choices:
          images: {{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}
          audio: {{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}
          video: {{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}
          visual_style: {{ inputs.get('collected', {}).get('ask_style', {}).get('visual_style', '') }}

    - id: webpage_write
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [webpage_generation, webpage_source_validate, webpage_generation_retry]
      tool_args:
        command: |
          python -m opensquilla.skills.bundled.AwesomeWebpageMetaSkill.scripts.webpage_write
        timeout: 30
        stdin: "{{ (outputs.get('webpage_generation_retry', '') or outputs.webpage_generation) | tojson }}"
        env:
          WORKSPACE_DIR: "{{ inputs.workspace_dir }}"
          PROJECT_ROOT: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}"

    - id: media_bind_validate
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [webpage_write, media_assets_collect]
      tool_args:
        command: |
          python -m opensquilla.skills.bundled.AwesomeWebpageMetaSkill.scripts.media_bind_validate
        timeout: 30
        env:
          PROJECT_ROOT: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}"
          IMAGE_DOWNLOAD: "{{ outputs.get('image_download', '') }}"
          IMAGE_AIGC: "{{ outputs.get('image_aigc', '') }}"
          AUDIO_AIGC: "{{ outputs.get('audio_aigc', '') }}"
          VIDEO_AIGC: "{{ outputs.get('video_aigc', '') }}"
          INCLUDE_IMAGE: "{{ inputs.get('collected', {}).get('ask_images', {}).get('include_images', 'YES') }}"
          INCLUDE_AUDIO: "{{ inputs.get('collected', {}).get('ask_audio', {}).get('include_audio', 'YES') }}"
          INCLUDE_VIDEO: "{{ inputs.get('collected', {}).get('ask_video', {}).get('include_video', 'YES') }}"

    - id: quick_validate
      kind: agent
      skill: filesystem
      depends_on: [media_bind_validate]
      with:
        task: |
          Quick path-level sanity pass over the generated local webpage project.

          Fixed ClawHub skill URL: https://clawhub.ai/gtrusler/clawdbot-filesystem
          Use only the per-project root:
          {{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}
          Do not choose a new output directory and do not install another filesystem skill.

          Expected tree:
          - project/index.html
          - project/style.css
          - project/script.js
          - project/assets/images
          - project/assets/audio
          - project/assets/video

          Allowed operations (path-level only, no content reads):
          - `test -f` each of the three authored files; report MISSING_FILE
            with the specific path for any missing one.
          - `ls` the three asset directories; missing dirs are warnings, not
            failures. `mkdir -p` to create any missing asset directory.
          - Normalize relative links at the path level only — do not open
            files to check link targets.

          Hard prohibitions:
          - Do NOT run `cat`, `head`, `tail`, `sed`, `grep`, `wc`, `diff`,
            or any content-inspection command on index.html, style.css, or
            script.js. Trust the webpage_generation output as the source of
            truth.
          - Do not regenerate, rewrite, or patch authored file content. If a
            file is missing, report it; do not synthesize a replacement.
          - Skip any packaging step (zip/tar) — the project tree is the
            deliverable.

          Output: a 1-paragraph summary stating either VALIDATED (all three
          authored files exist and asset directories are present) or one of
          MISSING_FILE / MISSING_ASSETS with the specific path. Stop within
          three shell commands.

          Webpage write output:
          {{ outputs.webpage_write | truncate(2500) }}

          Media bind/validate output:
          {{ outputs.media_bind_validate | truncate(3500) }}

    - id: publish_webpage
      kind: tool_call
      tool: publish_artifact
      tool_allowlist: [publish_artifact]
      depends_on: [quick_validate]
      tool_args:
        path: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project/index.html"
        mime: "text/html"
        bundle: "directory"
        bundle_root: "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project"

    - id: delivery_guide
      kind: llm_chat
      depends_on: [quick_validate, publish_webpage]
      with:
        system: |
          You are the Delivery Guide stage for AwesomeWebpageMetaSkill.
          Produce concise user-facing run and edit instructions.
        task: |
          {{ inputs.language_instruction }}

          Write a delivery guide containing:
          - output project path from config.awesome_webpage.output_dir: {{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/{{ outputs.project_slug | slugify }}/project
          - how to open project/index.html locally
          - how to replace images, audio, and video assets
          - how to edit content in index.html, style.css, and script.js
          - what was validated, including the deterministic media bind gate
          - confirm that the complete project was published as one webpage
            bundle; do not invent or print an artifact URL
          - any CONFIG_NEEDED items if config values were missing
          - do not claim a requested media modality is available unless the
            media bind report says it is present and referenced

          Validation report:
          {{ outputs.quick_validate | truncate(5000) }}

          Media bind report:
          {{ outputs.media_bind_validate | truncate(5000) }}

          Webpage bundle publication:
          {{ outputs.publish_webpage | truncate(1500) }}
---

# AwesomeWebpageMetaSkill

Build a local multimedia webpage project from a topic, audience, language,
style, and media preference request.

Pipeline:

1. Requirement Framing
2. Deep Research
3. Page Outline (section structure + media intents; no filenames)
4. Media Acquisition (search / AIGC; producers emit `*_READY:` lines)
5. Media Assets Collect (deterministic path + existence check)
6. Webpage Source Generation
7. Deterministic Webpage Write
8. Deterministic Media Bind + Validation
9. Local Validation
10. Publish Complete Webpage Bundle
11. Delivery Guide

Provider, OpenRouter model, API key, output directory, and media strategy are
configuration-owned. The meta-skill may pass the config contract through the
DAG, but individual steps must not invent those values.
