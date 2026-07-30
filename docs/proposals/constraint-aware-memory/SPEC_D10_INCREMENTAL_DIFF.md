# SPEC_D10: Dream 增量 Diff（Content-Hash 去重）

> **Status**: 实施 v1.0
> **Date**: 2026-07-29
> **Scope**: Dream candidate 扫描阶段的内容去重
> **Depends on**: evidence store (existing)

> **Implementation invariant update (2026-07-30)**: observation identity is
> `(source_path, SHA-256(full file content))`; the bounded head/tail snippet is
> only LLM input and never defines whether a file changed. Discovery recursively
> follows `memory/**/*.md`. Progress uses `(mtime_ns, normalized_source_path)`,
> while legacy timestamp-only cursor files remain readable. Tied mtimes cannot
> starve deferred files even when evidence loading is unavailable.

## v1.1 semantic identity correction

Deduplication is scoped to an observation identity, not a global content hash:

```python
known_observations: set[tuple[str, str]]  # (source_path, snippet_sha256)
```

- The same source path with the same normalized content is a touch-only
  duplicate and does not increment evidence.
- Identical content in a different path or dated note is an independent
  recurrence. It remains visible so `seen_count`, `source_days`, and source
  diversity preserve their information utility.
- Cursor advancement means “scanned through”, never “all equal hashes have
  been semantically absorbed”.

The implementation and examples below use this observation-scoped identity.

---

## 1. 问题陈述

当前 `scan_dream_candidates()` 对 mtime > cursor 的文件无条件读取全文并创建
`RawDreamCandidate`。如果文件被 touch 但内容未变（编辑器保存、git checkout 等），
会产生无效 candidate，导致：

1. **无效 seen_count 增加**：`update_promotion_evidence` 匹配 claim_sha256 后
   增加 seen_count，但内容并没有"又被独立观察到"
2. **无效 LLM 调用**：ranked candidates 进入 promotion prompt，浪费 token
3. **cursor 推进过快**：max_mtime 被 touch-only 文件推高，可能跳过真正变化的文件

## 2. 设计

### 2.1 Content-hash 去重

在 `scan_dream_candidates()` 中加可选参数 `known_observations: set[tuple[str, str]] | None`：

```python
def scan_dream_candidates(
    workspace: Path,
    *,
    cursor: float,
    max_batch_size: int,
    agent_id: str,
    quarantine_enabled: bool = True,
    known_observations: set[tuple[str, str]] | None = None,
) -> list[RawDreamCandidate]:
    ...
    snippet_sha = _sha256(snippet)
    if known_observations and (rel_path, snippet_sha) in known_observations:
        skipped_count += 1
        continue  # D10: content unchanged since last Dream run
    ...
```

### 2.2 known_observations 来源

`Dream._run_evidence_consolidation()` 在调用 scan 前，从 evidence store 提取
所有已有 entry 的 `snippet_sha256`：

```python
evidence = load_evidence_store(self.workspace)
known_observations = {
    (entry.source_path, entry.snippet_sha256)
    for entry in evidence.entries.values()
    if entry.source_path and entry.snippet_sha256
}
```

### 2.3 语义正确性

- **跳过 = 内容未变**：snippet_sha256 匹配意味着文件内容（normalize 后）完全相同
- **seen_count 不增加**：touch-only 不算"独立观察"，这是正确的
- **cursor 仍推进**：即使所有文件被跳过，cursor 仍推进到 max_mtime
  （避免下次 Dream 重复扫描同一批文件）

### 2.4 DreamResult 追踪

新增 `files_skipped_unchanged: int = 0` 字段，记录跳过的文件数。
`_emit_log()` 中输出此字段。

### 2.5 降级链

| 条件 | 行为 |
|------|------|
| known_observations=None | 当前行为（不去重） |
| evidence store 为空 | known_observations=空集 → 不去重 |
| evidence store 读取失败 | known_observations=空集 → 不去重 |

### 2.6 Cursor high-watermark invariant

The scanner returns a structured batch containing candidates, unchanged-file
counts, and a safe cursor high-water mark:

- If every changed candidate fits in the batch, hash-equivalent files may
  advance the cursor because their information utility is already represented
  by the evidence store.
- If `max_batch_size` defers any changed candidate, the high-water mark must not
  move past the selected candidate batch. A newer unchanged file must never
  cause a real information change to be skipped.
- Dry runs never persist the cursor.

This makes content-hash dedup a semantics-preserving transformation: duplicate
observations are discarded, while unprocessed information-bearing changes
remain reachable.

## 3. 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/dream/candidates.py` | `scan_dream_candidates` 加 `known_observations` 参数 |
| `src/opensquilla/memory/dream/runner.py` | 提取 known_observations + 传给 scan + DreamResult 新字段 |
| `tests/test_memory_dream_runner.py` | D10 去重测试 |

## 4. 测试计划

1. **Unit**: same-path known observation skips touch-only content
2. **Unit**: same hash in a different path remains a recurrence
3. **Unit**: scan without known observations processes all
4. **Integration**: Dream run with unchanged files → files_skipped_unchanged > 0
5. **Regression**: existing Dream tests pass unchanged
