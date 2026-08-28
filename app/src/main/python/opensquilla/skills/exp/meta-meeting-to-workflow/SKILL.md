---
name: meta-meeting-to-workflow
description: "Use this meta-skill instead of answering directly when the user has meeting notes, transcripts, recordings, or rough discussion notes and wants them converted into decisions, owners, follow-ups, tasks, issues, or shareable minutes through multi-skill orchestration."
kind: meta
meta_priority: 66
always: false
final_text_mode: "step:workflow_pack"
triggers:
  - "meeting to tasks"
  - "meeting notes"
  - "会议纪要"
  - "会议转任务"
  - "把会议整理成"
  - "整理录音"
  - "action items"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [filesystem-write, network]
composition:
  steps:
    - id: intake
      kind: llm_chat
      with:
        system: "You classify meeting material and output needs without inventing attendees."
        task: |
          Extract the meeting workflow contract.

          Request and material:
          {{ inputs.user_message | xml_escape | truncate(5000) }}

          Return exactly:
          MATERIAL_TYPE: <transcript|notes|recording_path|mixed|none>
          MEETING_TOPIC: <topic or unknown>
          ATTENDEES:
            - <person or unknown>
          TARGET_SYSTEMS:
            - <github|trello|notion|slack|email|docx|none>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <material|desired_targets|none>
          OUTPUT_LANGUAGE: <language>
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: "会议转执行流还缺少材料或目标系统。请补齐最小信息。"
        nl_extract: true
        fields:
          - name: meeting_material
            type: string
            required: true
            prompt: "会议文字、录音路径或摘要 / Meeting material"
            max_chars: 3000
          - name: target_systems
            type: string
            prompt: "要生成到哪些系统 / Target systems"
            max_chars: 200
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: transcript_digest
      kind: skill_exec
      skill: summarize
      depends_on: [intake, clarify]
      with:
        text: "{{ inputs.user_message | xml_escape | truncate(8000) }}\n\nClarification:\n{{ inputs.get('collected', {}).get('clarify', {}) | tojson }}"
        style: meeting_minutes
        max_words: 2200
    - id: decision_map
      kind: llm_chat
      depends_on: [transcript_digest]
      with:
        system: "You extract meeting decisions and action items with owner confidence."
        task: |
          Build a structured map:
          - decisions
          - action items with owner, due date, dependency, confidence
          - unresolved questions
          - follow-up messages
          - items unsafe to assign because owner/due date is missing

          Digest:
          {{ outputs.transcript_digest | truncate(5000) }}
    - id: repo_or_issue_context
      kind: skill_exec
      skill: github
      depends_on: [decision_map]
      when: "'github' in (outputs.intake | lower) or 'issue' in (inputs.user_message | lower) or 'pr' in (inputs.user_message | lower)"
      with:
        task: "Inspect relevant GitHub issue/PR context mentioned in this meeting request and return only directly supported links and statuses."
    - id: artifact_plan
      kind: agent
      skill: sub-agent
      depends_on: [decision_map, repo_or_issue_context]
      with:
        task: |
          Convert the decision map into target-system payload drafts. Do not
          actually create external tasks unless the user explicitly asked for
          execution; produce copy-paste-ready GitHub issue bodies, Trello card
          titles, Slack follow-ups, or Notion sections as appropriate.

          Decision map:
          {{ outputs.decision_map | truncate(6000) }}

          GitHub context:
          {{ outputs.repo_or_issue_context | truncate(3000) }}
    - id: workflow_pack
      kind: llm_chat
      depends_on: [transcript_digest, decision_map, artifact_plan]
      with:
        system: "You assemble meeting-to-workflow deliverables with clear ownership and uncertainty labels."
        task: |
          Return the final package:
          - executive minutes
          - confirmed decisions
          - action table
          - ready-to-send follow-ups
          - task/issue/card drafts
          - missing owner/date questions
          - source limits

          Transcript digest:
          {{ outputs.transcript_digest | truncate(3500) }}
          Decision map:
          {{ outputs.decision_map | truncate(4500) }}
          Artifact plan:
          {{ outputs.artifact_plan | truncate(4500) }}
---

# Meeting To Workflow

Turns messy meeting material into minutes, decisions, action ownership, and
execution-ready task drafts.
