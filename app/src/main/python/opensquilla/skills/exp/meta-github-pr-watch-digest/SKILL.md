---
name: meta-github-pr-watch-digest
description: "Inspect the user's open GitHub PRs / failing CI / new issues via `gh`, summarize into 3 buckets (to-review / awaiting-me / CI-red), and persist follow-ups to memory."
kind: meta
meta_priority: 45
always: false
triggers:
  - "PR 巡检"
  - "PR digest"
  - "github 巡检"
  - "watch my prs"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: pull_prs
      skill: github
      with:
        task: "List open PRs in the relevant repos: those awaiting my review, those I authored that received new comments, and any failing CI runs. Filter scope from this user prompt: {{ inputs.user_message | xml_escape | truncate(256) }}"
    - id: digest
      skill: summarize
      depends_on: [pull_prs]
      with:
        text: "{{ outputs.pull_prs }}"
        style: bulleted
        max_words: 400
    - id: memorize
      kind: tool_call
      tool: memory_save
      tool_allowlist: [memory_save]
      depends_on: [digest]
      tool_args:
        path: "memory/pr-watch.md"
        mode: append
        content: "{{ outputs.digest }}"
---

# GitHub PR Watch & Digest (Meta-Skill)

Daily PR-queue triage: pulls open PRs / failing CI / new comments via
the `github` skill (gh CLI), digests into three actionable buckets, and
records follow-ups to long-term memory.

## Fallback

LLM should manually call `gh pr list`, `gh run list --status failure`,
summarize, and `memory_save`.
