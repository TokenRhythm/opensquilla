---
name: meta-multi-format-export-pack
description: "From one piece of source content, render four deliverables: .docx report, .pptx slides, .xlsx data, and an HTML/PDF public version."
kind: meta
meta_priority: 60
always: false
triggers:
  - "多格式导出"
  - "multi format export"
  - "全格式输出"
  - "export pack"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: model
      skill: summarize
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(40000) }}"
        style: structured_model
        max_words: 1500
    - id: to_docx
      skill: docx
      depends_on: [model]
      with:
        title: "{{ inputs.user_message | xml_escape | truncate(128) }} — report"
        body: "{{ outputs.model }}"
    - id: to_pptx
      skill: pptx
      depends_on: [model]
      with:
        title: "{{ inputs.user_message | xml_escape | truncate(128) }} — slides"
        outline: "{{ outputs.model }}"
    - id: to_xlsx
      skill: xlsx
      depends_on: [model]
      with:
        task: "Create a workbook named '{{ inputs.user_message | slugify | truncate(64) }}-data.xlsx' from this structured content: {{ outputs.model }}"
    - id: to_pdf
      skill: html-to-pdf
      depends_on: [model]
      with:
        html: |
          <!DOCTYPE html>
          <html><head><meta charset="utf-8"><title>{{ inputs.user_message | xml_escape | truncate(128) }}</title></head>
          <body>
            <h1>{{ inputs.user_message | xml_escape | truncate(128) }}</h1>
            <article>{{ outputs.model | xml_escape }}</article>
          </body></html>
        page_size: A4
---

# Multi-Format Export Pack (Meta-Skill)

Renders one source content into four deliverables for different audiences:
- `.docx` — detailed report
- `.pptx` — slide deck
- `.xlsx` — data breakdown
- `.pdf` — public-facing print version

MVP runs the four renders **sequentially** (after the shared `model` step);
true parallel fan-out is future work (M7 in the proposal).

## Fallback

LLM should manually summarize first, then call docx / pptx / xlsx /
html-to-pdf in order.
