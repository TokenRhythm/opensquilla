---
name: meta-home-it-rescue
description: "Use this meta-skill instead of answering directly when the user needs help with home, small-team, laptop, browser, printer, Docker, Git, network, UI, or deployment troubleshooting that benefits from multi-skill orchestration across symptom intake, environment capture, web lookup, and repair planning."
kind: meta
meta_priority: 60
always: false
final_text_mode: "step:rescue_plan"
triggers:
  - "home it rescue"
  - "电脑坏了"
  - "网络不通"
  - "打印机"
  - "Docker 出错"
  - "Git 出错"
  - "浏览器问题"
  - "部署坏了"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [shell, network, filesystem-read]
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You classify IT rescue issues and separate safe observation from risky repair."
        task: |
          Parse the troubleshooting request.

          Request:
          {{ inputs.user_message | xml_escape | truncate(4000) }}

          Return exactly:
          DOMAIN: <browser|network|printer|docker|git|deployment|desktop|unknown>
          SYMPTOMS:
            - <symptom>
          PLATFORM: <mac|linux|windows|unknown>
          CAN_RUN_COMMANDS: <yes|no|unknown>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <symptom|platform|none>
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: "排障需要症状和设备/系统信息。"
        nl_extract: true
        fields:
          - name: symptom
            type: string
            required: true
            prompt: "具体症状 / Symptom"
            max_chars: 600
          - name: platform
            type: string
            prompt: "系统/设备 / Platform"
            max_chars: 160
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: knowledge_search
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [intake, clarify]
      with:
        query: "{{ outputs.intake | truncate(320) }} troubleshooting official docs error"
        engines: [duckduckgo, brave]
        max_results: 12
    - id: local_context
      kind: agent
      skill: sub-agent
      depends_on: [intake, clarify]
      with:
        task: |
          From the user's pasted logs, screenshots descriptions, commands, or
          file paths, build a safe diagnostic context. Do not run destructive
          commands. For Docker/Git/deployment cases, propose read-only checks.

          Request:
          {{ inputs.user_message | xml_escape | truncate(5000) }}
    - id: repair_strategy
      kind: llm_chat
      depends_on: [knowledge_search, local_context]
      with:
        system: "You design reversible troubleshooting plans with stop conditions."
        task: |
          Build a rescue plan:
          - likely causes ranked
          - read-only checks first
          - reversible fixes
          - commands with purpose
          - danger zone actions requiring explicit approval
          - when to escalate to vendor/professional

          Search:
          {{ outputs.knowledge_search | truncate(5000) }}
          Local context:
          {{ outputs.local_context | truncate(5000) }}
    - id: rescue_plan
      kind: llm_chat
      depends_on: [repair_strategy]
      with:
        system: "You return practical IT rescue instructions."
        task: |
          Return:
          - immediate diagnosis
          - first 3 safe checks
          - step-by-step repair path
          - rollback
          - what evidence to capture if it still fails

          Strategy:
          {{ outputs.repair_strategy | truncate(7000) }}
---

# Home IT Rescue

Creates safe, reversible troubleshooting plans for home and small-team IT issues.
