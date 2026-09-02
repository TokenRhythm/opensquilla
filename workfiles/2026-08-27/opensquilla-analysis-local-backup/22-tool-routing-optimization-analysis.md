# 22 · 工具路由优化分析：现状、缺口与优化路线

> 分析日期：2026-08-26 | 基线：main @ v0.5.4（`a0bbe0235` 系，21 号基线）
> 方法：档案 11/19/20/21 号交叉引用 + 本日源码定向取证（标注 repo_verified 者均为本轮 文件:行号 亲证）
> 定位：回答"统一路由路线中，工具面（tool routing）需要哪些优化"。前置阅读：21 号（最新勘误层）。
> 本文与 19/20 号（模型/执行路由内核）互补：那两篇管"用哪个脑子、怎么执行"，这篇管"暴露哪些工具"。

---

## 0. 结论速览

| # | 判定 | 置信度 |
|---|------|--------|
| 1 | 引擎**已有六层工具路由**（会话类型/声明式策略/per-job/模式/模型来源/条件暴露），工具面不是空白——此前"SPOT 零起步"的评估实际漏判了既有机制 | high（本日全锚点亲证） |
| 2 | 真实缺口三条：① 运行配置未启用 `[tools]` 策略（主会话全量暴露 60 工具，本会话亲测）② 模型路由与工具路由是两套平行系统，无共享意图分类 ③ 工具面每发布净增约 5 个（74→89）且无 per-turn 工具面 token 实测账 | high |
| 3 | 零代码优化已存在且立即可用：`[tools]` profile + `group:` 选择器、cron per-job `toolPolicy`（引擎支持完备，我们未用） | high |
| 4 | SPOT 应收敛为"**分类审计工作**"（89 工具 → core/low_freq/deny 三档），执行层直接复用 policy_config，不建新机制 | high |
| 5 | 与模型/上下文路由统一的落点 = 共享意图表（"各路由先做后统一"的第一步）；执行形态归属参照 21 号 §9.3（routing_strategy 三值 + c3 子模式分层） | medium（依赖 P1 数据） |

---

## 1. 现状盘点：六层工具路由（全量源码实证）

**架构要点**：`tools/dispatch.py` **无路由表**（名字→处理器在各 `@tool` 注册内，21 号 N2 已证）。"路由"全部发生在**策略层（谁能看到哪些工具）**，而非分发层（工具如何被调用）。分发层是纯查表——这是好消息：所有优化空间在策略层，**无需触碰 dispatch.py**。

| 层 | 机制 | 锚点（repo_verified 2026-08-26） | 状态 |
|----|------|--------------------------------|------|
| L0 会话类型 | cron 会话 `available_tools=CRON_AGENT_ALLOW \| CRON_AGENT_DENY` | `gateway/routing.py:770` | 引擎强制，生效 |
| L0' 子代理 | `SUBAGENT_TOOL_DENY` 过滤子会话工具定义；spawn 走 `child_tool_definitions` | `engine/agent.py:24900`、`:24754-24763` | 引擎强制，生效 |
| L1 声明式策略 | `ToolsConfig.profile` + `group:`/`channel:` 选择器 + allow/also_allow/deny + fnmatch 通配 | `gateway/config.py:282`；`tools/policy_config.py:11-114`（选择器表）、`:253-268`（expand_selectors） | **实现完备，运行配置未启用**（见 §2） |
| L1' 策略分层 | base → agent → channel → sender 四层叠加 | `tools/policy_helpers.py:43-115`（apply_base_policy/apply_channel_layer/apply_sender_layer 调用链） | 实现完备，未使用 |
| L2 per-job | cron 任务 `tool_policy` 持久化（`tool_policy_json` 列）→ 执行时 `apply_tool_policy_layer` | `scheduler/persistence.py:89`；`gateway/routing.py:309-311`、`:767-769`；RPC 面 `gateway/rpc_cron.py:285-292` | 实现完备，我们的 cron 未配置 |
| L3 模式 | `restricted_tool_boundary`（PromptAnnotation 回合工具面收窄） | `engine/runtime.py:8488-8509` | 引擎内部使用 |
| L4 模型来源 | `supports_tools` 来源语义：**仅显式 False 才拒绝**（N6 反转，21 号 §4-N6） | `model_catalog.py:607` 区段（21 号亲读） | 生效中 |
| L5 条件暴露 | document_browser 五工具 owner_only、默认不暴露；artifact 候选环工具集 | `tools/builtin/document_browser.py`（21 号 N2 亲读） | 生效中 |

**选择器表已内置的工具分组**（policy_config.py:11-114，可直接用，无需自定义）：

```
group:runtime  = exec_command, background_process
group:fs       = read_file, write_file, edit_file, apply_patch, list_dir, glob_search, grep_search
group:sessions = sessions_*, session_status
group:memory   = memory_search, memory_get
group:web      = web_search, web_discover, web_fetch, http_request
group:messaging= message
group:trusted_host = install_skill_deps, skill_install_community, skill_create/edit/delete
channel:chat/media/doc/wiki/drive = 各通道场景预设
```

另有 `_REPO_CODING_SOURCE_EDIT_TOOLS` / `_STRICT_TOOLS` 两组编码模式专用集合（:101-120），runtime.py:7971 经 `coding_mode_denied_tools` 消费——编码模式已有独立工具面语义。

**工具计数口径**：21 号 §9.4 序列 74/88/84/89（@tool 注册口径，89 为 v0.5.4 实测）；本日原始 grep：src 全域 97 命中（含 registry.py 装饰器定义 1 处 + 文档字符串误匹配），tools/ 目录内 90。**采用 89 为基准值**，97 为原始上限。

---

## 2. 问题量化：全量暴露到底多贵

**本会话实测数据点**（2026-08-26 主会话，全量注入）：

| 项 | 值 | 口径 |
|----|----|------|
| 系统提示暴露工具数 | **60** | 本会话系统提示工具清单逐条计数（亲测） |
| 注册工具总数 | 89 | 21 号 §9.4 |
| 未暴露占比 | 29/89 ≈ 33% | 被 L0/L3/L5 各层滤掉的（document_browser 5、部分 admin/meta 等） |
| 工具 schema 块 token | ≈10K±2K | 估算值——**待 P1-4 遥测替代** |
| 技能清单 | 66 个 skill 名 | 系统提示 available_skills 计数（每条仅名称+描述，成本较低） |

**成本结构判断**（接 11 号数据）：
- 11 号实测 flash 缓存命中 92% → 工具 schema 在缓存命中轮的**边际现金成本**很低（缓存读价约 0.02¥/M）；
- 但 **窗口占用是永久的**：10K token 的 schema 块每轮挤占路由窗口，直接推高压缩触发频率与水位线压力（21 号 N1 的 waterline 双件套正是为治长会话膨胀而生——工具面是其中稳定的大项）；
- **选择精度退化**：工具面越大，模型选错工具/不选工具的倾向越高（行业共识，无本机实证，标注为推断）；
- **增长趋势**：74→89 四轮净增 15 个，约每发布周期 +3~5 → 全量暴露成本是**单调上升**的，不治理就持续劣化。

**关键发现：引擎已有工具 schema 遥测，未聚合。**
- `_record_provider_tool_schema_event`（engine/agent.py:4798，`feature="provider_tool_schema"` 事件，:4836 发射）——每次 provider 请求的工具 schema 都有事件记录；
- `observability/prompt_report.py:67` `_tool_schema_payload`——schema 序列化口径已存在。

→ "全量暴露 = 10K token" 目前是**估算**，而测量所需的管道已经埋好。这是 P1 的第一优先级依据。

**运行配置现状**（本日 spot check，C:\Users\chine\.opensquilla\config.toml）：`[tools]` 策略段**未见配置**（按 section 扫描未命中 profile/deny/allow 行）。即：L1 声明式策略层处于**完全闲置**状态，主会话走默认全量面。此为推断性结论（基于扫描而非逐行通读，置信 high 非 100%）。

---

## 3. 优化路线（按成本分层）

### P0 · 零代码，立即可用（配置层，引擎现成能力）

**P0-1 运行配置启用 `[tools]` 策略（按场景）**
- 主私有会话：**保持全量**。定位是全能工作室，全量面是设计意图，不是缺陷。
- 真正的受益场景：① 飞书群聊/共享上下文（当前 L1' 的 channel 层未配置）② 未来新增的受限 agent。
- 用法：`[tools]` 下配 `profile` 或 allow/also_allow/deny，直接引用 §1 内置 `group:` 选择器。改动 = 配置几行，重启生效。
- 风险：极低（策略层 fail 时默认行为不变；建议先用 also_allow 增量验证，再收紧 deny）。

**P0-2 cron 任务补 per-job `toolPolicy`**
- 我们的三段式复习任务：引擎已支持（L2 全链亲证），当前未配置。
- 示例形态：deny `group:messaging`、`group:sessions`（复习任务不需要发消息/跨会话）。
- 红线前置：改任何 cron 前，确认任务文本自述所需工具（`tools/triple_session_protocol.md` 自包含，风险低）。

### P1 · 小代码（本地补丁，锚点 ≤2 处）

**P1-4（建议先行）工具面遥测聚合**
- 聚合 `provider_tool_schema` 观测事件 → 每日产出"暴露工具数 + schema 字符数/token 估算"。
- 规模：独立脚本约 20-40 行，数据源为现有观测流，零引擎改动。
- **意义：把 §2 的估算变成实测，为 P0-1 收紧、SPOT 审计、P2 内核化提供共同度量基线。** 没有这个数，后面所有优化都是拍脑袋。

**P1-3 意图表统一：上下文路由与工具策略共享一张表**
- 8/21 定案的 `workspace.py` route_keywords 设计（上下文文件路由）与 P0-1 的工具 profile 选择，消费同一数据：
  ```
  { domain: { keywords: [...], context_files: [...], tool_profile: {...} } }
  ```
- 这是"统一路由"的**最小实体**：不写新框架，只是让两个既有消费者读同一份声明数据。
- 上游对齐参照：21 号 §9.3（保持 routing_strategy 三值、c3 下子模式分层 = 零上游改动路线）。

### P2 · 中代码（按 19/20 号路由内核路线）

**P2-5 L1 路由内核化（execution_mode 总线）**
- 20 号锚点预算（晚钩子 ~10-15 行 + `engine/exec_modes/` 新目录 ~160 行 + 测试），按 21 号 §9.1 行号修正（晚钩子位 `runtime.py:9761` 终收口前）。
- 工具路由内核化后成为决策总线的一个消费者：`RoutingDecision → (model, execution_mode, tool_surface)`。

**P2-6 能力签名注册表（统一路由的前提）**
- AGENTOS 设想改造中已有 yaml schema 草案（`kind: tool | skill | mcp_server | auxiliary_model`）。
- 首版数据可从 P1-3 的统一表自动生成（工具域先行），不追求一步到位六面覆盖。
- 前置条件（维持 8/21 判断）：各路由面先有数据可查——本文 P0/P1 就是工具面的数据准备。

### P3 · 上游贡献候选

**P3-7 工具面成本进 RoutePlan 观测**
- RoutePlan 当前无 tool_surface 字段；决策日志缺"本轮暴露了什么"维度。
- 小 PR（字段 + as_dict），上游友好度高，且与 21 号 §7-N3 的 RouterTierSnapshot 观测取向一致。
- 时机：随 P2-5 一并提，单独提显得动机单薄。

---

## 4. 风险与不做清单

| 项 | 说明 |
|----|------|
| N6 语义反转红线 | 自写工具准入逻辑时**只认显式 supports_tools=False**，不用 provenance 做能力否决（21 号 D11：该语义变化无 changelog 记录，属上游隐性漂移，升级时须重查） |
| 不建 SPOT 式平行 deny 表 | policy_config 就是 deny 机制本身，再造一份 = 双份 Compaction 教训（19 号明确不做清单第 1 条） |
| 不触碰 dispatch.py | 分发层无路由表是正确架构，优化空间全在策略层 |
| 工具数增长不是 bug | 89→N 是产品演进健康信号；治理对象是"暴露策略"，不是"阻止注册" |
| 估算不作决策依据 | §2 的 10K token 是估算——P1-4 上线前，任何基于 token 数的收紧决策都不启动 |

---

## 5. 与 AGENTOS 路线的对齐

| AGENTOS 既有概念 | 本文对应 | 状态 |
|------------------|----------|------|
| SPOT（core/low_freq/deny 三档工具审计） | 执行层 = L1 policy_config（现成）；缺的是**审计工作本身** | 待启动（建议 1 天工时：89 工具逐个分档） |
| SPOT-E（config + 热生效） | `[tools]` profile 即此形态（gateway 重启生效） | 零开发 |
| 能力签名统一路由 | P1-3（统一意图表）→ P2-6（注册表） | 设计定案，未动工 |
| 模型路由 + 工具路由 + 上下文路由统一 | 先各做（P0/P1），再统一（P2）——与 8/21 拍板的"先各路由后统一"一致 | — |
| 路由中枢（设想改造） | P2-5 execution_mode 总线（19/20 号方案，锚点预算已修订） | 待主人裁决参数（21 号 §9.3 两问） |

**SPOT 执行建议**：审计（89 工具分档）→ 写入 `[tools]` 配置 → P1-4 遥测跑一周 → 用实测数据决定收紧幅度。顺序不可颠倒——先有账，再动刀。

---

## 6. 待主人裁决（4 项，均可独立决策）

1. **P0-1**：飞书通道是否现在做 `[tools]` 策略实验（also_allow 增量验证）？还是先只留主会话全量不动？
2. **P0-2**：三段式 cron 任务是否补 `toolPolicy`（deny messaging/sessions）？
3. **P1-4**：是否先做工具面遥测脚本（1 天内可交付，后续一切以实测为据）？
4. **SPOT 审计**：89 工具分档是否启动（1 天工时，产出 = `[tools]` 配置草案，不直接生效）？

---

## 7. 证据附录

**本日亲证锚点（2026-08-26，main @ v0.5.4）**：

| 证据 | 位置 |
|------|------|
| cron 工具面强制 | `gateway/routing.py:770`（CRON_AGENT_ALLOW \| CRON_AGENT_DENY） |
| cron toolPolicy 执行链 | `gateway/routing.py:309-311`（metadata 透传）、`:767-769`（apply_tool_policy_layer）；`scheduler/persistence.py:89`（tool_policy_json 列）；`gateway/rpc_cron.py:268-292`（wire 归一化） |
| 子代理工具面 | `engine/agent.py:24900`（SUBAGENT_TOOL_DENY 过滤）、`:24754-24763`（child_tool_definitions） |
| 声明式策略层 | `tools/policy_config.py:11-114`（group 表）、`:253-268`（expand_selectors/fnmatch）、`:101-120`（编码模式工具组）；`tools/policy_helpers.py:43-115`（四层应用链）；`gateway/config.py:282`（ToolsConfig.profile） |
| restricted 模式 | `engine/runtime.py:8488-8509`（bootstrap_context_mode == "restricted_tool_boundary"） |
| 工具 schema 遥测 | `engine/agent.py:4798`（_record_provider_tool_schema_event）、`:9224`（发射点）、`observability/prompt_report.py:67`（_tool_schema_payload） |
| 编码模式工具面消费 | `engine/runtime.py:7971`（coding_mode_denied_tools import） |
| 工具计数 | 本日 git grep：src 97 / tools/ 90；基准值采 21 号 §9.4 的 89 |
| 运行配置 | C:\Users\chine\.opensquilla\config.toml section 扫描：未见 [tools] 策略段（推断性结论） |
| 本会话暴露面 | 系统提示工具清单 60 条 + skill 清单 66 条（本轮注入上下文亲数） |

**引用档案**：
- 11 号：SquillaRouter 7 闸门 / 成本数据（v0.5.3 基线；档位数字按 21 号 N7 勘误）
- 19 号：六真接缝 / L1-L3 改造分级（锚点按 21 号 §5 漂移表修正）
- 20 号：MVP 锚点预算（行号按 21 号 §9.1 修正：晚钩子位 runtime.py:9761）
- 21 号：N2（artifact 工具面）/ N6（supports_tools 反转）/ §9.3（统一路由机制位）/ §9.4（工具计数）

---

*取证方式：主代理本日定向 grep/read（policy 全链 / cron 全链 / 子代理链 / 遥测点 / 运行配置 / 本会话注入计数）。估算项已显式标注，未与实测混用。*
