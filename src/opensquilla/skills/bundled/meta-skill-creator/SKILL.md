---
name: meta-skill-creator
description: "Use this meta-skill instead of answering directly only when the current user explicitly asks to create, compose, synthesize, or propose a new meta-skill that orchestrates multiple existing skills. It uses multi-skill orchestration for intent clarification, optional history mining, trigger-collision checks, linting, smoke/runtime gates, preview, and optional proposal persistence. Do not use it for creating a normal standalone skill, asking how meta-skills work, analyzing pasted skill lists, or discussing existing meta-skills."
description_zh: "仅当用户明确要求创建、组合、合成或提出一个编排多个现有技能的新元技能时，使用此元技能而非直接回答。它通过多技能编排完成意图澄清、可选的历史挖掘、触发词冲突检查、linting、冒烟/运行时闸门、预览和可选的提案持久化。不用于创建普通独立技能、询问元技能原理、分析粘贴的技能列表或讨论现有元技能。"
kind: meta
meta_priority: 90
always: false
final_text_mode: "step:final_response"
request_template:
  outcome: "Proposed meta-skill spec or saved proposal with trigger, DAG, tests, and validation notes."
  outcome_zh: "生成 meta-skill 提案或保存方案，包含触发词、DAG、测试和验证记录。"
  outcome_en: "Proposed meta-skill spec or saved proposal with trigger, DAG, tests, and validation notes."
  fields:
    - name: meta_skill_goal
      label_zh: "Meta-skill 目标"
      label_en: "Meta-skill goal"
      required: true
    - name: existing_skills_to_orchestrate
      label_zh: "要编排的现有技能"
      label_en: "Existing skills to orchestrate"
      required: false
    - name: save_or_preview
      label_zh: "保存或预览"
      label_en: "Save or preview"
      required: false
      default: "preview unless the user asks to persist"
      default_zh: "默认预览；仅在用户要求持久化时保存"
      default_en: "preview unless the user asks to persist"
    - name: constraints
      label_zh: "限制条件"
      label_en: "Constraints"
      required: false
    - name: audience
      label_zh: "受众"
      label_en: "Audience"
      required: false
      default: "meta-skill author"
      default_zh: "meta-skill 作者"
      default_en: "meta-skill author"
    - name: language
      label_zh: "输出语言"
      label_en: "Output language"
      required: false
      default: "match the user's language"
      default_zh: "跟随用户语言"
      default_en: "match the user's language"
  assumptions:
    - "Create a meta-skill only when orchestration is explicitly requested."
    - "Check trigger collisions and lint before presenting a proposal."
  assumptions_zh:
    - "仅在用户明确要求编排时创建 meta-skill。"
    - "展示提案前检查触发词冲突并运行 lint。"
  assumptions_en:
    - "Create a meta-skill only when orchestration is explicitly requested."
    - "Check trigger collisions and lint before presenting a proposal."
output_contract:
  append_to_final_text: false
  required_sections:
    - "Intent summary"
    - "Proposed DAG"
    - "Trigger and collision notes"
    - "Validation results"
    - "Save or next-step status"
  assumptions:
    - "Preview mode is used unless persistence is explicit."
  unverified:
    - "Live runtime smoke results when execution gates are unavailable."
  artifacts:
    - name: "meta_skill_proposal"
      required: false
eval_prompts:
  - name: "meta-skill-creator-baseline"
    prompt: "Draft a meta-skill that orchestrates search, synthesis, validation, and proposal persistence."
    rubric:
      - "Intent summary"
      - "Proposed DAG"
      - "Trigger and collision notes"
      - "Validation results"
      - "Save or next-step status"
preference_keys:
  - preferred_language
  - meta_authoring_style
policy_tags:
  - trigger-collision-check
  - lint-before-enable
triggers:
  - "新增 meta 技能"
  - "组合现有 skill 成 meta-skill"
  - "create a meta-skill"
  - "new meta-skill"
  - "orchestrates existing skills"
  - "orchestrates search"
  - "compose existing skills"
  - "synthesize meta-skill"
  - "compose meta-skill"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: clarify_intent
      label: "意图澄清"
      label_en: "Intent clarification"
      kind: llm_chat
      with:
        system: |
          You are the intent gate for meta-skill-creator. Do not inspect
          workspace files, history, memory, or external sources. Decide only
          from the explicit user request and activation context.
        task: |
          Clarify whether the user wants a meta-skill, not a normal standalone
          skill. If the request is generic skill creation, return
          ROUTE: normal-skill. If it requires orchestrating multiple existing
          skills, return ROUTE: meta-skill. Also summarize desired inputs,
          outputs, trigger phrases, and whether a human preference branch is
          needed. Set NEEDS_CLARIFICATION: yes only when the workflow goal,
          output shape, trigger boundary, or human preference branch is
          genuinely ambiguous and the request is an interactive user request.
          For unattended auto-propose, dream, or cron activation, set
          NEEDS_CLARIFICATION: no and continue from available context.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Outer system / activation context:
          {{ inputs.system_prompt | default("") | xml_escape | truncate(1200) }}

          Return:
          ROUTE: <normal-skill|meta-skill>
          WORKFLOW_GOAL: <goal or unclear>
          OUTPUT_SHAPE: <deliverable or unclear>
          TRIGGERS: <phrases or unclear>
          HUMAN_PREFERENCE_BRANCH: <yes|no|unclear>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <workflow_goal|output_shape|trigger_boundary|human_preference_branch|none>
          CLARIFY_REASON: <one concise reason, or none>

    - id: creator_clarify
      label: "创建澄清"
      label_en: "Creation clarification"
      kind: user_input
      depends_on: [clarify_intent]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and 'needs_clarification: yes' in (outputs.clarify_intent | lower)"
      clarify:
        mode: form
        intro: |
          新 meta-skill 的边界还不够明确。请补齐目标和输出形态，避免生成过宽的触发词。
        intro_zh: "新 meta-skill 的边界还不够明确。请补齐目标和输出形态，避免生成过宽的触发词。"
        intro_en: "The new meta-skill boundary is not clear enough. Fill in the goal and output shape so the trigger stays precise."
        nl_extract: true
        fields:
          - name: workflow_goal
            type: string
            required: true
            prompt: "工作流目标 / Workflow goal"
            prompt_zh: "工作流目标"
            prompt_en: "Workflow goal"
            max_chars: 300
          - name: output_shape
            type: string
            required: true
            prompt: "最终输出形态 / Output shape"
            prompt_zh: "最终输出形态"
            prompt_en: "Output shape"
            max_chars: 200
          - name: trigger_boundary
            type: string
            prompt: "触发边界或不要覆盖的场景 / Trigger boundary"
            prompt_zh: "触发边界或不要覆盖的场景"
            prompt_en: "Trigger boundary or cases to avoid"
            max_chars: 300
          - name: human_preference_branch
            type: bool
            default: false
            prompt: "是否需要运行中让用户选择偏好 / Need human preference branch?"
            prompt_zh: "是否需要运行中让用户选择偏好"
            prompt_en: "Need a human preference branch during the run?"
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24

    - id: normal_skill_exit
      label: "普通技能退出"
      label_en: "Regular skill exit"
      kind: tool_call
      depends_on: [clarify_intent]
      when: "'route: normal-skill' in (outputs.clarify_intent | lower)"
      tool: emit_text
      tool_args:
        text: |
          This request was classified as a normal standalone skill request, not
          a meta-skill composition request. The meta-skill creator stopped
          before proposal assembly or persistence.

    - id: creator_mode
      label: "创建模式"
      label_en: "Creation mode"
      kind: llm_classify
      depends_on: [clarify_intent, creator_clarify]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      output_choices:
        - PREVIEW_ONLY
        - PERSISTED_PROPOSAL
        - FULL_GATED
      with:
        text: |
          Classify how far the creator workflow should go.

          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Outer system / activation context:
          {{ inputs.system_prompt | default("") | xml_escape | truncate(1200) }}

          Clarified intent:
          {{ outputs.clarify_intent | truncate(1200) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('creator_clarify', {}) | tojson }}

          Decision rules:
          - PREVIEW_ONLY: user asks for an example, template, plan, draft,
            or wants to inspect before writing/persisting anything.
          - PERSISTED_PROPOSAL: user asks to create/save/write/propose a
            meta-skill but does not ask for exhaustive smoke testing.
          - FULL_GATED: user asks for a production-ready, accepted, tested,
            validated, or fully gated meta-skill.
          - FULL_GATED: unattended auto-propose, dream, or cron activation
            requires preserving all creator gates before any auto-enable
            decision.

    - id: harvest
      label: "需求采集"
      label_en: "Requirement capture"
      kind: skill_exec
      skill: history-explorer
      depends_on: [clarify_intent, creator_clarify]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and 'Unattended meta-skill auto-propose run' in inputs.get('system_prompt', '')"
      on_failure: harvest_empty
      with:
        query: |
          Co-occurring skill chains and meta-skill usage for: {{ outputs.clarify_intent | truncate(1000) }}
          Clarification answers:
          {{ inputs.get('collected', {}).get('creator_clarify', {}) | tojson }}
        window_days: 30
        include: [co_occurrences, meta_usage, router_fixtures]

    - id: harvest_empty
      label: "空采集兜底"
      label_en: "Empty-capture fallback"
      kind: tool_call
      tool: emit_text
      tool_args:
        text: "no history available; downstream should rely on user intent only"

    - id: pick_pattern
      label: "模式选择"
      label_en: "Mode selection"
      kind: llm_classify
      depends_on: [creator_mode, harvest]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      output_choices: [p1_sequential, p2_fan_out_merge, p3_condition_gated]
      with:
        history_summary: "{{ outputs.harvest | truncate(2000) }}"
        user_intent: |
          Raw user request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Clarified intent:
          {{ outputs.clarify_intent | truncate(1000) }}

    - id: fill_slots
      label: "填充槽位"
      label_en: "Fill slots"
      kind: tool_call
      depends_on: [pick_pattern]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      tool: meta_skill_fill_slots
      tool_args:
        pattern_id: "{{ outputs.pick_pattern }}"
        history_summary: "{{ outputs.harvest | truncate(2000) }}"
        user_intent: |
          Raw user request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Clarified intent:
          {{ outputs.clarify_intent | truncate(1000) }}

    - id: assemble
      label: "组装"
      label_en: "Assembly"
      kind: tool_call
      depends_on: [fill_slots]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      tool: meta_skill_assemble
      tool_args:
        pattern_id: "{{ outputs.pick_pattern }}"
        slots_json: "{{ outputs.fill_slots }}"

    - id: collision_check
      label: "冲突检查"
      label_en: "Conflict check"
      kind: llm_chat
      depends_on: [assemble]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      with:
        system: |
          You are a trigger-collision reviewer for meta-skill-creator. Use only
          the candidate SKILL.md provided in the task and the bundled creator
          boundaries named there. Do not call tools or inspect the workspace.
        task: |
          Review this generated meta-skill proposal for trigger collisions with
          existing bundled skills. Flag generic triggers, overlaps with
          meta-skill-creator, and broad phrases that would steal unrelated user
          intent. Return PASS or REVISE_NEEDED plus reasons.

          Candidate SKILL.md:
          {{ outputs.assemble | truncate(8000) }}

    - id: lint
      label: "Lint 检查"
      label_en: "Lint check"
      kind: tool_call
      depends_on: [collision_check]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      tool: meta_skill_lint_run
      tool_args:
        skill_md: "{{ outputs.assemble }}"
        gates: "G1,G2"

    - id: risk_classify
      label: "风险分类"
      label_en: "Risk classification"
      kind: llm_chat
      depends_on: [lint]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      with:
        system: |
          You are an operational-risk classifier for generated meta-skills. Use
          only the candidate SKILL.md and lint result in the task. Do not call
          tools or inspect the workspace.
        task: |
          Classify operational risk for the generated meta-skill. Consider file
          writes, network access, GitHub/gh actions, shell commands, memory
          writes, and destructive operations. Return:
          RISK: low|medium|high
          CAPABILITIES:
            - <capability>
          REQUIRED_GATES:
            - <gate>

          Candidate SKILL.md:
          {{ outputs.assemble | truncate(8000) }}

          Lint result:
          {{ outputs.lint | truncate(2000) }}

    - id: single_model_baseline
      label: "单模基线"
      label_en: "Single-mode baseline"
      kind: llm_chat
      depends_on: [creator_mode]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and outputs.creator_mode == 'FULL_GATED'"
      with:
        system: |
          You are the highest-tier baseline model for meta-skill authoring.
          Solve the same task directly in one pass under the same outer
          assistant system prompt and user request, but without history
          mining, intent clarification output, deterministic slot filling,
          lint tools, smoke tools, persistence, or sub-skill orchestration.
          Produce the strongest standalone SKILL.md candidate you can from
          that full prompt context.
        task: |
          Same task as the orchestrated meta-skill creator workflow, but solve
          it as a standalone highest-tier model response. Use the outer system
          prompt and raw user request below; do not rely on any meta-skill
          intermediate output.

          Outer system prompt:
          {{ inputs.system_prompt | xml_escape | truncate(12000) }}

          User request:
          {{ inputs.user_message | xml_escape | truncate(1600) }}

          Return:
          - proposed meta-skill name
          - triggers
          - inputs
          - step graph
          - gates
          - collision risks
          - SKILL.md preview

          Boundary rule:
          Creator validation, proposal persistence, auto-enable decisions,
          and gate execution are handled by the outer meta-skill-creator
          workflow. Do not require the generated candidate SKILL.md itself to
          contain steps for saving proposals, running creator gates, comparing
          against baselines, or deciding auto-enable. The candidate SKILL.md
          should describe only the reusable business workflow that will run
          later when the new meta-skill is invoked.

    - id: acceptance_compare
      label: "验收对比"
      label_en: "Acceptance comparison"
      kind: llm_chat
      depends_on: [assemble, single_model_baseline]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and outputs.creator_mode == 'FULL_GATED'"
      with:
        system: |
          You are an acceptance reviewer. Compare an orchestrated candidate
          against a single-model baseline that used the highest-tier model on
          the same task. Reward verifiable skill composition, trigger safety,
          gates, operational risk handling, and reusable SKILL.md quality.
          Keep the boundary strict: proposal persistence, gate execution,
          runtime E2E, acceptance comparison, and auto-enable decisions belong
          to the outer meta-skill-creator workflow. Do not penalize a candidate
          SKILL.md for omitting creator-workflow steps that should not run when
          the generated meta-skill is invoked later.
        task: |
          User request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Orchestrated candidate:
          {{ outputs.assemble | truncate(7000) }}

          Single-model baseline:
          {{ outputs.single_model_baseline | truncate(7000) }}

          Return this exact structure:
          WINNER: orchestrated|single-model|tie
          QUALITY_SCORE: <0.00-1.00 weighted final product quality score>
          REASONS:
          - <specific evidence>
          REGRESSIONS:
          - <what the orchestrated candidate lacks versus the baseline>
          REQUIRED_IMPROVEMENTS:
          - <blocking edit required before acceptance, or "none">

          Treat REQUIRED_IMPROVEMENTS as a hard acceptance gate. Do not list
          optional nice-to-have enhancements there. If the orchestrated
          candidate is production-acceptable and any baseline advantages are
          non-blocking, put those advantages under REGRESSIONS and set
          REQUIRED_IMPROVEMENTS to "none".
          Score final product quality with high weight: 40% usefulness and
          completeness of the generated SKILL.md, 25% trigger/input/output
          specificity, 20% gate/risk/collision coverage, and 15% reusable
          workflow generality. Scores below 0.80 are not acceptable for
          FULL_GATED persistence even when WINNER is orchestrated.
          Never make proposal persistence, auto-enable state, acceptance
          comparison, or runtime E2E execution a REQUIRED_IMPROVEMENT for the
          candidate SKILL.md; those are already performed by this outer creator
          workflow and are evaluated from the creator's gate outputs.

    - id: smoke
      label: "冒烟测试"
      label_en: "Smoke test"
      kind: tool_call
      depends_on: [risk_classify]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and outputs.creator_mode != 'PREVIEW_ONLY'"
      tool: meta_skill_smoke_run
      tool_args:
        skill_md: "{{ outputs.assemble }}"
        fixture_gen_model: openai/gpt-4o-mini
        classifier_model: openrouter/auto

    - id: runtime_e2e
      label: "运行时 E2E"
      label_en: "Runtime E2E"
      kind: tool_call
      depends_on: [assemble, smoke]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and outputs.creator_mode == 'FULL_GATED'"
      tool: meta_skill_runtime_e2e_run
      tool_args:
        skill_md: "{{ outputs.assemble }}"
        # Leave eval_prompts empty so the runtime gate derives an operational
        # positive prompt from the candidate skill's own trigger. The outer
        # creator request asks for a meta-skill proposal; using it here would
        # incorrectly compare a candidate workflow run against proposal prose.
        eval_prompts: ""

    - id: preview
      label: "预览"
      label_en: "Preview"
      kind: llm_chat
      depends_on: [smoke, acceptance_compare, runtime_e2e]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower)"
      with:
        system: |
          You are the final preview writer for meta-skill-creator. Produce only
          a concise operator-facing proposal preview from the supplied step
          outputs. Do not call tools, inspect files, or invent persistence IDs.
        task: |
          Produce a concise proposal preview for the user/operator before
          persistence. Include proposed name, triggers, DAG summary, collision
          result, risk classification, lint status, smoke status, baseline
          comparison status, and whether it appears eligible for acceptance.
          Do not invent paths or proposal IDs.

          Candidate SKILL.md:
          {{ outputs.assemble | truncate(8000) }}

          Collision check:
          {{ outputs.collision_check | truncate(1200) }}

          Risk:
          {{ outputs.risk_classify | truncate(1200) }}

          Creator mode:
          {{ outputs.creator_mode }}

          Lint:
          {{ outputs.lint | truncate(2000) }}

          Smoke:
          {{ outputs.smoke | truncate(2000) }}

          Baseline comparison:
          {{ outputs.acceptance_compare | truncate(2000) }}

          Runtime E2E:
          {{ outputs.runtime_e2e | truncate(2000) }}

    - id: persist
      label: "保存"
      label_en: "Save"
      kind: tool_call
      depends_on: [preview]
      when: "'route: meta-skill' in (outputs.clarify_intent | lower) and outputs.creator_mode != 'PREVIEW_ONLY'"
      tool: meta_skill_persist_proposal
      tool_args:
        skill_md: "{{ outputs.assemble }}"
        lint_result: "{{ outputs.lint }}"
        smoke_result: "{{ outputs.smoke }}"
        creator_mode: "{{ outputs.creator_mode }}"
        acceptance_result: "{{ outputs.acceptance_compare }}"
        runtime_e2e_result: "{{ outputs.runtime_e2e }}"
        collision_result: "{{ outputs.collision_check }}"
        risk_result: "{{ outputs.risk_classify }}"

    - id: final_response
      label: "最终回复"
      label_en: "Final response"
      kind: tool_call
      depends_on: [preview, normal_skill_exit]
      tool: emit_text
      tool_args:
        text: |
          {% if outputs.normal_skill_exit %}
          {{ outputs.normal_skill_exit }}
          {% else %}
          {{ outputs.preview }}
          {% endif %}
---

# Meta-Skill Creator

Safeguarded DAG that synthesizes a new bundled meta-skill from observed skill
co-occurrence patterns + user description of the desired workflow. It now
separates preview-only, persisted-proposal, and fully gated modes so lightweight
requests do not pay for persistence or smoke testing. The workflow separates
generic skill creation from meta-skill composition, checks trigger collisions,
classifies operational risk, and previews the proposal before optional
persistence.

Output is a SKILL.md candidate written to `~/.opensquilla/proposals/<id>/`.
By default it is not auto-loaded; run `opensquilla meta accept <id>` (Phase 2)
to enable. If the operator has enabled the auto-propose `auto_enable` setting,
this manual path also runs the same conservative static safety preflight used by
cron/dream auto-propose and may promote a low-risk gated proposal immediately.

## Fallback

If creator's pipeline fails at any step, **report the failure verbatim** to the
user:

1. State which step failed (e.g. "harvest", "lint")
2. Quote the error message from the orchestrator's structured log
3. Stop. Do NOT improvise.

Do NOT:
- Claim a proposal was written unless you have verified it by reading
  `~/.opensquilla/proposals/<id>/SKILL.md` with the `read_file` tool
- Invent file paths, proposal IDs, or skill names that you have not seen
  in the orchestrator's actual output
- "Manually run" the individual skills as a recovery — that bypasses
  the validation gates the user explicitly opted into

If the user wants to retry, suggest they re-issue the request after the
underlying error is resolved (often a sandbox or provider issue), not a
manual workaround.
