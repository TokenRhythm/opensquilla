---
name: meta-pdf-reformat-pipeline
description: "Modernize a legacy PDF: structural extraction → natural-language rewrite of problem pages → audit summary → re-merge into the final PDF."
kind: meta
meta_priority: 45
always: false
triggers:
  - "pdf 重排"
  - "pdf reformat"
  - "pdf 现代化"
  - "rewrite pdf"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: extract
      skill: pdf-toolkit
      with:
        task: "Extract structured text + page metadata from the PDF referenced in: {{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: rewrite
      skill: nano-pdf
      depends_on: [extract]
      with:
        instruction: "Modernize phrasing and standardize headings on problematic pages while preserving original meaning."
        source_text: "{{ outputs.extract }}"
    - id: audit
      skill: summarize
      depends_on: [rewrite]
      with:
        text: "{{ outputs.rewrite }}"
        style: change_summary
        max_words: 600
    - id: merge
      skill: pdf-toolkit
      depends_on: [rewrite, audit]
      with:
        task: "Merge the rewritten pages back into the source PDF. Audit summary for review: {{ outputs.audit }}"
---

# PDF Reformat Pipeline (Meta-Skill)

Historical-contract / scanned-manual / legal-document modernization in
4 steps: extract → rewrite → audit → re-merge. The `audit` step's output
gives a human reviewer a diff-summary before the merge is finalized.

## Fallback

Run pdf-toolkit extract → nano-pdf rewrite → summarize → pdf-toolkit merge
manually.
