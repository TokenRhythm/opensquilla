---
name: meta-scheduled-morning-digest
description: "Compose a morning digest combining local weather, news for the user's interest topic, a structured summary, and a memory note."
kind: meta
meta_priority: 40
always: false
triggers:
  - "晨报"
  - "morning digest"
  - "每日简报"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: weather
      skill: weather
      with:
        location: "{{ inputs.user_message | xml_escape | truncate(128) }}"
    - id: news
      skill: multi-search-engine
      with:
        query: "{{ inputs.user_message | xml_escape | truncate(256) }} latest news"
        engines: [brave, duckduckgo]
        max_results: 5
    - id: digest
      skill: summarize
      depends_on: [weather, news]
      with:
        text: "Weather:\n{{ outputs.weather }}\n\nNews:\n{{ outputs.news }}"
        style: bulleted
        max_words: 500
    - id: memorize
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [digest]
      tool_args:
        path: "memory/morning-digest.md"
        mode: append
        content: "{{ outputs.digest }}"
---

# Scheduled Morning Digest (Meta-Skill)

Pulls today's weather and news on a topic, summarizes them, and records
the digest in long-term memory for later recall. The MVP runs once per
invocation; recurring scheduling is left to the host (`cron` skill or
external scheduler) and is intentionally out of scope here.

## Fallback

Have the LLM call `weather`, then `multi-search-engine`, summarize, and
finally `memory_save` manually.
