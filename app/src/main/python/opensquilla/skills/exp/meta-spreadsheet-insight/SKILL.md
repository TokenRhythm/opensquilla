---
name: meta-spreadsheet-insight
description: "Turn an Excel workbook into business insight: structured read → trend/anomaly summary → write back to a new 'Insights' sheet → persist KPIs to memory."
kind: meta
meta_priority: 55
always: false
triggers:
  - "spreadsheet insight"
  - "excel 分析"
  - "xlsx 洞察"
  - "数据复盘"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: read
      skill: xlsx
      with:
        task: "Inspect the spreadsheet referenced in this user request and return all sheets' data: {{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: analyze
      skill: summarize
      depends_on: [read]
      with:
        text: "{{ outputs.read }}"
        style: trend_analysis
        max_words: 1000
    - id: writeback
      skill: xlsx
      depends_on: [analyze]
      with:
        task: "Append a new 'Insights' sheet to the workbook with the following analysis: {{ outputs.analyze }}"
    - id: memorize
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [analyze]
      tool_args:
        path: "memory/spreadsheet-kpi.md"
        mode: append
        content: "{{ outputs.analyze }}"
---

# Spreadsheet Insight (Meta-Skill)

Reads a workbook, computes a structured trend / anomaly analysis, writes
the result back as a new sheet, and persists key KPIs to long-term memory.

## Fallback

LLM should call xlsx read, summarize, xlsx append, then `memory_save`.
