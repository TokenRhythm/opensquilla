---
name: meta-research-to-slide-deck
description: "Use this meta-skill instead of answering directly when the user needs a researched presentation, leadership briefing, competitive analysis deck, or source-backed slide outline that benefits from multi-skill orchestration across search, source curation, synthesis, slides, and document export."
kind: meta
meta_priority: 63
always: false
final_text_mode: "step:final_deck_brief"
triggers:
  - "research deck"
  - "调研做成PPT"
  - "做一份汇报"
  - "竞品分析PPT"
  - "slides with sources"
  - "汇报材料"
  - "source-backed deck"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [network, filesystem-write]
composition:
  steps:
    - id: brief
      kind: llm_chat
      with:
        system: "You infer deck requirements and pick conservative defaults."
        task: |
          Parse a research-to-deck request.

          Request:
          {{ inputs.user_message | xml_escape | truncate(2000) }}

          Return exactly:
          TOPIC: <topic>
          AUDIENCE: <audience or ASSUMED: leadership>
          SLIDE_COUNT: <number or ASSUMED: 8>
          DECISION_CONTEXT: <decision/use or unknown>
          STYLE: <executive|technical|sales|teaching>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <topic|audience|none>
    - id: clarify
      kind: user_input
      depends_on: [brief]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.brief"
      clarify:
        mode: form
        intro: "汇报材料需要最小主题和受众。"
        nl_extract: true
        fields:
          - name: topic
            type: string
            required: true
            prompt: "主题 / Topic"
            max_chars: 200
          - name: audience
            type: string
            prompt: "听众 / Audience"
            max_chars: 160
          - name: slide_count
            type: int
            min: 3
            max: 30
            prompt: "页数 / Slides"
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: search
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [brief, clarify]
      with:
        query: "{{ outputs.brief | truncate(260) }}"
        engines: [brave, duckduckgo, tavily]
        max_results: 18
    - id: research
      kind: skill_exec
      skill: deep-research
      depends_on: [search]
      with:
        question: "{{ outputs.brief | truncate(500) }}"
        sources: "{{ outputs.search | truncate(6000) }}"
        rounds: 2
    - id: storyline
      kind: llm_chat
      depends_on: [research]
      with:
        system: "You turn research into an executive slide storyline with source boundaries."
        task: |
          Create a slide storyline:
          - one governing message
          - slide-by-slide title, takeaway, evidence, visual suggestion
          - source IDs and unsupported-claim caveats
          - speaker notes bullets

          Brief:
          {{ outputs.brief | truncate(1200) }}
          Research:
          {{ outputs.research | truncate(8000) }}
    - id: pptx_artifact
      kind: skill_exec
      skill: pptx
      depends_on: [storyline]
      with:
        task: "Create or outline a PowerPoint deck from this source-backed storyline. Preserve source notes in speaker notes or appendix.\n{{ outputs.storyline | truncate(8000) }}"
    - id: final_deck_brief
      kind: llm_chat
      depends_on: [storyline, pptx_artifact]
      with:
        system: "You return presentation deliverables without process commentary."
        task: |
          Return:
          - deck title and audience
          - slide outline table
          - talk track
          - source list
          - artifact status/path if generated
          - what to verify before presenting

          Storyline:
          {{ outputs.storyline | truncate(6000) }}
          PPTX artifact:
          {{ outputs.pptx_artifact | truncate(3000) }}
---

# Research To Slide Deck

Builds source-backed presentation packages from a research question.
