# A1: L1 写入路径设计 — 静态平衡方案

> **Status**: 草案 v1.0（待 KunYu 确认）
> **Date**: 2026-07-29
> **Scope**: L1 constraint classification 在写入路径上的 LLM/启发式平衡设计
> **Non-scope**: 动态自适应路由（记入 Future Work，后续讨论）

---

## 1. 问题陈述

当前 L1 分类器有两条写入路径，质量不对称：

| 写入路径 | 分类方式 | 置信度 | 状态 |
|----------|---------|--------|------|
| **Flush 路径**（session_flush → memory file） | LLM 提取 `FlushCandidate.kind` → inline marker → `parse_inline_constraint()` | 0.9 | ✅ 已实现（B4） |
| **直接索引路径**（`store.index_file()` → `classify_constraint_sync()`） | 纯启发式关键词匹配 | 0.4–0.6 | ⚠️ 质量缺口 |

**核心问题**：直接索引路径（用户手动编辑 MEMORY.md、memory/*.md 等文件后触发 reindex）只走启发式，置信度低 → L2 boost 阈值 0.6 导致大部分 chunk 的 boost 为 1.0（中性）→ L2 路由对直接索引的内容几乎无效。

---

## 2. 设计方案：分层升级（Tiered Escalation）

### 2.1 核心思路

```
写入 chunk
  │
  ├─ ① Frontmatter override ──────────→ (type, 1.0)  [用户显式标注]
  │
  ├─ ② Inline marker (flush path) ───→ (type, 0.9)  [LLM 已分类]
  │
  ├─ ③ Signal Gate ──────────────────→ (fact, None)  [低信号跳过]
  │
  ├─ ④ Heuristic classify
  │     ├─ conf >= 0.6 ──────────────→ (type, conf)  [高置信度接受]
  │     └─ conf < 0.6
  │           ├─ llm_call available ─→ ⑤ LLM classify → (type, 0.8)
  │           └─ llm_call unavailable → (type, conf)  [保持启发式]
  │
  └─ ⑤ LLM classify
        ├─ success ─────────────────→ (type, 0.8)
        └─ failure ─────────────────→ 回退到 ④ 的启发式结果
```

### 2.2 与当前代码的差异

**当前 `classify_constraint()`（async，line 245-282）**：
```python
# 3. LLM classification (if available)
if llm_call is not None:
    result = await llm_classify(text, llm_call)
    if result is not None:
        return result
# 4. Heuristic fallback
return heuristic_classify(text)
```
→ LLM 在启发式**之前**调用（如果可用）。

**提案修改**：
```python
# 3. Heuristic first (fast path)
h_type, h_conf = heuristic_classify(text)
if h_conf >= HEURISTIC_ACCEPT_THRESHOLD:  # 0.6
    return h_type, h_conf

# 4. LLM escalation (only for low-confidence heuristic)
if llm_call is not None:
    result = await llm_classify(text, llm_call)
    if result is not None:
        return result

# 5. Keep heuristic result
return h_type, h_conf
```

### 2.3 写入路径接入

**当前 `store.py` line 838**：
```python
ct, conf = classify_constraint_sync(txt)  # 纯启发式
```

**提案**：`index_file()` 已经是 `async def`，可以调用 async 版本：
```python
# 如果 LLM 可用（通过配置注入），使用 async 分层升级
if self._constraint_llm_call is not None:
    ct, conf = await classify_constraint(txt, llm_call=self._constraint_llm_call)
else:
    ct, conf = classify_constraint_sync(txt)
```

**LLM 注入方式**：`LongTermMemoryStore.__init__()` 接受可选的 `constraint_llm_call: LlmCallFn | None` 参数，由上层 `MemoryManager` 在初始化时注入（复用已有的 LLM provider 连接）。

### 2.4 成本估算

假设典型 MEMORY.md 有 50 个 chunk：
- Signal Gate 过滤 ~35%（17 个跳过）
- 启发式 conf >= 0.6 接受 ~40%（13 个直接通过）
- 需要 LLM 升级 ~25%（**~13 个 LLM 调用**）
- 每次调用 ~200 input tokens + ~5 output tokens ≈ 205 tokens
- 总计 ~2,665 tokens per reindex → 成本可忽略

### 2.5 置信度体系（不变）

| 来源 | 置信度 | 说明 |
|------|--------|------|
| Frontmatter override | 1.0 | 用户显式标注 |
| Inline marker (flush) | 0.9 | LLM 在 flush 时已分类 |
| LLM escalation | 0.8 | 写入时 LLM 分类 |
| Heuristic (high conf) | 0.6 | 关键词强匹配 |
| Heuristic (low conf) | 0.4–0.5 | 弱匹配 / 默认 fact |
| Signal Gate skip | None | 未标注 |

L2 boost 阈值 0.6 不变 → 只有 frontmatter / inline / LLM / 强启发式匹配的 chunk 会获得 boost。

---

## 3. 实现清单

| # | 任务 | 文件 | 复杂度 |
|---|------|------|--------|
| 1 | `classify_constraint()` 改为启发式优先 + LLM 升级 | `constraint_classifier.py` | 低 |
| 2 | `classify_constraint_sync()` 保持不变（无 LLM 时的 fast path） | `constraint_classifier.py` | 无 |
| 3 | `LongTermMemoryStore.__init__()` 接受 `constraint_llm_call` 参数 | `store.py` | 低 |
| 4 | `index_file()` 中根据 LLM 可用性选择 sync/async 分类 | `store.py` | 低 |
| 5 | `MemoryManager` 初始化时注入 LLM call（如果 provider 可用） | `manager.py` | 中 |
| 6 | 新增常量 `HEURISTIC_ACCEPT_THRESHOLD = 0.6` | `constraint_classifier.py` | 低 |
| 7 | 测试：分层升级路径（mock LLM）| `tests/` | 中 |
| 8 | 测试：LLM 不可用时回退到 sync | `tests/` | 低 |

**预估改动量**：~80 行代码 + ~120 行测试。

---

## 4. 设计决策记录

| 决策 | 选择 | 理由 | 替代方案 |
|------|------|------|---------|
| 启发式 vs LLM 顺序 | 启发式优先 | 成本敏感；高置信度关键词匹配无需 LLM | LLM 优先（成本更高） |
| 升级阈值 | 0.6 | 与 L2 boost 阈值对齐；< 0.6 的 chunk 即使分类了也不会被 boost | 0.5（更多 LLM 调用）/ 0.7（更少升级） |
| LLM 失败回退 | 保持启发式结果 | 不因 LLM 故障丢失已有分类 | 返回 (fact, None)（丢失信息） |
| 直接索引路径 LLM 注入 | 可选参数 | 零依赖原则；无 LLM 时行为不变 | 强制依赖（破坏零依赖） |
| 批量 LLM 调用 | 不做（v1） | 增加复杂度；当前 chunk 数量下逐条调用成本可接受 | 批量 prompt（节省 token 但增加解析复杂度） |

---

## 5. Future Work（动态自适应 — 暂不实现）

> 记录为后续讨论项，不在本次实现范围内。

### 5.1 动态方案设计草案

```
chunk 文本
    ↓
Signal Gate ──→ skip ──→ (fact, None)
    ↓ pass
┌─ 快路径：启发式 ──→ conf >= 动态阈值? ──→ 直接使用
│                    ↓ conf < 阈值
├─ 中路径：embedding 相似度 ──→ conf >= 阈值? ──→ 使用
│                           ↓ conf < 阈值
└─ 慢路径：LLM 分类 ──→ 使用

自适应阈值：根据历史准确率调整各路径的 conf 阈值
成本预算：限制 LLM 调用次数/分钟
反馈回路：检索结果点击率 → 反向调整分类置信度
```

### 5.2 参考方向

- **写入决策循环**：由 LLM 判定 ADD/UPDATE/DELETE/NOOP
- **使用信号反馈**：在后处理阶段按实际使用调整权重
- **联合分类模型**：联合执行 intent 分类与 entity 提取
- **Confidence calibration**：Platt scaling / isotonic regression 校准启发式置信度
- **分类结果缓存**：相同 chunk hash 不重复分类
- **Lazy classification**：检索时对未分类 chunk 做延迟分类

### 5.3 待讨论问题

1. 动态阈值的粒度：全局 vs 按类型 vs 按用户？
2. 反馈信号的来源：检索点击率？用户手动修正？
3. 成本预算模型：LLM 调用配额如何分配？
4. Embedding 模型依赖：是否引入 `multilingual-e5-small` 等轻量模型？

---

## 6. 与现有架构的兼容性

- **L0（归档搜索）**：不受影响
- **D12（Compaction Anchor）**：不受影响（anchor 在 transcript 层，不涉及 constraint 分类）
- **L2（路由 boost）**：直接受益——更多 chunk 获得 >= 0.6 置信度 → boost 生效范围扩大
- **L3（充分性检查）**：间接受益——分类质量提升 → 检索结果更精准
- **B4（inline marker）**：不受影响——flush 路径仍走 inline marker（conf 0.9）
- **Feature flag**：`_constraint_annotation_enabled` 不变，关闭时整个 L1 是 no-op

---

## 7. A1 审计问题回顾

**朋友审计 A1（第二轮）**：L1 LLM 写入路径断裂

**结论**：🔶 已知边界 → 本方案解决。
- `classify_constraint_sync` 在 `store.py` 中使用，确实没有 LLM 路径
- 本方案通过可选 LLM 注入 + 分层升级解决此问题
- 无 LLM 时行为不变（向后兼容）
