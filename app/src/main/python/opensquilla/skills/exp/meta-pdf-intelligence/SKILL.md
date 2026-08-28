---
name: meta-pdf-intelligence
description: "Use this meta-skill instead of answering directly when the user needs PDF analysis, pasted PDF excerpt analysis, digesting, comparison, or question answering that benefits from multi-skill orchestration across PDF extraction, summarization, cross-document synthesis, traceable evidence indexing, and memory capture."
kind: meta
meta_priority: 55
always: false
final_text_mode: "step:cross_document_synthesis"
triggers:
  - "看一下这个 PDF"
  - "看看这个 PDF"
  - "读一下这个 PDF"
  - "分析这个 PDF"
  - "总结这个 PDF"
  - "帮我看 PDF"
  - "处理 PDF"
  - "PDF 抽要"
  - "PDF intelligence"
  - "pdf digest"
  - "compare these PDFs"
  - "page-backed findings"
  - "PDF excerpt"
  - "pasted PDF excerpt"
  - "PDF page says"
  - "analyze these PDFs"
  - "PDF analysis"
  - "PDF comparison"
  - "PDF 摘录"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You parse PDF-analysis requests into strict extraction contracts."
        task: |
          Parse the PDF request into a document-analysis contract. Determine
          whether this is a single-PDF summary, multi-PDF comparison, or a
          targeted question-answer task. Preserve every file path or URL the
          user mentioned. Treat quoted page text, pasted excerpts, and phrases
          like "I don't have the PDF upload handy" as first-class source
          status signals.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          MODE: <single_summary|multi_compare|question_answer>
          SOURCE_STATUS: <readable_pdf|inline_excerpts_only|mixed_pdf_and_inline|reference_without_content>
          DOCUMENTS:
            - <path or URL>
          USER_EXCERPTS:
            - PAGE: <page number or unknown>
              QUOTE: <verbatim user-provided excerpt or empty>
          QUESTION: <specific question or empty>
          OUTPUT_LANGUAGE: <language>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <source_material|question|none>
          CLARIFY_REASON: <one concise reason, or none>
    - id: pdf_clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: |
          PDF 分析缺少可用来源或目标问题。请补齐材料，或确认只基于已提供摘录/文件名给出有限结论。
        nl_extract: true
        fields:
          - name: source_status
            type: enum
            required: true
            choices: [readable_pdf, inline_excerpts_only, reference_only]
            prompt: "来源状态 / Source status"
          - name: source_material
            type: string
            required: true
            prompt: "PDF 路径/URL、上传说明，或页面摘录 / PDF path, URL, upload note, or excerpts"
            max_chars: 2000
          - name: question
            type: string
            prompt: "具体问题 / Specific question"
            max_chars: 300
          - name: output_language
            type: enum
            choices: [zh, en, ja, other]
            default: zh
            prompt: "输出语言 / Output language"
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24
    - id: extract
      skill: pdf-toolkit
      depends_on: [intake, pdf_clarify]
      when: >-
        'SOURCE_STATUS: inline_excerpts_only' not in outputs.intake
        and (
          'SOURCE_STATUS: reference_without_content' not in outputs.intake
          or inputs.get('collected', {}).get('pdf_clarify', {}).get('source_status') == 'readable_pdf'
        )
        and "don't have the pdf" not in (inputs.user_message | lower)
        and "do not have the pdf" not in (inputs.user_message | lower)
        and "no pdf upload" not in (inputs.user_message | lower)
        and "pdf upload handy" not in (inputs.user_message | lower)
        and not ('page ' in (inputs.user_message | lower) and ' says ' in (inputs.user_message | lower))
      on_failure: inline_excerpt_extract
      with:
        task: |
          Extract text, tables, page numbers, headings, and document names for
          this PDF analysis contract:
          {{ outputs.intake | truncate(2000) }}

          Do not invent PDF content. If no readable local path, URL, or
          attachment is actually accessible, return UNAVAILABLE with the
          reason instead of a synthetic summary.
    - id: inline_excerpt_extract
      kind: llm_chat
      with:
        system: "You provide a safe fallback when a PDF file is unavailable to the extractor."
        task: |
          The PDF extraction skill could not read the file in this runtime.
          Build a minimal evidence packet only from filenames, URLs, quoted
          excerpts, pasted text, and explicit user claims in the request.
          Clearly label missing page evidence as unavailable.

          User request:
          {{ inputs.user_message | xml_escape | truncate(4000) }}
    - id: per_document_digest
      skill: summarize
      depends_on: [extract]
      with:
        text: |
          Intake:
          {{ outputs.intake }}

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(4000) }}

          Extracted PDF content:
          {{ outputs.extract }}

          If extraction was skipped, unavailable, or inconsistent with the
          USER_EXCERPTS in intake, summarize only the user-provided excerpts
          and explicitly mark all other document content as unavailable.
        style: pdf_per_document_digest
        max_words: 2500
    - id: cross_document_synthesis
      kind: llm_chat
      depends_on: [per_document_digest]
      with:
        system: "You synthesize PDF findings with traceable evidence, evidence IDs, and explicit limits."
        task: |
          Synthesize the PDF analysis according to the intake mode. For
          single_summary, produce a structured summary. For multi_compare,
          compare agreements, conflicts, and unique claims. For question_answer,
          answer the question directly first.

          Requirements:
          - produce a compact final deliverable, not process commentary
          - source hierarchy: first trust verbatim user-provided excerpts and
            pasted text; then trust extractor output only when it is actually
            from a readable PDF and does not conflict with the user excerpts;
            then place synthesis under Inferences
          - if SOURCE_STATUS is inline_excerpts_only or reference_without_content,
            ignore any downstream claims that are not present in USER_EXCERPTS
            or the original user request
          - if the original user request says the PDF is not uploaded, no PDF
            is handy, or uses inline phrasing like "page 3 says ...", treat
            the entire answer as EXCERPT-ONLY even if intake or a downstream
            digest says otherwise
          - if extractor output conflicts with USER_EXCERPTS, treat it as an
            extraction anomaly, do not include the conflicting claim as fact
          - in EXCERPT-ONLY mode, never claim page count, section headings,
            tables, figures, authors, in-memory extraction, or unseen page
            coverage unless those exact facts appear in the user's request
          - use Evidence IDs: E1, E2, E3...
          - include an Evidence Matrix with columns:
            ID | Document | Page | Evidence | Supports | Confidence
          - cite file names and page numbers whenever available
          - every Key Fact must cite at least one Evidence ID
          - separate Direct Evidence from Inferences; do not put inference
            inside the fact list
          - never merge evidence from different documents without naming them
          - if the PDF file was not available and only excerpts/user claims
            were provided, label the answer EXCERPT-ONLY and do not make
            document-wide claims
          - for EXCERPT-ONLY answers, include a Source Excerpts table with
            Page and Verbatim Text before key facts
          - include open questions, extraction limits, and verification needs
          - include a Reusable Memory Index as YAML or JSON with:
            documents, evidence_ids, key_facts, page_refs, open_questions,
            tags, confidence

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(4000) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('pdf_clarify', {}) | tojson }}

          Intake:
          {{ outputs.intake | truncate(2000) }}

          Per-document digest:
          {{ outputs.per_document_digest | truncate(8000) }}
    - id: traceable_index
      kind: llm_chat
      depends_on: [cross_document_synthesis]
      with:
        system: "You build compact structured indexes for later PDF recall."
        task: |
          Build a compact memory index for later recall. Use structured fields:
          documents, key_facts, page_refs, tables, open_questions.

          Analysis:
          {{ outputs.cross_document_synthesis | truncate(6000) }}
    - id: memorize
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [traceable_index]
      tool_args:
        path: "memory/pdf-intel.md"
        mode: append
        content: "{{ outputs.traceable_index }}"
---

# PDF Intelligence (Meta-Skill)

Process one or more PDFs into a traceable analysis entry. The workflow first
classifies the request, preserves file/page evidence, synthesizes across
documents when needed, and stores a structured memory index.

## Fallback

LLM should manually run `pdf-toolkit` scripts then summarize and
`memory_save`.
