---
name: meta-content-publish-pipeline
description: "Use this meta-skill instead of answering directly when the user wants to turn an idea, research, notes, talk, or document into publishable blog, social, Xiaohongshu, Zhihu, slide, newsletter, or short-video content through multi-skill orchestration."
kind: meta
meta_priority: 57
always: false
final_text_mode: "step:publish_pack"
triggers:
  - "content pipeline"
  - "发布成内容"
  - "小红书文案"
  - "知乎文章"
  - "博客改写"
  - "短视频脚本"
  - "一稿多发"
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
        system: "You parse content publishing requests and identify channels and source material."
        task: |
          Parse the publishing contract.

          Request:
          {{ inputs.user_message | xml_escape | truncate(5000) }}

          Return exactly:
          SOURCE_TYPE: <idea|notes|research|transcript|document|mixed>
          CHANNELS:
            - <blog|newsletter|xiaohongshu|zhihu|slides|short_video|other>
          AUDIENCE: <audience or unknown>
          TONE: <practical|personal|expert|sales|story>
          NEEDS_RESEARCH: <yes|no>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <source_material|channels|none>
    - id: clarify
      kind: user_input
      depends_on: [intake]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.intake"
      clarify:
        mode: form
        intro: "内容流水线需要素材和发布渠道。"
        nl_extract: true
        fields:
          - name: source_material
            type: string
            required: true
            prompt: "素材 / Source material"
            max_chars: 3000
          - name: channels
            type: string
            required: true
            prompt: "发布渠道 / Channels"
            max_chars: 200
        cancel_keywords: ["取消", "算了", "cancel", "stop"]
        timeout_hours: 24
    - id: research
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [intake, clarify]
      when: "'NEEDS_RESEARCH: yes' in outputs.intake"
      with:
        query: "{{ outputs.intake | truncate(260) }}"
        engines: [duckduckgo, brave]
        max_results: 12
    - id: source_digest
      kind: skill_exec
      skill: summarize
      depends_on: [intake, clarify, research]
      with:
        text: "Request:\n{{ inputs.user_message | xml_escape | truncate(7000) }}\n\nResearch:\n{{ outputs.research | truncate(5000) }}"
        style: content_source_digest
        max_words: 1800
    - id: channel_strategy
      kind: llm_chat
      depends_on: [source_digest]
      with:
        system: "You adapt one source idea into channel-specific content plans without clickbait."
        task: |
          Build channel strategy:
          - core thesis
          - proof/examples
          - per-channel angle
          - title hooks
          - visual suggestions
          - compliance/risk notes

          Intake:
          {{ outputs.intake | truncate(1200) }}
          Digest:
          {{ outputs.source_digest | truncate(5000) }}
    - id: publish_pack
      kind: llm_chat
      depends_on: [channel_strategy]
      with:
        system: "You write publication-ready content packs."
        task: |
          Return:
          - master brief
          - channel-specific drafts
          - short-video script if relevant
          - image/slide prompts if relevant
          - source/citation notes
          - posting checklist
          - what needs human approval

          Strategy:
          {{ outputs.channel_strategy | truncate(7000) }}
---

# Content Publish Pipeline

Transforms one idea or source packet into multi-channel publishing drafts.
