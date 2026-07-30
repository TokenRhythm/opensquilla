---
name: skill-creator-proposals
description: "Internal tool (not user-invocable). Called by meta-skill-creator's persist step and by `opensquilla meta accept` CLI (Phase 2) to manage `~/.opensquilla/proposals/`: write_proposal / list / accept. Returns JSON."
description_zh: "内部工具（不可由用户直接调用）。由meta-skill-creator的persist步骤及 opensquilla meta accept CLI（第二阶段）调用，用于管理 ~/.opensquilla/proposals/：write_proposal / list / accept。返回JSON。"
user-invocable: false
disable-model-invocation: true
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  requires:
    anyBins: ["python", "python3"]
entrypoint:
  command: python {baseDir}/scripts/proposals.py
  args:
    - --action
    - "{{ with.action | default('write_proposal') }}"
    - --skill-md-inline
    - "{{ with.skill_md | default('') }}"
    - --lint-result
    - "{{ with.lint_result | default('{}') }}"
    - --smoke-result
    - "{{ with.smoke_result | default('{}') }}"
    - --creator-mode
    - "{{ with.creator_mode | default('') }}"
    - --acceptance-result
    - "{{ with.acceptance_result | default('') }}"
    - --runtime-e2e-result
    - "{{ with.runtime_e2e_result | default('') }}"
  parse: json
  timeout: 30
---

# Skill Creator Proposals

CRUD for meta-skill proposal candidates at `~/.opensquilla/proposals/<id>/`.

## Actions

- `write_proposal --skill-md path --lint-result json --smoke-result json [--creator-mode FULL_GATED --acceptance-result text --runtime-e2e-result json]` — atomic write to `~/.opensquilla/proposals/<uuid8>/{SKILL.md,gates.json}`. Returns `{proposal_id, auto_enable_eligible}`. In `FULL_GATED` mode runtime E2E must show the meta-skill route wins or ties against the no-meta highest-tier baseline with no regressions.
- `list` — enumerate proposals with their eligibility flag
- `accept --proposal-id <id> [--force]` — move proposal to `~/.opensquilla/skills/<name>/` so it gets loaded by MANAGED layer; refuses if any gate failed (unless `--force`)

## Atomicity

write_proposal writes to `~/.opensquilla/.tmp/proposal-<id>/` then `os.rename()` to the final location, so a partial write leaves no orphan proposal dir.

## Fallback

If invoked from chat, manually create the proposals dir, copy SKILL.md, run the skill-creator-linter to populate gates.json by hand.
