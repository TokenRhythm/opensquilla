# L3: 检索充分性检查 — 实现 Spec

> **Status**: 实现中
> **依赖**: L2（约束感知检索路由）✅ 已实现
> **Feature flag**: `memory.experimental.sufficiency_check = false`
> **分支**: `feature/constraint-aware-memory`
> **作者**: KunYu + OpenSquilla
> **日期**: 2026-07-29

---

## 1. 设计目标

约束感知检索的最后一环：当检索结果不足时，**告知模型这一事实**，让模型做出知情决策而非盲目基于不充分证据推理。

**核心原则**：不阻塞（D4），只注入元认知提示。模型可以选择：
- 调整关键词重新检索
- 使用 session_search 补充
- 告知用户需要更多上下文

---

## 2. 设计决策（已对齐，2026-07-29）

| # | 决策 | 选择 | 参考模式 |
|---|------|------|---------|
| D1 | 注入位置 | `memory_tools.py` 返回层（结果格式化后追加） | OpenHuman `MemoryAccessSection`（section 追加在结果后） |
| D2 | 语言策略 | 跟随 query 语言（CJK 比例 > 0.3 → 中文） | 和约束 ontology 双语设计一致 |
| D3 | 注入格式 | `<memory_sufficiency_note>` 有界 XML 标记 | Codex `ContextualUserFragment`（有界、可识别、不可变） |
| D4 | 提示强度 | 区分"无结果"(results=0)和"不充分"(0<results<3) | 反转 OpenHuman 静默-section 模式：无结果时反而告知 |
| D5 | 触发条件 | `results < 3 AND intent_confidence > 0.7` | D4 不阻塞原则 + 低置信度不自扰 |

### 2.1 为何不注入到 `retrieval.py` 内部

`retrieval.py` 的 `search()` 返回 `list[MemorySearchResult]`（结构化数据），不负责格式化输出。注入时机应在 `memory_tools.py` 的 `memory_search` 工具函数中——此时结果已格式化为文本，可以直接追加提示。

### 2.2 为何不注入到所有检索路径

`session_search` 是 transcript 全文检索，没有 constraint routing，不适用 L3。L3 仅对 `memory_search`（memory source）路径生效。

### 2.3 L3 与 L2 的关系

L3 消费 L2 的 `classify_query_intent()` 输出（intent + confidence）。实现上：当 L2 或 L3 任一启用时，retriever 都会执行 intent 分类；但 boost 仅在 L2 启用时应用。这意味着 L3 可以独立于 L2 boost 工作，但需要 intent 分类数据。

---

## 3. 注入格式

### 3.1 无结果（results = 0）— 中文

```xml
<memory_sufficiency_note intent="transfer_knowledge" results="0" confidence="0.90">
未找到与当前查询匹配的记忆。
建议：这可能是新话题，使用 web_search 获取外部信息，或向用户确认背景。
</memory_sufficiency_note>
```

### 3.2 无结果（results = 0）— 英文

```xml
<memory_sufficiency_note intent="transfer_knowledge" results="0" confidence="0.90">
No memory results found for the current query.
Suggestions: this may be a new topic — consider web_search for external information, or confirm context with the user.
</memory_sufficiency_note>
```

### 3.3 不充分（0 < results < 3）— 中文

```xml
<memory_sufficiency_note intent="avoid_failure" results="2" confidence="0.85">
当前检索结果（2 条）可能不足以完全覆盖推理需求。
建议：调整关键词重新检索，使用 session_search 获取对话上下文，或告知用户需要更多信息。
</memory_sufficiency_note>
```

### 3.4 不充分（0 < results < 3）— 英文

```xml
<memory_sufficiency_note intent="avoid_failure" results="2" confidence="0.85">
Current retrieval results (2 items) may be insufficient for the current reasoning need.
Suggestions: retry with adjusted keywords, use session_search for conversation context, or ask the user for more background.
</memory_sufficiency_note>
```

### 3.5 充分（results ≥ 3 或 confidence ≤ 0.7）

不注入任何内容。静默通过。

---

## 4. 数据流

```
memory_tools.memory_search(query)
    │
    ▼
retriever.search(query, opts, intent=SearchIntent.TOOL)
    │
    ├── L2/L3: classify_query_intent(query) → (intent, confidence)
    │     ├── 存储 _last_query_intent, _last_query_confidence
    │     └── L2 only: apply_constraint_boost(filtered, intent)
    │
    └── 返回 list[MemorySearchResult]
    │
    ▼
memory_tools 格式化结果
    │
    ▼
L3 检查（sufficiency_check_enabled?）
    │
    ├── 否 → 返回格式化结果（无变化）
    │
    └── 是 → 读取 retriever.last_query_intent / last_query_confidence
           │
           ├── confidence > 0.7 AND results < 3
           │     ├── results = 0 → 注入"无结果"提示
           │     └── results > 0 → 注入"不充分"提示
           │
           └── 否则 → 不注入（静默）
```

---

## 5. 实现清单

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/sufficiency_check.py` | **新增**：核心模块（阈值、语言检测、触发判断、格式化） |
| `src/opensquilla/memory/retrieval.py` | 修改：保留 confidence + 新增 `sufficiency_check_enabled` 参数 + properties |
| `src/opensquilla/tools/builtin/memory_tools.py` | 修改：空结果路径 + 部分结果路径注入 |
| `src/opensquilla/memory/manager.py` | 修改：接线 `sufficiency_check` config |
| `tests/test_sufficiency_check.py` | **新增**：单元测试 |

---

## 6. 测试矩阵

| # | 场景 | 输入 | 预期 |
|---|------|------|------|
| T1 | 无结果 + 高置信度 | count=0, conf=0.85 | 注入"无结果"提示 |
| T2 | 不充分 + 高置信度 | count=2, conf=0.85 | 注入"不充分"提示 |
| T3 | 充分（结果足够） | count=5, conf=0.85 | 不注入 |
| T4 | 低置信度 | count=0, conf=0.5 | 不注入 |
| T5 | 功能关闭 | enabled=False | 不注入 |
| T6 | 中文 query + 无结果 | query="上次那个bug", count=0, conf=0.9 | 中文提示 |
| T7 | 英文 query + 不充分 | query="what was that bug", count=1, conf=0.9 | 英文提示 |
| T8 | 置信度为 None | count=0, conf=None | 不注入（retriever 未分类） |
| T9 | 边界：results=3 | count=3, conf=0.9 | 不注入 |
| T10 | 边界：confidence=0.7 | count=0, conf=0.7 | 不注入（strictly greater） |
| T11 | CJK 检测：混合 | query="Python 错误处理" | 中文（CJK > 30%） |
| T12 | CJK 检测：纯英文 | query="error handling in Python" | 英文 |

---

## 7. 与已有系统的兼容性

| 系统 | 影响 |
|------|------|
| L1 约束标注 | 无影响（L3 独立于 L1） |
| L2 路由增强 | L3 消费 L2 的 classify_query_intent 输出 |
| D9 Provenance Marker | 无影响（独立判断） |
| D12 Compaction Anchor | 无影响 |
| Dream 评分 | 无影响（L3 只影响读取端） |

---

## 8. 后议事项

- 充分性阈值 `results < 3` 是否需要可配置？
- 是否需要根据 intent 类型调整阈值（如 `avoid_failure` 更严格）？
- 是否需要在 note 中包含 top result 的 constraint_type 分布信息？
