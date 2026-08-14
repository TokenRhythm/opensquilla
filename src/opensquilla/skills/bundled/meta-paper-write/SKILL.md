---
name: meta-paper-write
description: "Use this meta-skill instead of answering directly when the current user asks to draft or produce a new academic/research paper or LaTeX manuscript. It uses multi-skill orchestration for manuscript workflows that need source search, citation planning, experiment or figure/table placeholders, drafting, length checks, citation integrity, and LaTeX/PDF compilation. Ordinary paper requests use a compact draft path; explicit full/PDF/long-form requests use the full manuscript path. Do not use it to repair or compile an existing manuscript, or for web research reports, slide decks, document decisions, or generic plotting."
description_zh: "当用户要求起草或产出新的学术/研究论文或 LaTeX 手稿时，使用此元技能而非直接回答。它通过多技能编排处理来源搜索、引用规划、实验或图表占位、起草、篇幅检查、引用完整性及 LaTeX/PDF 编译。普通请求走精简起草路径，明确要求完整/PDF/长篇时走完整手稿路径。不用于修复或编译现有手稿，也不用于网页研究报告、幻灯片、文档决策或通用绘图。"
kind: meta
meta_priority: 50
always: false
final_text_mode: "step:deliver_paper"
request_template:
  outcome: "New academic manuscript draft with citation and compilation checks as requested."
  outcome_zh: "按需生成新的学术稿件，并检查引用与编译状态。"
  outcome_en: "New academic manuscript draft with citation and compilation checks as requested."
  fields:
    - name: paper_topic_or_manuscript
      label_zh: "论文主题"
      label_en: "Paper topic"
      required: true
    - name: mode
      label_zh: "模式"
      label_en: "Mode"
      required: false
      default: "compact draft unless full/PDF/long-form is explicit"
      default_zh: "默认紧凑草稿；仅在明确要求完整/PDF/长文时生成完整稿件"
      default_en: "compact draft unless full/PDF/long-form is explicit"
    - name: target_venue_or_style
      label_zh: "目标会议/期刊或风格"
      label_en: "Target venue or style"
      required: false
    - name: citation_requirements
      label_zh: "引用要求"
      label_en: "Citation requirements"
      required: false
    - name: audience
      label_zh: "受众"
      label_en: "Audience"
      required: false
      default: "academic reader or target venue"
      default_zh: "学术读者或目标投稿 venue"
      default_en: "academic reader or target venue"
    - name: language
      label_zh: "输出语言"
      label_en: "Output language"
      required: false
      default: "match the user's language"
      default_zh: "跟随用户语言"
      default_en: "match the user's language"
  assumptions:
    - "Do not fabricate citations or experimental results."
    - "Use compact output unless the request explicitly asks for full manuscript artifacts."
  assumptions_zh:
    - "不编造引用或实验结果。"
    - "除非用户明确要求完整稿件产物，否则使用紧凑输出。"
  assumptions_en:
    - "Do not fabricate citations or experimental results."
    - "Use compact output unless the request explicitly asks for full manuscript artifacts."
output_contract:
  append_to_final_text: false
  required_sections:
    - "Manuscript output"
    - "Citation and source status"
    - "Known gaps"
    - "Next validation step"
  assumptions:
    - "Draft mode and artifact generation follow the user's explicit request."
  unverified:
    - "Claims without supplied data or verified citations."
  artifacts:
    - name: "paper_artifact"
      required: false
eval_prompts:
  - name: "paper-write-baseline"
    prompt: "Draft a compact research-paper outline with citation status and known gaps for a supplied topic."
    rubric:
      - "Manuscript output"
      - "Citation and source status"
      - "Known gaps"
      - "Next validation step"
preference_keys:
  - preferred_language
  - citation_style
policy_tags:
  - no-fabricated-citations
  - no-fabricated-results
triggers:
  - "draft a paper"
  - "write a research paper"
  - "academic manuscript"
  - "research manuscript"
  - "latex manuscript"
  - "long-form paper"
  - "写篇论文"
  - "写一篇论文"
  - "撰写论文"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  platform:
    requires:
      bins: ["xelatex", "bibtex"]
    install:
      - kind: toolchain
        id: paper-tex
        label: "Install verified TeX toolchain"
        bins: ["xelatex", "bibtex"]
        os: [darwin, linux, windows]
  opensquilla:
    risk: high
    capabilities:
      - filesystem-write
      - process-control
composition:
  steps:
    - id: paper_collect
      label: "论文收集"
      label_en: "Paper intake"
      kind: llm_chat
      with:
        system: "You extract paper requirements and decide whether clarification is required."
        task: |
          Extract a structured paper brief from the original user request.
          Do NOT ask a question in this step. Instead, mark
          NEEDS_CLARIFICATION: yes when any required field is missing,
          ambiguous, or only guessable. The next paper_clarify step will
          ask the user for missing information.

          Mode defaults:
          - Use COMPACT_SKELETON by default for ordinary "write/draft a
            paper" requests. This is the fast path and still produces a
            coherent LaTeX-ready draft with citations and a compiled PDF.
          - Use FULL_MANUSCRIPT only when the user explicitly asks for a full
            manuscript, long-form paper, publication-ready paper, PDF, LaTeX
            manuscript, section-by-section drafting, or gives a target of 8+
            pages.
          - Use COMPACT_SKELETON when the user explicitly asks for a short
            skeleton, outline, compact draft, or does not specify length.
          - This workflow creates new manuscripts. Requests to repair or only
            compile an existing workspace artifact are outside this public
            contract and must not select a separate paper mode.

          Clarification policy:
          - Required field: topic.
          - Infer language from the user request whenever possible. For an
            English request, set LANGUAGE: en. For a Chinese request, set
            LANGUAGE: zh.
          - If target pages are missing, use TARGET_PAGES: 4 for
            COMPACT_SKELETON and 10 for FULL_MANUSCRIPT.
          - If audience is missing, use AUDIENCE: academic.
          - Treat phrases such as "at least 15 references", "minimum 15
            citations", "至少15篇参考文献", and "不少于15个来源" as an
            explicit numeric citation target. Copy the integer into
            CITATION_TARGET; do not leave it as AUTO or move it only into
            ASSUMPTIONS.
          - Set EVIDENCE_STATUS: supplied ONLY when the request includes or
            attaches completed empirical measurements, experiment outputs, or
            a results dataset. A topic, hypothesis, desired experiment, sample
            size proposal, or request to "write results" is not evidence.
            Otherwise set EVIDENCE_STATUS: not_supplied.
          - Set NEEDS_CLARIFICATION: yes only when the topic is missing or
            the request explicitly asks to be interviewed before drafting.
          - Do not set NEEDS_CLARIFICATION: yes for missing paper_mode,
            language, target_pages, citation_target, or audience; apply the
            defaults above instead.
          - If clarification is required, write CLARIFY_QUESTION in the same
            language as the original request. For English requests, the
            question must be English.

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(1400) }}

          Return exactly:
          TOPIC: <paper topic, or MISSING_TOPIC>
          PAPER_MODE: <FULL_MANUSCRIPT|COMPACT_SKELETON>
          LANGUAGE: <en|zh|ja|other>
          TARGET_PAGES: <integer 1-50, or MISSING_TARGET_PAGES>
          AUDIENCE: <academic|technical|business|general>
          CITATION_TARGET: <integer if user explicitly requested one, otherwise AUTO>
          EVIDENCE_STATUS: <supplied|not_supplied>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <field name, or none>
          CLARIFY_QUESTION: <single concise question in the same language as the original request if NEEDS_CLARIFICATION is yes, otherwise none>
          ASSUMPTIONS:
            - <assumption or none>
    - id: paper_clarify
      label: "论文澄清"
      label_en: "Paper clarification"
      kind: user_input
      depends_on: [paper_collect]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.paper_collect"
      clarify:
        mode: form
        intro: |
          {% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}
          论文信息还不完整。请补齐下面字段；除非你选择完整论文，我会优先使用更快的草稿模式。
          {% else %}
          Some paper details are missing. Please fill in the fields below; I will draft with the fastest suitable mode unless you choose a full manuscript.
          {% endif %}
        intro_zh: "论文信息还不完整。请补齐下面字段；除非你选择完整论文，我会优先使用更快的草稿模式。"
        intro_en: "Some paper details are missing. Please fill in the fields below; I will draft with the fastest suitable mode unless you choose a full manuscript."
        nl_extract: true
        fields:
          - name: topic
            type: string
            required: true
            prompt: "{% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}论文主题{% else %}Paper topic{% endif %}"
            prompt_zh: "论文主题"
            prompt_en: "Paper topic"
            max_chars: 200
          - name: paper_mode
            type: enum
            choices:
              - FULL_MANUSCRIPT
              - COMPACT_SKELETON
            default: COMPACT_SKELETON
            prompt: "{% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}类型（默认 COMPACT_SKELETON = 更快草稿；选择 FULL_MANUSCRIPT 生成完整论文 + PDF）{% else %}Mode (default COMPACT_SKELETON = faster draft; choose FULL_MANUSCRIPT for full paper + PDF){% endif %}"
            prompt_zh: "类型（默认 COMPACT_SKELETON = 更快草稿；选择 FULL_MANUSCRIPT 生成完整论文 + PDF）"
            prompt_en: "Mode (default COMPACT_SKELETON = faster draft; choose FULL_MANUSCRIPT for full paper + PDF)"
          - name: language
            type: enum
            required: true
            choices: [en, zh, ja, other]
            prompt: "{% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}语言{% else %}Language{% endif %}"
            prompt_zh: "语言"
            prompt_en: "Language"
          - name: target_length_pages
            type: int
            min: 1
            max: 50
            default: 4
            prompt: "{% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}目标页数（1-50）{% else %}Target pages (1-50){% endif %}"
            prompt_zh: "目标页数（1-50）"
            prompt_en: "Target pages (1-50)"
          - name: audience
            type: enum
            choices: [academic, technical, business, general]
            default: academic
            prompt: "{% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}受众{% else %}Audience{% endif %}"
            prompt_zh: "受众"
            prompt_en: "Audience"
          - name: citation_target
            type: int
            min: 1
            max: 100
            required: false
            prompt: "{% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}最少可核验参考文献数量（可选）{% else %}Minimum verifiable references (optional){% endif %}"
            prompt_zh: "最少可核验参考文献数量（可选）"
            prompt_en: "Minimum verifiable references (optional)"
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24
    - id: paper_contract
      label: "论文契约"
      label_en: "Paper contract"
      kind: llm_chat
      depends_on: [paper_collect, paper_clarify]
      with:
        system: "You merge extracted paper requirements and clarification answers into the final paper contract."
        task: |
          Build the final paper contract. Prefer explicit clarification
          answers over the first-pass extraction. If clarification is empty,
          use only confidently extracted values. Do not invent missing topic.
          Preserve the conservative evidence classification: use supplied only
          when completed empirical data or results are explicitly present in
          the original request; otherwise use not_supplied.
          Re-scan the original request, including any [Additional user notes],
          for an explicit minimum/reference count. A phrase such as "at least
          15", "minimum 15 references", "至少15篇", or "不少于15个来源"
          MUST produce CITATION_TARGET: 15 even when the first-pass extraction
          said AUTO. Prefer the clarification field citation_target when set.

          First-pass extraction:
          {{ outputs.paper_collect | truncate(1200) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('paper_clarify', {}) | tojson }}

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          TOPIC: <resolved topic>
          PAPER_MODE: <FULL_MANUSCRIPT|COMPACT_SKELETON>
          LANGUAGE: <en|zh|ja|other>
          TARGET_PAGES: <integer 1-50>
          AUDIENCE: <academic|technical|business|general>
          CITATION_TARGET: <integer if explicitly requested, otherwise AUTO>
          EVIDENCE_STATUS: <supplied|not_supplied>
          PDF_REQUIRED: yes
          ASSUMPTIONS:
            - <assumption or none>
    - id: paper_preferences
      label: "论文偏好"
      label_en: "Paper preferences"
      kind: llm_chat
      depends_on: [paper_contract]
      with:
        system: "You expand extracted paper requirements into a structured planning contract."
        task: |
          Expand the extracted paper facts into a full planning contract.

          Extracted paper contract (DO NOT override these):
          {{ outputs.paper_contract | truncate(1200) }}

          Original user request (context only, do NOT override confirmed facts):
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          PAPER_MODE: <copy PAPER_MODE from extracted contract verbatim>
          MODE: DIRECT
          TOPIC: <copy TOPIC from extracted contract verbatim>
          AUDIENCE: <copy AUDIENCE from extracted contract verbatim>
          VENUE_STYLE: <generic research paper or inferred venue>
          LANGUAGE: <copy LANGUAGE from extracted contract verbatim — use the exact enum value, do not translate>
          TARGET_LENGTH: <copy TARGET_PAGES from extracted contract verbatim> compiled pages unless the user requested a different unit
          CITATION_TARGET: <integer only: copy explicit citation target, otherwise derive one integer from target length, audience, and venue style; never output AUTO, ≥, prose, or units>
          EVIDENCE_STATUS: <copy EVIDENCE_STATUS from extracted contract verbatim>
          LENGTH_STRATEGY: <section-level page/word allocation based on TARGET_LENGTH and user intent>
          CITATION_STRATEGY: <how many sources to use per major section and why>
          CITATION_STYLE: BibTeX cite keys, LaTeX \cite{...}
          ASSUMPTIONS:
            - <assumption>
    - id: search_query_translation
      label: "检索翻译"
      label_en: "Search translation"
      kind: llm_chat
      depends_on: [paper_contract]
      with:
        system: "You translate paper topics into concise English academic search queries. Output only the query text."
        task: |
          Translate the user-confirmed paper topic into one concise
          English academic search query optimised for arXiv / ACL
          Anthology / ACM DL / OpenReview / IEEE / Nature / Science.

          Strict rules:
          - Output ONLY the English query text on a single line.
          - Do NOT include preambles, labels (no "Query:", "Translation:"),
            quotes, the word "search", boolean operators, site filters,
            or the year — those are appended downstream by the runtime.
          - Keep it ≤ 12 words; prefer the canonical English term for any
            non-English research area (e.g. 检索增强生成 → retrieval-augmented
            generation; 大模型对齐 → large language model alignment).
          - If the topic is already in English, return it unchanged
            (clean up only obvious typos / extraneous words).

          Topic (may be Chinese, Japanese, or English):
          TOPIC: {{ outputs.paper_contract | truncate(1200) }}, MODE: {{ outputs.paper_contract | truncate(400) }}, PAGES: {{ outputs.paper_contract | truncate(400) }}
    - id: search_papers
      label: "论文检索"
      label_en: "Paper search"
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [paper_preferences, search_query_translation]
      with:
        # Keep the canonical query clean. Search backends apply their own
        # academic metadata/domain handling; a giant cross-site OR expression
        # materially reduces recall on Crossref and Tavily.
        query: "{{ outputs.search_query_translation | xml_escape | truncate(200) }}"
        engines: [crossref, brave, tavily]
        max_results: 30
    - id: refbib
      label: "参考文献"
      label_en: "References"
      kind: skill_exec
      skill: paper-refbib-stub
      depends_on: [search_papers]
      with:
        search_results: "{{ outputs.search_papers | truncate(8000) }}"
    - id: source_pack
      label: "来源包"
      label_en: "Source pack"
      kind: llm_chat
      depends_on: [search_papers, refbib]
      with:
        system: "You curate paper sources and enforce citation coverage."
        task: |
          Build a source pack for a paper draft. Prefer primary papers,
          official documentation, surveys, and reputable technical reports.
          Keep enough usable references to satisfy CITATION_TARGET and
          CITATION_STRATEGY from paper_preferences when the search results
          allow it. If fewer credible references are available than the
          requested/derived target, keep all credible references and state the
          gap.

          Paper preferences:
          {{ outputs.paper_preferences | truncate(2000) }}

          Search results:
          {{ outputs.search_papers | truncate(8000) }}

          Bibliography:
          {{ outputs.refbib | truncate(8000) }}

          Return:
          SOURCE_STATUS: <sufficient|insufficient>
          CITATION_TARGET: <copy the integer CITATION_TARGET from paper_preferences>
          USABLE_REFERENCE_COUNT: <integer count of unique, relevant, verifiable primary references>
          USABLE_KEYS:
            - <refN, one key per line; only keys also listed under PRIMARY_REFERENCES>
          EXCLUDED_KEYS:
            - <refN | reason, or none>
          SOURCE_PACK:
          PRIMARY_REFERENCES:
            - refN | title | supported claim
          COVERAGE_GAPS:
            - <gap or none>
          Count only sources that are topically relevant and have a verifiable
          URL, DOI, or arXiv identifier in the bibliography. Search-result
          pages, proceedings indexes, duplicates, and unrelated papers belong
          under EXCLUDED_KEYS and must not inflate USABLE_REFERENCE_COUNT.
    - id: source_readiness_gate
      label: "来源就绪门禁"
      label_en: "Source readiness gate"
      kind: skill_exec
      skill: paper-source-readiness-gate
      depends_on: [paper_contract, paper_preferences, source_pack, refbib]
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "paper_preferences": {{ outputs.paper_preferences | tojson }},
            "source_pack": {{ outputs.source_pack | tojson }},
            "bibliography": {{ outputs.refbib | tojson }}
          }
    - id: experiment_design
      label: "实验设计"
      label_en: "Experiment design"
      kind: llm_chat
      depends_on: [paper_preferences, source_pack, source_readiness_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        system: "You design rigorous, falsifiable experiments. You also decide how many figures and tables the paper needs based on the target page budget, the research questions, and the analysis dimensions — do not over- or under-provision."
        task: |
          Design the experiments and supporting figures/tables for this
          paper. The design must be tight enough that downstream LaTeX
          generation can render placeholder figure/table environments
          straight from your output.

          Paper facts:
          TOPIC: {{ outputs.paper_contract | truncate(1200) }}, MODE: {{ outputs.paper_contract | truncate(400) }}, PAGES: {{ outputs.paper_contract | truncate(400) }}

          Original user request (authoritative for any user-supplied outcome
          threshold; do not infer thresholds from literature or defaults):
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Preferences:
          {{ outputs.paper_preferences | truncate(2000) }}

          Source pack (cite keys must come from here):
          {{ outputs.source_pack | truncate(6000) }}

          Provisioning rules (you decide the actual count within these):
          - target ≤8 pages    → 1–2 figures, 0–1 tables
          - target 9–14 pages  → 2–4 figures, 1–2 tables
          - target 15–24 pages → 4–6 figures, 2–3 tables
          - target ≥25 pages   → 6–10 figures, 3–5 tables
          Every figure/table MUST trace to a research question or an
          analysis dimension. Do not invent purely decorative figures.

          Reply with EXACTLY this structure (verbatim section headers, no
          markdown fences):

          EVIDENCE_STATUS: <copy supplied|not_supplied from paper facts verbatim>

          RESEARCH_QUESTIONS:
            - id: RQ1
              question: <one sentence>
            - id: RQ2
              question: <one sentence>
            - id: RQ3
              question: <one sentence>

          HYPOTHESES:
            - id: H1; supports: RQ1; statement: <one sentence>
            - id: H2; supports: RQ2; statement: <one sentence>

          VARIABLES:
            independent: <list>
            dependent: <list>
            controlled: <list>

          DATASETS:
            - name; size; split; license/source; rationale

          BASELINES:
            - name; rationale; cite_key (from source_pack); ablation_relationship

          METRICS:
            - name; definition; supports: RQ#

          FIGURE_PLAN:
            - id: fig1
              type: <line|bar|scatter|heatmap|violin|timeline|cdf|box|matrix>
              x_axis: <semantic + unit>
              y_axis: <semantic + unit>
              comparison_groups: <list>
              supports: <RQ#|H#>
              caption_hint: <short planned/placeholder/hypothesis caption>
            - id: fig2
              ... (repeat per provisioning rules)

          TABLE_PLAN:
            - id: tab1
              columns: <list of column headers>
              rows_shape: <e.g. "one row per baseline + ours + 2 ablations">
              supports: <RQ#|H#>
              caption_hint: <short planned/placeholder/hypothesis caption>
            - id: tab2
              ... (repeat per provisioning rules)

          ANALYSIS_DIMENSIONS:
            - dimension: performance; figures: [fig1]; tables: [tab1]; coverage_note: <why this matters>
            - dimension: ablation; figures: [fig2]; tables: [tab2]; coverage_note: <...>
            - dimension: sensitivity_or_robustness; figures: [...]; tables: [...]; coverage_note: <...>
            - dimension: efficiency; figures: [...]; tables: [...]; coverage_note: <...>
            - dimension: failure_analysis_or_qualitative; figures: [...]; tables: [...]; coverage_note: <...>

          Strict rules:
          - Every figure/table id appears in at least one ANALYSIS_DIMENSIONS row.
          - Every RESEARCH_QUESTION is supported by ≥1 figure AND/OR ≥1 table.
          - cite_key fields must reference IDs that exist in source_pack;
            do not invent new ref keys here.
          - When EVIDENCE_STATUS is not_supplied, every caption_hint MUST
            explicitly identify itself as a planned evaluation placeholder or
            hypothesis in the manuscript language. Prefer the prefix
            "Planned evaluation placeholder:" / "计划评估占位：". A result
            comparison is allowed only as an explicit hypothesis, question,
            or future test (for example, "Hypothesis H1: ... will ..." /
            "假设 H1：将检验是否……"). Never write a present- or past-tense
            categorical finding such as "Ours achieves lower cost" or
            "所提方法保持最低通信成本与最高精度".
          - Concrete setup values and predefined metric thresholds may appear
            in captions. When evidence is not supplied, a numeric outcome
            threshold may appear only when it is copied from the original
            user request and labeled as a decision criterion. Do not invent
            quantitative hypotheses, expected ranges, improvements, scores,
            latency reductions, convergence-round deltas, or ablation effects.
            Unknown outcomes must remain TBD / 待实验确定.
          - Do not emit Unicode U+2011, U+2013, or U+2014 punctuation. Use an
            ASCII hyphen, LaTeX ``--`` / ``---``, or native full-width
            sentence punctuation instead.
    - id: figure_placeholders
      label: "图占位"
      label_en: "Figure placeholders"
      kind: llm_chat
      depends_on: [experiment_design]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        system: "You render LaTeX placeholder figure environments from a structured figure plan. Output is pure LaTeX, ready to inline into a manuscript."
        task: |
          For EACH figure listed in FIGURE_PLAN below, emit one LaTeX
          ``figure`` environment. Use ``\fbox{\parbox{0.8\linewidth}{...}}``
          as the placeholder body — DO NOT use ``\includegraphics``
          because no PDFs exist yet.

          Body of each placeholder MUST list:
            * the figure's id (fig1, fig2, …)
            * the chart type
            * x_axis / y_axis labels with units
            * comparison_groups
            * RQ/H it supports

          Caption normally comes from caption_hint (escape LaTeX specials).
          First read EVIDENCE_STATUS from the experiment design. When it is
          not_supplied, the rendered caption MUST begin with an explicit
          planned/placeholder/hypothesis marker in the manuscript language,
          such as "Planned evaluation placeholder:" / "计划评估占位：" or
          "Hypothesis H1:" / "假设 H1：". This is the only case where you
          MUST rewrite a noncompliant caption_hint. Do not append a categorical
          observed outcome; phrase comparisons as a question, hypothesis, or
          future test, and leave unknown outcomes TBD / 待实验确定.

          Normalize literal Unicode Greek math symbols to LaTeX math macros
          such as ``\(\alpha\)``, ``\(\delta\)``, and ``\(\varepsilon\)``.
          Do not emit Unicode U+2011, U+2013, or U+2014 punctuation; use an
          ASCII hyphen or LaTeX ``--`` / ``---`` punctuation. Label MUST be
          ``\label{fig:<id>}`` so analysis_outline
          and final_manuscript_package can ``\ref{fig:<id>}`` them.

          Experiment design:
          {{ outputs.experiment_design | truncate(8000) }}

          Reply with ONLY the concatenated LaTeX figure environments,
          one per FIGURE_PLAN entry, separated by a blank line. No
          preamble, no markdown, no commentary. Wrap the entire block
          between sentinel comments so downstream sanitizer can locate
          it:

          % BEGIN_FIGURE_PLACEHOLDERS
          \begin{figure}[t]
            \centering
            \fbox{\parbox{0.8\linewidth}{\centering\vspace{1em}
              \textbf{[Placeholder] fig1: line plot}\\
              x: training step (1k iter); y: validation accuracy (\%)\\
              groups: ours / baseline-A / baseline-B\\
              supports: RQ1
              \vspace{1em}}}
            \caption{<caption_hint>}
            \label{fig:fig1}
          \end{figure}

          \begin{figure}[t]
            ... (repeat per FIGURE_PLAN entry)
          \end{figure}
          % END_FIGURE_PLACEHOLDERS
    - id: table_placeholders
      label: "表占位"
      label_en: "Table placeholders"
      kind: llm_chat
      depends_on: [experiment_design]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        system: "You render LaTeX placeholder table environments from a structured table plan. Output is pure LaTeX, ready to inline into a manuscript."
        task: |
          For EACH table listed in TABLE_PLAN below, emit one LaTeX
          ``table`` environment with a ``tabular`` body. Use ``---`` or
          ``<TBD>`` for cells (DO NOT fabricate numbers). Every non-label data cell MUST be a placeholder;
          table headers and row labels may be concrete, but metric values, percentages,
          counts, scores, latency, costs, and confidence intervals must
          remain ``---`` or ``<TBD>`` until real experiments are supplied.
          Use booktabs (``\toprule``, ``\midrule``, ``\bottomrule``) for
          clean spacing. Wrap every ``tabular`` in
          ``\resizebox{\linewidth}{!}{...}`` so multi-column placeholders
          cannot overflow the text block.

          Never place literal Unicode Greek letters in headers, captions, or
          cells. Render them in math mode with named LaTeX macros, for example
          ``\(\alpha\)``, ``\(\delta\)``, and ``\(\varepsilon\)``. Do not
          emit Unicode U+2011, U+2013, or U+2014 punctuation; use an ASCII
          hyphen or LaTeX ``--`` / ``---`` punctuation.

          Header row comes from TABLE_PLAN columns; row labels come from
          rows_shape (expand the shape into concrete row names like
          "Baseline-A", "Baseline-B", "Ours", "Ours w/o module X", …).
          Caption normally comes from caption_hint. First read EVIDENCE_STATUS
          from the experiment design. When it is not_supplied, the rendered
          caption MUST begin with an explicit planned/placeholder/hypothesis
          marker in the manuscript language, such as "Planned evaluation
          placeholder:" / "计划评估占位：" or "Hypothesis H1:" / "假设 H1：".
          Rewrite a noncompliant hint rather than copying a categorical
          observed result. Comparisons must be framed as a question,
          hypothesis, or future test; unknown outcomes remain TBD / 待实验确定.
          Label MUST be ``\label{tab:<id>}``.

          Experiment design:
          {{ outputs.experiment_design | truncate(8000) }}

          Reply with ONLY the concatenated LaTeX table environments,
          one per TABLE_PLAN entry, between sentinel comments:

          % BEGIN_TABLE_PLACEHOLDERS
          \begin{table}[t]
            \centering
            \resizebox{\linewidth}{!}{
              \begin{tabular}{lccc}
                \toprule
                Method & Acc & F1 & Latency \\
                \midrule
                Baseline-A & --- & --- & --- \\
                Baseline-B & --- & --- & --- \\
                Ours       & --- & --- & --- \\
                \bottomrule
              \end{tabular}
            }
            \caption{<caption_hint>}
            \label{tab:tab1}
          \end{table}
          ... (repeat per TABLE_PLAN entry)
          % END_TABLE_PLACEHOLDERS
    - id: analysis_outline
      label: "分析大纲"
      label_en: "Analysis outline"
      kind: llm_chat
      depends_on: [experiment_design, figure_placeholders, table_placeholders]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        system: "You design analysis-chapter outlines that bind every figure/table to a claim and an analysis dimension."
        task: |
          Produce the Analysis chapter outline. Each subsection must
          ``\ref{fig:...}`` or ``\ref{tab:...}`` AT LEAST ONE artefact
          you actually have (do not reference figures/tables that don't
          exist in the placeholders below). Cover every ANALYSIS_DIMENSION
          from experiment_design.

          Experiment design:
          {{ outputs.experiment_design | truncate(8000) }}

          Figure placeholders (label IDs you may \ref):
          {{ outputs.figure_placeholders | truncate(3000) }}

          Table placeholders (label IDs you may \ref):
          {{ outputs.table_placeholders | truncate(3000) }}

          PAPER_MODE depth control:
          - FULL_MANUSCRIPT: 1 subsection per analysis dimension; each
            with interpretation_criteria (3 bullets) + threats_to_validity
            (1–2 bullets).
          - COMPACT_SKELETON: 1 subsection per dimension; interpretation_criteria
            (1 bullet); skip threats_to_validity.

          Evidence-safety rule:
          - Read EVIDENCE_STATUS from experiment_design and never upgrade it.
          - When it is not_supplied, every interpretation criterion must be a
            future test, decision rule, or boundary condition. Do not predict
            concrete percentages, numeric ranges, scores, latencies,
            convergence-round changes, ablation deltas, or categorical wins.
            Use TBD / 待实验确定 for every unknown outcome magnitude. Add depth
            through protocol rationale and threats to validity, not invented
            findings.

          Reply in this exact shape between sentinels:

          % BEGIN_ANALYSIS_OUTLINE
          \subsection{Performance}
          \label{sec:analysis-performance}
          References: \ref{fig:fig1}, \ref{tab:tab1}.
          Planned interpretation criteria:
          \begin{itemize}
            \item ...
          \end{itemize}
          Threats to validity:
          \begin{itemize}
            \item ...
          \end{itemize}

          \subsection{Ablation}
          ... (repeat per ANALYSIS_DIMENSION)
          % END_ANALYSIS_OUTLINE
    - id: outline
      label: "大纲"
      label_en: "Outline"
      kind: llm_chat
      depends_on: [source_pack, source_readiness_gate, experiment_design]
      with:
        system: "You design long-form LaTeX paper outlines with citation plans."
        task: |
          Create a paper outline matching TARGET_PAGES from paper_preferences
          research-paper outline with enough section depth for a substantial
          manuscript. Every section must name planned cite keys from the
          bibliography. Tie the Method section to experiment_design's
          variables/datasets/baselines and the Results section to the
          figure/table plan (by id).

          Paper preferences:
          {{ outputs.paper_preferences | truncate(2000) }}

          Source pack:
          {{ outputs.source_pack | truncate(6000) }}

          Experiment design:
          {{ outputs.experiment_design | truncate(6000) }}

          Cite keys hint:
          {{ outputs.refbib | truncate(8000) }}
    - id: citation_plan
      label: "引用计划"
      label_en: "Citation plan"
      kind: llm_chat
      depends_on: [outline, source_pack, source_readiness_gate, refbib]
      with:
        system: "You plan citation placement for clean BibTeX/LaTeX manuscripts. You ONLY use cite keys that exist in the provided bibliography — never invent keys."
        task: |
          Build a citation plan that follows CITATION_TARGET and
          CITATION_STRATEGY from paper_preferences. If the user did not give
          an explicit citation count, derive a target from target length,
          source availability, audience, and venue style instead of using a
          fixed number. Use only keys that appear in the BibTeX below (every
          key must be present verbatim — verify by string match before you
          write it). Attach citations to claims, not paragraphs in bulk.
          Treat source_pack.USABLE_KEYS as the authoritative allowlist; merely
          appearing in the raw bibliography is not enough. If SOURCE_STATUS is
          insufficient or USABLE_REFERENCE_COUNT is below CITATION_TARGET,
          return CITATION_PLAN_STATUS: blocked with the concrete found/target
          count and emit no \cite commands or placeholder assignments. Never
          use an excluded or unrelated source merely to satisfy a count.

          Topic and mode:
          TOPIC: {{ outputs.paper_contract | truncate(1200) }}, MODE: {{ outputs.paper_contract | truncate(400) }}, PAGES: {{ outputs.paper_contract | truncate(400) }}

          Outline:
          {{ outputs.outline | truncate(6000) }}

          Source pack:
          {{ outputs.source_pack | truncate(8000) }}

          Bibliography (authoritative — cite keys MUST come from here):
          {{ outputs.refbib | truncate(8000) }}

          Paper preferences (authoritative for length/citation targets):
          {{ outputs.paper_preferences | truncate(2000) }}
    # ─── Plan→Write→Unify (FULL_MANUSCRIPT mode only) ──────────────────
    # The explicit full path writes section-by-section, unifies the manuscript,
    # runs quality gates, compiles a PDF, and delivers the artifact.
    - id: writing_plan
      label: "写作计划"
      label_en: "Writing plan"
      kind: llm_chat
      depends_on: [paper_preferences, outline, citation_plan, experiment_design, figure_placeholders, table_placeholders, analysis_outline, refbib]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        system: "You build a writing blueprint for a long-form academic manuscript. The blueprint is consumed verbatim by per-section authors; precision matters more than prose."
        task: |
          Synthesize the upstream planning outputs into a single
          authoritative WRITING_PLAN that every section author must
          obey. Lock terminology, notation, claim mapping, and
          per-section length budget BEFORE any prose is written.

          Paper facts:
          TOPIC: {{ outputs.paper_contract | truncate(1200) }}
          MODE: {{ outputs.paper_contract | truncate(400) }}
          LANGUAGE: {{ outputs.paper_contract | truncate(400) }}
          TARGET_PAGES: {{ outputs.paper_contract | truncate(400) }}
          AUDIENCE: {{ outputs.paper_contract | truncate(400) }}

          Preferences:
          {{ outputs.paper_preferences | truncate(2000) }}

          Outline:
          {{ outputs.outline | truncate(6000) }}

          Experiment design:
          {{ outputs.experiment_design | truncate(6000) }}

          Citation plan:
          {{ outputs.citation_plan | truncate(6000) }}

          Bibliography (cite keys MUST come from here):
          {{ outputs.refbib | truncate(4000) }}

          Figure placeholders (IDs only):
          {{ outputs.figure_placeholders | truncate(1500) }}

          Table placeholders (IDs only):
          {{ outputs.table_placeholders | truncate(1500) }}

          Length/citation budget rules:
          - Treat paper_preferences.LENGTH_STRATEGY and TARGET_LENGTH as
            authoritative; do not use a fixed default page or word budget when
            the user requested a different length.
          - This writing plan is the length-control point. Solve length by
            allocating enough section scope, subclaims, evidence, analysis,
            and limitations now; do not assume a downstream checker will fix
            an undersized manuscript later.
          - Convert the requested compiled-page target into an approximate
            total word budget using the paper language, figure/table count,
            and venue style. For normal academic article formatting, set the
            minimum total target_words to at least TARGET_PAGES × 820 English
            words (or the equivalent dense prose units for non-English text).
            Do not reduce below TARGET_PAGES × 760 for figures/tables; instead
            add analysis, limitations, related-work synthesis, and method detail.
          - Allocate words across sections according to the requested paper
            type and contribution shape. A method-heavy paper should give
            more budget to Method; an empirical paper should give more to
            Experiments/Results; a survey should give more to Related Work.
          - The sum of PER_SECTION_BLUEPRINT.*.target_words must meet or
            exceed the minimum total target_words implied by TARGET_PAGES. If
            the target is 12 pages, the blueprint should normally allocate at
            least 9,840 total words across abstract/introduction/related_work/
            method/experiments/discussion/conclusion.
          - In every PER_SECTION_BLUEPRINT entry, target_words is a
            lower-bound writing budget. It is not a ceiling. Give each
            section enough planned subclaims, paragraphs, evidence, analysis,
            and transitions that a section author can satisfy at least 90% of
            target_words without padding.
          - Do not return an undersized section from any non-abstract section author.
          - Treat paper_preferences.CITATION_TARGET and CITATION_STRATEGY as
            authoritative. If they are AUTO, derive a citation budget
            proportional to target length and available verified references;
            never invent citations to hit a count.
          - Return explicit per-section target_words and cite_keys budgets
            that downstream section authors must obey.

          Return EXACTLY this structure (no preamble, no markdown headings):

          TITLE:
          <final paper title, ≤16 words>

          EVIDENCE_STATUS:
          <copy EVIDENCE_STATUS from paper_preferences verbatim>

          ABSTRACT_DRAFT:
          <120-220 word draft abstract — section authors may polish but
          may not change the thesis, scope, terminology, or
          PLACEHOLDER_RESULT_TOKEN. Do not invent empirical numbers.>

          NARRATIVE_ARC:
          - thesis: <one sentence>
          - story_beats:
              1. <intro beat>
              2. <related-work positioning>
              3. <method core idea>
              4. <experimental verification>
              5. <discussion+conclusion takeaway>

          KEY_CLAIMS:
          - C1: <one sentence, must be defensible by an experiment>
          - C2: ...
          - ...
          - Cn: ... (5-8 total)

          NOTATION_LOCK:
          - symbol: $\theta$  meaning: model parameters
          - symbol: $\mathcal{D}$  meaning: dataset
          - (list every symbol that will appear in math)

          TERMINOLOGY_LOCK:
          - "ours" (proposed method)  forbidden_aliases: ["our method", "the proposed", "本文方法", "the method"]
          - "DPR" (baseline)  forbidden_aliases: ["dpr", "Dpr"]
          - ... (every named entity that appears more than once)

          PER_SECTION_BLUEPRINT:
            abstract:
              target_words: <int>
              key_claims: [C1, C2, ...]
              cite_keys: []           # abstract never cites
              figures: []
              must_mention: [TITLE, PLACEHOLDER_RESULT_TOKEN, EVIDENCE_STATUS]
            introduction:
              target_words: <int>
              key_claims: [C1, C2]
              cite_keys: [ref_x, ref_y, ...]   # from citation_plan
              figures: []
              structure: [motivation, problem, contributions]
              contributions_count: <int>
            related_work:
              target_words: <int>
              key_claims: []
              cite_keys: [ref_x, ...]
              figures: []
              structure: [survey by axis]
            method:
              target_words: <int>
              key_claims: [C3, C4]
              cite_keys: [...]
              figures: [fig1, ...]
              tables: []
              structure: [overview → component A → component B → algorithm box]
              notation_introduced: [θ, f_φ, ...]
            experiments:
              target_words: <int>
              key_claims: [C5, C6]
              cite_keys: [...]
              figures: [fig2, ...]
              tables: [tab1, ...]
              structure: [setup → main results → ablations]
              must_include_baselines: [...]
            discussion:
              target_words: <int>
              key_claims: [C7]
              cite_keys: [...]
              figures: []
              structure: [insights → limitations → threats_to_validity]
            conclusion:
              target_words: <int>
              key_claims: [C1-Cn 重申]
              cite_keys: []
              figures: []
              must_call_back_to_abstract: yes

          CROSS_SECTION_DEPENDENCIES:
          - method.NOTATION_LOCK symbols MUST be reused verbatim in experiments + discussion
          - intro.contributions_count MUST equal method.structure step count
          - abstract.PLACEHOLDER_RESULT_TOKEN == experiments.PLACEHOLDER_RESULT_TOKEN
          - experiments, discussion, and conclusion MUST use the same
            qualitative result placeholder until real experiment outputs
            are supplied; do not state exact numeric improvements.
          - when EVIDENCE_STATUS is not_supplied, every results-facing section
            MUST explicitly say that no empirical results were supplied and
            describe evaluation in planned/future tense only.
          - In that mode, every unknown outcome value must be written as TBD
            or 待实验确定. Never replace it with a guessed percentage, score,
            latency, effect size, or other concrete forecast.
          - In that mode, every figure/table caption must be explicitly
            labeled as a planned evaluation placeholder or hypothesis. A
            caption may name concrete setup values or a predefined metric
            threshold, but must not state a categorical observed finding.
          - All section authors must avoid Unicode U+2011, U+2013, and U+2014
            punctuation. Use an ASCII hyphen, LaTeX ``--`` / ``---``, or
            native full-width sentence punctuation instead.

          WRITING_VOICE:
          - tense: <e.g. "we present / we observe", active>
          - perspective: <e.g. third-person except contributions list>
          - formality: academic; no contractions, no marketing language
          - language: {{ outputs.paper_contract | truncate(400) }}

          PLACEHOLDER_RESULT_TOKEN:
          <one stable phrase such as "the planned evaluation will test
          the thesis across performance, robustness, and efficiency axes";
          use this same phrase in abstract, experiments, discussion, and
          conclusion. Do not invent empirical numbers.>
    - id: section_abstract
      label: "摘要段"
      label_en: "Abstract section"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the ABSTRACT section. Follow the writing plan
          and produce a single dense paragraph 4-6 sentences covering
          problem → approach → key result → significance.

          section: abstract
          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          outline:
          {{ outputs.outline | truncate(3000) }}

          citation_plan:
          {{ outputs.citation_plan | truncate(3000) }}

          cite_keys_hint:
          {{ outputs.refbib | truncate(2000) }}

          Output rules:
          - Use \begin{abstract} ... \end{abstract}.
          - Do not include \cite{...}.
          - Match TERMINOLOGY_LOCK and NOTATION_LOCK exactly.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.abstract.target_words
          - For the abstract, follow the 4-6 sentence contract first; do not
            expand it just to satisfy the long-form page target.
          - If EVIDENCE_STATUS is not_supplied, state explicitly that empirical
            results are not yet available; describe only the planned evaluation
            and never claim observed findings.
          - Only output the LaTeX fragment. No commentary, no fences.
    - id: section_introduction
      label: "引言段"
      label_en: "Introduction section"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan, section_abstract]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the INTRODUCTION section.

          section: introduction
          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          previous_section_tail (last paragraphs of the abstract):
          {{ outputs.section_abstract | truncate(2000) }}

          outline:
          {{ outputs.outline | truncate(3000) }}

          citation_plan (your assigned cite keys are listed under introduction:):
          {{ outputs.citation_plan | truncate(3000) }}

          cite_keys_hint (only these keys exist in the bibliography):
          {{ outputs.refbib | truncate(2000) }}

          Output rules:
          - Start with \section{Introduction}.
          - Structure: motivation → problem → prior-work clusters → gap →
            our contributions (numbered \begin{enumerate}) → paper roadmap.
          - Use only cite keys assigned to introduction in citation_plan,
            and only keys present in cite_keys_hint.
          - Match TERMINOLOGY_LOCK and NOTATION_LOCK exactly.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.introduction.target_words.
          - Length floor: target_words is a lower-bound writing budget. Do
            not return until the section reaches at least 90% of target_words;
            expand with plan-aligned motivation, prior-work contrast,
            contribution detail, and roadmap prose if short. Do not return an
            undersized section.
          - Output ONLY the LaTeX fragment for this section. No fences.
    - id: section_related_work
      label: "相关工作"
      label_en: "Related work"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan, section_introduction]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the RELATED WORK section.

          section: related_work

          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          previous_section_tail (last paragraphs of the introduction):
          {{ outputs.section_introduction | truncate(2000) }}

          outline:
          {{ outputs.outline | truncate(3000) }}

          citation_plan (your assigned cite keys are listed under related_work:):
          {{ outputs.citation_plan | truncate(3000) }}

          cite_keys_hint (only these keys exist in the bibliography):
          {{ outputs.refbib | truncate(2500) }}

          Output rules:
          - Start with \section{Related Work}.
          - Survey by 2-4 thematic axes (e.g. efficiency / fidelity /
            agentic / dataset construction). Use \subsection for each.
          - Cite from your assigned keys; do not introduce new claims.
          - Do NOT include figures/tables here.
          - Match TERMINOLOGY_LOCK exactly.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.related_work.target_words.
          - Length floor: target_words is a lower-bound writing budget. Do
            not return until the section reaches at least 90% of target_words;
            expand with plan-aligned thematic comparisons, citation synthesis,
            and explicit gap analysis if short. Do not return an undersized
            section.
          - Output ONLY the LaTeX fragment. No fences, no preamble.
    - id: section_method
      label: "方法段"
      label_en: "Methods section"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan, section_related_work, figure_placeholders]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the METHOD section.

          section: method
          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          previous_section_tail (last paragraphs of related work):
          {{ outputs.section_related_work | truncate(2000) }}

          outline:
          {{ outputs.outline | truncate(3000) }}

          citation_plan:
          {{ outputs.citation_plan | truncate(3000) }}

          cite_keys_hint:
          {{ outputs.refbib | truncate(2500) }}

          figure_placeholders (you may reference these via \ref{fig:<id>} when relevant):
          {{ outputs.figure_placeholders | truncate(2000) }}

          Output rules:
          - Start with \section{Method}.
          - Use \subsection{Setup}, \subsection{Algorithm} (or {Approach}),
            \subsection{Instrumentation}, and \subsection{Baselines}.
          - Introduce notation per writing_plan.NOTATION_LOCK
            (every symbol used later in experiments/discussion MUST
            be defined here).
          - You may inline ONE figure environment from figure_placeholders
            that supports method exposition; reference it via \ref{fig:<id>}.
          - If pseudocode is useful, use only the `algorithm` float with the
            `algorithmic` environment and uppercase commands such as
            `\STATE`, `\FOR`, `\ENDFOR`, `\IF`, and `\ENDIF`. The manuscript
            template loads `algorithm` + `algorithmic`. Do not use
            `algorithm2e`, `algpseudocode`, or commands from another dialect.
          - Match TERMINOLOGY_LOCK / NOTATION_LOCK exactly.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.method.target_words.
          - Length floor: target_words is a lower-bound writing budget. Do
            not return until the section reaches at least 90% of target_words;
            expand with plan-aligned assumptions, definitions, algorithmic
            detail, instrumentation, and reproducibility notes if short. Do
            not return an undersized section.
          - Output ONLY the LaTeX fragment. No fences.
    - id: section_experiments
      label: "实验段"
      label_en: "Experiments section"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan, section_method, figure_placeholders, table_placeholders]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the EXPERIMENTS / RESULTS section. Use the
          paper-section-author "results" contract.

          section: results
          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          previous_section_tail (last paragraphs of method):
          {{ outputs.section_method | truncate(2500) }}

          outline:
          {{ outputs.outline | truncate(3000) }}

          citation_plan:
          {{ outputs.citation_plan | truncate(3000) }}

          cite_keys_hint:
          {{ outputs.refbib | truncate(2500) }}

          figure_placeholders (inline ALL remaining figures here):
          {{ outputs.figure_placeholders | truncate(4000) }}

          table_placeholders (inline ALL tables here):
          {{ outputs.table_placeholders | truncate(4000) }}

          Output rules:
          - Start with \section{Experiments}.
          - Inline EVERY figure and table from figure_placeholders /
            table_placeholders that has not already been inlined in method.
          - Reference via \ref{fig:<id>} and \ref{tab:<id>}.
          - Structure: \subsection{Setup} → \subsection{Main Results} →
            \subsection{Ablations} → \subsection{Sensitivity}.
          - Use writing_plan.PLACEHOLDER_RESULT_TOKEN for the headline
            evidence claim. Do not state exact numeric improvements,
            percentages, scores, latency reductions, or win rates unless
            they are explicitly present in user-provided experiment data.
          - If EVIDENCE_STATUS is not_supplied, include the literal sentence
            "No empirical results were supplied; this section specifies the
            planned evaluation." and keep ALL setup, results, ablation, and
            sensitivity prose in proposed/planned/future tense. Do not claim
            that a dataset was collected, participants were recruited, or a
            hypothesis was confirmed.
          - Use ONLY notation/terminology locked in writing_plan.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.experiments.target_words.
          - Length floor: target_words is a lower-bound writing budget. Do
            not return until the section reaches at least 90% of target_words;
            expand with plan-aligned setup, metric rationale, baseline
            comparison, ablation interpretation, sensitivity analysis, and
            failure-case discussion if short. Do not return an undersized
            section.
          - Output ONLY the LaTeX fragment. No fences.
    - id: section_discussion
      label: "讨论段"
      label_en: "Discussion section"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan, section_experiments, analysis_outline]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the DISCUSSION section.

          section: discussion
          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          previous_section_tail (last paragraphs of experiments):
          {{ outputs.section_experiments | truncate(2500) }}

          outline:
          {{ outputs.outline | truncate(3000) }}

          citation_plan:
          {{ outputs.citation_plan | truncate(3000) }}

          cite_keys_hint:
          {{ outputs.refbib | truncate(2500) }}

          analysis_outline (use this as the structural blueprint):
          {{ outputs.analysis_outline | truncate(4000) }}

          Output rules:
          - Start with \section{Discussion}.
          - Inline the analysis_outline subsections verbatim where they fit,
            but expand each with 1-2 paragraphs of substantive commentary.
            Reference concrete experiment results only when EVIDENCE_STATUS is
            supplied; otherwise discuss planned interpretation criteria and
            explicitly say that empirical results are not yet available.
            Unknown outcomes must remain TBD / 待实验确定; do not predict exact
            percentages, scores, latency reductions, or ablation effects.
          - End the section with explicit \subsection{Limitations} and
            \subsection{Threats to Validity}.
          - Match TERMINOLOGY_LOCK / NOTATION_LOCK exactly.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.discussion.target_words.
          - Length floor: target_words is a lower-bound writing budget. Do
            not return until the section reaches at least 90% of target_words;
            expand with plan-aligned interpretation, boundary conditions,
            limitations, threats to validity, and implications if short. Do
            not return an undersized section.
          - Output ONLY the LaTeX fragment.
    - id: section_conclusion
      label: "结论段"
      label_en: "Conclusion section"
      kind: agent
      skill: paper-section-author
      depends_on: [writing_plan, section_discussion, section_abstract]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        task: |
          You are writing the CONCLUSION section. Must close the loop on the abstract.

          section: conclusion

          writing_plan:
          {{ outputs.writing_plan | truncate(8000) }}

          evidence_contract (authoritative even if writing_plan is truncated):
          {{ outputs.paper_contract | truncate(1200) }}

          abstract (the conclusion must echo its claims):
          {{ outputs.section_abstract | truncate(1500) }}

          previous_section_tail (discussion ending):
          {{ outputs.section_discussion | truncate(2000) }}

          Output rules:
          - Start with \section{Conclusion}.
          - Cover: 1) restated thesis + headline result status, 2) key contributions
            reiterated, 3) scope and limitations, 4) future-work pointer. Use
            as many concise paragraphs as the writing_plan target_words
            requires; do not cap the conclusion at 2-3 paragraphs when the
            requested page target is long.
          - No new claims, no new figures, no \cite{}.
          - If EVIDENCE_STATUS is not_supplied, do not state that the method
            worked, improved, outperformed, or was statistically significant.
            Restate that evaluation is planned and results remain unavailable.
          - Match TERMINOLOGY_LOCK exactly.
          - target_words from writing_plan.PER_SECTION_BLUEPRINT.conclusion.target_words.
          - Length floor: target_words is a lower-bound writing budget. Do
            not return until the section reaches at least 90% of target_words;
            expand with plan-aligned synthesis and implications if short. Do
            not return an undersized section.
          - Output ONLY the LaTeX fragment.
    - id: persist_sections
      label: "保存章节"
      label_en: "Save sections"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [section_abstract, section_introduction, section_related_work, section_method, section_experiments, section_discussion, section_conclusion]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        # Persist large section bodies to disk and return only a compact
        # manifest. This keeps later LLM steps from repeatedly ingesting the
        # full manuscript and reduces repeated context-compaction pressure.
        payload: |
          {
            "operation": "persist_sections",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "sections": {
              "abstract": {{ outputs.section_abstract | tojson }},
              "introduction": {{ outputs.section_introduction | tojson }},
              "related_work": {{ outputs.section_related_work | tojson }},
              "method": {{ outputs.section_method | tojson }},
              "experiments": {{ outputs.section_experiments | tojson }},
              "discussion": {{ outputs.section_discussion | tojson }},
              "conclusion": {{ outputs.section_conclusion | tojson }}
            }
          }
    - id: assemble_manuscript_tex
      label: "组装 TEX"
      label_en: "Assemble TEX"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [writing_plan, persist_sections, refbib]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        # Concatenate section artifact files into a full LaTeX document and
        # write it to paper/<meta_run_id>/paper.tex. Return a compact manifest instead of
        # echoing the full manuscript back into the meta context.
        payload: |
          {
            "operation": "assemble_manuscript_tex",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "bib_text": {{ outputs.refbib | tojson }},
            "writing_plan": {{ outputs.writing_plan | tojson }},
            "topic": {{ (outputs.paper_contract | truncate(400)) | tojson }}
          }
    - id: consistency_pass
      label: "一致性检查"
      label_en: "Consistency check"
      kind: llm_chat
      depends_on: [writing_plan, assemble_manuscript_tex]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      with:
        system: "You are the consistency auditor for an academic manuscript. You inspect compact manifests and return actionable checks without rewriting the full manuscript."
        task: |
          Review the assembled manuscript manifest against the writing plan.
          Do NOT request or reproduce the full manuscript text in this step.
          The full manuscript is persisted on disk; keep this output compact
          so long paper runs do not trigger repeated context compaction.

          Drift to check:
          1. Terminology: any synonym variant of a TERMINOLOGY_LOCK term
             should be flagged for later repair.
          2. Notation: any math symbol that disagrees with NOTATION_LOCK
             should be flagged.
          3. Numbers: if abstract / experiments / discussion mention the
             same headline metric with different values, flag the drift.
          4. Cite keys: ensure every \cite{...} key exists in the
             REFERENCES_BIB block; citation_map performs the exact parse.
          5. Section ordering: keep abstract → intro → related → method →
             experiments → discussion → conclusion.

          Writing plan (authoritative):
          {{ outputs.writing_plan | truncate(8000) }}

          Assembled manuscript manifest:
          {{ outputs.assemble_manuscript_tex | truncate(2000) }}

          Output EXACTLY:
          MANUSCRIPT_PATH: <copy MANUSCRIPT_PATH from assembled manifest>
          REFERENCES_PATH: <copy REFERENCES_PATH from assembled manifest>
          COMPILE_NOTES:
          - consistency_findings: <one line per possible drift, OR "none">
          CONTEXT_POLICY: artifact-only; full manuscript omitted from prompt/output

    - id: final_manuscript_package
      label: "终稿打包"
      label_en: "Final package"
      kind: llm_chat
      depends_on: [paper_contract, outline, citation_plan, refbib, figure_placeholders, table_placeholders, analysis_outline]
      when: "'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        system: "You write clean LaTeX manuscripts. Output only the requested manuscript package. NEVER invent cite keys — every \\cite{...} you emit MUST exist verbatim in REFERENCES_BIB below."
        task: |
          Draft a compact manuscript package. The output must be clean
          LaTeX-ready paper text, not planning notes. Do not include markdown
          fences, chat commentary, progress notes, or tool logs.

          Paper mode:
          TOPIC: {{ outputs.paper_contract | truncate(1200) }}, MODE: {{ outputs.paper_contract | truncate(400) }}, PAGES: {{ outputs.paper_contract | truncate(400) }}

          Mode behavior:
          - COMPACT_SKELETON is the lower-latency authoring path, but its
            compiled artifact MUST still meet TARGET_PAGES. Produce a coherent
            compact manuscript, not a one-page outline followed by a promise
            to expand later. Size substantive MANUSCRIPT_TEX prose to at least
            TARGET_PAGES x 550 English words, or TARGET_PAGES x 950 CJK
            characters, before returning. For the default four-page contract,
            this normally means at least 2,200 English words (or about 3,800
            CJK characters), distributed across every required section.
            The final package MUST also include a concise manuscript plan,
            target-length expansion plan, limitations/threats-to-validity,
            and reference placeholders sized to the requested/derived citation
            strategy when verified BibTeX entries are unavailable. Put the
            complete MANUSCRIPT_TEX first so all required sections survive any
            downstream truncation. Do not use blank pages, repeated paragraphs,
            oversized headings, spacing tricks, or filler to reach the target.

          CITATION CONTRACT (load-bearing):
          - DO NOT invent cite keys. Use ONLY keys that appear verbatim in
            REFERENCES_BIB below.
          - DO NOT cite a key that REFERENCES_BIB does not contain.
          - Every claim that needs evidence MUST cite at least one key from
            REFERENCES_BIB.
          - Distribute citations according to paper_preferences.CITATION_STRATEGY;
            avoid repeatedly citing one key when enough verified sources exist.
          - If REFERENCES_BIB is empty or lacks enough verified entries, do
            not emit \cite{...}. Use visible placeholders such as
            [REF-01 needed: agent benchmark survey] in the LaTeX text and
            list them under REFERENCE_PLACEHOLDERS instead. Placeholder
            references are safer than fabricated BibTeX.

          EMPIRICAL EVIDENCE CONTRACT (load-bearing):
          - Read EVIDENCE_STATUS from paper_contract; never upgrade it.
          - When EVIDENCE_STATUS is not_supplied, include the exact disclosure
            "No empirical results were supplied; the evaluation described here
            is planned." in MANUSCRIPT_TEX.
          - In that case, describe datasets, participants, experiments,
            ablations, sensitivity analyses, results, discussion, abstract,
            and conclusion in proposed/planned/future tense only.
          - Do not invent completed sample counts, measurements, p-values,
            effect sizes, confidence intervals, numeric improvements, observed
            findings, or novelty claims such as "first experimental evidence".
          - Keep every result table cell as <TBD> or --- and every finding as a
            falsifiable hypothesis or planned analysis criterion.
          - Every figure/table caption must explicitly identify itself as a
            planned evaluation placeholder or hypothesis. It may state setup
            values or a predefined target threshold, but may not present a
            categorical observed result.

          LATEX GLYPH CONTRACT (load-bearing):
          - Never emit literal Unicode Greek letters in prose, captions, or
            tables. Use named macros in math mode, e.g. ``\(\alpha\)``,
            ``\(\delta\)``, and ``\(\varepsilon\)``.
          - Do not emit Unicode U+2011, U+2013, or U+2014 punctuation. Use an
            ASCII hyphen, LaTeX ``--`` / ``---``, or native full-width
            sentence punctuation instead.
          - Wrap every ``tabular`` in ``\resizebox{\linewidth}{!}{...}``.

          FIGURE/TABLE CONTRACT:
          - Inline the figure_placeholders block verbatim into Results.
          - Inline the table_placeholders block verbatim into Method or
            Results (split by purpose).
          - Inline the analysis_outline block verbatim into Discussion.
          - Reference figures/tables via \\ref{fig:<id>} and \\ref{tab:<id>}
            where they appear in the body; never reference an id not present
            in the placeholders.

          Paper preferences:
          {{ outputs.paper_preferences | truncate(2000) }}

          Outline:
          {{ outputs.outline | truncate(8000) }}

          Citation plan:
          {{ outputs.citation_plan | truncate(8000) }}

          Figure placeholders (inline this verbatim somewhere in Results):
          {{ outputs.figure_placeholders | truncate(4000) }}

          Table placeholders (inline this verbatim in Method/Results):
          {{ outputs.table_placeholders | truncate(4000) }}

          Analysis outline (inline this verbatim in Discussion):
          {{ outputs.analysis_outline | truncate(4000) }}

          Bibliography (cite keys MUST come from here):
          {{ outputs.refbib | truncate(8000) }}

          CRITICAL OUTPUT CONTRACT (load-bearing — the downstream
          compile_pdf step parses these markers literally):

          - The MANUSCRIPT_TEX section is MANDATORY and MUST come first.
            It MUST start with the literal token `MANUSCRIPT_TEX:` on its
            own line, immediately followed by `\documentclass{article}`
            and end with `\end{document}`. Do NOT wrap in ```latex
            fences. Do NOT prefix with markdown headings.
          - If you find yourself running out of tokens, shorten section
            bodies — DO NOT omit MANUSCRIPT_TEX. A short complete
            \documentclass…\end{document} block is FAR more useful than
            a long MANUSCRIPT_PLAN with no LaTeX.
          - REFERENCES_BIB is the second mandatory section. Use
            `REFERENCES_BIB:` on its own line followed by BibTeX entries.
            If the bibliography is empty, output `REFERENCES_BIB:`
            followed by a single line `% no verified references` (the
            \cite{} keys in MANUSCRIPT_TEX should then be visible
            placeholders, not BibTeX-keyed cites).

          Return EXACTLY in this order (no preamble, no markdown headings):

          MANUSCRIPT_TEX:
          \documentclass{article}
          \usepackage{fontspec}
          \setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}
          \usepackage{graphicx}
          \usepackage{booktabs}
          \usepackage{amsmath}
          \usepackage{algorithm}
          \usepackage{algorithmic}
          \usepackage[hidelinks]{hyperref}
          \title{...}
          \author{...}
          \date{\today}
          \begin{document}
          \maketitle
          \begin{abstract}...\end{abstract}
          \section{Introduction}...
          \section{Related Work}...
          \section{Method}...
          \section{Experiments}...
          (inline the figure_placeholders, table_placeholders, and
          analysis_outline blocks verbatim where appropriate)
          \section{Discussion}...
          \section{Limitations}...
          \section{Threats to Validity}...
          \section{Conclusion}...
          \bibliographystyle{plain}
          \bibliography{references}
          \end{document}

          REFERENCES_BIB:
          <BibTeX entries copied verbatim from the provided bibliography —
          only the entries actually cited in MANUSCRIPT_TEX. If empty,
          output a single `% no verified references` line.>

          MANUSCRIPT_PLAN:
          - (optional) section-by-section plan with target pages and
            contribution. Skip this section if MANUSCRIPT_TEX is already
            tight on tokens.

          TARGET_LENGTH_EXPANSION_PLAN:
          - For COMPACT_SKELETON, list the concrete section expansions,
            extra experiments, figures, tables, and citation work needed
            to grow this package into the user-requested target length.

          REFERENCE_PLACEHOLDERS:
          - (optional) placeholder reference notes if REFERENCES_BIB is
            empty or sparse.

          COMPILE_NOTES:
          - <short note about figure/reference assumptions>
    - id: latex_sanitizer
      label: "LaTeX 清理"
      label_en: "LaTeX cleanup"
      kind: skill_exec
      skill: paper-latex-sanitizer
      depends_on: [paper_contract, final_manuscript_package, consistency_pass, assemble_manuscript_tex]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "user_request": {{ (inputs.user_message | truncate(4000)) | tojson }},
            "manuscript_package": {{ (outputs.get('consistency_pass') or outputs.get('assemble_manuscript_tex') or outputs.get('final_manuscript_package', '')) | tojson }}
          }
    - id: materialize_manuscript
      label: "固化稿件"
      label_en: "Materialize manuscript"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [paper_contract, latex_sanitizer]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "operation": "materialize_manuscript",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "manuscript_package": {{ outputs.latex_sanitizer | tojson }}
          }
    - id: paper_length_preflight
      label: "篇幅预检"
      label_en: "Length preflight"
      kind: skill_exec
      skill: paper-length-gate
      depends_on: [paper_contract, materialize_manuscript]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        # Report-only makes the target-correlated deficit available to the
        # bounded authoring repair below. The later paper_length_gate remains
        # fail-closed and must pass before any compiler is invoked.
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "manuscript_package": {{ outputs.materialize_manuscript | tojson }},
            "report_only": true
          }
    - id: precompile_length_expansion
      label: "篇幅扩写"
      label_en: "Pre-compile expansion"
      kind: llm_chat
      depends_on: [paper_contract, paper_preferences, outline, materialize_manuscript, paper_length_preflight]
      when: "'below target-correlated readiness floor' in outputs.paper_length_preflight"
      with:
        system: "You author one substantive, evidence-safe LaTeX expansion fragment for an undersized academic manuscript."
        task: |
          The deterministic preflight found that this manuscript is too small
          for its requested compiled-page contract. Produce one body-only
          expansion that closes the reported content-unit deficit with a 15%
          safety margin. This is the first of at most two bounded repair
          attempts; make it substantive enough to pass now.

          Paper contract:
          {{ outputs.paper_contract | truncate(1200) }}

          Preferences:
          {{ outputs.paper_preferences | truncate(2000) }}

          Outline:
          {{ outputs.outline | truncate(3500) }}

          Deterministic deficit report (authoritative):
          {{ outputs.paper_length_preflight | truncate(2000) }}

          Existing sanitized source excerpt (compact packages are inline;
          full manuscripts may provide only an artifact manifest):
          {{ outputs.latex_sanitizer | truncate(12000) }}

          Run-owned artifact manifest:
          {{ outputs.materialize_manuscript | truncate(2000) }}

          Rules:
          - Write only new subsection-level prose that deepens method
            rationale, evaluation design, limitations, boundary conditions,
            and reproducibility. Do not repeat paragraphs or pad formatting.
          - Do not emit \documentclass, \begin{document}, \end{document},
            \section, \bibliography, \input, \include, \includegraphics,
            \usepackage, or any \cite command. Existing citations and document
            boundaries are locked.
          - Preserve EVIDENCE_STATUS. If evidence is not_supplied, use
            planned/future tense, keep outcomes TBD, and invent no results,
            measurements, sources, or citations.
          - Match the manuscript language. Use ASCII/LaTeX-safe punctuation
            and named math macros rather than literal Unicode Greek.
          - Emit at least the missing MINIMUM_CONTENT_UNITS minus
            ESTIMATED_CONTENT_UNITS, plus 15%. Cap the fragment at 3,000
            English words or 5,000 CJK characters; if the requested target
            cannot be repaired within that bound, still use the full budget
            and let the strict gate fail honestly.

          Return exactly:
          % BEGIN_LENGTH_EXPANSION
          \subsection{Target-Length Elaboration}
          <substantive LaTeX body only>
          % END_LENGTH_EXPANSION
    - id: apply_precompile_length_expansion
      label: "应用篇幅扩写"
      label_en: "Apply pre-compile expansion"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [paper_contract, materialize_manuscript, paper_length_preflight, precompile_length_expansion]
      when: "'below target-correlated readiness floor' in outputs.paper_length_preflight"
      with:
        payload: |
          {
            "operation": "apply_length_expansion",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "repair_id": "precompile",
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "manuscript_package": {{ outputs.materialize_manuscript | tojson }},
            "expansion": {{ outputs.precompile_length_expansion | tojson }}
          }
    - id: length_repair_sanitizer
      label: "扩写清理"
      label_en: "Expansion cleanup"
      kind: skill_exec
      skill: paper-latex-sanitizer
      depends_on: [paper_contract, materialize_manuscript, apply_precompile_length_expansion]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "user_request": {{ (inputs.user_message | truncate(4000)) | tojson }},
            "manuscript_package": {{ (outputs.get('apply_precompile_length_expansion') or outputs.materialize_manuscript) | tojson }}
          }
    - id: citation_map
      label: "引用映射"
      label_en: "Citation mapping"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [length_repair_sanitizer, refbib]
      with:
        # Deterministically parse citations from artifact files instead of
        # sending the full manuscript back through an LLM.
        payload: |
          {
            "operation": "citation_map",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "manifest": {{ outputs.length_repair_sanitizer | tojson }},
            "refbib": {{ outputs.refbib | tojson }}
          }
    - id: paper_length_gate
      label: "篇幅门禁"
      label_en: "Length gate"
      kind: skill_exec
      skill: paper-length-gate
      depends_on: [paper_contract, length_repair_sanitizer]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "manuscript_package": {{ outputs.length_repair_sanitizer | tojson }}
          }
    - id: citation_integrity_gate
      label: "引用门禁"
      label_en: "Citation gate"
      kind: skill_exec
      skill: paper-citation-integrity-gate
      depends_on: [paper_contract, paper_preferences, citation_map, paper_length_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "paper_preferences": {{ outputs.paper_preferences | tojson }},
            "citation_map": {{ outputs.citation_map | tojson }}
          }
    - id: publication_quality_gate
      label: "发布质量门禁"
      label_en: "Publication quality gate"
      kind: skill_exec
      skill: paper-quality-gate
      depends_on: [paper_contract, length_repair_sanitizer, paper_length_gate, citation_integrity_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "length_gate": {{ outputs.paper_length_gate | tojson }},
            "citation_gate": {{ outputs.citation_integrity_gate | tojson }},
            "manuscript_package": {{ outputs.length_repair_sanitizer | tojson }}
          }
    - id: compile_probe
      label: "编译页数探测"
      label_en: "Compile page probe"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [length_repair_sanitizer, publication_quality_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        # This is a real compile. A short PDF is retained only as an internal
        # probe and can never reach publish_pdf. It gives the one bounded
        # page-shortfall repair an authoritative pypdf count.
        payload: |
          {
            "operation": "compile_pdf",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "manuscript_package": {{ outputs.length_repair_sanitizer | tojson }},
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "enforce_page_target": false,
            "reuse_existing": false
          }
    - id: page_shortfall_expansion
      label: "页数修复扩写"
      label_en: "Page shortfall expansion"
      kind: llm_chat
      depends_on: [paper_contract, paper_preferences, outline, length_repair_sanitizer, compile_probe]
      when: "'PDF_PAGE_TARGET_NOT_MET:' in outputs.compile_probe"
      with:
        system: "You repair one measured PDF page shortfall with substantive, evidence-safe LaTeX prose."
        task: |
          The real XeLaTeX+pypdf probe is shorter than TARGET_PAGES. This is
          the final automatic repair attempt. Add enough substantive body
          content for the measured missing pages plus one-quarter-page safety
          margin: about 650 English words or 1,050 CJK characters per missing
          page. Do not fake page count with spacing, blank pages, repeated
          prose, oversized headings, or forced page breaks.

          Paper contract:
          {{ outputs.paper_contract | truncate(1200) }}
          Compile probe (authoritative actual/target pages):
          {{ outputs.compile_probe | truncate(1200) }}
          Preferences:
          {{ outputs.paper_preferences | truncate(1800) }}
          Outline:
          {{ outputs.outline | truncate(3000) }}
          Existing compact package or full-manuscript writing plan (context
          only; never reproduce it verbatim):
          {{ (outputs.get('final_manuscript_package') or outputs.get('writing_plan', '')) | truncate(12000) }}

          Rules:
          - Write only new subsection-level method rationale, evaluation
            protocol, robustness analysis plan, limitations, and
            reproducibility detail. Do not repeat existing paragraphs.
          - Do not emit \documentclass, \begin{document}, \end{document},
            \section, \bibliography, \input, \include, \includegraphics,
            \usepackage, \cite, or forced page/spacing commands.
          - Preserve EVIDENCE_STATUS; when evidence is not_supplied, never
            invent measurements, findings, sources, or completed experiments.
          - Match the manuscript language. Cap this final fragment at 2,600
            English words or 4,500 CJK characters. The final strict compile
            must fail honestly if this bounded substantive repair is not enough.

          Return exactly:
          % BEGIN_LENGTH_EXPANSION
          \subsection{Measured Page-Target Elaboration}
          <substantive LaTeX body only>
          % END_LENGTH_EXPANSION
    - id: apply_page_shortfall_expansion
      label: "应用页数修复"
      label_en: "Apply page repair"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [paper_contract, length_repair_sanitizer, compile_probe, page_shortfall_expansion]
      when: "'PDF_PAGE_TARGET_NOT_MET:' in outputs.compile_probe"
      with:
        payload: |
          {
            "operation": "apply_length_expansion",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "repair_id": "page-shortfall",
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "manuscript_package": {{ outputs.length_repair_sanitizer | tojson }},
            "expansion": {{ outputs.page_shortfall_expansion | tojson }}
          }
    - id: final_latex_sanitizer
      label: "终稿 LaTeX 清理"
      label_en: "Final LaTeX cleanup"
      kind: skill_exec
      skill: paper-latex-sanitizer
      depends_on: [paper_contract, length_repair_sanitizer, apply_page_shortfall_expansion]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "user_request": {{ (inputs.user_message | truncate(4000)) | tojson }},
            "manuscript_package": {{ (outputs.get('apply_page_shortfall_expansion') or outputs.length_repair_sanitizer) | tojson }}
          }
    - id: final_page_length_gate
      label: "终稿篇幅门禁"
      label_en: "Final length gate"
      kind: skill_exec
      skill: paper-length-gate
      depends_on: [paper_contract, final_latex_sanitizer]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "manuscript_package": {{ outputs.final_latex_sanitizer | tojson }}
          }
    - id: final_publication_quality_gate
      label: "终稿质量门禁"
      label_en: "Final quality gate"
      kind: skill_exec
      skill: paper-quality-gate
      depends_on: [paper_contract, final_latex_sanitizer, final_page_length_gate, citation_integrity_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "length_gate": {{ outputs.final_page_length_gate | tojson }},
            "citation_gate": {{ outputs.citation_integrity_gate | tojson }},
            "manuscript_package": {{ outputs.final_latex_sanitizer | tojson }}
          }
    - id: compile_pdf
      label: "编译 PDF"
      label_en: "Compile PDF"
      kind: skill_exec
      skill: paper-artifact-runtime
      depends_on: [final_latex_sanitizer, final_publication_quality_gate, compile_probe]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        # Reuses the probe only when its exact TeX/Bib inputs already met the
        # target. A repaired manuscript differs and therefore receives exactly
        # one second real xelatex/bibtex compile before fail-closed page checks.
        payload: |
          {
            "operation": "compile_pdf",
            "meta_run_id": {{ inputs.meta_run_id | tojson }},
            "manuscript_package": {{ outputs.final_latex_sanitizer | tojson }},
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "enforce_page_target": true,
            "reuse_existing": true
          }
    - id: publish_pdf
      label: "发布 PDF"
      label_en: "Publish PDF"
      kind: tool_call
      tool: publish_artifact
      tool_allowlist: [publish_artifact]
      depends_on: [compile_pdf]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      tool_args:
        path: "paper/{{ inputs.meta_run_id }}/paper.pdf"
        name: "paper.pdf"
        mime: "application/pdf"
    - id: deliver_paper
      label: "论文交付"
      label_en: "Paper delivery"
      kind: skill_exec
      skill: paper-delivery-summary
      depends_on: [final_manuscript_package, compile_pdf, publish_pdf, citation_map]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract"
      with:
        payload: |
          {
            "paper_contract": {{ outputs.paper_contract | tojson }},
            "language_instruction": {{ inputs.get('language_instruction', '') | tojson }},
            "compile_pdf": {{ outputs.compile_pdf | tojson }},
            "citation_map": {{ outputs.citation_map | tojson }}
          }
---

# meta-paper-write (Meta-Skill)

Draft a long LaTeX manuscript by orchestrating paper-specific skills and
bounded LLM synthesis. The pipeline now leads with explicit experiment
design + placeholder figures/tables + citation provenance audit so the
deliverable can be reviewed for academic rigor, not just length.

DAG (in order):

1. **`paper_collect`** — extracts topic, mode, language, target length,
   audience, and reference count from the same turn without pausing for a
   form. Missing facts are marked as assumptions so first-pass paper
   requests complete inline.
2. **`paper_preferences`** — expand the collected facts into a planning
   contract.
3. **`search_papers`** — sends a clean academic query to Crossref, Brave,
   and Tavily; backend-specific academic filtering stays with each engine.
4. **`refbib`** — `paper-refbib-stub` now extracts ``eprint``/``doi``
   from arXiv/DOI URLs and tags each entry with ``note = {source: <domain>}``
   so downstream gates can classify provenance without re-fetching.
5. **`source_pack`** — curates unique, relevant, verifiable references and
   emits a machine-readable usable count against the integer citation target.
6. **`source_readiness_gate`** — deterministically blocks before experiment
   design or drafting when the curated primary references cannot meet the
   target, reporting a concrete found/required count.
7. **`experiment_design`** — **decides** how many figures and tables the
   paper needs based on RQs, hypotheses, analysis dimensions, and the
   target page budget. Every figure/table is tied to an RQ or analysis
   dimension; no decorative artefacts.
8. **`figure_placeholders`** — render LaTeX ``\fbox{\parbox{...}}``
   placeholder figure environments for each entry in FIGURE_PLAN. Zero
   matplotlib dependency.
9. **`table_placeholders`** — render LaTeX ``\begin{tabular}`` placeholder
   tables for each entry in TABLE_PLAN. Cells contain ``---``/``<TBD>``;
   no fabricated numbers.
10. **`analysis_outline`** — bind every figure/table id to a Discussion
   subsection that names potential findings + threats to validity, and
   covers every ANALYSIS_DIMENSION.
11. **`outline`** — paper outline that ties Method to experiment design
    and Results to the figure/table plan.
12. **`citation_plan`** — assigns concrete cite keys from `refbib` to
    claims; cannot invent keys.
13. **`writing_plan` + section authors** — the explicit FULL_MANUSCRIPT path
    converts the user's page target into section-level `target_words` and
    citation budgets before prose is written; section authors obey that plan.
14. **`final_manuscript_package`** — the lower-latency compact path still
    writes a complete, target-sized MANUSCRIPT_TEX with the
    figure/table/analysis blocks inlined verbatim, plus REFERENCES_BIB
    containing only the entries actually cited.
15. **Sanitize, materialize, and preflight** — persist the manuscript under
    the runtime-owned run directory, then compare language-aware content units
    with TARGET_PAGES. An undersized draft receives one bounded substantive
    expansion; a strict second gate must pass before compilation.
16. **`citation_map`** — strict markdown audit table:
    ``Cite Key | Cited Times | Title | URL/DOI/arXiv | Source Quality``
    with INVALID / UNUSED / WEAK detection. Inlined into the final
    deliverable AND queryable per-run via
    ``opensquilla skills meta runs show``.
17. **Citation and publication gates** — deterministically read the numeric
    `citation_map SUMMARY`; blocks when cited keys are below `CITATION_TARGET`,
    INVALID > 0, a cited source is WEAK, or the sanitized artifact violates
    publication rules. They never trust an LLM verdict or a fixed citation
    count.
18. **Compile probe and bounded page repair** — run the real
    XeLaTeX/BibTeX cycle and count pages with `pypdf`. A measured shortfall gets
    one final substantive expansion and one recompile. The runtime permits only
    the fixed `precompile` and `page-shortfall` repair ids and rejects citation,
    document-boundary, external-input, forced-page, and spacing commands.
19. **Final gates / `compile_pdf` / publish / delivery** — re-run sanitizer,
    length, and publication checks. An unchanged successful probe is reused by
    input fingerprint; a changed manuscript is recompiled once and must meet
    TARGET_PAGES before `publish_artifact` can run.

Removed from the previous version:

- `paper_mode` (llm_classify) — superseded by `paper_collect`
- `experiment` (skill_exec → `paper-experiment-stub`, fake CSV) —
  superseded by `experiment_design` (real plan, not data). The
  bundled `paper-experiment-stub` skill was deleted with this rewrite.
- `plot` (skill_exec → `paper-plot-stub`, matplotlib line chart) —
  superseded by `figure_placeholders` (zero-dependency LaTeX). The
  bundled `paper-plot-stub` skill was deleted with this rewrite.

The default path is COMPACT_SKELETON and ends with a compiled PDF without
section-by-section drafting. Explicit full/PDF/long-form requests use
FULL_MANUSCRIPT. If the topic is missing, `paper_clarify` pauses and asks the
user before generation continues. The compiler refuses to synthesize a degraded
PDF when the manuscript contract is missing.
