# S5 深挖报告：辅助功能模型（Auxiliary）模块 — 路由结构与实现现状

> 分析对象：OpenSquilla-QinLuza-Studio（v0.5.2，`feat/openai-bridge` 分支）
> 分析依据：`session/naming.py`、`provider/auxiliary_budget.py`、`gateway/task_runtime.py`、`gateway/compaction_target.py`、`gateway/config.py`、`router_tiers.py`、`engine/selector_override.py` + 用户提供的调研记录（2026-08-11）
> 置信度约定：`confirmed` = 源码直接验证；`high` = 单文件内直接可见；`medium` = 推断

---

## 一、核心结论（先行）

你那份调研文件的**全部源码论断在本仓库中 100% 命中**，无一处失实。上游架构的真实形态是：**主模型路由（c0-c3 + image_model 档）与辅助模型通道（auxiliary）是两套并行体系**——主路由负责用户回合，辅助通道负责"旁路单次短调用"（标题、压缩、视觉门控等），各有独立的模型解析链、预算闸门、并发槽与记账（call_kind）。

按"可实现 / 已实现 / 部分实现"三档盘点：

| 能力 | 状态 | 实现深度 |
|---|---|---|
| AI 标题生成（命名） | **已实现** | 完整独立通道，但**不能跨提供商**（缺 provider 键）→ 部分 |
| 上下文总结（压缩） | **已实现** | 最彻底：可 provider+model 成对指定物理部署，跨提供商 ✅ |
| 图像识别（视觉档） | **已实现** | 独立档位 `image_model`（image_only=true），VQA 调用仍走活动连接 → 半独立 |
| 图像生成 | **已实现** | 完全独立配置段 + 多 provider，仅共享凭证 |
| 视觉追问门控 | **已实现** | 独立配置组，默认 c0 低成本档 |
| 记忆导入 / 会话刷写 | **部分实现** | 预算与记账剥离，模型仍跟主配置 |
| 统一辅助模型管理 | **未实现** | 无统一配置根，逐通道分散 |

---

## 二、路由结构全景（已验证）

### 2.1 主路由：四档文本 + 一档视觉

`router_tiers.py` L9-12（confirmed）：

```
TEXT_TIERS = ("c0", "c1", "c2", "c3")      # 文本四档
DEFAULT_TEXT_TIER = "c1"                    # 默认档（低置信度兜底）
HIGHEST_TEXT_TIER = "c3"                    # 最高档
IMAGE_TIER = "image_model"                  # 视觉专用档（与文本档平级）
```

遗留别名 `t0-t3 → c0-c3`（L14-19），路由类 `R0-R3 → c0-c3`（L21-27）。

### 2.2 辅助通道：四条旁路 + 两条半旁路

```
主路由（用户回合）
├─ c0/c1/c2/c3 文本档（squilla_router 分类 + 7 闸门）
└─ image_model 视觉档（image_only=true，supports_image=true）

辅助通道（旁路单次调用，不进用户回合主链路）
├─ auxiliary.naming        标题生成 → session/naming.py
├─ auxiliary.compaction    上下文总结 → gateway/compaction_target.py
├─ auxiliary.session_flush 会话刷写/记忆修复 → runtime.py / memory_repair_service.py
├─ auxiliary.profile_import 记忆导入 → rpc_memory_import.py
└─ vision_followup_gate    视觉追问门控 → config.py 独立配置组（默认 c0）
```

`call_kind` 18 处调用点全部命中（`grep auxiliary.*` 实测，confirmed），分布在 agent.py、runtime.py、gateway/rpc_*.py、cli/ 各层。

---

## 三、逐通道符号追踪（源码级）

### 3.1 标题生成（auxiliary.naming）

**主符号**：`session/naming.py` → `generate_session_title` / `NamingTarget` / `_tier_model` / `title_slot_is_empty` / `is_naming_eligible`

**模型解析链**（docstring L7-15 原文，confirmed）：
```
naming.model（显式）→ naming.tier 档位模型 → 路由器 default_tier 模型 → 会话模型兜底
```
关键约束（L9-13）：
- "Model selection deliberately does NOT reuse the session model" —— 刻意不复用主模型
- "A tier model is only eligible when the tier targets the active provider" —— **provider 匹配门槛**：档位提供商与活动提供商不一致则跳过（`tier_model_skipped_provider_mismatch`）
- "Connection credentials come from the same provider the compaction path resolves" —— 连接强制取自活动提供商

**无 provider 键的实证**（L100-108，confirmed）：`NamingTarget` 字段只有 `model / api_key / base_url / timeout / provider`，其中 `provider` 是**只读标识**而非配置键——你无法为标题生成单独指定跨提供商连接。这正是调研文件所说"命名是最后一个未打通的跨提供商口子"。

**防呆与边界**（confirmed）：
- `_MAX_INPUT_CHARS = 4000` —— 未信任首条消息上限
- `_TITLE_MAX_TOKENS = 512` —— 预算须覆盖 thinking+标题（`_OPENROUTER_REASONING_DEFAULT_MODELS` 显式列出 deepseek-v4/glm-5.x 等 reasoning-by-default 模型族）
- `title_slot_is_empty` —— 幂等：derived_title 已设不重命名；display_name 非 generic 不覆盖用户手动改名
- `is_naming_eligible` —— surfaces 白名单（webchat/cli/channel/chat catch-all）；**cron 与 subagent 会话永不自动命名**
- 失败即 no-op：走 `derive_transcript_title` 截断兜底，不浪费一次调用

### 3.2 上下文总结（auxiliary.compaction）

**主符号**：`gateway/compaction_target.py` → `GatewayCompactionTarget` / `GatewayConsumerBudget` / `_NamedAuthProfileDeployment` / `resolve_gateway_consumer_budget`；`session/compaction_deployment.py`

**部署解析**（L1 docstring "Resolve an isolated physical deployment"，confirmed）：
- `provider + model` 成对 = **显式物理部署**（named auth profile），经 `build_provider_from_config` 构造独立 provider
- 独立 context window / output reserve / 认证 profile（`_NamedAuthProfileDeployment` 只保留非敏感 provenance + fingerprint）
- 失败分支：`named_auth_profile_unavailable` / `named_auth_profile_provider_build_failed`（L181-200）
- `compaction.provider` 不能单独设置（`_normalize_explicit_deployment` 静默忽略 + `validate_compaction_deployment_write` 抛错）——provider+model 必须成对（confirmed，调研文件论断命中）

**这是唯一一条彻底打通跨提供商的辅助通道**。

### 3.3 视觉档与视觉追问门控

**image_model 档**（confirmed）：`router_tiers.py` L12 定义，`config.py` 档位 schema 含 `image_only=true`、`supports_image=true`；负责附件/截图/图表/VQA。

**vision_followup_gate 配置组**（`config.py` L1284-1290，confirmed）：

```python
vision_followup_gate_enabled: bool = True
vision_followup_gate_tier: str = "c0"            # 默认低成本档
vision_followup_gate_model: str | None = None    # 可显式覆盖
vision_followup_gate_timeout_seconds: float = 10.0
vision_followup_gate_max_output_tokens: int = 512
vision_followup_gate_fallback_recent_turns: int = 2
vision_followup_gate_unknown_policy: str = "image_if_recent"
```

印证设计取向：**每个旁路能力预留独立模型覆盖位，默认倾向低成本档，不把辅助任务压给主模型**。

### 3.4 图像生成

`config.py` L1502-1541（confirmed）：`ImageGenerationOpenAIProviderConfig` / `ImageGenerationOpenRouterProviderConfig` / `ImageGenerationTokenRhythmProviderConfig` / `ImageGenerationQwenTokenPlanProviderConfig` → `ImageGenerationProvidersConfig` → `ImageGenerationConfig`（L1541）。多 provider 完全独立，仅经 `_acquire_image_generation_profile_credential`（L2342）共享凭证。

### 3.5 支撑基建（三层）

**① 空闲槽调度**（`task_runtime.py` L1015-1071，confirmed）：
```
cancel_auxiliary(session_key)          # 取消低优先级工作，不等它
run_auxiliary_if_idle(session_key, op) # 仅当会话无真实任务时运行
  ├─ busy 检查：pending/running/reservations/auxiliary 四表
  ├─ _auxiliary_slot = Semaphore(1)    # 全局单并发槽
  └─ 二次检查 real_work_arrived        # 进槽后真实任务到达 → 放弃
```
设计精髓：**辅助工作绝不阻塞用户输入**——取消直接从 enqueue 传播进 provider 流。

**② 预算闸门**（`provider/auxiliary_budget.py`，confirmed）：
- `AuxiliaryRequestBudget`：provider_id/model/context_window/max_output/max_input/request_max_chars/window_source
- `AuxiliaryRequestTooLargeError`：**fail-closed**，发起前即抛（chars/tokens 双检查）
- 未知部署保守兜底 `_UNKNOWN_DEPLOYMENT_CONTEXT_TOKENS = 32_000`；溢出阈值 0.85

**③ 记账**：`call_kind` 区分四种 auxiliary 类型，与主模型调用分离统计（18 处调用点实测）。

---

## 四、状态机（辅助任务生命周期）

```
触发条件（会话空闲 + 符合 eligible）
  → run_auxiliary_if_idle
      ├─ busy? → 放弃（return False）
      ├─ 获 auxiliary_slot（Semaphore(1)）
      │    └─ 二次检查 real_work_arrived?
      │         ├─ 是 → 放弃（用户输入优先）
      │         └─ 否 → 执行 operation()
      │              ├─ resolve_auxiliary_request_budget（fail-closed 预检）
      │              ├─ 发起 provider 调用（独立连接/模型）
      │              ├─ 成功 → 写结果（如 derived_title）/ 失败 → no-op 兜底
      └─ finally：清理 _auxiliary_tasks_by_session
取消路径：enqueue 真实任务 → cancel_auxiliary → task.cancel() → 传播进 provider 流
```

---

## 五、复杂度分析

| 维度 | 评估 |
|---|---|
| 通道数 | 4 主 + 2 半旁路，每通道独立配置根 |
| 解析链深度 | 命名 4 级（model→tier→default_tier→session），压缩 3 级（named→provider+model→session） |
| 并发模型 | 全局 Semaphore(1) 单槽 + 每会话单任务映射，串行化极强 |
| 预算维度 | 4 类预算（window/output/input/chars），fail-closed |
| 记账面 | 18 个 call_kind 调用点 × 4 类型 |
| 风险 | 全局单槽 = 辅助任务天然低吞吐（设计取舍：宁可排队不抢主链路） |

**复杂度评级：中高**——单通道实现简单（都是"解析链 + 预算 + 单次调用"），但 6 通道 × 各自解析语义 × provider 匹配约束的**组合面**才是复杂度所在。

---

## 六、已实现 vs 部分实现（对照你的设想）

你的设想："把上下文总结、标题生成、图像识别拆给子代理，独立于主模型主智能体"。

| 你的设想 | 上游实际 | 差距 |
|---|---|---|
| 派生子代理会话 | **不派生子代理**，gateway 进程内 auxiliary 通道 | 形态更薄：单次短调用用"空闲槽+预算"而非"会话生命周期" |
| 主模型分离 | 命名/压缩/视觉/图像生成全部剥离 | 达成，但**逐通道放开** |
| 自选模型 | 全通道可显式覆盖 model/tier | 达成 |
| 跨提供商自选 | 仅压缩（#921）、图像生成可；**命名无 provider 键**、视觉半绑 | **未完全达成** |

**已实现（main 主干）**：命名通道全链路、压缩部署感知（#921）、视觉档位、图像生成独立段、vision 门控、auxiliary 三层基建、call_kind 记账（#589）。

**部分实现（有骨架缺最后一环）**：
1. 命名通道缺 `naming.provider` 键——连接强制活动提供商（`NamingTarget` 无 provider 配置位）
2. 视觉 VQA 调用仍走活动连接（档位自带 provider 但调用层未解耦）
3. 记忆导入/会话刷写仅剥离预算与记账，模型仍跟主配置

**未实现**：统一辅助模型管理 UI/配置根；#1133（Meta 子代理物理契约，draft 未合，2026-08-10 刚开）是官方朝"子代理继承窄化物理请求契约"方向的第一步，未进 main。

---

## 七、配置速查（可操作）

```toml
# 标题生成（env 前缀 OPENSQUILLA_NAMING_）
[naming]
enabled = true
surfaces = ["webchat", "cli", "channel", "chat"]  # cron/subagent 永不参与
model = ""          # 显式模型（最高优先）
tier = ""           # 指定档位（provider 必须匹配活动提供商，否则跳过）

# 上下文总结（env 前缀 OPENSQUILLA_COMPACTION_）
[compaction]
model = ""          # 留空 = 用会话模型
provider = ""       # 不可单独设置，必须与 model 成对 = 显式物理部署

# 视觉档（与 c0-c3 平级）
[squilla_router.tiers.image_model]
provider = "tokenrhythm"
model = "kimi-k2.6"          # 默认视觉模型
image_only = true
supports_image = true

# 视觉追问门控
vision_followup_gate_enabled = true
vision_followup_gate_tier = "c0"        # 默认低成本
vision_followup_gate_model = ""         # 可显式覆盖

# 图像生成（完全独立）
[image_generation]
enabled = false
# primary = "qwen_token_plan/wan2.7-image"  # 示例
```

---

## 八、研判与建议

1. **设计取向确认**：上游刻意走"轻量 auxiliary 通道"而非子代理——代码注释可读出对会话生命周期/usage 归集/上下文复制成本的回避。你若要仿制，直接抄 `task_runtime.run_auxiliary_if_idle` + `auxiliary_budget` 这对组合即可，不必复制整个子代理体系。

2. **命名通道是最近的可落地改进点**：给 `NamingTarget` 增加 provider 键 + 解析链插入 provider 位，就能打通最后一道跨提供商口子。改动面小（naming.py 单文件 + config 类 + toml 示例），风险低（有 `tier_model_skipped_provider_mismatch` 既有降级路径）。

3. **跟踪上游演进盯三个号**：#1133（Meta 子代理物理契约，draft）、#5（provider pinning，candidate）、命名通道 provider 键（当前缺失）。

---

## References

1. `src/opensquilla/session/naming.py` — 命名通道解析链（L1-21 docstring、L100-108 NamingTarget、L146+ _tier_model）
2. `src/opensquilla/provider/auxiliary_budget.py` — 预算闸门（L20-66）
3. `src/opensquilla/gateway/task_runtime.py` — 空闲槽调度（L1015-1071）
4. `src/opensquilla/gateway/compaction_target.py` — 压缩部署感知（L1、L73-200）
5. `src/opensquilla/gateway/config.py` — L1284-1290 vision 门控、L1416 SessionNamingConfig、L1502-1541 图像生成
6. `src/opensquilla/router_tiers.py` — L9-12 档位定义
7. 用户调研记录 `opensquilla-chat-辅助功能模型实现现状-2026-08-11.md`（含 #207/#921/#589/#1133/#5 等上游证据）
