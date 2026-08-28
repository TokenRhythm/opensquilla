---
name: meta-issue-to-pr-autopilot
description: "[DEPRECATED] Issue-to-PR autopilot — opens a PR via `gh`, runs a sub-agent fix loop, and writes to git. Disabled pending the E5 bounded sub-agent contract + side-effect ledger (plan §3.1 A8 / §5.3 E4): no risk metadata enforcement, no per-step budget, no rollback path. Do not re-enable without `metadata.opensquilla.risk: high` + capabilities {vcs, filesystem-write, network-write, subprocess} and a saga-style compensation step."
kind: meta
meta_priority: 0
always: false
disable-model-invocation: true
triggers: []
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: high
    capabilities:
      - vcs
      - filesystem-write
      - network-write
      - subprocess
composition:
  steps:
    - id: fetch_issue
      skill: github
      with:
        task: "Fetch the issue referenced in the user request and gather the relevant repo context: {{ inputs.user_message | xml_escape | truncate(512) }}"
    - id: patch
      skill: sub-agent
      depends_on: [fetch_issue]
      with:
        task: "Implement a fix for this issue."
        issue: "{{ outputs.fetch_issue }}"
    - id: pr_body
      skill: summarize
      depends_on: [patch]
      with:
        text: "{{ outputs.patch }}"
        style: pr_description
        max_words: 400
    - id: open_pr
      skill: github
      depends_on: [pr_body]
      with:
        task: "Open a pull request with the fix. Title: fix: {{ inputs.user_message | xml_escape | truncate(80) }}. Body: {{ outputs.pr_body }}"
---

# Issue-to-PR Autopilot (Meta-Skill)

Triages an issue, delegates the fix to `sub-agent`, drafts a PR
description with `summarize`, and opens the PR via `gh`. Best used on
small, well-scoped issues with clear acceptance criteria.

## Fallback

Manually call `gh issue view`, code the fix, write the PR body, then
`gh pr create`.
