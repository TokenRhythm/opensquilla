---
name: say-and-dispatch
description: "Use this meta-skill instead of answering directly when the user asks to use specific named models for different parts of a task in a single sentence, e.g. 'use deepseek-flash to review the doc then use deepseek-pro to write code'. It parses the natural-language request into per-model subtasks, resolves model aliases against the real router tier configuration via gateway config_get, dispatches each task to the real model via sessions_spawn with model override, and summarizes which real model completed which work. Do not use for single-model requests, general questions, or when the user does not mention specific model names."
description_zh: "当用户在一句话内用自然语言指定不同模型执行不同分工任务时，使用此元技能而非直接回答。它将自然语言请求解析为各模型子任务，通过 gateway config_get 检查真实路由配置，将模型别名匹配到实际路由分层，通过 sessions_spawn 以模型覆盖方式分发任务到真实模型执行，并在总结中提及真实模型完成了哪些工作。不用于单模型请求、一般问题或用户未提及具体模型名称的场景。"
kind: meta
meta_priority: 75
always: false
final_text_mode: "step:synthesize"
request_template:
  outcome: "Multi-model task execution summary mentioning which real model completed which work."
  outcome_zh: "多模型任务执行总结，提及真实模型完成了哪些工作。"
  outcome_en: "Multi-model task execution summary mentioning which real model completed which work."
  fields:
    - name: task_assignment
      label_zh: "任务分配描述"
      label_en: "Task assignment description"
      required: true
    - name: constraints
      label_zh: "限制条件"
      label_en: "Constraints"
      required: false
  assumptions:
    - "The user explicitly mentions model names (e.g. deepseek-flash, deepseek-pro, glm)."
    - "Model aliases are matched semantically by the LLM against real router tier model IDs — no hard-coded substring rules."
    - "If multiple tiers share the same model, the lowest tier is used."
    - "Tasks may have dependencies; execute in topological order."
  assumptions_zh:
    - "用户明确提及模型名称（如 deepseek-flash、deepseek-pro、glm）。"
    - "模型别名由 LLM 语义匹配到真实路由层模型 ID，不使用硬编码子串规则。"
    - "如果多个层共享同一模型，使用最低层。"
    - "任务可能有依赖关系；按拓扑顺序执行。"
output_contract:
  append_to_final_text: false
  required_sections:
    - "Per-task: real model name, user alias, task description, result summary"
    - "Overall completion status mentioning real model names"
  assumptions:
    - "Router configuration is available and readable via gateway config_get."
  unverified:
    - "Network availability for sub-agent sessions."
    - "Model availability and response quality depend on provider status."
  artifacts: []
eval_prompts:
  - name: "two-model-basic"
    prompt: "使用deepseek-flash查看文档中最欠缺的一个地方，然后使用deepseek-pro编写代码完善它。"
    rubric:
      - "Two model assignments extracted correctly"
      - "deepseek-flash matched to deepseek-v4-flash"
      - "deepseek-pro matched to deepseek-v4-pro"
      - "Second task depends on first (depends_on: 0)"
      - "Summary mentions real model names deepseek-v4-flash and deepseek-v4-pro"
  - name: "three-model-chain"
    prompt: "用glm做计划，用deepseek-flash查资料，然后用deepseek-pro写代码"
    rubric:
      - "Three model assignments extracted"
      - "glm matched to glm-5.2"
      - "Task dependency chain preserved"
      - "Summary mentions real model names"
  - name: "parallel-tasks"
    prompt: "Use deepseek-flash to check the weather and use glm to analyze the stock market at the same time."
    rubric:
      - "Two model assignments extracted"
      - "Both tasks have depends_on: -1 (parallel)"
      - "Summary mentions real model names"
triggers:
  - "使用deepseek"
  - "用deepseek"
  - "使用glm"
  - "用glm"
  - "use deepseek"
  - "use glm"
  - "多模型分工"
  - "模型分工"
  - "指定模型"
  - "不同模型"
  - "分给不同模型"
  - "multi model task"
  - "model dispatch"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
metadata:
  opensquilla:
    risk: medium
    capabilities: [network]
composition:
  steps:
    # ── Step 1: Parse user intent into structured assignments ──────────
    - id: parse_intent
      label: "意图解析"
      label_en: "Intent parsing"
      kind: llm_chat
      with:
        system: |
          You are a multi-model task parser. Extract model-to-task assignments
          from the user's natural-language request.

          The user says things like:
          - "使用deepseek-flash查看文档中最欠缺的一个地方，然后使用deepseek-pro编写代码完善它。"
          - "Use deepseek to analyze the code, then use glm to write the report."
          - "用glm做计划，用deepseek-flash查资料，用deepseek-pro写代码"
          - "Use deepseek-flash to check the weather and use glm to analyze the stock market at the same time."

          Extract each (model, task) pair and their dependency chain.

          Output ONLY a JSON array (no markdown fences, no other text):
          [{"model_alias":"deepseek-flash","task":"查看文档中最欠缺的一个地方","depends_on":-1},{"model_alias":"deepseek-pro","task":"基于前一个任务的发现结果，编写代码完善文档中欠缺的地方","depends_on":0}]

          Rules:
          - model_alias: preserve the user's original model name spelling
            (e.g. "deepseek-flash", "deepseek-pro", "glm", "deepseek-v4-pro")
          - task: concise task description in the same language as the user
          - depends_on: 0-based index of a prior task whose output this task
            needs, or -1 if the task is independent (no dependency)
          - Sequential markers (then, 然后, 之后, 接着, after that, next)
            indicate depends_on the previous task
          - Parallel markers (at the same time, 同时, 并行, in parallel)
            indicate depends_on: -1 for all parallel tasks
          - If the same model is mentioned for multiple tasks, create separate
            entries in order
          - If no specific model is mentioned for a task, use "default"
          - Keep task descriptions concise but preserve the user's intent
          - If the request does not mention any model names at all, return:
            [{"error":"no_model_mentioned","message":"No model names found in the request. This meta-skill requires explicit model names."}]
        task: |
          Parse this request:
          {{ inputs.user_message | xml_escape | truncate(2000) }}

    # ── Step 2: Dispatch tasks to real models via worker skill ─────────
    - id: dispatch
      label: "任务分发"
      label_en: "Task dispatch"
      kind: agent
      skill: dispatch-worker
      depends_on: [parse_intent]
      progress_emits: true
      with:
        text: |
          Execute the following model-specific task assignments.

          Task Assignments (JSON from parse step):
          {{ outputs.parse_intent | truncate(8000) }}

          Original user request for context:
          {{ inputs.user_message | xml_escape | truncate(500) }}

          Key instructions:
          1. First, call gateway with action=config_get, key=squilla_router.tiers
             to read the real router configuration. Parse the JSON response;
             the "value" field contains a dict keyed by tier name (c0-c3),
             each with a "model" field — that is the real model ID.
          2. Match each model_alias to the real model IDs in the config.
             Use LLM-based semantic matching (not substring/regex):
             (a) Prefer the most specific match. If the user says
                 "deepseek-v4-flash", match exactly "deepseek-v4-flash", not
                 just any "flash" model.
             (b) If the alias is a series name without a version (e.g.
                 "deepseek", "deepseek-flash"), match the newest version
                 available in the config for that series.
             (c) If the alias only mentions a capability word (e.g.
                 "flash", "pro"), match the model in the config whose name
                 contains that word and is the latest version.
             (d) If the alias is a family name (e.g. "glm"), match the
                 latest model in that family.
             (e) If no match is found at all, mark it as unresolved and
                 let the router decide (omit the model parameter).
             (f) If multiple tiers share the same resolved model, pick the
                 lowest tier (c0 < c1 < c2 < c3).
          3. If the JSON contains an "error" key (no model mentioned),
             return the error message and stop — do not spawn any sessions.
          4. For each task in order:
             a. If depends_on >= 0, include the previous task's result
                (truncated to 2000 chars) in the new task's description.
             b. Call sessions_spawn(task=<full task description>,
                model=<resolved real model ID>).
             c. Parse the JSON response to extract session_key.
             d. Call sessions_yield(session_key=<key>, timeout_seconds=180).
             e. Call sessions_history(session_key=<key>, limit=20).
             f. Extract the last assistant message as the result.
          5. Return a DISPATCH_RESULTS summary with real model IDs, tier names,
             aliases, tasks, results, and statuses. Include the tier name
             alongside each real model for the summary.

    # ── Step 3: Synthesize user-facing summary ─────────────────────────
    - id: synthesize
      label: "总结"
      label_en: "Synthesis"
      kind: llm_chat
      depends_on: [dispatch]
      with:
        system: |
          You are a multi-model task summarizer. Given the dispatch results,
          produce a clear user-facing summary that explicitly mentions which
          REAL model completed which work. This is the most important
          requirement — the summary MUST use real model IDs, not user aliases.

          Use the same language as the user's original request. Format:

          ## 多模型任务执行总结 / Multi-Model Task Summary

          ### 任务 1: <task description>
          - 用户指定模型 / User specified: <alias>
          - 实际执行模型 / Real model: <real model ID>
          - 路由层 / Router tier: <tier name, e.g. c0>
          - 执行结果 / Result: <brief summary>
          - 状态 / Status: completed|failed|timeout

          ### 任务 2: <task description>
          ...

          ---
          **总结 / Summary**: 真实模型 <model_1> 完成了 <task_1> 的工作；
          真实模型 <model_2> 完成了 <task_2> 的工作。
          (If tasks have dependencies, explain how results flowed between them.
           If any task failed, explain the failure and its impact on downstream tasks.)

          Rules:
          - Always use the REAL model ID (e.g. "deepseek-v4-flash"), never
            the alias alone — this is the core requirement
          - Mention each model's contribution explicitly in the summary line
            at the bottom: "真实模型 <X> 完成了 <Y> 的工作"
          - If a task failed or timed out, note the failure reason and
            explain how it affected dependent tasks
          - If the dispatch returned an error (e.g. no model mentioned),
            explain the issue and suggest the user rephrase with model names
          - Keep it concise but complete
          - Match the user's language (Chinese or English)
          - If the dispatch used a fallback (gateway unavailable), mention
            that the model mapping was based on a fallback table
        task: |
          User request:
          {{ inputs.user_message | xml_escape | truncate(500) }}

          Dispatch results:
          {{ outputs.dispatch | truncate(8000) }}
---
