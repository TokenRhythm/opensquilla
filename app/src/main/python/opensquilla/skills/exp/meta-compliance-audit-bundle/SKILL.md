---
name: meta-compliance-audit-bundle
description: "Auditable compliance bundle: deep-research with citations → signable .docx report → read-only PDF archive → memory note of audit findings."
kind: meta
meta_priority: 45
always: false
triggers:
  - "合规审计"
  - "compliance audit"
  - "audit bundle"
  - "GDPR 自评"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: research
      skill: deep-research
      with:
        question: "{{ inputs.user_message | xml_escape | truncate(512) }}"
        rounds: 1
    - id: report
      skill: docx
      depends_on: [research]
      with:
        title: "Compliance Report — {{ inputs.user_message | xml_escape | truncate(96) }}"
        body: "{{ outputs.research }}"
    - id: archive
      skill: html-to-pdf
      depends_on: [research]
      with:
        html: |
          <!DOCTYPE html>
          <html><head><meta charset="utf-8"><title>Audit Archive — {{ inputs.user_message | xml_escape | truncate(96) }}</title></head>
          <body>
            <h1>Audit Archive: {{ inputs.user_message | xml_escape | truncate(128) }}</h1>
            <article>{{ outputs.research | xml_escape }}</article>
          </body></html>
        page_size: A4
    - id: memorize
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [research]
      tool_args:
        path: "memory/compliance-audit.md"
        mode: append
        content: "{{ outputs.research }}"
---

# Compliance Audit Bundle (Meta-Skill)

Audit-grade output for GDPR / SOC2-style self-assessments. The
deep-research step preserves a per-claim citation list; the `.docx`
report is the signable version, the `.pdf` is the read-only archive,
and the memory note guarantees the evidence chain is recallable later.

## Fallback

Manually run deep-research → docx → html-to-pdf → memory_save in order.
