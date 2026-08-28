---
name: meta-sales-lead-researcher
description: "Use this meta-skill instead of answering directly when the user wants account research, lead qualification, company/person briefing, outreach prep, or a sales call brief that benefits from multi-skill orchestration across web research, browser/source review, CRM-style notes, and email drafting."
kind: meta
meta_priority: 59
always: false
final_text_mode: "step:lead_brief"
triggers:
  - "sales lead research"
  - "account brief"
  - "客户调研"
  - "销售线索"
  - "拜访前调研"
  - "outreach prep"
  - "company brief"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [network, filesystem-write]
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You parse lead research requests and avoid inventing private contact data."
        task: |
          Parse the lead research contract.

          Request:
          {{ inputs.user_message | xml_escape | truncate(2500) }}

          Return exactly:
          TARGETS:
            - <company/person/domain>
          SALES_GOAL: <meeting|proposal|partnership|renewal|unknown>
          PRODUCT_CONTEXT: <brief or unknown>
          REGION: <region or unknown>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <target|sales_goal|none>
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: "销售调研需要目标公司/联系人和拜访目的。"
        nl_extract: true
        fields:
          - name: target
            type: string
            required: true
            prompt: "目标公司/联系人 / Target"
            max_chars: 240
          - name: sales_goal
            type: string
            prompt: "销售目标 / Sales goal"
            max_chars: 240
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: web_search
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [intake, clarify]
      with:
        query: "{{ outputs.intake | truncate(320) }} company news hiring funding product leadership competitors"
        engines: [duckduckgo, brave]
        max_results: 20
    - id: source_review
      kind: agent
      skill: sub-agent
      depends_on: [web_search]
      with:
        task: |
          Review the public-source lead evidence. Do not scrape login-gated
          sites or infer private personal data. Summarize official site,
          news, hiring signals, product clues, pain hypotheses, and weak data.

          Intake:
          {{ outputs.intake | truncate(1000) }}
          Search:
          {{ outputs.web_search | truncate(8000) }}
    - id: outreach_drafts
      kind: llm_chat
      depends_on: [source_review]
      with:
        system: "You write respectful, source-grounded B2B outreach drafts."
        task: |
          Draft:
          - 3 personalized email options
          - 1 LinkedIn/message variant
          - discovery-call questions
          - objections and responses
          - CRM notes

          Source review:
          {{ outputs.source_review | truncate(7000) }}
    - id: lead_brief
      kind: llm_chat
      depends_on: [source_review, outreach_drafts]
      with:
        system: "You assemble sales account briefs with evidence and caveats."
        task: |
          Return:
          - account snapshot
          - trigger events
          - pain hypotheses
          - stakeholder map if public evidence supports it
          - outreach drafts
          - discovery questions
          - do-not-claim list
          - source links

          Review:
          {{ outputs.source_review | truncate(6000) }}
          Drafts:
          {{ outputs.outreach_drafts | truncate(5000) }}
---

# Sales Lead Researcher

Creates evidence-grounded account briefs and outreach drafts from public data.
