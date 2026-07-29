# OpenHuman Memory System vs Constraint-Aware Memory: 对比学习

> **Date**: 2026-07-29
> **Purpose**: 从 OpenHuman (tinyhumansai) 的记忆系统实现中提取设计洞见，对照我们的 constraint-aware-memory 提案，识别可借鉴的模式和差异化方向。
> **Source**: `C:\Users\KunYu\workspace\harness\openhuman` (Rust core + React/Tauri)

---

## 1. 项目概览

**OpenHuman** 是 TinyHumansAI 的"个人 AI 超级智能"桌面应用，定位为：

```
三大支柱:
1. 大脑 (Brain)       — 构建持久化本地记忆，记住你的世界
2. 编排器 (Orchestrator) — 在持久图上运行 agent 舰队
3. 深度研究者 (Deep Researcher) — 在你问完之前扫描你的数据和网络
```

技术栈：Rust 核心（`src/openhuman/`，100+ 子模块）+ React/Tauri 前端（`app/`）。
记忆相关模块约 15 个顶级目录，是项目中最大的子系统。

---

## 2. 记忆系统架构总览

### 2.1 OpenHuman 的六层记忆栈

```
┌─────────────────────────────────────────────────────────────────────┐
│ 使用层:                                                             │
│   memory_tree retrieval tools (8 种 mode)                           │
│   memory_tools (agent 读写工具)                                     │
│   subconscious (离线反思 + 决策)                                    │
│   learning (自我学习 → prompt 注入)                                 │
├─────────────────────────────────────────────────────────────────────┤
│ 编排层: memory/ (Orchestration layer)                               │
│   sync orchestration (cron/manual → memory_sync)                    │
│   query orchestration (dispatching → memory_tree)                   │
│   remember (chat/upload/LLM-thought → ingest)                      │
│   ingest pipeline (source → canonicalise → chunk → score → persist)│
├─────────────────────────────────────────────────────────────────────┤
│ 树结构层: memory_tree/ (Generic tree mechanics)                     │
│   tree/ — bucket-seal, flush, registry                              │
│   retrieval/ — walk, drill_down, fetch_leaves, cover_window,       │
│                search_entities, smart_walk (E2GraphRAG)             │
│   score/ — per-chunk scoring + embedding + entity extraction        │
│   summarise.rs — L_n → L_{n+1} 文本摘要                            │
├─────────────────────────────────────────────────────────────────────┤
│ 同步层: memory_sync/                                                │
│   Composio providers (Gmail/Slack/Notion/ClickUp/Linear/GitHub)     │
│   MCP providers + workspace watcher                                 │
├─────────────────────────────────────────────────────────────────────┤
│ 存储层: memory_store/                                               │
│   raw / chunks / entities / trees / vectors / kv / contacts         │
│   SQLite + 磁盘 md 文件                                             │
├─────────────────────────────────────────────────────────────────────┤
│ 反思层: subconscious/ + learning/                                   │
│   subconscious: observe → prepare → reflect → commit (cron loop)    │
│   learning: candidates → stability detector → facets → prompt       │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 OpenSquilla 当前 + 我们的提案

```
┌─────────────────────────────────────────────────────────────────────┐
│ 使用层: memory_search tool → 注入 system prompt                     │
│         session_search tool → transcript 全文检索                    │
├─────────────────────────────────────────────────────────────────────┤
│ 检索层: MemoryRetriever                                             │
│   hybrid search (vector 0.7 + FTS5 0.3)                            │
│   temporal decay + MMR diversity rerank                             │
│   [L2 ✅] constraint-aware routing + boost                          │
├─────────────────────────────────────────────────────────────────────┤
│ 索引层: LongTermMemoryStore                                         │
│   SQLite + FTS5 + sqlite-vec                                        │
│   [L0 ✅] compacted_transcript_entries FTS                           │
│   [L1 ✅] constraint_type 标注                                      │
├─────────────────────────────────────────────────────────────────────┤
│ 整合层: Dream (cron-scheduled)                                      │
│   evidence-gated promotion                                          │
├─────────────────────────────────────────────────────────────────────┤
│ 会话层: SessionManager                                              │
│   transcript_entries → compacted_transcript_entries                 │
│   compaction (context window 管理)                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 核心设计哲学对比

| 维度 | OpenHuman | OpenSquilla (当前) | 我们的提案 |
|------|----------|-------------------|-----------|
| **记忆本质** | "大脑"——持久化你世界的知识图谱 | 文档检索系统 | 推理资源管理系统 |
| **存储模型** | 分层树（bucket-seal → L0→L1→L2 摘要） | 扁平 chunks + FTS5 | 同 OpenSquilla + 约束标注 |
| **检索模型** | 8 种 mode-driven 工具 + E2GraphRAG | hybrid search top-k | 同 + constraint boost |
| **写入触发** | 实时 ingest + cron sync + subconscious | 实时 flush + cron Dream | 同 OpenSquilla |
| **学习/反思** | stability detector + subconscious 反思循环 | 无（Dream 是评分，不是反思） | 无（L1 是分类，不是学习） |
| **注入模型** | 分层 prompt sections (Learned + Profile + MemoryAccess) | 检索 top-k 注入 | 同 + constraint routing + L3 充分性提示 |
| **质量控制** | per-chunk scoring (admission gate) + entity extraction | 无显式准入 | L1 signal gate + 约束分类 |

---

## 4. 关键设计模式提取

### 4.1 分层树摘要 (Bucket-Seal + Summarise)

OpenHuman 的 `memory_tree` 使用**桶密封（bucket-seal）**机制：

```
chunk → L0 leaf → when bucket full → seal → LLM summarise → L1 summary
L1 summary → when bucket full → seal → LLM summarise → L2 summary
...
```

构建了一个**可钻取的信息金字塔**，对应 8 种检索 mode：

| Mode | 功能 | 粒度 |
|------|------|------|
| `search_entities` | 模糊匹配实体名 → canonical id | 实体级 |
| `query_source` | 按源类型 + 时间窗口检索 | 源级 |
| `query_global` | 跨源 digest（时间窗口） | 全局级 |
| `query_topic` | 实体范围跨树检索 | 主题级 |
| `drill_down` | 从粗粒度摘要深入一层 | 层级 |
| `cover_window` | 覆盖时间窗口的最小节点集 | 时间级 |
| `fetch_leaves` | 拉取原始 chunks（cap 20） | 叶子级 |
| `smart_walk` | E2GraphRAG：提取实体 → 路由（实体图/摘要）→ 排序证据 | 混合 |

**对我们的启示**：
- 当前 OpenSquilla 没有摘要层级——所有 chunk 是扁平的
- `cover_window` 概念（"覆盖时间窗口的最小节点集"）对应我们的 temporal decay + MMR 去重
- `smart_walk` 的"无 LLM 确定性检索"思路值得借鉴：提取 query 实体 → 路由到实体图或摘要搜索 → 返回排序证据
- **长期方向**：如果 compaction 时生成摘要层级，L3 充分性检查发现"信息不够"时可以自动 drill_down

### 4.2 Per-Chunk 评分准入 (Score Gate)

`memory_tree/score/` 在 chunk 进入树之前做**准入评分**：

```
score_chunk:
  1. Entity extraction (regex + LLM NER)
  2. Cheap signals (token count, unique words, metadata weight, source weight, interaction tags, entity density)
  3. Optional borderline LLM importance call
  4. Admission gate: score < DROP_THRESHOLD → 丢弃
  5. Persist score rationale + entity index
```

**与我们的 L1 Signal Gate 对比**：

| 维度 | OpenHuman Score Gate | 我们的 L1 Signal Gate |
|------|---------------------|---------------------|
| 目的 | 决定是否保留 chunk | 决定是否值得 LLM 分类 |
| 信号 | 6 种 cheap signals + LLM | 长度 + 自然语言检测 |
| 输出 | 连续分数 [0,1] + 准入决策 | 布尔（classify / skip） |
| 实体提取 | 有（regex + LLM NER） | 无 |

**可借鉴**：
- 我们的 signal gate 可以更丰富（加入 entity density、interaction tags）
- 连续分数比布尔决策更灵活——未来 L1 可以输出 `classification_priority` 分数

### 4.3 稳定性检测器 (Stability Detector)

OpenHuman 的 `learning` 模块有精妙的 **stability 公式**：

```
stability(class, key) = base × cue_mult × user_state_mult

base = Σ(cue_family.weight() × exp(-Δt / half_life(class)) × ln(1 + evidence_count))
cue_mult = 2.0 if Explicit, else 1.0
user_state_mult = ∞ if Pinned, 0 if Forgotten, 1 otherwise
```

**关键参数**：

| Class | Half-life | Budget (max Active) |
|-------|-----------|-------------------|
| Identity | 90 天 | 4 |
| Veto | 60 天 | 3 |
| Tooling | 30 天 | 5 |
| Goal | 30 天 | — |
| Style | 14 天 | 4 |
| Channel | 7 天 | — |

**生命周期状态**：Candidate → Provisional (τ=0.7) → Active (τ=1.5) → Dropped (<0.4)

**与我们的对比**：

| 维度 | OpenHuman Stability | 我们的 constraint_stability |
|------|-------------------|---------------------------|
| 证据来源 | 多次会话的 candidate 积累 | 跨 session 的约束类型计数 |
| 时间衰减 | `exp(-Δt / half_life)` 连续 | 暂未定义 |
| 用户干预 | Pin/Forgot 状态 | frontmatter 覆盖 |
| 冲突解决 | 按 stability 排序 + per-class budget | 按约束类型优先级 |
| 触发 | 30 min 周期 + 事件驱动 debounce | Dream cron |

**可借鉴**：
- `cue_mult`（显式 vs 隐式）：用户 frontmatter 覆盖 = 显式 (×2.0)，自动分类 = 隐式 (×1.0)
- per-class half_life：不同约束类型可以有不同的衰减速率（`anti_pattern` 应该比 `fact` 更持久）
- 生命周期状态：约束类型标注也可以有 Candidate → Confirmed → Stable 的晋升路径

### 4.4 Subconscious 离线反思循环

OpenHuman 的 `subconscious` 是一个**离线 cron 循环**，每个 tick：

```
observe: diff connected sources vs baseline checkpoint
  ↓ (quiet → commit → done)
  ↓ (changed → prepare → reflect)
prepare: run context_scout over diff (read-only)
reflect: slim decision agent → to-dos, goals, notify, delegate
commit: re-checkpoint baseline
```

**关键设计决策**：
- 每个 world 一个 profile（memory, tinyplace），复用相同的 generic runner
- `TICK_TIMEOUT` (30 min) 防死锁
- per-instance tick lock + 5s acquisition skip
- **advance-baseline-only-on-success**（失败不回滚，只重试）
- **quiet-tick short-circuit**（没变化时静默跳过）
- generation/supersede counter（被取代的 tick 不会 commit）

**与我们的 Dream 对比**：

| 维度 | OpenHuman Subconscious | 我们的 Dream |
|------|----------------------|-------------|
| 触发 | cron（可配置间隔，min 5 min） | cron |
| 观察对象 | 世界 diff（memory_diff vs baseline） | 所有 chunks（全量扫描） |
| 行为 | LLM 决策（to-dos, goals, notify, delegate） | 评分 + promotion |
| 失败处理 | 不更新 baseline，下次重试 | 无显式机制 |
| 静默跳过 | 有（diff 为空时） | 无 |
| 增量性 | 增量（只看 diff） | 全量 |

**可借鉴**（中期 Dream 改进）：
- **静默跳过**：Dream 如果没有新 chunks，跳过整个 tick
- **增量 diff**：Dream 只处理"自上次以来变化的部分"
- **失败不回滚**：Dream 评分失败时不更新状态，下次重试
- 但定位不同：OpenHuman subconscious 是**高层推理**（决策），我们的 Dream 是**低层评分**（promotion）

### 4.5 分层 Prompt 注入

OpenHuman 的 `learning/prompt_sections.rs` 定义了**分层注入**：

```rust
// 三个独立 PromptSection，按顺序注入 system prompt：

LearnedContextSection:
  ## Learned Context
  ### Recent Observations
  - [observation 1]
  ### Recognized Patterns
  - [pattern 1]

UserProfileSection:
  ## Your standing preferences
  - [preference 1]

MemoryAccessSection (静态编译时指令):
  "Before answering questions about named people, projects, or prior
   decisions, call memory_recall/memory_search first."
```

**关键模式**：
- 每个 section 是独立的 `PromptSection` trait 实现
- **空数据时 section 不输出任何内容**（`return Ok(String::new())`）
- 注入顺序固定（section 链）
- `MemoryAccessSection` 是**静态指令**——不是数据，而是告诉模型"什么时候该检索记忆"

**对我们的 L3 直接参考**：
- L3 的"检索充分性检查"本质上是一个**动态注入的 section**
- 类似 `MemoryAccessSection` 但更智能：根据检索结果动态决定是否注入
- 空结果时也应该注入（告诉模型"没找到什么，考虑换策略"）
- 注入位置：在检索结果之后、模型回复之前

### 4.6 实体索引 + E2GraphRAG

OpenHuman 的 `memory_tree/score/extract/` 做实体提取：

```
EntityExtractor trait:
  RegexEntityExtractor — 机械标识符（email, URL, handle, hashtag）
  LlmEntityExtractor — 语义 NER + importance rating
  CompositeExtractor — 链式组合
```

提取后存入 `mem_tree_entity_index`（倒排索引：entity_id → node_id）。

`smart_walk` 检索流程：
```
query → extract entities → route:
  if entities found → entity-graph search (local)
  else → dense-summary search (global)
→ rank evidence hits → return
```

**关键**：smart_walk 是**无 LLM 的确定性检索**——提取实体用 regex/NER，路由用规则，排序用分数。

**对我们的启示**：
- 当前 OpenSquilla 没有实体概念——检索完全依赖语义相似度
- L2 的 constraint routing 是"意图→类型"映射，OpenHuman 的 smart_walk 是"实体→图/摘要"路由
- 两者可以组合：先做 constraint routing（选类型），再做 entity routing（选具体 chunk）
- **长期方向**：在 chunk 索引时提取实体，建立倒排索引

---

## 5. 差异化优势总结

### 5.1 OpenHuman 有而我们缺的

| OpenHuman 设计 | 我们当前状态 | 建议优先级 |
|--------------|------------|-----------|
| **分层树摘要** (bucket-seal + summarise) | 扁平 chunks | 长期 |
| **稳定性检测器** (stability formula + half-life) | 无 | 中期（Dream 改进） |
| **Subconscious 反思循环** | 无 | 长期 |
| **实体索引** (entity_index + smart_walk) | 无 | 长期 |
| **多源同步** (12+ Composio providers) | 无 | 不适用（定位不同） |
| **分层 Prompt 注入** (PromptSection trait) | 简单 top-k 注入 | 短期（L3） |
| **Per-chunk 评分准入** (6 signals + admission gate) | 简单 signal gate | 中期 |
| **静默跳过** (quiet-tick short-circuit) | Dream 总是运行 | 短期 |
| **增量 diff** (baseline checkpoint) | Dream 全量扫描 | 中期 |

### 5.2 我们有而 OpenHuman 缺的

| 我们的设计 | OpenHuman 对应 | 差异 |
|-----------|-------------|------|
| **约束类型 ontology (10 类)** | 无显式分类（LLM 自由判断） | 我们更结构化、可审计、可校准 |
| **Constraint-aware routing (L2)** | 无——检索是纯语义的 | 我们引入了"推理需求→信息类型"的映射 |
| **充分性检查 (L3)** | 无——注入后不验证 | 我们闭环验证"这些信息够不够用" |
| **Compaction archive 可搜索 (L0)** | 无压缩概念（只增不删） | 我们在压缩后仍提供结构化搜索 |
| **三级降级链** (LLM→启发式→默认) | 无 | 我们更细粒度 |
| **用户 frontmatter 覆盖** | 无——用户不能直接标注记忆类型 | 我们允许人类专家修正自动分类 |
| **D9 Provenance Marker** | 无 | 检索结果带约束类型标签 |

---

## 6. 对 L3 设计的具体启示

基于 OpenHuman + Codex 的设计模式，L3"检索充分性检查"的设计建议：

### 6.1 注入位置：工具返回层

在 `memory_tools.py` 的 search 函数返回结果文本之前，追加一个独立的 section。
类似 OpenHuman 的 `MemoryAccessSection` 但**动态生成**：

```python
# 触发条件：results < 3 AND intent_confidence > 0.7
if should_inject_sufficiency_note(results, intent):
    note = format_sufficiency_note(intent, results_count, confidence)
    # 追加在检索结果文本之后
    output_text += "\n\n" + note
```

### 6.2 注入格式：结构化标记

参考 Codex 的 `ContextualUserFragment`（有界、可识别、不可变）：

```xml
<memory_sufficiency_check intent="avoid_failure" results="2" confidence="0.85">
当前检索结果可能不足以完全覆盖你的需求。
建议：调整关键词重新检索 / 使用 session_search / 告知用户需要更多上下文。
</memory_sufficiency_check>
```

### 6.3 空结果处理

参考 OpenHuman 的"空 section 不输出"模式，但**反转**：
- 有结果且充分 → 不注入（静默）
- 有结果但不充分 → 注入提示
- 无结果 → 注入更强的提示（"未找到相关记忆，建议..."）

### 6.4 双语策略

建议**跟随 query 语言**（而非固定双语），因为：
- 我们的约束 ontology 是中英双语设计的
- 但提示文本应该和用户的 query 语言一致，减少认知负担
- 实现：检测 query 中 CJK 字符比例 > 0.3 → 中文提示，否则英文

---

## 7. 三方对比总结

| 维度 | Codex | OpenHuman | 我们 (Constraint-Aware) |
|------|-------|----------|----------------------|
| **核心问题** | "写什么"（写入端质量控制） | "如何持久化世界"（存储和结构） | "找什么"（读取端需求匹配） |
| **记忆本质** | 行为改变工具 | 大脑/知识图谱 | 推理资源 |
| **检索创新** | 无（纯 grep/keyword） | 分层树 + E2GraphRAG | 约束路由 + 充分性检查 |
| **写入创新** | 两阶段管线 + signal gate | 评分准入 + 实体提取 | 约束分类 + signal gate |
| **反思/学习** | 无 | subconscious + stability detector | Dream（评分，非反思） |
| **注入模型** | ContextualUserFragment (有界标记) | PromptSection 链 (分层) | top-k + constraint boost + L3 |
| **增量性** | git-baseline diff | baseline checkpoint + quiet-tick | 无（全量） |
| **用户控制** | 无 | Pin/Forgot | frontmatter 覆盖 |

> **一句话**：Codex 管"写"，OpenHuman 管"存"，我们管"找"。完整系统需要三者。

---

## 附录 A: OpenHuman 关键文件索引

| 路径 | 角色 |
|------|------|
| `src/openhuman/memory/README.md` | 记忆栈架构总览 |
| `src/openhuman/memory/query/mod.rs` | 统一检索工具（8 种 mode） |
| `src/openhuman/memory/ingestion/README.md` | 摄取管线设计 |
| `src/openhuman/memory_tree/README.md` | 树结构设计文档 |
| `src/openhuman/memory_tree/retrieval/README.md` | 检索层设计（6 个 LLM-callable 原语） |
| `src/openhuman/memory_tree/score/README.md` | 评分管线设计 |
| `src/openhuman/memory_store/` | 存储原语：SQLite + 磁盘 |
| `src/openhuman/memory_sync/README.md` | 外部源同步设计 |
| `src/openhuman/subconscious/README.md` | Subconscious 架构文档 |
| `src/openhuman/subconscious/profiles/memory.rs` | Memory world profile 实现 |
| `src/openhuman/learning/README.md` | 学习子系统 Phase 1-4 文档 |
| `src/openhuman/learning/prompt_sections.rs` | 分层 prompt 注入 |
| `src/openhuman/learning/stability_detector.rs` | 稳定性公式 + 阈值 + 预算 |
| `AGENTS.md` | 项目架构总览 + 开发规则 (45KB) |

## 附录 B: OpenHuman 记忆相关测试文件

| 测试 | 覆盖 |
|------|------|
| `tests/memory_roundtrip_e2e.rs` | 记忆读写往返 |
| `tests/memory_fast_retrieve_e2e.rs` | 快速检索 |
| `tests/memory_golden_parity_e2e.rs` | 黄金标准一致性 |
| `tests/memory_graph_sync_e2e.rs` | 图同步 |
| `tests/memory_sources_e2e.rs` | 多源 |
| `tests/memory_sync_pipeline_e2e.rs` | 同步管线 |
| `tests/memory_tree_summarizer_e2e.rs` | 树摘要 |
| `tests/memory_artifacts_e2e.rs` | 制品 |
| `tests/autocomplete_memory_e2e.rs` | 自动补全记忆 |
| `tests/transcript_search_e2e.rs` | Transcript 搜索 |
| `tests/learning_phase4_integration_test.rs` | 学习 Phase 4 集成 |
| `tests/subconscious_*_e2e.rs` (4 个) | Subconscious 各层 |
