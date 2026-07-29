# Constraint-Aware Memory: 设计提案

> **Status**: Active development — L0/D12/L1/L2 ✅ implemented, L3 🔶 pending
> **Version**: v1.3 (2026-07-29)
> **Branch**: `feature/constraint-aware-memory`
> **Base**: `origin/main`
> **Date**: 2026-07-29
> **Authors**: KunYu + OpenSquilla
> **Upstream**: https://github.com/opensquilla/opensquilla

---

## 0. 核心命题

> **Compaction 是在资源耗尽时"怎么丢得少"，Memory 是在资源充足时"怎么组织得对"。**

> **压缩 = 在特定目的下，信息效用 preserved 的语义等价变换。**

当前 OpenSquilla 的 memory 系统是一个**文档检索系统**（hybrid search + temporal decay + MMR + evidence-gated promotion）。本提案将其演进为**推理资源管理系统**：不只找"和 query 语义相似的片段"，而是找"能让 agent 正确完成当前推理的信息"。

三个诊断出的结构性缺陷：

1. **目的盲**：压缩/检索不知道结果要给谁用、用来做什么
2. **无自我模型**：不知道下游推理依赖哪些条件
3. **无等效验证**：压缩/检索后不检验"基于这个结果，推理还能不能走通"

---

## 1. 当前架构（代码级基线）

```
┌─────────────────────────────────────────────────────────────────┐
│ 使用层: memory_search tool → 注入 system prompt                 │
├─────────────────────────────────────────────────────────────────┤
│ 检索层: MemoryRetriever                                         │
│   hybrid search (vector 0.7 + FTS5 0.3)                        │
│   temporal decay (half_life=30d, evergreen exempt)              │
│   MMR diversity rerank (λ=0.7, Jaccard similarity)             │
│   source_weights (sessions: 0.92)                               │
├─────────────────────────────────────────────────────────────────┤
│ 索引层: LongTermMemoryStore                                     │
│   SQLite + FTS5 (unicode61 tokenizer)                           │
│   sqlite-vec (optional, L2 normalized)                          │
│   embedding_cache (provider+model+hash keyed)                   │
├─────────────────────────────────────────────────────────────────┤
│ 整合层: Dream (cron-scheduled)                                  │
│   evidence-gated promotion                                      │
│   ranking: 0.35×frequency + 0.30×signal_balance                │
│          + 0.20×source_confidence + 0.15×consolidation          │
├─────────────────────────────────────────────────────────────────┤
│ 文件层: MEMORY.md, memory/**/*.md, USER.md, SOUL.md             │
│   turn_capture → .opensquilla/turns/ (raw, NOT indexed)         │
│   flush → memory/ (pre-compaction sub-agent)                    │
├─────────────────────────────────────────────────────────────────┤
│ Session 层: transcript_entries (active)                         │
│   compacted_transcript_entries (archived, NO FTS index)         │
│   transcript_fts (FTS5, active table only)                      │
└─────────────────────────────────────────────────────────────────┘
```

### 1.1 关键缺口：归档 Transcript 不可达（✅ 已由 L0 修复）

Compaction 后，原始 entries 被移入 `compacted_transcript_entries`。该表：

- ✅ 数据完整保留（content, tool_calls, reasoning_content, timestamps）
- ✅ 有 session_id, session_key, cursor, compaction_id 索引
- ❌ **无 FTS 索引** — 不可全文搜索
- ❌ **无工具暴露** — 无任何 tool path 可达
- ❌ **无 session source sync** — `SessionSourceIndexer` 只读活跃表

模型在 compaction 后**完全不能**搜索、验证、引用原始对话。只能盲目信任摘要。

### 1.2 已有的约束类型胚胎

`session_flush.py` 已在做分类：

```python
CandidateKind = Literal[
    "fact", "event", "preference", "decision", "procedure", "todo", "goal"
]
```

但分类结果只用于决定写入位置，**不参与检索排序**。本提案统一并扩展它。

---

## 2. 设计原则

| # | 原则 | 理由 |
|---|------|------|
| P1 | 每层的"关闭"状态精确等于当前行为 | 实验功能绝不回退 |
| P2 | API 签名不变 | `memory_search`、`memory_save` 保持契约 |
| P3 | 数据只增不改 | 新列/新表；不删不改现有 schema |
| P4 | 三级降级 | 分类失败 → 默认 "fact" → boost 1.0 → 当前行为 |
| P5 | 隐私通过模型认同实现，不通过信息封锁 | 模型应有完整历史访问权；负责任使用是 alignment 问题 |
| P6 | Feature branch only | 不合入 main，直到验证通过且 KunYu 明确批准 |

---

## 3. 四层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ L3: 检索充分性检查 (experimental, default off)                   │
│   问: 检索结果是否覆盖了当前推理所需的约束？                      │
│   做: 不充分时注入元认知提示，不阻塞                              │
├─────────────────────────────────────────────────────────────────┤
│ L2: 约束感知检索路由 (experimental, default off)                 │
│   问: 当前 query 的约束结构是什么？                               │
│   做: 按约束类型匹配度加权排序                                    │
├─────────────────────────────────────────────────────────────────┤
│ L1: 约束类型标注 (experimental, default off)                     │
│   问: 这条记忆是什么类型的推理资源？                              │
│   做: 索引时标注 constraint_type                                 │
├─────────────────────────────────────────────────────────────────┤
│ L0: 归档 Transcript 可搜索 (infrastructure, always on)           │
│   修复: 让模型能搜索被压缩掉的原始对话                            │
│   意义: 为 L1-L3 提供 ground truth 参照物                        │
└─────────────────────────────────────────────────────────────────┘
```

### 层间依赖

```
L0 (infrastructure) ── 无依赖
L1 (annotation)     ── 无依赖（可独立运行）
L2 (routing)        ── 依赖 L1（需要 constraint_type 元数据）
L3 (sufficiency)    ── 依赖 L2（需要路由后的结果）
```

### 实现状态（2026-07-29）

| 层 | 状态 | Commit | Feature Flag | 测试 |
|----|------|--------|-------------|------|
| L0 | ✅ 已实现 | `648628e6` | always on | 回归通过 |
| D12 | ✅ 已实现 | `6038ff55` | `compaction.anchor_enabled` | 20 tests |
| L1 | ✅ 已实现 | `ef5fc037` | `memory.experimental.constraint_annotation` | 55 tests |
| L2 | ✅ 已实现 | `69d7fdd8` | `memory.experimental.constraint_routing` | 43 tests |
| L3 | ✅ 已实现 | `9de891cb` | `memory.experimental.sufficiency_check` | 37 tests |

**全套件**: 278 passed, 6 skipped, 0 failures

---

## 4. Layer 0: 归档 Transcript 可搜索

> **✅ 已实现** (`648628e6`) — FTS5 索引 + 触发器 + `include_archived` 参数 + 存量回填

### 4.1 为什么是必做的基础设施

L0 是 ground truth 参照层。没有它：
- 模型不能验证 compaction 摘要是否准确
- 模型不能在摘要不足时恢复原始推理
- L1-L3 失去验证基础（只能在"摘要的摘要"上操作）

**这不是实验功能，是能力缺失修复。**

### 4.2 Schema 新增

```sql
-- storage.py: 新增 DDL
CREATE VIRTUAL TABLE IF NOT EXISTS compacted_transcript_fts
USING fts5(content, content=compacted_transcript_entries, content_rowid=id);

-- 触发器：与 transcript_fts 同模式
CREATE TRIGGER IF NOT EXISTS compacted_transcript_fts_ai
AFTER INSERT ON compacted_transcript_entries BEGIN
    INSERT INTO compacted_transcript_fts(rowid, content)
    VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS compacted_transcript_fts_ad
AFTER DELETE ON compacted_transcript_entries BEGIN
    INSERT INTO compacted_transcript_fts(compacted_transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS compacted_transcript_fts_au
AFTER UPDATE ON compacted_transcript_entries BEGIN
    INSERT INTO compacted_transcript_fts(compacted_transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO compacted_transcript_fts(rowid, content)
    VALUES (new.id, new.content);
END;
```

### 4.3 搜索 API 扩展

```python
async def search_transcript(
    self,
    query: str,
    session_id: str | None = None,
    limit: int = 20,
    *,
    include_archived: bool = False,  # NEW
) -> list[dict[str, Any]]:
    """Full-text search across transcript entries (active + optionally archived)."""
    results = await self._search_active_transcript(query, session_id, limit)

    if include_archived and len(results) < limit:
        archived = await self._search_compacted_transcript(
            query, session_id, limit - len(results)
        )
        for r in archived:
            r["archived"] = True
            r["compaction_id"] = r.get("compaction_id")
        results.extend(archived)

    return results[:limit]
```

### 4.4 工具暴露

`session_search` 工具增加可选参数 `include_archived: bool = False`。
归档结果在 snippet 中标记 `[archived]`，使模型能区分来源。

### 4.5 存量数据回填

首次启动时（检测到 `compacted_transcript_fts` 不存在）：

```sql
INSERT INTO compacted_transcript_fts(rowid, content)
SELECT id, content FROM compacted_transcript_entries
WHERE content IS NOT NULL;
```

### 4.6 影响范围

| 文件 | 变更 | 风险 |
|------|------|------|
| `src/opensquilla/session/storage.py` | 加 FTS DDL + 扩展 search_transcript | 低 |
| `src/opensquilla/session/manager.py` | 暴露 include_archived 参数 | 低 |
| 数据库迁移 | 新 DDL，增量 | 低 |

---

## 5. Layer 1: 约束类型标注

> **✅ 已实现** (`ef5fc037` + A1 `3cf54191`) — Signal Gate + 启发式优先 + LLM 分层升级 + frontmatter 覆盖 + 置信度保护

**Config gate**: `memory.experimental.constraint_annotation = false`

### 5.1 统一约束类型 Ontology

统一 `FlushCandidate.kind` 和检索导向的新类型：

| constraint_type | 含义 | 典型召回场景 | FlushCandidate.kind 映射 |
|----------------|------|------------|------------------------|
| `fact` | 稳定事实（默认） | 通用召回 | `fact` |
| `event` | 某时发生的事 | "上周做了什么？" | `event` |
| `preference` | 用户稳定偏好 | 输出格式、风格选择 | `preference` |
| `decision` | 已做出的选择 + 理由 | "为什么选 X？" | `decision` |
| `procedure` | 怎么做某事 | "怎么跑测试？" | `procedure` |
| `goal` | 用户目标/任务意图 | "继续"、"接着上次" | `goal`, `todo` |
| `assumption` | 未验证的前提 | 需要验证或修正 | (新增) |
| `constraint` | 硬约束（技术/业务） | 影响方案选择 | (新增) |
| `anti_pattern` | 失败过的方式 + 原因 | 避免重复犯错 | (新增) |
| `pattern` | 可迁移的问题解决结构 | 新任务与旧任务同构 | (新增) |

### 5.2 存储

```sql
ALTER TABLE chunks ADD COLUMN constraint_type TEXT DEFAULT 'fact';
ALTER TABLE chunks ADD COLUMN constraint_confidence REAL DEFAULT NULL;
```

Nullable 列。现有行默认 `'fact'`。无需迁移现有数据。

### 5.3 标注管线

**A1 分层升级**（`3cf54191`）：`classify_constraint()` 改为启发式优先（conf >= 0.6 直接接受，零 LLM 成本），仅低置信度 chunk 升级到 LLM（~25% traffic）。详见 §5.7。

**触发点 A：memory 文件 sync 时**（`LongTermMemoryStore.index_file()`）

```python
if self._experimental.constraint_annotation:
    for chunk in new_chunks:
        ct, confidence = await self._classify_constraint(chunk.text)
        await self._db.execute(
            "UPDATE chunks SET constraint_type=?, constraint_confidence=? WHERE id=?",
            (ct, confidence, chunk.id),
        )
```

**触发点 B：flush candidate 提取时**（已有 `CandidateKind`）

直接映射，无需额外 LLM 调用。

### 5.4 分类方法

**主路径：LLM 分类**

```
Given this memory chunk, classify it into exactly one type:
[fact, event, preference, decision, procedure, goal, assumption, constraint, anti_pattern, pattern]

Chunk:
{chunk_text}

Reply with ONLY the type name.
```

**降级路径：关键词启发式**（零成本、确定性）

```python
def _heuristic_constraint_type(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ("decided", "chose", "选择", "决定")):
        return "decision"
    if any(w in lower for w in ("failed", "don't", "avoid", "失败", "不要")):
        return "anti_pattern"
    if any(w in lower for w in ("must", "cannot", "limit", "必须", "不能")):
        return "constraint"
    if any(w in lower for w in ("prefer", "like", "偏好", "喜欢")):
        return "preference"
    if any(w in lower for w in ("step", "how to", "run", "步骤")):
        return "procedure"
    return "fact"
```

**降级链**：LLM 不可用 → 启发式 → 默认 `"fact"`。

### 5.5 用户可编辑 Frontmatter

```markdown
---
constraint_type: decision
confidence: 0.9
---

We chose SQLite over Postgres because deployment must be zero-dependency.
```

Sync 时解析 frontmatter → 覆盖 LLM 分类结果。

### 5.6 影响范围

| 文件 | 变更 | 风险 |
|------|------|------|
| `src/opensquilla/memory/store.py` | schema 扩展 + 标注逻辑 | 低 |
| `src/opensquilla/memory/types.py` | 新增 ConstraintType | 低 |
| `src/opensquilla/memory/session_flush.py` | CandidateKind → constraint_type 映射 | 低 |
| 数据库迁移 | ALTER TABLE 增量 | 低 |

---

### 5.7 A1 写入路径分层升级（Tiered Escalation）

> **✅ 已实现** (`3cf54191`)：`classify_constraint()` async 版已实现启发式优先 + LLM 升级。
> **⚠️ 已知限制**：`store.py` 直接索引路径仍调用 `classify_constraint_sync`（纯启发式），LLM 注入尚未接通（SPEC_A1 清单 #3-5）。

**管线顺序**（A1 修改后）：

```
写入 chunk
  →
  ├─ ① Frontmatter override ──────────→ (type, 1.0)  [用户显式标注]
  │
  ├─ ② Inline marker (flush path) ───→ (type, 0.9)  [LLM 已分类]
  │
  ├─ ③ Signal Gate ──────────────────→ (fact, None)  [低信号跳过]
  │
  ├─ ④ Heuristic classify
  │    ├─ conf >= 0.6 ──────────────→ (type, conf)  [高置信度接受，零 LLM 成本]
  │    └─ conf < 0.6
  │          ├─ llm_call available ──→ LLM classify → (type, 0.8)
  │          └─ llm_call unavailable → (type, conf)  [保持启发式]
  │
  └─ ⑤ LLM classify
        ├─ success ─────────────────→ (type, 0.8)
        └─ failure ─────────────────→ 回退到启发式结果
```

**阈值**：`_HEURISTIC_ACCEPT_THRESHOLD = 0.6`（与 L2 boost 阈值对齐）

**置信度体系**：

| 来源 | 置信度 | 说明 |
|------|--------|------|
| Frontmatter override | 1.0 | 用户显式标注 |
| Inline marker (flush) | 0.9 | LLM 在 flush 时已分类 |
| LLM escalation | 0.8 | 写入时 LLM 分类 |
| Heuristic (high conf >= 0.6) | 0.6 | 关键词强匹配 |
| Heuristic (low conf < 0.6) | 0.4–0.5 | 弱匹配 / 默认 fact |
| Signal Gate skip | None | 未标注 |

**成本估算**（典型 50 chunk reindex）：Signal Gate 过滤 ~35% → 启发式接受 ~40% → 需 LLM 升级 ~25%（~13 次调用，~2,665 tokens）。对比 Mem0（每 chunk 2 次 LLM），约为其 **1/4**。

**测试**：6 个新测试（含 LLM mock 验证跳过逻辑），全量 172 passed

---

## 6. Layer 2: 约束感知检索路由

> **✅ 已实现** (`69d7fdd8`) — QueryIntent 5 种意图 + 双语启发式 + boost [0.85, 1.8] + D9 Provenance Marker

**Config gate**: `memory.experimental.constraint_routing = false`
**Depends on**: Layer 1

### 6.1 Query 意图分类

> **B6 中文覆盖**（`3cf54191`）：扩展 ~40 个中文关键词 + 否定检测（`_NEGATION_PREFIX_RE`，window=5 字符）+ 疑问句排除（`_INTERROGATIVE_RE`：有没有/是不是/会不会/能不能/可不可以）+ 英文词形变体（crashed/failed/failure）。~30 个新测试。

| query_intent | 触发模式 | 优先约束类型 |
|-------------|---------|------------|
| `continue_task` | "continue", "resume", "接着", "继续" | goal ×1.5, assumption ×1.3, decision ×1.2 |
| `retrieve_rationale` | "why", "reason", "为什么", "原因" | decision ×1.5, constraint ×1.4 |
| `avoid_failure` | "problem", "error", "wrong", "问题", "错误" | anti_pattern ×2.0, constraint ×1.3 |
| `transfer_knowledge` | "similar", "like before", "类似", "有没有经验" | pattern ×2.0, decision ×1.2 |
| `general` | (默认) | 全部 ×1.0（无操作） |

### 6.2 评分修改

```python
# MemoryRetriever.search() 中，hybrid score 计算之后：
if self._experimental.constraint_routing:
    query_intent = classify_query_intent(query)
    boost_map = INTENT_BOOST_MAP.get(query_intent, {})
    for result in raw_results:
        ct = result.metadata.get("constraint_type", "fact")
        result.score *= boost_map.get(ct, 1.0)
    raw_results.sort(key=lambda r: r.score, reverse=True)
```

**关键约束**：boost 范围 `[0.8, 2.0]`。即使分类完全错误，最坏情况是某条结果分数降 20%。结果永远不会被完全抑制。

### 6.3 降级链

```
constraint_routing enabled
  → constraint_annotation disabled → 所有 chunk 是 "fact" → boost 1.0 → no-op
  → 分类 LLM 不可用 → 启发式 → 可能不准 → soft boost（有界）
  → query 意图分类失败 → "general" → 全部 boost 1.0 → no-op
```

**实验功能永远不会让检索结果比当前行为差超过有界因子（0.8×）。**

### 6.4 影响范围

| 文件 | 变更 | 风险 |
|------|------|------|
| `src/opensquilla/memory/retrieval.py` | 约束路由逻辑 | 低 |
| `src/opensquilla/memory/manager.py` | 传递 query_intent | 低 |
| System prompt | memory_search 新增 query_intent 参数说明 | 低 |

---

## 7. Layer 3: 检索充分性检查

> **✅ 已实现** — 元认知提示注入（不阻塞）；触发：results<3 AND intent_confidence>=0.7

**Config gate**: `memory.experimental.sufficiency_check = false`
**Depends on**: Layer 2

### 7.1 机制

检索后、注入 prompt 前：

```python
if self._experimental.sufficiency_check and results:
    sufficiency = await check_retrieval_sufficiency(
        query=query,
        results=results,
        task_context=current_task_context,
    )
    if sufficiency.status == "insufficient":
        injection_prefix = (
            f"[Memory note: Retrieved results may not fully cover the "
            f"constraints for this task. Possibly missing: "
            f"{', '.join(sufficiency.missing[:3])}. "
            f"Consider a more specific memory search, or use "
            f"session_search with include_archived=true for raw transcript.]\n\n"
        )
```

### 7.2 设计决策：提示，不阻塞

充分性检查**永不阻塞**检索结果。只添加元认知前缀，帮助模型决定是否追加搜索。保护"永不阻塞"原则。

### 7.3 成本控制

- 每次搜索最多 1 次额外 LLM 调用
- 仅在 `constraint_routing` 激活且结果 < 3 条时触发
- `SearchIntent.ADMIN` 查询完全跳过

### 7.4 影响范围

| 文件 | 变更 | 风险 |
|------|------|------|
| `src/opensquilla/memory/retrieval.py` | 充分性检查 + 提示注入 | 低 |
| 新增 `src/opensquilla/memory/sufficiency.py` | 检查逻辑 | 低 |

---

## 8. 配置

```toml
# opensquilla.toml

[memory]
# 现有配置不变
auto_capture_enabled = true
flush_pre_compaction = false

[memory.experimental]
# Layer 1: 写入时标注约束类型
constraint_annotation = false
# constraint_model = ""  # 空则用主模型
# constraint_method = "llm"  # "llm" | "heuristic" | "hybrid"

# Layer 2: 检索时按约束类型路由
constraint_routing = false

# Layer 3: 检索充分性元认知提示
sufficiency_check = false
```

Layer 0 无开关，始终启用（基础设施修复）。

---

## 9. 保护了什么 / 伤害了什么 / 优化了什么

### 9.1 保护

| 方面 | 为什么 |
|------|--------|
| Memory vs Compaction 边界 | 约束感知在 memory 层；compaction 不变 |
| 用户可编辑性 | 所有标注通过 markdown frontmatter；用户可覆盖 |
| 现有向量索引投资 | 附加列；embedding cache、FTS5、sqlite-vec 全部复用 |
| Fallback 安全性 | 分类失败 → "fact" → boost 1.0 → 当前行为 |
| Evergreen 豁免 | MEMORY.md 和非日期文件仍不衰减 |
| Dream 门控 | 仍需多次正面信号才能 promote |
| Turn capture 不索引 | `.opensquilla/turns/` 仍是 raw audit，不进入检索 |
| API 契约 | `memory_search`、`memory_save` 签名不变 |
| "永不阻塞"原则 | 无实验层能阻止检索返回结果 |

### 9.2 伤害（接受的权衡）

| 方面 | 为什么 | 严重程度 | 缓解 |
|------|--------|---------|------|
| 写入延迟 | 每 chunk 多一次 LLM 分类 | 中 | 异步、批量、启发式降级 |
| 分类错误传播 | 错误类型 → 错误 boost → 次优排序 | 中 | 有界 [0.8, 2.0]；graceful degradation |
| Schema 复杂度 | chunks 表两个新 nullable 列 | 低 | 附加；无需迁移现有数据 |
| Ontology 僵化 | 10 种类型可能不够细 | 中 | 可通过配置扩展；用户 frontmatter 覆盖 |
| Dream 复杂度 | Promotion scoring 增加新维度 | 中 | 附加项；现有权重保留 |

### 9.3 优化

| 方面 | 为什么 |
|------|--------|
| 信息效用密度 | 同样 token 预算，更多支撑推理的内容 |
| 推理连续性 | Agent 保留"为什么"而不只是"是什么" |
| 错误可发现性 | "检索不充分"从隐性 bug 变成可检测状态 |
| 跨任务迁移 | Pattern 类型支持同构问题识别 |
| Compaction 间接受益 | Obligation 提取可引用结构化 memory 事实 |
| 归档历史可达性 | 模型能验证、引用、从完整对话中学习 |

---

## 10. 版本路线图

| 版本 | 范围 | 门控 | 状态 |
|------|------|------|------|
| v0.6.x | Layer 0 + D12 (Compaction Anchor) | 始终启用 / config | ✅ 已实现 |
| v0.7.0 | Layer 1 + 2 作为 experimental，默认关闭 | Config gate | ✅ 已实现 |
| v0.7.x | 收集约束分类准确率数据 | Telemetry | 🔶 待启动 |
| v0.7.0+ | A1 写入路径分层升级 + B6 中文意图覆盖 | `3cf54191` | ✅ 已实现 |
| v0.7.1 | A1-3 LLM 注入 store 直接索引路径 | `1e77f919` | ✅ 已实现 |
| v0.7.2 | D11 Usage Tracking + D5 Dream 评分增强 | `a07ae662` | ✅ 已实现 |
| v0.7.3 | D10 Dream 增量 Diff（content-hash 去重） | `06cf8e00` | ✅ 已实现 |
| v0.8.0 | 若准确率 > 85%：Layer 1+2 默认开启 | 数据驱动 | 🔶 待启动 |
| v0.9.0 | Layer 3 作为 experimental | Config gate | ✅ 已实现 |
| v1.0.0 | 评估用充分性检查替代 coverage check | 验证 | 🔶 待评估 |

---

## 11. 与现有系统的关系

### 11.1 Compaction

本提案**不修改** compaction。但创建间接改进路径：

- 若 memory 已有结构化 `decision`、`constraint`、`goal` 条目，compaction 的 obligation 提取可引用它们，而非从原始 transcript 正则猜测
- Layer 0（归档搜索）给 compaction 提供 ground truth 参照

### 11.2 Dream

> **✅ 已实现** (`a07ae662`) — constraint_stability 从 L1 annotation 读取（`get_dominant_constraint_types()`），cross_task_relevance 从 D11 `chunk_usage` 表计算

Promotion scoring 增加附加项：

```python
score = (
    0.25 * frequency            # was 0.35
    + 0.25 * signal_balance     # was 0.30
    + 0.15 * source_confidence  # was 0.20
    + 0.10 * consolidation      # was 0.15
    + 0.15 * constraint_stability   # NEW: fact/decision > assumption
    + 0.10 * cross_task_relevance   # NEW: 多少不同 session 召回过
)
```

### 11.3 Flush

`session_flush.py` 的 `CandidateKind` 直接映射到统一 ontology。无行为变更，只是命名统一。

### 11.4 Session Source

`SessionSourceIndexer` 可选包含归档条目（Layer 0 扩展，默认关闭）。

---

## 12. 隐私立场

本提案**明确拒绝**以信息封锁作为隐私机制。

> 隐私保护应通过模型认同和 alignment 实现，而非通过系统层面限制历史可见性。

模型**应当**有完整对话历史的访问权（包括归档 transcript）。负责任地使用这些访问权是 alignment 层面的问题（SOUL.md、安全训练），不是存储或检索层面的问题。

用户想清除历史时，使用显式删除命令（`opensquilla sessions purge`）——这是用户主动行为，不是系统强制限制。

---

## 13. 测试策略

### 实际测试结果（2026-07-29）

| 模块 | 测试数 | 状态 |
|------|--------|------|
| L1 约束类型标注 | 55 | ✅ 全部通过 |
| L2 约束感知检索路由 | 43 | ✅ 全部通过 |
| D12 Compaction Anchor | 20 | ✅ 全部通过 |
| 回归（全套件） | 278 passed, 6 skipped | ✅ 0 failures |

### 计划测试矩阵

| 层 | 测试类型 | 方法 |
|----|---------|------|
| L0 | Unit | FTS 索引创建、search_transcript(include_archived=True) |
| L0 | Integration | Compact session → 搜索归档 → 验证原始文本可达 |
| L1 | Unit | 分类准确率（50 chunk 标注测试集） |
| L1 | Regression | annotation off 时 memory_search 结果不变 |
| L2 | Unit | Boost 计算、降级链 |
| L2 | A/B | 同 query，有/无路由，测量检索精度 |
| L3 | Unit | 充分性检查返回正确状态 |
| L3 | Integration | 不充分提示注入后，模型能据此行动 |
| 全局 | Regression | 所有实验 flag 关闭时，行为与当前完全一致 |

---

## 14. 开放问题

1. **Ontology 粒度**：`decision` 是否应拆分为 `architectural_decision` vs `preference_decision`？推迟到 v0.8 数据评审。

2. **Pattern memory 存储**：跨域模式存在哪里？`memory/patterns/` 目录？还是专用表？推迟到 v0.9。

3. **分类成本**：大规模（1000+ chunks）时，每 chunk LLM 分类可能昂贵。考虑批量分类或 embedding-based zero-shot。推迟到 v0.7.x telemetry。

4. **A1 LLM 注入**：`classify_constraint()` async 版已实现分层升级，但 `store.py` 直接索引路径仍调用 `classify_constraint_sync`（纯启发式）。需 MemoryManager 注入 `constraint_llm_call` 参数（SPEC_A1 清单 #3-5）。

5. **Compaction 集成**：compaction 的 obligation 提取是否应直接查询 memory 的 active constraints？这会耦合两个系统。推迟到 v1.0 评估。

6. **Multi-agent**：多 agent workspace 中，constraint types 是 per-agent 还是共享？当前设计：per-agent（跟随现有 `agent_id` scoping）。

---

## Appendix A: 算法类比（来自 KunYu 笔记）

| 算法 | Memory 类比 | 应用 |
|------|------------|------|
| 外部排序 | 分层存储 + 换入换出 | Context window ↔ long-term store |
| 倒排索引 | 约束类型索引 | `decision` → 所有 decision chunks |
| LRU/LFU cache | 基于价值的衰减 | 使用频率 + 验证状态 |
| B+ tree | 读优化的结构化记忆 | 用户画像、稳定偏好 |
| LSM-tree | 写缓冲 + 合并 | turn_capture → flush → Dream → MEMORY.md |
| K-way merge | 多源检索融合 | vector + FTS + constraint boost + temporal |

## Appendix B: 关键源文件
| 文件 | 角色 |
|------|------|
| `src/opensquilla/memory/store.py` | SQLite + FTS5 + sqlite-vec 存储 |
| `src/opensquilla/memory/retrieval.py` | Hybrid 检索 + temporal decay + MMR + L2/L3 接口 |
| `src/opensquilla/memory/types.py` | MemorySource, SearchMode, SearchIntent |
| `src/opensquilla/memory/sync_manager.py` | Sync 触发器（6 个触发点） |
| `src/opensquilla/memory/session_source.py` | Session → 衍生 .md 文档 |
| `src/opensquilla/memory/session_flush.py` | LLM flush + CandidateKind |
| `src/opensquilla/memory/dream/runner.py` | Cron 定时整合 |
| `src/opensquilla/memory/dream/ranking.py` | Promotion 评分 |
| `src/opensquilla/session/storage.py` | Transcript 存储 + FTS |
| `src/opensquilla/session/compaction.py` | Context 压缩主逻辑 |
| `src/opensquilla/session/compaction_state.py` | 结构化摘要 + obligation |
| `src/opensquilla/memory/constraint_classifier.py` | L1: Signal Gate + LLM/启发式分类 |
| `src/opensquilla/memory/constraint_routing.py` | L2: QueryIntent 分类 + boost + D9 marker |
| `src/opensquilla/memory/sufficiency_check.py` | L3: 检索充分性检查核心模块 |
| `src/opensquilla/tools/builtin/memory_tools.py` | memory_search 工具 + L3 注入点 |
| `src/opensquilla/memory/manager.py` | MemoryManager 配置接线 |
| `src/opensquilla/gateway/config.py` | MemoryExperimentalConfig (L1/L2/L3 flags) |
| `tests/test_memory/test_constraint_annotation.py` | L1: 55 tests |
| `tests/test_memory/test_constraint_routing.py` | L2: 43 tests |
| `tests/test_sufficiency_check.py` | L3: 37 tests |
| `docs/proposals/constraint-aware-memory/SPEC_L0_ARCHIVED_SEARCH.md` | L0 实现规格 |
| `docs/proposals/constraint-aware-memory/SPEC_D12_COMPACTION_ANCHOR.md` | D12 实现规格 |
| `docs/proposals/constraint-aware-memory/SPEC_L1_CONSTRAINT_ANNOTATION.md` | L1 实现规格 |
| `docs/proposals/constraint-aware-memory/SPEC_L2_CONSTRAINT_ROUTING.md` | L2 实现规格 |
| `docs/proposals/constraint-aware-memory/SPEC_L3_SUFFICIENCY_CHECK.md` | L3 实现规格 |
| `docs/proposals/constraint-aware-memory/SPEC_A1_WRITE_PATH_DESIGN.md` | A1 写入路径设计 |
| `docs/proposals/constraint-aware-memory/RESEARCH_B6_CHINESE_INTENT_COVERAGE.md` | B6 中文意图调研 |
| `docs/proposals/constraint-aware-memory/SPEC_A1_3_LLM_INJECTION.md` | A1-3 LLM 注入设计 |
| `docs/proposals/constraint-aware-memory/SPEC_D11_D5_USAGE_DREAM.md` | D11+D5 Usage+Dream 设计 |
| `docs/proposals/constraint-aware-memory/SPEC_D10_INCREMENTAL_DIFF.md` | D10 增量 Diff 设计 |
| `tests/test_memory/test_dream_dedup.py` | D10: 6 tests |
| `src/opensquilla/memory/dream_factory.py` | Dream 工厂 + memory_store 注入 |
| `tests/test_memory/test_usage_tracking.py` | D11+D5: 24 tests |
