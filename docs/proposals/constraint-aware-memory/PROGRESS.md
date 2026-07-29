# Constraint-Aware Memory: 进度跟踪

> **Last updated**: 2026-07-29 (L1 implemented)
> **Branch**: `feature/constraint-aware-memory`

---

## 总览

| 层 | 名称 | 状态 | Commit | Feature Flag |
|----|------|------|--------|-------------|
| L0 | 归档 Transcript 可搜索 | ✅ 已实现 | `648628e6` | always on |
| D12 | Compaction Anchor | ✅ 已实现 | `6038ff55` | `compaction.anchor_enabled` |
| L1 | 约束类型标注 | ✅ 已实现 | pending | `memory.experimental.constraint_annotation` |
| L2 | 约束感知检索路由 | ⬜ 待实现 | — | `memory.experimental.constraint_routing` |
| L3 | 检索充分性检查 | ⬜ 待实现 | — | `memory.experimental.sufficiency_check` |

---

## L0: 归档 Transcript 可搜索 ✅

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/session/storage.py` | `compacted_transcript_fts` FTS5 表 + 触发器 + `search_transcript(include_archived=True)` + 存量回填 |
| `src/opensquilla/session/manager.py` | 暴露 `include_archived` 参数 |
| `src/opensquilla/tools/builtin/session_search.py` | 工具 schema 新增 `include_archived` |

### Commits

```
648628e6 feat(session): make archived transcript entries full-text searchable (L0)
ae769572 docs: add L0 archived transcript search implementation spec
f65304da docs: mark L0 as implemented, update DoD checklist
```

---

## D12: Compaction Anchor ✅

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/session/storage.py` | `compaction_anchor_id` 列 + `extracted_anchors` 列 + anchor 精确查找 |
| `src/opensquilla/session/compaction.py` | `_format_chunk_for_llm(include_anchors)` + `_ANCHOR_INSTRUCTION` + `extract_anchors_from_summary()` |
| `src/opensquilla/session/manager.py` | 传递 `anchor_enabled` + `extracted_anchors` |
| `src/opensquilla/session/models.py` | `SessionSummary.extracted_anchors` 字段 |
| `src/opensquilla/tools/builtin/session_search.py` | 工具 schema 新增 `anchor` 参数 |
| `src/opensquilla/gateway/config.py` | `CompactionLlmConfig.anchor_enabled` |
| `tests/test_session/test_compaction_anchor.py` | 12 个测试 |

### Commits

```
6038ff55 feat(session): implement D12 compaction anchor mechanism
c7cef6fe docs(constraint-aware-memory): add alignment record, Codex comparison, and D12 anchor spec
```

---

## L1: 约束类型标注 🔶

### 已对齐设计决策

| # | 决策 | 内容 |
|---|------|------|
| D1 | Ontology 粒度 | 核心 6 类（v0.7）+ 扩展 4 类（v0.8） |
| D2 | 分类方法 | LLM 主路径 → 启发式降级 → 默认 "fact" |
| D7 | todo → goal | CandidateKind.todo 映射到 goal |
| D8 | Signal Gate | <20字/纯工具输出/心跳 → 跳过分类 |
| D6 | 用户覆盖 | Markdown frontmatter `constraint_type:` |

### 实现步骤

- [x] 1. Schema: `chunks` 表新增 `constraint_type` + `constraint_confidence`
- [x] 2. Types: 新增 `ConstraintType` 枚举 + `CANDIDATE_KIND_TO_CONSTRAINT` 映射
- [x] 3. Classifier: Signal Gate + LLM + 启发式降级 + Frontmatter 覆盖
- [x] 4. Store 集成: `index_file` 路径调用 `classify_constraint_sync`
- [x] 5. Config: `MemoryExperimentalConfig` + `MemoryConfig.experimental`
- [x] 6. Manager: wire `config.experimental.constraint_annotation` → store
- [x] 7. Tests: 55/55 ✅ (Signal Gate + heuristic + LLM mock + frontmatter + store + config + regression)

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/types.py` | 新增 `ConstraintType` 枚举 + `CANDIDATE_KIND_TO_CONSTRAINT` + `CORE_CONSTRAINT_TYPES` |
| `src/opensquilla/memory/constraint_classifier.py` | **新文件**：Signal Gate + LLM + 启发式 + Frontmatter |
| `src/opensquilla/memory/store.py` | Schema 迁移 + `index_file` 集成分类器 |
| `src/opensquilla/memory/manager.py` | wire config flag → store constructor |
| `src/opensquilla/gateway/config.py` | `MemoryExperimentalConfig` + `MemoryConfig.experimental` |
| `tests/test_memory/test_constraint_annotation.py` | **新文件**：55 个测试 |

### 测试结果

```
tests/test_memory/test_constraint_annotation.py: 55 passed
tests/test_session/test_compaction_anchor.py: 20 passed (no regression)
Total: 75 passed
```

---

## L2: 约束感知检索路由 ⬜

### 已对齐设计决策

| # | 决策 | 内容 |
|---|------|------|
| D3 | Boost 范围 | [0.85, 1.8] |
| D9 | Provenance Marker | `<memory_result type="..." confidence="...">` |

### 待实现

1. Query 意图分类（5 种）
2. Boost 计算 + 排序
3. Provenance Marker 注入
4. 降级链

---

## L3: 检索充分性检查 ⬜

### 待实现

1. `check_retrieval_sufficiency()` 函数
2. 元认知提示注入（不阻塞）
3. 成本控制

---

## 后议项

| # | 内容 | 优先级 |
|---|------|--------|
| D5 | Dream 评分权重重分配 | 🟡 中期 |
| D10 | Dream 增量 Diff 模式 | 🟡 中期 |
| D11 | Usage Tracking | 🟡 中期 |
| Q1 | Pattern 跨 session 匹配 | 🟢 长期 |
