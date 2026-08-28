---
name: meta-knowledge-base-bootstrap
description: "Bootstrap a domain knowledge base from a single seed (URL / PDF path / git repo / free-text topic): classify source → ingest with the right tool → persist to memory + xlsx index."
kind: meta
meta_priority: 40
always: false
triggers:
  - "搭建知识库"
  - "knowledge base"
  - "kb 启动"
  - "bootstrap kb"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: classify
      kind: llm_classify
      output_choices: [URL, PDF, GIT, TEXT]
      with:
        text: |
          Inspect this user input and decide its primary source type.

          Decision rules:
          - URL  → input contains an http/https link to a webpage (not a PDF, not a git host).
          - PDF  → input references a .pdf path or URL ending in .pdf.
          - GIT  → input references github.com / gitlab / a .git URL / a local repo path.
          - TEXT → everything else (free-text topic, question, concept).

          Input:
          {{ inputs.user_message | xml_escape | truncate(400) }}
    - id: ingest
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [classify]
    - id: memorize
      kind: tool_call
      tool: memory_save
      depends_on: [ingest]
      tool_args:
        content: |
          # KB Bootstrap: {{ inputs.user_message | xml_escape | truncate(80) }}
          Classifier verdict: {{ outputs.classify }}
          Ingestion (multi-search-engine, JSON):
          {{ outputs.ingest | truncate(2000) }}
        path: "memory/kb-bootstrap.md"
        mode: "append"
    - id: index
      skill: xlsx
      depends_on: [ingest]
      with:
        task: "Create a workbook 'kb-index.xlsx' with columns [Engine, Title, URL, Snippet]. Populate it from this multi-search-engine JSON output: {{ outputs.ingest | truncate(3000) }}"
---

# Knowledge Base Bootstrap (Meta-Skill)

Seed a domain knowledge base in one turn. The pipeline classifies the seed
source type (URL / PDF / GIT / TEXT) and ingests it via the
`multi-search-engine` skill, then persists the report and produces an
index.

| step       | kind          | skill                  | what it does                                |
|------------|---------------|------------------------|---------------------------------------------|
| classify   | `llm_classify`| —                      | label the seed as one of `URL / PDF / GIT / TEXT` |
| ingest     | `skill_exec`  | `multi-search-engine`  | run a DuckDuckGo search (JSON to stdout)    |
| memorize   | `tool_call`   | — (`memory_save`)      | append the ingestion summary to memory      |
| index      | `agent`       | `xlsx`                 | write `kb-index.xlsx` with the result table |

> The classifier is currently informational only — the ingest step always
> calls `multi-search-engine`. A previous design routed `PDF → pdf-toolkit`
> and `GIT → github`, but those branches were dropped when the DSL moved to
> `skill_exec`. A follow-up will reintroduce per-classification routing once
> the corresponding bundled skills also expose `entrypoint:` manifests.

## Fallback

If the meta-flow fails: run the classifier prompt manually, then invoke
the appropriate ingestion skill, then `memory_save` the result, then
create the xlsx index with openpyxl.
