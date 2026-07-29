# Constraint-Aware Memory: 对齐记录 2026-07-29

> **Participants**: KunYu + OpenSquilla
> **Context**: DESIGN.md v1.1 设计对齐
> **Status**: ✅ 全部确认（除 D5/D10/D11 后议）
> **Final confirmation**: 2026-07-29 12:50 CST — KunYu 确认 Q1/Q2/Q3 + D1-D9 + D12 + 推进顺序 1→2→3→4

---

## 1. 已确认的设计决策

### D1: Ontology 粒度 → 核心 6 + 扩展 4

**核心 6 类**（v0.7 激活）：
| 类型 | 语义 | 示例 |
|------|------|------|
| `fact` | 客观事实 | "Python 3.12 于 2023-10 发布" |
| `event` | 时间性事件 | "昨天部署了 v2.1" |
| `preference` | 用户偏好 | "喜欢用 TypeScript" |
| `decision` | 已做出的决策 | "选择 SQLite 而非 Postgres" |
| `procedure` | 操作步骤 | "部署流程：build → test → push" |
| `goal` | 目标/意图 | "下周完成 API 重构" |

**扩展 4 类**（保留枚举，v0.8 再激活）：
| 类型 | 语义 | 推迟理由 |
|------|------|---------|
| `assumption` | 隐含假设 | 需要推理链分析，启发式难以准确识别 |
| `constraint` | 硬约束 | 与 assumption 边界模糊，需使用数据验证 |
| `anti_pattern` | 反面模式 | 与 pattern 是同一结构的两面 |
| `pattern` | 可迁移问题结构 | 匹配机制未就绪，只有标签语义 |

**D7 确认**：`todo` 映射到 `goal`（待办 = 未完成的目标）。

### D2: 分类方法 → LLM 主 + 启发式降级 + 置信度保护

```
chunk 写入
  │
  ├─ Signal Gate (D8): <20字 / 纯工具输出 / 心跳 → 跳过，默认 fact
  │
  ├─ LLM 分类（主路径，L1 开启时）
  │     ├─ 成功 → 输出 (type, confidence)
  │     └─ 失败/超时 → 启发式降级
  │
  ├─ 启发式分类（降级路径）
  │     └─ 关键词 + 结构匹配 → 输出 (type, confidence)
  │
  └─ 置信度保护: confidence < 0.6 → 不应用 boost（等价于 L2 关闭）
```

**关键设计原则**：错误分类比不分类更糟。最坏情况是"没有 boost"（中性），不是"错误 boost"（debuff）。

**成本分析**：L1 整体 default off（experimental），成本只在用户主动开启时产生。

### D3: Boost 范围 → [0.85, 1.8]

- 下界 0.85：轻微抑制不相关类型，不完全排除
- 上界 1.8：保守起步，减少错误分类影响半径
- 永不完全抑制（boost > 0）
- 未来可根据使用数据校准

### D4: L3 触发条件

```python
trigger_l3 = (
    len(results) < 3
    and intent_confidence >= 0.7  # 接受真实分类器的最高置信度
    and constraint_routing_enabled
)
```

### D6: 用户覆盖 → frontmatter 保持

```markdown
---
constraint_type: procedure
---
部署流程：build → test → push → verify
```

用户显式标注覆盖自动分类。够用，不增加复杂度。

### D8: Signal Gate ✅

低信号 chunk 跳过 LLM 分类，默认标 `fact`：
- 长度 < 20 字符
- 纯工具输出（无自然语言）
- 心跳/状态消息
- 预估节省 30-40% 分类调用

### D9: Provenance Marker ✅

注入格式（轻量 XML）：
```xml
<memory_result type="procedure" confidence="0.85">
[content]
</memory_result>
```

- 额外 token ~40-50/条，可忽略
- 让模型知道"为什么这条被选中"
- 方便 L3 充分性检查引用
- 无已知 debuff

### D12: Compaction Anchor 机制（新增，来自 KunYu Q3）

**核心思想**：Compaction 不是"被动压缩后能搜回来"，而是"模型主动判断是否需要展开"。

**机制**：
```
Compaction Summary 输出：
  "用户确认了 P10 的 S 束从 4 维降为 3 维 [anchor:session_abc:42]"

模型判断信息不足 → 调用：
  search_transcript(anchor="session_abc:42")
  → 返回原始 entry 全文
```

**实现要点**：
1. Compaction prompt 引导模型在 summary 中嵌入 `[anchor:session_id:entry_index]`
2. `search_transcript` 增加 `anchor` 参数（精确查找模式，非 FTS）
3. System prompt 告诉模型："摘要中有 anchor 标记且信息不足时，可展开原文"
4. `compacted_transcript_entries` 表已有 `session_id` + `compaction_index` = 天然 anchor ID

**比全文搜索好在**：
- 精确（按 ID 取，不需要 FTS 匹配）
- 模型主动（自己判断需不需要展开）
- 轻量（一次精确查找）
- 可组合（多个 anchor，选择性展开）

**优先级**：🔴 短期（L0 的自然延伸）

---

## 2. 后议项

| # | 内容 | 原因 |
|---|------|------|
| D5 | Dream 评分权重重分配 | Dream 是可选功能，不是高优先级 |
| D10 | Dream 增量 Diff 模式 | 同上 |
| D11 | Usage Tracking | 需要更多设计讨论 |

---

## 3. Q1-Q3 对齐结论

### Q1: Pattern 类型时机

**结论**：`pattern` 保留在枚举中（零成本），但：
- L2 路由中 pattern 的 boost **默认不激活**
- 跨 session pattern 匹配作为独立开关 `cross_session_pattern_matching`，默认关闭
- 只有全局 memory 层面可见
- 匹配机制推迟到有足够使用数据后

### Q2: 分类方法成本-精度

**结论**：
- 主路径 LLM，降级路径启发式
- 置信度 < 0.6 → 不 boost（保护机制）
- L1 整体 default off → 成本问题被开关机制自然解决
- 未来可选：embedding zero-shot 作为中间层（复用已有 embedding，零额外 API）

### Q3: L0 对 Compaction

**结论**：两者不矛盾。L0 给的能动性是：
> 模型在 compaction 后如果认为信息不够"锚定"、清晰，可以自己进一步搜索补充。

实现方式不是"告诉模型你可以搜索"（太泛），而是**在 compaction 输出中嵌入精确的展开入口**（anchor）。模型看到 `[anchor:...]` 就知道"这里可以展开"，不需要自己构造搜索 query。

---

## 4. 优先级排序（更新）

```
🔴 短期（v0.6.x - v0.7）:
  L0: 归档 Transcript 可搜索 ← ✅ 已实现
  D12: Compaction Anchor 机制 ← L0 自然延伸
  L1: 约束类型标注（核心 6 类 + Signal Gate）
  L2: 约束感知检索路由（含 Provenance Marker）

🟡 中期（v0.8）:
  扩展 4 类激活（assumption/constraint/anti_pattern/pattern）
  L3: 检索充分性检查
  Embedding zero-shot 分类中间层

🟢 长期（v0.9+）:
  跨 session pattern 匹配
  Dream 增量 Diff
  Usage Tracking + Boost 校准
  Compaction 策略联动
```

---

---

## 6. 设计原则（确认）

1. **关闭 = no-op**：所有 experimental flag 关闭时，行为与当前完全一致
2. **API 签名不变**：`memory_search`/`session_search` 工具签名不变
3. **数据只增不改**：新字段 nullable，不修改已有数据
4. **三级降级**：LLM → 启发式 → 默认 fact
5. **隐私通过认同**：不通过信息封锁，通过模型认同（KunYu 确认）
6. **错误分类保护**：置信度 < 0.6 → 不 boost（最坏 = 中性）
7. **模型主动性**：anchor 机制让模型自己判断是否需要展开（KunYu 提出）

---

## 7. 文档清单

| 文档 | 路径 | 状态 |
|------|------|------|
| 设计提案 | `DESIGN.md` | v1.1（待更新为 v1.2） |
| 2026-07-29 对齐记录 | `ALIGNMENT_2026-07-29.md` | ✅ |
| L0 实现 spec | `SPEC_L0_ARCHIVED_TRANSCRIPT_SEARCH.md` | ✅ 已实现 |
| D12 Compaction Anchor spec | `SPEC_D12_COMPACTION_ANCHOR.md` | ✅ 已实现 |

## 8. 推进计划（已确认）

```
1. D12: Compaction Anchor 实现 ← 当前
2. L1: 约束类型标注实现 spec
3. L2: 约束感知检索路由
4. DESIGN.md → v1.2 统一更新
```
