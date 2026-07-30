# Constraint-Aware Memory: 进度跟踪

> **Last updated**: 2026-07-30 (structural invariant hardening)
> **Branch**: `feature/constraint-aware-memory`

---

## 2026-07-30 结构性加固

- L3 intent/confidence 改为 request-scoped outcome，消除并发串线。
- compaction index 由数据库 identity 决定，并在持久化事务内复核。
- anchor 在写入前校验 compaction 与 entry 范围。
- Dream 使用完整内容 hash、递归扫描和 `(mtime_ns, path)` 复合 cursor。
- `MEMORY.md` apply 使用短临界区、内容 CAS 与原子替换。
- fallback/previous summary 受 token budget 约束，优先恢复窗口可用性。
- full fork 的 SQLite lineage copy 与 child row 创建收敛为单事务。
- constraint classification 增加有界并发与 chunk-hash cache；usage
  后台写入有上限；L3 advisory 考虑证据强度与重复度。

---

## 总览

| 层 | 名称 | 状态 | Commit | Feature Flag |
|----|------|------|--------|-------------|
| L0 | 归档 Transcript 可搜索 | ✅ 已实现 | `648628e6` | always on |
| D12 | Compaction Anchor | ✅ 已实现 | `6038ff55` | `compaction.anchor_enabled` |
| L1 | 约束类型标注 | ✅ 已实现 | `ef5fc037` | `memory.experimental.constraint_annotation` |
| L2 | 约束感知检索路由 | ✅ 已实现 | `69d7fdd8` | `memory.experimental.constraint_routing` |
| L3 | 检索充分性检查 | ✅ 已实现 | `9de891cb` | `memory.experimental.sufficiency_check` |
| A1 | L1 写入路径分层升级 | ✅ 已实现 | `3cf54191` | `memory.experimental.constraint_annotation` |
| B6 | L2 中文意图覆盖 | ✅ 已实现 | `3cf54191` | `memory.experimental.constraint_routing` |

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
c7cef6fe docs(constraint-aware-memory): add alignment record and D12 anchor spec
```

---

## L1: 约束类型标注 ✅

### 已对齐设计决策

| # | 决策 | 内容 |
|---|------|------|
| D1 | Ontology 粒度 | 核心 6 类（v0.7）+ 扩展 4 类（v0.8） |
| D2 | 分类方法 | A1 分层升级：启发式优先 → LLM 升级（低置信度）→ 默认 "fact" |
| D7 | todo → goal | CandidateKind.todo 映射到 goal |
| D8 | Signal Gate | <20字/纯工具输出/心跳 → 跳过分类 |
| D6 | 用户覆盖 | Markdown frontmatter `constraint_type:` |

### 实现步骤

- [x] 1. Schema: `chunks` 表新增 `constraint_type` + `constraint_confidence`
- [x] 2. Types: 新增 `ConstraintType` 枚举 + `CANDIDATE_KIND_TO_CONSTRAINT` 映射
- [x] 3. Classifier: Signal Gate + 启发式优先 + LLM 分层升级 (A1) + Frontmatter 覆盖
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

### A1: 写入路径分层升级（`3cf54191`）

**设计文档**：`SPEC_A1_WRITE_PATH_DESIGN.md`

**改动**：`classify_constraint()` 从"LLM 优先 → 启发式降级"改为"启发式优先 → LLM 升级"：
- `_HEURISTIC_ACCEPT_THRESHOLD = 0.6`：启发式 conf >= 0.6 直接接受，零 LLM 成本
- 仅低置信度 chunk（~25%）升级到 LLM
- LLM 不可用/失败时回退到启发式结果
- Inline marker / frontmatter 仍绕过 LLM（不变）

**已知限制**：`store.py` 直接索引路径仍调用 `classify_constraint_sync`（纯启发式），LLM 注入尚未接通（SPEC_A1 清单 #3-5）

**测试**：6 个新测试（含 LLM mock 验证跳过逻辑），全量 172 passed

---

## L2: 约束感知检索路由 ✅

### 已对齐设计决策

| # | 决策 | 内容 |
|---|------|------|
| D3 | Boost 范围 | [0.85, 1.8]，永不完全抑制 |
| D9 | Provenance Marker | `<memory_result type="..." confidence="...">` |
| D4 | L3 触发条件 | results < 3 AND intent_confidence >= 0.7 |

### 实现步骤

- [x] 1. Store 检索路径返回 constraint_type + constraint_confidence 元数据
- [x] 2. `constraint_routing.py` 新模块：QueryIntent 枚举 + 意图分类 + boost 计算 + marker
- [x] 3. `MemoryRetriever` 集成 boost（feature flag 控制）
- [x] 4. D9 Provenance Marker 注入到 `memory_tools.py`
- [x] 5. Manager 接线 config flag → retriever
- [x] 6. Tests: 43/43 ✅（intent + boost + marker + degradation + regression）

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/constraint_routing.py` | **新文件**：QueryIntent + 意图分类 + boost + D9 marker |
| `src/opensquilla/memory/store.py` | FTS/hybrid 检索 SQL 加 constraint 列 + metadata 填充 |
| `src/opensquilla/memory/retrieval.py` | MemoryRetriever 加 `constraint_routing_enabled` + boost 调用 + 公开属性 |
| `src/opensquilla/memory/manager.py` | 接线 config flag → retriever constructor |
| `src/opensquilla/tools/builtin/memory_tools.py` | D9 Provenance Marker 注入 |
| `tests/test_memory/test_constraint_routing.py` | **新文件**：43 个测试 |

### 测试结果

```
tests/test_memory/test_constraint_routing.py: 43 passed
tests/test_memory/test_constraint_annotation.py: 55 passed (no regression)
tests/test_session/test_compaction_anchor.py: 20 passed (no regression)
Total: 278 passed, 6 skipped, 0 failures
```

### B6: 中文意图覆盖（`3cf54191`）

**调研文档**：`RESEARCH_B6_CHINESE_INTENT_COVERAGE.md`

**改动**：
- 4 种 intent 中文关键词扩展（~40 新词：恢复/没做完/参考/借鉴/决策依据/怎么修/搞不定/出错了/排查 等）
- 否定检测：`_NEGATION_PREFIX_RE`（没有/不是/别/未/无/非），window=5 字符
- 疑问句排除：`_INTERROGATIVE_RE`（有没有/是不是/会不会/能不能/可不可以）
- 英文词形变体：crashed/failed/failure 等

**测试**：~30 个新测试（中文/否定/疑问句/英文回归），全量 172 passed

---

## L3: 检索充分性检查 ✅

### 已实现（`9de891cb`）

- `check_retrieval_sufficiency()` 函数
- 元认知提示注入（不阻塞），触发条件：results < 3 AND intent_confidence >= 0.7
- 中英双语跟随 query
- 两种提示强度（empty vs partial）
- 37 新测试 + 241 回归全通过

---

## A1-3: LLM 注入 Store 直接索引路径 ✅

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/store.py` | `__init__` 加 `constraint_llm_call`；`index_file` 事务外分类 |
| `src/opensquilla/memory/manager.py` | `_make_constraint_llm_call()` 适配器 + 传参 |
| `src/opensquilla/gateway/boot.py` | 传 `provider_selector` 给 `build_memory_managers` |
| `tests/test_memory/test_constraint_annotation.py` | 9 个 A1-3 新测试 |

### 测试结果

```
tests/test_memory/test_constraint_annotation.py: 140 passed
```

---

## D11: Usage Tracking + D5: Dream 评分增强 ✅

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/store.py` | `chunk_usage` 表 + `record_chunk_usage()` + `get_usage_stats()` + `get_dominant_constraint_types()` |
| `src/opensquilla/memory/retrieval.py` | `usage_tracking_enabled` flag + search() 非阻塞写入 |
| `src/opensquilla/gateway/config.py` | `memory.experimental.usage_tracking` |
| `src/opensquilla/memory/dream/ranking.py` | D5 增强评分公式 + stability/cross_task 项 |
| `src/opensquilla/memory/dream/runner.py` | 注入 usage_stats + constraint_types |
| `src/opensquilla/memory/dream_factory.py` | `memory_stores` 接线 |
| `src/opensquilla/gateway/boot.py` | 传 `memory_stores` 给 factory |
| `tests/test_memory/test_usage_tracking.py` | 24 个新测试 |

### 测试结果

```
tests/test_memory/test_usage_tracking.py: 24 passed
Total regression: 616 passed, 7 skipped (1 pre-existing failure unrelated)
```

---

## D10: Dream 增量 Diff ✅

### 改动文件

| 文件 | 变更 |
|------|------|
| `src/opensquilla/memory/dream/candidates.py` | `known_observations` 参数 + `(source_path, content_hash)` 去重 |
| `src/opensquilla/memory/dream/runner.py` | evidence store hash 提取 + `files_skipped_unchanged` |
| `tests/test_memory/test_dream_dedup.py` | 6 个新测试 |

### 测试结果

```
tests/test_memory/test_dream_dedup.py: 6 passed
tests/test_memory_dream_runner.py + paths + handler: 17 passed (no regression)
```

---

## 后议项

| # | 内容 | 优先级 |
|---|------|--------|
| Q1 | Pattern 跨 session 匹配 | 🟢 长期 |
| A1-D | A1 动态自适应路由（阈值/成本预算/反馈回路） | 🟢 长期 |
