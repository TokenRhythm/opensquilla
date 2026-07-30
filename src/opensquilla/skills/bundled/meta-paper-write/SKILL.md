---
name: meta-paper-write
description: "Use this meta-skill instead of answering directly when the current user asks to draft, repair, compile, or produce an academic/research paper or LaTeX manuscript. It uses multi-skill orchestration for manuscript workflows that need source search, citation planning, experiment or figure/table placeholders, drafting, length checks, citation integrity, and LaTeX/PDF compilation. Ordinary paper requests use a compact draft path; explicit full/PDF/long-form requests use the full manuscript path. Do not use it for web research reports, slide decks, document decisions, or generic plotting."
description_zh: "当用户要求起草、修复、编译或产出学术/研究论文或LaTeX手稿时，使用此元技能而非直接回答。它通过多技能编排处理需要来源搜索、引用规划、实验或图表占位、起草、篇幅检查、引用完整性以及LaTeX/PDF编译的手稿工作流。普通论文请求走精简起草路径，明确要求完整/PDF/长篇的走完整手稿路径。不用于网页研究报告、幻灯片、文档决策或通用绘图。"
kind: meta
meta_priority: 50
always: false
final_text_mode: "step:deliver_paper"
request_template:
  outcome: "Academic manuscript draft or repair pass with citation and compilation checks as requested."
  outcome_zh: "按需生成或修订学术稿件，并检查引用与编译状态。"
  outcome_en: "Academic manuscript draft or repair pass with citation and compilation checks as requested."
  fields:
    - name: paper_topic_or_manuscript
      label_zh: "论文主题或稿件"
      label_en: "Paper topic or manuscript"
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
    - "Manuscript or repair output"
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
      - "Manuscript or repair output"
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
          - Use REPAIR_EXISTING only when the user provides or references an
            existing manuscript to fix.
          - Use COMPILE_ONLY only when the user explicitly asks only to compile
            an existing LaTeX manuscript.

          Clarification policy:
          - Required field: topic.
          - Infer language from the user request whenever possible. For an
            English request, set LANGUAGE: en. For a Chinese request, set
            LANGUAGE: zh.
          - If target pages are missing, use TARGET_PAGES: 4 for
            COMPACT_SKELETON and 10 for FULL_MANUSCRIPT.
          - If audience is missing, use AUDIENCE: academic.
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
          PAPER_MODE: <FULL_MANUSCRIPT|COMPACT_SKELETON|REPAIR_EXISTING|COMPILE_ONLY>
          LANGUAGE: <en|zh|ja|other>
          TARGET_PAGES: <integer 1-50, or MISSING_TARGET_PAGES>
          AUDIENCE: <academic|technical|business|general>
          CITATION_TARGET: <integer if user explicitly requested one, otherwise AUTO>
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
              - REPAIR_EXISTING
              - COMPILE_ONLY
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

          First-pass extraction:
          {{ outputs.paper_collect | truncate(1200) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('paper_clarify', {}) | tojson }}

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          TOPIC: <resolved topic>
          PAPER_MODE: <FULL_MANUSCRIPT|COMPACT_SKELETON|REPAIR_EXISTING|COMPILE_ONLY>
          LANGUAGE: <en|zh|ja|other>
          TARGET_PAGES: <integer 1-50>
          AUDIENCE: <academic|technical|business|general>
          CITATION_TARGET: <integer if explicitly requested, otherwise AUTO>
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
          CITATION_TARGET: <copy explicit citation target, otherwise derive from target length, source availability, audience, and venue style>
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
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
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
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
      with:
        # search_query_translation returns ONLY the English query text
        # (no labels / no preamble), so we can inline it directly.
        # Academic-site bias filters out blog/wiki/social.
        query: "{{ outputs.search_query_translation | xml_escape | truncate(200) }} (site:arxiv.org OR site:aclanthology.org OR site:dl.acm.org OR site:openreview.net OR site:ieee.org OR site:nature.com OR site:science.org)"
        engines: [brave, duckduckgo, tavily]
        # Brave Web Search API hard-caps ``count`` at 20 (multi-search-engine
        # clamps internally as defense in depth). Set explicitly so future
        # readers don't need to re-discover the limit.
        max_results: 20
    - id: refbib
      label: "参考文献"
      label_en: "References"
      kind: skill_exec
      skill: paper-refbib-stub
      depends_on: [search_papers]
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
      with:
        search_results: "{{ outputs.search_papers | truncate(8000) }}"
    - id: source_pack
      label: "来源包"
      label_en: "Source pack"
      kind: llm_chat
      depends_on: [search_papers, refbib]
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
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
          SOURCE_PACK:
          PRIMARY_REFERENCES:
            - refN | title | supported claim
          COVERAGE_GAPS:
            - <gap or none>
    - id: experiment_design
      label: "实验设计"
      label_en: "Experiment design"
      kind: llm_chat
      depends_on: [paper_preferences, source_pack]
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
              caption_hint: <short, factual>
            - id: fig2
              ... (repeat per provisioning rules)

          TABLE_PLAN:
            - id: tab1
              columns: <list of column headers>
              rows_shape: <e.g. "one row per baseline + ours + 2 ablations">
              supports: <RQ#|H#>
              caption_hint: <short, factual>
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

          Caption must come from caption_hint verbatim (escape LaTeX
          specials). Label MUST be ``\label{fig:<id>}`` so analysis_outline
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
          clean spacing.

          Header row comes from TABLE_PLAN columns; row labels come from
          rows_shape (expand the shape into concrete row names like
          "Baseline-A", "Baseline-B", "Ours", "Ours w/o module X", …).
          Caption is caption_hint verbatim. Label MUST be
          ``\label{tab:<id>}``.

          Experiment design:
          {{ outputs.experiment_design | truncate(8000) }}

          Reply with ONLY the concatenated LaTeX table environments,
          one per TABLE_PLAN entry, between sentinel comments:

          % BEGIN_TABLE_PLACEHOLDERS
          \begin{table}[t]
            \centering
            \begin{tabular}{lccc}
              \toprule
              Method & Acc & F1 & Latency \\
              \midrule
              Baseline-A & --- & --- & --- \\
              Baseline-B & --- & --- & --- \\
              Ours       & --- & --- & --- \\
              \bottomrule
            \end{tabular}
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
            with potential_findings (3 bullets) + threats_to_validity
            (1–2 bullets).
          - COMPACT_SKELETON: 1 subsection per dimension; potential_findings
            (1 bullet); skip threats_to_validity.

          Reply in this exact shape between sentinels:

          % BEGIN_ANALYSIS_OUTLINE
          \subsection{Performance}
          \label{sec:analysis-performance}
          References: \ref{fig:fig1}, \ref{tab:tab1}.
          Potential findings:
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
      depends_on: [source_pack, experiment_design]
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
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
      depends_on: [outline, source_pack, refbib]
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
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
              must_mention: [TITLE, PLACEHOLDER_RESULT_TOKEN]
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
          - Inline the analysis_outline subsections verbatim where they
            fit, but expand each with 1-2 paragraphs of substantive
            commentary referencing concrete experiment results.
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

          abstract (the conclusion must echo its claims):
          {{ outputs.section_abstract | truncate(1500) }}

          previous_section_tail (discussion ending):
          {{ outputs.section_discussion | truncate(2000) }}

          Output rules:
          - Start with \section{Conclusion}.
          - Cover: 1) restated thesis + headline result, 2) key contributions
            reiterated, 3) scope and limitations, 4) future-work pointer. Use
            as many concise paragraphs as the writing_plan target_words
            requires; do not cap the conclusion at 2-3 paragraphs when the
            requested page target is long.
          - No new claims, no new figures, no \cite{}.
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
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [section_abstract, section_introduction, section_related_work, section_method, section_experiments, section_discussion, section_conclusion]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      tool_args:
        # Persist large section bodies to disk and return only a compact
        # manifest. This keeps later LLM steps from repeatedly ingesting the
        # full manuscript and reduces repeated context-compaction pressure.
        command: |
          python3 - <<'PY'
          import os, re
          from pathlib import Path

          def clean(text):
              text = re.sub(r'^```(?:latex|tex)?\s*\n', '', text or '', flags=re.MULTILINE)
              text = re.sub(r'\n```\s*$', '', text)
              return text.strip()

          sections = {
              'abstract':     os.environ.get('SEC_ABSTRACT', ''),
              'introduction': os.environ.get('SEC_INTRO', ''),
              'related_work': os.environ.get('SEC_RELATED', ''),
              'method':       os.environ.get('SEC_METHOD', ''),
              'experiments':  os.environ.get('SEC_EXPERIMENTS', ''),
              'discussion':   os.environ.get('SEC_DISCUSSION', ''),
              'conclusion':   os.environ.get('SEC_CONCLUSION', ''),
          }
          out_dir = Path('paper') / 'sections'
          out_dir.mkdir(parents=True, exist_ok=True)

          print('SECTION_ARTIFACTS:')
          total = 0
          for name, text in sections.items():
              body = clean(text)
              path = out_dir / f'{name}.tex'
              path.write_text(body, encoding='utf-8')
              chars = len(body)
              total += chars
              first_line = next((line.strip() for line in body.splitlines() if line.strip()), '')
              print(f'- {name}: path={path.as_posix()} chars={chars} first_line={first_line[:120]!r}')
          print(f'TOTAL_SECTION_CHARS: {total}')
          print('CONTEXT_POLICY: downstream steps must read section files from disk and pass only paths/summaries to LLM prompts')
          PY
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 30
        env:
          SEC_ABSTRACT:    "{{ outputs.section_abstract }}"
          SEC_INTRO:       "{{ outputs.section_introduction }}"
          SEC_RELATED:     "{{ outputs.section_related_work }}"
          SEC_METHOD:      "{{ outputs.section_method }}"
          SEC_EXPERIMENTS: "{{ outputs.section_experiments }}"
          SEC_DISCUSSION:  "{{ outputs.section_discussion }}"
          SEC_CONCLUSION:  "{{ outputs.section_conclusion }}"
    - id: assemble_manuscript_tex
      label: "组装 TEX"
      label_en: "Assemble TEX"
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [writing_plan, persist_sections, refbib]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract"
      tool_args:
        # Concatenate section artifact files into a full LaTeX document and
        # write it to paper/paper.tex. Return a compact manifest instead of
        # echoing the full manuscript back into the meta context.
        command: |
          python3 - <<'PY'
          import os, re, sys
          from pathlib import Path

          section_dir = Path('paper') / 'sections'
          sections = {
              'abstract':     section_dir / 'abstract.tex',
              'introduction': section_dir / 'introduction.tex',
              'related_work': section_dir / 'related_work.tex',
              'method':       section_dir / 'method.tex',
              'experiments':  section_dir / 'experiments.tex',
              'discussion':   section_dir / 'discussion.tex',
              'conclusion':   section_dir / 'conclusion.tex',
          }
          section_text = {
              name: path.read_text(encoding='utf-8') if path.is_file() else ''
              for name, path in sections.items()
          }
          bib = os.environ.get('BIB_TEXT', '').strip()
          # Extract TITLE from the writing_plan envelope. Falls back to the
          # raw topic when the LLM omits a TITLE line so the PDF gets a
          # meaningful title regardless.
          writing_plan = os.environ.get('WRITING_PLAN', '')
          topic_fallback = os.environ.get('TOPIC', 'Untitled Manuscript')
          tm = re.search(r'^\s*TITLE\s*:\s*(.+?)\s*$', writing_plan, re.MULTILINE)
          raw_title = (tm.group(1).strip() if tm else topic_fallback) or topic_fallback
          # LaTeX-escape the title so user-provided text can't break the preamble.
          def latex_escape(s):
              s = s.replace('\\', r'\textbackslash{}')
              for ch in '&%$#_{}':
                  s = s.replace(ch, '\\' + ch)
              s = s.replace('~', r'\textasciitilde{}')
              s = s.replace('^', r'\textasciicircum{}')
              return s

          def scrub_placeholder_table_cells(tex):
              """Scrub numeric-looking data cells from placeholder tables."""
              numeric = re.compile(
                  r'^\s*(?:\\textbf\{)?[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|ms|s|x|MB|GB|points?)?(?:\})?\s*$',
                  re.I,
              )
              out = []
              in_tabular = False
              after_midrule = False
              for line in tex.splitlines():
                  if r'\begin{tabular}' in line:
                      in_tabular = True
                      after_midrule = False
                      out.append(line)
                      continue
                  if in_tabular and r'\end{tabular}' in line:
                      in_tabular = False
                      after_midrule = False
                      out.append(line)
                      continue
                  if in_tabular and r'\midrule' in line:
                      after_midrule = True
                      out.append(line)
                      continue
                  if in_tabular and after_midrule and '&' in line and r'\bottomrule' not in line:
                      suffix = r' \\' if line.rstrip().endswith(r'\\') else ''
                      row = line.rstrip()
                      if suffix:
                          row = row[:-2].rstrip()
                      cells = [cell.strip() for cell in row.split('&')]
                      if len(cells) > 1:
                          cells = [cells[0], *('---' if numeric.match(cell) else cell for cell in cells[1:])]
                          indent = re.match(r'^\s*', line).group(0)
                          line = indent + ' & '.join(cells) + suffix
                  out.append(line)
              return '\n'.join(out)
          title_tex = latex_escape(raw_title)
          # Build preamble — load xeCJK if title or any section has CJK
          any_cjk = (re.search(r'[一-鿿]', raw_title) is not None) or any(
              re.search(r'[一-鿿]', v) for v in section_text.values()
          )
          preamble = [
              r"\documentclass{article}",
              r"\usepackage{xeCJK}" if any_cjk else r"% no CJK",
              r"\usepackage{graphicx}",
              r"\usepackage{booktabs}",
              r"\usepackage{amsmath,amssymb}",
              r"\usepackage{hyperref}",
              r"\usepackage{geometry}",
              r"\geometry{margin=2.5cm}",
              r"\title{" + title_tex + r"}",
              r"\author{OpenSquilla meta-paper-write}",
              r"\date{\today}",
              r"\begin{document}",
              r"\maketitle",
          ]
          body_parts = [
              section_text['abstract'],     # \begin{abstract}...\end{abstract}
              section_text['introduction'], # \section{Introduction}...
              section_text['related_work'],
              section_text['method'],
              section_text['experiments'],
              section_text['discussion'],
              section_text['conclusion'],
          ]
          tail = [
              r"\bibliographystyle{plain}",
              r"\bibliography{references}",
              r"\end{document}",
          ]
          tex = '\n'.join(preamble) + '\n\n' + '\n\n'.join(p for p in body_parts if p) + '\n\n' + '\n'.join(tail)
          tex = scrub_placeholder_table_cells(tex)
          paper_dir = Path('paper')
          paper_dir.mkdir(exist_ok=True)
          tex_path = paper_dir / 'paper.tex'
          bib_path = paper_dir / 'references.bib'
          tex_path.write_text(tex, encoding='utf-8')
          bib_path.write_text(bib if bib else '% no verified references', encoding='utf-8')
          print(f'MANUSCRIPT_PATH: {tex_path.resolve()}')
          print(f'REFERENCES_PATH: {bib_path.resolve()}')
          print(f'MANUSCRIPT_CHARS: {len(tex)}')
          print(f'REFERENCES_CHARS: {len(bib)}')
          print('COMPILE_NOTES:')
          print('- assembled section-by-section via paper-section-author')
          print(f'- sections present: {", ".join(k for k, v in section_text.items() if v)}')
          print(f'- total section chars: {sum(len(v) for v in section_text.values())}')
          print('- context policy: full manuscript persisted on disk; downstream prompts should use path/summary only')
          PY
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 30
        env:
          BIB_TEXT:        "{{ outputs.refbib }}"
          WRITING_PLAN:    "{{ outputs.writing_plan }}"
          TOPIC:           "{{ outputs.paper_contract | truncate(400) }}"
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
      when: "'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract"
      with:
        system: "You write clean LaTeX manuscripts. Output only the requested manuscript package. NEVER invent cite keys — every \\cite{...} you emit MUST exist verbatim in REFERENCES_BIB below."
        task: |
          Draft a full manuscript package. The default output must be clean
          LaTeX-ready paper text, not planning notes. Do not include markdown
          fences, chat commentary, progress notes, or tool logs.

          Paper mode:
          TOPIC: {{ outputs.paper_contract | truncate(1200) }}, MODE: {{ outputs.paper_contract | truncate(400) }}, PAGES: {{ outputs.paper_contract | truncate(400) }}

          Mode behavior:
          - FULL_MANUSCRIPT: produce enough substance for
            TARGET_LENGTH from paper_preferences as compiled pages, using
            the user-requested or derived CITATION_TARGET instead of a fixed
            reference count. Distribute verified citation keys across
            abstract, introduction, related work, method, results, discussion,
            limitations, and conclusion.
          - COMPACT_SKELETON: produce a compact LaTeX-ready manuscript
            skeleton with section goals, planned citations, and expansion
            notes; do not pretend it is a finished paper of the requested
            length. For this
            mode, the final package MUST include an explicit manuscript plan,
            a target-length expansion plan, limitations/threats-to-validity,
            and reference placeholders sized to the requested/derived citation
            strategy when verified BibTeX entries are unavailable. Keep the compact package short enough that all
            required sections are visible before any evaluator truncation:
            put the plan and expansion plan before the LaTeX skeleton, and
            keep MANUSCRIPT_TEX under 2,500 words.
          - REPAIR_EXISTING: return a repaired clean LaTeX package focused on
            citation integrity, structure, and removal of process text.
          - COMPILE_ONLY: return a compile handoff package and blockers only;
            do not invent missing manuscript body.

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
          \usepackage{xeCJK}
          \usepackage{graphicx}
          \usepackage{booktabs}
          \usepackage{amsmath}
          \usepackage{hyperref}
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
    - id: citation_map
      label: "引用映射"
      label_en: "Citation mapping"
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [final_manuscript_package, consistency_pass, assemble_manuscript_tex, refbib]
      when: "'PAPER_MODE: COMPILE_ONLY' not in outputs.paper_contract"
      tool_args:
        # Deterministically parse citations from artifact files instead of
        # sending the full manuscript back through an LLM.
        command: |
          python3 - <<'PY'
          import os, re
          from pathlib import Path

          pkg = os.environ.get('MANIFEST', '')
          m = re.search(r'MANUSCRIPT_PATH:\s*(.+)', pkg)
          b = re.search(r'REFERENCES_PATH:\s*(.+)', pkg)
          tex_path = Path(m.group(1).strip()) if m else Path('paper/paper.tex')
          bib_path = Path(b.group(1).strip()) if b else Path('paper/references.bib')

          tex = tex_path.read_text(encoding='utf-8', errors='ignore') if tex_path.is_file() else ''
          bib = bib_path.read_text(encoding='utf-8', errors='ignore') if bib_path.is_file() else os.environ.get('REFBIB', '')

          cite_counts = {}
          for group in re.findall(r'\\cite\{([^}]+)\}', tex):
              for key in [k.strip() for k in group.split(',') if k.strip()]:
                  cite_counts[key] = cite_counts.get(key, 0) + 1

          entries = {}
          for match in re.finditer(r'@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@\w+\s*\{|\Z)', bib, re.DOTALL):
              key = match.group(1).strip()
              body = match.group(2)
              title = re.search(r'title\s*=\s*[\{\"]([^}\"]+)', body, re.I)
              url = re.search(r'(?:url|howpublished)\s*=\s*[\{\"]([^}\"]+)', body, re.I)
              doi = re.search(r'doi\s*=\s*[\{\"]([^}\"]+)', body, re.I)
              eprint = re.search(r'eprint\s*=\s*[\{\"]([^}\"]+)', body, re.I)
              locator = (url.group(1) if url else '') or (f'doi:{doi.group(1)}' if doi else '') or (f'arXiv:{eprint.group(1)}' if eprint else '')
              entries[key] = {
                  'title': title.group(1).strip() if title else '',
                  'locator': locator,
              }

          strong_domains = ('arxiv.org', 'aclanthology.org', 'dl.acm.org', 'openreview.net', 'ieee.org', 'nature.com', 'science.org', 'biorxiv.org', 'pnas.org')
          weak_markers = ('medium.com', 'wikipedia.org', 'github.com', 'stackoverflow.com', 'twitter.com', 'x.com')
          def quality(locator, invalid=False):
              low = locator.lower()
              if invalid:
                  return 'INVALID'
              if any(d in low for d in strong_domains) or 'doi:' in low or 'arxiv:' in low:
                  return 'STRONG'
              if any(w in low for w in weak_markers):
                  return 'WEAK'
              if locator:
                  return 'OK'
              return 'WEAK'

          rows = []
          invalid = weak = strong = ok = unused = 0
          all_keys = sorted(set(cite_counts) | set(entries))
          print('CITATION_MAP:')
          print()
          print('| Cite Key | Cited Times | Title | URL / DOI / arXiv | Source Quality |')
          print('|---|---:|---|---|---|')
          for key in all_keys:
              count = cite_counts.get(key, 0)
              entry = entries.get(key)
              invalid_row = entry is None
              q = quality(entry['locator'] if entry else '', invalid=invalid_row)
              if invalid_row:
                  invalid += 1
              elif count == 0:
                  unused += 1
                  q = 'UNUSED'
              elif q == 'STRONG':
                  strong += 1
              elif q == 'OK':
                  ok += 1
              elif q == 'WEAK':
                  weak += 1
              title = entry['title'] if entry else '(MISSING IN BIB)'
              locator = entry['locator'] if entry else '-'
              print(f'| {key} | {count} | {title} | {locator} | {q} |')
          print()
          print(f'SUMMARY: total_cite_keys={len(cite_counts)}, strong={strong}, ok={ok}, weak={weak}, invalid={invalid}, unused={unused}')
          print(f'ARTIFACTS: manuscript={tex_path} references={bib_path}')
          PY
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 30
        env:
          MANIFEST: "{{ outputs.get('consistency_pass') or outputs.get('assemble_manuscript_tex') or outputs.get('final_manuscript_package', '') }}"
          REFBIB: "{{ outputs.refbib }}"
    - id: paper_length_gate
      label: "篇幅门禁"
      label_en: "Length gate"
      kind: llm_chat
      depends_on: [final_manuscript_package, consistency_pass, assemble_manuscript_tex]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract"
      with:
        system: "You verify manuscript length requirements before final packaging."
        task: |
          Check whether the manuscript package satisfies the requested paper
          length, section coverage, and compact/skeleton mode constraints.

          Paper preferences:
          {{ outputs.paper_preferences | truncate(4000) }}

          Manuscript package:
          {{ outputs.get('consistency_pass') or outputs.get('assemble_manuscript_tex') or outputs.get('final_manuscript_package', '') | truncate(8000) }}

          Reply with:
          LENGTH_GATE: <pass|warn|block>
          ESTIMATED_WORDS: <int or unknown>
          BLOCKERS:
            - <blocker or none>
          WARNINGS:
            - <warning or none>
    - id: citation_integrity_gate
      label: "引用门禁"
      label_en: "Citation gate"
      kind: llm_chat
      depends_on: [final_manuscript_package, consistency_pass, assemble_manuscript_tex, citation_plan, refbib, citation_map, paper_length_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract"
      with:
        system: "You verify LaTeX/BibTeX citation integrity."
        task: |
          Validate citation integrity before LaTeX compilation.

          Requirements (LOAD-BEARING — block compilation if any fails):
          - REFERENCES_BIB and body citations satisfy the user-requested or
            derived CITATION_TARGET from paper_preferences when sources allow it
          - distinct citation keys used/planned in the body match
            paper_preferences.CITATION_STRATEGY; do not enforce a fixed count
          - NO citation keys absent from references.bib (citation_map column
            "INVALID" must be 0)
          - every cited entry MUST have a verifiable URL or DOI or arXiv
            eprint field in REFERENCES_BIB; entries with only howpublished
            text are degraded but acceptable; entries with no URL/DOI/eprint
            at all are blockers
          - no Source Quality == WEAK in citation_map for primary claims
            (introduction headline / method core / results headline);
            warn but do not block for related-work / motivation context
          - every major claim has nearby citation support or an explicit caveat

          Citation plan:
          {{ outputs.citation_plan | truncate(8000) }}

          Bibliography:
          {{ outputs.refbib | truncate(8000) }}

          Citation audit table (read this — do NOT re-derive):
          {{ outputs.citation_map | truncate(4000) }}

          Reply with:
          INTEGRITY: <pass|warn|block>
          INVALID_COUNT: <int>
          WEAK_PRIMARY_COUNT: <int>
          UNUSED_COUNT: <int>
          BLOCKERS:
            - <blocker or none>
          WARNINGS:
            - <warning or none>
    - id: latex_sanitizer
      label: "LaTeX 清理"
      label_en: "LaTeX cleanup"
      kind: llm_chat
      depends_on: [citation_integrity_gate]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract or 'PAPER_MODE: COMPILE_ONLY' in outputs.paper_contract"
      with:
        system: "You sanitize LaTeX deliverables and reject process text."
        task: |
          Sanitize the final LaTeX package contract before compilation. Confirm
          that process commentary, markdown fences, chat preambles, debug logs,
          and non-paper text are absent from MANUSCRIPT_TEX and REFERENCES_BIB.
          Preserve valid LaTeX, CJK text, citations, figure references,
          placeholder figure/table blocks (\fbox + tabular), and section content.
          Reply with a concise readiness note and any blocking issue only.

          Citation gate:
          {{ outputs.citation_integrity_gate | truncate(2000) }}
    - id: compile_latex
      label: "编译 LaTeX"
      label_en: "Compile LaTeX"
      kind: llm_chat
      depends_on: [latex_sanitizer]
      when: "'PAPER_MODE: COMPILE_ONLY' in outputs.paper_contract"
      with:
        system: "You prepare compile-only handoff notes without invoking LaTeX in this step."
        task: |
          Produce a concise compile handoff note. COMPILE_ONLY is for
          assessing an existing LaTeX manuscript. The full manuscript and
          compact skeleton paths compile a PDF via compile_pdf after quality
          gates pass.

          Sanitizer result:
          {{ outputs.latex_sanitizer | truncate(2000) }}

          Reply exactly:
          COMPILE_READY: <yes|blocked>
          NEXT_STEP: provide or select an existing manuscript package to compile
          BLOCKERS:
            - <blocker or none>
    - id: compile_pdf
      label: "编译 PDF"
      label_en: "Compile PDF"
      kind: tool_call
      tool: exec_command
      tool_allowlist: [exec_command]
      depends_on: [latex_sanitizer, consistency_pass, assemble_manuscript_tex, final_manuscript_package]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract"
      tool_args:
        # Runs the actual xelatex × bibtex × xelatex × 2 cycle so the
        # user gets a real PDF, not just LaTeX source. Extracts
        # MANUSCRIPT_TEX / REFERENCES_BIB from the consistency/assembly/final
        # package contract (passed via env var to dodge shell-escape hell).
        command: |
          python3 - <<'PY'
          import os, re, subprocess, sys
          from pathlib import Path

          pkg = os.environ.get('MANUSCRIPT_PKG', '')

          # 1. Try MANUSCRIPT_TEX: / REFERENCES_BIB: contract markers first.
          m = re.search(r'MANUSCRIPT_TEX:\s*(.+?)(?:REFERENCES_BIB:|COMPILE_NOTES:|\Z)', pkg, re.DOTALL)
          tex_body = m.group(1).strip() if m else ''
          mb = re.search(r'REFERENCES_BIB:\s*(.+?)(?:COMPILE_NOTES:|\Z)', pkg, re.DOTALL)
          bib = mb.group(1).strip() if mb else ''

          # 2. Fallback A: maybe LLM wrapped LaTeX in ```latex fences without the marker.
          if not tex_body:
              fenced = re.search(r'```(?:latex|tex)?\s*(\\documentclass[\s\S]+?\\end\{document\})', pkg)
              if fenced:
                  tex_body = fenced.group(1).strip()

          # 3. Fallback B: maybe there's a raw \documentclass…\end{document} block.
          if not tex_body:
              raw = re.search(r'(\\documentclass[\s\S]+?\\end\{document\})', pkg)
              if raw:
                  tex_body = raw.group(1).strip()

          # 4. Fallback C: artifact-only FULL_MANUSCRIPT path. Read the
          # persisted manuscript and bibliography from disk instead of
          # requiring the full document to be present in the meta context.
          manifest_tex_path = None
          manifest_bib_path = None
          if not tex_body:
              pm = re.search(r'MANUSCRIPT_PATH:\s*(.+)', pkg)
              bm = re.search(r'REFERENCES_PATH:\s*(.+)', pkg)
              if pm:
                  manifest_tex_path = Path(pm.group(1).strip())
                  if manifest_tex_path.is_file():
                      tex_body = manifest_tex_path.read_text(encoding='utf-8')
              if bm:
                  manifest_bib_path = Path(bm.group(1).strip())
                  if manifest_bib_path.is_file():
                      bib = manifest_bib_path.read_text(encoding='utf-8')

          # 5. Strip any leftover markdown fences from extracted bodies.
          tex_body = re.sub(r'^```(?:latex|tex)?\s*\n', '', tex_body)
          tex_body = re.sub(r'\n```\s*$', '', tex_body)

          def scrub_placeholder_table_cells(tex):
              """Scrub numeric-looking data cells from placeholder tables."""
              numeric = re.compile(
                  r'^\s*(?:\\textbf\{)?[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|ms|s|x|MB|GB|points?)?(?:\})?\s*$',
                  re.I,
              )
              out = []
              in_tabular = False
              after_midrule = False
              for line in tex.splitlines():
                  if r'\begin{tabular}' in line:
                      in_tabular = True
                      after_midrule = False
                      out.append(line)
                      continue
                  if in_tabular and r'\end{tabular}' in line:
                      in_tabular = False
                      after_midrule = False
                      out.append(line)
                      continue
                  if in_tabular and r'\midrule' in line:
                      after_midrule = True
                      out.append(line)
                      continue
                  if in_tabular and after_midrule and '&' in line and r'\bottomrule' not in line:
                      suffix = r' \\' if line.rstrip().endswith(r'\\') else ''
                      row = line.rstrip()
                      if suffix:
                          row = row[:-2].rstrip()
                      cells = [cell.strip() for cell in row.split('&')]
                      if len(cells) > 1:
                          cells = [cells[0], *('---' if numeric.match(cell) else cell for cell in cells[1:])]
                          indent = re.match(r'^\s*', line).group(0)
                          line = indent + ' & '.join(cells) + suffix
                  out.append(line)
              return '\n'.join(out)

          # 6. If still empty, fail loudly. Quality-first paper generation
          # must not disguise a missing manuscript as a degraded PDF.
          if not tex_body:
              print('COMPILE_FAILED: MANUSCRIPT_TEX block missing; refusing to create degraded PDF')
              print('PACKAGE_PREVIEW:')
              print(pkg[:2000])
              sys.exit(1)

          # 7. Auto-wrap if the LLM gave a body fragment but no \documentclass.
          if '\\documentclass' not in tex_body:
              tex_body = (
                  r"\documentclass{article}" "\n"
                  r"\usepackage{xeCJK}" "\n"
                  r"\usepackage{graphicx}\usepackage{booktabs}\usepackage{amsmath}\usepackage{hyperref}" "\n"
                  r"\begin{document}" "\n"
                  + tex_body + "\n"
                  r"\bibliographystyle{plain}" "\n"
                  r"\bibliography{references}" "\n"
                  r"\end{document}" "\n"
              )

          # 8. Auto-add xeCJK if the body contains CJK chars but doesn't load it.
          if re.search(r'[一-鿿]', tex_body) and 'xeCJK' not in tex_body:
              tex_body = tex_body.replace(
                  r'\documentclass{article}',
                  r'\documentclass{article}' + '\n' + r'\usepackage{xeCJK}',
                  1,
              )

          tex_body = scrub_placeholder_table_cells(tex_body)

          paper = Path('paper'); paper.mkdir(exist_ok=True)
          (paper / 'paper.tex').write_text(tex_body, encoding='utf-8')
          (paper / 'references.bib').write_text(bib, encoding='utf-8')

          logs = []
          for cmd in (
              ['xelatex','-interaction=nonstopmode','paper.tex'],
              ['bibtex','paper'],
              ['xelatex','-interaction=nonstopmode','paper.tex'],
              ['xelatex','-interaction=nonstopmode','paper.tex'],
          ):
              r = subprocess.run(cmd, cwd='paper', capture_output=True, text=True)
              logs.append(f"--- {' '.join(cmd)} (rc={r.returncode}) ---")

          pdf = (paper / 'paper.pdf').resolve()
          if pdf.is_file():
              log_text = (paper / 'paper.log').read_text(encoding='utf-8', errors='ignore') if (paper / 'paper.log').is_file() else ''
              pm = re.search(r'Output written on .+?\((\d+) pages?', log_text)
              pages = pm.group(1) if pm else '?'
              print(f'PDF_PATH: {pdf}')
              print(f'PDF_PAGES: {pages}')
              print(f'PDF_BYTES: {pdf.stat().st_size}')
              print(f'TEX_BYTES: {(paper / "paper.tex").stat().st_size}')
              print(f'BIB_BYTES: {(paper / "references.bib").stat().st_size}')
          else:
              tail = '\n'.join(logs[-3:])
              # Dump the last 80 lines of paper.log so the failure mode is visible.
              log_text = (paper / 'paper.log').read_text(encoding='utf-8', errors='ignore') if (paper / 'paper.log').is_file() else ''
              log_tail = '\n'.join(log_text.splitlines()[-80:])
              print(f'COMPILE_FAILED:\n{tail}\n\n=== paper.log tail ===\n{log_tail}')
              sys.exit(1)
          PY
        workdir: "{{ inputs.workspace_dir }}"
        timeout: 120
        env:
          MANUSCRIPT_PKG: "{{ outputs.get('consistency_pass') or outputs.get('assemble_manuscript_tex') or outputs.get('final_manuscript_package', '') }}"
    - id: publish_pdf
      label: "发布 PDF"
      label_en: "Publish PDF"
      kind: tool_call
      tool: publish_artifact
      tool_allowlist: [publish_artifact]
      depends_on: [compile_pdf]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract"
      tool_args:
        path: "paper/paper.pdf"
        name: "paper.pdf"
        mime: "application/pdf"
    - id: deliver_paper
      label: "论文交付"
      label_en: "Paper delivery"
      kind: llm_chat
      depends_on: [final_manuscript_package, compile_pdf, publish_pdf, citation_map]
      when: "'PAPER_MODE: FULL_MANUSCRIPT' in outputs.paper_contract or 'PAPER_MODE: COMPACT_SKELETON' in outputs.paper_contract or 'PAPER_MODE: REPAIR_EXISTING' in outputs.paper_contract"
      with:
        system: "You write a one-paragraph delivery note for a compiled academic paper. Output is concise — no LaTeX source, no markdown fences. Obey USER_LANGUAGE strictly: en means English only; zh means Chinese only."
        task: |
          Produce the user-facing delivery message. Confirm the PDF
          is ready, name its location, page count, citation summary,
          and list any open warnings from the citation audit. Keep
          the message under 200 words.

          USER_LANGUAGE: {{ inputs.get('user_language', 'zh' if (inputs.user_message | contains_cjk) else 'en') }}

          Language rules:
          - If USER_LANGUAGE is en, write English only. Do not include Chinese
            headings, labels, warnings, or bilingual labels.
          - If USER_LANGUAGE is zh, write Chinese only. Do not include English
            headings except literal file paths, artifact IDs, and citation keys.
          - Do not copy warning prose from intermediate audit text; translate
            any warning into the selected USER_LANGUAGE.

          Original request:
          {{ inputs.user_message | xml_escape | truncate(400) }}

          PDF compile result (paths are absolute):
          {{ outputs.compile_pdf | truncate(800) }}

          Artifact publication result:
          {{ outputs.publish_pdf | truncate(800) }}

          Citation audit summary tail:
          {{ outputs.citation_map | truncate(2000) }}

          {% if inputs.get('user_language') == 'zh' or (inputs.user_message | contains_cjk) %}
          Format:
          📄 论文已生成

          - PDF: <absolute path or artifact id>
          - 页数: <N>
          - 引用: <total / strong / weak / invalid / unused>
          - 备注: <one line about figures, tables, analysis dimensions>

          If the audit shows INVALID > 0, prefix the message with
          "⚠️ 注意: <N> 处引用未在 bib 中，建议重新生成" and list the offending
          cite keys.
          {% else %}
          Format:
          📄 Paper compiled

          - PDF: <absolute path or artifact id>
          - Pages: <N>
          - Citations: <total / strong / weak / invalid / unused>
          - Notes: <one line about figures, tables, analysis dimensions>

          If the audit shows INVALID > 0, prefix the message with
          "⚠️ Warning: <N> citation keys are missing from references.bib; regenerate
          or repair the bibliography" and list the offending cite keys.
          {% endif %}
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
3. **`search_papers`** — `multi-search-engine` query biased toward arXiv
   / ACL Anthology / ACM DL / OpenReview / IEEE / Nature / Science so
   the returned URLs translate into real bibliographic identifiers.
4. **`refbib`** — `paper-refbib-stub` now extracts ``eprint``/``doi``
   from arXiv/DOI URLs and tags each entry with ``note = {source: <domain>}``
   so downstream gates can classify provenance without re-fetching.
5. **`source_pack`** — curate references and enforce ≥20-source coverage.
6. **`experiment_design`** — **decides** how many figures and tables the
   paper needs based on RQs, hypotheses, analysis dimensions, and the
   target page budget. Every figure/table is tied to an RQ or analysis
   dimension; no decorative artefacts.
7. **`figure_placeholders`** — render LaTeX ``\fbox{\parbox{...}}``
   placeholder figure environments for each entry in FIGURE_PLAN. Zero
   matplotlib dependency.
8. **`table_placeholders`** — render LaTeX ``\begin{tabular}`` placeholder
   tables for each entry in TABLE_PLAN. Cells contain ``---``/``<TBD>``;
   no fabricated numbers.
9. **`analysis_outline`** — bind every figure/table id to a Discussion
   subsection that names potential findings + threats to validity, and
   covers every ANALYSIS_DIMENSION.
10. **`outline`** — paper outline that ties Method to experiment design
    and Results to the figure/table plan.
11. **`citation_plan`** — assigns concrete cite keys from `refbib` to
    claims; cannot invent keys.
12. **`writing_plan` + section authors** — the explicit FULL_MANUSCRIPT path
    converts the user's page target into section-level `target_words` and
    citation budgets before prose is written; section authors obey that plan.
13. **`final_manuscript_package`** — compact / repair modes produce
    MANUSCRIPT_TEX with the figure/table/analysis blocks inlined verbatim,
    plus REFERENCES_BIB containing only the entries actually cited.
14. **`citation_map`** — strict markdown audit table:
    ``Cite Key | Cited Times | Title | URL/DOI/arXiv | Source Quality``
    with INVALID / UNUSED / WEAK detection. Inlined into the final
    deliverable AND queryable per-run via
    ``opensquilla skills meta runs show``.
15. **`citation_integrity_gate`** — reads `citation_map` directly; blocks
    when INVALID > 0 or any primary claim cites a WEAK source.
16. **`latex_sanitizer`** — strips process text without rewriting the
    paper.
17. **`compile_pdf` / `publish_pdf` / `deliver_paper`** — compile and
    publish the final PDF for FULL_MANUSCRIPT, COMPACT_SKELETON, and
    REPAIR_EXISTING. The compiler refuses to create degraded PDFs when
    MANUSCRIPT_TEX is missing.
18. **`compile_latex`** — handoff note (COMPILE_ONLY mode).

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
