# OpenSquilla 增量体检更新（基线 f87fed4b → main）

> 更新日期：2026-08-24 | 工具：deep-code-analyzer v3.x（断点恢复 + 增量体检）
> 旧基线：upstream/main `f87fed4b`（2026-08-08 分析，分支 feat/openai-bridge）
> 当前状态：分支 **main**，HEAD `4e48f9b56`，版本 **0.5.3**
> 性质：对 `10-stage1-4-checkpoint.md` / `13-s7-final-report.md` 中已过时结论的勘误与增补。所有量化数字标注 origin。

---

## 1. 演进规模总览

| 指标 | 数值 | origin |
|---|---|---|
| 基线以来提交数 | **164 commits**（f87fed4b..HEAD，f87fed4b 为 merge-base 祖先） | repo_verified（git rev-list --count） |
| `src/opensquilla` 变更 | **341 个文件，+96,436 / −8,942 行** | repo_verified（git diff --stat） |
| 变更最集中的目录 | opensquilla-webui/src（424 文件）、src/opensquilla（341）、tests/test_engine（88）、tests/test_gateway（75）、desktop/electron（47）、tests/test_provider（28） | repo_verified（git diff --name-only 分组计数） |
| 新增源码热区（按新增文件计） | gateway +13、artifact_session +10、skills +10、engine +7、runtime_packs +5、tools +5、openai_bridge +2 | repo_verified（git diff --diff-filter=A） |

结论：这不是小版本漂移，而是一次**结构性演进**——模块清单、巨型文件规模、功能面均有实质变化，旧报告的数字需按下文修正。

## 2. 版本与分支状态

| 字段 | 旧值（2026-08-08 报告） | 新值（本次实测） | origin |
|---|---|---|---|
| 版本 | 0.5.2 stable | **0.5.3**（pyproject.toml:3），CHANGELOG 另有 [Unreleased] 段 | repo_verified |
| 分支 | feat/openai-bridge | **main**（OpenAI bridge 已以 `openai_bridge/` 模块合入主线，另有本地提交 4e48f9b56 继续 openai bridge/routing 实验） | repo_verified |
| 工作区 | — | 有未提交改动：`src/opensquilla/session/storage.py`（M）、workfiles/2026-08-24/（未跟踪） | repo_verified（git status） |

## 3. 异常信号复测（A1–A8：旧值 → 新值）

原 S1 的 8 条异常信号全部**仍然成立且加剧**：

| # | 信号 | 旧值（B） | 新值（B） | 变化 |
|---|---|---|---|---|
| A1 | engine/agent.py 巨型单文件 | 935,229 | **1,166,429**（≈1.11 MiB） | ▲ +231KB（+24.7%） |
| A2 | engine/runtime.py | 459,842 | **585,031** | ▲ +125KB（+27.2%） |
| A3 | gateway/rpc_sessions.py | 344,919 | **500,775** | ▲ +156KB（+45.2%） |
| A4 | gateway/boot.py | 200,795 | **235,347** | ▲ +35KB（+17.2%） |
| A5 | gateway/channel_dispatch.py | 168,579 | **186,413** | ▲ +18KB（+10.6%） |
| A6 | gateway/config.py | 152,813 | **156,352** | ▲ 微增 |
| A7 | skills/bundled/ 深嵌套 | ≥5 级 | 未复测（非本次目标） | — |
| A8 | gateway/ 模块文件数 | ~100 | **108** 个 .py | ▲ +8 |

> 全部 origin=repo_verified（Get-Item 字节实测，2026-08-24）。
> 值得注意的正面信号：巨石并非无序膨胀——本轮演进把作品的修订/变更集/审计核心**抽出了独立模块** `artifact_session/`（10 文件 336KB），说明团队有意识地在外科手术式拆分，但 agent.py 本体仍在变胖。
> [推演] 若拆分速度持续低于增长速度（A1 三周 +24.7%），agent.py 将在数月内突破 1.5MB，工具链（编辑器/评审/合并）会率先不可用。

## 4. 模块结构变化（S2 修订）

- 业务模块：33 → **35** 个（覆盖率口径不变）：
  - **新增 `artifact_session/`**：作品的持久修订（Revision）、变更集（ChangeSet）、提示标注（PromptAnnotation）、编辑会话与审计事件核心；导出 WriterLease 冲突/过期错误族。支撑 Workbench 文档协作与"Electron-first HTML artifact editing"。origin=repo_verified（__init__.py docstring + 导出表）
  - **新增 `runtime_packs/`**：可选、独立管理的开发者 Runtime Packs（catalog/manager/models/resolver 四件套 + 操作状态机 RuntimeOperationState）。origin=repo_verified（__init__.py 导出表）
- 文档面：20 篇 docs 变更；新增 `docs/goal-mode.md`、`docs/features/prompt-annotation-editing.md`、`docs/releases/0.5.3.md`。origin=repo_verified（git diff --name-only f87fed4b..HEAD -- docs/）

## 5. 功能演进要点（自 CHANGELOG 0.5.3 与提交史提炼）

origin=repo_verified（CHANGELOG.md 头部 + git log --oneline），置信度 high（未逐特性进源码验证行为细节）：

1. **持久化目标（Durable Goals）**：跨回合延续，显式 progress/pause/resume/edit/clear + Plan 模式推迟控制（对应新 docs/goal-mode.md；解释了 engine/ 7 个新文件与 test_engine 测试激增）。
2. **MetaSkills + Cron workspace 管理 + `/meta` 内联请求**：技能生命周期诊断增强（gateway/rpc_skills.py 大改，对应 rpc_skills 中 capability 参数族）。
3. **社区技能源（ClawHub / GitHub）**：不可变源解析 + 事务化管理，Gateway RPC 与 CLI 增加只读 Doctor 诊断。
4. **TokenRhythm 默认档位刷新**：C0=DeepSeek V4 Flash 0731、C1 直连默认=DeepSeek V4 Pro 0813、C2=Kimi K2.7 Code、C3=GLM 5.2 B5 fusion（[Unreleased]；已有自定义 inline tiers 不迁移）。→ **旧报告 11-s5-squillarouter-deep-dive.md 中的档位示例已过时**。
5. **自选服务商（custom provider）恢复**：经网关 RPC 发现模型（592ecf5b2 先改为直连 HTTP，28d5565f8 又改回网关 RPC——两次方向反转值得注意）。
6. **会话管理增强**：session move-to-workspace RPC（侧栏子菜单）、新建对话可选智能体（e4d6cea72）。
7. **稳定性修复**：SQLite busy_timeout 100ms→5000ms（6a8007eba）；Stop 立即确认 + 在途文件变更安全收尾 [Unreleased]；transcript 读不再阻塞回合写（93287d1ff）。
8. **模型能力元数据**：Qwen 已验证模型显式提示缓存（dccc1424c）；辅助用量通用自定义定价修复（65265b98f）。

## 6. 对既有分析结论的影响

| 既有结论 | 判定 | 说明 |
|---|---|---|
| 主链路 5 环节假设（S3） | ✅ 仍成立 | 入口/管线/路由/持久化路径未见结构性迁移（本轮深挖期间反复穿越该链路均吻合） |
| 25 项架构模式（S4） | ✅ 基本成立 | P1–P9/A1–A5/C1–C5/S1–S6 所锚定的符号在本次检索中仍可见；未发现被删除的模式证据 |
| "33 个业务模块" | ⚠️ 需修正 | 改为 35（见 §4） |
| 档位默认模型示例（11-s5 报告） | ⚠️ 需修正 | 见 §5 第 4 条 TokenRhythm 刷新 |
| 巨型文件数值（A1–A6） | ⚠️ 需修正 | 见 §3 表 |
| 版本号 0.5.2 / 分支 feat/openai-bridge | ⚠️ 需修正 | 见 §2 |
| 12-s5 辅助模型报告 | ➖ 已由 15 号勘误覆盖 | 本次无新反证 |

## 7. 待办建议（供后续轮次消费）

1. `[必挖候选]` `engine/agent.py` 1.17MB 的内部构成复查：goal-mode 引擎并入后是否出现可再抽取的高内聚子域（对照 artifact_session 先例）。
2. `[可选]` `runtime_packs/` 与 `run_mode` 的关系尚未展开（仅读 __init__ 导出面）。
3. `[可选]` 自选服务商两次方向反转（直连 HTTP ↔ 网关 RPC）背后的决策依据，可在 docs/providers-and-models.md 与提交讨论中追溯。
4. 「配置的能力」模块深挖见姊妹篇 **17-s5-capability-config-deep-dive.md**。
