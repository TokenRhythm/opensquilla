# OpenSquilla (泰路札) 项目深度架构分析报告

> 分析日期：2026-08-08 | 分支：feat/openai-bridge | 跟踪：upstream/main f87fed4b
> **⚠ 增量更新（2026-08-26）**：源码已演进至 main @ `a0bbe0235` / **v0.5.4**（自 16 号基线以来 47 提交，src 变更 86 文件 +13036/−2929 行）。版本、巨型文件榜、TokenRhythm 档位、17/18/19 号锚点行号均已更新——**最新勘误层见 [`21-delta-update-v0.5.3-to-v0.5.4.md`](21-delta-update-v0.5.3-to-v0.5.4.md)**（含 waterline 治理双件套、artifact 编辑闭环、锚点漂移表）。
> **⚠ 前次增量（2026-08-24）**：v0.5.3 @ `4e48f9b56` 勘误见 [`16-delta-update-f87fed4b-to-main.md`](16-delta-update-f87fed4b-to-main.md)；深挖「配置的能力」见 [`17-s5-capability-config-deep-dive.md`](17-s5-capability-config-deep-dive.md)。
> 状态：进行中
> 工具：deep-code-analyzer v2.0.0（S1→S7 流程）

---

## 目录

1. [项目概览](#项目概览)
2. [架构总览图](#架构总览图)
3. [文档索引](#文档索引)

---

## 项目概览

**OpenSquilla 0.5.2** — 一个以"token 效率"为第一性原理的**微内核 AI Agent 运行时**。标语 "Same budget, more capability, better results" 直接定义了产品哲学：不追求单轮对话的极致智能，而是追求**单位预算下的长期可用性**（cost-per-success）。

项目横跨五类交付面：CLI（typer）、Web UI（Vue3 + Vite）、桌面端（Electron）、网关 RPC（WebSocket + ASGI）、消息通道（Terminal/WebSocket/Slack/飞书/Discord/Telegram 六通道）。所有表面共享同一条运行时路径、同一套工具、同一份记忆、同一个用量账本——这是"统一表面"架构的核心承诺。

版本状态：Alpha（Development Status :: 3），Apache-2.0，Python 3.12+，Node 22.12+（WebUI 构建）。

### 核心能力

| 能力域 | 说明 |
|--------|------|
| **Multi-Provider LLM** | 30+ LLM 提供商统一抽象（OpenAI/Anthropic/Ollama/Ensemble/失败分类），同构于 `LLMProvider` 协议，工厂按 providerType 创建 |
| **Agent System** | 主 Agent 状态机 + 工具循环，显式状态机驱动（AgentState/AgentEvent 枚举），无递归调用，回合级预算治理 |
| **Tool Ecosystem** | 文件操作、搜索、Python 执行、Web 抓取、MCP 等，工具注册表 + 内置工具，清单驱动注册 |
| **Sandbox** | 多层安全沙箱（Bubblewrap / Seatbelt / Noop 自适应），权限矩阵 + 注入防护信封 + 工具分档 + 审批门控 |
| **Session Management** | 多会话 + 长期记忆（嵌入/检索/刷写）+ 压缩（compaction）+ 会话刷写（session_flush），SQLite + sqlite-vec 持久化 |
| **Multi-Channel** | 六通道统一：Terminal / WebSocket / Slack / 飞书 / Discord / Telegram，通道归一化 + 审批门控 |
| **Artifact System** | 作品、报告、图纸的生成与归档，支持 Markdown/HTML/PDF 等多格式输出 |

### 平台形态

| 面 | 形态 |
|----|------|
| **CLI** | Typer 命令行 + questionary 交互 + rich 渲染，TUI 支持 |
| **Web UI** | Vue3（536 ts + 139 vue）+ Vite + 多语言内置 |
| **桌面端** | Electron（desktop/ 82 文件），打包 gateway runtime + 控制台 |
| **网关** | ASGI + WebSocket + RPC 调度，100+ rpc 处理器 |
| **OpenAI 兼容桥** | `/v1/chat/completions` 供 CodeBuddy 等外部接入 |
| **容器** | Docker 多阶段（node:22 → python:3.13-slim），compose.yaml 单服务 |

### 关键设计决策

1. **Token 效率优先** — 路由选便宜模型、92% 缓存命中优先、预算封顶（Context/Result/Auxiliary 三层互不越权）。
2. **微内核架构** — 核心引擎最小化，所有入口（CLI/WebUI/通道）跑同一 `engine/agent.py` 状态机，技能/通道/Provider/搜索均可插拔注册。
3. **本地优先的路由** — SquillaRouter on-device 按轮次选 cheapest capable model，v4_phase3 ML 推理 + 自学习闭环（self_learning/ 14 文件）。
4. **可插拔 Provider 层** — 同一 schema 接入 20+ LLM 提供商，统一故障分类与重试策略。
5. **有界执行** — 技能系统自身贯彻显式状态机、重试上限、轮次上限四铁律。

---

## 架构总览图

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

**主链路 5 环节假设全部验证闭合**：
- 主链路假设（confirmed）
- 路由闸门顺序（confirmed，policy.py docstring 逐条核对）
- 辅助通道旁路（confirmed，task_runtime 空闲槽）
- 压缩隔离部署（confirmed，compaction_target 解析链）
- 观测流不阻塞主链路（confirmed，独立 append）

**33 个业务模块，S2 覆盖率 91.7%**，核心六件套：`engine/`、`gateway/`、`provider/`、`squilla_router/`、`sandbox/` + `safety/`、`memory/`，支撑层 21 个模块。

---

## 文档索引

| 文件 | 阶段 | 内容概要 |
|------|------|----------|
| `10-stage1-4-checkpoint.md` | S1-S4 | 全景卡片、33 模块映射（91.7% 覆盖率）、主链路数据流、25 项架构模式 |
| `11-s5-squillarouter-deep-dive.md` | S5① | SquillaRouter 路由算法深度拆解：7 闸门决策链、单/多路由省钱哲学、两个成本出血口 |
| `12-s5-auxiliary-model-deep-dive.md` | S5② | 辅助功能模型模块：命名/压缩/刷写/视觉门控的已实现与部分实现边界 + 落地建议 |
| `13-s7-final-report.md` | S7 | 最终综合报告：12 维度覆盖、架构定性、三大亮点、三大技术债务、优先级建议 |
| `15-auxiliary-module-source-code-analysis.md` | 勘误 | 2026-08-11 复核：naming 通道遗留传输层定性修正 + 四源码库实证 + 落地建议更新 |
| `16-delta-update-f87fed4b-to-main.md` | 增量体检 | 2026-08-24：基线以来 164 提交的漂移勘误（版本/模块数/巨型文件复测/新增 artifact_session 与 runtime_packs/0.5.3 功能面） |
| `17-s5-capability-config-deep-dive.md` | S5③ | 「配置的能力」模块深挖：四能力写路径事务、五态状态机、所有权与 resettable 双模型、差分持久化存储、复杂度与替代对比、配置速查 |
| `18-s5-session-storage-memory-deep-dive.md` | S5④ | 会话存储（session/ 646KB 巨石、28 张内联表、accept_turn 原子提交、SQLite 争用治理、压缩后三层读路径 §3.1）与记忆体系（memory/ 取证） |
| `19-pluginization-feasibility-routing-kernel.md` | 可行性 | 对照 DSH/pi 插件模型评估"以路由为内核、其余皆插件化"：六真接缝盘点 + L1 路由升格执行模式总线（衔接 AGENTOS c3 决策档）+ L2 装配声明化 + 上游共存策略 |
| `20-preflight-routing-kernel-mvp.md` | 前置复核 | 路由内核化 MVP 纯读取证：决策对象两级链（RoutingDecision/RoutePlan）、单模型+融合路径行级定位、bypass hack 移位确认、早/晚钩子裁决输入、修订锚点预算 |
| `21-delta-update-v0.5.3-to-v0.5.4.md` | 增量体检 | 2026-08-26：v0.5.4 勘误（47 提交/86 文件；waterline 投影+告警双件套、artifact 编辑闭环、每会话路由策略、runtime_packs 固化目录、死代码清理潮、supports_tools 语义反转、TokenRhythm 新档位、**17/18/19 号锚点漂移表**、D10/D11 漂移信号） |
| `22-tool-routing-optimization-analysis.md` | 工具路由 | 2026-08-26：六层工具路由机制全量盘点（cron/子代理/声明式策略/per-job/模式/模型来源，全锚点亲证）、全量暴露成本量化（本会话实测 60 工具暴露 + schema 遥测管道已埋）、P0-P3 优化路线（零代码配置启用 / cron toolPolicy / 遥测聚合 / 统一意图表 / 路由内核 / 能力签名注册表）、4 项待主人裁决 |

> **使用指南**：按 S1→S4→S5→S7 顺序阅读，或按需直接跳至深挖专题。`15-auxiliary-module-source-code-analysis.md` 是对 `12-s5-auxiliary-model-deep-dive.md` 的勘误补充，建议两篇对照阅读。**`21` 号为全档案的最新勘误层**（其次 16 号），先读它再读旧数字与旧行号锚点。
