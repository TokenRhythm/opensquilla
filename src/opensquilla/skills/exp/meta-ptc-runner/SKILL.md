---
name: meta-ptc-runner
description: "Explicit MetaSkill for Programmatic Tool Calling, invoked only with `/meta meta-ptc-runner -- <request>`. The normal Squilla Router selects the model tier for the request, that model writes one deterministic async Python orchestration program, and ptc_run performs repeated tool work without model round trips before the same request produces a curated report. Use it for batch, fan-out, pagination, retry, filtering, deduplication, or aggregation workflows. It has no keyword auto-triggers."
description_zh: "用于 Programmatic Tool Calling 的显式 MetaSkill，仅通过 `/meta meta-ptc-runner -- <任务>` 调用。该请求先由 Squilla Router 按正常规则选择模型档位，所选模型编写一段确定性的异步 Python 调度程序，再由 ptc_run 在没有模型往返的情况下完成重复工具工作，最后在同一请求中生成经过筛选的报告。适用于批处理、扇出、分页、重试、过滤、去重和聚合工作流；不提供关键词自动触发。"
kind: meta
meta_priority: 40
always: false
final_text_mode: "step:final_report"
request_template:
  outcome: "One aggregated report produced by a single request-routed ptc_run program execution."
  outcome_zh: "由正常请求路由选择模型，并通过一次 ptc_run 程序执行产出的汇总报告。"
  fields:
    - name: job_goal
      label_zh: "要批量完成的目标"
      label_en: "Batch goal"
      required: true
    - name: items
      label_zh: "遍历/采样的对象（地区、URL、文件等）"
      label_en: "Items to iterate or sample"
      required: false
    - name: per_item_steps
      label_zh: "每个对象要执行的操作"
      label_en: "Steps per item"
      required: false
    - name: aggregation
      label_zh: "如何汇总结果"
      label_en: "How to aggregate"
      required: false
  assumptions:
    - "The job is decomposable into a deterministic program (tools only, no human-in-the-loop inside the loop)."
    - "Only the program's printed lines and return value re-enter the model context."
assumptions_zh:
  - "任务可分解为确定性程序（仅调用工具，循环内不依赖人工介入）。"
  - "只有程序的输出与返回值会回到模型上下文。"
output_contract:
  append_to_final_text: false
  required_sections:
    - "Job spec recap (items x per-item steps x aggregation)"
    - "Run outcome (status, elapsed, logs/error when relevant)"
    - "Aggregated results"
    - "Written summary path when the job requests a file output"
    - "Request-scoped routing note"
  unverified:
    - "Per-item live results when the run was blocked by policy."
eval_prompts:
  - name: "meta-ptc-runner-baseline"
    prompt: "/meta meta-ptc-runner -- 检查当前仓库中所有 package.json，汇总同一依赖出现的不同版本，只返回存在版本冲突的依赖。"
    rubric:
      - "Job spec recap"
      - "Single ptc_run execution"
      - "Aggregated results"
      - "Normal Squilla request routing remains authoritative"
policy_tags:
  - ptc-programmatic-tool-calling
  - request-scoped-routing
provenance:
  origin: opensquilla-original
  license: Apache-2.0
  maintained_by: OpenSquilla
metadata:
  opensquilla:
    risk: medium
    capabilities: [filesystem-read, filesystem-write, network-read, command-exec]
    requires_tools:
      - ptc_run
      - read_file
      - write_file
      - edit_file
      - glob_search
      - grep_search
      - list_dir
      - web_discover
      - web_search
      - web_fetch
      - execute_code
      - git_status
      - git_diff
      - git_log
composition:
  steps:
    - id: collect_spec
      label: "作业规格采集"
      label_en: "Job spec"
      kind: llm_chat
      with:
        system: "You extract a structured PTC (Programmatic Tool Calling) job spec. Do not run tools."
        task: |
          Extract a structured job spec from the original user request. This
          job will be executed as ONE async Python program that calls tools
          in-process and runs end-to-end.

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(1400) }}

          Return exactly:
          JOB_GOAL: <one sentence describing the batch outcome>
          ITEMS: <the objects to iterate/sample, e.g. regions, URLs, files, or 'infer from the request'>
          PER_ITEM_STEPS: <the tool operations to run per item>
          AGGREGATION: <how to combine per-item results into the final answer>
          ALLOWED_TOOLS: <comma-separated tool names the program may use — choose only from: read_file, write_file, edit_file, glob_search, grep_search, list_dir, web_discover, web_search, web_fetch, execute_code, git_status, git_diff, git_log>
          NEEDS_CLARIFICATION: <yes|no — yes only when items or per-item steps are genuinely missing and cannot be conservatively inferred>
          CLARIFY_REASON: <one concise reason, or none>
    - id: spec_clarify
      label: "规格澄清"
      label_en: "Spec clarification"
      kind: user_input
      depends_on: [collect_spec]
      when: "'needs_clarification: yes' in (outputs.collect_spec | lower)"
      clarify:
        mode: form
        intro: |
          PTC 作业的关键输入还不完整。请补齐遍历对象和每个对象要执行的操作；如果已有信息不变，可以重复填写。
        nl_extract: true
        fields:
          - name: items
            type: string
            required: true
            prompt: "遍历/采样对象（地区、URL、文件等）/ Items to iterate"
            max_chars: 300
          - name: per_item_steps
            type: string
            required: true
            prompt: "每个对象要执行的操作 / Steps per item"
            max_chars: 400
          - name: aggregation
            type: string
            prompt: "如何汇总 / Aggregation"
            max_chars: 300
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24
    - id: write_program
      label: "编写 PTC 程序"
      label_en: "Write PTC program"
      kind: llm_chat
      depends_on: [collect_spec, spec_clarify]
      with:
        system: "You write async Python PTC programs against the ptc_run tools SDK. Return ONLY a cleanly indented function body: no markdown fence, no def line, no commentary, no surrounding text."
        task: |
          Write the BODY of an async function that takes one argument `tools`
          and runs the batch job end-to-end.

          JOB SPEC:
          {{ outputs.collect_spec | truncate(1400) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('spec_clarify', {}) | tojson }}

          OUTPUT FORMAT (failure-proof — the #1 cause of failure is broken
          indentation that makes the program fail to compile):
          - Return ONLY the function body. No markdown fence, no
            `async def main(tools):` line, no commentary, no text before or
            after the code.
          - Put every `import ...` statement at the very TOP of the body, at
            the SAME indentation level as the rest of the code. Never mix a
            column-0 line with indented body lines.
          - Keep every line at one consistent indent. The body is wrapped as
            `async def main(tools):` with 4 spaces added automatically — you
            only write the statements themselves.

          SDK CONTRACT (must be followed exactly):
          - The body runs inside `async def main(tools):` — do not write the
            def line, only its indented body.
          - Call tools as `await tools.<name>(**args)` (e.g.
            `await tools.web_search(query=..., max_results=5)`). Only tools
            listed in ALLOWED_TOOLS exist.
          - For requested Markdown output, aggregate first and call
            `await tools.write_file(path=..., content=...)` exactly once. If
            the destination already exists, read it fully first so normal
            workspace write-safety checks can validate the overwrite.
          - A failed call raises `ToolCallError` with `.tool_name` and
            `.message` — wrap per-item work in try/except and record the
            error into that item's row so one failure does not abort the
            whole batch.
          - Each call returns the tool's canonical value: JSON-decoded when
            the tool returns JSON, otherwise a string.
          - Independent read-only calls MAY run concurrently with
            `asyncio.gather(...)`; sequence dependent work with `await`.
          - Loop over every item (for example
            `for region in regions: for sample in range(10):`), collect
            results into a list, then aggregate them (counts, averages,
            tables, top findings).
          - Emit the final answer with `return <value>` (a JSON-friendly
            dict/list/str). Use `print(...)` for progress lines only.
          - ONLY what you print or return comes back to the model —
            intermediate tool results never enter the conversation, so
            extract exactly what the aggregation needs.

          FORBIDDEN (keeps policy/sandbox/approval in effect):
          - Do NOT touch the filesystem or network with raw Python such as
            `import os`, `import pathlib`, `open(...)`, `shutil`,
            `subprocess`, or `socket`. Every file/web operation MUST go
            through `await tools.<name>(...)` so the normal tool policy still
            applies. Enumerate with `await tools.glob_search(...)` or
            `await tools.list_dir(...)`, and read with
            `await tools.read_file(path=...)`.

          PRELOADED (already in scope — no import needed; if you do import,
          put it at the very top of the body at the same indent):
          `json`, `re`, `collections`, `math`, `statistics`, `itertools`,
          `datetime`, `textwrap`, `functools`, `time`, `asyncio`,
          `ToolCallError`, `tools`.

          EXAMPLE SHAPE (fenced here for readability only — your output must
          have NO fence; adapt, do not copy verbatim):
          ```python
          regions = ["beijing", "shanghai", "shenzhen", "hangzhou",
                     "chengdu", "wuhan", "xian", "nanjing"]
          rows = []
          for region in regions:
              for sample in range(10):
                  try:
                      out = await tools.web_search(
                          query=f"{region} 新能源 新闻 第{sample + 1}页",
                          max_results=5,
                      )
                      rows.append({"region": region, "sample": sample, "out": out})
                  except ToolCallError as exc:
                      rows.append({"region": region, "sample": sample, "error": exc.message})
          # aggregate
          per_region = {}
          for row in rows:
              per_region.setdefault(row["region"], []).append(row)
          return {"total": len(rows), "per_region_counts": {
              r: len(v) for r, v in per_region.items()
          }, "rows": rows}
          ```
    - id: run_program
      label: "执行 PTC 程序"
      label_en: "Run PTC program"
      kind: tool_call
      depends_on: [write_program]
      tool: ptc_run
      tool_args:
        code: "{{ outputs.write_program }}"
        description: "PTC batch run from meta-ptc-runner"
        timeout: 300
        allowed_tools:
          - read_file
          - write_file
          - edit_file
          - glob_search
          - grep_search
          - list_dir
          - web_discover
          - web_search
          - web_fetch
          - execute_code
          - git_status
          - git_diff
          - git_log
    - id: final_report
      label: "汇总报告"
      label_en: "Aggregated report"
      kind: llm_chat
      depends_on: [run_program]
      with:
        system: "You turn a ptc_run JSON envelope into a clear aggregated report. Do not invent results the envelope does not contain."
        task: |
          Write the final report from the PTC run envelope below. Keep it
          compact and factual.

          PTC RUN ENVELOPE:
          {{ outputs.run_program | truncate(20000) }}

          Required sections:
          1. Job recap — the items, per-item steps, and aggregation from the
             spec ({{ outputs.collect_spec | truncate(900) }}).
          2. Run outcome — status (ok/error/blocked), elapsed_ms, and the
             error/log excerpt when the run did not succeed.
          3. Aggregated results — render the run's `result` as tables/lists;
             when `result` is a JSON object, present its key fields. When the
             program recorded per-item errors, summarize them (how many, which
             items) without dumping raw tool output.
          4. Routing note — state that Squilla Router selected the model for
             this explicit MetaSkill request under the normal request-level
             policy; the nested tool loop itself used no per-call LLM round
             trips.

          If status is "blocked", explain the policy reason and how the user
          could adjust the request. If status is "error", restate the error
          and propose a corrected program as a follow-up.
---

# Programmatic Tool Calling Runner (Meta-Skill)

把连续、重复、大批量的工具操作组织成**一段可执行的小程序**：
采集作业规格 → 编写 PTC 程序 → 通过 `ptc_run` 执行（程序内部自行循环调用工具并聚合）
→ 汇总报告。它减少的不是嵌套工具调用次数，而是「调用一次工具、把结果送回模型、
再决定下一步」的大量模型往返；任务步骤越长、重复越多、中间数据越大，节省越明显。

## 选型边界

- **用本技能**：长串、重复的工具调用循环（批量采样、分页、扇出、重试、去重、聚合）。
- **不用本技能**：一次性本地计算或单次文件操作（扫描目录统计字数、读写单个文件等）。
  这类任务直接调用 `execute_code` 一步完成——让模型「先写程序再交给 ptc_run 执行」
  反而多两层出错机会（代码生成格式损坏 + 编译失败）。

## PTC 程序 SDK（与 ptc_run 工具契约一致）

- `code` 是接收一个参数 `tools` 的 async 函数**函数体**。
- 以 `await tools.<name>(**args)` 调用工具（特殊名字用 `tools["<name>"](**args)`）。
- 每次调用解析为该工具的标准值：工具返回 JSON 时已解码，否则为原始字符串。
- 失败调用抛出 `ToolCallError`（含 `tool_name` 与 `message`）——用 try/except 捕获并继续。
- 相互独立的只读调用可用 `asyncio.gather` 并发；有依赖的按 `await` 顺序执行。
- 以 `return <value>` 和/或 `print(...)` 输出结果；**只有打印的内容与返回值会回到模型上下文**，
  中间的工具结果不会进入对话，因此要在程序内完成聚合与提炼。

## 显式 MetaSkill 入口与请求级智能路由

- 唯一入口是 `/meta meta-ptc-runner -- <任务>`；不依赖关键词触发，也不要求
  `meta_skill.auto_trigger = true`。
- 当前 MetaSkill 请求仍由 Squilla Router 按用户自然语言、任务复杂度和现有会话策略
  选择 tier。
- 选中的模型负责规格提取、程序设计和最终报告；程序内部的数据收集、重试、过滤、
  去重与聚合由 `ptc_run` 执行，不产生逐工具调用的 LLM 往返。
- 本技能不调用 `router_control`、不设置持久 hold，也不改变后续会话路由。
- OpenSquilla 当前的 Meta `llm_chat` 不支持 DAG 步骤级 tier；不得声称同一次运行中的
  `write_program` 与 `final_report` 使用不同模型。

## Fallback

手工执行：按上述 SDK 编写一个 async 程序体，调用 `ptc_run`（`code` 传程序体、
`description` 传一句话说明、`timeout` 传时限），把返回的 JSON envelope
（`status` / `logs` / `result`）整理成汇总报告；若 `status` 为 `error`/`blocked`，
根据 `error` 修正程序或调整请求后重试。若任务循环内需要人工确认，改用普通工具循环逐
步执行，不要使用本技能。
