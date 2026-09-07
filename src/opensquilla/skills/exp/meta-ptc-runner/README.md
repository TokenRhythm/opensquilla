# meta-ptc-runner — PTC 调度 Meta Skill 设计说明

本目录实现目标 #2/#3：把 deepseek-harness 的 PTC（Programmatic Tool
Calling，即 Code Mode / `run_code`）思维抽象为 OpenSquilla 的一个
Meta skill + 一个工具调用，并复用 OpenSquilla 既有的智能路由实现省钱调度。

## 1. deepseek-harness PTC 的映射

参考实现（克隆于 `deepseek-harness/packages/core/tools`）：

- `run_code` 传输（`src/code-mode.ts`）：模型写一段程序，程序通过生成的
  SDK（`tools:sdk`）以 `await tools.<name>(args)` 调用可见工具；每个绑定
  调用都重新进入完整工具管线（pre-execute → guards → execute →
  post-execute → result）；**只有外层程序的 print 与 return 值回到模型上下文**；
  中间结果以 `tool/code-dispatch` 事件留痕；并发安全调用可重叠
  （`maxParallelSubCalls`），独占调用作为顺序屏障。

| deepseek-harness 概念                    | OpenSquilla 落点                                            |
| ---------------------------------------- | ----------------------------------------------------------- |
| `run_code` 工具 / Code Mode 传输         | 新内置工具 `src/opensquilla/tools/builtin/ptc_run.py`        |
| 程序内 `await tools.<name>(args)`        | `_ToolsNamespace`：`__getattr__`/`__getitem__` 返回 async partial |
| 每次绑定调用回到完整工具管线             | `build_tool_handler(get_default_registry(), ctx)` 子派发     |
| 仅外层 print/返回值进入模型              | 返回 `{"status","logs","result","elapsed_ms"}` JSON envelope |
| 失败绑定抛出 `ToolCallError(name,msg)`   | `PTCError(tool_name, message)`（try/except 继续）            |
| 只读调用可并发（gather）                  | 原样支持（子派发各自走完整策略链）                            |
| 调用顺序/独占屏障                        | 由程序自身的 `await` 顺序决定（MVP 串行为主）                |
| SDK 指令块（tools:sdk）                  | meta-skill 的 `write_program` 步骤内嵌 SDK 契约              |

本 MetaSkill 直接复用当前项目已经注册的工具，不实现重复的文件或网络适配层：

| 用途 | 复用工具 |
| --- | --- |
| PTC 执行 | `ptc_run` |
| 本地读取与检索 | `read_file`, `glob_search`, `grep_search`, `list_dir` |
| 本地写入 | `write_file`, `edit_file` |
| Web 只读 | `web_discover`, `web_search`, `web_fetch` |
| 本地计算 | `execute_code` |
| Git 只读 | `git_status`, `git_diff`, `git_log` |

这些工具同时写入 `metadata.opensquilla.requires_tools` 和 `run_program.allowed_tools`；
缺少依赖时技能会被依赖门控过滤，而不会运行到中途才因未知工具失败。
`write_file`、`edit_file` 不属于 `ptc_run` 的默认工具集合，仅由该显式 MetaSkill 授权，
并继续经过普通文件策略、审批和写入追踪。

没有加入 `apply_patch`、`exec_command`、`git_commit`、会话工具或控制面工具：现有读写工具
已经覆盖批量 Markdown 汇总和常规 PTC 聚合，额外能力只会扩大程序权限与失败面。

## 2. 显式 MetaSkill 入口与调度

唯一入口是：

```text
/meta meta-ptc-runner -- <request>
```

该命令直接复用 OpenSquilla 既有的 `meta.run(name="meta-ptc-runner")` 启动链，
不依赖 `meta_skill.auto_trigger`，也不新增 slash command 或复制 MetaSkill 的
readiness、幂等、恢复和会话绑定逻辑。

```
user /meta meta-ptc-runner -- <request>
    → normal Squilla request routing
    → collect_spec (llm_chat)
    → write_program (llm_chat)
    → run_program (tool_call: ptc_run)
    → final_report (llm_chat)
```

- 本技能没有关键词触发器，普通对话中的「遍历、采样、城市、省钱」不会启动它。
- 标准模式：模型每步「调一次工具→看一次结果→再决定」，长任务 N 步 = N 次模型往返。
- PTC 模式：程序内部仍可能调用 N 次工具，但中间结果不逐次进入模型上下文；模型负责
  规格、程序设计和最终报告，机械工具循环由 `ptc_run` 完成。

## 3. 请求级智能路由

显式 MetaSkill 请求继续使用 OpenSquilla 原有 Squilla Router：用户可以在任务自然语言中
表达质量、速度或成本偏好，路由器按正常请求策略选择 tier。本技能不调用
`router_control`，也不设置会话级 hold。

- **程序设计**：使用本次 MetaSkill 请求被路由到的模型。
- **执行与收集**：在 `ptc_run` 内部完成，不产生逐工具调用的 LLM 往返。
- **最终报告**：Meta `llm_chat` 当前复用父 Agent 的 provider/model，因此与程序设计
  使用同一个请求模型。
- **诚实边界**：OpenSquilla 当前不支持 Meta DAG 的步骤级 tier，不能声称
  `write_program@c2`、`final_report@c0`。若未来加入 per-step model routing，再扩展此技能。

## 4. 文件清单

- `src/opensquilla/tools/builtin/ptc_run.py` — PTC 执行工具（新增，已注册到 builtin）。
- `src/opensquilla/tools/builtin/__init__.py` — `_NAMES` 增加 `"ptc_run"`。
- `src/opensquilla/skills/exp/meta-ptc-runner/SKILL.md` — 本 Meta skill。
- `src/opensquilla/skills/exp/meta-ptc-runner/README.md` — 本设计说明。

> 当前实现已通过模块编译、工具注册和 Meta DAG 解析；显式入口完全复用现有
> `/meta` 命令测试。真实 provider 的端到端结果仍取决于目标机器的模型、路由和工具配置。
