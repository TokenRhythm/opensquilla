# S5 深挖报告：SquillaRouter 路由算法（含多模型 Ensemble 与省钱机制）

> 深挖目标：SquillaRouter 路由算法 + 多模型路由（llm_ensemble）判断与省钱机制
> 输入材料：`C:/Users/chine/Downloads/opensquilla-chat-路由分析情况-2026-08-11.md`（3890 条调用日志分析，¥251.95，2026-07-31 ~ 08-07）
> 验证方式：对对话记录中的每一处源码论断，逐一与 `src/opensquilla/` 源码对照
> 分析日期：2026-08-11

---

## 一、符号追踪表（文件论断 → 源码定位）

| # | 对话中的论断 | 源码位置 | 验证结论 |
|---|---|---|---|
| 1 | 档位空间只有 c0–c3，无 c4 | `router_tiers.py` L9 `TEXT_TIERS = ("c0","c1","c2","c3")`，`HIGHEST_TEXT_TIER="c3"` | ✅ 精确命中（含 t0-t3 遗留别名 L14-19） |
| 2 | 置信度闸门 `confidence_gate`，阈值 0.5 | `engine/routing/policy.py` L213-248，`base_threshold=0.5`（L232） | ✅ 精确命中 |
| 3 | 高档位有 margin 惩罚 0.05 | L233 `confidence_high_tier_margin=0.05`，L245 `cutoff = threshold - margin` | ✅ 精确命中 |
| 4 | 低置信度落回 `default_tier` | L246-247：`gate_confidence < cutoff` 且 tier≠default → 返回 default_tier | ✅ 精确命中 |
| 5 | 分类器无效档位兜底 | `engine/steps/squilla_router.py` L1289-1297：`tier_name not in tiers` → 落 default，`source="default"` | ✅ 精确命中 |
| 6 | 大上下文地板 T3/T2 | `policy.py` L534-543 `large_context_min_tier`：T3 地板或上下文占比 → c3；T2 → c2 | ✅ 精确命中 |
| 7 | 投诉升级 | `policy.py` L260-292 `complaint_upgrade`：COMPLAINT_TERMS + 160 字符上限，从 pre-gate/上轮最高档起升 | ✅ 精确命中 |
| 8 | 反降级 | `policy.py` L301-316 `anti_downgrade`：KV 缓存窗口内不降档 | ✅ 精确命中 |
| 9 | 预算闸门 warn/cap/suspended | `policy.py` L628-689 `budget_gate`：action=warn 不改档、cap 强制降档、未知花费 suspended | ✅ 精确命中 |
| 10 | 启发式分类器置信度刻意压 0.55–0.60 | `engine/routing/heuristic.py` L36-49 docstring + 常量区：heavy→c3@0.60、code→c2@0.60、short→c0@0.55、medium→c1@0.55、borderline→c1@0.40 | ✅ 精确命中 |
| 11 | 预检：aggregator ready / proposer 名额 / 预算 | `provider/ensemble.py` L1429-1504：`member.ready` 检查、`project_provider_message_count` 预算投影 | ✅ 命中（对话称 L1429-1552，实际预检段至 ~L1504，范围略缩） |
| 12 | self_learning 框架已预留 | `squilla_router/self_learning/` 14 文件：orchestrator/train/alignment/feedback/promotion/gates | ✅ 命中 |

**验证总评**：对话记录中 12 处关键源码论断，11 处精确命中，1 处行号范围略有出入但逻辑一致。该记录的技术准确性可评为 **高**。

---

## 二、路由决策调用链（全链路）

```
用户消息
  │
  ▼
gateway/channel_dispatch.py ── 通道分发（WS/RPC/Telegram/Feishu…）
  │
  ▼
gateway/model_routing.py  L533-549  capture_model_routing_config()
  │   每轮 turn 接受时从 live config 深拷贝 squilla_router 子树做快照
  │   （快照机制 = 改配置文件不热生效，需重启 gateway）
  ▼
engine/steps/squilla_router.py  ── 路由步骤（回合前管线最后一环）
  │   ├─ ① strategy.classify() 分类（v4_phase3 ML / heuristic 启发式）
  │   ├─ ② 无效档位兜底 → default_tier（L1289-1297）
  │   └─ ③ 策略链执行（policy.py）
  │        confidence_gate → complaint_upgrade → anti_downgrade
  │        → capability_gate → bind → large_context_floor → provider_mismatch
  │        → budget_gate（最后一道，只降不升）
  ▼
RoutingDecision{tier, model, confidence, source}
  │
  ├── 单路由路径：直接绑 tier 模型 → engine/agent.py 状态机 → provider.chat()
  │
  └── 多路由路径（ensemble.enabled=true 时包裹）：
       runtime.py L7024-7120 用 EnsembleProvider 包裹单路由选定的 provider
       → validate_chat_request（禁图硬拦截）
       → 预检（aggregator ready / proposer 名额 / 预算投影）
       → 4×proposer 并行提案 → quorum 早停 → 草稿截断
       → 1×aggregator 融合 → 失败降级 fallback_single
```

---

## 三、路由策略状态机（7 道闸门，固定顺序）

```
   分类器输出 (tier, confidence)
        │
        ▼
┌─ confidence_gate ── 低置信度 → default_tier（高档需更高置信度，margin 0.05）
│
▼
┌─ complaint_upgrade ── 检测到抱怨词 → 升档（从 pre-gate/上轮最高档起）
│
▼
┌─ anti_downgrade ── KV 窗口内上轮高档 → 本轮不降
│
▼
┌─ capability_gate ── 缺视觉/窗口不足 → 强制抬升（默认 off）
│
▼
┌─ bind ── 记录 routing trail，绑定最终档位模型，协调 thinking/prompt
│
▼
┌─ large_context_floor ── 材料 token 超 T2/T3 地板 → 强制抬到 c2/c3
│
▼
┌─ provider_mismatch ── provider 不匹配 → flag 或 veto（默认 flag）
│
▼
┌─ budget_gate（最后）── 会话累计花费超限 → warn（不改档）/ cap（强制降档）
│                     未知花费 → suspended（绝不基于未知成本行动）
▼
   最终 tier → 模型绑定 → 执行
```

**关键设计**：预算闸门是唯一"只降不升"的闸门，必须放在最后——前面所有闸门表达的是"能力或偏好"（可升档），预算表达的是"硬约束"（只降档），顺序错位会导致升档后再被预算压回、产生抖动。

---

## 四、复杂度分析

### 4.1 单路由（squilla_router）
- **空间复杂度**：O(1)——每轮只需分类器输出 + 上轮 tier + 会话花费计数。
- **时间复杂度**：O(1) 决策（分类器推理开销恒定，与输入长度弱相关）。
- **每轮 LLM 调用**：1 次（分类器本身是本地 ONNX/LightGBM，不产生 API 费用）。

### 4.2 多路由（llm_ensemble）
- **空间复杂度**：O(N·L)——N 个 proposer 草稿全部驻留内存，L 为草稿长度；聚合器上下文 = 原始对话 + 全部草稿。
- **时间复杂度**：并行 O(1) 墙钟时间（asyncio 全量并行），但总计算量 O(N·L)。
- **每轮 LLM 调用**：N 提案 + 1 聚合 = **5 次**（用户配置 4 proposer + 1 aggregator）。
- **成本下界**：≥ 5 倍单路由输入 + 5 倍输出——结构性成本，算法无法消除。

### 4.3 省钱机制的复杂度权衡
- quorum 早停：最坏情况等待全部 N 个 proposer（门槛高时），平均情况提前终止。用户配置 `min_successful_proposers=1` → 第 1 个成功即掐断其余 3 个，平均等待时间 ≈ 最快 proposer 耗时。
- 草稿截断：O(N·L) 的等分截断，防止聚合器窗口溢出——截断越狠，聚合质量越低，是质量/成本的显式 trade-off。

---

## 五、数据验证（对话记录中的真实调用日志）

### 5.1 总账结构

| 口径 | 数值 |
|---|---|
| 全窗口总成本 | ¥251.95（3890 次调用，7/31–8/7） |
| 多路由占比 | ¥196.88 = **78.1%** |
| 单路由占比 | ¥55.07 = 21.9% |
| 多路由轮均 | ¥0.551（中位 ¥0.342，max ¥8.59） |
| flash 单步均 | **¥0.0076**（n=1120） |
| 倍数 | 多路由轮均 = flash 单步 **73 倍** = 单路由回合 **3 倍** |

### 5.2 机制对账（数据 vs 算法）

| 算法论断 | 数据证据 | 判定 |
|---|---|---|
| quorum 达标即掐断在途 | 同轮 proposer 完成时间差中位 **0.1s**，>10s 的轮仅 4/350 | ✅ 早停真实生效 |
| 失败不炸轮、失败零成本 | 28% 轮含失败 proposer（101/357），但 95% 正常出 aggregator；**183 次失败调用总成本 ¥0.0000** | ✅ 止损有效 |
| 整轮失败降级单模型 | 5 次 `ensemble.fallback_single`、12 轮无结局 | ✅ 兜底存在 |
| 每轮 4 proposer | proposer 行数呈 4 的倍数分布（4/8/12/16…） | ✅ 与配置一致 |

### 5.3 三个数据逼出的黑洞（对话记录的新发现）

1. **proposer 输出失控是最大泄漏点**：glm-5.2 烧掉 ¥112.43（44.6%），其中 **10 次打满 128K 上限的 runaway 调用 = ¥36.43**；2.5% 的轮（9/357）贡献 ensemble 成本的 22.1%。一次 glm runaway ≈ 500 次 flash 单步。
2. **聚合器是第二大支出**（≈¥39，占 ensemble ~20%）：qwen3.8-max 当 aggregator 烧 ¥23.62，吃全部草稿，输入 ¥12/M、输出 ¥36/M。
3. **单路由省钱的锚是缓存命中率**：flash 吃 100M tokens 只花 ¥13.74，缓存读 90.2M/输入 98.3M ≈ **92% 命中**（缓存价 ¥0.02/M）。有效输入价 0.058 ¥/M，比标价低 43 倍。反事实计价：全 qwen ¥2638（10.5×）、全 glm ¥1780（7.1×）、全 pro ¥643（2.6×）、**全 flash ¥214（0.85×，比实际便宜 ¥38）**。

---

## 六、两套路由的省钱算法对比（源码验证版）

| 省钱手段 | 单路由 | 多路由 | 源码依据 |
|---|---|---|---|
| 分类降档到便宜模型 | ✅ 核心 | ❌ 无（全跑） | heuristic.py / v4_phase3.py |
| 置信度门槛压制高档 | ✅ margin 0.05 | ❌ | policy.py L232-247 |
| 会话预算上限 warn/cap | ✅ budget_gate | ❌ 无会话级预算 | policy.py L628-689 |
| 预检不通过零花费 | — | ✅ 最强止损 | ensemble.py L1429-1504 |
| Quorum 达标即取消在途 | — | ✅ 核心 | ensemble.py（successful+pending < min → cancel） |
| 草稿截断省上下文费 | — | ✅ 三重约束等分截断 | ensemble.py `_cap_candidates_to_joint_budget` |
| 失败降级回单模型 | — | ✅ fallback_single | ensemble.py `all_failed_policy` |
| 省钱量化上报 | ✅ savings_pct | ✅ 逐候选 billed_cost | runtime.py `_compute_route_input_savings_usd` |

**一句话总结**：单路由省钱靠"**选便宜的**"（分类+闸门+预算封顶，结构性省钱）；多路由省钱靠"**别白花**"（预检+quorum 掐断+截断+超时，止损型省钱）。多路由永远不应该是省钱工具。

---

## 七、替代方案对比（"质量-价格平衡点"的概念框架）

对话最后讨论了"质量不是最高、价格不是最低但刚刚好"的平衡点，结合源码能力评估各方案：

| 方案 | 机制 | 现状 | 平衡点定位 |
|---|---|---|---|
| 单路由（现状） | 分类器定档 + 7 闸门 | ✅ 已启用 | 价格轴上的"最低可接受质量" |
| 多路由（配置未启用） | 4 提案 + 1 聚合 | ⚠️ `enabled=false` | 质量轴上的"最低可接受价格" |
| self_learning 闭环 | 返工标签反向校准分类器 | ⚠️ 框架齐全未启用 | 最正统的平衡点逼近路径（已预留） |
| 混合路由（第三态） | flash 快速答 + 质量闸门决定是否 qwen 重答 | ❌ 不存在 | 介于两者之间，可能最接近"刚刚好" |
| 预算感知分流 | budget_gate 从"封顶"升级为"动态调节平衡点" | ❌ 未实现 | 把预算约束变成平衡点调节器 |
| per-agent 绑定 | AgentRoutingConfig.default_tier/max_tier | ⚠️ schema 有、**无 consumer** | 按 agent 场景硬绑（上游标注的 follow-up） |

---

## 八、配置速查（关键参数与建议值）

| 参数 | 位置 | 默认 | 用户当前 | 建议 |
|---|---|---|---|---|
| `default_tier` | `[squilla_router]` | c1 | **c1**（8/7 已从 c3 改） | ✅ 保持，符合设计意图 |
| `confidence_threshold` | `[squilla_router]` | 0.5 | 0.5 | 保持 |
| `confidence_high_tier_margin` | `[squilla_router]` | 0.05 | 默认 | 保持（结构性压制高档） |
| `self_learning.enabled` | `[squilla_router.self_learning]` | false | false | 可试点开启校准分类器 |
| `[squilla_router.budget]` | — | off | 未启用 | **建议启用**：会话级封顶是单路由省钱主力 |
| `llm_ensemble.enabled` | `[llm_ensemble]` | false | false | ✅ 省钱最优解；高风险任务临时开 |
| `min_successful_proposers` | `[llm_ensemble]` | 3 | 1 | 若启用：1 即省钱最优，但聚合视角单一 |
| proposer 输出上限 | 无此配置 | — | 无 | **第一优化点**：堵 glm runaway 需在 proposer 层加 max_tokens |
| 聚合器草稿截断预算 | ensemble 内部 | — | 默认 | **第二优化点**：收紧可压 aggregator ~20% 成本 |

---

## 九、结论

对话记录中的技术论断经源码验证**高度准确**（12/12 命中，含 1 处行号范围微调）。SquillaRouter 的架构本质是：

1. **单路由 = 精确打击**：分类器把任务映射到"够用的最低档"，7 道闸门管理"猜错的代价"，省钱靠便宜模型 × 高缓存命中 × 预算封顶三要素。
2. **多路由 = 冗余投保**：不做选择、全跑再融合，省钱靠止损（预检零花费、quorum 掐断、截断、超时），结构性成本 ≥5 倍单路由不可消除。
3. **平衡点 = 任务分流规则**：返工成本 > 多路由溢价（≈¥0.38/轮）→ 走多路由买确定性；反之走单路由吃缓存红利。两套方案不是对立，而是同一平衡问题的两个逼近方向。
4. **两个明确的优化出血口**：proposer 无输出上限（glm runaway ¥36.4 = 总成本 14.4%）与聚合器草稿预算（~20% ensemble 成本）——堵上这两口，多路由轮均有望从 ¥0.55 压到 ¥0.3 量级。

---

## 参考源码文件

- `src/opensquilla/router_tiers.py` — 档位定义与规范化
- `src/opensquilla/engine/routing/policy.py` — 7 道策略闸门
- `src/opensquilla/engine/routing/heuristic.py` — 启发式分类器（降级路径）
- `src/opensquilla/engine/steps/squilla_router.py` — 路由步骤主流程
- `src/opensquilla/provider/ensemble.py` — 多模型集成 provider
- `src/opensquilla/squilla_router/v4_phase3.py` — V4 Phase3 ML 分类器适配
- `src/opensquilla/squilla_router/controller.py` — 档位→thinking/prompt 控制器
- `src/opensquilla/squilla_router/self_learning/` — 自我学习框架（14 文件）
- `src/opensquilla/gateway/model_routing.py` — 路由配置快照机制
