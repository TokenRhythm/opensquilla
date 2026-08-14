# 辅助模型模块优化方案 — 四源码库实证分析

> 日期：2026-08-11
> 基于：Hermes / Operit / Pi 三参照系源码 + OS 本地源码实证
> 方法：code-explorer 子代理并行深挖四代码库 + 主代理亲自读 OS 核心文件
> **2026-08-11 勘误**：原稿对 naming 通道的欠债范围定性夸大（子代理结论未经复核即采纳）；主代理已重新 grep/read 源码取证，修正 §1.1、§1.2、§3.1–3.3、§5。勘误过程本身按 deep-code-analyzer v2.2.0 第 5 条铁律（复核后落档）执行。

---

## 一、源码实证：OS 辅助通道的真实状态

### 1.1 四条通道的调用方式（源码确认）

| 通道 | 文件 | LLM 调用方式 | 走 provider adapter? | 走 auxiliary_budget? | call_kind 记账? |
|---|---|---|---|---|---|
| naming | `session/naming.py` | **裸 httpx.AsyncClient POST**（遗留传输层） | ❌ | ✅（L314/L354/L547） | ✅（rpc_sessions.py L2377） |
| compaction | `session/compaction.py` + `gateway/compaction_target.py` | provider adapter `chat()` 流式 | ✅ | ✅ | ✅ |
| session_flush | `memory/session_flush.py` (136KB) | `_provider_complete()` 支持 complete()+chat() | ✅ | ✅ | ✅ |
| profile_import | `gateway/rpc_memory_import.py` | provider adapter `chat()` + ChatConfig | ✅ | ✅ | ✅ |

**关键发现（2026-08-11 复核）**：naming 通道是**唯一不走 provider adapter 的通道**，但原稿“完全绕过统一基建”系夸大定性——它**确实走**预算闸门（`ensure_auxiliary_text_fits` L314、`resolve_auxiliary_request_budget` L354/L547）与 call_kind 记账（`rpc_sessions.py` L2377）。真实欠债仅限传输层：裸 httpx POST、provider 身份从 URL 字符串猜测，因此缺少 adapter 的故障转移链与配置热更：

```python
# naming.py L544-545 — provider 身份从 URL 猜测（L597 亦有类似逻辑；2026-08-11 复核确认）
budget_provider = provider or (
    "openrouter" if "openrouter.ai" in url.lower() else "openai_compat"
)
```

**重新定性（2026-08-11 复核）**：naming 的问题既不是第一轮的“缺 provider 键”，也不是第二轮的“完全绕过统一基建裸奔”，而是**遗留传输层**——预算与记账都在，缺的是 adapter 能力（故障转移链、配置热更、统一凭据解析）。迁移到 provider adapter 的方向不变，但优先级为 P1 改进项，不是 P0 基建欠债。

### 1.2 call_kind 记账机制（源码确认）

定义在 `provider/tokenrhythm_correlation.py` L37-55：

```python
_AUXILIARY_CALL_ROLES = frozenset({
    "meta", "vision_gate", "session_flush", "media",
    "naming", "compaction", "image_generation", "other",
})
_ENSEMBLE_CALL_PHASES = frozenset({"proposer", "aggregator", "fallback_single"})
```

- 作为 HTTP 头 `X-OpenSquilla-Call-Kind` 传输（仅 TokenRhythm 官方域名）
- `ProviderRequestCorrelation` dataclass 携带 session_id/turn_id/execution_id/call_kind
- **有按任务聚合**：`build_session_usage_scope()` (usage_ledger_runtime.py L47)
- naming 的记账在调用点完成（`rpc_sessions.py` L2377 `call_kind="auxiliary.naming"`），**未绕过**（2026-08-11 复核确认，更正原稿的疑似判断）

### 1.3 auxiliary_budget.py 的真实职责（主代理亲自读）

`resolve_auxiliary_request_budget()` 是**纯预算闸门**：

- 绑定到具体 provider+model（从 provider metadata 解析）
- 解析上下文窗口（有 hint matching + conservative_default 32K 兜底）
- 解析 max_output_tokens（通过 catalog.resolve_max_tokens）
- 计算 provider_request_max_chars（ContextBudgetGovernor snapshot）
- `ensure_auxiliary_text_fits()` 做预检——超限 raise AuxiliaryRequestTooLargeError

**关键**：它**不做模型解析**。模型解析由调用方自己做。这是与 Hermes 的根本差距——Hermes 的 `call_llm(task=...)` 四合一，OS 把预算抽出来但模型解析散落各处。

### 1.4 task_runtime.py 的调度机制

- 208KB 巨文件，包含空闲槽 + Semaphore + 会话级取消
- 全局 Semaphore(1)——粒度粗
- 有 hard deadline / overflow policy / reserved slots / terminal cleanup（从测试文件名推断）
- 未能完整读取（太大），但核心事实确认：全局 1 槽，非 per-task

---

## 二、三参照系源码实证对比

### 2.1 Hermes 辅助模型（`agent/auxiliary_client.py`）

**统一入口** `call_llm(task=...)` L8604-8657：
```python
@_relay_auxiliary_call                    # Relay 身份跟踪
def call_llm(task=None, *, provider=None, model=None, ...):
    semaphore = _acquire_sync_aux_semaphore(task)  # per-task 信号量
    if semaphore: semaphore.acquire()
    try:
        response = _call_llm_impl(task=task, ...)  # 委托实现
        return response
    finally:
        if semaphore: semaphore.release()
```

**任务级配置重写** `_resolve_task_provider_model()` L7369-7543：
- 三级优先级：显式参数 > `auxiliary.<task>.*` 配置 > `"auto"` 哨兵
- MoA 虚拟 provider 解包（`_unwrap_moa_provider`）
- `"auto"` 是哨兵值，触发全自动检测链

**8 层错误恢复链**（子代理报告，输出截断但确认存在）：
瞬时重试 → temperature 修正 → max_tokens 转换 → 402 降级 → 连接/限流/认证降级 → stale 自愈

**aux_accounting.py**（5KB）：
- `record_aux_usage(task, ...)` 按任务聚合 token/请求/并发
- per-task 独立追踪

### 2.2 Operit 功能模型（Kotlin）

**FunctionType 枚举**（`data/model/FunctionType.kt`）：
```kotlin
enum class FunctionType {
    CHAT, SUMMARY, TITLE_GENERATION, MEMORY, UI_CONTROLLER,
    TRANSLATION, GREP, ROLE_RESPONSE_PLANNER,
    IMAGE_RECOGNITION, AUDIO_RECOGNITION, VIDEO_RECOGNITION
}
```

**两层映射** `FunctionConfigMapping(configId, modelIndex)`：
- 持久化在 DataStore JSON
- `MultiServiceManager.getOrCreateServiceForFunctionLocked()` 按 FunctionType 创建/缓存 ManagedService
- `setConfigForFunction(functionType, configId, modelIndex)` 写入映射

**UI 自动生成**（`FunctionalConfigScreen.kt` L162）：
```kotlin
items(FunctionType.values()) { functionType ->
    // 遍历枚举自动生成每个功能的模型选择卡片
}
```

**能力标志**：supportsVision/Audio/Video 从配置读取（非硬编码）
**热更新**：Lease/Retire/Release 三段式（子代理报告确认存在，输出截断）
**modelName 变体**：逗号分隔 + modelIndex 选择 N 个备选

### 2.3 Pi 依赖纪律

**BeforeProviderRequest**（`core/extensions/runner.ts` L1016-1048）：
```typescript
async emitBeforeProviderRequest(payload: unknown): Promise<unknown> {
    let currentPayload = payload;
    for (const ext of this.extensions) {
        const handlers = ext.handlers.get("before_provider_request");
        for (const handler of handlers) {
            const handlerResult = await handler(event, ctx);
            if (handlerResult !== undefined) {
                currentPayload = handlerResult;  // 链式替换
            }
        }
    }
    return currentPayload;
}
```

**关键发现**：`onPayload` 是**契约约束非编译强制**。文档写"Implementations must invoke options.onPayload"。如果你写新 provider 不调 onPayload，扩展就不生效——这就是"必须改源码"的含义。

**Compat 反模式防护**：**没有自动化依赖图分析**。是声明式标记 + 有计划删除路线 + 契约约束。`compat.ts` 顶部明确标注"Temporary compatibility entrypoint"。

---

## 三、修正后的优化方案

### 3.1 核心问题重新定性

上一轮定性为"散装通道，缺统一入口"。源码实证后又经 2026-08-11 复核，修正为：

> **OS 的辅助通道是“三条全合规 + 一条半合规”**。compaction/session_flush/profile_import 三条都走 provider adapter + 预算 + 记账。naming 走预算 + 记账，但传输层为裸 httpx（缺故障转移链等 adapter 能力）。问题的核心是**“缺失任务类型（翻译/GREP/多媒体识别）” + “无用户可配层”**；naming 迁移属 P1 改进项，不是 P0 欠债（2026-08-11 复核）。

### 3.2 修正后的方案

| 优先级 | 问题 | 方案 | 红线影响 | 成本 |
|---|---|---|---|---|
| **P0** | 缺用户可配语义层 | 建 FunctionType 枚举（先 8 个：CHAT/SUMMARY/TITLE/MEMORY/TRANSLATION/GREP/IMAGE/AUDIO）+ 默认 tier 映射 | 纯新增 | 低 |
| **P1** | naming 传输层遗留（缺故障转移/热更） | 迁移 naming 到走 provider adapter（复用 compaction 那套模式） | 单文件改动 | 低 |
| **P2** | 缺翻译/GREP 两类高频辅助任务 | 新增 `auxiliary/translation.py` + `auxiliary/grep_planner.py`，走 compaction 模式（provider adapter + budget + call_kind） | 新增文件 | 中 |
| **P3** | 模型解析散落各处 | 引入 `auxiliary/registry.py` 统一入口（Hermes 式 call_aux），现有 4 条通道逐步迁移 | gateway 边界 | 中 |
| **P4** | 无用户可配 UI | webui 设置界面遍历任务类型生成配置卡片（Operit 式），配置走 `[auxiliary.<task>]` TOML | webui 层 | 中 |
| **P5** | 全局 1 槽并发粗 | task_runtime 引入 per-task 信号量（Hermes 式），可无限期推迟 | engine 边界 | 高 |
| **P6** | 缺多媒体识别 | IMAGE/AUDIO/VIDEO_RECOGNITION + 能力标志（Operit 式 supportsVision） | gateway 边界 | 高 |

### 3.3 与上一轮方案的关键差异

1. **P0 回调（2026-08-11 复核）**：第二轮曾以“naming 绕过一切基建”为由把 P0 从“建枚举”翻转为“修 naming”；复核证伪该前提的一半（预算 ✅ 记账 ✅），翻转不成立，P0 恢复为“建 FunctionType 枚举 + tier 映射”。
2. **naming 方案**：“迁移到 adapter”方向仍成立（第一轮的“补 provider 键”也不准确——缺的是 adapter 能力整体），但优先级从 P0 降为 P1 改进项。
3. **统一入口 P3、用户可配 UI P4、per-task 信号量 P5、多媒体 P6**：后半段次序不变，仅编号顺延。
4. **教训**：本轮翻转错误的根因是子代理的夸大定性未经复核即采纳；修正措施已写入 deep-code-analyzer v2.2.0 的“复核后落档”铁律（复核义务不区分结论来源）。

### 3.4 红线判断（维持）

- **P0/P1/P2 不碰 engine**：建枚举、naming 迁移与新增任务通道都在 session/gateway 层
- **P2 碰 gateway 边界**：统一入口在 `auxiliary/registry.py`，贴近 gateway 但不碰 engine 核心
- **P4 是唯一碰 engine 的**：per-task 信号量要改 task_runtime.py——这条可以无限期推迟，全局 1 槽不是致命问题

---

## 四、三参照系的精确借鉴点

| 参照系 | 借鉴什么 | 不借鉴什么 |
|---|---|---|
| **Hermes** | `call_llm(task=...)` 四合一入口模式；per-task 信号量；8 层错误恢复链的分层思路 | Hermes 的配置是 YAML，OS 用 TOML——不照搬格式 |
| **Operit** | FunctionType 枚举即数据；两层映射 (configId, modelIndex)；UI 遍历枚举生成卡片；能力标志从配置读取 | Operit 的 Lease/Retire/Release 热更新——OS 是服务端单进程，不需要三段式热更新 |
| **Pi** | 契约约束模式（统一入口靠"必须调用"契约而非编译强制）；Compat 层声明式标记 + 删除路线 | Pi 的 onPayload 链式替换——OS 的辅助任务不需要 payload 替换，只需要路由 |

---

## 五、结论

源码实证修正了两个关键判断：

1. **naming 既非“缺键”也非“裸奔”，而是“遗留传输层”**——预算闸门 ✅、call_kind 记账 ✅、provider adapter ❌（裸 httpx、URL 猜 provider），缺故障转移链与热更，属 P1 改进项（2026-08-11 复核，更正原稿夸大定性）
2. **OS 基建比预想的完善**——call_kind 记账有按任务聚合，auxiliary_budget 是合格的预算闸门，四条通道全部在预算/记账基建之内。不需要"重做"，只需要"补传输层迁移 + 补缺失任务类型 + 补用户可配层"

三参照系的精确借鉴：Hermes 给执行层模式，Operit 给产品形态（用户可配），Pi 给纪律约束（契约模式 + Compat 标记）。落地优先级（2026-08-11 复核后）：枚举与用户可配语义层先行（P0），naming 迁移（P1）与新任务类型（P2）随后，统一入口（P3）与用户可配 UI（P4）渐进推进。
