# OpenSquilla (泰路札) 项目深度架构分析报告

> 分析日期：2026-08-08 | 分支：feat/openai-bridge | 跟踪：upstream/main f87fed4b
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

> **使用指南**：按 S1→S4→S5→S7 顺序阅读，或按需直接跳至深挖专题。`15-auxiliary-module-source-code-analysis.md` 是对 `12-s5-auxiliary-model-deep-dive.md` 的勘误补充，建议两篇对照阅读。
