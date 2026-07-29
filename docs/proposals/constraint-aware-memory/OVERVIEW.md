# Constraint-Aware Memory: 整体设计总览

> **Status**: Active development
> **Branch**: `feature/constraint-aware-memory`
> **Base**: `origin/main`
> **Date**: 2026-07-29
> **Authors**: KunYu + OpenSquilla
> **Upstream**: https://github.com/opensquilla/opensquilla
> **Fork**: https://github.com/HuaXiawithMoon/opensquilla.git

---

## 0. 核心命题

> **Compaction 是在资源耗尽时"怎么丢得少"，Memory 是在资源充足时"怎么组织得对"。**
>
> **压缩 = 在特定目的下，信息效用 preserved 的语义等价变换。**

当前 OpenSquilla 的 memory 系统是一个**文档检索系统**（hybrid search + temporal decay + MMR + evidence-gated promotion）。本提案将其演进为**推理资源管理系统**：不只找"和 query 语义相似的片段"，而是找"能让 agent 正确完成当前推理的信息"。

三个诊断出的结构性缺陷：

1. **目的盲**：压缩/检索不知道结果要给谁用、用来做什么
2. **无自我模型**：不知道下游推理依赖哪些条件
3. **无等效验证**：压缩/检索后不检验"基于这个结果，推理还能不能走通"

---

## 1. 四层架构

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
│ D12: Compaction Anchor (experimental, default off)               │
│   问: 摘要中哪些地方值得展开原文？                                │
│   做: compaction summary 嵌入 [anchor:N:entry_NNN]               │
├─────────────────────────────────────────────────────────────────┤
│ L0: 归档 Transcript 可搜索 (infrastructure, always on)           │
│   修复: 让模型能搜索被压缩掉的原始对话                            │
│   意义: 为 L1-L3 提供 ground truth 参照物                        │
└─────────────────────────────────────────────────────────────────┘
```

### 层间依赖

```
L0 (infrastructure)     ── 无依赖
D12 (compaction anchor) ── 依赖 L0（archived entries 存在）
L1 (annotation)         ── 无依赖（可独立运行）
L2 (routing)            ── 依赖 L1（需要 constraint_type 元数据）
L3 (sufficiency)        ── 依赖 L2（需要路由后的结果）
```

---

## 2. 设计原则

| # | 原则 | 理由 |
|---|------|------|
| P1 | 每层的"关闭"状态精确等于当前行为 | 实验功能绝不回退 |
| P2 | API 签名不变 | `memory_search`、`session_search` 保持契约 |
| P3 | 数据只增不改 | 新列/新表；不删不改现有 schema |
| P4 | 三级降级 | 分类失败 → 默认 "fact" → boost 1.0 → 当前行为 |
| P5 | 隐私通过模型认同实现，不通过信息封锁 | 模型应有完整历史访问权；负责任使用是 alignment 问题 |
| P6 | Feature branch only | 不合入 main，直到验证通过且 KunYu 明确批准 |
| P7 | 错误分类保护 | 置信度 < 0.6 → 不 boost（最坏 = 中性，不是 debuff） |
| P8 | 模型主动性 | anchor 机制让模型自己判断是否需要展开 |

---

## 3. 约束类型 Ontology

### 核心 6 类（v0.7 激活）

| 类型 | 语义 | 典型召回场景 | FlushCandidate.kind 映射 |
|------|------|------------|------------------------|
| `fact` | 客观事实 | 通用召回 | `fact` |
| `event` | 时间性事件 | "上周做了什么？" | `event` |
| `preference` | 用户偏好 | 输出格式、风格选择 | `preference` |
| `decision` | 已做出的决策 + 理由 | "为什么选 X？" | `decision` |
| `procedure` | 操作步骤 | "怎么跑测试？" | `procedure` |
| `goal` | 目标/意图 | "继续"、"接着上次" | `goal`, `todo` |

### 扩展 4 类（保留枚举，v0.8 再激活）

| 类型 | 语义 | 推迟理由 |
|------|------|---------|
| `assumption` | 隐含假设 | 需要推理链分析，启发式难以准确识别 |
| `constraint` | 硬约束 | 与 assumption 边界模糊，需使用数据验证 |
| `anti_pattern` | 反面模式 | 与 pattern 是同一结构的两面 |
| `pattern` | 可迁移问题结构 | 匹配机制未就绪，只有标签语义 |

---

## 4. 各层设计摘要

### L0: 归档 Transcript 可搜索 ✅

- **机制**：`compacted_transcript_entries` 表增加 FTS5 索引 + 触发器
- **API**：`search_transcript(include_archived=True)`
- **工具**：`session_search` 暴露 `include_archived` 参数
- **状态**：始终启用（基础设施修复，非实验功能）

### D12: Compaction Anchor ✅

- **机制**：compaction summary 嵌入 `[anchor:N:entry_NNN]` 标记
- **展开**：`session_search(anchor="N:entry_NNN", session_id="...")` 精确查找
- **核心原则**：模型主动判断是否需要展开（不自动展开）
- **Feature flag**：`compaction.anchor_enabled`（`CompactionLlmConfig`）

### L1: 约束类型标注 ✅

- **触发点 A**：memory 文件 sync 时 LLM 分类（`index_file` 路径）
- **触发点 B**：flush candidate 提取时 `CandidateKind` → `constraint_type` 映射
- **Signal Gate**：低信号 chunk（<20字、纯工具输出、心跳）跳过分类
- **降级链**：LLM → 启发式 → 默认 "fact"
- **置信度保护**：confidence < 0.6 → L2 不应用 boost
- **Feature flag**：`memory.experimental.constraint_annotation`

### L2: 约束感知检索路由 ⬜

- **机制**：query 意图分类 → 按 constraint_type 加权排序
- **Boost 范围**：[0.85, 1.8]（永不完全抑制）
- **Provenance Marker**：结果带 `<memory_result type="..." confidence="...">` 标签
- **Feature flag**：`memory.experimental.constraint_routing`

### L3: 检索充分性检查 ⬜

- **机制**：检索后注入元认知提示（不阻塞）
- **触发条件**：`results < 3 AND intent_confidence > 0.7 AND constraint_routing_enabled`
- **Feature flag**：`memory.experimental.sufficiency_check`

---

## 5. 配置

```toml
# Compaction Anchor (D12) — 已实现
[compaction]
anchor_enabled = false

# Memory Experimental Features (L1/L2/L3) — 待实现
[memory.experimental]
constraint_annotation = false   # L1
constraint_routing = false      # L2
sufficiency_check = false       # L3
```

L0 无开关，始终启用（基础设施修复）。

---

## 6. 版本路线图

| 版本 | 范围 | 门控 | 状态 |
|------|------|------|------|
| v0.6.x | L0（归档 FTS） | 始终启用 | ✅ 已实现 |
| v0.6.x | D12（Compaction Anchor） | Config gate | ✅ 已实现 |
| v0.7.0 | L1 + L2 作为 experimental | Config gate | 🔶 L1 已实现 / L2 已实现 |
| v0.7.x | 收集约束分类准确率数据 | Telemetry | ⬜ |
| v0.8.0 | 扩展 4 类激活 + L1+2 默认开启 | 数据驱动 | ⬜ |
| v0.9.0 | L3 作为 experimental | Config gate | ⬜ |
| v1.0.0 | 评估用充分性检查替代 coverage check | 验证 | ⬜ |

---

## 7. 文档清单

| 文档 | 路径 | 状态 |
|------|------|------|
| 整体设计总览 | `OVERVIEW.md` | ✅ 本文档 |
| 进度跟踪 | `PROGRESS.md` | ✅ |
| 原始设计提案 | `DESIGN.md` | v1.1 |
| 对齐记录 | `ALIGNMENT_2026-07-29.md` | ✅ |
| Codex 对比分析 | `COMPARISON_CODEX.md` | ✅ |
| L0 实现 spec | `SPEC_L0_ARCHIVED_TRANSCRIPT_SEARCH.md` | ✅ 已实现 |
| D12 实现 spec | `SPEC_D12_COMPACTION_ANCHOR.md` | ✅ 已实现 |
| L1 实现 spec | `SPEC_L1_CONSTRAINT_ANNOTATION.md` | ✅ 已实现 |
| L2 实现 spec | `SPEC_L2_CONSTRAINT_ROUTING.md` | ✅ 已实现 |

---

## 8. 隐私立场

本提案**明确拒绝**以信息封锁作为隐私机制。

> 隐私保护应通过模型认同和 alignment 实现，而非通过系统层面限制历史可见性。

模型**应当**有完整对话历史的访问权（包括归档 transcript）。负责任地使用这些访问权是 alignment 层面的问题（SOUL.md、安全训练），不是存储或检索层面的问题。

---

## 9. 与现有系统的关系

### 9.1 Compaction
本提案**不修改** compaction 核心逻辑。D12 是附加机制：
- D12 给 compaction summary 嵌入 anchor，让模型能展开原文
- L0 给 compaction 提供 ground truth 参照
- 若 memory 已有结构化 `decision`/`constraint`/`goal`，compaction 的 obligation 提取可引用

### 9.2 Dream
Promotion scoring 计划增加附加项（D5，后议）：
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

### 9.3 Flush
`session_flush.py` 的 `CandidateKind` 直接映射到统一 ontology。无行为变更。

### 9.4 Codex 对比借鉴

| 借鉴 | 来源 | 纳入位置 |
|------|------|---------|
| No-op Signal Gate | Phase 1 prompt "No-op is allowed and preferred" | D8 (L1) |
| Provenance Markers | `ContextualUserFragment` + markers | D9 (L2) |
| Citation/Anchor | `MemoryCitation` + `rollout_ids` | D12 |
| Progressive Disclosure | memory_summary → MEMORY.md → skills | 中期三层注入 |
| Usage Telemetry | `MemoriesUsageKind` 5 种追踪 | D11（后议） |

---

## Appendix: 关键源文件

| 文件 | 角色 |
|------|------|
| `src/opensquilla/memory/store.py` | SQLite + FTS5 + sqlite-vec 存储 |
| `src/opensquilla/memory/retrieval.py` | Hybrid 检索 + temporal decay + MMR |
| `src/opensquilla/memory/types.py` | MemorySource, SearchMode, SearchIntent |
| `src/opensquilla/memory/session_flush.py` | LLM flush + CandidateKind |
| `src/opensquilla/memory/dream/ranking.py` | Promotion 评分 |
| `src/opensquilla/session/storage.py` | Transcript 存储 + FTS + D12 anchor |
| `src/opensquilla/session/compaction.py` | Context 压缩 + D12 anchor |
| `src/opensquilla/gateway/config.py` | 所有配置定义 |
