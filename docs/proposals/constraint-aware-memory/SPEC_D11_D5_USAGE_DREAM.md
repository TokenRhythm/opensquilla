# SPEC_D11_D5: Usage Tracking + Dream 评分增强

> **Status**: 实施 v1.0
> **Date**: 2026-07-29
> **Scope**: D11 chunk 使用统计 + D5 Dream promotion 评分增强
> **Depends on**: A1-3 (constraint_type annotations on chunks)

## Compatibility and lifecycle invariant

- D5 uses the exact pre-D5 formula when neither valid constraint data nor
  usage data exists. “No new signal” is a strict no-op.
- D11 classifies query intent independently when usage tracking is enabled.
- Usage writes are serialized with index mutations, drained on retriever
  shutdown, and orphan rows are removed with replaced/deleted chunks.
- Dream loads D5 signals for every pending evidence entry, not only the
  current scan batch.
- Cancellation after `BEGIN` rolls back before releasing the shared connection
  lock. Runtime reads use the same operation boundary and cannot observe a
  half-reindexed file.
- Reindex preserves usage rows for stable chunk IDs and deletes only IDs
  removed from the edited path; it does not scan or clear the whole usage
  table.
- The bounded fire-and-forget queue logs sampled saturation counts. Dropping
  usage statistics protects retrieval latency and never changes search
  results.

---

## 1. 问题陈述

### D11: Usage Tracking

当前 memory 系统无法回答：哪些 chunk 被召回？在哪种 query intent 下被召回？
没有使用数据，Dream 的 promotion 评分无法区分"被反复验证的事实"和"冷门假设"。

### D5: Dream 评分

DESIGN.md §11.2 已规划新公式：

```python
score = (
    0.25 * frequency            # was 0.35
    + 0.25 * signal_balance     # was 0.30
    + 0.15 * source_confidence  # was 0.20
    + 0.10 * consolidation      # was 0.15
    + 0.15 * constraint_stability   # NEW
    + 0.10 * cross_task_relevance   # NEW
)
```

但当前 Dream 的 `RawDreamCandidate` 来自文件级扫描（`scan_dream_candidates()`），
没有 chunk 级使用统计。需要 D11 提供数据基础。

---

## 2. D11: Usage Tracking 设计

### 2.1 新增表 `chunk_usage`

```sql
CREATE TABLE IF NOT EXISTS chunk_usage (
    chunk_id TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT 'general',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled_at REAL,
    PRIMARY KEY (chunk_id, intent)
);
CREATE INDEX IF NOT EXISTS idx_chunk_usage_chunk ON chunk_usage(chunk_id);
```

**设计决策**：
- 复合主键 `(chunk_id, intent)`：同一 chunk 在不同 intent 下分别计数
- 不记录 query 原文（隐私最小化）
- 不记录 session_id（避免 PII 累积；cross_task 用 intent 多样性近似）
- 与 chunks 同库（事务一致性 + 级联删除）

### 2.2 写入点

在 `MemoryRetriever.search()` 返回结果后，**非阻塞**写入：

```python
# retrieval.py search() 末尾，return k_selected 之前
if k_selected and self._usage_tracking_enabled:
    intent_str = self._last_query_intent.value if self._last_query_intent else "general"
    chunk_ids = [r.chunk_id for r in k_selected]
    # Fire-and-forget: usage tracking must never block search
    asyncio.get_event_loop().create_task(
        self._store.record_chunk_usage(chunk_ids, intent=intent_str)
    )
```

**实现约束**：
- **非阻塞**：search 返回不等待 usage 写入
- **批量 upsert**：`INSERT ... ON CONFLICT(chunk_id, intent) DO UPDATE SET recall_count = recall_count + 1`
- **可降级**：写入失败只记录 warning，不影响搜索结果
- **Feature flag**：`memory.experimental.usage_tracking = false`（默认关闭）

### 2.3 Store 方法

```python
async def record_chunk_usage(self, chunk_ids: list[str], intent: str = "general") -> None:
    """Increment recall count for chunks. Best-effort, never raises."""

async def get_usage_stats(self, paths: list[str]) -> dict[str, dict]:
    """Return {path: {total_recalls, intent_diversity, last_recalled_at}}.
    Aggregates chunk_usage by chunks.path for Dream consumption."""
```

### 2.4 降级链

| 条件 | 行为 |
|------|------|
| usage_tracking disabled | search 不写入 usage（当前行为） |
| 表创建失败 | search 不受影响（best-effort try/except） |
| increment 失败 | warning 日志，search 结果正常返回 |
| 查询失败 | D5 评分回退到 0（无加成） |

---

## 3. D5: Dream 评分增强

### 3.1 桥接：chunk 级 → 文件级

Dream candidates 是文件级（`source_path` = `memory/xxx.md`）。
桥接方式：`get_usage_stats(paths=[candidate.source_path])` 聚合该文件所有 chunk 的 usage。

### 3.2 constraint_stability

从 candidate 对应 chunks 的**主导 constraint_type** 读取：

| constraint_type | stability |
|----------------|-----------|
| fact, decision, constraint | 1.0 |
| preference, procedure, goal | 0.8 |
| event, pattern | 0.5 |
| assumption, anti_pattern | 0.3 |
| (default/unknown/无 annotation) | 0.65 |

### 3.3 cross_task_relevance

```python
cross_task = _clamp_score(
    math.log1p(total_recalls) / math.log1p(10)   # normalized [0, 1]
    * min(1.0, intent_diversity / 3)              # diversity multiplier
)
```

- `total_recalls`：该文件所有 chunk 的总召回次数
- `intent_diversity`：不同 intent 种类数（avoid_failure, continue_task, ...）

### 3.4 新评分公式

```python
def _score(entry, usage_stats=None, constraint_type=None):
    frequency = ...          # 0.25 (was 0.35)
    signal_balance = ...     # 0.25 (was 0.30)
    source_confidence = ...  # 0.15 (was 0.20)
    consolidation = ...      # 0.10 (was 0.15)
    constraint_stability = _CONSTRAINT_STABILITY.get(constraint_type, 0.65)  # 0.15 NEW
    cross_task = ...         # 0.10 NEW (from usage_stats)
    return _clamp_score(sum of weighted terms)
```

### 3.5 向后兼容

- `usage_stats=None` + `constraint_type=None` → 新项为 0，等效旧公式（权重重新归一化）
- 实际上：无 usage 数据时 cross_task=0，无 annotation 时 stability=0.65
- 旧测试不受影响（ranking 函数签名向后兼容）

---

## 4. 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/store.py` | `_ensure_schema` 加 `chunk_usage` 表；`record_chunk_usage()` + `get_usage_stats()` |
| `src/opensquilla/memory/retrieval.py` | `search()` 末尾非阻塞调用 `record_chunk_usage()`；`__init__` 加 `usage_tracking_enabled` |
| `src/opensquilla/memory/manager.py` | 传 `usage_tracking_enabled` config flag 给 retriever |
| `src/opensquilla/gateway/config.py` | `MemoryExperimentalConfig` 加 `usage_tracking` 字段 |
| `src/opensquilla/memory/dream/ranking.py` | `_score()` 加 constraint_stability + cross_task_relevance |
| `src/opensquilla/memory/dream/runner.py` | 调用 `get_usage_stats()` 传给 ranking |
| `tests/test_memory/test_usage_tracking.py` | **新文件**：D11 + D5 测试 |

---

## 5. 测试计划

1. **Schema**: `chunk_usage` 表存在 + 索引
2. **record_chunk_usage**: 幂等增量 + intent 分离
3. **get_usage_stats**: 按 path 聚合 + intent_diversity 计算
4. **search() 触发**: mock store 验证 usage 写入被调用
5. **Dream ranking**: 高 recall + 多 intent → 分数更高
6. **Dream ranking**: assumption (0.3) < fact (1.0) stability
7. **降级**: usage_tracking off → search 不写入
8. **回归**: 所有现有 Dream/retrieval/constraint 测试通过
