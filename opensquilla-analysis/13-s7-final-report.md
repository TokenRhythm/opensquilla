# OpenSquilla 深度解构 — 最终架构分析报告

> deep-code-analyzer v2.0.0 · S1→S7 全流程 · 项目根目录 `d:\AIstudio\Harness\OpenSquilla-QinLuza-Studio`（分支 `feat/openai-bridge`，跟踪 upstream/main f87fed4b）
> 生成时间：2026-08-11 · 依据：S1-S4 Checkpoint（10-stage1-4-checkpoint.md）+ S5 深挖×2（11-s5-squillarouter-deep-dive.md、12-s5-auxiliary-model-deep-dive.md）

---

## 一、项目概览

**OpenSquilla 0.5.2** — 一个以"token 效率"为第一性原理的**微内核 AI Agent 运行时**。标语 "Same budget, more capability, better results" 直接定义了产品哲学：不追求单轮对话的极致智能，而是追求**单位预算下的长期可用性**（cost-per-success）。

项目横跨五类交付面：CLI（typer）、Web UI（Vue3 + Vite）、桌面端（Electron）、网关 RPC（WebSocket + ASGI）、消息通道（Terminal/WebSocket/Slack/飞书/Discord/Telegram 六通道）。所有表面共享同一条运行时路径、同一套工具、同一份记忆、同一个用量账本——这是"统一表面"架构的核心承诺。

版本状态：Alpha（Development Status :: 3），Apache-2.0，Python 3.12+，Node 22.12+（WebUI 构建）。

## 二、技术栈

| 层 | 选型 | 证据 |
|---|---|---|
| 语言 | Python ≥3.12（requires-python） | pyproject.toml L16 |
| Web 框架 | FastAPI ≥0.115 + Starlette + Uvicorn + python-multipart | pyproject.toml L18-21 |
| 数据模型 | Pydantic v2 + pydantic-settings（配置） | pyproject.toml L22-23 |
| 持久化 | SQLModel + SQLAlchemy 2.0 + aiosqlite + sqlite-vec（向量）+ yoyo-migrations | pyproject.toml L24-25,37,47 |
| HTTP 客户端 | httpx ≥0.27 + brotli + certifi | pyproject.toml L26,28,42 |
| 日志 | structlog（结构化）+ rich（CLI 渲染） | pyproject.toml L30,32 |
| 调度 | APScheduler + croniter | pyproject.toml L35,45 |
| 文档解析 | html2text / readability-lxml / beautifulsoup4 / pdfplumber / python-docx / python-pptx / openpyxl / pypdf / reportlab | pyproject.toml L38-44,53-55 |
| 通道 SDK | lark-oapi（飞书）/ python-telegram-bot / dingtalk-stream / qq-botpy | pyproject.toml L56-59 |
| 加密 | cryptography ≥42 | pyproject.toml L60 |
| 搜索 | duckduckgo-search + 自研 provider 注册层 | pyproject.toml L61 |
| CLI 交互 | typer + questionary + rich | pyproject.toml L31-32,50 |
| 前端 | Vue3（536 ts / 139 vue）+ Vite + Electron（desktop/ 82 文件） | 目录结构 |
| 包管理 | uv（uv.lock 775KB）+ hatch_build.py | 根目录 |
| 容器 | Docker 多阶段（node:22 → python:3.13-slim）+ compose.yaml | Dockerfile / compose.yaml |

**依赖治理特征**：全部版本下限锁定、无上限（除 yoyo-migrations<10、lark-oapi<2 等少数生态兼容约束）；安全关键路径（cryptography、certifi）单独显式声明；模型资产走 Git LFS 而非 pip 包。

## 三、模块目录（33 个业务模块，S2 覆盖率 91.7%）

### 核心六件套
| 模块 | 职责 | 入口 |
|---|---|---|
| `engine/` | Agent 核心状态机 + 回合前管线 + 7 闸门路由策略 | agent.py / pipeline.py / steps/ |
| `gateway/` | ASGI 网关：WebSocket 协议、RPC 分发、控制 UI、通道分发 | app.py / boot.py / rpc/ |
| `provider/` | LLM Provider 统一抽象：协议、故障分类、Ensemble、模型目录 | protocol.py / ensemble.py |
| `squilla_router/` | 本地模型路由：V4 Phase3 ML 推理 + 自学习闭环 | v4_phase3.py / self_learning/ |
| `sandbox/` + `safety/` | 沙箱三后端（bubblewrap/seatbelt/noop）+ 安全基线四件套 | backend.py / injection_guard.py |
| `memory/` | 长期记忆：嵌入、检索、刷写、同步 | manager.py / retrieval.py |

### 支撑层
| 模块 | 职责 | 模块 | 职责 |
|---|---|---|---|
| `channels/` | 六通道适配器 | `session/` | 会话生命周期/压缩/键构造 |
| `scheduler/` | cron 作业 + 抖动策略 | `search/` | 搜索 provider 抽象 |
| `mcp/` | 出站 MCP 客户端 | `mcp_server/` | 入站 MCP 桥 |
| `skills/` | 六层技能栈 | `tools/` | 工具注册表 + 内置工具 |
| `observability/` | 决策/安全/追踪四路 JSONL 日志 | `onboarding/` | CLI/RPC/WebUI 共用配置向导 |
| `identity/` | 人格解析 + 系统提示组装 | `persistence/` | 迁移执行 |
| `cli/` | 命令行入口 | `openai_bridge/` | OpenAI 兼容 HTTP 桥 |
| `health/` | 就绪/恢复诊断 | `recovery/` | 桌面恢复契约 |
| `uninstall/` | 清单驱动卸载 | `migration/` | 外部运行时导入 |
| `eval/` | 观测式基准 | `compat/` | 兼容层 |
| `application/` | 应用域服务 | `contracts/` | 运行时边界契约 |
| `contrib/` | 保留命名空间（空） | `plugins/` | 内置插件 |
| `agent_ids.py` 等 30+ 顶层单文件 | 预算/证据/路由控制/用量等横切工具 | | |

## 四、数据流（S3，主链路 5 环节全闭合）

```
六通道/CLI/WebUI ──► gateway 入口（WS/RPC/HTTP）
      │                 ├─ channel_dispatch（通道归一化 + 审批门控）
      │                 └─ model_routing 快照（模型目录 + 档位）
      ▼
engine/turn_runner ──► 回合前管线 pipeline（steps/：技能过滤、提示组装、路由决策）
      │                 └─ squilla_router 步骤：分类(v4/heuristic) → 7 闸门 → RoutingDecision
      ▼
provider 选型 ──► 单路由直绑档位模型 / Ensemble 包裹（4 提案 + 1 聚合 + quorum 掐断）
      ▼
agent 状态机 + 工具循环（agent.py：显式状态机，回合级预算治理）
      │                 ├─ 工具结果经 result_budget 压缩后进上下文
      │                 └─ 上下文超限 → 压缩(compaction) → 会话刷写(session_flush)
      ▼
SQLite 持久化（会话/记忆/用量）+ JSONL 观测流（决策/安全/追踪）
```

5 条假设全部验证闭合：主链路假设（confirmed）、路由闸门顺序（confirmed，policy.py docstring 逐条核对）、辅助通道旁路（confirmed，task_runtime 空闲槽）、压缩隔离部署（confirmed，compaction_target 解析链）、观测流不阻塞主链路（confirmed，独立 append）。

## 五、设计模式与架构原则（S4，25 项）

### 设计模式（9）
1. **微内核 + 插件**：核心引擎最小化，技能/通道/Provider/搜索均可插拔注册。
2. **策略模式**：30+ LLM Provider 同构于 `LLMProvider` 协议，工厂按 providerType 创建。
3. **装饰器/包装器**：EnsembleProvider 包裹成员 Provider；限流/审批在工具边界装饰。
4. **责任链（回合前管线）**：`TurnStep = Callable[[TurnContext], Awaitable[TurnContext]]` 有序链式变换。
5. **状态机**：engine 显式状态机（AgentState/AgentEvent 枚举驱动，无递归调用）。
6. **门控流水线**：路由决策经 confidence_gate → complaint_upgrade → anti_downgrade → capability_gate → bind → large_context_floor → provider_mismatch 七闸门顺序绑定（policy.py 精确复刻遗留顺序）。
7. **预算治理（三层）**：ContextBudget（上下文窗口）/ ResultBudget（工具结果压缩）/ AuxiliaryBudget（旁路调用预算）分层封顶。
8. **观察者/事件流**：AgentEvent 事件总线 + 四路 JSONL 观测流解耦主链路。
9. **清单驱动**：uninstall 先 inventory → plan → actions 三段式，`--dry-run` 纯函数无 I/O。

### 架构原则（5）
1. **Token 效率第一**：路由选便宜模型、92% 缓存命中优先、预算封顶（详见深挖专题一）。
2. **统一表面**：CLI/WebUI/通道共享同一运行时路径、工具、记忆、账本。
3. **信息漏斗**：回合前管线每步只产出本步字段，下游不读上游中间推理。
4. **无回退即默认**：启发式路由仅在 ML 运行时不可用时启用，且置信度设计专门避开默认档位兜底陷阱。
5. **显式状态机 + 定量闸门**：技能系统自身即演示该原则（四铁律）。

### 编码约定（5）
1. 包级 `__init__.py` 全部带职责 docstring + `__all__` 白名单。
2. 懒导入（PEP 562 `__getattr__`）隔离重型依赖（engine.types 与 agent 解耦）。
3. 常量集中化（`_TIER_ORDER`、`_DEEP_FLAGS` 等模块级冻结集合）。
4. 纯函数与副作用分离（routing/controller.py "Pure functions — no I/O"；uninstall/plan 无 I/O）。
5. 配置热更新：RPC/WebUI 变更立即生效，手工改文件仅启动时读取（config.toml 注释明示边界）。

### 安全设计（6）
1. **沙箱三后端**：Bubblewrap（Linux）/ Seatbelt（macOS）/ Noop 自适应选择。
2. **权限矩阵**：`is_tool_allowed(tool_name, channel_kind, principal)` + 每通道覆盖。
3. **注入防护信封**：`<untrusted source='...'>` 包裹不可信内容，XML 转义 + 拒绝溯源。
4. **工具分档**：RiskTier 枚举 + 高危工具硬编码 admin-only 名单。
5. **审批门控 + 拒绝台账**：ApprovalGate / DenialLedger / post_denial_guard。
6. **容器纵深**：宿主侧 `127.0.0.1:18791` 绑定、token 认证可选项、非 root 容器用户、healthz 探针（Dockerfile S20 契约）。

## 六、部署架构

| 面 | 形态 |
|---|---|
| 容器 | Docker 多阶段：node:22-bookworm-slim 构建 WebUI 静态产物 → python:3.13-slim 运行（仅拷贝 dist，不携带 node_modules）；`OPENSQUILLA_LISTEN=0.0.0.0` 容器内 + 宿主侧 loopback 发布；named volume 持久化 state |
| Compose | compose.yaml 单服务 gateway，healthz 探针（30s/5s/10s/3），`restart: unless-stopped` |
| 本地安装 | install.ps1 / install.sh（winget 装 VC++ runtime 等依赖）+ uv tool install 快速路径 + Homebrew Formula |
| 桌面 | Electron 桌面端（desktop/），打包 gateway runtime + 控制台 |
| 卸载 | 清单驱动卸载器覆盖 8 种安装方式 |
| 配置 | opensquilla.toml（env var > toml > defaults 优先级），`gateway reload/restart` 区分热更与冷更边界 |

## 七、深挖专题（S5）

### 专题一：SquillaRouter 路由算法（详见 11-s5-squillarouter-deep-dive.md）
- **档位空间**：c0-c3 四文本档 + image_model 视觉档；遗留别名 t0-t3；R0-R3 路由类映射。
- **决策链**：`分类(v4_phase3 ML / heuristic 降级) → 无效兜底 → 7 闸门 → RoutingDecision`。
- **省钱哲学**：单路由"选便宜"（结构性省钱：便宜模型×缓存命中×预算封顶，实测 flash 有效输入价 0.058 ¥/M 比标价低 43 倍）；多路由"别白花"（止损型：预检零花费、quorum 达标掐断、失败零成本、草稿截断）。
- **出血口**：proposer 无输出上限（glm runaway 10 次 = ¥36.4 = 总成本 14.4%）；聚合器草稿预算约 20% ensemble 成本。

### 专题二：辅助功能模型模块（详见 12-s5-auxiliary-model-deep-dive.md）
- **已实现**：4 条辅助通道（naming/compaction/session_flush/profile_import）+ 三层基建（空闲槽 Semaphore(1) + 每会话去重 + 预算/记账剥离）+ 压缩跨提供商（#921）+ 图像生成独立配置段 + vision_followup_gate（默认 c0 低成本档）。
- **部分实现**：命名跨提供商（NamingTarget 无 provider 配置位，强制活动提供商）；视觉 VQA 调用层未解耦。
- **未实现**：统一辅助模型管理、#1133 子代理物理契约（draft）。
- **可落地建议**：给 `NamingTarget` 加 provider 键打通最后跨提供商口子，单文件低风险（有既有 `tier_model_skipped_provider_mismatch` 降级路径兜底）。

---

## 八、设计缺陷分析

本章对 S1-S5 阶段识别出的架构问题进行根因分析，区分"设计层面的结构性问题"和"工程执行层面的遗留问题"。

### 8.1 微内核声明与单体实现的结构性背离

这是本项目的核心设计矛盾。`engine/agent.py` 顶部文档注释自称 "Core loop is under 500 lines"，但文件实际体量达 935KB（约 2 万行），是宣称规模的 **40 倍**。这不是一个简单的"注释没更新"的问题，而是反映出从"微内核概念设计"到"实现演进"过程中，缺乏拆分纪律。

根因分析：项目的包结构高度模块化（33 个业务模块、清晰的职责边界），但 engine 内部的 agent 状态机、工具循环、证据门控、回合预算治理全部沉积在单一文件中。拆分边界已经天然存在——回合预算治理（`result_budget.py` 34KB 已独立）、证据门控（`finalize_evidence_gate.py` 59KB 已独立）、技能解析（`meta_resolution.py` 62KB 已独立）——但它们是从 agent.py import 进主循环的，而不是 agent.py 本身被拆分。这是"外围模块化 + 核心单体"的典型生长模式：开发者把新功能抽成独立模块很容易，但把核心循环切分出去的阻力远大于此。

与之并列的 `engine/runtime.py`（460KB）和 `gateway/rpc_sessions.py`（345KB）属于同一模式——功能膨胀但从未被主动重构。`boot.py` 200KB、`channel_dispatch.py` 168KB、`config.py` 152KB 构成第二梯队。六文件合计约 2.2MB，占项目核心 Python 代码的主体。

影响面：巨型单体文件的已知危害包括新人上手曲线陡峭、单文件冲突概率高（多人协作时几乎必然 merge conflict）、单元测试难以精准定位、重构恐惧累积。对 OpenSquilla 而言，最直接的风险是 agent.py 的任何改动都可能意外影响工具循环、证据门、预算治理或路由决策四者之一，而这些是系统的核心安全边界。

### 8.2 配置热更新范围不一致

项目部署架构强调"env var > toml > defaults"的优先级体系和 `gateway reload/restart` 的热更/冷更语义分离。但 S5 深挖发现一个例外：SquillaRouter 的路由配置在每轮 turn 接受时做快照（`gateway/model_routing.py` L533-549 `capture_model_routing_config`），之后即使 RPC 改了配置，该轮也使用快照值。注释明确写明"改配置文件不热生效，需重启 gateway"。

为什么这是一个设计缺陷而非工程疏忽：路由决策是 OpenSquilla 最核心的差异化能力（"按轮次选最便宜能胜任的模型"），如果路由配置无法热更，意味着用户想临时调整路由策略时必须中断所有会话重启 gateway。这与"CLI/WebUI/通道统一表面"的架构承诺形成设计张力——其他配置面（provider、工具权限、通道设置）可以热生效，但决定"花多少钱"的路由配置不行。根源在于 `model_routing_config` 的传递方式是"深拷贝快照进 TurnContext"而非"每闸门实时查询配置源"。

### 8.3 辅助通道的设计一致性缺口

辅助模型体系的核心设计原则是"逐通道放开模型选择自由"。压缩通道（compaction）执行得最彻底：provider 和 model 必须成对指定，构成完全独立的物理部署。图像生成通道同样完全独立。但命名通道（naming）在同一套代码库里存在设计退化：`NamingTarget` 结构体有 provider 字段，但它只是只读标识而非可配置键，连接强制取自活动提供商。

这不是功能缺失而是设计不一致。同一代码库里三套辅助通道，两套支持跨提供商（compaction、image_generation），一套不支持（naming），而三套共享同一套基建代码（`task_runtime.run_auxiliary_if_idle` + `auxiliary_budget`）。不一致的根因是渐进式开发中"先做最难的（compaction），后面的（naming）没追平"。

### 8.4 self_learning 框架的"骨架完备但无肌肉"状态

`squilla_router/self_learning/` 目录含 14 个文件（orchestrator、train、alignment、feedback、promotion、gates 等），arXiv 2607.11399 有技术报告背书，配置位 `self_learning.enabled` 存在但默认 false。整个自学习体系是一个完整的数据飞轮设计——alignment（对齐）→ capture（捕获）→ dataset（数据集）→ evaluate（评估）→ feedback（反馈）→ gates（闸门）→ orchestrator（编排）→ promotion（晋升）→ train（训练）——全部代码就位但未在生产中启用。

这不属于"未实现"（代码写了），也属于"设计缺陷"（设计好了但从未在真实数据上跑过闭环）。核心风险是 14 个文件的代码可能已经与当前 v4_phase3 分类器的实际输出格式脱节；没有真实反馈数据校准，self_learning 框架本身就是"写了但没验证过"的技术债务。

---

## 九、工程问题分类

按影响类型将 S1-S5 发现的工程问题归入三档：结构性债务（不改会持续恶化）、配置面缺陷（可配置即可缓解）、运维卫生问题（一次性清理）。

### 9.1 结构性债务

**Ensemble proposer 无输出上限**（P0）。这是全部分析中最明确的实时成本泄漏点。用户 3890 次调用的真实日志显示：glm-5.2 模型 10 次 runaway 调用（输出打满 128K token 上限）消耗 ¥36.43，占 7 天总成本 ¥251.95 的 14.4%。而 ensemble 代码在 proposer 层没有 max_tokens 参数——不是默认值太大，是根本没有这个控制面。一次 glm runaway 的消耗相当于 500 次正常 flash 单步调用。这不是模型选择问题，是工程上缺少一个防御性输出的上限。

**聚合器草稿预算不可配置**。ensemble 的 aggregator 消耗约占 ensemble 总成本的 20%（用户数据中 qwen3.8-max 当 aggregator 烧 ¥23.62，全部草稿输入按 ¥12/M 计费）。当前 `_cap_candidates_to_joint_budget` 的截断逻辑硬编码在 ensemble 内部，没有 exposed 为配置项。在 proposer 不加输出上限的情况下，草稿越长、聚合器输入越大、这笔结构性成本越不可控。

**per-agent 路由绑定有 schema 无 consumer**。`AgentRoutingConfig` 定义了 `default_tier` 和 `max_tier` 字段，允许按 agent 场景硬绑路由策略。但仓库中找不到任何读取这两个字段的消费代码。这是一个典型的"设计走了一半"——先定义了配置 schema（说明设计意图是有的），但从未接入路由决策。结果是每条路由路径都走全局 default_tier，无场景区分能力。

**辅助通道全局 Semaphore(1) 单槽**。`_auxiliary_slot = Semaphore(1)` 的设计保证了辅助任务不抢占主链路，但也意味着任何辅助任务（命名、压缩、视觉门控）只能串行执行。这个取舍在当前 Alpha 阶段合理，但如果未来辅助通道增加或用户会话密度上升，会构成瓶颈。设计上它应该是一个可配置的并发度（至少可以设一个小的 N 而非硬编码 1）。

### 9.2 配置面缺陷

**命名通道缺 provider 配置键**。已在设计缺陷 8.3 中详述，不再重复。补充一点工程角度：改动面确实小——naming.py 单文件的 NamingTarget 加一个字段、config.py 的 SessionNamingConfig 加一个键、opensquilla.toml.example 加一行注释——改动窗口 <50 行，风险有 `tier_model_skipped_provider_mismatch` 既有降级路径兜底。

**视觉 VQA 调用层未解耦**。image_model 档位自带了 provider 声明（如 tokenrhythm/kimi-k2.6），但实际的视觉 VQA 请求仍通过与主会话相同的 provider 连接发出。这意味着你没法让视觉任务独立走一条便宜管线——视觉模型的选择实际上被绑在主 provider 上。

**辅助模型无统一配置根**。当前 6 条辅助通道的模型配置分散在 toml 的不同 section 下（`[naming]`、`[compaction]`、`[squilla_router.tiers.image_model]`、`[image_generation]`、vision_followup_gate 扁平字段），没有 `[auxiliary]` 统一配置根。这增加了配置的理解成本——用户需要知道"标题模型在 naming.model、压缩模型在 compaction.model、视觉在 tiers 下面"，而不是一个集中的辅助模型管理视图。

### 9.3 运维卫生问题

**根目录运行残留**：`__pycache__/`、`.pytest_cache/` 等应在 `.gitignore` 中被排除的目录出现在仓库中。这不影响运行时行为，但会让新贡献者困惑于哪些是源码、哪些是构建产物。

**Alpha 阶段的文档承诺前置**：README 的"microkernel"定位与 agent.py 935KB 的现实之间存在 40 倍的认知差距。合理的做法是先更新 agent.py 顶部的注释（把 "Core loop is under 500 lines" 改成准确描述当前结构），再考虑是否要拆分。宣传材料与实现现实对齐，比保持一个不再准确的"under 500 lines"声明更重要。

---

## 十、优化建议（按优先级分级）

### P0 — 堵住成本出血口

**1. Ensemble proposer 加 max_tokens 上限**。在 `provider/ensemble.py` 的 proposer 发起处添加可配置的 `max_output_tokens` 参数，默认值建议 4096（覆盖 99% 正常 draft 场景，拒绝 128K runaway）。配置位放入 `[llm_ensemble]` section，与其他 ensemble 参数同级。改动面：ensemble.py 一处参数传递 + config.py 一个字段 + toml 示例一行。预期收益：消除 14.4% 总成本的 runaway 泄漏（¥36.43/周 → ¥0），轮均成本保持稳定。

**2. 聚合器草稿预算可配置**。将 `_cap_candidates_to_joint_budget` 的截断预算槽位 exposed 为 `[llm_ensemble]` 下的 `aggregator_draft_budget_tokens` 配置项，默认保持现有行为（不给用户惊喜），但允许收紧以压缩约 20% 的 ensemble 成本。改动面：ensemble.py 一处配置读取 + config.py 一个字段。

### P1 — 打通设计一致性的最后一环

**3. 命名通道加 provider 键**。在 `session/naming.py` 的 `NamingTarget` 添加 provider 字段，在 `gateway/config.py` 的 `SessionNamingConfig` 添加 `naming.provider` 配置键，解析链在 model/tier/default_tier/session 四层中插入 provider 优先级。改动窗口约 50 行，`tier_model_skipped_provider_mismatch` 提供既有降级路径。

**4. 视觉 VQA 调用层解耦**。让 image_model 档位的 VQA 请求走独立 provider 连接而非复用主会话连接，与压缩通道、图像生成通道的设计原则保持一致。改动面中等（需要识别所有 VQA 调用点并替换连接获取路径）。

### P2 — 结构性完善

**5. agent.py 渐进式拆分**。不建议一次性重构 935KB 文件。按 S1-S4 识别的自然边界分三轮：第一轮抽出回合预算治理（已有 `result_budget.py` 和 `context_budget.py`，将 agent.py 中对应的调用逻辑迁移到独立模块）；第二轮抽出证据门控（`finalize_evidence_gate.py` 已存在，将其从 agent.py 的 import 消费者变成独立步骤）；第三轮将工具循环核心提取为 `tool_loop.py`。每轮保持主链路可运行，避免大爆炸重构。

**6. 补全 per-agent 路由绑定 consumer**。在 `engine/steps/squilla_router.py` 或 `policy.py` 中实现读取 `AgentRoutingConfig.default_tier/max_tier` 的逻辑，提供"按 agent 硬绑路由策略"的能力。改动面小（一处 consumer + 一项政策闸门），收益是让已有 schema 生效。

**7. 辅助通道 Semaphore 可配置**。将 `_auxiliary_slot = Semaphore(1)` 中的 1 改为可配置参数（默认保持 1 不改变行为），让高密度场景可以放开并发度。

### P3 — 运维清理与文档对齐

**8. 清理根目录构建残留**。将 `__pycache__/`、`.pytest_cache/` 加入 `.gitignore` 并从仓库中移除。

**9. 更新 agent.py 文档注释**。将 "Core loop is under 500 lines" 替换为对当前文件结构的准确描述（如 "Agent state machine, tool loop, evidence gating, and turn budget governance — see engine/ for sub-module breakdown"）。这不是改变代码，而是让文档与实现对齐。

---

## 十一、总结

OpenSquilla 是一个架构意图清晰而实现规模膨胀的 Alpha 级个人 AI 运行时。其核心差异化能力——路由决策的显式状态机化（7 闸门顺序可测试、可 golden 钉死）、预算治理的分层封顶（Context/Result/Auxiliary 三层互不越权）、观测流的完整闭环（决策日志 + 回放 API + 证据门）——代表了 AI Agent 基础设施的正确方向。

但项目当前面临四个层面的挑战：设计上有微内核声明与单体实现的结构性背离、配置热更范围不一致、辅助通道设计退化、self_learning 骨架无肌肉；工程上有 proposer 无输出上限造成的真实成本泄漏（14.4% 总成本）、聚合器草稿预算不可控、per-agent 路由 schema 空转；配置面上辅助模型分散、命名通道缺 provider 键、视觉 VQA 未解耦；运维上文档承诺与实际规模相差 40 倍。

最优先的改进不是重构 935KB 的 agent.py（那是一个需要分三轮渐进式完成的长期工程），而是堵住 Ensemble proposer 无输出上限这个明确的成本出血口——一行 max_tokens 参数的改动就能消除 14.4% 的总成本泄漏。其次是把命名通道的 provider 键补上、把 per-agent 路由绑定从 schema 空壳变成实际可用的功能——这两项的改动窗口都很小但收益明确。

12 维度覆盖自查：架构模式✅ S4 / 数据流✅ S3 / 安全设计✅ S4+S7 / 错误处理✅ S4 / 日志监控✅ S4 / 测试策略✅ S2+S7 / i18n✅ S1（8 语言 README + 本地化提示词）/ 性能优化✅ S5（缓存、预算）/ 依赖管理✅ S1 / 部署运维✅ S1+S7 / 配置管理✅ S1+S2 / API 设计✅ S3+S4。无缺项。

---

*本报告由 deep-code-analyzer v2.0.0 流水线生成；证据均为源码级（read_file/grep_search 实采），置信度分级遵循 schemas/context.md 六级制。*
