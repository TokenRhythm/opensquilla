# L2: 约束感知检索路由 — 实现 Spec

> **Status**: ✅ 已实现并完成跨层验证
> **依赖**: L1（需要 constraint_type 元数据）✅ 已实现
> **Feature flag**: `memory.experimental.constraint_routing = false`
> **分支**: `feature/constraint-aware-memory`
> **作者**: KunYu + OpenSquilla
> **日期**: 2026-07-29

---

## 1. 设计目标

在检索时根据 query 意图和 chunk 约束类型加权排序，让"推理资源"匹配"推理需求"。同时注入 Provenance Marker（D9），让模型知道"为什么这条被选中"。

核心原则（已对齐）：
1. **关闭 = no-op**：flag 关闭时行为与当前完全一致
2. **Boost 范围 [0.85, 1.8]**（D3）：永不完全抑制；最坏情况 = 15% 分数降低
3. **置信度保护**（D2）：confidence < 0.6 → 不应用 boost（最坏 = 中性）
4. **L1 关闭时自动 no-op**：所有 chunk 是 "fact" → 全部 boost 1.0
5. **Provenance Marker（D9）**：注入轻量 XML 标签，~40-50 token/条

---

## 2. Query 意图分类

### 2.1 意图类型（v0.7）

| query_intent | 触发模式 | 优先约束类型（boost） |
|-------------|---------|---------------------|
| `continue_task` | "continue", "resume", "接着", "继续", "上次" | goal ×1.5, decision ×1.2 |
| `retrieve_rationale` | "why", "reason", "为什么", "原因", "怎么回事" | decision ×1.5 |
| `avoid_failure` | "problem", "error", "wrong", "问题", "错误", "失败" | (v0.8: anti_pattern ×1.8, constraint ×1.3) |
| `transfer_knowledge` | "similar", "like before", "类似", "有没有经验" | (v0.8: pattern ×1.8), decision ×1.2 |
| `general` | (默认) | 全部 ×1.0（无操作） |

**v0.7 注意**：`avoid_failure` 和 `transfer_knowledge` 的主要 boost 目标（anti_pattern, pattern）是 v0.8 扩展类型。v0.7 中这两个意图的 boost map 仅包含核心类型（decision ×1.2），效果有限但结构完整。

### 2.2 分类方法

**v0.7：纯启发式**（关键词匹配，零 LLM 成本）

```python
def classify_query_intent(query: str) -> tuple[QueryIntent, float]:
    """按优先级匹配关键词，返回 (intent, confidence)。"""
    # 优先级：avoid_failure > continue_task > retrieve_rationale > transfer_knowledge > general
    # 默认: (QueryIntent.general, 0.5)
```

**v0.8 可选**：LLM 分类（`classify_query_intent_llm`），需要时切换。

---

## 3. Boost 计算

### 3.1 Boost Map

```python
QUERY_INTENT_BOOST: dict[QueryIntent, dict[ConstraintType, float]] = {
    QueryIntent.continue_task: {
        ConstraintType.goal: 1.5,
        ConstraintType.decision: 1.2,
    },
    QueryIntent.retrieve_rationale: {
        ConstraintType.decision: 1.5,
    },
    QueryIntent.avoid_failure: {
        # v0.8: ConstraintType.anti_pattern: 1.8,
        # v0.8: ConstraintType.constraint: 1.3,
    },
    QueryIntent.transfer_knowledge: {
        ConstraintType.decision: 1.2,
        # v0.8: ConstraintType.pattern: 1.8,
    },
    QueryIntent.general: {},
}
```

### 3.2 应用逻辑

```python
def apply_constraint_boost(
    results: list[MemorySearchResult],
    query_intent: QueryIntent,
    *,
    confidence_threshold: float = 0.6,
    boost_min: float = 0.85,
    boost_max: float = 1.8,
) -> list[MemorySearchResult]:
    """Apply constraint-type boost to search results.

    Rules:
    - confidence < threshold → skip (neutral)
    - constraint_confidence is None (Signal Gate skipped) → skip
    - boost clipped to [boost_min, boost_max]
    - Re-sort after boost
    """
```

### 3.3 降级链

```
constraint_routing enabled
  → constraint_annotation disabled → 所有 chunk "fact" → boost 1.0 → no-op
  → query 意图分类失败 → "general" → 全部 boost 1.0 → no-op
  → confidence < 0.6 → 不 boost → 中性
  → confidence is None → 不 boost → 中性
```

**实验功能永远不会让检索结果比当前行为差超过有界因子（0.85×）。**

---

## 4. Provenance Marker（D9）

### 4.1 格式

```xml
<memory_result type="decision" confidence="0.80">
[content]
</memory_result>
```

### 4.2 注入位置

在 `memory_tools.py` 的 `memory_search` 工具结果格式化中：
- 条件：`constraint_routing_enabled` 且 result 有有效 constraint_type（非默认 "fact" 或 confidence ≥ 0.6）
- 在 evidence 文本外包裹 XML 标签
- 单条结果追加 ~40-50 token，可忽略

### 4.3 判断逻辑

```python
def should_add_provenance_marker(result: MemorySearchResult) -> bool:
    ct = result.metadata.get("constraint_type", "fact")
    conf = result.metadata.get("constraint_confidence")
    if conf is None:
        return False  # Signal Gate 跳过，无标注
    if ct == "fact":
        return False  # 默认类型无需标记
    return True
```

---

## 5. 实现步骤

### 5.1 Store: 检索路径返回 constraint 元数据

**`_fts_search`**：SELECT 增加 `c.constraint_type, c.constraint_confidence`

**`_hybrid_search`**：chunk_rows SELECT 增加 `constraint_type, constraint_confidence`

结果构造时填充 `MemorySearchResult.metadata["constraint_type"]` 和 `metadata["constraint_confidence"]`。

### 5.2 constraint_routing.py 新模块

- `QueryIntent` 枚举（5 种）
- `classify_query_intent(query)` → (intent, confidence)
- `QUERY_INTENT_BOOST` 常量
- `apply_constraint_boost(results, intent, ...)` → 排序后的 results
- `should_add_provenance_marker(result)` → bool
- `format_provenance_marker(result, content)` → str

### 5.3 MemoryRetriever 集成

```python
class MemoryRetriever:
    def __init__(self, ..., constraint_routing_enabled: bool = False):
        self._constraint_routing_enabled = constraint_routing_enabled

    async def search(self, query, opts, *, intent):
        # ... 现有逻辑 ...
        # 在 MMR 之前、filtered 排序之后应用 boost
        if self._constraint_routing_enabled:
            query_intent, _ = classify_query_intent(query)
            filtered = apply_constraint_boost(filtered, query_intent)
        # ... MMR, final slice ...
```

### 5.4 memory_tools.py D9 注入

在结果格式化循环中，如果 retriever 有 `constraint_routing_enabled`：
```python
if should_add_provenance_marker(result):
    evidence = format_provenance_marker(result, evidence)
```

### 5.5 Manager 接线

```python
retriever = MemoryRetriever(
    ...,
    constraint_routing_enabled=getattr(
        getattr(cfg, "experimental", None),
        "constraint_routing",
        False,
    ),
)
```

---

## 6. 改动范围

| 文件 | 变更 | 风险 |
|------|------|------|
| `src/opensquilla/memory/constraint_routing.py` | **新文件**：意图分类 + boost + marker | 低 |
| `src/opensquilla/memory/store.py` | 检索 SQL 加 constraint 列 + metadata 填充 | 低 |
| `src/opensquilla/memory/retrieval.py` | MemoryRetriever 加 constraint_routing_enabled + boost | 低 |
| `src/opensquilla/memory/manager.py` | 接线 config flag → retriever | 低 |
| `src/opensquilla/tools/builtin/memory_tools.py` | D9 Provenance Marker 注入 | 低 |
| `tests/test_memory/test_constraint_routing.py` | **新文件** | 低 |

---

## 7. 验收标准（DoD）

- [ ] Store 检索路径返回 `constraint_type` + `constraint_confidence`（metadata）
- [ ] `constraint_routing.py`：QueryIntent 枚举 + 意图分类 + boost 计算 + clipping
- [ ] `MemoryRetriever` 集成 boost（feature flag 控制）
- [ ] flag 关闭时行为不变（回归测试）
- [ ] L1 关闭时所有 chunk "fact" → boost 1.0 → no-op
- [ ] Confidence < 0.6 → 不 boost
- [ ] Confidence is None → 不 boost
- [ ] Boost 值 clipped 到 [0.85, 1.8]
- [ ] D9 Provenance Marker 注入（flag 开启 + 有效标注时）
- [ ] 单元测试：意图分类、boost 计算、marker 格式化、降级链
- [ ] 集成测试：完整 search → boost → format 流程
- [ ] 现有测试全部通过（无回归）

---

## 8. 与 L1/L3 的关系

- **L1**：提供 constraint_type 元数据（L2 的输入）
  - L1 关闭 → 所有 chunk "fact" → L2 boost 1.0 → no-op
  - L1 开启 + L2 关闭 → 分类结果存储但不影响检索
  - L1 开启 + L2 开启 → 完整约束感知检索
- **L3**：与 L2 共享 `classify_query_intent()` 的 request-scoped 输出
  - L3 可独立开启；只有 L2 开启时才应用 constraint boost
  - L3 触发条件：`results < 3 AND intent_confidence >= 0.7`
