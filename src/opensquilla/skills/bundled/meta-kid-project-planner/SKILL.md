---
name: meta-kid-project-planner
description: "Use this meta-skill instead of answering directly when a child or their guardian wants to plan a school project, science fair entry, hobby kit, or kid-sized creative venture (volcano model, bug-watching YouTube channel, magnet maze, model rocket). The skill assesses feasibility against the child's age band, builds an age-appropriate step plan, lists materials with budget substitutes, surfaces safety considerations, and produces a parent-facing learning-objective summary so the guardian can supervise meaningfully. Refuses inappropriate or unsafe projects."
description_zh: "当孩子或其监护人想要规划学校项目、科学展作品、兴趣套件或适合孩子的创意活动（火山模型、观虫YouTube频道、磁力迷宫、模型火箭）时，使用此元技能而非直接回答。它会按孩子的年龄段评估可行性，制定适龄步骤计划，列出材料及预算替代方案，指出安全注意事项，并生成面向家长的学习目标摘要以便有效监督；会拒绝不当或不安全的项目。"
kind: meta
meta_priority: 60
always: false
final_text_mode: "step:project_pack_audit"
request_template:
  outcome: "Age-appropriate project plan with materials, safety notes, and guardian guidance."
  outcome_zh: "适合孩子年龄的项目计划，包含材料、安全提示和家长指导。"
  outcome_en: "Age-appropriate project plan with materials, safety notes, and guardian guidance."
  fields:
    - name: project_topic
      label_zh: "项目主题"
      label_en: "Project topic"
      required: true
    - name: age_band
      label_zh: "年龄段"
      label_en: "Age band"
      required: false
      default: "early grade"
      default_zh: "小学低年级"
      default_en: "early grade"
    - name: deadline_days
      label_zh: "截止天数"
      label_en: "Days until due"
      required: false
    - name: budget_or_material_constraints
      label_zh: "预算或材料限制"
      label_en: "Budget or material constraints"
      required: false
    - name: audience
      label_zh: "受众"
      label_en: "Audience"
      required: false
      default: "child plus guardian"
      default_zh: "孩子和监护人"
      default_en: "child plus guardian"
    - name: language
      label_zh: "输出语言"
      label_en: "Output language"
      required: false
      default: "match the user's language"
      default_zh: "跟随用户语言"
      default_en: "match the user's language"
  assumptions:
    - "Refuse unsafe or age-inappropriate projects."
    - "Default to modest materials and light guardian supervision when unspecified."
  assumptions_zh:
    - "拒绝不安全或不适合年龄的项目。"
    - "未说明时，默认使用适度材料并安排轻量家长监督。"
  assumptions_en:
    - "Refuse unsafe or age-inappropriate projects."
    - "Default to modest materials and light guardian supervision when unspecified."
output_contract:
  append_to_final_text: false
  required_sections:
    - "Feasibility verdict"
    - "Step-by-step plan"
    - "Materials and substitutions"
    - "Safety notes"
    - "Guardian learning objectives"
  assumptions:
    - "Age band and supervision level may be defaulted when absent."
  unverified:
    - "Local material availability and school-specific rubric details."
  artifacts:
    - name: "project_pack"
      required: false
eval_prompts:
  - name: "kid-project-baseline"
    prompt: "Plan a safe age-appropriate science fair project for an elementary-school child with a small budget."
    rubric:
      - "Feasibility verdict"
      - "Step-by-step plan"
      - "Materials and substitutions"
      - "Safety notes"
      - "Guardian learning objectives"
preference_keys:
  - preferred_language
  - guardian_supervision_level
policy_tags:
  - child-safety
  - age-appropriate
triggers:
  - "school project"
  - "science fair"
  - "kid science"
  - "孩子做项目"
  - "做一个手工"
  - "科学课作业"
  - "help my kid build"
  - "我要做火山"
  - "child diy project"
  - "课外动手项目"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [network, filesystem-write]
    clawhub_top100_composition:
      - skill: "Multi Search Engine"
        local_skill: multi-search-engine
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        rank: 11
        role: "Find safe, age-appropriate how-to references and material alternatives."
      - skill: "Weather"
        local_skill: weather
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        role: "Plan outdoor child projects around realistic weather constraints."
      - skill: "Deep Researcher / deep research family"
        local_skill: deep-research
        rank_source: "ClawHub research-skill family, verified via current search results"
        role: "Add extra safety and feasibility research when the project is more complex."
      - skill: "PowerPoint / PPTX"
        local_skill: pptx
        rank_source: "Top ClawHub Skills downloads top100, 2026-05-28"
        role: "Produce kid-facing printable step cards or a simple presentation when requested."
composition:
  steps:
    - id: preferences
      label: "偏好提取"
      label_en: "Preference extraction"
      kind: llm_chat
      with:
        system: "You extract kid-project preferences. Return only the requested contract. Refuse to plan projects that are clearly unsafe (firearms, fireworks, drugs, sharp-weapon making, etc.) by setting PROJECT_SAFE: no."
        task: |
          Extract the kid-project brief.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1600) }}

          Clarification policy:
          - If the request already includes a project topic, child age or age
            band, and deadline or rough time window, set
            NEEDS_CLARIFICATION: no and MISSING_FIELDS: none.
          - Budget, exact presentation format, exact weather, and exact
            sunshine direction can remain explicitly unknown assumptions; do
            not block on them and do not invent precise values.
          - For common school projects, proceed with explicit assumptions or
            UNKNOWN markers instead of asking a form unless safety or the core
            topic is unclear.

          Return exactly:
          TOPIC: <short project description>
          AGE_BAND: <PRE_K|EARLY_GRADE|TWEEN|TEEN|UNKNOWN>
          DEADLINE_DAYS: <integer days until due, or UNKNOWN>
          BUDGET_BAND: <SHOESTRING|MODEST|COMFORTABLE|UNKNOWN>
          PARENT_SUPERVISION: <SOLO|LIGHT|HANDS_ON|UNKNOWN>
          LANGUAGE: <en|zh|mixed>
          PROJECT_SAFE: <yes|no>
          UNSAFE_REASON: <one phrase if PROJECT_SAFE is no, else none>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <topic|age_band|deadline|none>
          ASSUMPTIONS:
            - <assumption>
    - id: project_clarify
      label: "项目澄清"
      label_en: "Project clarification"
      kind: user_input
      depends_on: [preferences]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.preferences and 'PROJECT_SAFE: yes' in outputs.preferences and 'MISSING_FIELDS:\n  - none' not in outputs.preferences"
      clarify:
        mode: form
        intro: |
          再确认几件事，然后给你完整的项目规划（含家长版） / A few details and I'll build the kid + parent pack.
        intro_zh: "再确认几件事，然后给你完整的项目规划（含家长版）。"
        intro_en: "A few details and I will build the kid plus guardian project pack."
        nl_extract: true
        fields:
          - name: topic
            type: string
            required: true
            prompt: "项目主题（如：做一座火山模型）/ Project topic"
            prompt_zh: "项目主题（例如：做一座火山模型）"
            prompt_en: "Project topic"
            max_chars: 200
          - name: age_band
            type: enum
            required: true
            choices: [PRE_K, EARLY_GRADE, TWEEN, TEEN]
            prompt: "孩子年龄段 / Child age band (PRE_K = 3-5, EARLY_GRADE = 6-9, TWEEN = 10-12, TEEN = 13-17)"
            prompt_zh: "孩子年龄段（PRE_K=3-5 岁，EARLY_GRADE=6-9 岁，TWEEN=10-12 岁，TEEN=13-17 岁）"
            prompt_en: "Child age band (PRE_K = 3-5, EARLY_GRADE = 6-9, TWEEN = 10-12, TEEN = 13-17)"
          - name: deadline_days
            type: int
            min: 0
            max: 365
            default: 14
            prompt: "几天后要交（0 = 今天，14 = 两周）/ Days until due"
            prompt_zh: "几天后要交（0 = 今天，14 = 两周）"
            prompt_en: "Days until due"
          - name: budget_band
            type: enum
            choices: [SHOESTRING, MODEST, COMFORTABLE]
            default: MODEST
            prompt: "预算 / Budget"
            prompt_zh: "预算"
            prompt_en: "Budget"
          - name: parent_supervision
            type: enum
            choices: [SOLO, LIGHT, HANDS_ON]
            default: LIGHT
            prompt: "家长参与程度（SOLO 几乎不参与；LIGHT 偶尔帮一下；HANDS_ON 全程在旁）/ Parent supervision"
            prompt_zh: "家长参与程度（SOLO 几乎不参与；LIGHT 偶尔帮一下；HANDS_ON 全程在旁）"
            prompt_en: "Parent supervision (SOLO = mostly independent; LIGHT = occasional help; HANDS_ON = close supervision)"
          - name: language
            type: enum
            choices: [en, zh, mixed]
            default: mixed
            prompt: "输出语言 / Language"
            prompt_zh: "输出语言"
            prompt_en: "Output language"
        cancel_keywords: ["算了", "换个项目", "cancel", "stop"]
        timeout_hours: 48
    - id: feasibility
      label: "可行性"
      label_en: "Feasibility"
      kind: llm_classify
      depends_on: [preferences, project_clarify]
      output_choices:
        - STRAIGHTFORWARD
        - NEEDS_ADULT_HELP
        - NEEDS_SHOPPING
        - SAFETY_REVIEW_REQUIRED
        - INAPPROPRIATE
      with:
        text: |
          Classify project feasibility for this child.

          Topic: from preferences / clarify.
          Preferences:
          {{ outputs.get('preferences', '') | truncate(800) }}
          Clarification:
          {{ inputs.get('collected', {}).get('project_clarify', {}) | tojson }}

          Decision rules:
          - INAPPROPRIATE: project involves weapons, fire without
            supervision possibility, drugs, harmful chemistry,
            self-harm-adjacent themes, or other clearly unsafe topics
            for any minor.
          - SAFETY_REVIEW_REQUIRED: project involves heat (small stove,
            soldering iron), sharp blades (X-Acto knives), electronics
            with mains voltage, or moderately reactive chemistry. Adult
            must be present.
          - NEEDS_SHOPPING: project requires materials the household
            likely does not have (specific kit, model rocket motor,
            specialty paint).
          - NEEDS_ADULT_HELP: project is age-appropriate but a step
            requires hands the child does not yet have (cutting balsa
            wood for a 6-year-old, threading a needle for a 4-year-old).
          - STRAIGHTFORWARD: child can complete the bulk of the project
            with the declared PARENT_SUPERVISION level.
    - id: redirect_unsafe
      label: "安全改写"
      label_en: "Safety rewrite"
      kind: llm_chat
      depends_on: [preferences, feasibility]
      when: "'PROJECT_SAFE: no' in outputs.preferences or outputs.feasibility == 'INAPPROPRIATE'"
      with:
        system: "You write a gentle, non-shaming redirect when a project topic is unsafe or inappropriate. Always offer 3 alternative project ideas that are in the same SPIRIT as the original (curiosity-driven, hands-on, age-appropriate)."
        task: |
          Topic the user asked for:
          {{ inputs.user_message | xml_escape | truncate(400) }}

          Unsafe reason (from preferences):
          {{ outputs.get('preferences', '') | truncate(400) }}

          Write:
          1. One sentence acknowledging the curiosity behind the
             original idea (do not lecture).
          2. One sentence explaining gently why this version isn't a
             good kid project (concrete, not vague).
          3. Three alternative project ideas that scratch a similar
             itch but are safe and age-appropriate. Each: one-line
             topic + one-line "what the kid will learn / make".

          Language: match preferences (default zh).
          End with a single line:
          UNSAFE_REDIRECT: yes
    - id: recall_past_projects
      label: "项目召回"
      label_en: "Project recall"
      kind: tool_call
      tool: memory_search
      tool_allowlist: [memory_search]
      depends_on: [feasibility, project_clarify]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      on_failure: recall_past_projects_fallback
      tool_args:
        query: "child project prior projects preferences constraints {{ outputs.get('preferences', '') | truncate(600) }} {{ inputs.user_message | xml_escape | truncate(600) }}"
        max_results: 6
        source: memory
    - id: recall_past_projects_fallback
      label: "项目召回兜底"
      label_en: "Project recall fallback"
      kind: llm_chat
      with:
        system: "You produce a no-memory fallback note for child project planning."
        task: |
          No durable project memory was read. Continue using only the pasted
          child age, deadline, materials, budget, supervision, location, and
          project context. Do not mention runtime errors to the user.
    - id: web_research
      label: "网页研究"
      label_en: "Web research"
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [feasibility]
      when: "outputs.feasibility in ['STRAIGHTFORWARD', 'NEEDS_ADULT_HELP', 'NEEDS_SHOPPING', 'SAFETY_REVIEW_REQUIRED']"
      on_failure: web_research_fallback
      with:
        query: "{{ inputs.get('collected', {}).get('project_clarify', {}).get('topic', '') }} {{ outputs.get('preferences', '') | truncate(160) }} {{ inputs.user_message | xml_escape | truncate(160) }} kid science project step-by-step instructions safe"
        engines: [brave, tavily, duckduckgo]
        max_results: 8
    - id: web_research_fallback
      label: "网页研究兜底"
      label_en: "Web research fallback"
      kind: llm_chat
      with:
        system: "You produce a no-web fallback note for child project planning."
        task: |
          Web research was not available. Extract the project topic, age,
          deadline, materials, budget, parent availability, location, light,
          weather-sensitive constraints, and safety needs only from the pasted
          request. Do not expose tool names, paths, stack traces, connector
          wording, or runtime failures.

          Request:
          {{ inputs.user_message | xml_escape | truncate(3500) }}
    - id: weather_check
      label: "天气检查"
      label_en: "Weather check"
      kind: skill_exec
      skill: weather
      depends_on: [feasibility, project_clarify]
      when: "outputs.feasibility != 'INAPPROPRIATE' and ('outdoor' in (inputs.user_message | lower) or 'balcony' in (inputs.user_message | lower) or 'plant' in (inputs.user_message | lower) or 'garden' in (inputs.user_message | lower) or 'park' in (inputs.user_message | lower) or '户外' in inputs.user_message or '阳台' in inputs.user_message or '植物' in inputs.user_message or '豆芽' in inputs.user_message)"
      on_failure: weather_check_fallback
      with:
        location: "{{ inputs.user_message | xml_escape | truncate(60) }}"
        days: 7
    - id: weather_check_fallback
      label: "天气兜底"
      label_en: "Weather fallback"
      kind: llm_chat
      with:
        system: "You produce a no-live-weather fallback note for child project planning."
        task: |
          Live weather was not verified. Continue using only the pasted
          location and the user's supplied light/outdoor/indoor context. Do not
          infer forecasts, temperature ranges, rainfall, balcony direction,
          sunshine hours, or season-specific claims. Do not mention tool
          failures.

          Request:
          {{ inputs.user_message | xml_escape | truncate(2000) }}
    - id: project_fact_ledger
      label: "项目事实台账"
      label_en: "Project fact ledger"
      kind: llm_chat
      depends_on: [preferences, project_clarify, recall_past_projects, weather_check]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      with:
        system: "You extract a strict source-fact ledger for a child project plan. You do not write the plan. You separate user-provided facts, durable memory facts, unknowns, and unsafe or unsupported inferences."
        task: |
          Build a strict project fact ledger from the user's request and any
          clarification payload. Durable memory is also a source of facts when
          the memory output clearly states child profile, prior projects,
          preferences, or parent availability. Ignore memory status prose,
          file inventory, workspace paths, and runtime/tool wording; extract
          only the actual remembered facts.

          User request:
          {{ inputs.user_message | xml_escape | truncate(3500) }}

          Clarification:
          {{ inputs.get('collected', {}).get('project_clarify', {}) | tojson | truncate(1200) }}

          Durable memory / past-project recall:
          {{ outputs.get('recall_past_projects', '') | truncate(1800) }}

          Weather result or fallback:
          {{ outputs.get('weather_check', '') | truncate(800) }}

          Return exactly:
          OUTPUT_LANGUAGE: <zh|en|mixed>
          PROVIDED_CHILD_CONTEXT:
            - <age, ability, child preferences, guardian availability, or UNKNOWN>
          PROVIDED_MEMORY_CONTEXT:
            - <remembered child profile, prior projects, parent constraints, lessons learned, or none>
          PROVIDED_PROJECT_CONTEXT:
            - <topic, school deadline/time window, school output, or UNKNOWN>
          PROVIDED_MATERIALS_BUDGET:
            - <available materials and budget exactly as supplied, or UNKNOWN>
          PROVIDED_LOCATION_LIGHT:
            - <location and light exactly as supplied, or UNKNOWN>
          VERIFIED_WEATHER:
            - <verified forecast if actually present in weather result, else none>
          UNKNOWN_DETAILS:
            - <exact date, balcony direction, exact weather, exact school format, etc.>
          FORBIDDEN_INFERENCES:
            - <details that must not appear as facts, e.g. exact calendar date,
              balcony faces south/east/west, temperature range, rain forecast,
              school rule, allergy, fake measurements, tasting/eating>

          Rules:
          - If durable memory says the child is a specific age, likes/dislikes
            an activity style, has prior projects, or has parent time limits,
            put those in PROVIDED_CHILD_CONTEXT and PROVIDED_MEMORY_CONTEXT.
            Do not mark them UNKNOWN.
          - If the current request asks not to repeat previous projects, list
            remembered prior projects in PROVIDED_MEMORY_CONTEXT so downstream
            steps can avoid them explicitly.
          - If the source says only "two weeks later", mark exact date UNKNOWN.
          - If the source says only "half-day sun", mark orientation and hours
            UNKNOWN. Preserve "half-day sun" exactly.
          - If weather was not actually verified, VERIFIED_WEATHER must be none.
          - Never invent sample measurements, dates, allergies, school rules,
            or local forecasts.
    - id: deep_research
      label: "深度研究"
      label_en: "Deep research"
      kind: skill_exec
      skill: deep-research
      depends_on: [feasibility]
      when: "outputs.feasibility in ['SAFETY_REVIEW_REQUIRED', 'NEEDS_SHOPPING']"
      with:
        query: "{{ inputs.get('collected', {}).get('project_clarify', {}).get('topic', '') }} safe materials children"
        depth: "standard"
        max_rounds: 1
    - id: outline_steps
      label: "步骤大纲"
      label_en: "Step outline"
      kind: llm_chat
      depends_on: [feasibility, web_research, recall_past_projects, weather_check, project_clarify, preferences]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      with:
        system: "You break a project into kid-sized steps. Each step takes 5-20 minutes for an unhurried child. No step requires reading more than a paragraph. Use concrete, doable verbs."
        task: |
          Build a step-by-step plan grounded in the topic and research.

          Topic + context:
          {{ outputs.get('preferences', '') | truncate(500) }}
          {{ inputs.get('collected', {}).get('project_clarify', {}) | tojson }}
          If clarification fields are empty, extract topic, age, deadline,
          budget, materials, parent availability, location, and light from the
          user request:
          {{ inputs.user_message | xml_escape | truncate(2500) }}

          Past projects this child has done (avoid repeats; build on prior learning):
          {{ outputs.get('recall_past_projects', '') | truncate(600) }}

          Weather (for outdoor projects — pick a good day):
          {{ outputs.get('weather_check', '') | truncate(400) }}

          Web research:
          {{ outputs.get('web_research', '') | truncate(2500) }}

          Deep research (only present if research happened):
          {{ outputs.get('deep_research', '') | truncate(2000) }}

          Deadline: {{ inputs.get('collected', {}).get('project_clarify', {}).get('deadline_days', 14) }} days.
          Age band: {{ inputs.get('collected', {}).get('project_clarify', {}).get('age_band', 'EARLY_GRADE') }}.
          Parent supervision: {{ inputs.get('collected', {}).get('project_clarify', {}).get('parent_supervision', 'LIGHT') }}.

          Output a markdown numbered list. Each step has:
          - Title (one short sentence, kid-readable verb-first)
          - Time estimate (5-20 min)
          - Adult-needed: yes / no (be honest)
          - One supportive sentence ("You'll get to ...")

          Distribute the steps across the available days. If deadline is
          tight, mark which steps to skip or shorten.

          Language: match preferences (default mixed).
    - id: material_list
      label: "材料清单"
      label_en: "Materials list"
      kind: llm_chat
      depends_on: [outline_steps, project_clarify, web_research]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      with:
        system: "You list materials needed for a kid project. For each material, offer a SHOESTRING substitute the family likely has at home. Always be honest when a substitute genuinely won't work."
        task: |
          List materials needed.

          Step plan:
          {{ outputs.outline_steps | truncate(2500) }}

          Budget band: {{ inputs.get('collected', {}).get('project_clarify', {}).get('budget_band', 'MODEST') }}.

          Output a markdown table with columns:
          item | quantity | est_cost | shoestring_substitute |
          notes_if_substitute_changes_outcome.

          Add at the end a bullet line "Likely you have at home:" listing
          items the household probably already owns.
    - id: safety_notes
      label: "安全提示"
      label_en: "Safety notes"
      kind: llm_chat
      depends_on: [feasibility, outline_steps, project_clarify]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      with:
        system: "You surface safety considerations for a kid project. Be specific (not generic 'be careful'). Tailor to the age band. If feasibility says SAFETY_REVIEW_REQUIRED, the safety section is the most important part of the deliverable."
        task: |
          Surface safety notes specific to this project.

          Feasibility: {{ outputs.feasibility }}
          Age band: {{ inputs.get('collected', {}).get('project_clarify', {}).get('age_band', 'EARLY_GRADE') }}.
          Step plan (for reference):
          {{ outputs.outline_steps | truncate(2000) }}

          Output bullet points grouped under:
          ## ⚠️ Adult must be present for
          ## ✋ Stop and call an adult if
          ## 🧪 Materials to handle carefully

          Each bullet must reference a specific step from the plan (e.g.
          "Step 4 (mixing): vinegar can spray — wear safety glasses or
          old sunglasses").

          If feasibility is STRAIGHTFORWARD and the child is TWEEN+,
          keep this section short (3-5 bullets). If SAFETY_REVIEW_REQUIRED
          or younger child, be thorough (8-12 bullets).

          Language: match preferences (default mixed).
    - id: learning_objectives
      label: "学习目标"
      label_en: "Learning goals"
      kind: llm_chat
      depends_on: [outline_steps, preferences, project_clarify]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      with:
        system: "You write the parent-facing learning-objective section. This is what the GUARDIAN reads to know what their child is actually getting out of the project beyond the artifact."
        task: |
          Write the parent-facing learning objectives.

          Topic + step plan:
          {{ outputs.get('preferences', '') | truncate(400) }}
          {{ outputs.outline_steps | truncate(2500) }}

          Output:
          ## 👀 What your kid will actually learn

          3-5 bullets, each one concrete learning outcome grounded in a
          specific step. Avoid generic outcomes like "creativity" or
          "problem-solving" — name the specific concept (e.g. "How an
          acid-base reaction releases CO2 — they'll see the bubbling
          slowdown when vinegar runs out").

          ## 🧠 Conversation prompts during/after
          3 questions the parent can ask the child to deepen the
          learning. Each question must be open-ended.

          Language: match preferences (parent-facing — usually adult
          register).
    - id: kid_deck
      label: "儿童卡片"
      label_en: "Child card"
      kind: skill_exec
      skill: pptx
      depends_on: [outline_steps, material_list, safety_notes, project_clarify, feasibility]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences and inputs.get('collected', {}).get('project_clarify', {}).get('parent_supervision', 'LIGHT') == 'HANDS_ON'"
      with:
        mode: create
        title: "🛠️ {{ inputs.get('collected', {}).get('project_clarify', {}).get('topic', 'Your project') }}"
        slides:
          - title: "What we're going to make"
            body: "{{ outputs.get('preferences', '') | truncate(400) }}"
          - title: "What you need (materials)"
            body: "{{ outputs.get('material_list', '') | truncate(800) }}"
          - title: "Step-by-step plan"
            body: "{{ outputs.get('outline_steps', '') | truncate(1200) }}"
          - title: "⚠️ Stop and call grown-up if"
            body: "{{ outputs.get('safety_notes', '') | truncate(600) }}"
        output_path: "kid_project_{{ inputs.get('collected', {}).get('project_clarify', {}).get('topic', 'untitled') | slugify }}.pptx"
    - id: vocab_cards
      label: "词汇卡"
      label_en: "Vocabulary card"
      kind: llm_chat
      depends_on: [outline_steps, material_list, project_clarify, feasibility, preferences]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences and ('vocab' in (inputs.user_message | lower) or 'word card' in (inputs.user_message | lower) or 'bilingual' in (inputs.user_message | lower) or '英语' in inputs.user_message or '双语' in inputs.user_message or '单词' in inputs.user_message)"
      with:
        system: "You produce a small vocabulary card list grounded in the project content. Each card is age-appropriate."
        task: |
          Produce 6 vocab cards from the project content.

          Step plan:
          {{ outputs.outline_steps | truncate(2000) }}

          Materials:
          {{ outputs.material_list | truncate(800) }}

          Age band: {{ inputs.get('collected', {}).get('project_clarify', {}).get('age_band', 'EARLY_GRADE') }}.

          For each card:
          - Term (one English word + one Chinese gloss if the language
            preference is `mixed`)
          - Kid-friendly definition (one sentence)
          - Example sentence from the project ("In step 3, the vinegar
            ACID meets the baking soda BASE...")

          Output as a markdown numbered list.
    - id: deliver_project_pack
      label: "项目包交付"
      label_en: "Project package delivery"
      kind: llm_chat
      depends_on:
        - preferences
        - feasibility
        - redirect_unsafe
        - outline_steps
        - material_list
        - safety_notes
        - learning_objectives
        - vocab_cards
        - kid_deck
        - recall_past_projects
        - weather_check
        - project_fact_ledger
        - project_clarify
      with:
        system: "You assemble the final project pack the user will read. Return the complete deliverable inline in chat. Do not create, save, export, attach, or point primarily to an artifact unless the user explicitly asked for a file export with words like PDF, file, export, attachment, or download. Treat requests for a printable worksheet, printable record sheet, or poster layout as print-ready markdown included inline. Never mention workflow, meta-skill, tool names, connector failures, workspace paths, or runtime details."
        task: |
          Assemble the final project pack.

          If the project was unsafe (redirect_unsafe ran), output ONLY
          the content of {{ outputs.get('redirect_unsafe', '') }} verbatim
          and end with PACK_DELIVERED: no_safety_redirect — nothing else.

          Otherwise, synthesize a concise, non-duplicative project pack.
          do not copy intermediate outputs verbatim when that would repeat
          safety text, parent instructions, or child instructions. Use the
          intermediate outputs as source material and rewrite them into one
          practical final answer.

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(3500) }}

          Project fact ledger:
          {{ outputs.get('project_fact_ledger', '') | truncate(2500) }}

          Durable memory / past-project recall:
          {{ outputs.get('recall_past_projects', '') | truncate(1600) }}

          Source-constraint audit:
          - Treat the Project fact ledger as the source of truth. If an
            intermediate step conflicts with it, ignore the intermediate step.
          - Treat PROVIDED_MEMORY_CONTEXT in the fact ledger as user-relevant
            durable context. Preserve remembered age, child preferences, parent
            time limits, prior projects, and lessons learned unless the current
            request explicitly contradicts them.
          - If the fact ledger marks a detail UNKNOWN, leave it unknown or mark
            it as an assumption; do not fill it with common sense or current
            date/time.
          - Preserve every explicit user constraint before adding suggestions:
            age, deadline, location, available materials, budget,
            parent time, light/weather constraints, school deliverable, and
            requested output sections.
          - Do not replace user-provided materials with unrelated materials.
            Prefer the user's materials first, then list substitutes only as
            backups.
          - Do not invent calendar dates, school dates, temperature readings,
            weather forecasts, or live conditions. Use day numbers or say the
            exact date is unknown unless the user supplied one or live data was
            verified.
          - If the user gives only a relative deadline such as "two weeks
            later", do not convert it into a calendar date. Say "两周后 /
            relative deadline supplied; exact date not provided" and schedule
            by Day 1...Day 14.
          - Do not invent balcony direction, temperature ranges, sunshine
            hours, rain forecasts, school rules, allergies, or local weather.
            Keep weather/light claims to what the user supplied plus clearly
            labelled assumptions.
          - Do not prefill observation tables with fake measurements, fake
            dates, or predicted heights. For templates, leave measurement cells blank or as placeholders
            such as "__ cm" / "[记录]".
          - Do not suggest tasting or eating the experiment materials unless
            the user explicitly asks for an edible-food activity and safety has
            been reviewed. Observation projects should compare appearance,
            height, color, firmness by sight/touch if safe, and drawings.
          - Design a comparison experiment when it fits the project and stays
            within the user's materials, age, time budget, and safety limits.
            For observation projects, make the variable, control, and data table
            clear enough for a school presentation.
          - Prefer a clear comparison design for plant/observation projects:
            same seed, cup, water, and paper-towel conditions, with only one
            changed variable such as light exposure. If materials allow, use
            2-3 labelled groups; if not, make the single-group observation
            plan still presentation-ready.
          - "Printable" means a clean markdown table, worksheet block, or
            poster-board layout that the user can print from chat. Do not
            create or refer to PDFs, HTML files, downloads, attachments,
            local paths, generated artifacts, or workspace files unless the
            user explicitly asked for a file/PDF/export/download.
          - If the user asks for a beautiful or visually polished plan, make
            the inline markdown itself polished: a memorable title, a concise
            visual theme, color/palette suggestions, kid-facing labels,
            a drawing-heavy record sheet, and a parent-ready poster layout.
          - If the user asks to start with remembered constraints, include a
            section titled exactly "## Remembered constraints I used" near the
            top and list only facts found in PROVIDED_MEMORY_CONTEXT or the
            current request. Do not invent memory.

          Language and length:
          - Match the user's language. For Chinese requests, write Simplified
            Chinese throughout, including headings and child-facing text.
          - For Chinese requests, do not use English section headings such as
            "For You (the kid)" or "For the Grown-up".
          - Do not include vocabulary cards unless the user explicitly asked
            for vocab, word cards, bilingual support, 英语, 双语, or 单词.
          - For straightforward home/school projects, target 1800-3200 Chinese characters
            or an equivalent compact English length unless the user asks for a
            long worksheet.

          For Chinese safe projects, use this structure:

          # 🛠️ <Project Title>

          ## 先说假设
          State the age/deadline/material/weather assumptions and which facts
          are not live-verified.

          ## 项目设计
          Explain the child-friendly project question, final deliverable, and
          2-3 learning goals in plain language.

          ## 14 天计划
          Give an actionable schedule. Group days into phases when that is
          clearer than 14 long paragraphs, but preserve deadlines, daily
          observation habits, parent touchpoints, and what the child does.

          ## 材料和替代品
          Summarize the materials table and substitutions. Keep shopping and
          household alternatives practical.

          ## 安全和翻车点
          Include only the safety points that change behavior: stop-and-call
          conditions, allergy/toxin/sharp/hot/water/electricity risks where
          relevant, and 3 common failure modes with fixes.

          ## 数据记录和画图
          Include a simple observation template for date, height, leaf/color,
          water, light, and one child-friendly chart idea.

          ## 天气/光照调整
          Use live weather only if verified; otherwise state "live weather not
          verified / 实时天气未核验" and give safe indoor/low-light alternatives.

          ## 最后展示怎么讲
          Provide a 60-second child script and 3 likely teacher questions with
          simple answers.

          ## 家长每晚 20 分钟
          Summarize what the adult checks each night and which steps require
          hands-on help.

          ## 还缺哪些实时信息
          List only genuinely missing information that would improve the plan,
          without asking the user to confirm before using the current answer.

          For English safe projects, use equivalent English headings:
          remembered constraints used when requested, known facts and
          assumptions, project design, 14-day schedule,
          materials/substitutes, safety/failure modes, printable record
          sheet, poster-board layout, simple science explanation,
          Weather / light adjustment, adult 20-minute check, and missing live
          info. If the user asked for a visually polished plan, include a
          short "visual theme" subsection.

          End with a single line:
          PACK_DELIVERED: {{ outputs.feasibility }}
    - id: project_pack_audit
      label: "项目包审稿"
      label_en: "Project package review"
      kind: llm_chat
      depends_on:
        - deliver_project_pack
        - redirect_unsafe
        - project_fact_ledger
        - recall_past_projects
        - outline_steps
        - material_list
        - safety_notes
        - learning_objectives
        - weather_check
        - feasibility
        - preferences
      with:
        system: "You are the final quality gate for a child project plan. Return only the cleaned final answer that the user should read. Do not explain the audit. Do not mention workflow, meta-skill, tool names, connector failures, workspace paths, or runtime details."
        task: |
          Rewrite the draft below into the final user-facing project pack.
          Preserve useful content, but enforce the fact ledger strictly. If
          the draft is only JSON, artifact metadata, download references,
          process commentary, or otherwise not a complete user-facing answer,
          rebuild the final project pack from the fact ledger and intermediate
          source sections below.

          Unsafe redirect source:
          {{ outputs.get('redirect_unsafe', '') | truncate(1800) }}

          Project fact ledger:
          {{ outputs.get('project_fact_ledger', '') | truncate(2500) }}

          Durable memory / past-project recall:
          {{ outputs.get('recall_past_projects', '') | truncate(1600) }}

          Step plan source:
          {{ outputs.get('outline_steps', '') | truncate(2600) }}

          Materials source:
          {{ outputs.get('material_list', '') | truncate(1600) }}

          Safety source:
          {{ outputs.get('safety_notes', '') | truncate(1600) }}

          Learning source:
          {{ outputs.get('learning_objectives', '') | truncate(1600) }}

          Weather/light source:
          {{ outputs.get('weather_check', '') | truncate(900) }}

          Draft project pack:
          {{ outputs.get('deliver_project_pack', '') | truncate(8000) }}

          Required audit rules:
          - Return markdown only. Never return JSON, artifact metadata, file
            paths, download links, or attachment notes.
          - If feasibility is INAPPROPRIATE, PROJECT_SAFE is no, or the unsafe
            redirect source is non-empty, return the unsafe redirect source as
            the final answer. Preserve its refusal and all safe alternative
            project ideas. Do not rebuild a normal project pack. End with
            PACK_DELIVERED: no_safety_redirect.
          - If the draft contains JSON keys such as "text", "artifacts",
            "artifact_ref", "download_url", "mime", "sha256", "session_id",
            "created_at", or "store", discard those metadata fields and write
            a normal markdown answer instead.
          - Remove leading process commentary such as "perfect match", "let me
            run it", "I will run", "workflow", "meta-skill", or any similar
            explanation of how the answer was produced. The first non-empty
            line must be the user-facing project title, unless the user
            explicitly requested a different first heading such as
            "Remembered constraints I used".
          - Preserve the user's language. If the request is English, write
            English-only prose and English headings. If the request is Chinese,
            write Simplified Chinese throughout.
          - Remove exact calendar dates, weekdays, months, or current-year references
            unless the user explicitly provided those exact dates. If the user
            gave only a relative deadline, use Day 1...Day 14 and say exact
            date not provided.
          - Remove invented balcony direction, temperature ranges, rain forecasts,
            sunshine hours, school rules, allergies, and local weather claims
            unless they appear in the fact ledger as verified or user-provided.
          - Remove fake sample measurements, fake dates, and predicted heights.
            Observation tables must leave measurement cells blank or as
            placeholders like "__ cm" / "[记录]".
          - Remove tasting/eating suggestions. For observation projects, compare
            height, color, firmness by safe touch if appropriate, drawings, and
            notes.
          - Keep every explicit user constraint from the fact ledger: child age,
            relative deadline, location, available materials, budget, parent
            time, light constraint, and requested sections.
          - Keep every clear durable-memory constraint from the fact ledger:
            remembered age, child preferences, writing tolerance, parent
            availability, prior projects, and lessons learned. Do not rewrite
            those fields as UNKNOWN when they appear in PROVIDED_MEMORY_CONTEXT.
          - If the request asks not to repeat previous projects, explicitly
            avoid or name the prior projects as non-options in one concise
            sentence.
          - If the user asks to use remembered facts, include a
            "## Remembered constraints I used" section with the remembered
            age, preferences, writing tolerance, parent time limit, prior
            projects, and lessons learned when those facts appear in
            PROVIDED_MEMORY_CONTEXT. Do not say no memory exists when
            PROVIDED_MEMORY_CONTEXT contains facts.
          - Prefer a clear comparison design when it fits: same seed, cup,
            water, and paper-towel conditions, with only light exposure changed.
          - Treat "printable record sheet" and "poster board layout" as inline
            deliverables unless the user explicitly asked for PDF/file/export.
            Include a clean markdown worksheet/table with blank boxes,
            checkboxes, or placeholders; do not claim that a PDF, HTML file,
            local file, download, or artifact was generated.
          - For visually polished project packs, make the markdown itself
            beautiful and school-ready: memorable title, visual theme or
            palette, kid-facing labels, drawing-heavy worksheet, and a poster
            layout the parent can recreate.
          - Keep the answer compact and immediately usable: target 2500-3600 Chinese characters
            for Chinese requests. Avoid long daily tables when grouped phases
            are clearer.

          Output structure for Chinese requests:
          # 🛠️ <title>
          ## 先说清楚哪些是已知、哪些未知
          ## 项目设计
          ## 14 天计划
          ## 材料和替代品
          ## 安全和翻车点
          ## 数据记录和画图
          ## 天气/光照调整
          ## 最后展示怎么讲
          ## 家长每晚 20 分钟
          ## 还缺哪些实时信息

          For English requests, use equivalent English headings only:
          # <Project title>
          ## Remembered constraints I used
          ## Known facts and assumptions
          ## Project design
          ## 14-day plan
          ## Materials and substitutes
          ## Safety and failure modes
          ## Printable record sheet
          ## Poster-board layout
          ## Simple science explanation
          ## Weather / light adjustment
          ## Adult 20-minute check
          ## Missing live information

          End with:
          PACK_DELIVERED: {{ outputs.feasibility }}
    - id: store_project
      label: "存储项目"
      label_en: "Store project"
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [project_pack_audit, project_clarify, feasibility]
      when: "outputs.feasibility != 'INAPPROPRIATE' and 'PROJECT_SAFE: yes' in outputs.preferences"
      tool_args:
        path: "memory/meta-kid-projects.md"
        mode: append
        content: |
          ## Kid project run

          Preferences:
          {{ outputs.get('preferences', '') | truncate(1200) }}

          Fact ledger:
          {{ outputs.get('project_fact_ledger', '') | truncate(1800) }}

          Final project pack excerpt:
          {{ outputs.get('project_pack_audit', '') | truncate(2400) }}
---

# meta-kid-project-planner

Junior & guardian persona meta-skill. Turns a child's project idea —
"我要做火山", "I want to build a model rocket", "open a YouTube channel
about insects" — into a kid-friendly step plan PLUS a parent-friendly
oversight pack. The two audiences are concatenated in one markdown
deliverable with clear `## 👦 For You (the kid)` / `## 👨‍👩‍👧 For the
Grown-up` sections — a markdown-level workaround for the proposed
`audience:` primitive (portfolio design §4.2).

## Composition philosophy — multi-skill bundled orchestration

This meta-skill uses **only OpenSquilla-bundled atomic skills** plus
the five built-in step kinds — no external dependencies. The DAG calls
into **5 distinct bundled atomic skills**:

| Skill | Step(s) | Role in the DAG |
|---|---|---|
| `multi-search-engine` | `web_research` | Find existing how-to guides for the topic |
| `deep-research` | `deep_research` | Extra round for `SAFETY_REVIEW_REQUIRED` or `NEEDS_SHOPPING` feasibility |
| `memory` | `recall_past_projects`, `store_project` | Per-child memory: what they've already done; what they did this time. Avoids project repeats and builds a learning trajectory. |
| `weather` | `weather_check` | When the topic is outdoor / garden / park, pull a 7-day forecast so `outline_steps` can recommend the best day |
| `pptx` | `kid_deck` | When `PARENT_SUPERVISION: HANDS_ON`, produce a printable slide deck for the kid (visual step-by-step + safety callouts) |

Step kinds used: `llm_chat`, `llm_classify`, `user_input`, `skill_exec`,
`agent`.

Vocab card generation is a plain `llm_chat` step (`vocab_cards`)
grounded in `outputs.outline_steps + outputs.material_list` — the LLM
produces 6 age-appropriate cards directly. No external flashcard
skill is required.

Bilingual rendering for `LANGUAGE: mixed` is also prompt-side in the
relevant steps — no separate translation skill required.

## Safety design

Three layers of guardrail:

1. `preferences` step rejects clearly inappropriate topics by setting
   `PROJECT_SAFE: no` in its contract. This is prompt-side; cannot be
   bypassed by clever phrasing because the model's response is
   constrained to the return format.
2. `feasibility` classifier produces `INAPPROPRIATE` for any topic
   that involves weapons, dangerous chemistry, fire-without-adult,
   self-harm-adjacent themes. `INAPPROPRIATE` short-circuits the
   project pack and routes to `redirect_unsafe`.
3. `redirect_unsafe` produces a gentle, non-shaming redirect with 3
   alternative project ideas that scratch the same itch safely.

The skill never silently degrades safety — if the topic is unsafe, the
deliverable IS the redirect, with `PACK_DELIVERED: no_safety_redirect`.

## Honest limitations (first-wave)

- **`audience:` is markdown sections, not real two-principal output.**
  When the proposed primitive ships, the kid section can go to a
  child-facing surface while the parent section goes to the guardian's
  channel, separately.
- **No persistence of past projects.** Each invocation is independent
  — without `state:`, the skill cannot remember which projects the
  child has already done.
- **Vocab cards do not feed an FSRS deck.** The `vocab_cards` step
  emits a one-shot card list; integrating with a spaced-repetition
  state machine is reserved for a future `meta-spaced-rep-coach`.
- **Topic safety relies on prompt-side guardrails.** A future
  dedicated safety-policy step kind would be more robust than
  prompt-side judgment under adversarial inputs.
