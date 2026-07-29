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

## 2. 开源项目写入路径对比

### 2.1 Mem0（arXiv 2504.19413, ~48K Stars, Apache 2.0）

**写入管线**：
```
add() → 事实提取（LLM）→ 向量嵌入 → 检索相似记忆 → AUDN 决策（LLM）
                                                      ├── ADD: 存储新记忆
                                                      ├── UPDATE: 替换相似记忆
                                                      ├── DELETE: 删除冲突记忆
                                                      └── NOOP: 不操作
```

**关键特征**：
- **全 LLM 驱动**：事实提取和 AUDN 决策全部由 LLM 完成
- **无启发式门控**：2026 年论文指出 "Lack of Closed-Form Gates" 是已知限制
- **无约束类型分类**：不区分 fact/event/preference 等，所有记忆平等存储
- **成本**：每次写入至少 2 次 LLM 调用（提取 + AUDN）

**借鉴**：
- ❌ 全 LLM 路径不适合我们的零依赖 + 低成本约束
- ✅ AUDN 循环思想：flush 路径的 candidate 已有类似逻辑
- ✅ 向量相似度去重：我们的 `_RAW_FALLBACK_DEDUPE_MAX_ENTRIES` 有类似意图

### 2.2 Letta / MemGPT（~21K Stars, Apache 2.0）

**写入管线**：
```
Agent 推理循环 → 自主调用 memory 工具
  ├── core_memory_append / core_memory_replace（RAM 层）
  ├── archival_memory_insert / archival_memory_search（冷存储层）
  └── recall_memory_search（缓存层）
```

**关键特征**：
- **Agent 自编辑**：Agent 自己决定何时写入、写什么、写到哪里
- **无分类管线**：没有独立的分类步骤，分类隐含在 agent 的 tool-call 决策中
- **三层架构**：Core（上下文窗口内）/ Recall（可搜索会话历史）/ Archival（长期存储）
- **质量依赖模型**：如果 Agent 没保存，信息就丢了

**借鉴**：
- ❌ 架构完全不同——我们是 passive extraction pipeline，Letta 是 agentic runtime
- ✅ 分层记忆概念：flush（主动提取）+ compaction（隐式总结）有类似分层
- ✅ `CandidateKind` 概念与 Letta 的 core memory 块有相似之处

### 2.3 Cognee（$7.5M Seed, Apache 2.0）

**写入管线**：
```
add() → cognify() 六阶段管线：
  1. classify_documents — 识别文档类型和结构
  2. check_permissions — 权限检查
  3. extract_chunks — TextChunker / LangchainChunker 分块
  4. extract_graph — LLM 提取实体和关系 → 知识图谱三元组
  5. generate_summaries — 生成摘要
  6. embed + commit — 向量嵌入 + 图谱边提交
```

**关键特征**：
- **六阶段管线**：最接近我们的多阶段设计
- **LLM 提取实体**：`extract_graph_from_data` 用 LLM 提取 (subject, predicate, object) 三元组
- **增量处理**：re-run 时只处理新增/更新文件
- **默认零基础设施**：SQLite + LanceDB + Kuzu（文件型）

**借鉴**：
- ✅ 分阶段管线设计：Signal Gate → LLM → Heuristic 是类似的分阶段思想
- ✅ 零依赖默认：与我们的纯标准库原则一致
- ✅ 文档级分类（粗粒度）与 chunk 级分类（细粒度）可以分离

### 2.4 Zep / Graphiti（Apache 2.0）

**写入管线**：
```
Episode 摄入 → 实体提取 → 语义链接 → 社区聚类
  ├── Episode Subgraph（原始事件，含时间戳）
  ├── Semantic Entity Subgraph（实体 + 关系，1024D 嵌入）
  └── Community Subgraph（实体聚类 + 摘要）
```

**关键特征**：
- **双时间戳模型**：事件时间 (T) + 摄入时间 (T')，完整溯源
- **LLM 提取实体**：语义实体从 episode 中提取
- **边失效机制**：新事实可 invalidate 旧边
- **LongMemEval SOTA**：+18.5% 准确率，-90% 延迟

**借鉴**：
- ✅ 时间戳双模型：我们已有 `source_date` + 摄入时间的设计
- ✅ 分层子图：Episode → Semantic → Community 与 raw → chunk → constraint 有类比
- ❌ 全图谱方法：我们的约束类型标注是更轻量级的增强

### 2.5 对比总结

| 项目 | 写入分类方式 | 启发式角色 | LLM 角色 | 约束类型 | 成本 |
|------|------------|-----------|---------|---------|------|
| Mem0 | 全 LLM (A.U.D.N.) | 无 | 决策者 | 无 | 高 |
| Letta | Agent tool-call | 无 | 隐含在推理中 | 无 | 高 |
| Cognee | 分阶段 pipeline | 文档级粗分类 | 实体/关系提取 | 无 | 中 |
| Zep | LLM 语义提取 | 无 | 实体/关系/社区 | 无 | 中-高 |
| **Ours（当前）** | 启发式 + inline marker | 主力（直接索引） | 仅 flush 路径 | **6 种** | **低** |
| **Ours（提案）** | 分层升级 | 快路径 + 门控 | 低置信度升级 | **6 种** | **低-中** |

**核心发现**：没有任何开源项目实现了"启发式优先 + LLM 升级"的约束类型分类管线。我们的 Signal Gate + heuristic + LLM 三层架构在业界是独特设计。

---

## 3. 设计方案：分层升级（Tiered Escalation）

### 3.1 核心思路

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

### 3.2 与当前代码的差异

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

### 3.3 写入路径接入

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

### 3.4 成本估算

假设典型 MEMORY.md 有 50 个 chunk：
- Signal Gate 过滤 ~35%（17 个跳过）
- 启发式 conf >= 0.6 接受 ~40%（13 个直接通过）
- 需要 LLM 升级 ~25%（**~13 个 LLM 调用**）
- 每次调用 ~200 input tokens + ~5 output tokens ≈ 205 tokens
- 总计 ~2,665 tokens per reindex → 成本可忽略

对比 Mem0（每个 chunk 至少 2 次 LLM 调用），我们的方案 LLM 调用量约为 Mem0 的 **1/4 到 1/3**。

### 3.5 置信度体系（不变）

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

## 4. 实现清单

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

## 5. 设计决策记录

| 决策 | 选择 | 理由 | 替代方案 |
|------|------|------|---------|
| 启发式 vs LLM 顺序 | 启发式优先 | 成本敏感；高置信度关键词匹配无需 LLM | LLM 优先（Mem0 模式，成本高） |
| 升级阈值 | 0.6 | 与 L2 boost 阈值对齐；< 0.6 的 chunk 即使分类了也不会被 boost | 0.5（更多 LLM 调用）/ 0.7（更少升级） |
| LLM 失败回退 | 保持启发式结果 | 不因 LLM 故障丢失已有分类 | 返回 (fact, None)（丢失信息） |
| 直接索引路径 LLM 注入 | 可选参数 | 零依赖原则；无 LLM 时行为不变 | 强制依赖（破坏零依赖） |
| 批量 LLM 调用 | 不做（v1） | 增加复杂度；当前 chunk 数量下逐条调用成本可接受 | 批量 prompt（节省 token 但增加解析复杂度） |

---

## 6. Future Work（动态自适应 — 暂不实现）

> 记录为后续讨论项，不在本次实现范围内。

### 6.1 动态方案设计草案

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

### 6.2 参考方向

- **Mem0 的 AUDN 循环**：LLM 判定 ADD/UPDATE/DELETE/NOOP 的决策模式
- **Cognee 的 memify()**：后处理阶段根据使用信号调整边权重
- **Rasa DIET**：intent 分类 + entity 提取的联合模型
- **Confidence calibration**：Platt scaling / isotonic regression 校准启发式置信度
- **分类结果缓存**：相同 chunk hash 不重复分类
- **Lazy classification**：检索时对未分类 chunk 做延迟分类（类似 Cognee 的 lazy cognify）

### 6.3 待讨论问题

1. 动态阈值的粒度：全局 vs 按类型 vs 按用户？
2. 反馈信号的来源：检索点击率？用户手动修正？
3. 成本预算模型：LLM 调用配额如何分配？
4. Embedding 模型依赖：是否引入 `multilingual-e5-small` 等轻量模型？

---

## 7. 与现有架构的兼容性

- **L0（归档搜索）**：不受影响
- **D12（Compaction Anchor）**：不受影响（anchor 在 transcript 层，不涉及 constraint 分类）
- **L2（路由 boost）**：直接受益——更多 chunk 获得 >= 0.6 置信度 → boost 生效范围扩大
- **L3（充分性检查）**：间接受益——分类质量提升 → 检索结果更精准
- **B4（inline marker）**：不受影响——flush 路径仍走 inline marker（conf 0.9）
- **Feature flag**：`_constraint_annotation_enabled` 不变，关闭时整个 L1 是 no-op

---

## 8. A1 审计问题回顾

**朋友审计 A1（第二轮）**：L1 LLM 写入路径断裂

**结论**：🔶 已知边界 → 本方案解决。
- `classify_constraint_sync` 在 `store.py` 中使用，确实没有 LLM 路径
- 本方案通过可选 LLM 注入 + 分层升级解决此问题
- 无 LLM 时行为不变（向后兼容）
